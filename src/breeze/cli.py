"""Breeze 命令行接口。

提供 breeze ask 单轮问答与 breeze chat 交互式多回合会话，
运行 Agent 并输出答案与 Token 摘要。
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from breeze import __version__
from breeze.agent import Agent, AgentEvent
from breeze.chat import run_chat
from breeze.chat_commands import ChatController
from breeze.config import settings
from breeze.llm import LLMClient
from breeze.session import SessionStore
from breeze.trace import RunTrace

app = typer.Typer(
    name="breeze",
    help="Token-efficient coding agent.",
    epilog=(
        "Examples:\n"
        '  breeze ask "解释 src/breeze/agent.py 的作用" --max-steps 4\n'
        '  breeze ask "修复 bug" --trace-name fix-bug\n'
        "  breeze chat --resume latest\n"
        "  breeze chat --list --search safe_path\n"
        "\n"
        "Run 'breeze ask --help' or 'breeze chat --help' for all options."
    ),
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
error_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    """打印版本后退出。"""
    if value:
        console.print(f"breeze {__version__}")
        raise typer.Exit()


@app.callback()
def breeze_callback(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="print version and exit",
    ),
) -> None:
    """Register a callback so Typer keeps the `breeze ask <question>` command group."""


@app.command()
def ask(
    question: str = typer.Argument(..., help="questions to ask Agent"),
    repo: Path = typer.Option(
        None,
        "--repo",
        "-r",
        help="root directory of the repository, current working directory by default",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    model: str = typer.Option(
        None,
        "--model",
        "-m",
        help="default model",
    ),
    max_steps: int = typer.Option(
        None,
        "--max-steps",
        help="max cycling steps (default: from breeze.toml or DEFAULTS)",
        min=1,
        max=50,
    ),
    trace_dir: Path = typer.Option(
        Path("traces"),
        "--trace-dir",
        help="trace file output directory",
        file_okay=False,
        dir_okay=True,
    ),
    trace_name: str = typer.Option(
        None,
        "--trace-name",
        help="trace filename prefix, useful when comparing runs in traces/",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="only outputs answers and does not display Token summaries",
    ),
) -> None:
    """Ask one question and print the answer (single turn)."""
    repo_root = (repo or Path.cwd()).resolve()
    if not repo_root.is_dir():
        error_console.print(f"[red]invalid repository directory: {repo_root}[/red]")
        raise typer.Exit(code=1)

    original_repo_root = settings.repo_root
    settings.repo_root = repo_root

    try:
        try:
            llm = LLMClient(model=model)
        except ValueError as exc:
            error_console.print(f"[red]LLM configuration error: {exc}[/red]")
            raise typer.Exit(code=1) from exc

        with RunTrace(trace_dir=trace_dir, name=trace_name) as trace:
            progress = _TurnProgress(console)
            agent = Agent(llm=llm, trace=trace, max_steps=max_steps, on_event=progress.on_event)

            if not quiet:
                console.print(f"[dim]model: {llm.model}[/dim]")
                console.print(f"[dim]repo: {repo_root}[/dim]")
                console.print()

            try:
                answer = agent.run(question)
            finally:
                progress.stop()

            if quiet:
                console.print(answer)
            else:
                console.print(_answer_panel(answer))

            if not quiet:
                _print_summary(trace.summary())

    except typer.Exit:
        raise
    except KeyboardInterrupt:
        error_console.print("\n[yellow]user interrupt[/yellow]")
        raise typer.Exit(code=130) from None
    except Exception as exc:
        error_console.print(f"[red]runtime error: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    finally:
        settings.repo_root = original_repo_root


@app.command()
def chat(
    repo: Path = typer.Option(
        None,
        "--repo",
        "-r",
        help="root directory of the repository, current working directory by default",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    model: str = typer.Option(
        None,
        "--model",
        "-m",
        help="default model",
    ),
    max_steps: int = typer.Option(
        None,
        "--max-steps",
        help="max cycling steps per turn (default: from breeze.toml or DEFAULTS)",
        min=1,
        max=50,
    ),
    trace_dir: Path = typer.Option(
        Path("traces"),
        "--trace-dir",
        help="trace file output directory, one trace file per turn",
        file_okay=False,
        dir_okay=True,
    ),
    trace_name: str = typer.Option(
        None,
        "--trace-name",
        help="trace filename prefix; session id is used by default",
    ),
    resume: str = typer.Option(
        None,
        "--resume",
        help="continue a saved session by id, or 'latest' for the most recent one",
    ),
    session_dir: Path = typer.Option(
        None,
        "--session-dir",
        help="session storage directory, ~/.breeze/sessions by default",
        file_okay=False,
        dir_okay=True,
    ),
    list_sessions: bool = typer.Option(
        False,
        "--list",
        help="list saved sessions and exit",
    ),
    search: str = typer.Option(
        None,
        "--search",
        help="with --list, only show sessions whose questions or answers contain the keyword",
    ),
) -> None:
    """Start an interactive multi-turn session; /help lists in-session commands."""
    if list_sessions:
        _print_sessions(session_dir, search)
        return
    if search:
        error_console.print("[red]--search 只能与 --list 一起使用[/red]")
        raise typer.Exit(code=1)

    repo_root = (repo or Path.cwd()).resolve()
    if not repo_root.is_dir():
        error_console.print(f"[red]invalid repository directory: {repo_root}[/red]")
        raise typer.Exit(code=1)

    original_repo_root = settings.repo_root
    settings.repo_root = repo_root

    try:
        try:
            if resume:
                session = SessionStore.resume(resume, session_dir)
                saved = session.saved_config()
            else:
                session = None
                saved = {}
        except FileNotFoundError as exc:
            error_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        try:
            llm = LLMClient(model=model or saved.get("model"), temperature=saved.get("temperature"))
        except ValueError as exc:
            error_console.print(f"[red]LLM configuration error: {exc}[/red]")
            raise typer.Exit(code=1) from exc

        if session is None:
            session = SessionStore(session_dir, repo_root=repo_root, model=llm.model)

        controller = ChatController(
            llm=llm,
            llm_factory=lambda name, temperature: LLMClient(model=name, temperature=temperature),
            session=session,
            repo_root=str(repo_root),
            max_steps=max_steps if max_steps is not None else saved.get("max_steps"),
            max_edit_attempts=saved.get("max_edit_attempts"),
            temperature=saved.get("temperature"),
            profile=saved.get("profile"),
            trace_prefix=trace_name or saved.get("trace_prefix") or session.session_id,
            intra_turn_budget=saved.get("intra_turn_budget"),
            answer_preview=saved.get("answer_preview"),
            max_records=saved.get("max_records"),
            max_chars=saved.get("max_chars"),
        )

        console.print(
            f"[dim]model: {controller.llm.model} | repo: {repo_root} | "
            f"session: {session.session_id} | /help 查看命令[/dim]"
        )
        if resume:
            info = session.info()
            if info.repo_root and Path(info.repo_root) != repo_root:
                console.print(
                    f"[yellow]提醒：该会话原先在 {info.repo_root} 下运行，"
                    f"当前仓库是 {repo_root}，历史索引可能指向已不存在的文件。[/yellow]"
                )

        summaries: list[dict] = []

        def _on_turn_summary(summary: dict, turn: int) -> None:
            summaries.append(summary)
            console.print(
                f"[turn {turn}] 本轮 tokens：{summary['total_tokens']} "
                f"(prompt {summary['prompt_tokens']} / completion {summary['completion_tokens']})",
                style="dim",
                markup=False,
            )

        progress = _TurnProgress(console)
        exit_code = run_chat(
            agent_factory=lambda trace: Agent(
                llm=controller.llm,
                trace=trace,
                max_steps=controller.max_steps,
                max_edit_attempts=controller.max_edit_attempts,
                context=controller.build_context(),
                on_event=progress.on_event,
            ),
            trace_factory=lambda: RunTrace(trace_dir=trace_dir, name=controller.trace_prefix),
            input_fn=input,
            print_fn=lambda text: console.print(text, markup=False),
            session=session,
            answer_fn=lambda answer: console.print(_answer_panel(answer)),
            summary_fn=_on_turn_summary,
            command_fn=controller.handle,
            agent_setup_fn=controller.apply_to_agent,
            progress=progress,
        )
        if summaries:
            _print_chat_summary(summaries)
    except typer.Exit:
        raise
    except KeyboardInterrupt:
        error_console.print("\n[yellow]user interrupt[/yellow]")
        raise typer.Exit(code=130) from None
    except Exception as exc:
        error_console.print(f"[red]runtime error: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    finally:
        settings.repo_root = original_repo_root

    if exit_code:
        raise typer.Exit(code=exit_code)


def _print_sessions(session_dir: Path | None, search: str | None = None) -> None:
    """列出已保存的会话，可按键词过滤。"""
    infos = SessionStore.list_sessions(session_dir, keyword=search)
    if not infos:
        if search:
            console.print(f"[dim]没有匹配 {search} 的会话[/dim]")
        else:
            console.print("[dim]没有保存的会话[/dim]")
        return

    table = Table(title="Saved sessions", border_style="cyan")
    # session id 要能整体复制去 --resume，窄终端下不能被挤成半截
    table.add_column("session", style="dim", min_width=22, no_wrap=True)
    table.add_column("created")
    table.add_column("turns", justify="right")
    table.add_column("tokens", justify="right")
    table.add_column("first question")

    for info in infos:
        question = info.first_question
        if len(question) > 60:
            question = question[:60] + "..."
        table.add_row(
            info.session_id,
            info.created_at.replace("T", " ")[:16],
            str(info.turns),
            str(info.total_tokens),
            question,
        )

    console.print(table)
    console.print("[dim]接续：breeze chat --resume <session>[/dim]")


class _TurnProgress:
    """把 Agent 事件渲染成单行状态提示。

    只在交互式终端启用：重定向或测试环境下 is_terminal 为 False，
    不产生任何输出，因此不会改变 ask/chat 的既有输出格式。
    """

    def __init__(self, target: Console) -> None:
        self.console = target
        self.enabled = target.is_terminal
        self._status = None

    def on_event(self, event: AgentEvent) -> None:
        if not self.enabled:
            return
        step = event.step + 1
        if event.kind == "llm_start":
            text = f"[dim]step {step}: waiting for model[/dim]"
        elif event.kind == "llm_end":
            if not event.ok:
                text = f"[dim]step {step}: model error[/dim]"
            elif event.tool_calls:
                text = f"[dim]step {step}: {event.tokens} tokens, {event.tool_calls} tool call(s)[/dim]"
            else:
                text = f"[dim]step {step}: {event.tokens} tokens[/dim]"
        elif event.kind == "tool_start":
            text = f"[dim]step {step}: {event.tool} ...[/dim]"
        elif event.kind == "tool_end":
            result = "ok" if event.ok else "failed"
            text = f"[dim]step {step}: {event.tool} {result} ({event.duration_ms:.0f}ms)[/dim]"
        else:
            return

        if self._status is None:
            self._status = self.console.status(text, spinner="dots")
            self._status.start()
        else:
            self._status.update(text)

    def stop(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None


def _answer_panel(answer: str) -> Panel:
    """把最终答案渲染为 Markdown 面板，去掉源码里的 ** 与 ``` 标记。"""
    return Panel(Markdown(answer), title="Answer", border_style="green")


def _print_summary(summary: dict, title: str = "Trace Summary") -> None:
    """打印 trace 摘要。"""
    table = Table(title=title, show_header=False, border_style="cyan")
    table.add_column("key", style="dim")
    table.add_column("value", justify="right")

    table.add_row("run_id", summary["run_id"])
    table.add_row("trace_path", summary["trace_path"])
    table.add_row("llm_calls", str(summary["llm_calls"]))
    table.add_row("tool_calls", str(summary["tool_calls"]))
    table.add_row("prompt_tokens", str(summary["prompt_tokens"]))
    table.add_row("completion_tokens", str(summary["completion_tokens"]))
    table.add_row("total_tokens", str(summary["total_tokens"]))
    table.add_row("llm_latency_ms", f"{summary['llm_latency_ms']:.1f}")
    table.add_row("tool_latency_ms", f"{summary['tool_latency_ms']:.1f}")
    table.add_row("errors", str(summary["errors"]))

    console.print()
    console.print(table)


def _print_chat_summary(summaries: list[dict]) -> None:
    """会话结束时汇总本次对话的调用与 Token。"""
    table = Table(title="Trace Summary", show_header=False, border_style="cyan")
    table.add_column("key", style="dim")
    table.add_column("value", justify="right")

    table.add_row("turns", str(len(summaries)))
    for key in ("llm_calls", "tool_calls", "prompt_tokens", "completion_tokens", "total_tokens", "errors"):
        table.add_row(key, str(sum(int(s.get(key, 0)) for s in summaries)))
    for key in ("llm_latency_ms", "tool_latency_ms"):
        table.add_row(key, f"{sum(float(s.get(key, 0.0)) for s in summaries):.1f}")

    console.print()
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())

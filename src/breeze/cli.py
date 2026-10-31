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
from breeze.agent import Agent
from breeze.chat import run_chat
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
            agent = Agent(llm=llm, trace=trace, max_steps=max_steps)

            if not quiet:
                console.print(f"[dim]model: {llm.model}[/dim]")
                console.print(f"[dim]repo: {repo_root}[/dim]")
                console.print()

            answer = agent.run(question)

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
            llm = LLMClient(model=model)
        except ValueError as exc:
            error_console.print(f"[red]LLM configuration error: {exc}[/red]")
            raise typer.Exit(code=1) from exc

        try:
            if resume:
                session = SessionStore.resume(resume, session_dir)
            else:
                session = SessionStore(session_dir, repo_root=repo_root, model=llm.model)
        except FileNotFoundError as exc:
            error_console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc

        console.print(
            f"[dim]model: {llm.model} | repo: {repo_root} | session: {session.session_id} | /help 查看命令[/dim]"
        )
        if resume:
            info = session.info()
            if info.repo_root and Path(info.repo_root) != repo_root:
                console.print(
                    f"[yellow]提醒：该会话原先在 {info.repo_root} 下运行，"
                    f"当前仓库是 {repo_root}，历史索引可能指向已不存在的文件。[/yellow]"
                )

        trace_prefix = trace_name or session.session_id
        exit_code = run_chat(
            agent_factory=lambda trace: Agent(llm=llm, trace=trace, max_steps=max_steps),
            trace_factory=lambda: RunTrace(trace_dir=trace_dir, name=trace_prefix),
            input_fn=input,
            print_fn=lambda text: console.print(text, markup=False),
            session=session,
            answer_fn=lambda answer: console.print(_answer_panel(answer)),
            summary_fn=lambda summary, turn: _print_summary(summary, title=f"Trace Summary · turn {turn}"),
        )
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


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())

"""Breeze 命令行接口。

提供 breeze ask 单轮问答与 breeze chat 交互式多回合会话，
运行 Agent 并输出答案与 Token 摘要。
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from breeze.agent import Agent
from breeze.chat import run_chat
from breeze.config import settings
from breeze.llm import LLMClient
from breeze.session import SessionStore
from breeze.trace import RunTrace

app = typer.Typer(
    name="breeze",
    help="Token-efficient coding agent.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
error_console = Console(stderr=True)


@app.callback()
def breeze_callback() -> None:
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
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="only outputs answers and does not display Token summaries",
    ),
) -> None:
    """向 Breeze 提问，输出答案。"""
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

        with RunTrace(trace_dir=trace_dir) as trace:
            agent = Agent(llm=llm, trace=trace, max_steps=max_steps)

            if not quiet:
                console.print(f"[dim]model: {llm.model}[/dim]")
                console.print(f"[dim]repo: {repo_root}[/dim]")
                console.print()

            answer = agent.run(question)

            if quiet:
                console.print(answer)
            else:
                console.print(Panel(answer, title="Answer", border_style="green"))

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
) -> None:
    """进入交互式多回合会话，输入 /exit 退出。"""
    if list_sessions:
        _print_sessions(session_dir)
        return

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

        exit_code = run_chat(
            agent_factory=lambda trace: Agent(llm=llm, trace=trace, max_steps=max_steps),
            trace_factory=lambda: RunTrace(trace_dir=trace_dir),
            input_fn=input,
            # markup=False：答案里的 Markdown（[文本](链接)、[Note]）不能被 rich 当样式吃掉
            print_fn=lambda text: console.print(text, markup=False),
            session=session,
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


def _print_sessions(session_dir: Path | None) -> None:
    """列出已保存的会话。"""
    infos = SessionStore.list_sessions(session_dir)
    if not infos:
        console.print("[dim]没有保存的会话[/dim]")
        return

    table = Table(title="Saved sessions", border_style="cyan")
    table.add_column("session", style="dim")
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


def _print_summary(summary: dict) -> None:
    """打印 trace 摘要。"""
    table = Table(title="Trace Summary", show_header=False, border_style="cyan")
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

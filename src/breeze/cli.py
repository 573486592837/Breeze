"""Breeze 命令行接口。

提供 breeze ask 命令，接受自然语言问题，运行 Agent 并输出答案与 Token 摘要。
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from breeze.agent import DEFAULT_MAX_STEPS, Agent
from breeze.config import settings
from breeze.llm import LLMClient
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
        DEFAULT_MAX_STEPS,
        "--max-steps",
        help="max cycling steps",
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

"""Breeze 命令行接口。

提供 breeze ask 单轮问答与 breeze chat 交互式多回合会话，
运行 Agent 并输出答案与 Token 摘要。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from breeze import __version__
from breeze.agent import Agent, AgentEvent
from breeze.chat import run_chat
from breeze.chat_commands import ChatController
from breeze.chat_input import make_chat_input
from breeze.config import PERMISSION_MODES, settings
from breeze.doctor import Check, run_checks
from breeze.llm import LLMClient
from breeze.session import SessionStore
from breeze.trace import RunTrace
from breeze.update_check import check_for_update

app = typer.Typer(
    name="breeze",
    help="Token-efficient coding agent.",
    epilog=(
        "Examples:\n"
        '  breeze ask "解释 src/breeze/agent.py 的作用" --max-steps 4\n'
        '  breeze ask "修复 bug" --trace-name fix-bug\n'
        "  breeze chat --resume latest\n"
        "  breeze chat --list --search safe_path\n"
        "  breeze doctor\n"
        "  breeze update\n"
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


def _validate_permission(value: str | None) -> str | None:
    """校验 --permission 取值并统一小写。"""
    if value is None:
        return None
    mode = value.lower()
    if mode not in PERMISSION_MODES:
        error_console.print("[red]--permission 只能是 strict、confirm 或 open[/red]")
        raise typer.Exit(code=1)
    return mode


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
    permission: str = typer.Option(
        None,
        "--permission",
        help="command permission: strict | confirm | open (default: from config)",
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
    permission = _validate_permission(permission)
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
            ask_user = _ask_user_callback(console, input, progress) if sys.stdin.isatty() else None
            agent = Agent(
                llm=llm,
                trace=trace,
                max_steps=max_steps,
                on_event=progress.on_event,
                permission_mode=permission,
                ask_user=ask_user,
            )

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
    permission: str = typer.Option(
        None,
        "--permission",
        help="command permission: strict | confirm | open (default: from config)",
    ),
    trace_dir: Path = typer.Option(
        Path("traces"),
        "--trace-dir",
        help="trace file output directory; chat appends turns to one file per session",
        file_okay=False,
        dir_okay=True,
    ),
    trace_name: str = typer.Option(
        None,
        "--trace-name",
        help="exact trace filename (without .jsonl); appended across turns",
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
    permission = _validate_permission(permission)
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

    _maybe_notify_update()

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
            max_edit_failures=saved.get("max_edit_failures"),
            permission=permission or saved.get("permission"),
            temperature=saved.get("temperature"),
            profile=saved.get("profile"),
            trace_prefix=trace_name or saved.get("trace_prefix") or session.session_id,
            # 同一会话默认追加到同一个 trace 文件；/trace auto 显式关闭后
            # 才恢复每回合独立文件。
            trace_pinned=True if trace_name else saved.get("trace_pinned", True) is not False,
            trace_dir=trace_dir,
            intra_turn_budget=saved.get("intra_turn_budget"),
            hard_budget=saved.get("hard_budget"),
            answer_preview=saved.get("answer_preview"),
            max_records=saved.get("max_records"),
            max_chars=saved.get("max_chars"),
        )
        if trace_name:
            # 把显式指定的 trace 名写回会话，--resume 时无需再次传参。
            session.record_config(trace_prefix=controller.trace_prefix, trace_pinned=True)

        console.print(
            f"[dim]model: {controller.llm.model} | repo: {repo_root} | "
            f"permission: {controller.permission} | "
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

        def _confirm_command(command: str) -> bool:
            console.print("未列入白名单的命令：", style="yellow")
            console.print(f"  {command}", markup=False)
            try:
                answer = input("允许执行？[y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return False
            return answer in {"y", "yes"}

        confirm_command = _confirm_command if sys.stdin.isatty() else None
        chat_input = make_chat_input()
        ask_user = _ask_user_callback(console, chat_input, progress) if sys.stdin.isatty() else None

        with tempfile.TemporaryDirectory(prefix="breeze-sides-") as side_dir:
            side_root = Path(side_dir)

            def _side_agent(trace):
                return Agent(
                    llm=controller.llm,
                    trace=trace,
                    max_steps=controller.max_steps,
                    max_edit_failures=controller.max_edit_failures,
                    context=controller.build_context(),
                    on_event=progress.on_event,
                    permission_mode=controller.permission,
                    read_only=True,
                )

            def _side_trace(task_id):
                return RunTrace(trace_dir=side_root, name=f"side-{task_id}")

            def _side_sync(agent):
                # 侧任务保留自己的 messages/state/context，只同步主对话的运行时参数。
                agent.llm = controller.llm
                agent.max_steps = controller.max_steps
                agent.max_edit_failures = controller.max_edit_failures

            exit_code = run_chat(
                agent_factory=lambda trace: Agent(
                    llm=controller.llm,
                    trace=trace,
                    max_steps=controller.max_steps,
                    max_edit_failures=controller.max_edit_failures,
                    context=controller.build_context(),
                    on_event=progress.on_event,
                    permission_mode=controller.permission,
                    confirm_command=confirm_command,
                    ask_user=ask_user,
                ),
                trace_factory=lambda: (
                    RunTrace(trace_dir=trace_dir, fixed_name=controller.trace_prefix)
                    if controller.trace_pinned
                    else RunTrace(trace_dir=trace_dir, name=controller.trace_prefix)
                ),
                input_fn=chat_input,
                print_fn=lambda text: console.print(text, markup=False),
                session=session,
                answer_fn=lambda answer: console.print(_answer_panel(answer)),
                summary_fn=_on_turn_summary,
                command_fn=controller.handle,
                agent_setup_fn=controller.apply_to_agent,
                progress=progress,
                side_agent_factory=_side_agent,
                side_trace_factory=_side_trace,
                side_setup_fn=_side_sync,
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


@app.command()
def update() -> None:
    """Check for a newer release; this command never modifies files."""
    result = check_for_update(force=True)

    if result.error and not result.latest:
        error_console.print(f"[red]检查更新失败：{escape(result.error)}[/red]")
        raise typer.Exit(code=1)

    if result.available:
        console.print(f"[yellow]发现新版本 v{escape(result.latest)}（当前 v{escape(result.current)}）。[/yellow]")
        if result.url:
            console.print(escape(result.url))
        console.print("升级：在 git 检出中执行 git fetch --tags 后 checkout 新 tag，或按 README 重新安装。")
        return

    console.print(f"[green]当前已是最新版本 v{escape(result.current)}。[/green]")


def _maybe_notify_update() -> None:
    """交互式 chat 启动时提示新版本；只读缓存/网络，不自动升级。"""
    if os.environ.get("BREEZE_NO_UPDATE_CHECK") or not console.is_terminal:
        return
    result = check_for_update(timeout=1.5)
    if result.available:
        console.print(
            f"[yellow]发现新版本 v{escape(result.latest)}（当前 v{escape(result.current)}），"
            "运行 breeze update 查看升级说明。[/yellow]"
        )


@app.command()
def doctor(
    ping: bool = typer.Option(
        False,
        "--ping",
        help="send one minimal model request to verify API key, endpoint and model",
    ),
) -> None:
    """Check local configuration and environment, then exit."""
    checks = run_checks(ping=ping)
    _print_doctor(checks)

    failures = sum(check.status == "fail" for check in checks)
    if failures:
        error_console.print("[red]请先修复失败项再运行 breeze ask/chat。[/red]")
        raise typer.Exit(code=1)


def _print_doctor(checks: list[Check]) -> None:
    """渲染诊断结果。"""
    styles = {
        "ok": "[green]OK[/green]",
        "warn": "[yellow]WARN[/yellow]",
        "fail": "[red]FAIL[/red]",
    }
    table = Table(title=f"Breeze doctor {__version__}")
    table.add_column("检查", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("说明", overflow="fold")

    for check in checks:
        detail = escape(check.detail)
        if check.hint:
            detail += f"\n[dim]{escape(check.hint)}[/dim]"
        table.add_row(escape(check.name), styles[check.status], detail)

    console.print(f"[dim]cwd: {escape(str(Path.cwd()))}[/dim]")
    console.print(table)
    counts = {status: sum(check.status == status for check in checks) for status in styles}
    console.print(f"[dim]{counts['ok']} 项通过，{counts['warn']} 项警告，{counts['fail']} 项失败[/dim]")


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

    等待模型时显示上一轮 ``reasoning_content`` 的尾部摘要与实时等待秒数；
    新的思考返回后会替换旧摘要，不累积历史。
    """

    _REASONING_CHARS = 700
    _REASONING_LINES = 6

    def __init__(self, target: Console) -> None:
        self.console = target
        self.enabled = target.is_terminal
        self._status = None
        self._last_reasoning = ""
        self._wait_started = 0.0
        self._timer_stop: threading.Event | None = None
        self._generation = 0
        self._lock = threading.Lock()

    def on_event(self, event: AgentEvent) -> None:
        if not self.enabled:
            return
        step = event.step + 1
        if event.kind == "llm_start":
            self._stop_timer()
            self._set_status(self._waiting_text(step, 0.0))
            self._start_timer(step)
            return

        self._stop_timer()
        if event.kind == "llm_end":
            self._last_reasoning = event.reasoning or ""
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
        self._set_status(text)

    def _waiting_text(self, step: int, elapsed: float) -> str:
        """构造等待提示：上一轮思考摘要 + 当前等待秒数。"""
        head = f"[dim]step {step}: waiting for model {elapsed:.0f}s[/dim]"
        preview = self._reasoning_preview(self._last_reasoning)
        if not preview:
            return head
        return f"{head}\n[dim]{escape(preview)}[/dim]"

    @classmethod
    def _reasoning_preview(cls, text: str) -> str:
        """取思考过程尾部若干行，保留最新意图并限制高度。"""
        if not text:
            return ""
        lines = [line.rstrip() for line in text.strip().splitlines()]
        compact: list[str] = []
        previous_blank = False
        for line in lines:
            blank = not line.strip()
            if blank and previous_blank:
                continue
            compact.append(line)
            previous_blank = blank
        joined = "\n".join(compact).strip()
        if not joined:
            return ""
        if len(joined) > cls._REASONING_CHARS:
            joined = "…" + joined[-cls._REASONING_CHARS :]
        return "\n".join(joined.splitlines()[-cls._REASONING_LINES :])

    def _start_timer(self, step: int) -> None:
        """后台每秒刷新等待秒数。"""
        self._wait_started = time.monotonic()
        stop = threading.Event()
        self._timer_stop = stop
        generation = self._generation

        def tick() -> None:
            while not stop.wait(1.0):
                elapsed = time.monotonic() - self._wait_started
                self._set_status(self._waiting_text(step, elapsed), generation=generation)

        threading.Thread(target=tick, daemon=True).start()

    def _stop_timer(self) -> None:
        self._generation += 1
        if self._timer_stop is not None:
            self._timer_stop.set()
            self._timer_stop = None

    def _set_status(self, text: str, *, generation: int | None = None) -> None:
        with self._lock:
            # 计时线程可能在 stop() 之后才拿到锁；代际不匹配时丢弃，
            # 避免退出后重新创建一个无人停止的 status。
            if generation is not None and generation != self._generation:
                return
            if self._status is None:
                self._status = self.console.status(text, spinner="dots")
                self._status.start()
            else:
                self._status.update(text)

    def stop(self) -> None:
        self._stop_timer()
        self._last_reasoning = ""
        with self._lock:
            if self._status is not None:
                self._status.stop()
                self._status = None


def _ask_user_callback(
    console: Console,
    read: Callable[[str], str],
    progress: _TurnProgress | None = None,
) -> Callable[[str, list[str] | None], str]:
    """构造 ask_user 的终端回调：展示问题与候选，返回选项文本或原始回答。"""

    def ask(question: str, options: list[str] | None) -> str:
        # Agent 可能正显示状态行；提问前先收起，避免 input 与动态状态重叠。
        if progress is not None:
            progress.stop()
        console.print()
        console.print("[cyan]Agent 提问：[/cyan]")
        console.print(question, markup=False)
        choices = [str(option) for option in (options or [])][:4]
        for index, option in enumerate(choices, 1):
            console.print(f"  {index}. {option}", markup=False)
        try:
            answer = read("Answer> ").strip()
        except EOFError:
            return ""
        if choices and answer.isdigit():
            picked = int(answer) - 1
            if 0 <= picked < len(choices):
                return choices[picked]
        return answer

    return ask


def _answer_panel(answer: str) -> Panel:
    """把最终答案渲染为 Markdown 面板，去掉源码里的 ** 与 ``` 标记。"""
    return Panel(Markdown(answer), title="Answer", border_style="green")


def _add_cost_cache_rows(
    table: Table,
    *,
    llm_calls: int,
    cost_usd: float | None,
    cache_reported_calls: int,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    cacheable_prefix_tokens: int,
    estimated_prompt_tokens: int,
) -> None:
    """向摘要表追加成本与缓存指标；缺失的指标不显示。"""
    if cost_usd is not None:
        table.add_row("cost_usd", f"${cost_usd:.6f}")
    if cache_reported_calls:
        table.add_row("cache_reported", f"{cache_reported_calls}/{llm_calls} calls")
        table.add_row("cache_hit_tokens", str(cache_hit_tokens))
        total = cache_hit_tokens + cache_miss_tokens
        if total:
            table.add_row("cache_hit_rate", f"{cache_hit_tokens / total:.1%}")
    if estimated_prompt_tokens:
        ratio = cacheable_prefix_tokens / estimated_prompt_tokens
        table.add_row("cacheable_prefix", f"{ratio:.1%} ({cacheable_prefix_tokens} tok)")


def _print_summary(summary: dict, title: str = "Trace Summary") -> None:
    """打印 trace 摘要。"""
    table = Table(title=title, show_header=False, border_style="cyan")
    table.add_column("key", style="dim")
    table.add_column("value", justify="right")

    table.add_row("run_id", summary["run_id"])
    table.add_row("trace_path", summary["trace_path"])
    if summary.get("messages_path"):
        table.add_row("messages_path", summary["messages_path"])
    table.add_row("llm_calls", str(summary["llm_calls"]))
    table.add_row("tool_calls", str(summary["tool_calls"]))
    table.add_row("prompt_tokens", str(summary["prompt_tokens"]))
    table.add_row("completion_tokens", str(summary["completion_tokens"]))
    table.add_row("total_tokens", str(summary["total_tokens"]))
    _add_cost_cache_rows(
        table,
        llm_calls=summary["llm_calls"],
        cost_usd=summary.get("cost_usd"),
        cache_reported_calls=summary.get("cache_reported_calls", 0),
        cache_hit_tokens=summary.get("cache_hit_tokens", 0),
        cache_miss_tokens=summary.get("cache_miss_tokens", 0),
        cacheable_prefix_tokens=summary.get("cacheable_prefix_tokens", 0),
        estimated_prompt_tokens=summary.get("estimated_prompt_tokens", 0),
    )
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
        table.add_row(key, str(sum(int(s.get(key, 0) or 0) for s in summaries)))
    costs = [s.get("cost_usd") for s in summaries if s.get("cost_usd") is not None]
    _add_cost_cache_rows(
        table,
        llm_calls=sum(int(s.get("llm_calls", 0) or 0) for s in summaries),
        cost_usd=sum(costs) if costs else None,
        cache_reported_calls=sum(int(s.get("cache_reported_calls", 0) or 0) for s in summaries),
        cache_hit_tokens=sum(int(s.get("cache_hit_tokens", 0) or 0) for s in summaries),
        cache_miss_tokens=sum(int(s.get("cache_miss_tokens", 0) or 0) for s in summaries),
        cacheable_prefix_tokens=sum(int(s.get("cacheable_prefix_tokens", 0) or 0) for s in summaries),
        estimated_prompt_tokens=sum(int(s.get("estimated_prompt_tokens", 0) or 0) for s in summaries),
    )
    for key in ("llm_latency_ms", "tool_latency_ms"):
        table.add_row(key, f"{sum(float(s.get(key, 0.0) or 0.0) for s in summaries):.1f}")

    captured = [s["messages_path"] for s in summaries if s.get("messages_path")]
    if captured:
        table.add_row(
            "messages_path",
            captured[0] if len(captured) == 1 else f"{len(captured)} files (one per turn)",
        )

    console.print()
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())

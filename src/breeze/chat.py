"""breeze chat 的交互会话循环。

run_chat 与 CLI 解耦（agent_factory / trace_factory / input_fn 可注入），
便于测试与复用。默认每个会话共用一个 trace 文件、逐回合追加（/trace auto
可恢复每回合独立文件）；Agent 跨回合存活，上一回合的问答结论由 TurnLedger
归档注入下一回合，工具过程不再进入窗口。

传入 session（SessionStore）时，会话开始前沿用其中已归档的回合与状态，
每回合结束把结论与状态快照落盘，进程退出后仍可 `--resume` 接续。

提供 side_agent_factory / side_trace_factory 时启用只读侧任务：侧任务挂在
当前活动节点下形成内存树，/back 暂停、/switch 切回、/close 丢弃；侧任务
不写主会话、不计入主回合摘要，退出后不可恢复。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from breeze.chat_commands import COMMANDS, HELP_TEXT
from breeze.side import SideTask, SideTree

_SIDE_COMMAND_NAMES = frozenset({"side", "sides", "back", "close", "switch", "help", "exit"})


def _side_help_text() -> str:
    """由命令元数据生成侧任务 /help 文本。"""
    lines = ["side task commands (read-only):"]
    lines.extend(f"  {spec.usage:<22} {spec.description}" for spec in COMMANDS if spec.name in _SIDE_COMMAND_NAMES)
    return "\n".join(lines)


SIDE_HELP_TEXT = _side_help_text()


def run_chat(
    agent_factory: Callable[[Any], Any],
    trace_factory: Callable[[], Any],
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
    session: Any | None = None,
    answer_fn: Callable[[str], None] | None = None,
    summary_fn: Callable[[dict[str, Any], int], None] | None = None,
    command_fn: Callable[[str, Any | None], str | None] | None = None,
    agent_setup_fn: Callable[[Any], None] | None = None,
    progress: Any | None = None,
    side_agent_factory: Callable[[Any], Any] | None = None,
    side_trace_factory: Callable[[str], Any] | None = None,
    side_summary_fn: Callable[[dict[str, Any], str, int], None] | None = None,
    side_setup_fn: Callable[[Any], None] | None = None,
) -> int:
    """运行交互式会话直到退出。

    Args:
        agent_factory: 接收首个 trace，构造主 Agent。
        trace_factory: 每回合调用一次，返回新的 RunTrace（用作上下文管理器）。
        input_fn: 读取一行用户输入。
        print_fn: 输出系统消息（帮助、退出提示等）。
        session: SessionStore；提供时会先恢复归档与状态，每回合结束落盘。
        answer_fn: 渲染最终答案，默认直接交给 print_fn。
        summary_fn: 渲染主回合的 trace 摘要，默认按单行文本输出。
        command_fn: 处理 /new、/help、/exit 以外的命令；返回 None 表示
            不是命令，按提问执行。侧任务内不会调用。
        agent_setup_fn: 主 Agent 创建并恢复会话状态后调用，用于同步运行时配置。
        progress: 进度展示对象，需提供 on_event(event) 与 stop()。
            仅在终端下由 CLI 传入；它只观察 Agent 事件，不影响 prompt 与 trace。
        side_agent_factory: 接收首个 trace，构造只读侧任务 Agent；None 时
            不启用侧任务。
        side_trace_factory: 接收侧任务 ID，返回该回合的 RunTrace；None 时
            不启用侧任务。
        side_summary_fn: 渲染侧任务回合摘要，默认按单行文本输出。
        side_setup_fn: 侧任务每回合执行前调用，用于同步主对话的模型与步数配置；
            不应替换侧任务自己的 messages/state/context。

    Returns:
        进程退出码，0 表示正常退出。
    """
    if answer_fn is None:
        answer_fn = print_fn
    if summary_fn is None:

        def _default_summary(summary: dict[str, Any], turn: int) -> None:
            print_fn(
                f"[turn {turn}] llm_calls={summary['llm_calls']} tool_calls={summary['tool_calls']} "
                f"total_tokens={summary['total_tokens']} trace={summary['trace_path']}"
            )

        summary_fn = _default_summary
    if side_summary_fn is None:

        def _default_side_summary(summary: dict[str, Any], task_id: str, turns: int) -> None:
            print_fn(
                f"[side {task_id} turn {turns}] llm_calls={summary['llm_calls']} "
                f"tool_calls={summary['tool_calls']} total_tokens={summary['total_tokens']} "
                f"trace={summary['trace_path']}"
            )

        side_summary_fn = _default_side_summary

    side_enabled = side_agent_factory is not None and side_trace_factory is not None
    side_tree = SideTree() if side_enabled else None

    agent = None
    turns = 0

    def _run_side(task: SideTask, question: str, trace: Any | None = None) -> None:
        """执行一个侧任务回合，不触碰主会话与主回合计数。"""
        if trace is None:
            trace = side_trace_factory(task.id)
        with trace:
            if side_setup_fn is not None:
                side_setup_fn(task.agent)
            if progress is not None:
                task.agent.on_event = progress.on_event
            task.agent.trace = trace
            try:
                answer = task.agent.run(question)
            except KeyboardInterrupt:
                print_fn("(side turn interrupted)")
                return
            finally:
                if progress is not None:
                    progress.stop()
        task.turns += 1
        answer_fn(answer)
        side_summary_fn(trace.summary(), task.id, task.turns)

    def _start_side(question: str) -> None:
        """创建侧任务并立即执行首个问题。"""
        task_id = side_tree.new_id()
        trace = None
        try:
            trace = side_trace_factory(task_id)
            side_agent = side_agent_factory(trace)
            task = side_tree.spawn(question, side_agent, task_id)
        except Exception as exc:
            if trace is not None:
                trace.close()
            print_fn(f"(failed to start side task: {exc})")
            return
        print_fn(f"(side {task.id} started)")
        _run_side(task, question, trace)

    while True:
        active = side_tree.active if side_tree is not None else None
        prompt = f"side[{active.id}]> " if active is not None else "You> "
        try:
            question = input_fn(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print_fn("bye.")
            return 0

        if not question:
            continue
        if question in ("/exit", "/quit"):
            print_fn("bye.")
            return 0
        if question == "/help":
            print_fn(SIDE_HELP_TEXT if active is not None else HELP_TEXT)
            continue

        if side_tree is not None:
            if question == "/side" or question.startswith("/side "):
                text = question[len("/side") :].strip()
                if not text:
                    print_fn("用法：/side <问题>")
                    continue
                _start_side(text)
                continue
            if question == "/sides":
                print_fn(side_tree.render())
                continue
            if question == "/back":
                if active is None:
                    print_fn("(not in a side task)")
                    continue
                parent = side_tree.back()
                print_fn("(back to main)" if parent is None else f"(back to {parent})")
                continue
            if question == "/close":
                if active is None:
                    print_fn("(not in a side task)")
                    continue
                removed = side_tree.close()
                parent = side_tree.active_id
                target = "main" if parent is None else parent
                print_fn(f"(closed {', '.join(removed)}; now at {target})")
                continue
            if question == "/switch" or question.startswith("/switch "):
                target = question[len("/switch") :].strip()
                if not target:
                    print_fn("用法：/switch <id|main>")
                    continue
                try:
                    side_tree.switch(target)
                except KeyError:
                    print_fn(f"(unknown side task: {target})")
                else:
                    print_fn(f"(switched to {target})")
                continue

        if active is not None:
            if question.startswith("/"):
                print_fn(
                    "side tasks only support /side /sides /back /close /switch /help /exit; "
                    "runtime config commands are disabled."
                )
                continue
            _run_side(active, question)
            continue

        if question == "/new":
            if agent is not None:
                agent.reset()
                if agent_setup_fn is not None:
                    agent_setup_fn(agent)
            if session is not None:
                session.record_reset()
            if side_tree is not None:
                side_tree = SideTree()
            turns = 0
            print_fn("(new conversation)")
            continue
        if command_fn is not None:
            response = command_fn(question, agent)
            if response is not None:
                print_fn(response)
                continue

        trace = trace_factory()
        with trace:
            if agent is None:
                agent = agent_factory(trace)
                if session is not None:
                    agent.ledger, agent.state = session.load()
                    snapshots = getattr(agent, "snapshots", None)
                    if snapshots is not None:
                        snapshots.restore(session.load_snapshots())
                if agent_setup_fn is not None:
                    agent_setup_fn(agent)
            else:
                agent.trace = trace
            if progress is not None:
                agent.on_event = progress.on_event
            ledger = getattr(agent, "ledger", None)
            records_before = len(ledger.records) if ledger is not None else 0
            try:
                answer = agent.run(question)
            except KeyboardInterrupt:
                # 回合中断：本回合未归档，消息日志保留现状继续会话。
                print_fn("(turn interrupted)")
                continue
            finally:
                if progress is not None:
                    progress.stop()
            turn_aborted = ledger is None or len(ledger.records) == records_before

        turns += 1
        if session is not None and ledger is not None and not turn_aborted:
            session.record_turn(ledger.records[-1], agent.state, trace.summary())
            snapshots = getattr(agent, "snapshots", None)
            if snapshots is not None:
                session.save_snapshots(snapshots.to_dict())

        answer_fn(answer)
        summary_fn(trace.summary(), turns)

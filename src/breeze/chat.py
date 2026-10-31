"""breeze chat 的交互会话循环。

run_chat 与 CLI 解耦（agent_factory / trace_factory / input_fn 可注入），
便于测试与复用。每个提问回合创建独立的 trace 文件；Agent 跨回合存活，
上一回合的问答结论由 TurnLedger 归档注入下一回合，工具过程不再进入窗口。

传入 session（SessionStore）时，会话开始前沿用其中已归档的回合与状态，
每回合结束把结论与状态快照落盘，进程退出后仍可 `--resume` 接续。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

HELP_TEXT = (
    "commands:\n"
    "  /model [name]       查看或切换模型\n"
    "  /trace [prefix]     查看或设置 trace 文件名前缀\n"
    "  /profile [name]     查看或切换压缩档位（short / medium / long / custom）\n"
    "  /set <key> <value>  调整 max_steps / max_edit_attempts / temperature\n"
    "  /config             查看当前运行配置\n"
    "  /new                重置对话（运行时配置保留）\n"
    "  /help               显示帮助\n"
    "  /exit               退出（Ctrl+C / Ctrl+D 同样有效）"
)


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
) -> int:
    """运行交互式会话直到退出。

    Args:
        agent_factory: 接收首个 trace，构造 Agent。
        trace_factory: 每回合调用一次，返回新的 RunTrace（用作上下文管理器）。
        input_fn: 读取一行用户输入。
        print_fn: 输出系统消息（帮助、退出提示等）。
        session: SessionStore；提供时会先恢复归档与状态，每回合结束落盘。
        answer_fn: 渲染最终答案，默认直接交给 print_fn。
        summary_fn: 渲染每回合的 trace 摘要，默认按单行文本输出。
        command_fn: 处理 /new、/help、/exit 以外的命令；返回 None 表示
            不是命令，按提问执行。
        agent_setup_fn: Agent 创建并恢复会话状态后调用，用于同步运行时配置。
        progress: 进度展示对象，需提供 on_event(event) 与 stop()。
            仅在终端下由 CLI 传入；它只观察 Agent 事件，不影响 prompt 与 trace。

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

    agent = None
    turns = 0

    while True:
        try:
            question = input_fn("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print_fn("bye.")
            return 0

        if not question:
            continue
        if question in ("/exit", "/quit"):
            print_fn("bye.")
            return 0
        if question == "/help":
            print_fn(HELP_TEXT)
            continue
        if question == "/new":
            if agent is not None:
                agent.reset()
                if agent_setup_fn is not None:
                    agent_setup_fn(agent)
            if session is not None:
                session.record_reset()
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
                if agent_setup_fn is not None:
                    agent_setup_fn(agent)
            else:
                agent.trace = trace
            if progress is not None:
                agent.on_event = progress.on_event
            try:
                answer = agent.run(question)
            except KeyboardInterrupt:
                # 回合中断：本回合未归档，消息日志保留现状继续会话。
                print_fn("(turn interrupted)")
                continue
            finally:
                if progress is not None:
                    progress.stop()

        turns += 1
        if session is not None and agent.ledger.records:
            session.record_turn(agent.ledger.records[-1], agent.state, trace.summary())

        answer_fn(answer)
        summary_fn(trace.summary(), turns)

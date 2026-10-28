"""breeze chat 的交互会话循环。

run_chat 与 CLI 解耦（agent_factory / trace_factory / input_fn 可注入），
便于测试与复用。每个提问回合创建独立的 trace 文件；Agent 跨回合存活，
上一回合的问答结论由 TurnLedger 归档注入下一回合，工具过程不再进入窗口。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

HELP_TEXT = "commands: /new 重置会话 | /help 显示帮助 | /exit 退出（Ctrl+C / Ctrl+D 同样有效）"


def run_chat(
    agent_factory: Callable[[Any], Any],
    trace_factory: Callable[[], Any],
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> int:
    """运行交互式会话直到退出。

    Args:
        agent_factory: 接收首个 trace，构造 Agent。
        trace_factory: 每回合调用一次，返回新的 RunTrace（用作上下文管理器）。
        input_fn: 读取一行用户输入。
        print_fn: 输出文本。

    Returns:
        进程退出码，0 表示正常退出。
    """
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
            turns = 0
            print_fn("(new conversation)")
            continue

        trace = trace_factory()
        with trace:
            if agent is None:
                agent = agent_factory(trace)
            else:
                agent.trace = trace
            try:
                answer = agent.run(question)
            except KeyboardInterrupt:
                # 回合中断：本回合未归档，消息日志保留现状继续会话。
                print_fn("(turn interrupted)")
                continue

        turns += 1
        print_fn(answer)
        s = trace.summary()
        print_fn(
            f"[turn {turns}] llm_calls={s['llm_calls']} tool_calls={s['tool_calls']} "
            f"total_tokens={s['total_tokens']} trace={s['trace_path']}"
        )

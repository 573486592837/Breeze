"""交互式会话循环的单元测试。"""

from typing import Any

import pytest

from breeze.chat import HELP_TEXT, run_chat


class StubAgent:
    """按脚本返回答案的假 Agent。答案为 "INTERRUPT" 时模拟回合中断。"""

    def __init__(self, trace: Any, answers: list[str]) -> None:
        self.trace = trace
        self.answers = list(answers)
        self.questions: list[str] = []
        self.resets = 0

    def run(self, question: str) -> str:
        self.questions.append(question)
        answer = self.answers.pop(0)
        if answer == "INTERRUPT":
            raise KeyboardInterrupt
        return answer

    def reset(self) -> None:
        self.resets += 1


class StubTrace:
    """假 trace，记录开关次数并输出固定摘要。"""

    def __init__(self, turn_id: int) -> None:
        self.turn_id = turn_id
        self.opened = 0
        self.closed = 0

    def __enter__(self) -> "StubTrace":
        self.opened += 1
        return self

    def __exit__(self, *exc: Any) -> None:
        self.closed += 1

    def summary(self) -> dict[str, Any]:
        return {
            "llm_calls": self.turn_id,
            "tool_calls": 0,
            "total_tokens": 100 * self.turn_id,
            "trace_path": f"traces/{self.turn_id}.jsonl",
        }


class ScriptInput:
    """按脚本提供输入，耗尽后抛 EOFError。"""

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)

    def __call__(self, prompt: str) -> str:
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


def _run(
    lines: list[str],
    answers: list[str] | None = None,
) -> tuple[int, list[str], list[StubTrace], list[StubAgent]]:
    """执行 run_chat，返回 (退出码, 输出行, traces, agents)。"""
    traces: list[StubTrace] = []
    agents: list[StubAgent] = []
    outputs: list[str] = []

    def agent_factory(trace: Any) -> StubAgent:
        agent = StubAgent(trace, list(answers or ["answer"]))
        agents.append(agent)
        return agent

    def trace_factory() -> StubTrace:
        trace = StubTrace(len(traces) + 1)
        traces.append(trace)
        return trace

    code = run_chat(
        agent_factory=agent_factory,
        trace_factory=trace_factory,
        input_fn=ScriptInput(lines),
        print_fn=outputs.append,
    )
    return code, outputs, traces, agents


# ─── 退出路径 ───


def test_exit_command() -> None:
    """/exit 正常退出且不建 Agent。"""
    code, outputs, _, agents = _run(["/exit"])
    assert code == 0
    assert outputs == ["bye."]
    assert agents == []


def test_eof_exits() -> None:
    """输入耗尽（Ctrl+D）时退出。"""
    code, outputs, _, _ = _run(["hello"])
    assert code == 0
    assert "answer" in outputs
    assert outputs[-1] == "bye."


def test_blank_line_skipped() -> None:
    """空行不触发回合。"""
    code, _, traces, _ = _run(["   ", "/exit"])
    assert code == 0
    assert traces == []


# ─── 命令 ───


def test_help_prints_commands() -> None:
    """/help 输出命令说明且不建 Agent。"""
    code, outputs, _, agents = _run(["/help", "/exit"])
    assert code == 0
    assert HELP_TEXT in outputs
    assert agents == []


def test_new_without_agent_is_noop() -> None:
    """尚未创建 Agent 时 /new 只重置计数。"""
    code, outputs, _, agents = _run(["/new", "/exit"])
    assert code == 0
    assert "(new conversation)" in outputs
    assert agents == []


def test_new_resets_existing_agent() -> None:
    """/new 调用 Agent.reset 并保留 Agent 实例。"""
    code, _, _, agents = _run(["first", "/new", "second", "/exit"], answers=["a1", "a2"])
    assert code == 0
    assert len(agents) == 1
    assert agents[0].resets == 1
    assert agents[0].questions == ["first", "second"]


# ─── 回合与 trace ───


def test_each_turn_gets_its_own_trace() -> None:
    """每回合新开一个 trace 并挂到同一个 Agent 上。"""
    code, _, traces, agents = _run(["q1", "q2", "/exit"], answers=["a1", "a2"])
    assert code == 0
    assert len(traces) == 2
    assert all(t.opened == 1 and t.closed == 1 for t in traces)
    assert len(agents) == 1
    assert agents[0].trace is traces[1]


def test_turn_summary_line_printed() -> None:
    """回合结束后打印 Token 摘要行。"""
    _, outputs, _, _ = _run(["q1", "/exit"])
    assert "[turn 1] llm_calls=1 tool_calls=0 total_tokens=100 trace=traces/1.jsonl" in outputs


def test_keyboard_interrupt_keeps_session_alive() -> None:
    """回合中途中断后提示并继续会话。"""
    code, outputs, traces, agents = _run(["boom", "next", "/exit"], answers=["INTERRUPT", "after"])
    assert code == 0
    assert "(turn interrupted)" in outputs
    assert "after" in outputs
    assert len(traces) == 2
    assert agents[0].questions == ["boom", "next"]


def test_trace_closed_when_turn_raises() -> None:
    """Agent 抛异常时 trace 仍被关闭，异常向上传播给 CLI 处理。"""

    class ExplodingAgent:
        def __init__(self, trace: Any) -> None:
            self.trace = trace

        def run(self, question: str) -> str:
            raise RuntimeError("boom")

    trace = StubTrace(1)
    with pytest.raises(RuntimeError):
        run_chat(
            agent_factory=lambda t: ExplodingAgent(t),
            trace_factory=lambda: trace,
            input_fn=lambda _: "q",
            print_fn=lambda _: None,
        )
    assert trace.closed == 1

"""交互式会话循环的单元测试。"""

from typing import Any

import pytest

from breeze.chat import HELP_TEXT, run_chat
from breeze.state import SessionState, TurnLedger


class StubAgent:
    """按脚本返回答案的假 Agent。答案为 "INTERRUPT" 时模拟回合中断。"""

    def __init__(self, trace: Any, answers: list[str]) -> None:
        self.trace = trace
        self.answers = list(answers)
        self.questions: list[str] = []
        self.resets = 0
        self.ledger = TurnLedger()
        self.state = SessionState()

    def run(self, question: str) -> str:
        self.questions.append(question)
        answer = self.answers.pop(0)
        if answer == "INTERRUPT":
            raise KeyboardInterrupt
        self.ledger.record(question, answer, ["a.py"])
        return answer

    def reset(self) -> None:
        self.resets += 1
        self.ledger = TurnLedger()
        self.state = SessionState()


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


class StubSession:
    """假 SessionStore：可提供恢复内容并记录落盘调用。"""

    def __init__(self, ledger: TurnLedger | None = None, state: SessionState | None = None) -> None:
        self.restore = (ledger or TurnLedger(), state or SessionState())
        self.saved: list[tuple[Any, dict[str, Any]]] = []
        self.resets = 0

    def load(self) -> tuple[TurnLedger, SessionState]:
        return self.restore

    def record_turn(self, record: Any, state: Any, summary: dict[str, Any]) -> None:
        self.saved.append((record, summary))

    def record_reset(self) -> None:
        self.resets += 1


def _run(
    lines: list[str],
    answers: list[str] | None = None,
    session: StubSession | None = None,
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
        session=session,
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
    assert "/plan" in HELP_TEXT
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
    """默认实现仍打印单行 Token 摘要，便于脚本与测试复用。"""
    _, outputs, _, _ = _run(["q1", "/exit"])
    assert "[turn 1] llm_calls=1 tool_calls=0 total_tokens=100 trace=traces/1.jsonl" in outputs


def test_custom_renderers_receive_answer_and_summary() -> None:
    """CLI 可注入富文本渲染器，答案与摘要不再走默认输出。"""
    outputs: list[str] = []
    answers: list[str] = []
    summaries: list[tuple[dict[str, Any], int]] = []
    trace = StubTrace(1)

    code = run_chat(
        agent_factory=lambda t: StubAgent(t, ["rendered"]),
        trace_factory=lambda: trace,
        input_fn=ScriptInput(["q1", "/exit"]),
        print_fn=outputs.append,
        answer_fn=answers.append,
        summary_fn=lambda summary, turn: summaries.append((summary, turn)),
    )

    assert code == 0
    assert answers == ["rendered"]
    assert summaries[0][1] == 1
    assert outputs == ["bye."]


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


# ─── 运行时命令接线 ───


def test_command_fn_output_skips_turn() -> None:
    """命令处理器返回文本时打印并跳过本回合，不建 trace。"""
    outputs: list[str] = []
    traces: list[StubTrace] = []

    code = run_chat(
        agent_factory=lambda t: StubAgent(t, ["answer"]),
        trace_factory=lambda: traces.append(StubTrace(1)) or traces[-1],
        input_fn=ScriptInput(["/config", "q", "/exit"]),
        print_fn=outputs.append,
        command_fn=lambda line, _agent: "cfg" if line == "/config" else None,
    )

    assert code == 0
    assert outputs[0] == "cfg"
    assert "answer" in outputs
    assert outputs[-1] == "bye."
    assert len(traces) == 1


def test_agent_setup_runs_after_session_restore() -> None:
    """Agent 创建并恢复会话状态后，再应用运行时配置。"""
    restored = TurnLedger()
    restored.record("历史问题", "历史答案", [])
    session = StubSession(ledger=restored)
    seen: list[tuple[Any, Any]] = []

    run_chat(
        agent_factory=lambda t: StubAgent(t, ["a"]),
        trace_factory=lambda: StubTrace(1),
        input_fn=ScriptInput(["q", "/exit"]),
        print_fn=lambda _: None,
        session=session,
        agent_setup_fn=lambda agent: seen.append((agent.ledger, agent.state)),
    )

    assert seen == [(session.restore[0], session.restore[1])]


def test_agent_setup_reruns_after_new() -> None:
    """/new 会重建 Agent 的归档，因此重新应用运行时配置。"""
    calls: list[Any] = []

    code = run_chat(
        agent_factory=lambda t: StubAgent(t, ["a1", "a2"]),
        trace_factory=lambda: StubTrace(1),
        input_fn=ScriptInput(["q1", "/new", "q2", "/exit"]),
        print_fn=lambda _: None,
        agent_setup_fn=calls.append,
    )

    assert code == 0
    assert len(calls) == 2


# ─── 会话持久化接线 ───


def test_session_records_each_completed_turn() -> None:
    """每回合结束后落盘该回合的归档条目与 Token 摘要。"""
    session = StubSession()
    _run(["q1", "q2", "/exit"], answers=["a1", "a2"], session=session)

    assert [(r.index, r.question, r.touched) for r, _ in session.saved] == [
        (1, "q1", ["a.py"]),
        (2, "q2", ["a.py"]),
    ]
    assert session.saved[1][1]["trace_path"] == "traces/2.jsonl"


def test_session_restores_before_first_turn() -> None:
    """首个回合前把恢复出的 ledger/state 挂到 Agent 上。"""
    ledger = TurnLedger()
    ledger.record("历史问题", "历史答案", ["old.py"])
    state = SessionState()
    state.symbols["target"] = "old.py:1"
    session = StubSession(ledger=ledger, state=state)

    _, _, _, agents = _run(["next question", "/exit"], answers=["a"], session=session)

    assert agents[0].ledger.records == ledger.records
    assert agents[0].state is state


def test_interrupted_turn_is_not_recorded() -> None:
    """中断的回合没有归档条目，因而不落盘。"""
    session = StubSession()
    _run(["boom", "next", "/exit"], answers=["INTERRUPT", "ok"], session=session)

    assert [r.index for r, _ in session.saved] == [1]


def test_new_writes_reset_event() -> None:
    """/new 写入 reset 事件，恢复时以此分界。"""
    session = StubSession()
    _run(["q1", "/new", "q2", "/exit"], answers=["a1", "a2"], session=session)

    assert session.resets == 1
    assert [r.index for r, _ in session.saved] == [1, 1]


def test_without_session_no_persistence_calls() -> None:
    """未传 session 时行为不变（回归保护）。"""
    code, _, traces, agents = _run(["q1", "/exit"])

    assert code == 0
    assert len(traces) == 1
    assert len(agents[0].ledger.records) == 1


# ─── 进度展示 ───


def test_progress_receives_turn_end_stop() -> None:
    """每回合结束调用 progress.stop()，便于终端收起状态行。"""

    class Progress:
        def __init__(self) -> None:
            self.events: list[Any] = []
            self.stops = 0

        def on_event(self, event: Any) -> None:
            self.events.append(event)

        def stop(self) -> None:
            self.stops += 1

    progress = Progress()
    outputs: list[str] = []
    traces: list[StubTrace] = []

    def agent_factory(trace: Any) -> StubAgent:
        return StubAgent(trace, ["answer"])

    def trace_factory() -> StubTrace:
        trace = StubTrace(len(traces) + 1)
        traces.append(trace)
        return trace

    code = run_chat(
        agent_factory=agent_factory,
        trace_factory=trace_factory,
        input_fn=ScriptInput(["hello", "/exit"]),
        print_fn=outputs.append,
        progress=progress,
    )

    assert code == 0
    assert progress.stops == 1


def test_progress_not_required() -> None:
    """不传 progress 时行为不变。"""
    code, outputs, _, _ = _run(["hello"], answers=["plain"])
    assert code == 0
    assert "plain" in outputs

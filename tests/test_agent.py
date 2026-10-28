"""Agent Loop 的单元测试。"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from breeze.agent import Agent
from breeze.llm import LLMResponse
from breeze.tools import registry
from breeze.trace import RunTrace


@pytest.fixture(autouse=True)
def _force_context_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试假设压缩默认开启，隔离外部配置。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "context_enable", True)


class FakeLLMClient:
    """测试用 LLM 客户端，按预设脚本返回响应。"""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.model = "fake-model"
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self.responses:
            raise RuntimeError("FakeLLMClient: no more responses")
        return self.responses.pop(0)


def _text(content: str, tokens: int = 10) -> LLMResponse:
    """构造纯文本响应。"""
    return LLMResponse(
        content=content,
        tool_calls=[],
        prompt_tokens=tokens,
        completion_tokens=tokens,
        total_tokens=tokens * 2,
    )


def _tool_call(
    name: str,
    arguments: dict[str, Any],
    call_id: str = "call_1",
    content: str | None = None,
    tokens: int = 10,
) -> LLMResponse:
    """构造带工具调用的响应。"""
    return LLMResponse(
        content=content,
        tool_calls=[
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
        prompt_tokens=tokens,
        completion_tokens=tokens,
        total_tokens=tokens * 2,
    )


@pytest.fixture
def trace(tmp_path: Path) -> Iterator[RunTrace]:
    """构造一个临时 trace。"""
    with RunTrace(trace_dir=tmp_path / "traces", run_id="agent-test") as t:
        yield t


@pytest.fixture
def fake_tool() -> Iterator[None]:
    """注册一个测试工具，结束后移除。"""
    if "fake_echo" in registry.TOOLS:
        yield
        return

    registry.register(
        name="fake_echo",
        description="回显输入",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )(lambda text: f"echo: {text}")

    yield

    registry.TOOLS.pop("fake_echo", None)


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ─── 基础行为 ───


def test_agent_returns_direct_answer(trace: RunTrace, fake_tool: None) -> None:
    """无工具调用时直接返回答案。"""
    llm = FakeLLMClient([_text("hello world")])
    agent = Agent(llm=llm, trace=trace)

    assert agent.run("hi") == "hello world"
    assert len(llm.calls) == 1


def test_agent_calls_tool_then_returns(trace: RunTrace, fake_tool: None) -> None:
    """先调用工具，再返回答案。"""
    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "abc"}),
            _text("done: echo: abc"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)

    assert agent.run("say abc") == "done: echo: abc"
    assert len(llm.calls) == 2


def test_agent_message_flow(trace: RunTrace, fake_tool: None) -> None:
    """验证 messages 中 assistant 和 tool 消息的结构。"""
    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "xyz"}, call_id="c1"),
            _text("done"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)
    agent.run("echo xyz")

    second = llm.calls[1]["messages"]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]

    assistant_msg = second[2]
    assert assistant_msg["tool_calls"][0]["id"] == "c1"
    assert assistant_msg["tool_calls"][0]["function"]["name"] == "fake_echo"

    tool_msg = second[3]
    assert tool_msg["tool_call_id"] == "c1"
    assert tool_msg["content"] == "echo: xyz"


def test_agent_max_steps(trace: RunTrace, fake_tool: None) -> None:
    """连续调用工具达到 max_steps 时返回提示。"""
    responses = [_tool_call("fake_echo", {"text": str(i)}, call_id=f"c{i}") for i in range(5)]
    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=3)

    result = agent.run("loop forever")
    assert "max steps" in result.lower()
    assert len(llm.calls) == 3


# ─── 异常与错误处理 ───


def test_agent_unknown_tool_records_error(trace: RunTrace, fake_tool: None) -> None:
    """调用未注册工具时，错误应记录到 trace，Agent 继续。"""
    llm = FakeLLMClient(
        [
            _tool_call("not_a_real_tool", {}),
            _text("recovered"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)

    assert agent.run("call unknown") == "recovered"

    tool_events = [e for e in _events(trace.path) if e["event"] == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["error"] is not None


def test_agent_invalid_arguments_json(trace: RunTrace, fake_tool: None) -> None:
    """工具参数 JSON 非法时，错误应记录，Agent 继续。"""
    bad = LLMResponse(
        content=None,
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "fake_echo",
                    "arguments": "{not valid json",
                },
            }
        ],
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    llm = FakeLLMClient([bad, _text("done")])
    agent = Agent(llm=llm, trace=trace)

    assert agent.run("test") == "done"

    tool_events = [e for e in _events(trace.path) if e["event"] == "tool_call"]
    assert tool_events[0]["error"] is not None


def test_agent_llm_exception(trace: RunTrace, fake_tool: None) -> None:
    """LLM 调用异常应记录并返回错误提示。"""

    class FailingLLM:
        model = "failing"

        def chat(self, messages, tools=None, temperature=0.0):
            raise RuntimeError("network down")

    agent = Agent(llm=FailingLLM(), trace=trace)
    result = agent.run("anything")
    assert "failed" in result.lower()

    error_events = [e for e in _events(trace.path) if e["event"] == "error"]
    assert len(error_events) == 1


# ─── Trace 集成 ───


def test_agent_trace_records_llm_and_tool(trace: RunTrace, fake_tool: None) -> None:
    """LLM 和工具调用都应记录到 trace。"""
    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "hi"}, call_id="c1"),
            _text("done"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)
    agent.run("go")

    events = _events(trace.path)
    llm_events = [e for e in events if e["event"] == "llm_call"]
    tool_events = [e for e in events if e["event"] == "tool_call"]

    assert len(llm_events) == 2
    assert len(tool_events) == 1
    assert llm_events[0]["tool_calls"][0]["name"] == "fake_echo"
    assert tool_events[0]["tool"] == "fake_echo"
    assert tool_events[0]["arguments"] == {"text": "hi"}


def test_agent_trace_summary_tokens(trace: RunTrace, fake_tool: None) -> None:
    """summary 应累计所有 LLM 调用的 Token。"""
    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "a"}, tokens=100),
            _text("done", tokens=50),
        ]
    )
    agent = Agent(llm=llm, trace=trace)
    agent.run("go")

    s = trace.summary()
    assert s["llm_calls"] == 2
    assert s["tool_calls"] == 1
    assert s["total_tokens"] == 100 * 2 + 50 * 2


# ─── 系统提示与工具 schema ───


def test_agent_passes_system_prompt(trace: RunTrace, fake_tool: None) -> None:
    """第一次调用的 messages 应以 system prompt 开头。"""
    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace, system_prompt="CUSTOM PROMPT")
    agent.run("hi")

    first = llm.calls[0]["messages"]
    assert first[0]["role"] == "system"
    assert first[0]["content"] == "CUSTOM PROMPT"


def test_agent_passes_tool_schemas(trace: RunTrace, fake_tool: None) -> None:
    """每次调用都应传递工具 schema。"""
    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace)
    agent.run("hi")

    names = {s["function"]["name"] for s in llm.calls[0]["tools"]}
    assert "fake_echo" in names


def test_agent_injects_closing_hint_near_max_steps(trace: RunTrace, fake_tool: None) -> None:
    """接近 max_steps 时应注入收尾提示。"""
    responses = [_tool_call("fake_echo", {"text": str(i)}, call_id=f"c{i}") for i in range(5)]
    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=3)
    agent.run("go")

    # 第 1 步 (remaining=2) 的消息里应含提示
    step1_messages = llm.calls[1]["messages"]
    hints = [m for m in step1_messages if m["role"] == "user" and "工具调用机会" in m.get("content", "")]
    assert len(hints) == 1


def test_agent_disables_tools_on_last_step(trace: RunTrace, fake_tool: None) -> None:
    """最后一步不应再传工具 schema。"""
    responses = [_tool_call("fake_echo", {"text": str(i)}, call_id=f"c{i}") for i in range(5)]
    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=3)
    agent.run("go")

    last_tools = llm.calls[-1]["tools"]
    assert last_tools == []


def test_agent_uses_context_manager(trace: RunTrace, fake_tool: None) -> None:
    """Agent 应在每轮调用 LLM 前用 ContextManager 构建消息并传入 state。"""
    from breeze.context import ContextManager

    seen_state: list[bool] = []
    calls: list[int] = []

    class RecordingContext(ContextManager):
        def build(self, messages, state=None):
            seen_state.append(state is not None)
            return super().build(messages, state=state)

    class RecordingLLM:
        model = "recorder"

        def __init__(self) -> None:
            self.responses = [_tool_call("fake_echo", {"text": str(i)}, call_id=f"c{i}") for i in range(3)] + [
                _text("done")
            ]

        def chat(self, messages, tools=None, temperature=0.0):
            calls.append(len(messages))
            return self.responses.pop(0)

    agent = Agent(llm=RecordingLLM(), trace=trace, max_steps=6, context=RecordingContext())
    assert agent.run("go") == "done"

    # 每轮 build 都传入了 SessionState
    assert seen_state == [True] * 4
    assert len(calls) == 4


# ─── 编辑能力集成 ───


def test_agent_system_prompt_contains_edit_guidance() -> None:
    """默认系统提示应包含编辑相关指引。"""
    from breeze.agent import DEFAULT_SYSTEM_PROMPT

    prompt = DEFAULT_SYSTEM_PROMPT
    assert "apply_patch" in prompt
    assert "git_diff" in prompt
    assert "git_restore" in prompt
    assert "run_tests" in prompt
    assert "不要修改测试文件" in prompt
    assert "不得以方案、计划或建议结尾" in prompt
    assert "先 grep 搜索符号名" in prompt
    assert "禁止凭记忆编写 old" in prompt


def test_agent_max_edit_attempts_limits_patches(trace: RunTrace, monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 max_edit_attempts 的 apply_patch 调用应被拦截。"""
    from breeze.agent import Agent
    from breeze.tools import registry

    executed: list[str] = []

    def fake_execute(name, arguments):
        executed.append(name)
        return f"Patched {arguments.get('path', '?')} (1 replacement)"

    monkeypatch.setattr(registry, "execute", fake_execute)

    responses = [
        _tool_call(
            "apply_patch",
            {"path": "x.py", "old": "a", "new": "b"},
            call_id=f"c{i}",
        )
        for i in range(6)
    ] + [_text("done")]

    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=10, max_edit_attempts=3)
    result = agent.run("edit x.py")

    assert result == "done"
    assert executed.count("apply_patch") == 3

    tool_events = [e for e in _events(trace.path) if e["event"] == "tool_call"]
    apply_events = [e for e in tool_events if e["tool"] == "apply_patch"]
    assert len(apply_events) == 6

    errors = [e for e in apply_events if e["error"]]
    assert len(errors) == 3
    assert all("max_edit_attempts" in e["error"] for e in errors)


def test_agent_edit_count_resets_between_runs(trace: RunTrace, monkeypatch: pytest.MonkeyPatch) -> None:
    """两次 run 的编辑计数独立。"""
    from breeze.agent import Agent
    from breeze.tools import registry

    executed: list[str] = []

    def fake_execute(name, arguments):
        executed.append(name)
        return "Patched"

    monkeypatch.setattr(registry, "execute", fake_execute)

    # 第一次 run：3 次调用
    responses1 = [
        _tool_call(
            "apply_patch",
            {"path": "x.py", "old": "a", "new": "b"},
            call_id=f"c{i}",
        )
        for i in range(3)
    ] + [_text("done1")]

    llm1 = FakeLLMClient(responses1)
    agent1 = Agent(llm=llm1, trace=trace, max_steps=10, max_edit_attempts=3)
    assert agent1.run("first") == "done1"
    assert executed.count("apply_patch") == 3

    # 第二次 run：又是 3 次调用，应全部执行
    executed.clear()
    responses2 = [
        _tool_call(
            "apply_patch",
            {"path": "y.py", "old": "c", "new": "d"},
            call_id=f"d{i}",
        )
        for i in range(3)
    ] + [_text("done2")]

    llm2 = FakeLLMClient(responses2)
    agent2 = Agent(llm=llm2, trace=trace, max_steps=10, max_edit_attempts=3)
    assert agent2.run("second") == "done2"
    assert executed.count("apply_patch") == 3


def test_agent_edit_count_ignores_other_tools(
    trace: RunTrace, fake_tool: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 apply_patch 工具不占编辑配额。"""
    from breeze.agent import Agent

    # 前 3 轮用 fake_echo，后 3 轮用 apply_patch（应全部执行）
    responses = (
        [_tool_call("fake_echo", {"text": "x"}, call_id=f"e{i}") for i in range(3)]
        + [
            _tool_call(
                "apply_patch",
                {"path": "x.py", "old": "a", "new": "b"},
                call_id=f"p{i}",
            )
            for i in range(3)
        ]
        + [_text("done")]
    )

    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=10, max_edit_attempts=3)
    result = agent.run("mixed")

    assert result == "done"

    tool_events = [e for e in _events(trace.path) if e["event"] == "tool_call"]
    apply_events = [e for e in tool_events if e["tool"] == "apply_patch"]
    errors = [e for e in apply_events if e["error"]]
    assert len(errors) == 0


# ─── 配置集成 ───


def test_agent_uses_settings_for_max_steps(trace: RunTrace, monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 max_steps 时用 settings 的值。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "agent_max_steps", 7)

    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace)

    assert agent.max_steps == 7


def test_agent_explicit_max_steps_overrides_settings(trace: RunTrace, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式传 max_steps 时覆盖 settings。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "agent_max_steps", 7)

    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace, max_steps=3)

    assert agent.max_steps == 3


def test_agent_uses_settings_for_max_edit_attempts(trace: RunTrace, monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 max_edit_attempts 时用 settings 的值。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "agent_max_edit_attempts", 2)

    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace)

    assert agent.max_edit_attempts == 2


# ─── 结构化会话状态接线 ───


def test_agent_updates_state_from_real_tool_results(
    trace: RunTrace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具执行后 SessionState 应记录读取范围与符号。"""
    from breeze.config import settings

    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)
    (root / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")

    llm = FakeLLMClient(
        [
            _tool_call("read_file", {"path": "a.py"}),
            _text("done"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)
    agent.run("read a.py")

    assert "a.py" in agent.state.reads
    assert agent.state.symbols["target"] == "a.py:1"


def test_agent_injects_state_block_on_compression(
    trace: RunTrace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """压缩发生后，发给 LLM 的消息应包含状态块。"""
    from breeze.config import settings
    from breeze.context import ContextConfig, ContextManager

    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)
    (root / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")

    llm = FakeLLMClient(
        [
            _tool_call("read_file", {"path": "a.py"}, call_id="c1"),
            _tool_call("read_file", {"path": "a.py", "start_line": 1, "end_line": 2}, call_id="c2"),
            _text("done"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, context=ContextManager(ContextConfig(intra_turn_budget=1)))
    answer = agent.run("read a.py twice")

    assert answer == "done"
    final_messages = llm.calls[2]["messages"]
    joined = " ".join(str(m.get("content", "")) for m in final_messages)
    assert "[Session state" in joined
    assert "[read_file compressed]" in joined


def test_agent_halfway_nudge_when_no_edits(trace: RunTrace, fake_tool: None) -> None:
    """步数过半仍未编辑时应注入收敛提醒。"""
    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "a"}),
            _tool_call("fake_echo", {"text": "b"}),
            _tool_call("fake_echo", {"text": "c"}),
            _tool_call("fake_echo", {"text": "d"}),
            _text("done"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=8)
    assert agent.run("explore") == "done"

    def _flatten(call: dict[str, Any]) -> str:
        return " ".join(str(m.get("content", "")) for m in call["messages"])

    assert not any("一半的步数" in _flatten(call) for call in llm.calls[:4])
    assert any("一半的步数" in _flatten(call) for call in llm.calls[4:])


def test_agent_no_halfway_nudge_after_edit(trace: RunTrace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已有编辑动作时不注入半程提醒。"""
    from breeze.config import settings

    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")

    responses = [
        _tool_call(
            "apply_patch",
            {"path": "a.py", "old": "x = 1", "new": "x = 2"},
            call_id="edit",
        ),
    ]
    # 填充到 step==4 之前，确保越过半程点时 edit_count 已非零。
    for i in range(3):
        responses.append(_tool_call("read_file", {"path": "a.py"}, call_id=f"r{i}"))
    responses.append(_text("done"))

    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=8)
    assert agent.run("edit a.py") == "done"

    def _flatten(call: dict[str, Any]) -> str:
        return " ".join(str(m.get("content", "")) for m in call["messages"])

    assert not any("一半的步数" in _flatten(call) for call in llm.calls)


def test_agent_executes_parallel_tool_calls_in_one_step(trace: RunTrace, fake_tool: None) -> None:
    """一轮返回多个工具调用时，应全部执行且只消耗一步。"""
    resp = LLMResponse(
        content=None,
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "fake_echo", "arguments": json.dumps({"text": "a"})},
            },
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "fake_echo", "arguments": json.dumps({"text": "b"})},
            },
        ],
        prompt_tokens=5,
        completion_tokens=5,
        total_tokens=10,
    )
    llm = FakeLLMClient([resp, _text("done")])
    agent = Agent(llm=llm, trace=trace, max_steps=4)

    assert agent.run("echo both") == "done"
    assert len(llm.calls) == 2

    messages = llm.calls[1]["messages"]
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert len(assistant["tool_calls"]) == 2
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}
    assert "echo: a" in tool_msgs[0]["content"] and "echo: b" in tool_msgs[1]["content"]

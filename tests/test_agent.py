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


def test_agent_uses_context_manager(trace: RunTrace, fake_tool: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent 应在调用 LLM 前压缩 messages。"""
    from breeze.context import ContextConfig, ContextManager

    captured_lengths: list[int] = []

    class RecordingLLM:
        model = "recorder"

        def __init__(self) -> None:
            self.responses = [_tool_call("fake_echo", {"text": str(i)}, call_id=f"c{i}") for i in range(5)] + [
                _text("done")
            ]

        def chat(self, messages, tools=None, temperature=0.0):
            captured_lengths.append(len(messages))
            return self.responses.pop(0)

    cm = ContextManager(ContextConfig(max_recent_pairs=1))
    agent = Agent(llm=RecordingLLM(), trace=trace, max_steps=6, context=cm)
    agent.run("go")

    # 未压缩时长度会线性增长到 13 左右（6 轮 × 2 + 2 head + 注入提示）
    # 压缩后应稳定在小范围内，由 head(2) + summary(1) + recent 轮决定
    # max_recent_pairs=1 时 recent 轮最多 3 条（含收尾注入），上限为 6
    assert max(captured_lengths) <= 7
    assert len(captured_lengths) == 6

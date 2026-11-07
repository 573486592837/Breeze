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
        tool_choice: str | None = None,
    ) -> LLMResponse:
        self.calls.append({"messages": list(messages), "tools": tools, "tool_choice": tool_choice})
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

        def chat(self, messages, tools=None, temperature=0.0, tool_choice=None):
            raise RuntimeError("network down")

    agent = Agent(llm=FailingLLM(), trace=trace)
    result = agent.run("anything")
    assert "failed" in result.lower()

    error_events = [e for e in _events(trace.path) if e["event"] == "error"]
    assert len(error_events) == 1
    assert agent.ledger.records == []


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


def test_default_system_prompt_includes_agents_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """仓库根目录的 AGENTS.md 应拼进默认系统提示。"""
    from breeze.agent import default_system_prompt
    from breeze.config import settings

    (tmp_path / "AGENTS.md").write_text("总是先跑 tests", encoding="utf-8")
    monkeypatch.setattr(settings, "repo_root", tmp_path)

    prompt = default_system_prompt()

    assert "[项目指引 — AGENTS.md]" in prompt
    assert "总是先跑 tests" in prompt


def test_default_system_prompt_lists_missing_external_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺失的外部命令应在默认提示里点名，并给出替代工具。"""
    from breeze.agent import default_system_prompt

    monkeypatch.setattr(
        "breeze.capabilities.shutil.which",
        lambda name: None if name in {"rg", "node"} else f"/fake/{name}",
    )

    prompt = default_system_prompt()
    assert prompt.startswith("你是 Breeze")
    assert "缺失：rg、node" in prompt
    assert "grep 工具" in prompt


def test_default_system_prompt_without_missing_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    """外部命令全部可用时不应出现缺失提示。"""
    from breeze.agent import default_system_prompt

    monkeypatch.setattr("breeze.capabilities.shutil.which", lambda name: f"/fake/{name}")

    prompt = default_system_prompt()
    assert "外部命令可用：git、rg、node。" in prompt
    assert "缺失" not in prompt


def test_agent_default_system_prompt_reaches_llm(trace: RunTrace, fake_tool: None) -> None:
    """不传 system_prompt 时，环境说明应出现在发给 LLM 的 system 消息里。"""
    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace)
    agent.run("hi")

    system = llm.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "运行环境：" in system["content"]


def _caps(**overrides: bool) -> Any:
    from breeze.capabilities import Capabilities

    values: dict[str, bool] = {
        "git": True,
        "git_repo": True,
        "node": True,
        "rg": True,
        "python_project": True,
    }
    values.update(overrides)
    return Capabilities(**values)


def _tool_names(llm: FakeLLMClient) -> set[str]:
    tools = llm.calls[0]["tools"] or []
    return {tool["function"]["name"] for tool in tools}


def test_agent_hides_git_tools_outside_git_repo(trace: RunTrace) -> None:
    """非 git 工作区不下发 git_diff / git_restore。"""
    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace, capabilities=_caps(git_repo=False))
    agent.run("hi")

    names = _tool_names(llm)
    assert "git_diff" not in names
    assert "git_restore" not in names
    assert "apply_patch" in names


def test_agent_hides_run_tests_without_python_project(trace: RunTrace) -> None:
    """非 Python 项目不下发 run_tests。"""
    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace, capabilities=_caps(python_project=False))
    agent.run("hi")

    assert "run_tests" not in _tool_names(llm)


def test_agent_keeps_capability_tools_when_available(trace: RunTrace) -> None:
    """环境齐全时下发 git 与测试工具。"""
    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace, capabilities=_caps())
    agent.run("hi")

    assert {"git_diff", "git_restore", "run_tests"} <= _tool_names(llm)


def test_default_system_prompt_adapts_git_rules() -> None:
    """非 git 工作区的提示词不再建议 git 工具，并说明替代方案。"""
    from breeze.agent import default_system_prompt

    prompt = default_system_prompt(_caps(git_repo=False))

    assert "git_restore 不可用" in prompt
    assert "用 git_restore 回滚" not in prompt
    assert "当前目录不是 git 工作区" in prompt


def test_default_system_prompt_marks_run_tests_unavailable() -> None:
    """非 Python 项目应改写依赖 run_tests 的规则。"""
    from breeze.agent import default_system_prompt

    prompt = default_system_prompt(_caps(python_project=False))

    assert "run_tests 不可用" in prompt
    assert "优先使用 run_tests" not in prompt
    assert "项目自带的测试命令" in prompt


def test_default_system_prompt_reflects_confirm_mode() -> None:
    """confirm 模式提示模型可以请求用户确认非白名单命令。"""
    from breeze.agent import default_system_prompt

    prompt = default_system_prompt(_caps(), permission_mode="confirm")

    assert "先询问用户是否执行" in prompt
    assert "可执行文件仅限" not in prompt


def test_default_system_prompt_reflects_open_mode() -> None:
    """open 模式提示模型可以执行任意命令。"""
    from breeze.agent import default_system_prompt

    prompt = default_system_prompt(_caps(), permission_mode="open")

    assert "open 模式" in prompt
    assert "可执行任意命令" in prompt


def test_default_system_prompt_strict_keeps_whitelist() -> None:
    """strict 模式保留原白名单表述。"""
    from breeze.agent import default_system_prompt

    prompt = default_system_prompt(_caps(), permission_mode="strict")

    assert "可执行文件仅限" in prompt


def test_default_system_prompt_reflects_edit_failure_limit() -> None:
    """编辑上限应来自 max_edit_failures，而不是过时的硬编码。"""
    from breeze.agent import default_system_prompt

    prompt = default_system_prompt(_caps(), max_edit_failures=7)

    assert "连续写操作失败 7 次" in prompt
    assert "默认 5 次" not in prompt


def test_default_system_prompt_lists_read_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置只读参考目录后，环境说明应告知模型。"""
    from breeze.agent import default_system_prompt
    from breeze.config import settings

    ref = tmp_path / "ref"
    ref.mkdir()
    monkeypatch.setattr(settings, "tools_read_roots", (ref.resolve(),))

    prompt = default_system_prompt(_caps())

    assert "只读参考目录" in prompt
    assert str(ref.resolve()) in prompt


def test_agent_refreshes_edit_limit_prompt(trace: RunTrace) -> None:
    """修改 max_edit_failures 后，下一回合的 system prompt 应同步。"""
    llm = FakeLLMClient([_text("one"), _text("two")])
    agent = Agent(llm=llm, trace=trace, max_edit_failures=3)

    agent.run("first")
    agent.max_edit_failures = 7
    agent.run("second")

    assert "连续写操作失败 3 次" in llm.calls[0]["messages"][0]["content"]
    assert "连续写操作失败 7 次" in llm.calls[1]["messages"][0]["content"]


def test_agent_prompt_follows_permission_mode(trace: RunTrace) -> None:
    """Agent 构造的系统提示应包含当前权限模式。"""
    llm = FakeLLMClient([_text("ok")])
    agent = Agent(
        llm=llm,
        trace=trace,
        capabilities=_caps(),
        permission_mode="open",
    )

    agent.run("hi")

    system = llm.calls[0]["messages"][0]["content"]
    assert "open 模式" in system


def test_agent_refreshes_capabilities_after_creating_pyproject(
    trace: RunTrace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """创建 Python 项目标记后，下一步即可看到 run_tests。"""
    from breeze.agent import Agent
    from breeze.config import settings
    from breeze.tools import registry

    monkeypatch.setattr(settings, "repo_root", tmp_path)
    state = {"caps": _caps(python_project=False, git_repo=False)}

    monkeypatch.setattr("breeze.agent.detect", lambda _root: state["caps"])

    def fake_execute(name, _arguments):
        if name == "create_file":
            state["caps"] = _caps(python_project=True, git_repo=False)
            return "Created pyproject.toml"
        return "ok"

    monkeypatch.setattr(registry, "execute", fake_execute)

    llm = FakeLLMClient(
        [
            _tool_call(
                "create_file",
                {"path": "pyproject.toml", "content": "[project]\nname='demo'\n"},
                call_id="c1",
            ),
            _text("done"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=4)

    assert agent.run("init project") == "done"

    first_tools = {tool["function"]["name"] for tool in llm.calls[0]["tools"]}
    second_tools = {tool["function"]["name"] for tool in llm.calls[1]["tools"]}
    assert "run_tests" not in first_tools
    assert "run_tests" in second_tools
    assert "未检测到 Python 项目" in llm.calls[0]["messages"][0]["content"]
    assert "未检测到 Python 项目" not in llm.calls[1]["messages"][0]["content"]


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
    hints = [m for m in step1_messages if m["role"] == "system" and "工具调用机会" in m.get("content", "")]
    assert len(hints) == 1


def test_agent_keeps_tools_but_forbids_calls_on_last_step(
    trace: RunTrace, fake_tool: None
) -> None:
    """最后一步保留工具 schema 以维持缓存，但要求模型不要调用。"""
    responses = [_tool_call("fake_echo", {"text": str(i)}, call_id=f"c{i}") for i in range(5)]
    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=3)
    agent.run("go")

    last = llm.calls[-1]
    assert last["tools"] != []
    assert last["tool_choice"] == "none"
    assert llm.calls[0]["tool_choice"] is None


def test_agent_ignores_tool_calls_on_last_step(trace: RunTrace, fake_tool: None) -> None:
    """provider 无视 tool_choice 时，最终步的工具调用不执行。"""
    responses = [
        _tool_call("fake_echo", {"text": "one"}, call_id="c1"),
        _tool_call("fake_echo", {"text": "two"}, call_id="c2"),
        _tool_call("fake_echo", {"text": "three"}, call_id="c3", content="final answer"),
    ]
    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=3)
    assert agent.run("go") == "final answer"

    tool_events = [e for e in _events(trace.path) if e["event"] == "tool_call"]
    assert len(tool_events) == 2
    guards = [e for e in _events(trace.path) if e["event"] == "guard"]
    assert any("ignored at final step" in e["message"] for e in guards)


def test_agent_uses_context_manager(trace: RunTrace, fake_tool: None) -> None:
    """Agent 应在每轮调用 LLM 前用 ContextManager 构建消息并传入 state。"""
    from breeze.context import ContextManager

    seen_state: list[bool] = []
    calls: list[int] = []

    class RecordingContext(ContextManager):
        def build_with_stats(
            self,
            messages,
            state=None,
            archive=None,
            plan=None,
            *,
            include_state: bool = False,
            snapshots=None,
            instructions=None,
        ):
            seen_state.append(state is not None)
            return super().build_with_stats(
                messages,
                state=state,
                archive=archive,
                plan=plan,
                include_state=include_state,
                snapshots=snapshots,
                instructions=instructions,
            )

    class RecordingLLM:
        model = "recorder"

        def __init__(self) -> None:
            self.responses = [_tool_call("fake_echo", {"text": str(i)}, call_id=f"c{i}") for i in range(3)] + [
                _text("done")
            ]

        def chat(self, messages, tools=None, temperature=0.0, tool_choice=None):
            calls.append(len(messages))
            return self.responses.pop(0)

    agent = Agent(llm=RecordingLLM(), trace=trace, max_steps=6, context=RecordingContext())
    assert agent.run("go") == "done"

    # 每轮 build 都传入了 SessionState
    assert seen_state == [True] * 4
    assert len(calls) == 4


def test_agent_logs_prompt_breakdown_and_prefix_reuse(trace: RunTrace, fake_tool: None) -> None:
    """每次 LLM 调用应记录组成，第二次调用能复用第一批消息。"""
    from breeze.agent import Agent

    responses = [_tool_call("fake_echo", {"text": "x"}, call_id="c1"), _text("done")]
    agent = Agent(llm=FakeLLMClient(responses), trace=trace, max_steps=4)
    assert agent.run("go") == "done"

    events = [e for e in _events(trace.path) if e["event"] == "llm_call"]
    assert events[0]["prompt_breakdown"]["system"] > 0
    assert events[0]["cacheable_prefix_tokens"] == 0
    assert events[1]["cacheable_prefix_tokens"] > 0


# ─── 编辑能力集成 ───


def test_agent_system_prompt_contains_edit_guidance() -> None:
    """默认系统提示应包含编辑相关指引。"""
    from breeze.agent import DEFAULT_SYSTEM_PROMPT

    prompt = DEFAULT_SYSTEM_PROMPT
    assert "apply_patch" in prompt
    assert "git_diff" in prompt
    assert "git_restore" in prompt
    assert "run_tests" in prompt
    assert "不要删除或弱化测试" in prompt
    assert "不得以方案、计划或建议结尾" in prompt
    assert "先 grep 搜索符号名" in prompt
    assert "禁止凭记忆编写 old" in prompt
    assert "必须退出码为 0" in prompt
    assert '不得以"与本次任务无关"为由跳过' in prompt
    assert "不要用 run_shell 调 grep" in prompt
    assert "cwd 参数" in prompt
    assert "全量测试用 run_tests" in prompt
    assert "update_plan" in prompt


def test_agent_appends_session_plan_after_question(trace: RunTrace) -> None:
    """已有计划时，第一次调用就把它追加在问题之后。"""
    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace, system_prompt="SYS")
    agent.state.plan = "1. inspect\n2. patch"

    agent.run("hi")

    messages = llm.calls[0]["messages"]
    assert messages[0]["content"] == "SYS"
    assert messages[1]["content"] == "hi"
    assert messages[-1]["content"].startswith("[Current plan")
    assert "2. patch" in messages[-1]["content"]


def test_agent_append_mode_keeps_previous_turn_messages(
    trace: RunTrace, fake_tool: None
) -> None:
    """append 模式保留上一回合的完整消息，不注入回合归档。"""
    from breeze.context import ContextConfig, ContextManager

    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "one"}, call_id="t1c1"),
            _text("first answer"),
            _text("second answer"),
        ]
    )
    context = ContextManager(ContextConfig(enable=False, cross_turn="append"))
    agent = Agent(llm=llm, trace=trace, context=context)
    agent.run("q1")
    agent.run("q2")

    messages = llm.calls[2]["messages"]
    joined = " ".join(str(m.get("content", "")) for m in messages)
    assert "q1" in joined
    assert "echo: one" in joined
    assert "first answer" in joined
    assert "q2" in joined
    assert not any("Completed turns" in str(m.get("content", "")) for m in messages)


def test_agent_update_plan_is_injected_next_step(trace: RunTrace) -> None:
    """update_plan 写入状态，并在下一次 LLM 调用时追加注入。"""
    llm = FakeLLMClient(
        [
            _tool_call("update_plan", {"content": "1. inspect\n2. patch"}, call_id="p1"),
            _text("done"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, system_prompt="SYS")

    assert agent.run("plan it") == "done"

    assert agent.state.plan == "1. inspect\n2. patch"
    assert not any("[Current plan" in str(m.get("content", "")) for m in llm.calls[0]["messages"])
    second = llm.calls[1]["messages"]
    plan_msgs = [m for m in second if m["role"] == "system" and "[Current plan" in m["content"]]
    assert len(plan_msgs) == 1
    assert "2. patch" in plan_msgs[0]["content"]


def test_many_successful_edits_do_not_hit_failure_cap(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续成功的并行编辑不受失败上限约束。"""
    from breeze.agent import Agent
    from breeze.tools import registry

    executed: list[str] = []

    def fake_execute(name, arguments):
        executed.append(name)
        return f"Patched {arguments.get('path', '?')} (1 replacement)"

    monkeypatch.setattr(registry, "execute", fake_execute)

    responses = [
        _tool_call("apply_patch", {"path": f"f{i}.py", "old": "a", "new": "b"}, call_id=f"c{i}")
        for i in range(6)
    ] + [_text("done")]

    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=10, max_edit_failures=3)
    result = agent.run("edit six files")

    assert result == "done"
    assert executed.count("apply_patch") == 6

    tool_events = [e for e in _events(trace.path) if e["event"] == "tool_call"]
    apply_events = [e for e in tool_events if e["tool"] == "apply_patch"]
    assert len(apply_events) == 6
    assert not [e for e in apply_events if e["error"]]


def test_agent_stops_after_consecutive_edit_failures(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续失败达到上限后，本回合后续编辑被拒绝。"""
    from breeze.agent import Agent
    from breeze.tools import registry

    executed: list[str] = []

    def fake_execute(name, _arguments):
        executed.append(name)
        return "Error: old text not found in x.py"

    monkeypatch.setattr(registry, "execute", fake_execute)

    responses = [
        _tool_call("apply_patch", {"path": "x.py", "old": "a", "new": "b"}, call_id=f"c{i}")
        for i in range(6)
    ] + [_text("done")]

    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=10, max_edit_failures=3)
    result = agent.run("edit x.py")

    assert result == "done"
    assert executed.count("apply_patch") == 3

    tool_events = [e for e in _events(trace.path) if e["event"] == "tool_call"]
    apply_events = [e for e in tool_events if e["tool"] == "apply_patch"]
    assert len(apply_events) == 6

    rejected = [e for e in apply_events if e["error"]]
    assert len(rejected) == 3
    assert all("max_edit_failures" in e["error"] for e in rejected)
    assert all("do not claim" in e["error"] for e in rejected)


def test_agent_edit_failure_streak_resets_on_success(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """成功编辑会清零连续失败计数，卡顿后仍可继续编辑。"""
    from breeze.agent import Agent
    from breeze.tools import registry

    results = iter(
        [
            "Error: old text not found",
            "Error: old text not found",
            "Patched x.py (1 replacement)",
            "Error: old text not found",
            "Error: old text not found",
            "Patched x.py (1 replacement)",
        ]
    )
    executed: list[str] = []

    def fake_execute(name, _arguments):
        executed.append(name)
        return next(results)

    monkeypatch.setattr(registry, "execute", fake_execute)

    responses = [
        _tool_call("apply_patch", {"path": "x.py", "old": "a", "new": "b"}, call_id=f"c{i}")
        for i in range(6)
    ] + [_text("done")]

    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=10, max_edit_failures=3)
    result = agent.run("edit x.py")

    assert result == "done"
    assert executed.count("apply_patch") == 6

    tool_events = [e for e in _events(trace.path) if e["event"] == "tool_call"]
    apply_events = [e for e in tool_events if e["tool"] == "apply_patch"]
    assert not [e for e in apply_events if e["error"]]


def test_agent_edit_failure_cap_resets_between_runs(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两次 run 的失败计数独立。"""
    from breeze.agent import Agent
    from breeze.tools import registry

    results = iter(
        [
            "Error: old text not found",
            "Error: old text not found",
            "Error: old text not found",
            "Patched x.py (1 replacement)",
        ]
    )
    executed: list[str] = []

    def fake_execute(name, _arguments):
        executed.append(name)
        return next(results)

    monkeypatch.setattr(registry, "execute", fake_execute)

    responses = [
        _tool_call("apply_patch", {"path": "x.py", "old": "a", "new": "b"}, call_id=f"c{i}")
        for i in range(3)
    ] + [_text("done1")]
    responses += [_tool_call("apply_patch", {"path": "y.py", "old": "c", "new": "d"}, call_id="d0")]
    responses += [_text("done2")]

    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=10, max_edit_failures=3)

    assert agent.run("first") == "done1"
    assert agent.run("second") == "done2"
    assert executed.count("apply_patch") == 4


def test_agent_edit_failure_cap_ignores_read_tools(
    trace: RunTrace, fake_tool: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读工具失败不占编辑失败配额。"""
    from breeze.agent import Agent

    responses = (
        [_tool_call("fake_echo", {"text": "x"}, call_id=f"e{i}") for i in range(4)]
        + [_tool_call("apply_patch", {"path": "x.py", "old": "a", "new": "b"}, call_id="p0")]
        + [_text("done")]
    )

    llm = FakeLLMClient(responses)
    agent = Agent(llm=llm, trace=trace, max_steps=10, max_edit_failures=3)
    result = agent.run("mixed")

    assert result == "done"

    tool_events = [e for e in _events(trace.path) if e["event"] == "tool_call"]
    apply_events = [e for e in tool_events if e["tool"] == "apply_patch"]
    assert not [e for e in apply_events if e["error"]]


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


def test_agent_uses_settings_for_max_edit_failures(trace: RunTrace, monkeypatch: pytest.MonkeyPatch) -> None:
    """不传 max_edit_failures 时用 settings 的值。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "agent_max_edit_failures", 2)

    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace)

    assert agent.max_edit_failures == 2


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


def test_agent_clears_read_counts_between_turns(
    trace: RunTrace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每个用户回合重新计数，上一回合的重复读取不影响新回合。"""
    from breeze.config import settings

    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)
    (root / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")

    llm = FakeLLMClient(
        [
            _tool_call("read_file", {"path": "a.py"}, call_id="c1"),
            _text("first"),
            _tool_call("read_file", {"path": "a.py"}, call_id="c2"),
            _text("second"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)
    agent.run("read a.py")
    assert agent.state.read_counts[("a.py", None, None)] == 1

    agent.run("read a.py again")

    assert agent.state.read_counts[("a.py", None, None)] == 1


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
    agent = Agent(
        llm=llm,
        trace=trace,
        context=ContextManager(ContextConfig(intra_turn_budget=1, hard_budget=1)),
    )
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


# ─── 最终答案自检：声称已改但未编辑 ───


@pytest.mark.parametrize(
    "text",
    [
        "我已经修复了分数问题。",
        "我已经找到了根源并完成了修复。",
        "我修改了 css 定位。",
        "I have fixed the score bug.",
    ],
)
def test_claims_completed_edit_detects_first_person_claims(text: str) -> None:
    """第一人称完成声明应被识别。"""
    from breeze.agent import _claims_completed_edit

    assert _claims_completed_edit(text)


@pytest.mark.parametrize(
    "text",
    [
        "这个函数会修改文件内容。",
        "修复这个问题的思路是……",
        "本次未修改任何文件。",
        "The fix is already in place.",
    ],
)
def test_claims_completed_edit_ignores_non_claims(text: str) -> None:
    """解释性文字不应被误判为完成声明。"""
    from breeze.agent import _claims_completed_edit

    assert not _claims_completed_edit(text)


@pytest.mark.parametrize(
    "text",
    [
        "我将按上述方式完成实现。",
        "我尚未完成实现。",
        "我无法完成修改。",
        "下一步我会完成修复。",
    ],
)
def test_claims_completed_edit_ignores_future_and_negative(text: str) -> None:
    """计划、未来动作与未完成报告不应触发“已完成编辑”提醒。"""
    from breeze.agent import _claims_completed_edit

    assert not _claims_completed_edit(text)


def test_agent_reprompts_when_claiming_edit_without_edits(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """声称已改但本轮没有编辑时，应追加提醒并继续循环。"""
    from breeze.agent import Agent
    from breeze.tools import registry

    executed: list[str] = []
    monkeypatch.setattr(
        registry,
        "execute",
        lambda name, _arguments: executed.append(name) or "Patched x.py (1 replacement)",
    )

    llm = FakeLLMClient(
        [
            _text("我已经修复了分数问题。"),
            _tool_call("apply_patch", {"path": "x.py", "old": "a", "new": "b"}),
            _text("已完成。"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=10)

    assert agent.run("修好分数") == "已完成。"
    assert executed == ["apply_patch"]

    roles = [m["role"] for m in llm.calls[1]["messages"]]
    assert roles[-2:] == ["assistant", "system"]
    nudge = llm.calls[1]["messages"][-1]["content"]
    assert "没有" in nudge
    assert "apply_patch" in nudge

    guard_events = [e for e in _events(trace.path) if e["event"] == "guard"]
    assert len(guard_events) == 1
    assert trace.summary()["errors"] == 0


def test_agent_keeps_last_answer_at_step_limit(trace: RunTrace, fake_tool: None) -> None:
    """最后一步的文本答案必须原样返回，不能被 max steps 占位符覆盖。"""
    from breeze.agent import Agent

    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "x"}),
            _text("我将按上述方式完成实现。"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=2)

    assert agent.run("继续") == "我将按上述方式完成实现。"


def test_agent_annotates_false_edit_claim_at_step_limit(
    trace: RunTrace, fake_tool: None
) -> None:
    """最后一步无法重做时，完成声明前加事实标注而不是丢弃答案。"""
    from breeze.agent import Agent

    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "x"}),
            _text("我已经完成了实现。"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=2)

    answer = agent.run("继续")

    assert answer == "[未检测到成功的编辑] 我已经完成了实现。"
    guard_events = [e for e in _events(trace.path) if e["event"] == "guard"]
    assert any("no edit tool succeeded" in e["message"] for e in guard_events)


def test_agent_nudges_only_once_per_turn(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复声称已改时只提醒一次，之后保留答案并加事实标注。"""
    from breeze.agent import Agent

    monkeypatch.setattr("breeze.agent.registry.execute", lambda _name, _arguments: "ok")

    llm = FakeLLMClient([_text("我已经修复了。"), _text("我已经修复了。")])
    agent = Agent(llm=llm, trace=trace, max_steps=10)

    assert agent.run("修复") == "[未检测到成功的编辑] 我已经修复了。"
    assert len(llm.calls) == 2


def test_agent_no_reprompt_without_claim(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """普通解释性回答不触发提醒。"""
    from breeze.agent import Agent

    monkeypatch.setattr("breeze.agent.registry.execute", lambda _name, _arguments: "ok")

    llm = FakeLLMClient([_text("这个函数会修改文件内容。")])
    agent = Agent(llm=llm, trace=trace, max_steps=10)

    assert agent.run("解释一下") == "这个函数会修改文件内容。"
    assert len(llm.calls) == 1


def test_agent_no_reprompt_after_successful_edit(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本轮有成功编辑时，完成声明不再提醒。"""
    from breeze.agent import Agent
    from breeze.tools import registry

    monkeypatch.setattr(
        registry,
        "execute",
        lambda _name, _arguments: "Patched x.py (1 replacement)",
    )

    llm = FakeLLMClient(
        [
            _tool_call("apply_patch", {"path": "x.py", "old": "a", "new": "b"}),
            _text("我已经修复了分数问题。"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=10)

    assert agent.run("修好分数") == "我已经修复了分数问题。"
    assert len(llm.calls) == 2


def test_agent_reprompts_after_failed_edit(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """编辑工具返回 Error 时不算成功，完成声明仍触发提醒。"""
    from breeze.agent import Agent
    from breeze.tools import registry

    monkeypatch.setattr(
        registry,
        "execute",
        lambda _name, _arguments: "Error: old text not found in x.py",
    )

    llm = FakeLLMClient(
        [
            _tool_call("apply_patch", {"path": "x.py", "old": "a", "new": "b"}),
            _text("我已经修复了。"),
            _text("本轮未修改：补丁未匹配，需要重新读取目标区域。"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=10)

    assert agent.run("修复") == "本轮未修改：补丁未匹配，需要重新读取目标区域。"
    assert len(llm.calls) == 3


def test_agent_recovers_from_empty_final_answer(trace: RunTrace) -> None:
    """最终回答为空时应再请求一次总结，而不是直接返回空占位。"""
    from breeze.agent import Agent

    llm = FakeLLMClient([_text(""), _text("已完成撤销逻辑，界面与测试尚未完成。")])
    agent = Agent(llm=llm, trace=trace)

    assert agent.run("继续") == "已完成撤销逻辑，界面与测试尚未完成。"
    assert len(llm.calls) == 2
    assert llm.calls[1]["tools"] == []
    assert "没有内容" in llm.calls[1]["messages"][-1]["content"]
    guard_events = [e for e in _events(trace.path) if e["event"] == "guard"]
    assert [e["message"] for e in guard_events] == ["empty final answer; asking for a summary once"]


def test_agent_empty_answer_recovery_runs_once(trace: RunTrace) -> None:
    """恢复调用仍为空时返回占位文本，不无限重试。"""
    from breeze.agent import Agent

    llm = FakeLLMClient([_text(""), _text("")])
    agent = Agent(llm=llm, trace=trace, max_steps=1)

    assert agent.run("继续") == "(empty response)"
    assert len(llm.calls) == 2


@pytest.mark.parametrize(
    "text",
    [
        "Both failures are clear.\n\nLet me fix the test file. First add the pytest import:",
        "我将先修改 cli.py，然后补测试。",
        "首先添加 `get_run_tools`。",
        "让我直接修复 `tests/test_diff.py`。",
    ],
)
def test_is_plan_answer_detects_intent(text: str) -> None:
    """句首的第一人称意图或步骤引导视为计划式回答。"""
    from breeze.agent import _is_plan_answer

    assert _is_plan_answer(text)


@pytest.mark.parametrize(
    "text",
    [
        "建议下一步：在 query.py 末尾追加 get_run_tools。",
        "已完成实现：新增了 diff 命令，57 passed。",
        "Let me know if you need anything else.",
        "下一步 | 在 cli.py 注册 diff 命令。",
    ],
)
def test_is_plan_answer_ignores_reports(text: str) -> None:
    """最终报告里的建议与完成声明不算计划。"""
    from breeze.agent import _is_plan_answer

    assert not _is_plan_answer(text)


def test_agent_reprompts_when_answer_is_a_plan(trace: RunTrace, fake_tool: None) -> None:
    """无工具调用的计划式回答应触发一次纠正，而不是直接归档。"""
    llm = FakeLLMClient(
        [
            _text("Both failures are clear.\n\nLet me fix the test. First add the import:"),
            _tool_call("fake_echo", {"text": "done"}),
            _text("已修复并验证。"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=6)

    assert agent.run("修复测试") == "已修复并验证。"
    assert len(llm.calls) == 3
    assert "计划" in llm.calls[1]["messages"][-1]["content"]
    guards = [e for e in _events(trace.path) if e["event"] == "guard"]
    assert any("final answer was a plan" in e["message"] for e in guards)


def test_agent_plan_answer_repropted_once(trace: RunTrace, fake_tool: None) -> None:
    """连续给计划时只纠正一次，第二次计划作为最终答案返回。"""
    plan = "Let me fix the test file."
    llm = FakeLLMClient([_text(plan), _text(plan)])
    agent = Agent(llm=llm, trace=trace, max_steps=6)

    assert agent.run("修复测试") == plan
    assert len(llm.calls) == 2


# ─── 多回合会话 ───


def test_agent_archives_completed_turns(
    trace: RunTrace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二回合的 LLM 视图应含上一回合问答，并注入工作集而不是工具结果原文。"""
    from breeze.config import settings

    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)
    (root / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")

    llm = FakeLLMClient(
        [
            _tool_call("read_file", {"path": "a.py"}),
            _text("turn1 conclusion"),
            _text("turn2 answer"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)

    assert agent.run("where is target defined?") == "turn1 conclusion"
    assert agent.run("thanks") == "turn2 answer"

    turn2_view = llm.calls[2]["messages"]
    joined = " ".join(str(m.get("content", "")) for m in turn2_view)
    assert "[Completed turns" in joined
    assert "where is target defined?" in joined
    assert "turn1 conclusion" in joined
    # L2：上一回合触碰过的文件内容以工作集快照注入，而不是原始 tool 消息。
    assert "[Working set" in joined
    assert "def target():" in joined
    assert not any(m.get("role") == "tool" for m in turn2_view)

    # 归档的触碰文件来自真实工具参数
    assert agent.ledger.records[0].touched == ["a.py"]
    assert agent.ledger.touched_files() == ["a.py"]

    # 完整消息日志仍在 self.messages 里供调试
    assert any("def target():" in str(m.get("content", "")) for m in agent.messages)


def test_agent_injects_post_edit_snapshot_next_turn(
    trace: RunTrace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上一回合创建/编辑的文件内容应作为工作集注入下一回合。"""
    from breeze.config import settings

    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)

    llm = FakeLLMClient(
        [
            _tool_call("create_file", {"path": "new.py", "content": "value = 42\n"}),
            _text("created"),
            _text("answer"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)

    assert agent.run("create new.py") == "created"
    assert agent.run("what is the value?") == "answer"

    turn2_view = llm.calls[2]["messages"]
    joined = " ".join(str(m.get("content", "")) for m in turn2_view)
    assert "[Working set" in joined
    assert "value = 42" in joined


def test_agent_skips_snapshot_changed_between_turns(
    trace: RunTrace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文件在回合间被外部修改后，旧快照不得注入。"""
    from breeze.config import settings

    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)

    llm = FakeLLMClient(
        [
            _tool_call("create_file", {"path": "new.py", "content": "value = 1\n"}),
            _text("created"),
            _text("answer"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)

    assert agent.run("create new.py") == "created"
    target = root / "new.py"
    target.write_text("value = 2\n", encoding="utf-8")
    assert agent.run("what is the value?") == "answer"

    turn2_view = llm.calls[2]["messages"]
    joined = " ".join(str(m.get("content", "")) for m in turn2_view)
    assert "value = 1" not in joined


def test_agent_second_turn_view_only_grows_within_turn(
    trace: RunTrace,
    fake_tool: None,
) -> None:
    """回合内视图含本回合工具轮，不含上一回合的轮。"""
    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "one"}, call_id="t1c1"),
            _text("first answer"),
            _tool_call("fake_echo", {"text": "two"}, call_id="t2c1"),
            _text("second answer"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)
    agent.run("q1")
    agent.run("q2")

    # 第二次回合的首次 LLM 调用：system + 归档 + 当前问题
    roles = [m["role"] for m in llm.calls[2]["messages"]]
    assert roles == ["system", "system", "user"]

    # 第三次调用带上了本回合的工具轮
    roles = [m["role"] for m in llm.calls[3]["messages"]]
    assert roles == ["system", "system", "user", "assistant", "tool"]
    assert "echo: two" in str(llm.calls[3]["messages"][-1]["content"])
    assert "echo: one" not in " ".join(str(m.get("content", "")) for m in llm.calls[3]["messages"])


def test_agent_reset_clears_history_and_ledger(trace: RunTrace, fake_tool: None) -> None:
    """reset 后消息日志、归档与状态全部清空，下一回合无归档注入。"""
    llm = FakeLLMClient([_text("a1"), _text("a2")])
    agent = Agent(llm=llm, trace=trace)
    agent.run("q1")
    agent.reset()

    assert agent.messages == []
    assert agent.ledger.records == []
    assert agent.state.reads == {}

    agent.run("q2")
    joined = " ".join(str(m.get("content", "")) for m in llm.calls[1]["messages"])
    assert "[Completed turns" not in joined


def test_agent_archives_failed_turns(trace: RunTrace, fake_tool: None) -> None:
    """失败回合（步数耗尽）也进入归档，供下一回合知道未完成任务。"""
    responses = [_tool_call("fake_echo", {"text": str(i)}, call_id=f"c{i}") for i in range(3)]
    llm = FakeLLMClient(responses + [_text("recovered")])
    agent = Agent(llm=llm, trace=trace, max_steps=3)

    result = agent.run("loop forever")
    assert "max steps" in result.lower()
    assert agent.ledger.records[0].answer.startswith("Reached max steps")


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


# ─── 进度事件 ───


def test_agent_emits_progress_events(trace: RunTrace, fake_tool: None) -> None:
    """事件顺序覆盖每次 LLM 调用与工具调用，且带 Token 与耗时。"""
    from breeze.agent import AgentEvent

    llm = FakeLLMClient([_tool_call("fake_echo", {"text": "abc"}, tokens=7), _text("done", tokens=3)])
    events: list[AgentEvent] = []
    agent = Agent(llm=llm, trace=trace, on_event=events.append)

    assert agent.run("say abc") == "done"

    assert [e.kind for e in events] == [
        "llm_start",
        "llm_end",
        "tool_start",
        "tool_end",
        "llm_start",
        "llm_end",
    ]
    assert events[0].step == 0
    assert events[1].tokens == 14
    assert events[1].tool_calls == 1
    assert events[2].tool == "fake_echo"
    assert events[3].tool == "fake_echo"
    assert events[3].ok is True
    assert events[3].duration_ms >= 0.0
    assert events[5].step == 1
    assert events[5].tool_calls == 0


def test_agent_event_errors_do_not_affect_run(trace: RunTrace) -> None:
    """展示层回调抛异常时任务照常完成。"""
    llm = FakeLLMClient([_text("fine")])

    def boom(event) -> None:
        raise RuntimeError("ui broke")

    agent = Agent(llm=llm, trace=trace, on_event=boom)

    assert agent.run("hi") == "fine"


def test_agent_events_do_not_touch_messages(trace: RunTrace, fake_tool: None) -> None:
    """事件只做观察：消息序列与不传回调时一致。"""

    def build(on_event):
        llm = FakeLLMClient([_tool_call("fake_echo", {"text": "x"}), _text("ok")])
        agent = Agent(llm=llm, trace=trace, on_event=on_event)
        agent.run("go")
        return agent.messages, llm.calls

    with_events, calls_with = build(lambda _event: None)
    plain, calls_plain = build(None)

    assert with_events == plain
    assert calls_with[0]["messages"] == calls_plain[0]["messages"]


def test_agent_delete_counts_as_edit_for_claim_guard(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """delete_path 属于编辑操作，删除后的完成声明不应触发重申。"""

    def fake_execute(name: str, arguments: dict[str, Any]) -> str:
        return f"Deleted {arguments['path']}"

    monkeypatch.setattr(registry, "execute", fake_execute)

    llm = FakeLLMClient(
        [
            _tool_call("delete_path", {"path": "old.py"}, call_id="d1"),
            _text("我已经删除了 old.py"),
        ]
    )
    agent = Agent(llm=llm, trace=trace)

    assert agent.run("delete old.py") == "我已经删除了 old.py"
    assert len(llm.calls) == 2


# ─── 命令权限模式 ───


def _tool_messages(agent: Agent) -> list[str]:
    return [str(m.get("content", "")) for m in agent.messages if m.get("role") == "tool"]


def _permission_events(trace: RunTrace) -> list[dict[str, Any]]:
    return [e for e in _events(trace.path) if e["event"] == "permission"]


def test_agent_strict_permission_rejects_unlisted(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """strict 模式下非白名单命令按工具错误返回。"""

    def fake_execute(name: str, arguments: dict[str, Any]) -> str:
        raise ValueError("Executable not allowed: ls")

    monkeypatch.setattr(registry, "execute", fake_execute)

    llm = FakeLLMClient(
        [_tool_call("run_shell", {"command": "ls -la"}, call_id="s1"), _text("done")]
    )
    agent = Agent(llm=llm, trace=trace, permission_mode="strict")

    assert agent.run("list") == "done"
    assert any("Executable not allowed" in msg for msg in _tool_messages(agent))
    assert _permission_events(trace) == []


def test_agent_confirm_permission_allows(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """confirm 模式在用户同意后执行非白名单命令并记录事件。"""
    from breeze.tools import shell as shell_module

    executed: list[str] = []

    def reject(command: str, cwd: str = ".") -> None:
        raise ValueError("Executable not allowed: ls")

    monkeypatch.setattr(shell_module, "check_command", reject)
    monkeypatch.setattr(
        shell_module,
        "run_unlisted",
        lambda command, _cwd=".", **_kwargs: executed.append(command) or "ran",
    )

    llm = FakeLLMClient(
        [_tool_call("run_shell", {"command": "ls -la"}, call_id="s1"), _text("done")]
    )
    agent = Agent(
        llm=llm,
        trace=trace,
        permission_mode="confirm",
        confirm_command=lambda _command: True,
    )

    assert agent.run("list") == "done"
    assert executed == ["ls -la"]
    events = _permission_events(trace)
    assert events[-1]["decision"] == "allowed"
    assert events[-1]["mode"] == "confirm"


def test_agent_confirm_permission_denied(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """confirm 模式拒绝后不执行命令，并把拒绝结果返回给模型。"""
    from breeze.tools import shell as shell_module

    executed: list[str] = []

    def reject(command: str, cwd: str = ".") -> None:
        raise ValueError("Executable not allowed: ls")

    monkeypatch.setattr(shell_module, "check_command", reject)
    monkeypatch.setattr(
        shell_module,
        "run_unlisted",
        lambda command, _cwd=".", **_kwargs: executed.append(command) or "ran",
    )

    llm = FakeLLMClient(
        [_tool_call("run_shell", {"command": "ls -la"}, call_id="s1"), _text("done")]
    )
    agent = Agent(
        llm=llm,
        trace=trace,
        permission_mode="confirm",
        confirm_command=lambda _command: False,
    )

    assert agent.run("list") == "done"
    assert executed == []
    assert any("denied" in msg for msg in _tool_messages(agent))
    assert _permission_events(trace)[-1]["decision"] == "denied"


def test_agent_confirm_without_interactive_user_denies(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非交互环境没有确认回调时，confirm 模式直接拒绝。"""
    from breeze.tools import shell as shell_module

    def reject(command: str, cwd: str = ".") -> None:
        raise ValueError("Executable not allowed: ls")

    monkeypatch.setattr(shell_module, "check_command", reject)

    llm = FakeLLMClient(
        [_tool_call("run_shell", {"command": "ls -la"}, call_id="s1"), _text("done")]
    )
    agent = Agent(llm=llm, trace=trace, permission_mode="confirm", confirm_command=None)

    assert agent.run("list") == "done"
    assert any("no interactive user" in msg for msg in _tool_messages(agent))
    assert _permission_events(trace)[-1]["decision"] == "denied"


def test_agent_open_permission_runs_without_whitelist(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """open 模式跳过白名单直接执行。"""
    from breeze.tools import shell as shell_module

    executed: list[str] = []

    def reject(command: str, cwd: str = ".") -> None:
        raise ValueError("Executable not allowed: ls")

    monkeypatch.setattr(shell_module, "check_command", reject)
    monkeypatch.setattr(
        shell_module,
        "run_unlisted",
        lambda command, _cwd=".", **_kwargs: executed.append(command) or "ran",
    )

    llm = FakeLLMClient(
        [_tool_call("run_shell", {"command": "ls -la"}, call_id="s1"), _text("done")]
    )
    agent = Agent(llm=llm, trace=trace, permission_mode="open")

    assert agent.run("list") == "done"
    assert executed == ["ls -la"]
    events = _permission_events(trace)
    assert events[-1]["decision"] == "allowed"
    assert events[-1]["mode"] == "open"


def test_agent_rejects_invalid_permission_mode(trace: RunTrace) -> None:
    """构造 Agent 时校验权限模式。"""
    llm = FakeLLMClient([_text("ok")])

    with pytest.raises(ValueError, match="Invalid permission mode"):
        Agent(llm=llm, trace=trace, permission_mode="yolo")


def test_agent_captures_full_prompt_when_enabled(tmp_path: Path) -> None:
    """开启捕获时，Agent 把实际发送的完整 prompt 与工具 schema 落盘。"""
    llm = FakeLLMClient([_text("ok")])

    with RunTrace(trace_dir=tmp_path / "traces", run_id="cap", capture_messages=True) as tr:
        agent = Agent(llm=llm, trace=tr, system_prompt="SYS")
        assert agent.run("hi") == "ok"

    assert tr.messages_path is not None
    records = [
        json.loads(line) for line in tr.messages_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    record = records[0]
    assert record["messages"][0]["content"] == "SYS"
    assert record["messages"][-1]["content"] == "hi"
    assert record["tool_choice"] is None
    assert isinstance(record["temperature"], float)
    assert record["response"]["content"] == "ok"
    assert record["response"]["tool_calls"] == []
    names = {tool["function"]["name"] for tool in record["tools"]}
    assert "read_file" in names


# ─── 验证结果一致性 ───


def test_claims_verification_success_distinguishes_failure() -> None:
    from breeze.agent import _claims_verification_success

    assert _claims_verification_success("29 passed")
    assert _claims_verification_success("ruff 全绿，pytest 29 passed")
    assert not _claims_verification_success("6 failed, 23 passed")
    assert not _claims_verification_success("测试未通过")


def test_verification_status_parses_reports() -> None:
    from breeze.agent import _verification_status

    ok, summary = _verification_status("run_tests", "exit_code=1\nsummary: 6 failed, 23 passed in 2s\n")
    assert not ok
    assert "6 failed" in summary

    ok, summary = _verification_status("run_tests", "exit_code=0\nsummary: 29 passed in 2s\n")
    assert ok
    assert "29 passed" in summary

    ok, summary = _verification_status(
        "run_shell",
        "exit_code: 1\n--- stdout ---\n...\n--- stderr ---\nE501 line too long\n",
    )
    assert not ok
    assert "E501" in summary


def test_agent_flags_false_verification_claim(
    trace: RunTrace,
    fake_tool: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最后一次验证失败时，最终回答不能以“通过”收尾。"""
    import breeze.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_is_verification_call", lambda name, _args: name == "fake_echo")
    monkeypatch.setattr(agent_mod, "_verification_status", lambda _name, _result: (False, "summary: 1 failed"))

    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "x"}),
            _text("全部通过，29 passed"),
            _text("29 passed，全部通过"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=4)

    answer = agent.run("verify")

    assert answer.startswith("[验证未通过]")
    assert "summary: 1 failed" in answer
    joined = " ".join(str(m.get("content", "")) for m in llm.calls[1]["messages"])
    assert "验证未通过" in joined


def test_agent_honest_failure_report_not_flagged(
    trace: RunTrace,
    fake_tool: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """如实报告失败的最终回答保持原样。"""
    import breeze.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_is_verification_call", lambda name, _args: name == "fake_echo")
    monkeypatch.setattr(agent_mod, "_verification_status", lambda _name, _result: (False, "summary: 1 failed"))

    llm = FakeLLMClient(
        [
            _tool_call("fake_echo", {"text": "x"}),
            _text("验证失败：1 项失败，任务未完成"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=4)

    answer = agent.run("verify")

    assert answer == "验证失败：1 项失败，任务未完成"
    assert not answer.startswith("[验证未通过]")


def test_agent_passes_reasoning_content_back_with_tool_results(
    trace: RunTrace, fake_tool: None
) -> None:
    """推理模型的 thinking 随工具调用回传，避免多步后偏离目标。"""
    first = _tool_call("fake_echo", {"text": "x"})
    first.reasoning_content = "先回显 x，再给最终答案"
    llm = FakeLLMClient([first, _text("done")])
    agent = Agent(llm=llm, trace=trace, max_steps=4)

    assert agent.run("go") == "done"

    assistant = [
        msg
        for msg in llm.calls[1]["messages"]
        if msg.get("role") == "assistant" and msg.get("tool_calls")
    ]
    assert assistant
    assert assistant[0]["reasoning_content"] == "先回显 x，再给最终答案"


def test_rejected_verification_command_is_not_a_failed_check(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命令因管道/权限被拒绝时没有产生验证结论，不应触发“验证失败”纠偏。"""
    import breeze.agent as agent_mod
    from breeze.tools import registry

    monkeypatch.setattr(agent_mod, "_is_verification_call", lambda name, _args: name == "run_tests")
    monkeypatch.setattr(
        registry,
        "execute",
        lambda _name, _arguments: "Tool execution failed: Shell operators are not supported",
    )
    llm = FakeLLMClient([_tool_call("run_tests", {}), _text("done")])
    agent = Agent(llm=llm, trace=trace, max_steps=4)

    assert agent.run("verify") == "done"

    joined = " ".join(str(m.get("content", "")) for m in llm.calls[1]["messages"])
    assert "验证未通过" not in joined
    guards = [e for e in _events(trace.path) if e["event"] == "guard"]
    assert any("did not run" in e["message"] for e in guards)


# ─── 重复读取与跨回合状态 ───


def test_duplicate_read_is_suppressed_after_two_executions(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同一回合内第三次相同 read_file 不再执行，返回抑制提示。"""
    from breeze.tools import registry

    executed: list[str] = []

    def fake_execute(name, arguments):
        if name == "read_file":
            executed.append(str(arguments.get("path")))
            return "1 | def f():\n--- (file has 2 lines total) ---"
        return "ok"

    monkeypatch.setattr(registry, "execute", fake_execute)

    llm = FakeLLMClient(
        [
            _tool_call("read_file", {"path": "a.py"}, call_id="r1"),
            _tool_call("read_file", {"path": "a.py"}, call_id="r2"),
            _tool_call("read_file", {"path": "a.py"}, call_id="r3"),
            _text("done"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=6)

    assert agent.run("go") == "done"
    assert executed == ["a.py", "a.py"]
    tool_messages = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert "duplicate read suppressed" in tool_messages[-1]
    guards = [e for e in _events(trace.path) if e["event"] == "guard"]
    assert any("duplicate read suppressed" in e["message"] for e in guards)


def test_duplicate_read_allowed_when_previous_copy_compressed(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """旧副本已被 hard limit 压成指针时，允许再读一次，但仍只放行一次。"""
    from breeze.context import ContextConfig, ContextManager
    from breeze.tools import registry

    executed: list[str] = []

    def fake_execute(name, arguments):
        if name == "read_file":
            executed.append(str(arguments.get("path")))
            return "1 | " + "x" * 2000
        return "ok"

    monkeypatch.setattr(registry, "execute", fake_execute)

    llm = FakeLLMClient(
        [
            _tool_call("read_file", {"path": "a.py"}, call_id=f"r{i}")
            for i in range(1, 5)
        ]
        + [_text("done")]
    )
    context = ContextManager(ContextConfig(intra_turn_budget=1, hard_budget=1))
    agent = Agent(llm=llm, trace=trace, max_steps=8, context=context)

    assert agent.run("go") == "done"
    assert executed == ["a.py", "a.py", "a.py"]
    guards = [e["message"] for e in _events(trace.path) if e["event"] == "guard"]
    assert any("duplicate read allowed: earlier copies were compressed" in m for m in guards)
    assert any("duplicate read suppressed" in m for m in guards)


def test_read_after_edit_is_not_suppressed(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件被编辑后版本变化，允许再次读取。"""
    from breeze.tools import registry

    executed: list[str] = []

    def fake_execute(name, arguments):
        if name == "read_file":
            executed.append(str(arguments.get("path")))
            return "1 | old\n--- (file has 1 lines total) ---"
        if name == "apply_patch":
            return "Patched a.py (1 replacement)"
        return "ok"

    monkeypatch.setattr(registry, "execute", fake_execute)

    llm = FakeLLMClient(
        [
            _tool_call("read_file", {"path": "a.py"}, call_id="r1"),
            _tool_call("read_file", {"path": "a.py"}, call_id="r2"),
            _tool_call(
                "apply_patch",
                {"path": "a.py", "old": "old", "new": "new"},
                call_id="p1",
            ),
            _tool_call("read_file", {"path": "a.py"}, call_id="r3"),
            _text("done"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=6)

    assert agent.run("go") == "done"
    assert executed == ["a.py", "a.py", "a.py"]


def test_second_turn_first_request_includes_prior_state(
    trace: RunTrace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二个回合首次请求带上一回合的读取与符号索引。"""
    from breeze.tools import registry

    monkeypatch.setattr(
        registry,
        "execute",
        lambda _name, _arguments: "1 | def helper():\n--- (file has 2 lines total) ---",
    )
    llm = FakeLLMClient(
        [
            _tool_call("read_file", {"path": "a.py"}, call_id="c1"),
            _text("done"),
            _text("again"),
        ]
    )
    agent = Agent(llm=llm, trace=trace, max_steps=4)

    assert agent.run("first") == "done"
    assert agent.run("second") == "again"

    second_turn = llm.calls[2]["messages"]
    text = "\n".join(str(msg.get("content")) for msg in second_turn)
    assert "[Session state — carried from previous turns]" in text
    assert "a.py" in text


def test_first_turn_does_not_inject_prior_state(trace: RunTrace) -> None:
    """首个回合没有历史事实，不注入状态块。"""
    llm = FakeLLMClient([_text("ok")])
    agent = Agent(llm=llm, trace=trace, max_steps=2)

    assert agent.run("hi") == "ok"
    text = "\n".join(str(msg.get("content")) for msg in llm.calls[0]["messages"])
    assert "[Session state" not in text


def test_agent_injects_agents_md_as_separate_system_message(
    trace: RunTrace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AGENTS.md 单独注入，不再混进主 system prompt。"""
    from breeze.config import settings

    (tmp_path / "AGENTS.md").write_text("总是先跑 tests", encoding="utf-8")
    monkeypatch.setattr(settings, "repo_root", tmp_path)
    llm = FakeLLMClient([_text("ok")])

    agent = Agent(llm=llm, trace=trace)
    agent.run("hi")

    messages = llm.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "总是先跑 tests" not in messages[0]["content"]
    assert messages[1]["role"] == "system"
    assert "总是先跑 tests" in messages[1]["content"]
    assert messages[2] == {"role": "user", "content": "hi"}


def test_agent_custom_system_prompt_skips_agents_md(
    trace: RunTrace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """显式传入 system_prompt 时保持原有行为：不额外注入 AGENTS.md。"""
    from breeze.config import settings

    (tmp_path / "AGENTS.md").write_text("总是先跑 tests", encoding="utf-8")
    monkeypatch.setattr(settings, "repo_root", tmp_path)
    llm = FakeLLMClient([_text("ok")])

    agent = Agent(llm=llm, trace=trace, system_prompt="CUSTOM")
    agent.run("hi")

    assert agent.project_instructions == ""
    assert [m["role"] for m in llm.calls[0]["messages"]] == ["system", "user"]


def test_agent_emits_reasoning_for_display(trace: RunTrace, fake_tool: None) -> None:
    """每次 LLM 调用都通过事件回传思考内容，供展示层替换显示。"""
    first = _tool_call("fake_echo", {"text": "one"}, call_id="c1")
    first.reasoning_content = "先读文件"
    second = _text("done")
    second.reasoning_content = "再总结"
    events: list[Any] = []

    agent = Agent(llm=FakeLLMClient([first, second]), trace=trace, on_event=events.append)
    assert agent.run("go") == "done"

    reasonings = [e.reasoning for e in events if e.kind == "llm_end"]
    assert reasonings == ["先读文件", "再总结"]

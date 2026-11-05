"""breeze chat 运行时命令的单元测试。"""

from pathlib import Path
from types import SimpleNamespace

from breeze.chat_commands import ChatController
from breeze.state import SessionState, TurnLedger


class FakeLLM:
    def __init__(self, model: str = "base-model", temperature: float = 0.0) -> None:
        self.model = model
        self.temperature = temperature


def _factory(name: str, temperature: float) -> FakeLLM:
    if name == "bad-model":
        raise ValueError("API key 未设置")
    return FakeLLM(name, temperature)


class StubSession:
    session_id = "20260101T000000_abc"

    def __init__(self) -> None:
        self.changes: list[dict] = []

    def record_config(self, **changes: object) -> None:
        self.changes.append(changes)


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        llm=FakeLLM("agent-model"),
        max_steps=10,
        max_edit_failures=3,
        context=None,
        ledger=TurnLedger(),
        state=SessionState(),
    )


def _controller(**kwargs: object) -> ChatController:
    return ChatController(
        llm=FakeLLM("base-model"),
        llm_factory=_factory,
        repo_root="/repo",
        **kwargs,
    )


# ─── 分发 ───


def test_non_command_returns_none() -> None:
    """普通提问交给 Agent，不当作命令。"""
    assert _controller().handle("解释 registry", None) is None


def test_unknown_command() -> None:
    """未知命令提示 /help。"""
    response = _controller().handle("/nope", None)

    assert response is not None
    assert "未知命令" in response
    assert "/help" in response


# ─── /model ───


def test_model_show() -> None:
    assert _controller().handle("/model", None) == "当前模型：base-model"


def test_model_switch_updates_agent_and_session() -> None:
    """切换模型同时更新控制器、现有 Agent 与会话配置。"""
    session = StubSession()
    agent = _agent()
    controller = _controller(session=session)

    response = controller.handle("/model new-model", agent)

    assert response == "模型已切换：base-model → new-model"
    assert controller.llm.model == "new-model"
    assert agent.llm is controller.llm
    assert session.changes[-1] == {"model": "new-model"}


def test_model_switch_failure_keeps_current() -> None:
    """工厂报错时保留原模型并返回原因。"""
    agent = _agent()
    controller = _controller()

    response = controller.handle("/model bad-model", agent)

    assert "模型切换失败" in response
    assert "API key 未设置" in response
    assert controller.llm.model == "base-model"
    assert agent.llm.model == "agent-model"


# ─── /trace ───


def test_trace_show_and_set() -> None:
    controller = _controller(trace_prefix="first")

    assert "first" in controller.handle("/trace", None)
    assert "next-run" in controller.handle("/trace next-run.jsonl", None)
    assert controller.trace_prefix == "next-run"


def test_trace_name_is_normalized() -> None:
    """非法字符与扩展名被清理，避免生成奇怪的 trace 文件名。"""
    controller = _controller()

    controller.handle("/trace my run/2026", None)

    assert controller.trace_prefix == "my_run_2026"


def test_trace_pins_exact_file(tmp_path: Path) -> None:
    """ /trace <name> 固定为 dev.jsonl，并把状态写入会话。"""
    session = StubSession()
    controller = _controller(session=session, trace_dir=tmp_path)

    response = controller.handle("/trace dev.jsonl", None)

    assert controller.trace_pinned is True
    assert controller.trace_prefix == "dev"
    assert str(tmp_path / "dev.jsonl") in response
    assert session.changes[-1] == {"trace_prefix": "dev", "trace_pinned": True}


def test_trace_rejects_existing_trace_or_messages(tmp_path: Path) -> None:
    """目标 trace 或 messages 文件已存在时拒绝固定，保持原模式。"""
    for filename in ("dev.jsonl", "dev.messages.jsonl"):
        (tmp_path / filename).write_text("old\n", encoding="utf-8")
        controller = _controller(trace_prefix="first", trace_dir=tmp_path)

        response = controller.handle("/trace dev", None)

        assert "已存在" in response
        assert controller.trace_pinned is False
        assert controller.trace_prefix == "first"
        (tmp_path / filename).unlink()


def test_trace_auto_restores_per_turn_files(tmp_path: Path) -> None:
    """ /trace auto 取消固定，恢复每回合独立文件。"""
    session = StubSession()
    controller = _controller(session=session, trace_dir=tmp_path)
    controller.handle("/trace dev", None)

    response = controller.handle("/trace auto", None)

    assert controller.trace_pinned is False
    assert "每回合" in response
    assert session.changes[-1] == {"trace_pinned": False}


def test_trace_show_pinned_path(tmp_path: Path) -> None:
    """固定模式下列出实际目标文件。"""
    controller = _controller(trace_dir=tmp_path)
    controller.handle("/trace dev", None)

    response = controller.handle("/trace", None)

    assert "固定" in response
    assert str(tmp_path / "dev.jsonl") in response


def test_trace_repin_same_name_is_noop(tmp_path: Path) -> None:
    """已在固定同一文件时再次设置，不重复记录配置。"""
    session = StubSession()
    controller = _controller(session=session, trace_dir=tmp_path)
    controller.handle("/trace dev", None)

    response = controller.handle("/trace dev", None)

    assert "已在追加" in response
    assert len(session.changes) == 1


# ─── /profile ───


def test_profile_shows_current_budget() -> None:
    controller = _controller(profile="medium")

    response = controller.handle("/profile", None)

    assert "medium" in response
    assert "intra_turn_budget=24000" in response


def test_profile_switch_updates_budgets_and_agent() -> None:
    session = StubSession()
    agent = _agent()
    controller = _controller(profile="medium", session=session)

    response = controller.handle("/profile short", agent)

    assert "压缩档位已切换：short" in response
    assert controller.intra_turn_budget == 12000
    assert controller.hard_budget == 12000
    assert controller.answer_preview == 800
    assert controller.max_records == 4
    assert controller.max_chars == 3000
    assert agent.context.config.intra_turn_budget == 12000
    assert agent.context.config.hard_budget == 12000
    assert agent.ledger.max_records == 4
    assert session.changes[-1]["profile"] == "short"
    assert session.changes[-1]["intra_turn_budget"] == 12000
    assert session.changes[-1]["hard_budget"] == 12000


def test_custom_profile_freezes_current_budget() -> None:
    """custom 不套用 preset，冻结切换前的预算。"""
    controller = _controller(profile="medium")

    controller.handle("/profile long", None)
    controller.handle("/profile custom", None)

    assert controller.profile == "custom"
    assert controller.intra_turn_budget == 48000
    assert controller.max_records == 16


def test_invalid_profile() -> None:
    controller = _controller(profile="medium")

    response = controller.handle("/profile huge", None)

    assert "无效档位" in response
    assert controller.profile == "medium"


def test_explicit_budget_overrides_preset() -> None:
    """CLI/session 传入的显式预算优先于档位 preset。"""
    controller = _controller(profile="short", intra_turn_budget=20000)

    assert controller.intra_turn_budget == 20000
    assert controller.max_records == 4


# ─── /set ───


def test_set_max_steps() -> None:
    session = StubSession()
    agent = _agent()
    controller = _controller(session=session)

    assert "已设为：3" in controller.handle("/set max_steps 3", agent)
    assert controller.max_steps == 3
    assert agent.max_steps == 3
    assert session.changes[-1] == {"max_steps": 3}


def test_set_max_edit_failures() -> None:
    agent = _agent()
    controller = _controller()

    controller.handle("/set max_edit_failures 2", agent)

    assert controller.max_edit_failures == 2
    assert agent.max_edit_failures == 2


def test_set_permission() -> None:
    """切换权限模式并同步到 Agent。"""
    session = StubSession()
    agent = _agent()
    controller = _controller(session=session)

    response = controller.handle("/set permission open", agent)

    assert "open" in response
    assert controller.permission == "open"
    assert agent.permission_mode == "open"
    assert session.changes[-1] == {"permission": "open"}


def test_set_permission_rejects_invalid() -> None:
    """非法权限模式被拒，保持原值。"""
    controller = _controller()

    response = controller.handle("/set permission yolo", None)

    assert "strict" in response
    assert "confirm" in response
    assert controller.permission == "strict"


def test_set_temperature() -> None:
    controller = _controller()

    response = controller.handle("/set temperature 0.3", None)

    assert "0.3" in response
    assert controller.temperature == 0.3
    assert controller.llm.temperature == 0.3


def test_set_rejects_invalid_values() -> None:
    controller = _controller()

    assert "用法" in controller.handle("/set", None)
    assert "未知参数" in controller.handle("/set foo 1", None)
    assert "需要整数" in controller.handle("/set max_steps abc", None)
    assert "1 到 50" in controller.handle("/set max_steps 0", None)
    assert "0 到 2" in controller.handle("/set temperature 3", None)
    assert controller.max_steps == 15


# ─── /plan ───


def test_plan_before_agent_exists() -> None:
    """首个回合前查看计划给出提示而非报错。"""
    response = _controller().handle("/plan", None)

    assert "尚未创建" in response


def test_plan_empty() -> None:
    """没有计划时说明为空。"""
    response = _controller().handle("/plan", _agent())

    assert "当前计划：(空)" in response


def test_plan_shows_current_plan() -> None:
    """有计划时原样展示。"""
    agent = _agent()
    agent.state.plan = "1. 读代码\n2. 改代码"

    response = _controller().handle("/plan", agent)

    assert "当前计划：" in response
    assert "2. 改代码" in response


# ─── /config ───


def test_config_lists_runtime_values() -> None:
    session = StubSession()
    controller = _controller(session=session, trace_prefix="run-a", profile="short")

    response = controller.handle("/config", None)

    assert "运行配置：" in response
    assert "model: base-model" in response
    assert "session: 20260101T000000_abc" in response
    assert "trace 前缀: run-a" in response
    assert "profile: short (intra_turn_budget=12000)" in response
    assert "context: hard_budget=12000" in response

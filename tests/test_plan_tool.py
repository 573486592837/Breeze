"""update_plan 工具的单元测试。"""

from breeze.state import PLAN_MAX_CHARS, SessionState
from breeze.tools import registry


def test_tool_registered_with_content_argument() -> None:
    """工具应以 content 为唯一必填参数注册。"""
    schema = next(s["function"] for s in registry.schemas() if s["function"]["name"] == "update_plan")

    assert schema["parameters"]["required"] == ["content"]
    assert "计划" in schema["description"]


def test_update_plan_confirms_and_reports_length() -> None:
    """成功更新时返回字符数确认。"""
    content = "1. 读代码\n2. 改代码"

    result = registry.execute("update_plan", {"content": content})

    assert result.startswith("Plan updated")
    assert str(len(content)) in result


def test_blank_content_clears_plan() -> None:
    """空白内容表示清空计划。"""
    result = registry.execute("update_plan", {"content": "   "})

    assert result == "Plan cleared. It is removed from the injected context."


def test_over_limit_returns_error() -> None:
    """超长计划拒绝并提示上限，便于模型自行缩短。"""
    result = registry.execute("update_plan", {"content": "x" * (PLAN_MAX_CHARS + 1)})

    assert result.startswith("Error:")
    assert str(PLAN_MAX_CHARS) in result


def test_state_records_successful_plan() -> None:
    """成功的工具结果写入 SessionState.plan。"""
    state = SessionState()
    state.update("update_plan", {"content": "1. a\n2. b"}, "Plan updated (10 chars).", 0)

    assert state.plan == "1. a\n2. b"


def test_state_keeps_old_plan_on_error() -> None:
    """工具返回 Error 时不覆盖已有计划。"""
    state = SessionState()
    state.plan = "1. old"
    state.update("update_plan", {"content": "1. new"}, "Error: plan is 9000 chars; keep it under 2000.", 1)

    assert state.plan == "1. old"

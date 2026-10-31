"""registry 模块的单元测试。"""

from collections.abc import Iterator

import pytest

from breeze.tools import registry


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """快照并恢复 TOOLS，避免测试之间互相污染。"""
    snapshot = dict(registry.TOOLS)
    yield
    registry.TOOLS.clear()
    registry.TOOLS.update(snapshot)


def test_repo_map_not_registered() -> None:
    """repo_map 已移除：评估 trace 显示它只增加探索步数，未降低 Token。"""
    assert "repo_map" not in registry.TOOLS


def test_register_creates_schema() -> None:
    """注册后 schemas 应包含对应条目，结构与 OpenAI 兼容。"""
    registry.register(
        name="_test_echo",
        description="Echo the input string.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )(lambda text: text)

    schemas = registry.schemas()
    entry = next(s for s in schemas if s["function"]["name"] == "_test_echo")

    assert entry["type"] == "function"
    assert entry["function"]["description"] == "Echo the input string."
    assert entry["function"]["parameters"]["required"] == ["text"]


def test_execute_passes_kwargs() -> None:
    """execute 应把 arguments 以关键字参数传入工具函数。"""

    def _add(a: int, b: int) -> str:
        return str(a + b)

    registry.register(
        name="_test_add",
        description="Add two numbers.",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )(_add)

    assert registry.execute("_test_add", {"a": 3, "b": 5}) == "8"


def test_duplicate_registration_raises() -> None:
    """同名工具重复注册应抛 ValueError。"""

    registry.register(
        name="_test_duplicate",
        description="First registration.",
        parameters={"type": "object", "properties": {}, "required": []},
    )(lambda: "first")

    with pytest.raises(ValueError):
        registry.register(
            name="_test_duplicate",
            description="Second registration.",
            parameters={"type": "object", "properties": {}, "required": []},
        )(lambda: "second")


def test_execute_unknown_tool_raises() -> None:
    """执行未注册工具应抛 KeyError。"""
    with pytest.raises(KeyError):
        registry.execute("_test_not_exist", {})


def test_schemas_returns_new_list() -> None:
    """schemas 每次返回新的列表对象，避免外部修改影响内部状态。"""
    first = registry.schemas()
    second = registry.schemas()
    assert first is not second

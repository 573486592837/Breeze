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


def test_schemas_filters_by_names() -> None:
    """schemas 只返回指定工具，用于按环境裁剪工具集。"""
    for name in ("_test_one", "_test_two"):
        registry.register(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
        )(lambda: "")

    names = [schema["function"]["name"] for schema in registry.schemas({"_test_one"})]

    assert names == ["_test_one"]


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


def _register_echo() -> None:
    registry.register(
        name="_test_echo",
        description="Echo the input string.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )(lambda text: text)


def test_execute_reports_missing_required_argument() -> None:
    """漏传必填参数应给出可修正的说明，而不是裸 TypeError。"""
    _register_echo()

    with pytest.raises(registry.ToolArgumentError) as excinfo:
        registry.execute("_test_echo", {})

    message = str(excinfo.value)
    assert "missing required argument 'text'" in message
    assert "Expected: text (string, required)" in message


def test_execute_reports_unknown_argument() -> None:
    """拼错参数名应指出未知键，并列出可用参数。"""
    _register_echo()

    with pytest.raises(registry.ToolArgumentError) as excinfo:
        registry.execute("_test_echo", {"text": "x", "txt": "y"})

    assert "unknown argument 'txt'" in excinfo.value.args[0]


def test_execute_reports_argument_type_mismatch() -> None:
    """类型不符时说明期望类型与实际类型。"""
    registry.register(
        name="_test_double",
        description="Double a number.",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    )(lambda value: str(value * 2))

    with pytest.raises(registry.ToolArgumentError) as excinfo:
        registry.execute("_test_double", {"value": "3"})

    message = str(excinfo.value)
    assert "must be integer, got string" in message


def test_execute_treats_null_optional_as_omitted() -> None:
    """可选项显式传 null 时按未传处理，让函数默认值生效。"""
    registry.register(
        name="_test_optional",
        description="Echo with an optional limit.",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["text"],
        },
    )(lambda text, limit=5: f"{text}:{limit}")

    assert registry.execute("_test_optional", {"text": "x", "limit": None}) == "x:5"
    with pytest.raises(registry.ToolArgumentError, match="got null"):
        registry.execute("_test_optional", {"text": None})


def test_execute_rejects_bool_for_integer() -> None:
    """bool 是 int 的子类，但不能当作 integer 参数。"""
    registry.register(
        name="_test_double",
        description="Double a number.",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    )(lambda value: str(value * 2))

    with pytest.raises(registry.ToolArgumentError, match="got boolean"):
        registry.execute("_test_double", {"value": True})


def test_schemas_returns_new_list() -> None:
    """schemas 每次返回新的列表对象，避免外部修改影响内部状态。"""
    first = registry.schemas()
    second = registry.schemas()
    assert first is not second

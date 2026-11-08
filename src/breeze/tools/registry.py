"""工具注册与执行。

工具通过 register 装饰器注册，注册后可通过 execute 调用，
并通过 schemas 导出为 OpenAI 兼容的 JSON Schema 列表。
"""

from collections.abc import Callable, Collection
from typing import Any

TOOLS: dict[str, dict[str, Any]] = {}

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


class ToolArgumentError(ValueError):
    """调用参数不符合工具 schema。

    Agent 单独处理该异常：参数错误不是工具执行失败，不应消耗连续编辑
    失败次数；模型只需修正参数后重试。
    """


def register(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """注册一个工具

    Args:
        name: 工具名称，须全局唯一。
        description: 工具用途描述，供 LLM 判断何时调用。
        parameters: 参数的 JSON Schema。

    Returns:
        装饰器，原样返回被装饰函数。

    Raises:
        ValueError: 工具名称已注册。
    """

    def decorator(fn: Callable[..., str]) -> Callable[..., str]:
        if name in TOOLS:
            raise ValueError(f"Tool name '{name}' is already registered.")
        TOOLS[name] = {
            "fn": fn,
            "schema": {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                },
            },
        }
        return fn

    return decorator


def _describe_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def validate_arguments(name: str, arguments: dict[str, Any]) -> str | None:
    """按注册的 JSON Schema 检查参数。

    模型漏传必填项或拼错参数名时，直接调用函数只会得到一句 ``TypeError``，
    既看不出正确参数，也会被 Agent 当成一次真实执行失败。这里提前给出
    可修正的说明，并把参数错误与执行错误区分开。

    Args:
        name: 已注册的工具名。
        arguments: 模型给出的关键字参数。

    Returns:
        参数合法时返回 None，否则返回面向模型的错误说明。
    """
    parameters = TOOLS[name]["schema"]["function"]["parameters"]
    properties: dict[str, Any] = parameters.get("properties", {})
    required: list[str] = parameters.get("required", [])

    issues: list[str] = []
    for key in required:
        if key not in arguments:
            issues.append(f"missing required argument '{key}'")
    for key, value in arguments.items():
        spec = properties.get(key)
        if spec is None:
            issues.append(f"unknown argument '{key}'")
            continue
        if value is None and key not in required:
            # 显式 null 视同未传，交给函数默认值处理。
            continue
        expected = spec.get("type")
        allowed = _JSON_TYPES.get(expected) if isinstance(expected, str) else None
        if allowed is None:
            continue
        # bool 是 int 的子类，不能让 true/false 混过 integer/number 检查。
        if isinstance(value, bool) and expected in ("integer", "number"):
            issues.append(f"argument '{key}' must be {expected}, got boolean")
            continue
        if not isinstance(value, allowed):
            issues.append(
                f"argument '{key}' must be {expected}, got {_describe_type(value)}"
            )

    if not issues:
        return None

    expected_desc = ", ".join(
        f"{key} ({spec.get('type', 'any')}{', required' if key in required else ''})"
        for key, spec in properties.items()
    )
    detail = "; ".join(issues)
    return f"invalid arguments for {name}: {detail}. Expected: {expected_desc}."


def execute(name: str, arguments: dict[str, Any]) -> str:
    """执行已注册的工具。

    Args:
        name: 工具名称。
        arguments: 关键字参数。

    Returns:
        工具的字符串返回值。

    Raises:
        KeyError: 工具未注册。
        ToolArgumentError: 参数不符合注册的 schema。
    """
    if name not in TOOLS:
        raise KeyError(f"Tool name '{name}' is not registered.")
    problem = validate_arguments(name, arguments)
    if problem is not None:
        raise ToolArgumentError(problem)
    required = set(TOOLS[name]["schema"]["function"]["parameters"].get("required", []))
    clean = {key: value for key, value in arguments.items() if value is not None or key in required}
    return TOOLS[name]["fn"](**clean)


def schemas(only: Collection[str] | None = None) -> list[dict[str, Any]]:
    """返回已注册工具的 OpenAI 兼容 schema 列表。

    Args:
        only: 只返回这些工具；None 表示全部。用于按环境裁剪工具集。

    Returns:
        schema 列表，顺序与注册顺序一致。
    """
    if only is None:
        return [entry["schema"] for entry in TOOLS.values()]
    wanted = set(only)
    return [entry["schema"] for name, entry in TOOLS.items() if name in wanted]

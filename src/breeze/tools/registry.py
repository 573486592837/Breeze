"""工具注册与执行。

工具通过 register 装饰器注册，注册后可通过 execute 调用，
并通过 schemas 导出为 OpenAI 兼容的 JSON Schema 列表。
"""

from collections.abc import Callable, Collection
from typing import Any

TOOLS: dict[str, dict[str, Any]] = {}


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


def execute(name: str, arguments: dict[str, Any]) -> str:
    """执行已注册的工具。

    Args:
        name: 工具名称。
        arguments: 关键字参数。

    Returns:
        工具的字符串返回值。

    Raises:
        KeyError: 工具未注册。
    """
    if name not in TOOLS:
        raise KeyError(f"Tool name '{name}' is not registered.")
    return TOOLS[name]["fn"](**arguments)


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

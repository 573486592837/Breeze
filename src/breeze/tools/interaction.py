"""交互工具。

ask_user 让 Agent 把项目决策交给用户判断，而不是自行猜测。真正读取用户
输入的回调由 CLI 注入（Agent.ask_user）；这里只提供 schema，Agent 会在
执行前拦截。非交互环境不会下发该工具。
"""

from typing import Any

from breeze.tools.registry import register


@register(
    name="ask_user",
    description=(
        "让用户对项目决策做选择。只在多个合理方案会影响实现方向、范围、兼容性"
        "或验收标准时使用；能从仓库或工具确认的事实、以及可自行按最佳实践决定"
        "的实现细节不要问。可以一次提出 1-5 个相互独立的问题，让用户在同一轮"
        "一并回答；每个问题可带最多 4 个候选方案。不要用它请求执行许可或确认计划。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "要问用户的决策问题，简洁明确。",
                        },
                        "options": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {"type": "string"},
                            "description": "候选方案（最多 4 个）。用户可选编号或自由回答。",
                        },
                    },
                    "required": ["question"],
                },
                "description": "一次提出的问题列表（1-5 个），用户会在同一轮全部看到。",
            },
        },
        "required": ["questions"],
    },
)
def ask_user(questions: list[dict[str, Any]]) -> str:
    """占位实现；交互式运行由 Agent 拦截并调用注入的回调。"""
    _ = questions
    return "Error: ask_user is only available in an interactive session."

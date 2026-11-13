"""交互工具。

ask_user 让 Agent 在需求存在歧义时向用户确认，而不是自行猜测。
真正读取用户输入的回调由 CLI 注入（Agent.ask_user）；这里只提供 schema，
Agent 会在执行前拦截。非交互环境不会下发该工具。
"""

from breeze.tools.registry import register


@register(
    name="ask_user",
    description=(
        "向用户提问并等待回答。只在需求存在会改变实现、范围或验收标准的歧义时使用；"
        "能从仓库或工具确认的事实不要问。一次只问一个最关键的问题，"
        "可以把候选方案放进 options。不要用它请求执行许可或确认计划。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要问用户的问题，简洁明确。",
            },
            "options": {
                "type": "array",
                "maxItems": 4,
                "items": {"type": "string"},
                "description": "可选候选答案（最多 4 个）。用户可直接选编号或自由回答。",
            },
        },
        "required": ["question"],
    },
)
def ask_user(question: str, options: list[str] | None = None) -> str:
    """占位实现；交互式运行由 Agent 拦截并调用注入的回调。"""
    _ = question, options
    return "Error: ask_user is only available in an interactive session."

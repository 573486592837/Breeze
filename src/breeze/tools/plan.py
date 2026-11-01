"""短期计划工具。

update_plan 把 Agent 自己维护的多步计划写入 SessionState，
ContextManager 在每次调用 LLM 时重新渲染到系统提示之后，
因此计划不会随轮数累计或回合内压缩被挤出上下文。
"""

from breeze.state import PLAN_MAX_CHARS
from breeze.tools.registry import register


@register(
    name="update_plan",
    description=(
        "创建或覆盖当前会话的短期开发计划。用 3-7 个步骤描述接下来要做什么，"
        "每完成一步或方向发生变化后，用完整的新计划覆盖更新。只写计划本身，"
        "不要粘贴工具输出。传入空字符串可清空计划。"
        "计划会自动注入上下文，不必在回答中重复。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": f"完整的新计划，纯文本，最多 {PLAN_MAX_CHARS} 字符。",
            }
        },
        "required": ["content"],
    },
)
def update_plan(content: str) -> str:
    """更新会话计划。

    Args:
        content: 完整的新计划文本；空字符串表示清空。

    Returns:
        供模型确认的结果文本；超长时返回 Error 前缀，SessionState 保持原计划。
    """
    text = content.strip()
    if not text:
        return "Plan cleared. It is removed from the injected context."
    if len(text) > PLAN_MAX_CHARS:
        return f"Error: plan is {len(text)} chars; keep it under {PLAN_MAX_CHARS}."
    return f"Plan updated ({len(text)} chars). It stays pinned in the injected context."

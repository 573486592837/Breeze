"""上下文管理。

ContextManager 负责把完整的消息历史压缩成适合发送给 LLM 的版本。
原始消息列表保持不变，仅供 trace 和调试使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_RECENT_PAIRS = 3
DEFAULT_ARGUMENTS_PREVIEW = 60


@dataclass
class ContextConfig:
    """ContextManager 的配置。

    Attributes:
        max_recent_pairs: 完整保留的最近轮数。
        arguments_preview: 摘要中工具参数的最大字符数。
        enable: 是否启用压缩。False 时 build 直接返回原列表。
    """

    max_recent_pairs: int = DEFAULT_MAX_RECENT_PAIRS
    arguments_preview: int = DEFAULT_ARGUMENTS_PREVIEW
    enable: bool = True


class ContextManager:
    """消息历史压缩器。

    把历史消息切分成 system、初始 user、若干轮 assistant+tool。
    当轮数超过 max_recent_pairs 时，把旧轮压缩成一条 user 摘要消息。
    """

    def __init__(self, config: ContextConfig | None = None) -> None:
        """初始化。

        Args:
            config: 上下文配置，None 时使用默认值。
        """
        self.config = config or ContextConfig()

    def build(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """构建发给 LLM 的消息列表。

        Args:
            messages: 完整历史。

        Returns:
            压缩后的消息列表。轮数不超过阈值时返回原列表的浅拷贝。
        """
        if not self.config.enable or len(messages) <= 2:
            return list(messages)

        head = messages[:2]
        body = messages[2:]
        rounds = self._split_rounds(body)

        if len(rounds) <= self.config.max_recent_pairs:
            return list(messages)

        old_rounds = rounds[: -self.config.max_recent_pairs]
        recent_rounds = rounds[-self.config.max_recent_pairs :]

        summary = self._render_summary(old_rounds)

        result: list[dict[str, Any]] = list(head)
        result.append(summary)
        for rnd in recent_rounds:
            result.extend(rnd)
        return result

    def _split_rounds(self, body: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """把 assistant+tool 序列切分成轮。

        每轮以 assistant 消息开始，包含其后所有 tool 消息，
        直到下一个 assistant 或列表结束。

        Args:
            body: 去掉头部 system 和 user 后的消息列表。

        Returns:
            轮的列表。
        """
        rounds: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []

        for msg in body:
            if msg.get("role") == "assistant":
                if current:
                    rounds.append(current)
                current = [msg]
            else:
                current.append(msg)

        if current:
            rounds.append(current)
        return rounds

    def _render_summary(self, old_rounds: list[list[dict[str, Any]]]) -> dict[str, Any]:
        """把旧轮渲染成一条 user 摘要消息。

        Args:
            old_rounds: 被压缩的轮列表。

        Returns:
            一条 role=user 的消息字典。
        """
        lines: list[str] = [f"[Compressed history: {len(old_rounds)} earlier rounds]"]

        for idx, rnd in enumerate(old_rounds):
            for msg in rnd:
                if msg.get("role") != "assistant":
                    continue
                for tc in msg.get("tool_calls", []) or []:
                    name = self._extract_name(tc)
                    args = self._extract_arguments(tc)
                    preview = self._truncate(args, self.config.arguments_preview)
                    lines.append(f"- step {idx}: {name}({preview})")

        lines.append("")
        lines.append("Note: earlier tool results are omitted. Re-invoke tools if you need the full content.")

        return {"role": "user", "content": "\n".join(lines)}

    @staticmethod
    def _extract_name(tool_call: Any) -> str:
        """兼容 dict 和对象形式的 tool_call。"""
        if isinstance(tool_call, dict):
            func = tool_call.get("function", {})
            return func.get("name", "unknown")
        func = getattr(tool_call, "function", None)
        if func is None:
            return "unknown"
        return getattr(func, "name", "unknown")

    @staticmethod
    def _extract_arguments(tool_call: Any) -> str:
        """提取参数 JSON 字符串。"""
        if isinstance(tool_call, dict):
            func = tool_call.get("function", {})
            return func.get("arguments", "{}")
        func = getattr(tool_call, "function", None)
        if func is None:
            return "{}"
        return getattr(func, "arguments", "{}")

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """截断字符串，超长时保留前 limit 字符并加省略号。"""
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

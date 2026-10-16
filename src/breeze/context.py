"""上下文管理。

ContextManager 负责把完整的消息历史压缩成适合发送给 LLM 的版本。
原始消息列表保持不变，仅供 trace 和调试使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_RECENT_PAIRS = 3
DEFAULT_MIN_RECENT_PAIRS = 1
DEFAULT_MAX_RECENT_TOKENS = 3_000
DEFAULT_ARGUMENTS_PREVIEW = 60

DEFAULT_TOOL_RESULT_MAX_BYTES = 4_000
DEFAULT_TOOL_RESULT_HEAD_BYTES = 2_000
DEFAULT_TOOL_RESULT_TAIL_BYTES = 2_000


@dataclass
class ContextConfig:
    """ContextManager 的配置。

    Attributes:
        enable: 是否启用压缩。False 时 build 直接返回原列表。
        max_recent_tokens: 保留的最近轮估计 Token 上限。
        min_recent_pairs: 无论多大，至少保留的最近轮数。
        max_recent_pairs: 保留的最近轮数硬上限（与 Token 预算同时生效）。
        arguments_preview: 摘要中工具参数的最大字符数。
        tool_result_max_bytes: 单条工具结果的字节上限。
        tool_result_head_bytes: 压缩时保留的头部字节数。
        tool_result_tail_bytes: 压缩时保留的尾部字节数。
    """

    enable: bool = True
    max_recent_tokens: int = DEFAULT_MAX_RECENT_TOKENS
    min_recent_pairs: int = DEFAULT_MIN_RECENT_PAIRS
    max_recent_pairs: int = DEFAULT_MAX_RECENT_PAIRS
    arguments_preview: int = DEFAULT_ARGUMENTS_PREVIEW
    tool_result_max_bytes: int = DEFAULT_TOOL_RESULT_MAX_BYTES
    tool_result_head_bytes: int = DEFAULT_TOOL_RESULT_HEAD_BYTES
    tool_result_tail_bytes: int = DEFAULT_TOOL_RESULT_TAIL_BYTES


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

        kept_rounds = self._select_recent_rounds(head, rounds)

        if len(kept_rounds) == len(rounds):
            result: list[dict[str, Any]] = list(head)
            for rnd in rounds:
                for msg in rnd:
                    result.append(self._compact_tool_result(msg))
            return result

        old_rounds = rounds[: len(rounds) - len(kept_rounds)]
        summary = self._render_summary(old_rounds)

        result: list[dict[str, Any]] = list(head)
        result.append(summary)
        for rnd in kept_rounds:
            for msg in rnd:
                result.append(self._compact_tool_result(msg))
        return result

    def _select_recent_rounds(
        self,
        head: list[dict[str, Any]],
        rounds: list[list[dict[str, Any]]],
    ) -> list[list[dict[str, Any]]]:
        """从尾部开始保留轮，直到触及 Token 或轮数上限。

        Args:
            head: system + 初始 user 消息。
            rounds: 全部轮。

        Returns:
            保留的轮列表，从旧到新。
        """
        kept: list[list[dict[str, Any]]] = []
        current_tokens = self._estimate_tokens(head)

        for rnd in reversed(rounds):
            rnd_tokens = self._estimate_tokens(rnd)
            at_min_pairs = len(kept) < self.config.min_recent_pairs

            if not at_min_pairs and current_tokens + rnd_tokens > self.config.max_recent_tokens:
                break

            if len(kept) >= self.config.max_recent_pairs:
                break

            kept.insert(0, rnd)
            current_tokens += rnd_tokens

        return kept

    @staticmethod
    def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
        """粗估消息列表的 Token 数。

        采用 4 字符约 1 Token 的经验比例。不做精确定量，
        仅用于上下文预算判断。

        Args:
            messages: 消息列表。

        Returns:
            估计的 Token 数。
        """
        total = 0
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content) // 4
            for tc in msg.get("tool_calls") or []:
                args = ContextManager._extract_arguments(tc)
                total += len(args) // 4 + 10  # 加常数近似工具调用开销
        return total

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

    def _compact_tool_result(self, msg: dict[str, Any]) -> dict[str, Any]:
        """压缩单条 tool 消息的内容。

        只处理 role=tool 且 content 为字符串且超过字节上限的情况。
        其他消息原样返回。

        Args:
            msg: 一条消息。

        Returns:
            原始消息或压缩后的新消息。原始消息不会被修改。
        """
        if msg.get("role") != "tool":
            return msg

        content = msg.get("content")
        if not isinstance(content, str):
            return msg

        encoded = content.encode("utf-8")
        if len(encoded) <= self.config.tool_result_max_bytes:
            return msg

        head_bytes = _safe_head(encoded, self.config.tool_result_head_bytes)
        tail_bytes = _safe_tail(encoded, self.config.tool_result_tail_bytes)
        omitted = len(encoded) - len(head_bytes) - len(tail_bytes)

        marker = f"\n\n... [{omitted} bytes omitted] ...\n\n"
        compacted = head_bytes.decode("utf-8") + marker + tail_bytes.decode("utf-8")

        new_msg = dict(msg)
        new_msg["content"] = compacted
        return new_msg

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


def _safe_head(encoded: bytes, n: int) -> bytes:
    """返回最多 n 字节，按 UTF-8 字符边界对齐。

    Args:
        encoded: UTF-8 编码的字节串。
        n: 期望保留的头部字节数。

    Returns:
        边界对齐后的头部字节串。
    """
    if len(encoded) <= n:
        return encoded
    end = n
    while end > 0 and (encoded[end] & 0xC0) == 0x80:
        end -= 1
    return encoded[:end]


def _safe_tail(encoded: bytes, n: int) -> bytes:
    """返回最多 n 字节，按 UTF-8 字符边界对齐。

    Args:
        encoded: UTF-8 编码的字节串。
        n: 期望保留的尾部字节数。

    Returns:
        边界对齐后的尾部字节串。
    """
    if len(encoded) <= n:
        return encoded
    start = len(encoded) - n
    while start < len(encoded) and (encoded[start] & 0xC0) == 0x80:
        start += 1
    return encoded[start:]

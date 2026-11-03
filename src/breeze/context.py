"""上下文管理（策略 v2：回合内分级保留）。

压缩单位从"轮"改为"用户回合"：在一轮提问到最终答案之间，
工具结果按类型分级处理——

- 定位类工具（grep、list_files）的信息已被 SessionState
  提取为符号/范围索引，其结果本体恒降级为一行指针。
- 内容类工具（read_file 等）默认原样保留，避免模型在编辑前丢失原文。
- 仅当回合实际 Token 超过 intra_turn_budget 时，按从旧到新依次把
  read_file 降级为"路径+行范围"指针，再不够降级其他可再生工具。

任何降级发生时，把 SessionState 状态块注入到消息末尾（body 之后），让模型基于
索引（而不是被丢弃的原文）定点重读并立即编辑；放在末尾也避免状态每次变化时
切断前面历史的缓存前缀。
原始消息列表保持不变，仅供 trace 和调试使用。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tiktoken

from breeze.config import settings

os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path.home() / ".breeze" / "tiktoken_cache"))

# Deprecated: values come from settings via ContextConfig.__post_init__.
DEFAULT_INTRA_TURN_BUDGET = 24_000
DEFAULT_TOOL_RESULT_MAX_BYTES = 16_000
DEFAULT_TOOL_RESULT_HEAD_BYTES = 8_000
DEFAULT_TOOL_RESULT_TAIL_BYTES = 8_000

POINTER_TOOLS: frozenset[str] = frozenset({"grep", "list_files"})
READ_TOOLS: frozenset[str] = frozenset({"read_file"})
SPILL_TOOLS: frozenset[str] = frozenset({"run_shell", "run_tests", "git_diff", "apply_patch", "create_file"})

_encoding: tiktoken.Encoding | None = None
_encoding_failed = False


def _get_encoding() -> tiktoken.Encoding | None:
    """获取 cl100k_base 编码器；加载失败时返回 None 并记住失败。"""
    global _encoding, _encoding_failed
    if _encoding is None and not _encoding_failed:
        try:
            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoding_failed = True
    return _encoding


def _count_text_tokens(text: str) -> int:
    """计算一段文本的实际 Token 数。

    Args:
        text: 任意文本。

    Returns:
        tiktoken 计数；编码器不可用时回退为 4 字符约 1 Token 的粗估。
    """
    enc = _get_encoding()
    if enc is None:
        return len(text) // 4
    return len(enc.encode(text, disallowed_special=()))


def _fingerprint(message: dict[str, Any]) -> str:
    """计算一条消息的稳定指纹，用于判断相邻调用的前缀是否相同。"""
    payload = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=8).hexdigest()


def _parse_arguments(raw: str) -> dict[str, Any]:
    """尽力解析 tool_call 的 arguments JSON，失败返回空 dict。"""
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _render_plan(plan: str | None) -> str:
    """把会话计划包装成注入块；空计划返回空字符串。

    Args:
        plan: SessionState.plan 的当前内容。

    Returns:
        带说明头的计划块，或空字符串。
    """
    text = (plan or "").strip()
    if not text:
        return ""
    return "[Current plan — agent-maintained; update with update_plan]\n" + text


def _render_pointer(tool: str, arguments: dict[str, Any], result: str) -> str:
    """把一条工具结果降级为一行指针。

    Args:
        tool: 工具名。
        arguments: 该调用的参数（解析失败时为空 dict）。
        result: 原始结果文本，用于统计行数或摘录首行。

    Returns:
        指针文本。
    """
    n = len([ln for ln in result.splitlines() if ln.strip()])

    if tool == "read_file":
        path = arguments.get("path", "?")
        start, end = arguments.get("start_line"), arguments.get("end_line")
        rng = f"lines {start}-{end}" if start and end else "full file"
        return (
            f"[read_file compressed] {path} ({rng}) dropped by budget; re-read this exact range and edit immediately."
        )

    if tool in POINTER_TOOLS:
        arg_bits = " ".join(f"{k}={v}" for k, v in list(arguments.items())[:3] if isinstance(v, (str, int)))
        return (
            f"[{tool} compressed] {arg_bits} → {n} lines; locations are in the session state; re-run if details needed."
        )

    detail = result.splitlines()[0][:80] if result.strip() else ""
    return f"[{tool} compressed] {detail} (output dropped by budget; re-run if needed)"


@dataclass
class ContextConfig:
    """ContextManager 的配置。

    All fields default to None. None means "use the value from settings".
    Explicit values (including 0 and False) take precedence over config.
    After __post_init__, every field holds a concrete non-None value.

    Attributes:
        enable: 是否启用上下文管理（False 时原样透传）。
        intra_turn_budget: 单回合发送给 LLM 的实际 Token 上限。
        tool_result_max_bytes: 单条工具结果的字节上限（兜底）。
        tool_result_head_bytes: 兜底压缩保留的头部字节数。
        tool_result_tail_bytes: 兜底压缩保留的尾部字节数。
    """

    enable: bool | None = None
    intra_turn_budget: int | None = None
    tool_result_max_bytes: int | None = None
    tool_result_head_bytes: int | None = None
    tool_result_tail_bytes: int | None = None

    def __post_init__(self) -> None:
        """Fill unset fields from settings."""
        if self.enable is None:
            self.enable = settings.context_enable
        if self.intra_turn_budget is None:
            self.intra_turn_budget = settings.context_intra_turn_budget
        if self.tool_result_max_bytes is None:
            self.tool_result_max_bytes = settings.context_tool_result_max_bytes
        if self.tool_result_head_bytes is None:
            self.tool_result_head_bytes = settings.context_tool_result_head_bytes
        if self.tool_result_tail_bytes is None:
            self.tool_result_tail_bytes = settings.context_tool_result_tail_bytes


@dataclass(frozen=True)
class PromptStats:
    """一次 build 的提示词组成与缓存可复用前缀。

    ``total_tokens`` 只统计消息本身，不含工具 schema；provider 返回的
    prompt_tokens 通常更大，两者的差额可用于观察 schema/格式开销。

    Attributes:
        cacheable_prefix_tokens: 与上一次 build 完全相同的消息前缀
            Token 数。provider 的缓存按前缀命中，因此这是本次调用
            “理论上最多可命中”的消息 Token 上界。
    """

    system_tokens: int = 0
    plan_tokens: int = 0
    archive_tokens: int = 0
    user_tokens: int = 0
    state_tokens: int = 0
    body_tokens: int = 0
    total_tokens: int = 0
    cacheable_prefix_tokens: int = 0

    def breakdown(self) -> dict[str, int]:
        """返回按注入位置分类的 Token 组成。"""
        return {
            "system": self.system_tokens,
            "plan": self.plan_tokens,
            "archive": self.archive_tokens,
            "user": self.user_tokens,
            "state": self.state_tokens,
            "body": self.body_tokens,
        }

    @property
    def prefix_reuse_ratio(self) -> float:
        """可复用前缀占消息 Token 的比例。"""
        return self.cacheable_prefix_tokens / self.total_tokens if self.total_tokens else 0.0


class ContextManager:
    """回合内上下文分级管理器。

    取代旧的"轮滑窗+摘要"策略：不再按轮数/估计 Token 挤出历史，
    而是在实际 Token 超预算时按工具类型分级降级，保留编辑所需的原文。
    """

    def __init__(self, config: ContextConfig | None = None) -> None:
        """初始化。

        Args:
            config: 上下文配置，None 时使用默认值。
        """
        self.config = config or ContextConfig()
        self._last_prefix: tuple[tuple[str, int], ...] | None = None

    def reset(self) -> None:
        """清空前缀复用基线（新会话时调用）。"""
        self._last_prefix = None

    def build(
        self,
        messages: list[dict[str, Any]],
        state: Any | None = None,
        archive: str | None = None,
        plan: str | None = None,
    ) -> list[dict[str, Any]]:
        """构建发给 LLM 的消息列表。

        Args:
            messages: 当前回合的完整历史（system、user、若干轮）。
            state: SessionState 实例；发生降级或存在回合归档时注入。
            archive: TurnLedger 渲染的已完成回合文本；注入在 system 之后。
            plan: Agent 维护的短期计划；非空时追加在 body 之后、state 之前，
                避免计划更新切断历史缓存前缀。

        Returns:
            分级处理后的消息列表。原始消息不被修改。
        """
        result, _ = self.build_with_stats(messages, state=state, archive=archive, plan=plan)
        return result

    def build_with_stats(
        self,
        messages: list[dict[str, Any]],
        state: Any | None = None,
        archive: str | None = None,
        plan: str | None = None,
    ) -> tuple[list[dict[str, Any]], PromptStats]:
        """构建消息列表，并返回本次提示词的组成与缓存复用量。

        参数与 ``build`` 相同。
        """
        if not self.config.enable or len(messages) < 2:
            result = list(messages)
            segments: dict[str, list[int]] = {
                "system": [0] if result else [],
                "user": [1] if len(result) > 1 else [],
                "body": list(range(2, len(result))),
            }
            return result, self._stats(result, segments)

        head = list(messages[:2])
        body = messages[2:]

        id_to_name, id_to_args = self._index_tool_calls(body)

        converted: list[dict[str, Any]] = []
        demoted = False
        for msg in body:
            if msg.get("role") != "tool":
                converted.append(msg)
                continue
            tool = id_to_name.get(msg.get("tool_call_id", ""), "")
            content = msg.get("content")
            if tool in POINTER_TOOLS and isinstance(content, str):
                new_msg = dict(msg)
                new_msg["content"] = _render_pointer(tool, id_to_args.get(msg.get("tool_call_id", ""), {}), content)
                demoted = True
                converted.append(self._compact_tool_result(new_msg))
            else:
                converted.append(self._compact_tool_result(msg))

        budget = self.config.intra_turn_budget
        if self._measure(head, converted, state, archive, plan) > budget:
            converted, spilled = self._spill(
                head, converted, id_to_name, id_to_args, budget, state, archive, plan, READ_TOOLS
            )
            demoted = demoted or spilled
        if self._measure(head, converted, state, archive, plan) > budget:
            converted, spilled = self._spill(
                head, converted, id_to_name, id_to_args, budget, state, archive, plan, SPILL_TOOLS
            )
            demoted = demoted or spilled

        result: list[dict[str, Any]] = []
        segments: dict[str, list[int]] = {}

        def add(msg: dict[str, Any], kind: str) -> None:
            segments.setdefault(kind, []).append(len(result))
            result.append(msg)

        add(head[0], "system")
        if archive:
            add({"role": "user", "content": archive}, "archive")
        add(head[1], "user")
        for msg in converted:
            add(msg, "body")
        plan_block = _render_plan(plan)
        if plan_block:
            add({"role": "user", "content": plan_block}, "plan")
        if state is not None and (demoted or archive):
            block = state.render()
            if block:
                add({"role": "user", "content": block}, "state")
        return result, self._stats(result, segments)

    def _spill(
        self,
        head: list[dict[str, Any]],
        converted: list[dict[str, Any]],
        id_to_name: dict[str, str],
        id_to_args: dict[str, dict[str, Any]],
        budget: int,
        state: Any | None,
        archive: str | None,
        plan: str | None,
        classes: frozenset[str],
    ) -> tuple[list[dict[str, Any]], bool]:
        """预算溢出时，从最旧开始把指定类别的工具结果降级为指针。

        Args:
            head: system+user 头部。
            converted: 当前消息列表。
            id_to_name: tool_call_id -> 工具名。
            id_to_args: tool_call_id -> 参数 dict。
            budget: Token 上限。
            state: 用于计入状态块开销，可为 None。
            archive: 回合归档文本，计入 head 开销，可为 None。
            plan: 计划块文本，计入 head 开销，可为 None。
            classes: 本次允许降级的工具类别。

        Returns:
            (新列表, 是否发生降级)。
        """
        out = list(converted)
        changed = False
        for i, msg in enumerate(out):
            if self._measure(head, out, state, archive, plan) <= budget:
                break
            if msg.get("role") != "tool":
                continue
            tc_id = msg.get("tool_call_id", "")
            tool = id_to_name.get(tc_id, "")
            if tool not in classes:
                continue
            content = msg.get("content")
            if not isinstance(content, str) or content.startswith(f"[{tool} compressed]"):
                continue
            new_msg = dict(msg)
            new_msg["content"] = _render_pointer(tool, id_to_args.get(tc_id, {}), content)
            out[i] = new_msg
            changed = True
        return out, changed

    @staticmethod
    def _index_tool_calls(
        body: list[dict[str, Any]],
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
        """建立 tool_call_id 到工具名与参数的索引。"""
        id_to_name: dict[str, str] = {}
        id_to_args: dict[str, dict[str, Any]] = {}
        for msg in body:
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id", "")
                func = tc.get("function", {})
                id_to_name[tc_id] = func.get("name", "")
                id_to_args[tc_id] = _parse_arguments(func.get("arguments", "{}"))
        return id_to_name, id_to_args

    def _measure(
        self,
        head: list[dict[str, Any]],
        body: list[dict[str, Any]],
        state: Any | None,
        archive: str | None = None,
        plan: str | None = None,
    ) -> int:
        """估算 head+plan+archive(+状态块)+body 的实际 Token 总量。"""
        total = self._count_messages(head) + self._count_messages(body)
        plan_block = _render_plan(plan)
        if plan_block:
            total += _count_text_tokens(plan_block) + 10
        if archive:
            total += _count_text_tokens(archive) + 10
        if state is not None:
            block = state.render()
            if block:
                total += _count_text_tokens(block) + 10
        return total

    def _stats(self, messages: list[dict[str, Any]], segments: dict[str, list[int]]) -> PromptStats:
        """统计本次消息组成，并与上一次 build 比较可复用前缀。"""
        counts = [self._count_message(msg) for msg in messages]

        def segment_tokens(kind: str) -> int:
            return sum(counts[i] for i in segments.get(kind, ()))

        signature = tuple((_fingerprint(msg), count) for msg, count in zip(messages, counts, strict=True))
        reusable = 0
        if self._last_prefix is not None:
            for (prev_hash, prev_count), (cur_hash, _) in zip(self._last_prefix, signature, strict=False):
                if prev_hash != cur_hash:
                    break
                reusable += prev_count
        self._last_prefix = signature

        return PromptStats(
            system_tokens=segment_tokens("system"),
            plan_tokens=segment_tokens("plan"),
            archive_tokens=segment_tokens("archive"),
            user_tokens=segment_tokens("user"),
            state_tokens=segment_tokens("state"),
            body_tokens=segment_tokens("body"),
            total_tokens=sum(counts),
            cacheable_prefix_tokens=reusable,
        )

    @staticmethod
    def _count_message(msg: dict[str, Any]) -> int:
        """统计单条消息的 Token（内容+工具调用参数+结构开销）。"""
        total = 4
        content = msg.get("content")
        if isinstance(content, str):
            total += _count_text_tokens(content)
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {}) if isinstance(tc, dict) else {}
            total += _count_text_tokens(str(func.get("arguments", ""))) + 10
        return total

    @classmethod
    def _count_messages(cls, messages: list[dict[str, Any]]) -> int:
        """统计消息列表 Token（内容+工具调用参数+每条结构开销）。"""
        return sum(cls._count_message(msg) for msg in messages)

    def _compact_tool_result(self, msg: dict[str, Any]) -> dict[str, Any]:
        """兜底：单条 tool 消息超过字节上限时保留头尾。

        工具输出上限已在各工具内部对齐预算，这里只处理意外情况。

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


def _safe_head(encoded: bytes, n: int) -> bytes:
    """返回最多 n 字节，按 UTF-8 字符边界对齐。"""
    if len(encoded) <= n:
        return encoded
    end = n
    while end > 0 and (encoded[end] & 0xC0) == 0x80:
        end -= 1
    return encoded[:end]


def _safe_tail(encoded: bytes, n: int) -> bytes:
    """返回最多 n 字节，按 UTF-8 字符边界对齐。"""
    if len(encoded) <= n:
        return encoded
    start = len(encoded) - n
    while start < len(encoded) and (encoded[start] & 0xC0) == 0x80:
        start += 1
    return encoded[start:]

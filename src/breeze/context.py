"""上下文管理（策略 v3：回合内只追加，硬上限兜底）。

压缩单位仍是"用户回合"，但降级时机改为两级：

- 软预算 ``intra_turn_budget`` 只降级可再生的工具输出（run_shell、run_tests
  等），**不降级 read_file**。文件原文是 apply_patch 的依据，换出后
  模型只能重读；改写历史还会切断前缀缓存。
- 只有超过 ``hard_budget``（默认与软预算相同）才动 read_file：保护每个路径最近
  一次读取，并按消息顺序从旧到新降级更早的读取，保证刚读到的新内容优先留在
  上下文里；仍超限时才放开保护。被降级的范围只剩指针，模型仍可能重读；这是
  硬上限的兜底代价，不是没有代价的保留策略。
- 定位类工具（grep、list_files）结果较小时原样保留；较大时保留有界预览与
  总行数，避免模型因为看不到结果而反复调用同一工具。
- 推理模型的 ``reasoning_content`` 随工具调用回传；只有最新一步保留全文，更早的
  截断为短摘录。被截断的消息只有一个，缓存失配范围也就限制在最近一步。
- 系统维护的元状态（AGENTS.md、回合归档、工作集快照、计划、SessionState、
  预算提示）统一使用 ``system`` 角色；只有用户真正输入的问题保留 ``user``，
  避免模型把状态索引当成新的用户指令。
- 每次构建提示词都会在末尾追加一条简短的预算提示，告诉模型已用/剩余 Token、
  当前仍可见的 read_file 文件与“旧读取先变指针”的淘汰顺序，让模型自己决定
  是否继续调研。部分网关在最后一条是 system 时会返回空内容，因此元状态之后
  再补一条固定的 user 续跑消息（不含新要求），保证请求以可回应的角色结束。

任何降级发生时，把 SessionState 状态块注入到消息末尾（body 之后），让模型基
于索引定点重读；放在末尾也避免状态每次变化时切断前面历史的缓存前缀。
原始消息列表保持不变，仅供 trace 和调试使用。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tiktoken

from breeze.config import settings

os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path.home() / ".breeze" / "tiktoken_cache"))

POINTER_TOOLS: frozenset[str] = frozenset({"grep", "list_files"})
# 部分 OpenAI-compatible 网关（例如 SJTU 的 deepseek-reasoner）在最后一条消息
# 是 system 时会直接 finish_reason=stop 并返回空内容。元状态之后补一条不含新
# 要求的短 user 消息，保证请求以可回应的角色结束。
_CONTINUATION_PROMPT = "继续当前任务。"
# 小于该字符数的定位结果直接透传：一行指针往往比结果本身还长，
# 且状态块并不记录目录列表，模型只能反复重跑同一工具。
POINTER_KEEP_CHARS = 1_000
# 大结果保留的预览长度：整块丢弃会让模型失去判断依据，只剩“重跑”一个选择。
POINTER_PREVIEW_CHARS = 1_500
READ_TOOLS: frozenset[str] = frozenset({"read_file"})
# 写操作结果不是可再生产出：压缩成“re-run for more”会诱导模型重复 apply_patch，
# 把同一段代码插入多次。写结果本身很短，始终保留原文。
SPILL_TOOLS: frozenset[str] = frozenset({"run_shell", "run_tests"})
# 硬上限兜底时，小文件换出省不下多少 Token，却要模型额外重读，收益为负。
READ_SPILL_MIN_CHARS = 512
# 一次软降级多留余量：只腾出刚好够的空间，下一步新增结果又会触发改写，
# 反复切断 body 中部的缓存前缀。降到 85% 可减少降级次数。
SOFT_SPILL_TARGET_RATIO = 0.85
# 推理模型的思考过程要随工具结果回传，但完整保留每一步会迅速撑爆上下文。
# 只保留最新一步全文：每步最多截断一条旧消息，前缀缓存失配被限制在最近一步；
# 保留两步会让上一步的思考在下一步也被改写，失配范围翻倍。
REASONING_KEEP_MESSAGES = 1
REASONING_PREVIEW_CHARS = 600
_REASONING_TRUNCATED_MARK = "\n…[earlier reasoning truncated]"

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


def _compact_reasoning(body: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留最近几步的完整思考，更早的截断为短摘录。

    推理模型要求思考过程随工具调用回传；全量保留会让提示词随步数线性膨胀，
    因此只让最近 ``REASONING_KEEP_MESSAGES`` 步保持全文，更早的留开头，
    仍能提醒模型当前目标，又不会挤占工具结果的预算。
    """
    indexes = [
        i
        for i, msg in enumerate(body)
        if msg.get("role") == "assistant" and isinstance(msg.get("reasoning_content"), str)
    ]
    keep = set(indexes[-REASONING_KEEP_MESSAGES:])
    if all(i in keep for i in indexes):
        return list(body)

    out: list[dict[str, Any]] = []
    for i, msg in enumerate(body):
        text = msg.get("reasoning_content")
        if i in keep or not isinstance(text, str) or len(text) <= REASONING_PREVIEW_CHARS:
            out.append(msg)
            continue
        trimmed = dict(msg)
        trimmed["reasoning_content"] = text[:REASONING_PREVIEW_CHARS] + _REASONING_TRUNCATED_MARK
        out.append(trimmed)
    return out


SPILL_POINTER_MAX_CHARS = 700
ERROR_KEEP_CHARS = 2_000
_PLAN_ARGUMENTS_PLACEHOLDER = '{"content": "[omitted; the current plan is injected below]"}'
_ERROR_PREFIXES = (
    "Error",
    "Tool execution failed",
    "Failed",
    "Executable not allowed",
    "Python module not allowed",
)


def _one_line(text: str, limit: int = 120) -> str:
    """把多行文本压成一行并限制长度。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _arg_summary(tool: str, arguments: dict[str, Any]) -> str:
    """生成工具调用的简短参数说明，用于结果头与压缩指针。

    并行调用的结果只带 ``tool_call_id``；把命令/路径写进结果本身，模型才能
    把远处的调用与结果对应起来。
    """
    if tool == "run_shell":
        command = arguments.get("command")
        if isinstance(command, str) and command.strip():
            return _one_line(command.strip(), 140)
    if tool == "run_tests":
        pattern = arguments.get("pattern")
        if isinstance(pattern, str) and pattern.strip():
            return f"pattern={pattern.strip()}"
        return "all tests"
    if tool == "read_file":
        path = arguments.get("path", "?")
        start, end = arguments.get("start_line"), arguments.get("end_line")
        if start and end:
            return f"{path} lines {start}-{end}"
        return str(path)
    if tool in POINTER_TOOLS:
        bits = [
            str(value)
            for key, value in arguments.items()
            if key in {"path", "pattern", "glob"} and isinstance(value, str) and value
        ]
        return _one_line(" ".join(bits), 100)
    if tool in {"apply_patch", "create_file", "delete_path", "git_restore"}:
        path = arguments.get("path")
        if isinstance(path, str) and path:
            return f"path={path}"
    bits = " ".join(f"{key}={value}" for key, value in list(arguments.items())[:2] if isinstance(value, (str, int)))
    return _one_line(bits, 100)


def _looks_like_error(result: str) -> bool:
    """判断结果是否是工具错误，而不是可重建的正常输出。"""
    text = result.lstrip()
    return text.startswith(_ERROR_PREFIXES) or "Traceback (most recent call last)" in text[:200]


def _render_error_pointer(tool: str, arg_bits: str, result: str) -> str:
    """错误结果保留原文与修复提示，不压缩成一行。"""
    body = result.strip()
    if len(body) > ERROR_KEEP_CHARS:
        body = body[:ERROR_KEEP_CHARS].rstrip() + "\n... (error truncated)"
    header = f"[{tool} error]" + (f" {arg_bits}" if arg_bits else "")
    return f"{header}\n{body}"


def _find_marker(lines: list[str], marker: str) -> int | None:
    return next((i for i, line in enumerate(lines) if line.strip() == marker), None)


def _shell_summary(result: str) -> str:
    """保留 run_shell 的命令、退出码与最有信息量的输出行。

    ``git status`` / ``git diff --stat`` 的价值在 stdout 首尾行；失败时
    stderr 首行通常就是原因。只留 ``exit_code`` 等于把结果全部丢掉。
    """
    lines = [line.rstrip() for line in result.splitlines()]
    head = lines[0].strip() if lines else ""
    stdout = _find_marker(lines, "--- stdout ---")
    stderr = _find_marker(lines, "--- stderr ---")

    parts = [head] if head else []
    if stdout is None:
        body = [line.strip() for line in lines[1:] if line.strip()]
        if body:
            parts.append(_one_line(body[0]))
        return " | ".join(parts)

    stdout_end = stderr if stderr is not None else len(lines)
    stdout_lines = [line.strip() for line in lines[stdout + 1 : stdout_end] if line.strip()]
    stderr_lines = [line.strip() for line in lines[stderr + 1 :] if line.strip()] if stderr is not None else []
    if stderr_lines:
        parts.append(_one_line(stderr_lines[0]))

    # ruff/flake8 的每条错误都是 “CODE 说明”，只留首尾会把中间的错误码
    # （例如 B905 之后的 E501）藏掉，模型就会漏修。
    lint_codes = [line for line in stdout_lines if re.match(r"^[A-Z]{1,3}\d{3,4}\b", line)]
    if lint_codes:
        for line in lint_codes[:4]:
            text = _one_line(line, 140)
            if text not in parts:
                parts.append(text)
        found = next((line for line in reversed(stdout_lines) if line.startswith("Found ")), "")
        if found and found not in parts:
            parts.append(_one_line(found))
    elif stdout_lines:
        parts.append(_one_line(stdout_lines[0]))
        if len(stdout_lines) > 1:
            last = _one_line(stdout_lines[-1])
            if last not in parts:
                parts.append(last)
    return " | ".join(part for part in parts if part)


def _tests_summary(result: str) -> str:
    """保留 run_tests 的退出码、pytest 汇总与失败项。"""
    keep: list[str] = []
    for line in result.splitlines():
        text = line.strip()
        if text.startswith(("exit_code=", "summary:", "status:", "- ")):
            keep.append(_one_line(text, 160))
    return " | ".join(keep[:6])


def _summarize_spill_result(tool: str, result: str) -> str:
    """按工具保留降级后仍然有用的结果摘要。"""
    if tool == "run_tests":
        return _tests_summary(result)
    if tool == "run_shell":
        return _shell_summary(result)
    first = next((line.strip() for line in result.splitlines() if line.strip()), "")
    return _one_line(first, 160)


def _render_spill_pointer(tool: str, arguments: dict[str, Any], result: str) -> str:
    """把可再生工具输出降级为自描述的摘要指针。"""
    arg_bits = _arg_summary(tool, arguments)
    if _looks_like_error(result):
        return _render_error_pointer(tool, arg_bits, result)

    summary = _summarize_spill_result(tool, result)
    header = f"[{tool} compressed] {arg_bits} → {summary}" if arg_bits else f"[{tool} compressed] → {summary}"
    if len(header) > SPILL_POINTER_MAX_CHARS:
        header = header[: SPILL_POINTER_MAX_CHARS - 1] + "…"
    return f"{header}\n(full output dropped by budget; re-run for more)"


def _render_pointer(tool: str, arguments: dict[str, Any], result: str) -> str:
    """把一条工具结果降级为指针。

    Args:
        tool: 工具名。
        arguments: 该调用的参数（解析失败时为空 dict）。
        result: 原始结果文本，用于统计行数或摘录摘要。

    Returns:
        指针文本。
    """
    n = len([ln for ln in result.splitlines() if ln.strip()])

    if tool == "read_file":
        path = arguments.get("path", "?")
        start, end = arguments.get("start_line"), arguments.get("end_line")
        if start and end:
            rng = f"lines {start}-{end}"
            return f"[read_file compressed] {path} ({rng}) dropped by hard limit; re-read this range before editing."
        return (
            f"[read_file compressed] {path} (full file) dropped by hard limit; "
            "use grep to locate the target, then re-read only that range."
        )

    if tool in POINTER_TOOLS:
        arg_bits = " ".join(f"{k}={v}" for k, v in list(arguments.items())[:3] if isinstance(v, (str, int)))
        body = result.strip()
        truncated = len(body) > POINTER_PREVIEW_CHARS
        if truncated:
            body = body[:POINTER_PREVIEW_CHARS].rstrip()
        header = f"[{tool} compressed] {arg_bits} → {n} lines" if arg_bits else f"[{tool} compressed] → {n} lines"
        pointer = f"{header}\n{body}"
        if truncated:
            pointer += "\n... (truncated; narrow the pattern or path for the rest)"
        return pointer

    return _render_spill_pointer(tool, arguments, result)


@dataclass
class ContextConfig:
    """ContextManager 的配置。

    All fields default to None. None means "use the value from settings".
    Explicit values (including 0 and False) take precedence over config.
    After __post_init__, every field holds a concrete non-None value.

    Attributes:
        enable: 是否启用上下文管理（False 时原样透传）。
        cross_turn: 跨回合策略；``archive`` 只发当前回合并以归档注入历史，
            ``append`` 保留全部历史消息（实验对照用）。
        intra_turn_budget: 单回合软预算；只降级可再生工具输出，不降级 read_file。
        hard_budget: 单回合硬上限；超过后才降级 read_file，默认与软预算相同，
            低于软预算时按软预算处理。
        tool_result_max_bytes: 单条工具结果的字节上限（兜底）。
        tool_result_head_bytes: 兜底压缩保留的头部字节数。
        tool_result_tail_bytes: 兜底压缩保留的尾部字节数。
    """

    enable: bool | None = None
    cross_turn: str | None = None
    intra_turn_budget: int | None = None
    hard_budget: int | None = None
    tool_result_max_bytes: int | None = None
    tool_result_head_bytes: int | None = None
    tool_result_tail_bytes: int | None = None

    def __post_init__(self) -> None:
        """Fill unset fields from settings."""
        if self.enable is None:
            self.enable = settings.context_enable
        if self.cross_turn is None:
            self.cross_turn = settings.context_cross_turn
        if self.cross_turn not in ("archive", "append"):
            raise ValueError(f"Invalid cross_turn mode: {self.cross_turn}. Choose from archive, append.")
        if self.intra_turn_budget is None:
            self.intra_turn_budget = settings.context_intra_turn_budget
        if self.hard_budget is None:
            self.hard_budget = settings.context_hard_budget
        self.hard_budget = max(int(self.hard_budget), int(self.intra_turn_budget))
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
    instructions_tokens: int = 0
    plan_tokens: int = 0
    archive_tokens: int = 0
    snapshot_tokens: int = 0
    budget_tokens: int = 0
    continuation_tokens: int = 0
    user_tokens: int = 0
    state_tokens: int = 0
    body_tokens: int = 0
    total_tokens: int = 0
    cacheable_prefix_tokens: int = 0
    demoted_read_ids: frozenset[str] = frozenset()

    def breakdown(self) -> dict[str, int]:
        """返回按注入位置分类的 Token 组成。"""
        return {
            "system": self.system_tokens,
            "instructions": self.instructions_tokens,
            "plan": self.plan_tokens,
            "archive": self.archive_tokens,
            "snapshot": self.snapshot_tokens,
            "budget": self.budget_tokens,
            "continuation": self.continuation_tokens,
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
        # 已降级的 tool_call_id。后续 build 先原样复用这些指针，避免预算
        # 一时回落时把旧结果还原成原文——那会让整段前缀缓存失效。
        self._spilled_ids: set[str] = set()

    def reset(self) -> None:
        """清空前缀复用基线（新会话时调用）。"""
        self._last_prefix = None
        self._spilled_ids.clear()

    def build(
        self,
        messages: list[dict[str, Any]],
        state: Any | None = None,
        archive: str | None = None,
        plan: str | None = None,
        *,
        include_state: bool = False,
        snapshots: str | None = None,
        instructions: str | None = None,
    ) -> list[dict[str, Any]]:
        """构建发给 LLM 的消息列表。

        Args:
            messages: 当前回合的完整历史（system、user、若干轮）。
            state: SessionState 实例；发生降级或回合启动时注入。
            archive: TurnLedger 渲染的已完成回合文本；注入在 system 之后。
            plan: Agent 维护的短期计划；非空时追加在 body 之后、state 之前，
                避免计划更新切断历史缓存前缀。
            include_state: 回合启动时注入上一回合遗留的状态索引。
            snapshots: 回合启动时注入的工作集文件内容；不参与降级。
            instructions: AGENTS.md 渲染块，作为独立 system 消息注入。

        Returns:
            分级处理后的消息列表。原始消息不被修改。
        """
        result, _ = self.build_with_stats(
            messages,
            state=state,
            archive=archive,
            plan=plan,
            include_state=include_state,
            snapshots=snapshots,
            instructions=instructions,
        )
        return result

    def build_with_stats(
        self,
        messages: list[dict[str, Any]],
        state: Any | None = None,
        archive: str | None = None,
        plan: str | None = None,
        *,
        include_state: bool = False,
        snapshots: str | None = None,
        instructions: str | None = None,
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
            if result and result[-1]["role"] == "system":
                segments["continuation"] = [len(result)]
                result.append({"role": "user", "content": _CONTINUATION_PROMPT})
            return result, self._stats(result, segments)

        head = list(messages[:2])
        # update_plan 的计划正文已经由尾部计划块注入；历史里保留调用与短确认，
        # 但省略参数中的计划全文，避免重复占预算。
        body = self._compact_plan_updates(messages[2:])
        body = _compact_reasoning(body)

        id_to_name, id_to_args = self._index_tool_calls(body)
        batch_index = self._batch_index(body)

        converted: list[dict[str, Any]] = []
        demoted = False
        for msg in body:
            if msg.get("role") != "tool":
                converted.append(msg)
                continue
            tool_call_id = msg.get("tool_call_id", "")
            tool = id_to_name.get(tool_call_id, "")
            content = msg.get("content")
            if tool in POINTER_TOOLS and isinstance(content, str) and len(content) > POINTER_KEEP_CHARS:
                new_msg = dict(msg)
                new_msg["content"] = _render_pointer(tool, id_to_args.get(tool_call_id, {}), content)
                demoted = True
                converted.append(self._compact_tool_result(new_msg))
            else:
                converted.append(self._compact_tool_result(msg))

        # 之前已降级的结果保持指针形态，即使本轮总量暂时低于预算也不再还原。
        demoted = self._apply_frozen_spills(converted, id_to_name, id_to_args) or demoted

        # 状态块只在发生降级时注入；未降级的回合里原始工具结果都还在，
        # 注入跨回合索引既占预算，又会把历史的读取计数误当成当前事实。
        soft_budget = self.config.intra_turn_budget
        counted_state = state if demoted else None
        if self._measure(head, converted, counted_state, archive, plan, snapshots, instructions) > soft_budget:
            converted, spilled = self._spill(
                head,
                converted,
                id_to_name,
                id_to_args,
                soft_budget,
                state,
                archive,
                plan,
                snapshots,
                SPILL_TOOLS,
                instructions=instructions,
                target=int(soft_budget * SOFT_SPILL_TARGET_RATIO),
            )
            demoted = demoted or spilled
            counted_state = state if demoted else None

        hard_budget = self.config.hard_budget
        if self._measure(head, converted, counted_state, archive, plan, snapshots, instructions) > hard_budget:
            protected = self._latest_read_ids(body)
            converted, spilled = self._spill(
                head,
                converted,
                id_to_name,
                id_to_args,
                hard_budget,
                state,
                archive,
                plan,
                snapshots,
                READ_TOOLS,
                instructions=instructions,
                protect=protected,
                min_chars=READ_SPILL_MIN_CHARS,
                oldest_first=True,
            )
            demoted = demoted or spilled
            counted_state = state if demoted else None
        if self._measure(head, converted, counted_state, archive, plan, snapshots, instructions) > hard_budget:
            converted, spilled = self._spill(
                head,
                converted,
                id_to_name,
                id_to_args,
                hard_budget,
                state,
                archive,
                plan,
                snapshots,
                READ_TOOLS,
                instructions=instructions,
                oldest_first=True,
            )
            demoted = demoted or spilled

        # 同一路径的旧读取被降级后，若后面还有一份可见的读取，指针不应再要求
        # 模型重读整个范围——否则模型会追着旧指针反复读取。
        converted = self._annotate_superseded_reads(converted, id_to_name, id_to_args)
        demoted_read_ids = self._demoted_read_ids(converted)
        budget_note = self._budget_note(
            head,
            converted,
            counted_state,
            archive,
            plan,
            snapshots,
            instructions,
            id_to_name,
            id_to_args,
        )

        # 批次头必须在降级之后加：否则 _shell_summary 会把 “[call 1/2 ...]”
        # 当成结果首行，丢掉 exit_code 与错误正文。
        if batch_index:
            headed: list[dict[str, Any]] = []
            for msg in converted:
                if msg.get("role") == "tool":
                    tool_call_id = msg.get("tool_call_id", "")
                    msg = self._with_batch_header(
                        msg,
                        id_to_name.get(tool_call_id, ""),
                        tool_call_id,
                        batch_index,
                        id_to_args,
                    )
                headed.append(msg)
            converted = headed

        result: list[dict[str, Any]] = []
        segments: dict[str, list[int]] = {}

        def add(msg: dict[str, Any], kind: str) -> None:
            segments.setdefault(kind, []).append(len(result))
            result.append(msg)

        add(head[0], "system")
        if instructions:
            add({"role": "system", "content": instructions}, "instructions")
        if archive:
            add({"role": "system", "content": archive}, "archive")
        add(head[1], "user")
        if snapshots:
            add({"role": "system", "content": snapshots}, "snapshot")
        for msg in converted:
            add(msg, "body")
        plan_block = _render_plan(plan)
        if plan_block:
            add({"role": "system", "content": plan_block}, "plan")
        if state is not None and (demoted or include_state):
            block = state.render(turn_start=include_state and not demoted)
            if block:
                add({"role": "system", "content": block}, "state")
        if state is not None and not demoted and not include_state:
            repeated = state.render_repeated()
            if repeated:
                add({"role": "system", "content": repeated}, "state")
        if budget_note:
            add({"role": "system", "content": budget_note}, "budget")
        if result[-1]["role"] == "system":
            add({"role": "user", "content": _CONTINUATION_PROMPT}, "continuation")
        return result, self._stats(result, segments, demoted_read_ids)

    def _apply_frozen_spills(
        self,
        converted: list[dict[str, Any]],
        id_to_name: dict[str, str],
        id_to_args: dict[str, dict[str, Any]],
    ) -> bool:
        """把之前已降级的结果重新压成指针。

        预算随新增消息上下浮动时，如果旧结果被还原成原文，它之后的所有
        消息都会失去前缀缓存；保持它已降级的形态更稳定，也省 Token。

        Args:
            converted: 当前消息列表，原地替换。
            id_to_name: tool_call_id -> 工具名。
            id_to_args: tool_call_id -> 参数 dict。

        Returns:
            是否有消息被重新压成指针。
        """
        if not self._spilled_ids:
            return False
        changed = False
        for i, msg in enumerate(converted):
            if msg.get("role") != "tool":
                continue
            tc_id = str(msg.get("tool_call_id", ""))
            if tc_id not in self._spilled_ids:
                continue
            tool = id_to_name.get(tc_id, "")
            content = msg.get("content")
            if not isinstance(content, str) or content.startswith(f"[{tool} compressed]"):
                continue
            new_msg = dict(msg)
            new_msg["content"] = _render_pointer(tool, id_to_args.get(tc_id, {}), content)
            converted[i] = self._compact_tool_result(new_msg)
            changed = True
        return changed

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
        snapshots: str | None,
        classes: frozenset[str],
        *,
        instructions: str | None = None,
        protect: frozenset[str] = frozenset(),
        min_chars: int = 0,
        oldest_first: bool = False,
        target: int | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """预算溢出时把指定类别的工具结果降级为指针。

        默认从最大的结果开始降级：小结果换出省不下多少 Token，却会迫使模型重读。
        ``oldest_first`` 用于 read_file：按消息顺序从旧到新降级，保证刚读到的新
        内容优先留在上下文里。否则模型会陷入“读取→被压缩→立刻重读”的循环，
        新的读取永远看不到原文。
        ``protect`` 中的 tool_call_id 先跳过（通常是每个文件最近一次读取），
        仍超限时再由调用方放开保护重试。

        Args:
            head: system+user 头部。
            converted: 当前消息列表。
            id_to_name: tool_call_id -> 工具名。
            id_to_args: tool_call_id -> 参数 dict。
            budget: Token 上限。
            state: 用于计入状态块开销，可为 None。
            archive: 回合归档文本，计入 head 开销，可为 None。
            plan: 计划块文本，计入 head 开销，可为 None。
            snapshots: 工作集快照文本，计入 head 开销且不参与降级，可为 None。
            classes: 本次允许降级的工具类别。
            instructions: AGENTS.md 块，计入 head 开销，可为 None。
            protect: 本轮不降级的 tool_call_id 集合。
            min_chars: 小于该字符数的结果不参与降级。
            oldest_first: True 时按消息顺序从旧到新降级，否则按结果大小降序。
            target: 降级目标 Token 数，None 时等于 budget。取小于 budget 的值
                可一次腾出余量，减少后续步骤再次改写 body 的次数。

        Returns:
            (新列表, 是否发生降级)。
        """
        out = list(converted)
        if self._measure(head, out, state, archive, plan, snapshots, instructions) <= budget:
            return out, False
        limit = min(budget, max(1, target)) if target is not None else budget

        candidates: list[tuple[int, int]] = []
        for i, msg in enumerate(out):
            if msg.get("role") != "tool":
                continue
            tc_id = msg.get("tool_call_id", "")
            tool = id_to_name.get(tc_id, "")
            if tool not in classes or tc_id in protect:
                continue
            content = msg.get("content")
            if not isinstance(content, str) or content.startswith(f"[{tool} compressed]"):
                continue
            if len(content) < min_chars:
                continue
            candidates.append((len(content), i))
        if oldest_first:
            candidates.sort(key=lambda item: item[1])
        else:
            candidates.sort(key=lambda item: (-item[0], item[1]))

        changed = False
        for _, i in candidates:
            if self._measure(head, out, state, archive, plan, snapshots, instructions) <= limit:
                break
            msg = out[i]
            tool = id_to_name.get(msg.get("tool_call_id", ""), "")
            new_msg = dict(msg)
            new_msg["content"] = _render_pointer(
                tool, id_to_args.get(msg.get("tool_call_id", ""), {}), msg.get("content", "")
            )
            out[i] = new_msg
            tc_id = str(msg.get("tool_call_id", ""))
            if tc_id:
                self._spilled_ids.add(tc_id)
            changed = True
        return out, changed

    def _budget_note(
        self,
        head: list[dict[str, Any]],
        body: list[dict[str, Any]],
        state: Any | None,
        archive: str | None,
        plan: str | None,
        snapshots: str | None,
        instructions: str | None,
        id_to_name: dict[str, str],
        id_to_args: dict[str, dict[str, Any]],
    ) -> str:
        """每轮告诉模型当前消息预算与仍可见的 read_file 工作集。

        read_file 的淘汰顺序是“旧读取先丢”，模型只有持续看到剩余预算和已读
        文件，才能主动收敛。提示块很小，不参与降级；它放在消息末尾，不切断
        历史前缀。
        """
        hard = self.config.hard_budget
        if hard <= 0:
            return ""
        used = self._measure(head, body, state, archive, plan, snapshots, instructions)

        reads = 0
        visible = 0
        read_tokens = 0
        paths: list[str] = []
        for msg in body:
            if msg.get("role") != "tool":
                continue
            tc_id = str(msg.get("tool_call_id", ""))
            if id_to_name.get(tc_id) != "read_file":
                continue
            content = msg.get("content")
            if not isinstance(content, str):
                continue
            reads += 1
            if content.startswith("["):
                continue
            visible += 1
            read_tokens += _count_text_tokens(content)
            path = id_to_args.get(tc_id, {}).get("path")
            if isinstance(path, str) and path and path not in paths:
                paths.append(path)

        remaining = max(hard - used, 0)
        if remaining:
            usage = f"Message budget: ~{used} / {hard} tokens used (~{remaining} left)."
        else:
            usage = (
                f"Message budget exhausted: ~{used} / {hard} tokens used. "
                "New reads will push older read_file results into pointers."
            )
        if visible:
            shown = ", ".join(paths[:6])
            if len(paths) > 6:
                shown += f" (+{len(paths) - 6} more)"
            if shown:
                files = f"Visible read_file content: ~{read_tokens} tokens / {visible} read(s) from {shown}."
            else:
                files = f"Visible read_file content: ~{read_tokens} tokens / {visible} read(s)."
        else:
            files = "Visible read_file content: none yet."
        if reads > visible:
            files += f" {reads - visible} older read(s) already compressed to pointers."
        return "\n".join(
            [
                "[Context budget — system-maintained, not user input]",
                usage,
                files,
                "When the limit is exceeded, the oldest read_file results become pointers first "
                "and the newest reads stay. Plan reads accordingly: do not re-read unchanged "
                "files, and read only the ranges you still need.",
            ]
        )

    @staticmethod
    def _annotate_superseded_reads(
        converted: list[dict[str, Any]],
        id_to_name: dict[str, str],
        id_to_args: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """同一文件有更新副本时，把旧读取指针改写成“已被更新副本取代”。

        旧指针原本写着“re-read this range”，模型会照着它反复读取；若后面已经
        有一份可见的读取，指针应引导模型使用新副本而不是再读一遍。
        """
        latest_visible: dict[str, int] = {}
        for i, msg in enumerate(converted):
            if msg.get("role") != "tool":
                continue
            tc_id = str(msg.get("tool_call_id", ""))
            if id_to_name.get(tc_id) != "read_file":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or content.startswith("["):
                continue
            path = id_to_args.get(tc_id, {}).get("path")
            if isinstance(path, str):
                latest_visible[path] = i
        if not latest_visible:
            return list(converted)

        out: list[dict[str, Any]] = []
        for i, msg in enumerate(converted):
            content = msg.get("content")
            if (
                msg.get("role") != "tool"
                or not isinstance(content, str)
                or not content.startswith("[read_file compressed]")
            ):
                out.append(msg)
                continue
            tc_id = str(msg.get("tool_call_id", ""))
            args = id_to_args.get(tc_id, {})
            path = args.get("path")
            if not isinstance(path, str) or latest_visible.get(path, -1) <= i:
                out.append(msg)
                continue
            start, end = args.get("start_line"), args.get("end_line")
            rng = f"lines {start}-{end}" if start and end else "full file"
            new_msg = dict(msg)
            new_msg["content"] = (
                f"[read_file superseded] {path} ({rng}) — older copy dropped by hard limit; "
                "a newer read of this file appears later in this context. Use that copy for "
                "the lines it covers; read only the missing range."
            )
            out.append(new_msg)
        return out

    @staticmethod
    def _demoted_read_ids(converted: list[dict[str, Any]]) -> frozenset[str]:
        """返回本次提示词中没有原文的 read_file tool_call_id。"""
        prefixes = ("[read_file compressed]", "[read_file superseded]")
        return frozenset(
            str(msg.get("tool_call_id", ""))
            for msg in converted
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"].startswith(prefixes)
        )

    @staticmethod
    def _compact_plan_updates(body: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """保留 update_plan 的调用与确认，但省略重复的计划正文。

        计划正文已经由尾部计划块注入；assistant tool_call 参数再复制一份
        既占预算又可能过期。只把参数替换成占位符，保留 tool_call 与短结果，
        模型才能确认调用已经执行，不会因为看不到结果而反复调用。
        """
        plan_ids = {
            str(tc.get("id", ""))
            for msg in body
            if msg.get("role") == "assistant"
            for tc in msg.get("tool_calls") or []
            if isinstance(tc, dict) and tc.get("function", {}).get("name") == "update_plan"
        }
        if not plan_ids:
            return list(body)

        out: list[dict[str, Any]] = []
        for msg in body:
            if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                out.append(msg)
                continue
            changed = False
            calls: list[dict[str, Any]] = []
            for tc in msg["tool_calls"]:
                if isinstance(tc, dict) and str(tc.get("id", "")) in plan_ids:
                    function = dict(tc.get("function") or {})
                    function["arguments"] = _PLAN_ARGUMENTS_PLACEHOLDER
                    tc = {**tc, "function": function}
                    changed = True
                calls.append(tc)
            if changed:
                msg = dict(msg)
                msg["tool_calls"] = calls
            out.append(msg)
        return out

    @staticmethod
    def _batch_index(body: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
        """返回同一步并行调用的位置与总数：tool_call_id -> (pos, total)。"""
        index: dict[str, tuple[int, int]] = {}
        for msg in body:
            calls = [tc for tc in msg.get("tool_calls") or [] if isinstance(tc, dict)]
            if len(calls) < 2:
                continue
            for pos, tc in enumerate(calls, 1):
                index[str(tc.get("id", ""))] = (pos, len(calls))
        return index

    @staticmethod
    def _with_batch_header(
        msg: dict[str, Any],
        tool: str,
        tool_call_id: str,
        batch_index: dict[str, tuple[int, int]],
        id_to_args: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """给并行批次中的结果加自描述头，便于与远处的调用对应。"""
        batch = batch_index.get(tool_call_id)
        content = msg.get("content")
        if batch is None or not isinstance(content, str) or not content or content.startswith("["):
            return msg
        pos, total = batch
        args = _arg_summary(tool, id_to_args.get(tool_call_id, {}))
        header = f"[call {pos}/{total} {tool}" + (f": {args}" if args else "") + "]"
        new_msg = dict(msg)
        new_msg["content"] = f"{header}\n{content}"
        return new_msg

    @staticmethod
    def _latest_read_ids(body: list[dict[str, Any]]) -> frozenset[str]:
        """返回每个文件路径最近一次 read_file 的 tool_call_id。"""
        latest: dict[str, str] = {}
        for msg in body:
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                func = tc.get("function", {})
                if func.get("name") != "read_file":
                    continue
                args = _parse_arguments(func.get("arguments", "{}"))
                path = args.get("path")
                if isinstance(path, str) and path:
                    latest[path] = str(tc.get("id", ""))
        return frozenset(latest.values())

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
        snapshots: str | None = None,
        instructions: str | None = None,
    ) -> int:
        """估算 head+instructions+plan+archive+snapshot(+状态块)+body 的 Token 总量。"""
        total = self._count_messages(head) + self._count_messages(body)
        if instructions:
            total += _count_text_tokens(instructions) + 10
        plan_block = _render_plan(plan)
        if plan_block:
            total += _count_text_tokens(plan_block) + 10
        if archive:
            total += _count_text_tokens(archive) + 10
        if snapshots:
            total += _count_text_tokens(snapshots) + 10
        if state is not None:
            block = state.render()
            if block:
                total += _count_text_tokens(block) + 10
        return total

    def _stats(
        self,
        messages: list[dict[str, Any]],
        segments: dict[str, list[int]],
        demoted_read_ids: frozenset[str] = frozenset(),
    ) -> PromptStats:
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
            instructions_tokens=segment_tokens("instructions"),
            plan_tokens=segment_tokens("plan"),
            archive_tokens=segment_tokens("archive"),
            snapshot_tokens=segment_tokens("snapshot"),
            budget_tokens=segment_tokens("budget"),
            continuation_tokens=segment_tokens("continuation"),
            user_tokens=segment_tokens("user"),
            state_tokens=segment_tokens("state"),
            body_tokens=segment_tokens("body"),
            total_tokens=sum(counts),
            cacheable_prefix_tokens=reusable,
            demoted_read_ids=demoted_read_ids,
        )

    @staticmethod
    def _count_message(msg: dict[str, Any]) -> int:
        """统计单条消息的 Token（内容+工具调用参数+结构开销）。"""
        total = 4
        content = msg.get("content")
        if isinstance(content, str):
            total += _count_text_tokens(content)
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str):
            total += _count_text_tokens(reasoning)
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

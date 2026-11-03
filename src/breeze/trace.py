"""运行追踪。

RunTrace 将一次 Agent 运行的所有 LLM 调用和工具调用写入 JSONL 文件，
便于事后复盘、Token 统计和评估对比。
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from breeze.config import settings

DEFAULT_TRACE_DIR = Path("traces")
RESULT_PREVIEW_LIMIT = 500
CONTENT_PREVIEW_LIMIT = 500
_NAME_MAX_LEN = 64


def normalize_trace_name(name: str) -> str:
    """把用户提供的 trace 名称转成安全的文件名片段。

    只保留字母、数字（含中文）、点、下划线与连字符；去掉结尾的
    .jsonl，防止用户误以为可以指定完整文件名。
    """
    if name.lower().endswith(".jsonl"):
        name = name[: -len(".jsonl")]
    stem = re.sub(r"[^\w.-]+", "_", name.strip()).strip("._-")
    return stem[:_NAME_MAX_LEN] or "trace"


@dataclass
class _Counters:
    """累计统计。"""

    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_prompt_tokens: int = 0
    cacheable_prefix_tokens: int = 0
    cache_reported_calls: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cost_reported_calls: int = 0
    cost_usd: float = 0.0
    llm_latency_ms: float = 0.0
    tool_latency_ms: float = 0.0
    errors: int = 0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _preview(text: str | None, limit: int) -> str | None:
    """截断文本时保留头部与尾部。

    只留头部会丢掉 pytest 等命令的失败摘要（通常在输出末尾），
    复盘时不得不重跑；头尾各留一半，单条记录长度仍受 limit 约束。
    """
    if text is None:
        return None
    if len(text) <= limit:
        return text
    marker = "\n...\n"
    keep = max(0, limit - len(marker))
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + text[-tail:]


class RunTrace:
    """一次运行的 JSONL 追踪记录器。

    Usage:
        with RunTrace(trace_dir="traces") as trace:
            trace.log_llm(step=0, model="...", prompt_tokens=100, ...)
            trace.log_tool(step=0, tool="read_file", arguments={...}, ...)
            print(trace.summary())

    Attributes:
        run_id: 本次运行的短 ID。
        path: JSONL 文件路径。
    """

    def __init__(
        self,
        trace_dir: str | Path = DEFAULT_TRACE_DIR,
        run_id: str | None = None,
        name: str | None = None,
        capture_messages: bool | None = None,
    ) -> None:
        """初始化追踪器并创建 JSONL 文件。

        Args:
            trace_dir: 追踪文件目录，不存在时自动创建。
            run_id: 可选的自定义 run_id，默认生成 8 位十六进制。
            name: 可选的文件名前缀，便于在 traces/ 里辨认；非法字符
                会被替换为下划线，完整文件名仍附带时间戳与 run_id。
            capture_messages: 是否把每次调用的完整 messages/tools 写入
                独立的 ``*.messages.jsonl``。None 时读取 trace 配置。
        """
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.name = normalize_trace_name(name) if name else None
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        stem = f"{timestamp}_{self.run_id}"
        if self.name:
            stem = f"{self.name}_{stem}"
        self.path = self.trace_dir / f"{stem}.jsonl"

        self._file = self.path.open("w", encoding="utf-8")
        self._counters = _Counters()
        self._closed = False

        capture = settings.trace_capture_messages if capture_messages is None else capture_messages
        self.messages_path = self.path.with_name(f"{self.path.stem}.messages.jsonl") if capture else None
        self._messages_file = (
            self.messages_path.open("w", encoding="utf-8") if self.messages_path else None
        )

        self._write(
            {
                "event": "run_start",
                "run_id": self.run_id,
                "ts": _now_iso(),
            }
        )

    def log_llm(
        self,
        step: int,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        latency_ms: float,
        content: str | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        cache_hit_tokens: int | None = None,
        cache_miss_tokens: int | None = None,
        cost_usd: float | None = None,
        prompt_breakdown: dict[str, int] | None = None,
        cacheable_prefix_tokens: int | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
    ) -> None:
        """记录一次 LLM 调用。

        Args:
            step: Agent 循环步数，从 0 开始。
            model: 模型名。
            prompt_tokens: 输入 Token 数。
            completion_tokens: 输出 Token 数。
            total_tokens: 总 Token 数。
            latency_ms: 调用耗时（毫秒）。
            content: 助手文本内容，可为 None。
            tool_calls: 工具调用列表，可为 None。
            cache_hit_tokens: provider 报告的缓存命中输入 Token；
                provider 不支持时为 None。
            cache_miss_tokens: provider 报告的缓存未命中输入 Token。
            cost_usd: LiteLLM 估算的本次调用费用（美元）。
            prompt_breakdown: 按注入位置统计的消息 Token 组成。
            cacheable_prefix_tokens: 与上一次调用相同的消息前缀 Token。
            messages: 本次实际发送给 LLM 的完整消息列表；仅在开启
                capture_messages 时写入独立的 ``*.messages.jsonl``。
            tools: 本次发送的工具 schema 列表。
            tool_choice: 本次的工具选择策略。
            temperature: 本次请求的温度；None 表示未知。
        """
        c = self._counters
        c.llm_calls += 1
        c.prompt_tokens += prompt_tokens
        c.completion_tokens += completion_tokens
        c.total_tokens += total_tokens
        c.llm_latency_ms += latency_ms

        if prompt_breakdown is not None:
            c.estimated_prompt_tokens += sum(prompt_breakdown.values())
        if cacheable_prefix_tokens is not None:
            c.cacheable_prefix_tokens += cacheable_prefix_tokens
        if cache_hit_tokens is not None or cache_miss_tokens is not None:
            c.cache_reported_calls += 1
            c.cache_hit_tokens += cache_hit_tokens or 0
            c.cache_miss_tokens += cache_miss_tokens or 0
        if cost_usd is not None:
            c.cost_reported_calls += 1
            c.cost_usd += cost_usd

        self._write(
            {
                "event": "llm_call",
                "run_id": self.run_id,
                "step": step,
                "ts": _now_iso(),
                "model": model,
                "temperature": temperature,
                "latency_ms": round(latency_ms, 2),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cache_hit_tokens": cache_hit_tokens,
                "cache_miss_tokens": cache_miss_tokens,
                "cost_usd": cost_usd,
                "prompt_breakdown": prompt_breakdown,
                "cacheable_prefix_tokens": cacheable_prefix_tokens,
                "content_preview": _preview(content, CONTENT_PREVIEW_LIMIT),
                "tool_calls": tool_calls or [],
            }
        )

        if self._messages_file is not None and messages is not None:
            self._write_messages(
                {
                    "event": "llm_messages",
                    "run_id": self.run_id,
                    "step": step,
                    "ts": _now_iso(),
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": tool_choice,
                    "temperature": temperature,
                    "response": {
                        "content": content,
                        "tool_calls": tool_calls or [],
                    },
                }
            )

    def log_tool(
        self,
        step: int,
        tool: str,
        arguments: dict[str, Any],
        result: str | None,
        latency_ms: float,
        error: str | None = None,
    ) -> None:
        """记录一次工具调用。

        Args:
            step: Agent 循环步数。
            tool: 工具名。
            arguments: 传给工具的参数。
            result: 工具返回值，可为 None。
            latency_ms: 执行耗时（毫秒）。
            error: 错误信息，成功时为 None。
        """
        c = self._counters
        c.tool_calls += 1
        c.tool_latency_ms += latency_ms
        if error:
            c.errors += 1

        self._write(
            {
                "event": "tool_call",
                "run_id": self.run_id,
                "step": step,
                "ts": _now_iso(),
                "tool": tool,
                "arguments": arguments,
                "result_preview": _preview(result, RESULT_PREVIEW_LIMIT),
                "error": error,
                "latency_ms": round(latency_ms, 2),
            }
        )

    def log_error(self, step: int, message: str) -> None:
        """记录一次非工具错误。

        Args:
            step: Agent 循环步数。
            message: 错误信息。
        """
        self._counters.errors += 1
        self._write(
            {
                "event": "error",
                "run_id": self.run_id,
                "step": step,
                "ts": _now_iso(),
                "message": message,
            }
        )

    def log_guard(self, step: int, message: str) -> None:
        """记录一次运行中自检拦截。

        自检拦截不是工具或 LLM 错误，不计入 errors，只用于事后诊断。

        Args:
            step: Agent 循环步数。
            message: 拦截原因。
        """
        self._write(
            {
                "event": "guard",
                "run_id": self.run_id,
                "step": step,
                "ts": _now_iso(),
                "message": message,
            }
        )

    def log_permission(self, step: int, command: str, decision: str, mode: str) -> None:
        """记录一次命令权限判定。

        Args:
            step: Agent 循环步数。
            command: 被检查的命令。
            decision: ``allowed`` 或 ``denied``。
            mode: 当前权限模式（confirm / open）。
        """
        self._write(
            {
                "event": "permission",
                "run_id": self.run_id,
                "step": step,
                "ts": _now_iso(),
                "command": command,
                "decision": decision,
                "mode": mode,
            }
        )

    def summary(self) -> dict[str, Any]:
        """返回当前累计统计。

        Returns:
            包含 run_id、路径、调用次数、Token 与延迟的字典。
        """
        c = self._counters
        cache_total = c.cache_hit_tokens + c.cache_miss_tokens
        cache_hit_rate = (
            round(c.cache_hit_tokens / cache_total, 4)
            if c.cache_reported_calls and cache_total
            else None
        )
        prefix_ratio = (
            round(c.cacheable_prefix_tokens / c.estimated_prompt_tokens, 4)
            if c.estimated_prompt_tokens
            else None
        )
        return {
            "run_id": self.run_id,
            "trace_path": str(self.path),
            "messages_path": str(self.messages_path) if self.messages_path else None,
            "llm_calls": c.llm_calls,
            "tool_calls": c.tool_calls,
            "prompt_tokens": c.prompt_tokens,
            "completion_tokens": c.completion_tokens,
            "total_tokens": c.total_tokens,
            "estimated_prompt_tokens": c.estimated_prompt_tokens,
            "cache_reported_calls": c.cache_reported_calls,
            "cache_hit_tokens": c.cache_hit_tokens,
            "cache_miss_tokens": c.cache_miss_tokens,
            "cache_hit_rate": cache_hit_rate,
            "cacheable_prefix_tokens": c.cacheable_prefix_tokens,
            "cacheable_prefix_ratio": prefix_ratio,
            "cost_usd": round(c.cost_usd, 6) if c.cost_reported_calls else None,
            "llm_latency_ms": round(c.llm_latency_ms, 2),
            "tool_latency_ms": round(c.tool_latency_ms, 2),
            "errors": c.errors,
        }

    def close(self) -> None:
        if self._closed:
            return
        self._write(
            {
                "event": "run_summary",
                "run_id": self.run_id,
                "ts": _now_iso(),
                **self.summary(),
            }
        )
        self._file.close()
        if self._messages_file is not None:
            self._messages_file.close()
        self._closed = True

    def __enter__(self) -> "RunTrace":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _write(self, event: dict[str, Any]) -> None:
        if self._closed:
            return
        line = json.dumps(event, ensure_ascii=False, default=str)
        self._file.write(line + "\n")
        self._file.flush()

    def _write_messages(self, event: dict[str, Any]) -> None:
        """写入完整 prompt 记录；不做截断，只受 run 时长与磁盘限制。"""
        if self._closed or self._messages_file is None:
            return
        line = json.dumps(event, ensure_ascii=False, default=str)
        self._messages_file.write(line + "\n")
        self._messages_file.flush()

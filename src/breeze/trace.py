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
    ) -> None:
        """初始化追踪器并创建 JSONL 文件。

        Args:
            trace_dir: 追踪文件目录，不存在时自动创建。
            run_id: 可选的自定义 run_id，默认生成 8 位十六进制。
            name: 可选的文件名前缀，便于在 traces/ 里辨认；非法字符
                会被替换为下划线，完整文件名仍附带时间戳与 run_id。
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
        """
        c = self._counters
        c.llm_calls += 1
        c.prompt_tokens += prompt_tokens
        c.completion_tokens += completion_tokens
        c.total_tokens += total_tokens
        c.llm_latency_ms += latency_ms

        self._write(
            {
                "event": "llm_call",
                "run_id": self.run_id,
                "step": step,
                "ts": _now_iso(),
                "model": model,
                "latency_ms": round(latency_ms, 2),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "content_preview": _preview(content, CONTENT_PREVIEW_LIMIT),
                "tool_calls": tool_calls or [],
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

    def summary(self) -> dict[str, Any]:
        """返回当前累计统计。

        Returns:
            包含 run_id、路径、调用次数、Token 与延迟的字典。
        """
        c = self._counters
        return {
            "run_id": self.run_id,
            "trace_path": str(self.path),
            "llm_calls": c.llm_calls,
            "tool_calls": c.tool_calls,
            "prompt_tokens": c.prompt_tokens,
            "completion_tokens": c.completion_tokens,
            "total_tokens": c.total_tokens,
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

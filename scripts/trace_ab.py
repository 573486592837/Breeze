"""对比两份 Breeze trace（baseline vs treatment）。

用法：
    python scripts/trace_ab.py BASE.jsonl TREATMENT.jsonl
    python scripts/trace_ab.py BASE.jsonl TREATMENT.jsonl --steps
    python scripts/trace_ab.py BASE.jsonl TREATMENT.jsonl --json

指标分为四组：调用规模、读取行为、Token/成本、任务推进。
``.messages.jsonl`` 同目录存在时，额外统计被指针化的工具结果数量
（按 tool_call_id 去重，避免同一指针在每个后续 prompt 里重复计数）。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

READ_TOOLS = frozenset({"read_file"})
EDIT_TOOLS = frozenset({"apply_patch", "create_file", "delete_path"})
TEST_TOOLS = frozenset({"run_tests"})
POINTER_MARKERS = ("compressed]", "dropped by budget")


@dataclass
class StepRow:
    """单次 LLM 调用的规模与工具分布。"""

    step: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    tools: Counter[str] = field(default_factory=Counter)


@dataclass
class TraceStats:
    """单份 trace 的汇总。"""

    path: Path
    llm_calls: int = 0
    tool_calls: int = 0
    read_calls: int = 0
    unique_reads: int = 0
    duplicate_reads: int = 0
    edit_calls: int = 0
    test_calls: int = 0
    prompt_tokens: int = 0
    estimated_prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    max_prompt_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cacheable_prefix: int = 0
    cache_hit: int = 0
    cache_reported: int = 0
    read_pointers: int = 0
    other_pointers: int = 0
    final_answer_chars: int = 0
    repeated: list[tuple[str, int]] = field(default_factory=list)
    steps: list[StepRow] = field(default_factory=list)

    @property
    def prefix_ratio(self) -> float | None:
        """本地可复用前缀占本地估算 prompt 的比例；无调用时为 None。"""
        if not self.estimated_prompt_tokens:
            return None
        return self.cacheable_prefix / self.estimated_prompt_tokens


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def _messages_path(trace: Path) -> Path:
    return trace.with_name(f"{trace.stem}.messages.jsonl")


def _read_key(arguments: dict[str, Any]) -> tuple[str, Any, Any]:
    return (
        str(arguments.get("path") or "?"),
        arguments.get("start_line"),
        arguments.get("end_line"),
    )


def _scan_messages(messages_path: Path) -> tuple[int, int, int]:
    """扫描 messages 文件：指针化结果数与最终答案长度。

    Returns:
        (read_file 指针数, 其它工具指针数, 最终答案字符数)。
    """
    if not messages_path.exists():
        return 0, 0, 0

    id_to_name: dict[str, str] = {}
    pointer_ids: dict[str, str] = {}
    answer_chars = 0
    for record in _load_jsonl(messages_path):
        response = record.get("response") or {}
        content = response.get("content")
        if isinstance(content, str):
            answer_chars = len(content)
        for message in record.get("messages") or []:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    call_id = str(call.get("id") or "")
                    name = str((call.get("function") or {}).get("name") or "")
                    if call_id:
                        id_to_name[call_id] = name
            elif message.get("role") == "tool":
                call_id = str(message.get("tool_call_id") or "")
                content = str(message.get("content") or "")
                if call_id and any(marker in content[:120] for marker in POINTER_MARKERS):
                    pointer_ids[call_id] = id_to_name.get(call_id, "")

    reads = sum(1 for name in pointer_ids.values() if name in READ_TOOLS)
    return reads, len(pointer_ids) - reads, answer_chars


def summarize(trace: Path) -> TraceStats:
    """汇总一份 trace。

    Args:
        trace: 不含 ``.messages`` 的主 trace 路径。

    Returns:
        TraceStats。
    """
    events = _load_jsonl(trace)
    stats = TraceStats(path=trace)
    read_keys: list[tuple[str, Any, Any]] = []
    tools_by_step: dict[int, Counter[str]] = defaultdict(Counter)

    for event in events:
        kind = event.get("event")
        if kind == "llm_call":
            stats.llm_calls += 1
            prompt = int(event.get("prompt_tokens") or 0)
            completion = int(event.get("completion_tokens") or 0)
            total = int(event.get("total_tokens") or 0)
            stats.prompt_tokens += prompt
            breakdown = event.get("prompt_breakdown") or {}
            estimate = sum(int(value or 0) for value in breakdown.values())
            stats.estimated_prompt_tokens += estimate or prompt
            stats.completion_tokens += completion
            stats.total_tokens += total
            stats.max_prompt_tokens = max(stats.max_prompt_tokens, prompt)
            stats.cost_usd += float(event.get("cost_usd") or 0.0)
            stats.latency_ms += float(event.get("latency_ms") or 0.0)
            stats.cacheable_prefix += int(event.get("cacheable_prefix_tokens") or 0)
            if event.get("cache_hit_tokens") is not None:
                stats.cache_reported += 1
                stats.cache_hit += int(event.get("cache_hit_tokens") or 0)
            stats.steps.append(
                StepRow(
                    step=int(event.get("step") or 0),
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=total,
                )
            )
        elif kind == "tool_call":
            name = str(event.get("tool") or "")
            stats.tool_calls += 1
            step = int(event.get("step") or 0)
            tools_by_step[step][name] += 1
            if name in READ_TOOLS:
                stats.read_calls += 1
                read_keys.append(_read_key(event.get("arguments") or {}))
            if name in EDIT_TOOLS:
                stats.edit_calls += 1
            if name in TEST_TOOLS:
                stats.test_calls += 1

    for row in stats.steps:
        row.tools = tools_by_step.get(row.step, Counter())

    counts = Counter(read_keys)
    stats.unique_reads = len(counts)
    stats.duplicate_reads = sum(count - 1 for count in counts.values() if count > 1)
    stats.repeated = [(path, count) for (path, _, _), count in counts.most_common() if count > 1]

    stats.read_pointers, stats.other_pointers, stats.final_answer_chars = _scan_messages(_messages_path(trace))
    return stats


def _fmt_ratio(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _delta(before: float, after: float) -> str:
    if before == 0:
        return "-" if after == 0 else "new"
    return f"{(after - before) / before * 100:+.1f}%"


def _row(name: str, base: Any, treat: Any, delta: str = "") -> str:
    return f"| {name} | {base} | {treat} | {delta} |"


def markdown(base: TraceStats, treat: TraceStats) -> str:
    """生成对比表。"""
    lines = [
        f"baseline: `{base.path.name}`",
        f"treatment: `{treat.path.name}`",
        "",
        "| Metric | baseline | treatment | Δ |",
        "|---|---:|---:|---:|",
        _row("LLM calls", base.llm_calls, treat.llm_calls, _delta(base.llm_calls, treat.llm_calls)),
        _row("Tool calls", base.tool_calls, treat.tool_calls, _delta(base.tool_calls, treat.tool_calls)),
        _row("read_file calls", base.read_calls, treat.read_calls, _delta(base.read_calls, treat.read_calls)),
        _row("unique reads", base.unique_reads, treat.unique_reads, _delta(base.unique_reads, treat.unique_reads)),
        _row(
            "duplicate reads",
            base.duplicate_reads,
            treat.duplicate_reads,
            _delta(base.duplicate_reads, treat.duplicate_reads),
        ),
        _row("read_file pointers", base.read_pointers, treat.read_pointers),
        _row("other pointers", base.other_pointers, treat.other_pointers),
        _row("edit calls", base.edit_calls, treat.edit_calls),
        _row("test calls", base.test_calls, treat.test_calls),
        _row(
            "prompt tokens",
            f"{base.prompt_tokens:,}",
            f"{treat.prompt_tokens:,}",
            _delta(base.prompt_tokens, treat.prompt_tokens),
        ),
        _row(
            "completion tokens",
            f"{base.completion_tokens:,}",
            f"{treat.completion_tokens:,}",
            _delta(base.completion_tokens, treat.completion_tokens),
        ),
        _row(
            "total tokens",
            f"{base.total_tokens:,}",
            f"{treat.total_tokens:,}",
            _delta(base.total_tokens, treat.total_tokens),
        ),
        _row(
            "max prompt",
            f"{base.max_prompt_tokens:,}",
            f"{treat.max_prompt_tokens:,}",
            _delta(base.max_prompt_tokens, treat.max_prompt_tokens),
        ),
        _row("local prefix", _fmt_ratio(base.prefix_ratio), _fmt_ratio(treat.prefix_ratio)),
        _row("cache hit tokens", base.cache_hit, treat.cache_hit),
        _row("cache reported calls", base.cache_reported, treat.cache_reported),
        _row("cost USD", f"{base.cost_usd:.4f}", f"{treat.cost_usd:.4f}", _delta(base.cost_usd, treat.cost_usd)),
        _row(
            "latency seconds",
            f"{base.latency_ms / 1000:.1f}",
            f"{treat.latency_ms / 1000:.1f}",
            _delta(base.latency_ms, treat.latency_ms),
        ),
        _row("final answer chars", base.final_answer_chars, treat.final_answer_chars),
    ]

    for label, stats in (("baseline", base), ("treatment", treat)):
        if stats.repeated:
            detail = ", ".join(f"{path} ×{count}" for path, count in stats.repeated[:8])
            lines.append("")
            lines.append(f"{label} repeated reads: {detail}")

    return "\n".join(lines)


def steps_table(stats: TraceStats) -> str:
    """单份 trace 的逐步明细。"""
    lines = [
        f"## {stats.path.name}",
        "",
        "| step | prompt | completion | reads | edits | tests | other |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in stats.steps:
        tools = row.tools
        reads = sum(tools[name] for name in READ_TOOLS)
        edits = sum(tools[name] for name in EDIT_TOOLS)
        tests = sum(tools[name] for name in TEST_TOOLS)
        other = sum(tools.values()) - reads - edits - tests
        lines.append(
            f"| {row.step} | {row.prompt_tokens:,} | {row.completion_tokens:,} | "
            f"{reads} | {edits} | {tests} | {other} |"
        )
    return "\n".join(lines)


def _as_dict(stats: TraceStats) -> dict[str, Any]:
    return {
        "path": str(stats.path),
        "llm_calls": stats.llm_calls,
        "tool_calls": stats.tool_calls,
        "read_calls": stats.read_calls,
        "unique_reads": stats.unique_reads,
        "duplicate_reads": stats.duplicate_reads,
        "read_pointers": stats.read_pointers,
        "other_pointers": stats.other_pointers,
        "edit_calls": stats.edit_calls,
        "test_calls": stats.test_calls,
        "prompt_tokens": stats.prompt_tokens,
        "estimated_prompt_tokens": stats.estimated_prompt_tokens,
        "completion_tokens": stats.completion_tokens,
        "total_tokens": stats.total_tokens,
        "max_prompt_tokens": stats.max_prompt_tokens,
        "prefix_ratio": stats.prefix_ratio,
        "cache_hit_tokens": stats.cache_hit,
        "cache_reported_calls": stats.cache_reported,
        "cost_usd": round(stats.cost_usd, 6),
        "latency_seconds": round(stats.latency_ms / 1000, 1),
        "final_answer_chars": stats.final_answer_chars,
        "repeated": stats.repeated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two Breeze traces.")
    parser.add_argument("baseline", type=Path, help="baseline trace JSONL")
    parser.add_argument("treatment", type=Path, help="treatment trace JSONL")
    parser.add_argument("--steps", action="store_true", help="print per-step tables")
    parser.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    args = parser.parse_args()

    base = summarize(args.baseline)
    treat = summarize(args.treatment)

    if args.json:
        print(json.dumps({"baseline": _as_dict(base), "treatment": _as_dict(treat)}, ensure_ascii=False, indent=2))
        return

    print(markdown(base, treat))
    if args.steps:
        print()
        print(steps_table(base))
        print()
        print(steps_table(treat))


if __name__ == "__main__":
    main()

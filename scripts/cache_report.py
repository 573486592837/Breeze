"""从 trace JSONL 汇总 prompt 组成、前缀复用与缓存成本。

用法：
    python scripts/cache_report.py [trace.jsonl | trace_dir ...]

默认读取 ``traces/``。旧 trace 没有缓存字段时按“未报告”处理。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PARTS = ("system", "plan", "archive", "user", "state", "body")


@dataclass
class RunStats:
    """单个 trace 文件的汇总。"""

    path: Path
    run_id: str = ""
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_prompt_tokens: int = 0
    cacheable_prefix_tokens: int = 0
    cache_reported_calls: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    cost_usd: float = 0.0
    cost_reported_calls: int = 0
    breakdown: Counter[str] = field(default_factory=Counter)

    def add(self, event: dict[str, Any]) -> None:
        self.calls += 1
        self.prompt_tokens += int(event.get("prompt_tokens") or 0)
        self.completion_tokens += int(event.get("completion_tokens") or 0)
        if not self.run_id:
            self.run_id = str(event.get("run_id") or "")

        breakdown = event.get("prompt_breakdown")
        if isinstance(breakdown, dict):
            self.estimated_prompt_tokens += sum(int(v or 0) for v in breakdown.values())
            for key in PARTS:
                self.breakdown[key] += int(breakdown.get(key) or 0)
        prefix = event.get("cacheable_prefix_tokens")
        if prefix is not None:
            self.cacheable_prefix_tokens += int(prefix)

        hit = event.get("cache_hit_tokens")
        miss = event.get("cache_miss_tokens")
        if hit is not None or miss is not None:
            self.cache_reported_calls += 1
            self.cache_hit_tokens += int(hit or 0)
            self.cache_miss_tokens += int(miss or 0)

        cost = event.get("cost_usd")
        if cost is not None:
            self.cost_reported_calls += 1
            self.cost_usd += float(cost)

    @property
    def reuse_ratio(self) -> float | None:
        if not self.estimated_prompt_tokens:
            return None
        return self.cacheable_prefix_tokens / self.estimated_prompt_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        total = self.cache_hit_tokens + self.cache_miss_tokens
        if not self.cache_reported_calls or not total:
            return None
        return self.cache_hit_tokens / total


def collect(paths: list[Path]) -> list[Path]:
    """展开输入路径为 trace 文件列表。"""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.jsonl")))
        elif path.is_file():
            files.append(path)
        else:
            print(f"skip missing path: {path}")
    return files


def load_run(path: Path) -> RunStats | None:
    """读取一个 trace 文件；没有 llm_call 时返回 None。"""
    stats = RunStats(path=path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"skip unreadable trace: {path}: {exc}")
        return None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "llm_call":
            stats.add(event)
    return stats if stats.calls else None


def _fmt_ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def _fmt_cost(stats: RunStats) -> str:
    return "-" if not stats.cost_reported_calls else f"${stats.cost_usd:.6f}"


def _fmt_cache(stats: RunStats) -> str:
    """缓存命中率；同时显示有多少次调用真正报告了缓存字段。"""
    if not stats.cache_reported_calls:
        return "-"
    rate = stats.cache_hit_rate
    if rate is None:
        return f"? ({stats.cache_reported_calls}/{stats.calls})"
    return f"{rate:.1%} ({stats.cache_reported_calls}/{stats.calls})"


def print_report(runs: list[RunStats]) -> None:
    """打印按运行分组的汇总表。"""
    header = (
        f"{'run':<30} {'calls':>5} {'prompt':>9} {'msgs':>9} "
        f"{'reuse':>7} {'cache':>15} {'cost':>11}"
    )
    print(header)
    print("-" * len(header))
    for run in runs:
        name = run.path.stem[-30:]
        print(
            f"{name:<30} {run.calls:>5} {run.prompt_tokens:>9} "
            f"{run.estimated_prompt_tokens:>9} {_fmt_ratio(run.reuse_ratio):>7} "
            f"{_fmt_cache(run):>15} {_fmt_cost(run):>11}"
        )

    total = RunStats(path=Path("TOTAL"))
    for run in runs:
        total.calls += run.calls
        total.prompt_tokens += run.prompt_tokens
        total.completion_tokens += run.completion_tokens
        total.estimated_prompt_tokens += run.estimated_prompt_tokens
        total.cacheable_prefix_tokens += run.cacheable_prefix_tokens
        total.cache_reported_calls += run.cache_reported_calls
        total.cache_hit_tokens += run.cache_hit_tokens
        total.cache_miss_tokens += run.cache_miss_tokens
        total.cost_usd += run.cost_usd
        total.cost_reported_calls += run.cost_reported_calls
        total.breakdown.update(run.breakdown)
    print("-" * len(header))
    print(
        f"{'TOTAL':<30} {total.calls:>5} {total.prompt_tokens:>9} "
        f"{total.estimated_prompt_tokens:>9} {_fmt_ratio(total.reuse_ratio):>7} "
        f"{_fmt_cache(total):>15} {_fmt_cost(total):>11}"
    )
    print(
        "\nreuse = cacheable message prefix / estimated message tokens; "
        "cache = provider-reported hit rate (reported/calls)"
    )

    if total.estimated_prompt_tokens:
        print("\nmessage composition (share of estimated prompt tokens):")
        for key in PARTS:
            tokens = total.breakdown[key]
            print(f"  {key:<8} {tokens:>9}  {tokens / total.estimated_prompt_tokens:>6.1%}")
        print(
            f"  {'tool+fmt':<8} "
            f"{max(total.prompt_tokens - total.estimated_prompt_tokens, 0):>9}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize cache and prompt composition from trace JSONL files.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path("traces")],
        help="trace files or directories (default: traces)",
    )
    args = parser.parse_args()

    files = collect(args.paths)
    if not files:
        raise SystemExit("no trace files found")
    runs = [stats for path in files if (stats := load_run(path)) is not None]
    if not runs:
        raise SystemExit("no llm_call events found")
    print_report(runs)


if __name__ == "__main__":
    main()

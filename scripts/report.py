"""从评估 JSON 生成 Markdown 报告。

Usage:
    python scripts/report.py
    python scripts/report.py --input reports/eval_results.json --output reports/eval_results.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_INPUT = Path("reports/eval_results.json")
DEFAULT_OUTPUT = Path("reports/eval_results.md")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render eval JSON to Markdown.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def _fmt_pct(numerator: int, denominator: int) -> str:
    """格式化百分比，分母为 0 时返回 'N/A'。"""
    if denominator <= 0:
        return "N/A"
    return f"{numerator / denominator * 100:.1f}%"


def _truncate(text: str, limit: int) -> str:
    """截断文本用于表格单元格。"""
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _status(success: bool | None) -> str:
    """把 success 字段渲染为符号。"""
    if success is True:
        return "PASS"
    if success is False:
        return "FAIL"
    return "UNGRADED"


def render_markdown(report: dict) -> str:
    """生成 Markdown 报告。"""
    summary = report["summary"]
    results = report["results"]

    total = summary["total_tasks"]
    success = summary["success"]
    failed = summary["failed"]
    ungraded = summary["ungraded"]

    lines: list[str] = []

    lines.append(f"# Breeze Baseline {report.get('version', 1)}")
    lines.append("")
    lines.append(f"- Model: `{report.get('model', 'unknown')}`")
    lines.append(f"- Repository: `{report.get('repo', 'breeze')}`")
    lines.append(f"- Generated at: {report.get('generated_at', 'unknown')}")
    lines.append(f"- Tasks: {total}")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    graded_tasks = summary.get("graded_tasks", 0)
    total_runs = summary.get("total_runs", 0)
    passes = summary.get("passes", 0)
    all_pass = summary.get("all_pass_tasks", 0)
    lines.append(f"| Success rate (by task) | {_fmt_pct(success, total)} ({success}/{total}) |")
    if total_runs:
        lines.append(f"| Pass rate (by run) | {_fmt_pct(passes, total_runs)} ({passes}/{total_runs} runs) |")
        lines.append(f"| All-pass tasks | {all_pass}/{graded_tasks} |")
    lines.append(f"| Failed | {failed} |")
    lines.append(f"| Ungraded | {ungraded} |")
    lines.append(f"| Total tokens | {summary['total_tokens']:,} |")
    lines.append(f"| Avg tokens per task | {summary['avg_tokens']:,.1f} |")
    lines.append(f"| Avg LLM calls | {summary['avg_llm_calls']} |")
    lines.append(f"| Avg tool calls | {summary['avg_tool_calls']} |")
    lines.append(f"| Avg duration (ms) | {summary['avg_duration_ms']:,.1f} |")
    lines.append(f"| Errors | {summary['errors']} |")
    lines.append("")

    lines.append("## Per-task results")
    lines.append("")
    lines.append("| ID | Type | Status | Pass | Tokens | LLM | Tool | Duration (ms) | Question |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        passes = r.get("passes")
        pass_cell = f"{r.get('pass_count', 0)}/{len(passes)}" if passes else "-"
        lines.append(
            f"| {r['id']} | {r['type']} | {_status(r.get('success'))} | {pass_cell} "
            f"| {r['total_tokens']:,} | {r['llm_calls']} | {r['tool_calls']} "
            f"| {r['duration_ms']:,.1f} | {_truncate(r['question'], 60)} |"
        )
    lines.append("")

    lines.append("## Answers")
    lines.append("")
    for r in results:
        lines.append(f"### {r['id']} — {_status(r.get('success'))}")
        lines.append("")
        lines.append(f"**Question:** {r['question']}")
        lines.append("")
        lines.append(f"**Success criteria:** {r['success_criteria']}")
        lines.append("")
        lines.append("**Answer:**")
        lines.append("")
        lines.append(r["answer"])
        lines.append("")
        if r.get("notes"):
            lines.append(f"**Notes:** {r['notes']}")
            lines.append("")
        lines.append(
            f"*tokens={r['total_tokens']}, llm_calls={r['llm_calls']}, "
            f"tool_calls={r['tool_calls']}, duration={r['duration_ms']}ms, "
            f"trace={r['trace_path']}*"
        )
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    if not args.input.exists():
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 1

    report = json.loads(args.input.read_text(encoding="utf-8"))
    md = render_markdown(report)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(md, encoding="utf-8")
    print(f"report written: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

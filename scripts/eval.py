"""批量评估脚本。

Usage:
    python scripts/eval.py
    python scripts/eval.py --task e01 --task l01
    python scripts/eval.py --dry-run
    python scripts/eval.py --no-llm --output reports/dry_run.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _path in (_ROOT / "src", _ROOT):
    if _path.is_dir():
        sys.path.insert(0, str(_path))


from breeze.llm import LLMResponse  # noqa: E402
from breeze_eval.harness import run_eval  # noqa: E402


class _FakeLLM:
    """假 LLM，用于 --no-llm 模式验证脚本本身。"""

    def __init__(self, *args, **kwargs) -> None:
        self.model = "fake-llm"

    def chat(self, messages, tools=None, temperature=0.0) -> LLMResponse:
        return LLMResponse(
            content="(fake answer from --no-llm mode)",
            tool_calls=[],
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Breeze baseline evaluation.",
    )
    parser.add_argument(
        "--tasks",
        type=Path,
        default=Path("tasks/baseline.yaml"),
        help="task set YAML (default: tasks/baseline.yaml)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/eval_results.json"),
        help="output JSON (default: reports/eval_results.json)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: cwd)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="override model name",
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        default=Path("traces"),
        help="trace directory (default: traces)",
    )
    parser.add_argument(
        "--task",
        action="append",
        dest="tasks_filter",
        help="run only the given task id, can be repeated",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list tasks without running them",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="use a fake LLM to verify the script itself",
    )
    parser.add_argument(
        "--repeat", type=int, default=1, help="run each task N times and use median tokens (default: 1)"
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("fixtures"),
        help="directory for setup patch files (default: fixtures)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    only_ids = set(args.tasks_filter) if args.tasks_filter else None

    def progress(msg: str) -> None:
        print(msg, flush=True)

    run_eval(
        tasks_path=args.tasks,
        output_path=args.output,
        repo_root=args.repo.resolve(),
        model=args.model,
        only_ids=only_ids,
        trace_dir=args.trace_dir,
        fixtures_dir=args.fixtures_dir,
        dry_run=args.dry_run,
        repeat=args.repeat,
        llm_factory=_FakeLLM if args.no_llm else None,
        progress=progress,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

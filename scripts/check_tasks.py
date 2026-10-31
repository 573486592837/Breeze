"""任务集预检：确认每个任务"修复前必然失败"。

对每个任务在临时 worktree 中执行 setup，然后检查两件事：
  1. setup 之后受保护路径（protect）应当是干净的——否则 Agent 一开始
     就被判违规，任务本身不成立；
  2. eval_command 在 setup 之后必须失败——否则任务未修复就能通过，
     无法区分模型是否真的解决了问题。

Usage:
    python scripts/check_tasks.py
    python scripts/check_tasks.py --suite core
    python scripts/check_tasks.py --task fix01 --keep
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _path in (_ROOT / "src", _ROOT):
    if _path.is_dir():
        sys.path.insert(0, str(_path))


from breeze_eval.harness import (  # noqa: E402
    TaskSpec,
    _commit_setup,
    _create_worktree,
    _dirty_protected,
    _is_git_repo,
    _remove_worktree,
    _reset_worktree,
    _run_eval_command,
    _run_setup,
    load_tasks,
)

_OK = "OK"
_FAIL = "FAIL"
_SKIP = "SKIP"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight-check eval tasks.")
    parser.add_argument(
        "--tasks",
        type=Path,
        default=Path("tasks/quality.yaml"),
        help="task set YAML (default: tasks/quality.yaml)",
    )
    parser.add_argument("--suite", type=str, default=None, help="only check the named suite")
    parser.add_argument(
        "--task",
        action="append",
        dest="task_ids",
        help="only check the given task id, can be repeated",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="repository root (default: cwd)",
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=Path("fixtures"),
        help="directory for setup patch files (default: fixtures)",
    )
    parser.add_argument(
        "--eval-timeout",
        type=int,
        default=300,
        help="seconds allowed per eval_command (default: 300)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep worktrees on failure for inspection",
    )
    return parser.parse_args()


def _check_task(
    task: TaskSpec,
    repo_root: Path,
    fixtures_dir: Path,
    eval_timeout: int,
    keep_on_failure: bool,
) -> tuple[str, str]:
    """在独立 worktree 中预检单个任务。

    Returns:
        (status, message)，status 为 OK / FAIL / SKIP。
    """
    eval_command = task.eval_command or ""
    if eval_command.startswith("node") and shutil.which("node") is None:
        return _SKIP, "node not found on PATH"

    worktree = _create_worktree(repo_root, f"check-{task.id}")
    keep = False
    try:
        base_sha = _git_sha(worktree)
        ok, msg = _run_setup(worktree, task, fixtures_dir)
        if not ok:
            return _FAIL, f"setup failed: {msg}"

        _commit_setup(worktree, task.id)

        dirty = _dirty_protected(worktree, task.protect) if task.protect else []
        if dirty:
            keep = True
            return _FAIL, "protected paths dirty right after setup: " + "; ".join(dirty)

        if not eval_command:
            return _FAIL, "missing eval_command"

        passed, _ = _run_eval_command(worktree, eval_command, timeout=eval_timeout)
        if passed:
            keep = True
            return _FAIL, "eval_command passed before the agent ran"

        if not task.setup:
            return _OK, "fails before the agent runs"

        # 回到 setup 前的状态：若干净树也失败，说明 eval 可能本身有问题
        _reset_worktree(worktree, base_sha)
        passed, _ = _run_eval_command(worktree, eval_command, timeout=eval_timeout)
        if not passed:
            return _OK, "fails after setup; note: clean tree also fails"
        return _OK, "fails after setup, passes on clean tree"
    finally:
        if keep and keep_on_failure:
            print(f"    kept worktree: {worktree}")
        else:
            _remove_worktree(repo_root, worktree)


def _git_sha(worktree: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        shell=False,
    )
    return proc.stdout.strip()


def main() -> int:
    args = _parse_args()
    repo_root = args.repo.resolve()
    fixtures_dir = args.fixtures_dir.resolve()

    if not _is_git_repo(repo_root):
        print(f"error: not a git repository: {repo_root}", file=sys.stderr)
        return 2

    metadata, tasks = load_tasks(args.tasks.resolve())

    if args.suite is not None:
        suites = metadata.get("suites") or {}
        if args.suite not in suites:
            print(f"error: unknown suite: {args.suite}", file=sys.stderr)
            return 2
        suite_ids = set(suites[args.suite])
        tasks = [t for t in tasks if t.id in suite_ids]

    if args.task_ids:
        wanted = set(args.task_ids)
        unknown = wanted - {t.id for t in tasks}
        if unknown:
            print(f"error: unknown task id(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        tasks = [t for t in tasks if t.id in wanted]

    if not tasks:
        print("error: no tasks selected", file=sys.stderr)
        return 2

    print(f"checking {len(tasks)} task(s) in {repo_root}")
    failures = 0
    for i, task in enumerate(tasks, start=1):
        status, message = _check_task(task, repo_root, fixtures_dir, args.eval_timeout, args.keep)
        print(f"[{i}/{len(tasks)}] {task.id}: {status} - {message}")
        if status == _FAIL:
            failures += 1

    if failures:
        print(f"\n{failures} task(s) failed preflight")
        return 1
    print("\nall tasks passed preflight")
    return 0


if __name__ == "__main__":
    sys.exit(main())

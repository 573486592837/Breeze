"""批量评估。

从 YAML 任务集读取任务，逐个运行 Agent，收集指标并输出 JSON 报告。
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from breeze.agent import Agent
from breeze.config import settings
from breeze.llm import LLMClient
from breeze.trace import RunTrace


@dataclass
class SetupOp:
    """任务 setup 的单个操作。

    Attributes:
        kind: "patch" 或 "command"。
        value: patch 相对 fixtures 目录的路径，或要执行的命令。
    """

    kind: str
    value: str


@dataclass
class TaskSpec:
    """单个评估任务。

    短任务只需 id/type/question/success_criteria。
    长任务额外使用 setup 和 eval_command 启用 worktree 隔离与自动判定。
    """

    id: str
    type: str
    question: str
    success_criteria: str
    max_steps: int = 6
    notes: str = ""
    setup: list[SetupOp] = field(default_factory=list)
    eval_command: str | None = None


@dataclass
class TaskResult:
    """单个任务的运行结果。

    success 和 notes 由人工填写，脚本初始化为 None 和空字符串。

    当 repeat > 1 时，total_tokens、llm_calls、tool_calls、duration_ms
    是 N 次运行的中位数，原始采样存在 tokens_samples 中。
    """

    id: str
    type: str
    question: str
    success_criteria: str
    max_steps: int
    answer: str
    trace_path: str
    llm_calls: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    llm_latency_ms: float
    tool_latency_ms: float
    errors: int
    duration_ms: float
    success: bool | None = None
    notes: str = ""
    # repeat > 1 时填充
    tokens_samples: list[int] | None = None
    tokens_median: float | None = None
    tokens_std: float | None = None
    tokens_min: int | None = None
    tokens_max: int | None = None
    auto_graded: bool = False


def load_tasks(path: Path) -> tuple[dict[str, Any], list[TaskSpec]]:
    """从 YAML 加载任务集。

    Args:
        path: YAML 文件路径。

    Returns:
        (metadata, tasks)。metadata 包含 version、repo、model 等顶层字段。

    Raises:
        ValueError: 文件格式非法、字段缺失或 id 重复。
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "tasks" not in raw:
        raise ValueError(f"invalid task file: {path}")

    tasks_raw = raw["tasks"]
    if not isinstance(tasks_raw, list):
        raise ValueError(f"tasks must be a list: {path}")

    required = {"id", "type", "question", "success_criteria"}
    tasks: list[TaskSpec] = []
    seen_ids: set[str] = set()

    for item in tasks_raw:
        if not isinstance(item, dict):
            raise ValueError(f"task must be a dict: {item}")
        missing = required - item.keys()
        if missing:
            raise ValueError(f"task {item.get('id')} missing fields: {missing}")

        if item["id"] in seen_ids:
            raise ValueError(f"duplicate task id: {item['id']}")
        seen_ids.add(item["id"])

        setup = _parse_setup(item.get("setup", []))
        eval_command = item.get("eval_command")
        if eval_command is not None and not isinstance(eval_command, str):
            raise ValueError(f"task {item['id']} eval_command must be a string")

        tasks.append(
            TaskSpec(
                id=item["id"],
                type=item["type"],
                question=item["question"],
                success_criteria=item["success_criteria"],
                max_steps=int(item.get("max_steps", 6)),
                notes=item.get("notes", ""),
                setup=setup,
                eval_command=eval_command,
            )
        )

    metadata = {k: v for k, v in raw.items() if k != "tasks"}
    return metadata, tasks


def _is_git_repo(root: Path) -> bool:
    """检查 root 是否在 git 工作区内。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            shell=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def _create_worktree(repo_root: Path, name: str) -> Path:
    """在临时目录下创建 worktree。

    Args:
        repo_root: 原始仓库根目录。
        name: 用于临时目录名前缀。

    Returns:
        worktree 路径。

    Raises:
        RuntimeError: git worktree add 失败。
    """
    base = Path(tempfile.mkdtemp(prefix=f"breeze-eval-{name}-"))
    wt = base / "wt"
    proc = subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt), "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        shell=False,
    )
    if proc.returncode != 0:
        with contextlib.suppress(OSError):
            base.rmdir()
        raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()[:500]}")
    return wt


def _remove_worktree(repo_root: Path, worktree: Path) -> None:
    """移除 worktree 并清理临时父目录。失败时静默。"""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=str(repo_root),
        capture_output=True,
        timeout=30,
        shell=False,
    )
    with contextlib.suppress(OSError):
        worktree.parent.rmdir()


def _reset_worktree(worktree: Path, target_sha: str) -> None:
    """把 worktree 重置到指定的 commit。

    Args:
        worktree: worktree 路径。
        target_sha: 目标 commit SHA。
    """
    subprocess.run(
        ["git", "reset", "--hard", target_sha],
        cwd=str(worktree),
        capture_output=True,
        timeout=15,
        shell=False,
    )
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=str(worktree),
        capture_output=True,
        timeout=15,
        shell=False,
    )


def _commit_setup(worktree: Path, task_id: str) -> None:
    """提交 setup 产生的改动，使 HEAD 反映注入 bug 后的状态。

    这样 agent 的修复会产生 working tree 与 HEAD 的差异，
    git_diff 能正确显示。

    Args:
        worktree: worktree 路径。
        task_id: 任务 ID，用于 commit message。
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "breeze-eval",
        "GIT_AUTHOR_EMAIL": "eval@breeze.local",
        "GIT_COMMITTER_NAME": "breeze-eval",
        "GIT_COMMITTER_EMAIL": "eval@breeze.local",
    }
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(worktree),
        capture_output=True,
        timeout=10,
        shell=False,
    )
    subprocess.run(
        ["git", "commit", "-m", f"setup: {task_id}", "--allow-empty", "-q"],
        cwd=str(worktree),
        capture_output=True,
        timeout=10,
        shell=False,
        env=env,
    )


def _run_setup(
    worktree: Path,
    task: TaskSpec,
    fixtures_dir: Path,
) -> tuple[bool, str]:
    """执行任务的 setup 操作。

    Args:
        worktree: 隔离的工作副本路径。
        task: 任务定义。
        fixtures_dir: patch 文件的基准目录。

    Returns:
        (ok, message)。失败时 ok=False，message 含原因。
    """
    for op in task.setup:
        if op.kind == "patch":
            patch_path = fixtures_dir / op.value
            if not patch_path.is_file():
                return False, f"patch not found: {patch_path}"
            proc = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", str(patch_path)],
                cwd=str(worktree),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                shell=False,
            )
            if proc.returncode != 0:
                return False, f"git apply failed: {proc.stderr.strip()[:300]}"
        elif op.kind == "command":
            # setup command 由任务定义者编写，不来自 LLM，允许 shell=True
            if not op.value.strip():
                return False, "empty setup command"
            proc = subprocess.run(
                op.value,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                shell=True,
            )
            if proc.returncode != 0:
                return False, f"setup command failed: {proc.stderr.strip()[:300]}"
        else:
            return False, f"unknown setup op kind: {op.kind}"
    return True, ""


def _run_eval_command(worktree: Path, command: str, timeout: int = 120) -> bool:
    """执行 eval_command，退出码 0 视为成功。"""
    if not command.strip():
        return False
    try:
        proc = subprocess.run(
            command,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=True,
        )
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return proc.returncode == 0


def _make_failed_result(task: TaskSpec, error: str) -> TaskResult:
    """构造一个失败的 TaskResult。"""
    return TaskResult(
        id=task.id,
        type=task.type,
        question=task.question,
        success_criteria=task.success_criteria,
        max_steps=task.max_steps,
        answer=f"[eval] {error}",
        trace_path="",
        llm_calls=0,
        tool_calls=0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        llm_latency_ms=0.0,
        tool_latency_ms=0.0,
        errors=1,
        duration_ms=0.0,
        success=False,
        notes=error,
        auto_graded=bool(task.eval_command),
    )


def _run_task_with_worktree(
    task: TaskSpec,
    llm: LLMClient,
    trace_dir: Path,
    repo_root: Path,
    fixtures_dir: Path,
    repeat: int,
    log: Callable[[str], None],
) -> list[TaskResult]:
    """在 worktree 里跑 repeat 次任务。

    Args:
        task: 任务定义。
        llm: LLM 客户端。
        trace_dir: trace 目录。
        repo_root: 原始仓库根目录。
        fixtures_dir: patch 文件的基准目录。
        repeat: 运行次数。
        log: 进度回调。

    Returns:
        repeat 次运行的 TaskResult 列表。
    """
    worktree = _create_worktree(repo_root, task.id)
    results: list[TaskResult] = []
    original_root = settings.repo_root

    # 记录 worktree 创建时的原始 SHA，用于每次 repeat 前重置
    original_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        shell=False,
    ).stdout.strip()

    try:
        for j in range(repeat):
            _reset_worktree(worktree, original_sha)
            ok, msg = _run_setup(worktree, task, fixtures_dir)
            if not ok:
                log(f"  run {j + 1}/{repeat}: setup failed: {msg}")
                results.append(_make_failed_result(task, f"setup failed: {msg}"))
                continue

            fixtures_in_wt = worktree / "fixtures"
            if fixtures_in_wt.exists():
                shutil.rmtree(fixtures_in_wt, ignore_errors=True)

            # 提交 setup 改动，让 HEAD 反映 buggy 状态
            _commit_setup(worktree, task.id)

            settings.repo_root = worktree
            log(f"  run {j + 1}/{repeat}")
            try:
                r = run_task(task, llm, trace_dir)
            except Exception as exc:
                log(f"  run {j + 1}/{repeat}: agent error: {exc}")
                results.append(_make_failed_result(task, f"agent error: {exc}"))
                continue
            finally:
                settings.repo_root = original_root

            if task.eval_command:
                passed = _run_eval_command(worktree, task.eval_command)
                r.success = passed
                r.auto_graded = True
                log(f"  run {j + 1}/{repeat}: eval {'PASS' if passed else 'FAIL'}")

            results.append(r)
    finally:
        settings.repo_root = original_root
        _remove_worktree(repo_root, worktree)
    return results


def _parse_setup(raw: Any) -> list[SetupOp]:
    """解析 setup 列表。

    Args:
        raw: YAML 中 setup 字段的值。

    Returns:
        SetupOp 列表。

    Raises:
        ValueError: 格式不合法。
    """
    if not isinstance(raw, list):
        raise ValueError(f"setup must be a list, got {type(raw).__name__}")

    ops: list[SetupOp] = []
    for item in raw:
        if not isinstance(item, dict) or len(item) != 1:
            raise ValueError(f"each setup op must have exactly one key: {item}")
        if "patch" in item:
            ops.append(SetupOp("patch", str(item["patch"])))
        elif "command" in item:
            ops.append(SetupOp("command", str(item["command"])))
        else:
            raise ValueError(f"unknown setup op keys: {list(item.keys())}")
    return ops


def run_task(
    task: TaskSpec,
    llm: LLMClient,
    trace_dir: Path,
) -> TaskResult:
    """运行单个任务并收集指标。

    Args:
        task: 任务定义。
        llm: LLM 客户端。
        trace_dir: trace 文件输出目录。

    Returns:
        TaskResult。
    """
    started = time.perf_counter()

    with RunTrace(trace_dir=trace_dir) as trace:
        try:
            agent = Agent(llm=llm, trace=trace, max_steps=task.max_steps)
            answer = agent.run(task.question)

        except Exception as exc:
            answer = f"[eval] agent raised: {type(exc).__name__}: {exc}"
            trace.log_error(-1, answer)

        summary = trace.summary()

    duration_ms = (time.perf_counter() - started) * 1000

    return TaskResult(
        id=task.id,
        type=task.type,
        question=task.question,
        success_criteria=task.success_criteria,
        max_steps=task.max_steps,
        answer=answer,
        trace_path=summary["trace_path"],
        llm_calls=summary["llm_calls"],
        tool_calls=summary["tool_calls"],
        prompt_tokens=summary["prompt_tokens"],
        completion_tokens=summary["completion_tokens"],
        total_tokens=summary["total_tokens"],
        llm_latency_ms=summary["llm_latency_ms"],
        tool_latency_ms=summary["tool_latency_ms"],
        errors=summary["errors"],
        duration_ms=round(duration_ms, 2),
    )


def _aggregate_runs(runs: list[TaskResult]) -> TaskResult:
    """把同一任务的多次运行聚合成一条结果。

    中位数用于 total_tokens、llm_calls、tool_calls、duration_ms。
    原始 Token 采样存入 tokens_samples。
    answer 和 trace_path 取第一次运行的，作为代表。

    Args:
        runs: 同一任务的 N 次运行结果，N >= 1。

    Returns:
        聚合后的 TaskResult。
    """
    if not runs:
        raise ValueError("runs must not be empty")
    if len(runs) == 1:
        return runs[0]

    base = asdict(runs[0])
    tokens = [r.total_tokens for r in runs]

    base["total_tokens"] = int(statistics.median(tokens))
    base["llm_calls"] = int(statistics.median([r.llm_calls for r in runs]))
    base["tool_calls"] = int(statistics.median([r.tool_calls for r in runs]))
    base["prompt_tokens"] = int(statistics.median([r.prompt_tokens for r in runs]))
    base["completion_tokens"] = int(statistics.median([r.completion_tokens for r in runs]))
    base["duration_ms"] = round(statistics.median([r.duration_ms for r in runs]), 2)
    base["llm_latency_ms"] = round(statistics.median([r.llm_latency_ms for r in runs]), 2)
    base["tool_latency_ms"] = round(statistics.median([r.tool_latency_ms for r in runs]), 2)
    base["errors"] = int(statistics.median([r.errors for r in runs]))

    base["tokens_samples"] = tokens
    base["tokens_median"] = float(statistics.median(tokens))
    base["tokens_std"] = round(statistics.stdev(tokens), 2) if len(tokens) > 1 else 0.0
    base["tokens_min"] = min(tokens)
    base["tokens_max"] = max(tokens)

    passes = [r.success for r in runs if r.success is not None]
    if passes:
        base["success"] = sum(passes) > len(passes) / 2
        base["auto_graded"] = any(r.auto_graded for r in runs)

    return TaskResult(**base)


def summarize(results: list[TaskResult]) -> dict[str, Any]:
    """计算汇总指标。

    Args:
        results: 所有任务的运行结果。

    Returns:
        汇总字典。std_tokens 为跨任务 total_tokens 的标准差。
    """
    n = len(results)
    if n == 0:
        return {
            "total_tasks": 0,
            "success": 0,
            "failed": 0,
            "ungraded": 0,
            "total_tokens": 0,
            "avg_tokens": 0.0,
            "std_tokens": 0.0,
            "total_llm_calls": 0,
            "avg_llm_calls": 0.0,
            "total_tool_calls": 0,
            "avg_tool_calls": 0.0,
            "avg_duration_ms": 0.0,
            "errors": 0,
        }

    tokens = [r.total_tokens for r in results]

    return {
        "total_tasks": n,
        "success": sum(1 for r in results if r.success is True),
        "failed": sum(1 for r in results if r.success is False),
        "ungraded": sum(1 for r in results if r.success is None),
        "total_tokens": sum(tokens),
        "avg_tokens": round(sum(tokens) / n, 2),
        "std_tokens": (round(statistics.stdev(tokens), 2) if n > 1 else 0.0),
        "total_llm_calls": sum(r.llm_calls for r in results),
        "avg_llm_calls": round(sum(r.llm_calls for r in results) / n, 2),
        "total_tool_calls": sum(r.tool_calls for r in results),
        "avg_tool_calls": round(sum(r.tool_calls for r in results) / n, 2),
        "avg_duration_ms": round(sum(r.duration_ms for r in results) / n, 2),
        "errors": sum(r.errors for r in results),
    }


def build_report(
    metadata: dict[str, Any],
    results: list[TaskResult],
) -> dict[str, Any]:
    """组装报告字典。

    Args:
        metadata: 任务集顶层元数据。
        results: 所有任务结果。

    Returns:
        可直接序列化为 JSON 的报告字典。
    """
    return {
        "version": metadata.get("version", 1),
        "repo": metadata.get("repo", "breeze"),
        "model": metadata.get("model", "unknown"),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "summary": summarize(results),
        "results": [asdict(r) for r in results],
    }


def _merge_manual_grades(new_report: dict[str, Any], old_path: Path) -> None:
    """按 id 合并旧报告中的人工评分和备注。

    原地修改 new_report。

    Args:
        new_report: 新生成的报告。
        old_path: 旧报告路径。不存在或不可读时静默跳过。
    """
    if not old_path.exists():
        return

    try:
        old = json.loads(old_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    old_by_id = {r["id"]: r for r in old.get("results", []) if "id" in r}

    for r in new_report["results"]:
        if r.get("auto_graded"):
            continue
        old_r = old_by_id.get(r["id"])
        if not old_r:
            continue
        if old_r.get("success") is not None:
            r["success"] = old_r["success"]
        if old_r.get("notes"):
            r["notes"] = old_r["notes"]

    new_report["summary"]["success"] = sum(1 for r in new_report["results"] if r["success"] is True)
    new_report["summary"]["failed"] = sum(1 for r in new_report["results"] if r["success"] is False)
    new_report["summary"]["ungraded"] = sum(1 for r in new_report["results"] if r["success"] is None)


def run_eval(
    tasks_path: Path,
    output_path: Path,
    repo_root: Path,
    *,
    model: str | None = None,
    only_ids: set[str] | None = None,
    trace_dir: Path = Path("traces"),
    fixtures_dir: Path = Path("fixtures"),
    dry_run: bool = False,
    repeat: int = 1,
    llm_factory: Callable[[], LLMClient] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """运行整个评估流程。

    Args:
        tasks_path: 任务 YAML 路径。
        output_path: 输出 JSON 路径。
        repo_root: 仓库根目录。
        model: 覆盖模型名，None 表示用 settings.model。
        only_ids: 只运行指定 id；None 表示全部。
        trace_dir: trace 目录。
        fixtures_dir: patch 文件基准目录。
        dry_run: 只打印任务，不运行。
        repeat: 每个任务运行的次数，用于降低噪声。
        llm_factory: 返回 LLMClient 的工厂函数，便于测试注入。
        progress: 进度回调，接收一行字符串。

    Returns:
        报告字典。dry_run 模式下返回空字典。
    """
    tasks_path = tasks_path.resolve()
    output_path = output_path.resolve()
    repo_root = repo_root.resolve()
    trace_dir = trace_dir.resolve()
    fixtures_dir = fixtures_dir.resolve()

    metadata, tasks = load_tasks(tasks_path)

    if only_ids is not None:
        tasks = [t for t in tasks if t.id in only_ids]

    log = progress or (lambda _msg: None)

    if dry_run:
        log(f"dry-run: {len(tasks)} task(s), repeat={repeat}")
        for t in tasks:
            extras = []
            if t.setup:
                extras.append(f"setup={len(t.setup)}")
            if t.eval_command:
                extras.append("eval_command=yes")
            extra = f" [{', '.join(extras)}]" if extras else ""
            log(f"  [{t.id}] {t.type} | max_steps={t.max_steps} | {t.question}{extra}")
        return {}

    if repeat < 1:
        raise ValueError(f"repeat must be >= 1, got {repeat}")

    needs_worktree = any(t.setup or t.eval_command for t in tasks)
    if needs_worktree and not _is_git_repo(repo_root):
        raise ValueError("long-task evaluation requires a git repository at repo_root")

    original_root = settings.repo_root
    try:
        factory = llm_factory or (lambda: LLMClient(model=model))
        llm = factory()

        log("[config] effective settings:")
        log(f"  model={model or settings.model}")
        log(f"  context.enable={settings.context_enable}")
        log(f"  context.max_recent_tokens={settings.context_max_recent_tokens}")
        log(f"  agent.max_steps(default)={settings.agent_max_steps}")
        log(f"  agent.max_edit_attempts={settings.agent_max_edit_attempts}")

        results: list[TaskResult] = []
        for i, task in enumerate(tasks, start=1):
            log(f"[{i}/{len(tasks)}] {task.id} ({task.type}) repeat={repeat}")

            if task.setup or task.eval_command:
                task_runs = _run_task_with_worktree(task, llm, trace_dir, repo_root, fixtures_dir, repeat, log)
            else:
                settings.repo_root = repo_root
                task_runs = []
                for j in range(repeat):
                    log(f"  run {j + 1}/{repeat}")
                    task_runs.append(run_task(task, llm, trace_dir))

            result = _aggregate_runs(task_runs)
            results.append(result)

            preview = result.answer[:80].replace("\n", " ")
            if len(result.answer) > 80:
                preview += "..."
            log(f"  answer: {preview}")
            log(
                f"  tokens={result.total_tokens} "
                f"llm_calls={result.llm_calls} "
                f"tool_calls={result.tool_calls} "
                f"duration={result.duration_ms}ms"
            )
            if result.tokens_samples is not None:
                log(f"  samples={result.tokens_samples} std={result.tokens_std}")

        report = build_report(metadata, results)
        _merge_manual_grades(report, output_path)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(f"report written: {output_path}")
        return report
    finally:
        settings.repo_root = original_root

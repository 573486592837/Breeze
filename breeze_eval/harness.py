"""批量评估框架（开发用，不随 release 分发）。

从 YAML 任务集读取任务，逐个运行 Agent，收集指标并输出 JSON 报告。
入口脚本是 scripts/eval.py；本模块位于包外，因此 pip 安装 breeze 的用户
拿不到它，评估只在源码仓库里进行。
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from breeze.agent import Agent
from breeze.chat import run_chat
from breeze.config import settings
from breeze.llm import LLMClient
from breeze.tools.shell import decode_process_output
from breeze.trace import RunTrace

# eval/setup 命令里的 {python} 替换为当前解释器，避免 PATH 上的 python
# 与运行评估的 conda 环境不一致。
_PYTHON_CMD = subprocess.list2cmdline([sys.executable]) if os.name == "nt" else shlex.quote(sys.executable)


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
    protect 列出 Agent 不允许改动的路径（通常为 tests/ 或校验脚本），
    自动判定前由 harness 用 git status 检查。
    turns 非空时按 breeze chat 的多回合会话运行（Agent 跨回合存活，
    每回合独立 trace），question 只用于报告与 dry-run 描述。
    """

    id: str
    type: str
    question: str
    success_criteria: str
    max_steps: int = settings.agent_max_steps
    notes: str = ""
    setup: list[SetupOp] = field(default_factory=list)
    eval_command: str | None = None
    protect: list[str] = field(default_factory=list)
    turns: list[str] = field(default_factory=list)


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
    # 自动判定任务的按 run 统计（repeat=1 时同样填充）
    runs: int = 1
    passes: list[bool] | None = None
    pass_count: int | None = None
    success_rate: float | None = None
    pass_all: bool | None = None
    # chat 任务：每回合的 trace 与答案
    trace_paths: list[str] = field(default_factory=list)
    turn_answers: list[str] | None = None
    # repeat > 1 时逐次运行的可诊断明细
    run_details: list[dict[str, Any]] | None = None


def load_tasks(path: Path) -> tuple[dict[str, Any], list[TaskSpec]]:
    """从 YAML 加载任务集。

    Args:
        path: YAML 文件路径。

    Returns:
        (metadata, tasks)。metadata 包含 version、repo 等顶层字段。

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
        protect = _parse_protect(item.get("protect", []), item["id"])
        turns = _parse_turns(item.get("turns", []), item["id"])
        eval_command = item.get("eval_command")
        if eval_command is not None and not isinstance(eval_command, str):
            raise ValueError(f"task {item['id']} eval_command must be a string")

        tasks.append(
            TaskSpec(
                id=item["id"],
                type=item["type"],
                question=item["question"],
                success_criteria=item["success_criteria"],
                max_steps=int(item.get("max_steps", settings.agent_max_steps)),
                notes=item.get("notes", ""),
                setup=setup,
                eval_command=eval_command,
                protect=protect,
                turns=turns,
            )
        )

    metadata = {k: v for k, v in raw.items() if k != "tasks"}
    suites = metadata.get("suites")
    if suites is not None:
        if not isinstance(suites, dict) or not all(
            isinstance(name, str) and isinstance(ids, list) for name, ids in suites.items()
        ):
            raise ValueError("suites must be a mapping of name -> task id list")
        known = {t.id for t in tasks}
        for name, ids in suites.items():
            unknown = [str(i) for i in ids if i not in known]
            if unknown:
                raise ValueError(f"suite {name!r} references unknown task ids: {unknown}")
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
                _expand_command(op.value),
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


def _run_eval_command(worktree: Path, command: str, timeout: int = 300) -> tuple[bool, str]:
    """执行 eval_command，退出码 0 视为成功。

    Returns:
        (是否通过, 失败时的输出尾部)。尾部写进报告 notes，便于事后诊断
        而不必重跑一次完整的 LLM 评估。
    """
    if not command.strip():
        return False, "empty eval command"
    try:
        proc = subprocess.run(
            _expand_command(command),
            cwd=str(worktree),
            capture_output=True,
            timeout=timeout,
            shell=True,
        )
    except subprocess.TimeoutExpired:
        return False, f"eval timed out after {timeout}s"
    except OSError as exc:
        return False, f"eval failed to start: {exc}"
    if proc.returncode == 0:
        return True, ""
    stdout = decode_process_output(proc.stdout)
    stderr = decode_process_output(proc.stderr)
    output = "\n".join(part for part in (stdout, stderr) if part and part.strip())
    return False, _tail(output.strip(), 800) or f"exit code {proc.returncode}"


def _tail(text: str, limit: int) -> str:
    """保留文本尾部，优先保留失败摘要。"""
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _dirty_protected(worktree: Path, patterns: list[str]) -> list[str]:
    """返回受保护路径下被修改或新增的文件列表。

    用 git status 而不是 git diff：后者看不到 Agent 新建的测试文件。
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", *patterns],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        shell=False,
    )
    if proc.returncode != 0:
        return [f"git status failed: {proc.stderr.strip()[:200]}"]
    return [line for line in proc.stdout.splitlines() if line.strip()]


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
        auto_graded=bool(task.eval_command or task.protect),
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

            if task.protect:
                dirty = _dirty_protected(worktree, task.protect)
                if dirty:
                    r.success = False
                    r.auto_graded = True
                    r.notes = "protected paths modified: " + "; ".join(line.strip() for line in dirty)
                    log(f"  run {j + 1}/{repeat}: protected paths modified, eval skipped")
                    results.append(r)
                    continue

            if task.eval_command:
                passed, detail = _run_eval_command(worktree, task.eval_command)
                r.success = passed
                r.auto_graded = True
                if passed:
                    log(f"  run {j + 1}/{repeat}: eval PASS")
                else:
                    r.notes = "eval failed: " + detail
                    last_line = detail.splitlines()[-1] if detail else "unknown error"
                    log(f"  run {j + 1}/{repeat}: eval FAIL: {last_line[:160]}")

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


def _parse_turns(raw: Any, task_id: str) -> list[str]:
    """解析 chat 任务的回合列表，只接受非空字符串。"""
    if not isinstance(raw, list) or not all(isinstance(t, str) and t.strip() for t in raw):
        raise ValueError(f"task {task_id} turns must be a list of non-empty strings")
    return [t.strip() for t in raw]


def _parse_protect(raw: Any, task_id: str) -> list[str]:
    """解析 protect 路径列表，只接受仓库内相对路径。"""
    if not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise ValueError(f"task {task_id} protect must be a list of relative paths")
    patterns: list[str] = []
    for pattern in raw:
        path = Path(pattern)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"task {task_id} protect path must be relative: {pattern}")
        patterns.append(pattern)
    return patterns


def _expand_command(command: str) -> str:
    """把 {python} 替换为当前解释器。"""
    return command.replace("{python}", _PYTHON_CMD)


def _run_chat_task(
    task: TaskSpec,
    llm: LLMClient,
    trace_dir: Path,
    started: float,
) -> TaskResult:
    """按 breeze chat 的会话循环跑多回合任务。

    复用 run_chat 而不是手动循环 Agent.run，保证评估路径与真实 chat
    一致：Agent 跨回合存活、每回合独立 trace、上一回合结论经 TurnLedger
    注入下一回合。回合输入由 turns 预先写死，耗尽后触发 EOF 退出。

    Args:
        task: 含 turns 的任务定义。
        llm: LLM 客户端。
        trace_dir: trace 文件输出目录。
        started: 任务开始的 perf_counter 时间。

    Returns:
        TaskResult，指标为全部回合之和。
    """
    pending = list(task.turns)
    traces: list[RunTrace] = []
    summaries: list[dict[str, Any]] = []
    answers: list[str] = []

    def input_fn(_prompt: str) -> str:
        if pending:
            return pending.pop(0)
        raise EOFError

    def trace_factory() -> RunTrace:
        trace = RunTrace(trace_dir=trace_dir)
        traces.append(trace)
        return trace

    def agent_factory(trace: RunTrace) -> Agent:
        return Agent(llm=llm, trace=trace, max_steps=task.max_steps)

    error = ""
    try:
        run_chat(
            agent_factory=agent_factory,
            trace_factory=trace_factory,
            input_fn=input_fn,
            print_fn=lambda _msg: None,
            answer_fn=answers.append,
            summary_fn=lambda summary, _turn: summaries.append(summary),
        )
    except Exception as exc:
        error = f"chat raised: {type(exc).__name__}: {exc}"

    answer = answers[-1] if answers else f"[eval] {error or 'no answer produced'}"
    if error:
        answer = f"[eval] {error}"

    totals = {
        "llm_calls": sum(int(s.get("llm_calls", 0)) for s in summaries),
        "tool_calls": sum(int(s.get("tool_calls", 0)) for s in summaries),
        "prompt_tokens": sum(int(s.get("prompt_tokens", 0)) for s in summaries),
        "completion_tokens": sum(int(s.get("completion_tokens", 0)) for s in summaries),
        "total_tokens": sum(int(s.get("total_tokens", 0)) for s in summaries),
        "llm_latency_ms": sum(float(s.get("llm_latency_ms", 0.0)) for s in summaries),
        "tool_latency_ms": sum(float(s.get("tool_latency_ms", 0.0)) for s in summaries),
        "errors": sum(int(s.get("errors", 0)) for s in summaries) + (1 if error else 0),
    }

    return TaskResult(
        id=task.id,
        type=task.type,
        question=task.question,
        success_criteria=task.success_criteria,
        max_steps=task.max_steps,
        answer=answer,
        trace_path=str(traces[-1].path) if traces else "",
        trace_paths=[str(t.path) for t in traces],
        turn_answers=answers or None,
        llm_calls=totals["llm_calls"],
        tool_calls=totals["tool_calls"],
        prompt_tokens=totals["prompt_tokens"],
        completion_tokens=totals["completion_tokens"],
        total_tokens=totals["total_tokens"],
        llm_latency_ms=round(totals["llm_latency_ms"], 2),
        tool_latency_ms=round(totals["tool_latency_ms"], 2),
        errors=totals["errors"],
        duration_ms=round((time.perf_counter() - started) * 1000, 2),
        notes=error,
    )


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

    if task.turns:
        return _run_chat_task(task, llm, trace_dir, started)

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


def _run_detail(run: TaskResult) -> dict[str, Any]:
    """把一次运行压缩成可诊断明细，写进聚合结果。"""
    trace_paths = run.trace_paths or ([run.trace_path] if run.trace_path else [])
    detail: dict[str, Any] = {
        "success": run.success,
        "trace_paths": trace_paths,
        "total_tokens": run.total_tokens,
        "llm_calls": run.llm_calls,
        "tool_calls": run.tool_calls,
        "duration_ms": run.duration_ms,
        "notes": run.notes,
        "answer_preview": run.answer[:300],
    }
    if run.turn_answers:
        detail["turn_answers"] = [answer[:200] for answer in run.turn_answers]
    return detail


def _apply_pass_metrics(result: TaskResult, runs: list[TaskResult]) -> TaskResult:
    """填充按 run 的通过统计。

    只看自动判定的 run：setup 失败和 protected 路径被改都记为失败。
    未自动判定的任务保持 None，由人工评分。

    Args:
        result: 聚合后的结果，原地修改。
        runs: 该任务的 N 次运行结果。

    Returns:
        result。
    """
    result.runs = len(runs)
    result.run_details = [_run_detail(r) for r in runs]
    graded = [r for r in runs if r.auto_graded and r.success is not None]
    if not graded:
        return result

    passes = [bool(r.success) for r in graded]
    result.passes = passes
    result.pass_count = sum(passes)
    result.success_rate = round(sum(passes) / len(passes), 4)
    result.pass_all = all(passes)
    return result


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
        return _apply_pass_metrics(runs[0], runs)

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

    graded = [r.success for r in runs if r.success is not None]
    if graded:
        base["success"] = sum(graded) > len(graded) / 2
        base["auto_graded"] = any(r.auto_graded for r in runs)

    return _apply_pass_metrics(TaskResult(**base), runs)


def summarize(results: list[TaskResult]) -> dict[str, Any]:
    """计算汇总指标。

    Args:
        results: 所有任务的运行结果。

    Returns:
        汇总字典。std_tokens 为跨任务 total_tokens 的标准差。
        graded_tasks/total_runs/passes/success_rate/all_pass_tasks 只统计自动判定任务，
        success_rate 按 run 计（每 run 一次通过与否），比按任务计更抗噪声。
    """
    n = len(results)
    if n == 0:
        return {
            "total_tasks": 0,
            "success": 0,
            "failed": 0,
            "ungraded": 0,
            "graded_tasks": 0,
            "total_runs": 0,
            "passes": 0,
            "success_rate": 0.0,
            "all_pass_tasks": 0,
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
    graded = [r for r in results if r.passes is not None]
    total_runs = sum(len(r.passes or []) for r in graded)
    passed_runs = sum(r.pass_count or 0 for r in graded)

    return {
        "total_tasks": n,
        "success": sum(1 for r in results if r.success is True),
        "failed": sum(1 for r in results if r.success is False),
        "ungraded": sum(1 for r in results if r.success is None),
        "graded_tasks": len(graded),
        "total_runs": total_runs,
        "passes": passed_runs,
        "success_rate": round(passed_runs / total_runs, 4) if total_runs else 0.0,
        "all_pass_tasks": sum(1 for r in graded if r.pass_all),
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


def _effective_config(llm: Any) -> dict[str, Any]:
    """收集影响评估结果的运行参数，写进报告便于跨档比较。"""
    return {
        "model": llm.model,
        "llm.timeout": settings.llm_timeout,
        "llm.connect_timeout": settings.llm_connect_timeout,
        "llm.max_retries": settings.llm_max_retries,
        "context.enable": settings.context_enable,
        "context.profile": settings.context_profile,
        "context.intra_turn_budget": settings.context_intra_turn_budget,
        "context.hard_budget": settings.context_hard_budget,
        "context.state_max_entries": settings.context_state_max_entries,
        "chat.answer_preview": settings.chat_answer_preview,
        "chat.max_records": settings.chat_max_records,
        "chat.max_chars": settings.chat_max_chars,
        "agent.max_steps": settings.agent_max_steps,
        "agent.max_edit_failures": settings.agent_max_edit_failures,
    }


def build_report(
    metadata: dict[str, Any],
    results: list[TaskResult],
    model: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装报告字典。

    Args:
        metadata: 任务集顶层元数据。
        results: 所有任务结果。
        model: 实际使用的模型名。
        config: 实际生效的配置摘要；None 时为空字典。

    Returns:
        可直接序列化为 JSON 的报告字典。
    """
    return {
        "version": metadata.get("version", 1),
        "repo": metadata.get("repo", "breeze"),
        "model": model,
        "effective_config": config or {},
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
    suite: str | None = None,
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
        suite: 只运行 YAML 中 suites 定义的某一档；None 表示全部。
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

    if suite is not None:
        suites = metadata.get("suites") or {}
        if suite not in suites:
            available = ", ".join(sorted(suites)) or "none"
            raise ValueError(f"unknown suite: {suite} (available: {available})")
        suite_ids = set(suites[suite])
        tasks = [t for t in tasks if t.id in suite_ids]

    if only_ids is not None:
        tasks = [t for t in tasks if t.id in only_ids]

    log = progress or (lambda _msg: None)

    if dry_run:
        suite_note = f", suite={suite}" if suite else ""
        log(f"dry-run: {len(tasks)} task(s), repeat={repeat}{suite_note}")
        for t in tasks:
            extras = []
            if t.setup:
                extras.append(f"setup={len(t.setup)}")
            if t.eval_command:
                extras.append("eval_command=yes")
            if t.protect:
                extras.append("protect=" + ",".join(t.protect))
            if t.turns:
                extras.append(f"turns={len(t.turns)}")
            extra = f" [{', '.join(extras)}]" if extras else ""
            log(f"  [{t.id}] {t.type} | max_steps={t.max_steps} | {t.question}{extra}")
        return {}

    if repeat < 1:
        raise ValueError(f"repeat must be >= 1, got {repeat}")

    needs_worktree = any(t.setup or t.eval_command or t.protect for t in tasks)
    if needs_worktree and not _is_git_repo(repo_root):
        raise ValueError("long-task evaluation requires a git repository at repo_root")

    original_root = settings.repo_root
    try:
        factory = llm_factory or (lambda: LLMClient(model=model))
        llm = factory()

        config = _effective_config(llm)
        log("[config] effective settings:")
        for key, value in config.items():
            log(f"  {key}={value}")

        results: list[TaskResult] = []
        for i, task in enumerate(tasks, start=1):
            log(f"[{i}/{len(tasks)}] {task.id} ({task.type}) repeat={repeat} max_steps={task.max_steps}")

            if task.setup or task.eval_command or task.protect:
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
            if result.passes is not None:
                log(f"  pass={result.pass_count}/{len(result.passes)} success_rate={result.success_rate}")
            if result.notes:
                log(f"  notes: {result.notes}")

        report = build_report(metadata, results, llm.model, config=config)
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

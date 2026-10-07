"""批量评估。

从 YAML 任务集读取任务，逐个运行 Agent，收集指标并输出 JSON 报告。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from breeze.agent import Agent
from breeze.config import settings
from breeze.llm import LLMClient
from breeze.trace import RunTrace


@dataclass
class TaskSpec:
    """单个评估任务。"""

    id: str
    type: str
    question: str
    success_criteria: str
    max_steps: int = 6
    notes: str = ""


@dataclass
class TaskResult:
    """单个任务的运行结果。

    success 和 notes 由人工填写，脚本初始化为 None 和空字符串。
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

        tasks.append(
            TaskSpec(
                id=item["id"],
                type=item["type"],
                question=item["question"],
                success_criteria=item["success_criteria"],
                max_steps=int(item.get("max_steps", 6)),
                notes=item.get("notes", ""),
            )
        )

    metadata = {k: v for k, v in raw.items() if k != "tasks"}
    return metadata, tasks


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


def summarize(results: list[TaskResult]) -> dict[str, Any]:
    """计算汇总指标。

    Args:
        results: 所有任务的运行结果。

    Returns:
        汇总字典。
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
            "total_llm_calls": 0,
            "avg_llm_calls": 0.0,
            "total_tool_calls": 0,
            "avg_tool_calls": 0.0,
            "avg_duration_ms": 0.0,
            "errors": 0,
        }

    return {
        "total_tasks": n,
        "success": sum(1 for r in results if r.success is True),
        "failed": sum(1 for r in results if r.success is False),
        "ungraded": sum(1 for r in results if r.success is None),
        "total_tokens": sum(r.total_tokens for r in results),
        "avg_tokens": round(sum(r.total_tokens for r in results) / n, 2),
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
    dry_run: bool = False,
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
        dry_run: 只打印任务，不运行。
        llm_factory: 返回 LLMClient 的工厂函数，便于测试注入。
        progress: 进度回调，接收一行字符串。

    Returns:
        报告字典。dry_run 模式下返回空字典。
    """
    metadata, tasks = load_tasks(tasks_path)

    if only_ids is not None:
        tasks = [t for t in tasks if t.id in only_ids]

    log = progress or (lambda _msg: None)

    if dry_run:
        log(f"dry-run: {len(tasks)} task(s)")
        for t in tasks:
            log(f" [{t.id}] {t.type} | max_steps={t.max_steps} | {t.question}")
        return {}

    original_root = settings.repo_root
    settings.repo_root = repo_root
    try:
        factory = llm_factory or (lambda: LLMClient(model=model))
        llm = factory()

        results: list[TaskResult] = []
        for i, task in enumerate(tasks, start=1):
            log(f"[{i}/{len(tasks)}] {task.id} ({task.type})")
            result = run_task(task, llm, trace_dir)
            results.append(result)

            preview = result.answer[:80].replace("\n", " ")
            if len(result.answer) > 80:
                preview += "..."
            log(f"answer: {preview}")
            log(
                f"tokens={result.total_tokens}\n"
                f"llm_calls={result.llm_calls}\n"
                f"tool_calls={result.tool_calls}\n"
                f"duration={result.duration_ms}ms"
            )

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

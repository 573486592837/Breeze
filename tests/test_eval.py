"""eval 模块的单元测试。"""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from breeze.config import settings
from breeze.eval import (
    TaskResult,
    TaskSpec,
    build_report,
    load_tasks,
    run_eval,
    run_task,
    summarize,
)
from breeze.llm import LLMResponse


class FakeLLM:
    """返回固定答案的假 LLM。"""

    def __init__(self, *args, **kwargs) -> None:
        self.model = "fake-model"
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, temperature=0.0) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools})
        return LLMResponse(
            content="fake answer",
            tool_calls=[],
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )


@pytest.fixture
def task_yaml(tmp_path: Path) -> Path:
    """构造一个最小任务集。"""
    data = {
        "version": 1,
        "repo": "test-repo",
        "model": "test-model",
        "tasks": [
            {
                "id": "t01",
                "type": "explain",
                "question": "What is 1 + 1?",
                "success_criteria": "Answer mentions 2.",
                "max_steps": 3,
            },
            {
                "id": "t02",
                "type": "locate",
                "question": "Where is the definition?",
                "success_criteria": "Answer points to a file.",
                "max_steps": 4,
            },
        ],
    }
    path = tmp_path / "tasks.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """构造一个最小仓库。"""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    return root


# ─── load_tasks ───


def test_load_tasks(task_yaml: Path) -> None:
    """正常加载任务集。"""
    metadata, tasks = load_tasks(task_yaml)
    assert metadata["repo"] == "test-repo"
    assert metadata["model"] == "test-model"
    assert len(tasks) == 2
    assert tasks[0].id == "t01"
    assert tasks[0].max_steps == 3


def test_load_tasks_rejects_missing_fields(tmp_path: Path) -> None:
    """字段缺失应抛 ValueError。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"tasks": [{"id": "x"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing fields"):
        load_tasks(bad)


def test_load_tasks_rejects_duplicate_ids(tmp_path: Path) -> None:
    """id 重复应抛 ValueError。"""
    bad = tmp_path / "dup.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "x",
                        "type": "explain",
                        "question": "q",
                        "success_criteria": "c",
                    },
                    {
                        "id": "x",
                        "type": "explain",
                        "question": "q",
                        "success_criteria": "c",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_tasks(bad)


def test_load_tasks_rejects_non_dict(tmp_path: Path) -> None:
    """顶层不是 dict 应抛 ValueError。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("just a string", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid task file"):
        load_tasks(bad)


# ─── run_task ───


def test_run_task_with_fake_llm(
    fake_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """用假 LLM 运行单个任务。"""
    monkeypatch.setattr(settings, "repo_root", fake_repo)

    task = TaskSpec(
        id="t01",
        type="explain",
        question="test question",
        success_criteria="mentions answer",
        max_steps=3,
    )
    llm = FakeLLM()
    result = run_task(task, llm, tmp_path / "traces")

    assert result.id == "t01"
    assert result.answer == "fake answer"
    assert result.llm_calls == 1
    assert result.tool_calls == 0
    assert result.total_tokens == 15
    assert result.success is None
    assert result.notes == ""
    assert Path(result.trace_path).exists()


# ─── summarize ───


def _make_result(success: bool | None, tokens: int) -> TaskResult:
    return TaskResult(
        id="x",
        type="explain",
        question="q",
        success_criteria="c",
        max_steps=3,
        answer="a",
        trace_path="p",
        llm_calls=1,
        tool_calls=0,
        prompt_tokens=tokens,
        completion_tokens=0,
        total_tokens=tokens,
        llm_latency_ms=0.0,
        tool_latency_ms=0.0,
        errors=0,
        duration_ms=100.0,
        success=success,
    )


def test_summarize_empty() -> None:
    """空结果集的汇总。"""
    s = summarize([])
    assert s["total_tasks"] == 0
    assert s["success"] == 0
    assert s["ungraded"] == 0


def test_summarize_counts() -> None:
    """汇总应正确累计。"""
    results = [
        _make_result(True, 100),
        _make_result(False, 200),
        _make_result(None, 300),
    ]
    s = summarize(results)
    assert s["total_tasks"] == 3
    assert s["success"] == 1
    assert s["failed"] == 1
    assert s["ungraded"] == 1
    assert s["total_tokens"] == 600
    assert s["avg_tokens"] == 200.0


# ─── build_report ───


def test_build_report_structure() -> None:
    """报告应包含所有顶层字段。"""
    metadata = {"version": 1, "repo": "test", "model": "m"}
    report = build_report(metadata, [])
    assert report["version"] == 1
    assert report["repo"] == "test"
    assert report["model"] == "m"
    assert "generated_at" in report
    assert "summary" in report
    assert "results" in report


# ─── run_eval ───


def test_run_eval_dry_run(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """dry-run 不运行、不写文件。"""
    messages: list[str] = []
    report = run_eval(
        tasks_path=task_yaml,
        output_path=tmp_path / "out.json",
        repo_root=fake_repo,
        dry_run=True,
        progress=messages.append,
    )
    assert report == {}
    assert any("dry-run" in m for m in messages)
    assert any("t01" in m for m in messages)
    assert not (tmp_path / "out.json").exists()


def test_run_eval_writes_json(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """正常运行应写出 JSON。"""
    output = tmp_path / "out.json"
    report = run_eval(
        tasks_path=task_yaml,
        output_path=output,
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        llm_factory=FakeLLM,
    )
    assert output.exists()

    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["repo"] == "test-repo"
    assert len(loaded["results"]) == 2
    assert loaded["summary"]["total_tasks"] == 2
    assert loaded["summary"]["ungraded"] == 2

    assert len(report["results"]) == 2


def test_run_eval_only_ids(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """only_ids 应只运行指定任务。"""
    report = run_eval(
        tasks_path=task_yaml,
        output_path=tmp_path / "out.json",
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        only_ids={"t01"},
        llm_factory=FakeLLM,
    )
    assert len(report["results"]) == 1
    assert report["results"][0]["id"] == "t01"


def test_run_eval_merges_manual_grades(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """人工评分应在重新运行时保留。"""
    output = tmp_path / "out.json"

    run_eval(
        tasks_path=task_yaml,
        output_path=output,
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        llm_factory=FakeLLM,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    data["results"][0]["success"] = True
    data["results"][0]["notes"] = "looks good"
    output.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    run_eval(
        tasks_path=task_yaml,
        output_path=output,
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        llm_factory=FakeLLM,
    )

    reloaded = json.loads(output.read_text(encoding="utf-8"))
    assert reloaded["results"][0]["success"] is True
    assert reloaded["results"][0]["notes"] == "looks good"
    assert reloaded["summary"]["success"] == 1
    assert reloaded["summary"]["ungraded"] == 1


def test_run_eval_restores_repo_root(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """运行后 settings.repo_root 应恢复。"""
    original = settings.repo_root
    run_eval(
        tasks_path=task_yaml,
        output_path=tmp_path / "out.json",
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        llm_factory=FakeLLM,
    )
    assert settings.repo_root == original


# ─── repeat 与聚合 ───


class VariableFakeLLM:
    """每次调用返回递增的 Token 数。"""

    def __init__(self, *args, **kwargs) -> None:
        self.model = "variable"
        self.call_count = 0

    def chat(self, messages, tools=None, temperature=0.0) -> LLMResponse:
        self.call_count += 1
        tokens = 10 * self.call_count
        return LLMResponse(
            content=f"answer {self.call_count}",
            tool_calls=[],
            prompt_tokens=tokens,
            completion_tokens=5,
            total_tokens=tokens + 5,
        )


def test_run_eval_repeat_aggregates_tokens(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """repeat=3 时 total_tokens 为中位数，samples 保留原始值。"""
    output = tmp_path / "out.json"
    run_eval(
        tasks_path=task_yaml,
        output_path=output,
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        repeat=3,
        llm_factory=VariableFakeLLM,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    first = data["results"][0]

    # VariableFakeLLM 每次 chat 返回 tokens=15, 25, 35
    # 每个任务只调用一次 chat（无 tool_calls）
    # 第一个任务取前 3 个
    assert first["tokens_samples"] == [15, 25, 35]
    assert first["tokens_median"] == 25.0
    assert first["tokens_std"] == 10.0
    assert first["tokens_min"] == 15
    assert first["tokens_max"] == 35

    # 第二个任务取后 3 个
    second = data["results"][1]
    assert second["tokens_samples"] == [45, 55, 65]
    assert second["tokens_median"] == 55.0


def test_run_eval_repeat_one_no_aggregation(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """repeat=1 时不填聚合字段。"""
    output = tmp_path / "out.json"
    run_eval(
        tasks_path=task_yaml,
        output_path=output,
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        repeat=1,
        llm_factory=FakeLLM,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    first = data["results"][0]

    assert first["tokens_samples"] is None
    assert first["tokens_median"] is None
    assert first["tokens_std"] is None
    assert first["total_tokens"] == 15  # FakeLLM 固定返回 15


def test_run_eval_repeat_zero_raises(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """repeat=0 应抛 ValueError。"""
    with pytest.raises(ValueError, match="repeat must be"):
        run_eval(
            tasks_path=task_yaml,
            output_path=tmp_path / "out.json",
            repo_root=fake_repo,
            trace_dir=tmp_path / "traces",
            repeat=0,
            llm_factory=FakeLLM,
        )


def test_run_eval_repeat_dry_run(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """dry-run 显示 repeat 值。"""
    messages: list[str] = []
    run_eval(
        tasks_path=task_yaml,
        output_path=tmp_path / "out.json",
        repo_root=fake_repo,
        dry_run=True,
        repeat=3,
        progress=messages.append,
    )
    assert any("repeat=3" in m for m in messages)


def test_summarize_includes_std() -> None:
    """summary 应包含 std_tokens。"""
    results = [
        _make_result(True, 100),
        _make_result(True, 200),
        _make_result(True, 300),
    ]
    s = summarize(results)
    assert "std_tokens" in s
    # statistics.stdev([100, 200, 300]) == 100.0
    assert s["std_tokens"] == 100.0


def test_summarize_single_task_std_zero() -> None:
    """只有一个任务时 std_tokens 为 0。"""
    s = summarize([_make_result(True, 100)])
    assert s["std_tokens"] == 0.0


def test_summarize_empty_has_std() -> None:
    """空结果集也应包含 std_tokens。"""
    s = summarize([])
    assert s["std_tokens"] == 0.0

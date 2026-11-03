"""breeze_eval.harness（评估框架，包外开发模块）的单元测试。"""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from breeze.config import settings
from breeze.llm import LLMResponse
from breeze_eval.harness import (
    TaskResult,
    TaskSpec,
    build_report,
    load_tasks,
    run_eval,
    run_task,
    summarize,
)


class FakeLLM:
    """返回固定答案的假 LLM。"""

    def __init__(self, *args, **kwargs) -> None:
        self.model = "fake-model"
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, temperature=0.0, tool_choice=None) -> LLMResponse:
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


@pytest.fixture
def git_repo(fake_repo: Path) -> Path:
    """在 fake_repo 基础上初始化 git 仓库并提交。"""
    subprocess.run(["git", "init", "-q"], cwd=fake_repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=fake_repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=fake_repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=fake_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=fake_repo, check=True)
    return fake_repo


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    d = tmp_path / "fixtures"
    d.mkdir()
    return d


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


def test_load_tasks_parses_chat_turns(tmp_path: Path) -> None:
    """turns 应按顺序保留，供多回合会话使用。"""
    path = tmp_path / "chat.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "chat01",
                        "type": "chat",
                        "question": "two turns",
                        "success_criteria": "c",
                        "turns": ["  first  ", "second"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    _, tasks = load_tasks(path)

    assert tasks[0].turns == ["first", "second"]


def test_load_tasks_rejects_invalid_turns(tmp_path: Path) -> None:
    """turns 含空字符串应抛 ValueError。"""
    bad = tmp_path / "bad-turns.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "chat01",
                        "type": "chat",
                        "question": "q",
                        "success_criteria": "c",
                        "turns": ["ok", "  "],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="turns"):
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


def test_run_task_chat_turns_keep_agent_alive(
    fake_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """turns 非空时走 run_chat：每回合一条 trace，上一回合结论进入归档。"""
    monkeypatch.setattr(settings, "repo_root", fake_repo)

    task = TaskSpec(
        id="chat01",
        type="chat",
        question="two turns",
        success_criteria="both answered",
        max_steps=3,
        turns=["first question", "second question"],
    )
    llm = FakeLLM()
    result = run_task(task, llm, tmp_path / "traces")

    assert result.answer == "fake answer"
    assert result.turn_answers == ["fake answer", "fake answer"]
    assert result.llm_calls == 2
    assert result.total_tokens == 30
    assert len(result.trace_paths) == 2
    assert all(Path(path).exists() for path in result.trace_paths)

    second_messages = llm.calls[1]["messages"]
    archive = "\n".join(str(m.get("content")) for m in second_messages)
    assert "Completed turns" in archive
    assert "first question" in archive


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
    """报告应包含所有顶层字段，model 取实际使用的模型。"""
    metadata = {"version": 1, "repo": "test", "model": "yaml-model"}
    report = build_report(metadata, [], "actual-model")
    assert report["version"] == 1
    assert report["repo"] == "test"
    assert report["model"] == "actual-model"
    assert "generated_at" in report
    assert "summary" in report
    assert "results" in report
    assert report["effective_config"] == {}


def test_build_report_carries_effective_config() -> None:
    """配置摘要写入报告，跨档评估时可以核对实际生效的参数。"""
    report = build_report({}, [], "m", config={"chat.max_chars": 3000})

    assert report["effective_config"] == {"chat.max_chars": 3000}


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
    assert loaded["model"] == "fake-model"
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


def test_run_eval_logs_effective_config(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """正常运行应输出实际生效的配置摘要。"""
    messages: list[str] = []
    run_eval(
        tasks_path=task_yaml,
        output_path=tmp_path / "out.json",
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        llm_factory=FakeLLM,
        progress=messages.append,
    )
    joined = "\n".join(messages)
    assert "model=" in joined
    assert "context.enable=" in joined
    assert "context.intra_turn_budget=" in joined
    assert "chat.max_chars=" in joined


def test_run_eval_report_records_effective_config(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """写出的 JSON 应携带实际生效的配置摘要。"""
    output = tmp_path / "out.json"
    run_eval(
        tasks_path=task_yaml,
        output_path=output,
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        llm_factory=FakeLLM,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["effective_config"]["model"] == "fake-model"
    assert data["effective_config"]["chat.max_chars"] == settings.chat_max_chars


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

    def chat(self, messages, tools=None, temperature=0.0, tool_choice=None) -> LLMResponse:
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


def test_load_tasks_max_steps_falls_back_to_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YAML 未写 max_steps 的任务应回退到 settings.agent_max_steps。"""
    monkeypatch.setattr(settings, "agent_max_steps", 7)
    path = tmp_path / "tasks.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "x01",
                        "type": "explain",
                        "question": "q",
                        "success_criteria": "c",
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    _, tasks = load_tasks(path)
    assert tasks[0].max_steps == 7


def test_run_eval_logs_task_max_steps(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """任务起始行应显示该任务实际生效的 max_steps。"""
    messages: list[str] = []
    run_eval(
        tasks_path=task_yaml,
        output_path=tmp_path / "out.json",
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        repeat=1,
        llm_factory=FakeLLM,
        progress=messages.append,
    )
    assert any("t01" in m and "max_steps=3" in m for m in messages)
    assert any("t02" in m and "max_steps=4" in m for m in messages)


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


# ─── setup / eval_command / worktree ───


def test_load_tasks_parses_setup_and_eval(
    tmp_path: Path,
) -> None:
    """load_tasks 应解析 setup 和 eval_command。"""
    yaml_path = tmp_path / "tasks.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "fix01",
                        "type": "fix",
                        "question": "fix it",
                        "success_criteria": "tests pass",
                        "setup": [{"patch": "fix01.patch"}],
                        "eval_command": "pytest -q",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    _, tasks = load_tasks(yaml_path)
    t = tasks[0]
    assert len(t.setup) == 1
    assert t.setup[0].kind == "patch"
    assert t.setup[0].value == "fix01.patch"
    assert t.eval_command == "pytest -q"


def test_load_tasks_rejects_invalid_setup(tmp_path: Path) -> None:
    """setup 格式不合法应抛错。"""
    yaml_path = tmp_path / "tasks.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "x",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                        "setup": "not a list",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="setup must be a list"):
        load_tasks(yaml_path)


def test_run_eval_requires_git_repo_for_long_tasks(fake_repo: Path, tmp_path: Path) -> None:
    """有 setup 的任务要求 repo_root 是 git 仓库。"""
    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "fix01",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                        "setup": [{"command": "echo hi"}],
                        "eval_command": "true",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="git repository"):
        run_eval(
            tasks_path=tasks_path,
            output_path=tmp_path / "out.json",
            repo_root=fake_repo,
            trace_dir=tmp_path / "traces",
            fixtures_dir=tmp_path / "fixtures",
            llm_factory=FakeLLM,
        )


def test_run_eval_with_worktree_end_to_end(
    git_repo: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """端到端：worktree 隔离 + setup command + eval_command。"""
    import sys

    (git_repo / "target.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add target"], cwd=git_repo, check=True)

    # 用 Python 替代 Unix 命令，跨平台
    setup_cmd = f"\"{sys.executable}\" -c \"from pathlib import Path; Path('target.txt').write_text('broken')\""
    eval_cmd = f"\"{sys.executable}\" -c \"import sys; sys.exit(0 if 'broken' in open('target.txt').read() else 1)\""

    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "fix01",
                        "type": "fix",
                        "question": "make eval pass",
                        "success_criteria": "eval passes",
                        "setup": [{"command": setup_cmd}],
                        "eval_command": eval_cmd,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "out.json"
    run_eval(
        tasks_path=tasks_path,
        output_path=output,
        repo_root=git_repo,
        trace_dir=tmp_path / "traces",
        fixtures_dir=fixtures_dir,
        llm_factory=FakeLLM,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    r = data["results"][0]

    assert r["auto_graded"] is True
    assert r["success"] is True
    assert r["run_details"][0]["trace_paths"]


def test_run_eval_failure_notes_include_output_tail(
    git_repo: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """eval 失败时 notes 记录输出尾部，供报告自证失败原因。"""
    import sys

    eval_cmd = f'"{sys.executable}" -c "import sys; print(\'BOOM_MARKER\'); sys.exit(1)"'
    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "fix01",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                        "eval_command": eval_cmd,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "out.json"
    run_eval(
        tasks_path=tasks_path,
        output_path=output,
        repo_root=git_repo,
        trace_dir=tmp_path / "traces",
        fixtures_dir=fixtures_dir,
        llm_factory=FakeLLM,
        repeat=2,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    r = data["results"][0]

    assert r["success"] is False
    assert "eval failed:" in r["notes"]
    assert "BOOM_MARKER" in r["notes"]
    assert len(r["run_details"]) == 2
    assert all(detail["success"] is False for detail in r["run_details"])


def test_run_eval_setup_failure_records_failed_result(
    git_repo: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """setup 失败时该 run 记录为失败，但不中断评估。"""
    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "fix01",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                        "setup": [{"patch": "nonexistent.patch"}],
                        "eval_command": "true",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "out.json"
    run_eval(
        tasks_path=tasks_path,
        output_path=output,
        repo_root=git_repo,
        trace_dir=tmp_path / "traces",
        fixtures_dir=fixtures_dir,
        llm_factory=FakeLLM,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    r = data["results"][0]
    assert r["success"] is False
    assert "setup failed" in r["notes"]


def test_run_eval_repeat_with_eval_majority(
    git_repo: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """多次运行按多数原则聚合 success。"""
    import sys

    # 用 Python -c "pass" 作为恒成功的 eval_command
    eval_cmd = f'"{sys.executable}" -c "import sys; sys.exit(0)"'

    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "fix01",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                        "eval_command": eval_cmd,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "out.json"
    run_eval(
        tasks_path=tasks_path,
        output_path=output,
        repo_root=git_repo,
        trace_dir=tmp_path / "traces",
        fixtures_dir=fixtures_dir,
        repeat=3,
        llm_factory=FakeLLM,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    r = data["results"][0]

    assert r["success"] is True
    assert r["auto_graded"] is True


def test_run_eval_short_tasks_unaffected_by_worktree(
    task_yaml: Path,
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """没有 setup / eval_command 的短任务不需要 git 仓库。"""
    output = tmp_path / "out.json"
    run_eval(
        tasks_path=task_yaml,
        output_path=output,
        repo_root=fake_repo,
        trace_dir=tmp_path / "traces",
        llm_factory=FakeLLM,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["summary"]["total_tasks"] == 2
    # 短任务不设 auto_graded
    assert data["results"][0]["auto_graded"] is False


def test_load_tasks_parses_protect(tmp_path: Path) -> None:
    """protect 应解析为路径列表，缺省为空。"""
    yaml_path = tmp_path / "tasks.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "fix01",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                        "protect": ["tests/", "samples/node-app/src/"],
                        "eval_command": "{python} -m pytest -q",
                    },
                    {
                        "id": "e01",
                        "type": "explain",
                        "question": "q",
                        "success_criteria": "c",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    _, tasks = load_tasks(yaml_path)
    assert tasks[0].protect == ["tests/", "samples/node-app/src/"]
    assert tasks[1].protect == []


def test_load_tasks_rejects_protect_outside_repo(tmp_path: Path) -> None:
    """protect 只接受仓库内相对路径。"""
    yaml_path = tmp_path / "tasks.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "x",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                        "protect": ["../outside"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be relative"):
        load_tasks(yaml_path)


def test_load_tasks_validates_suite_ids(tmp_path: Path) -> None:
    """suites 引用不存在的任务 id 应报错。"""
    yaml_path = tmp_path / "tasks.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "suites": {"core": ["nope"]},
                "tasks": [
                    {
                        "id": "x",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown task ids"):
        load_tasks(yaml_path)


def test_run_eval_unknown_suite_raises(task_yaml: Path, fake_repo: Path, tmp_path: Path) -> None:
    """suite 名称不存在时应给出可用列表。"""
    with pytest.raises(ValueError, match="unknown suite"):
        run_eval(
            tasks_path=task_yaml,
            output_path=tmp_path / "out.json",
            repo_root=fake_repo,
            dry_run=True,
            suite="core",
        )


def test_run_eval_suite_filters_tasks(
    fake_repo: Path,
    tmp_path: Path,
) -> None:
    """suite 应只保留档位内任务。"""
    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(
        yaml.safe_dump(
            {
                "suites": {"core": ["b"], "full": ["a", "b"]},
                "tasks": [
                    {"id": "a", "type": "explain", "question": "qa", "success_criteria": "c"},
                    {"id": "b", "type": "explain", "question": "qb", "success_criteria": "c"},
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    messages: list[str] = []
    run_eval(
        tasks_path=tasks_path,
        output_path=tmp_path / "out.json",
        repo_root=fake_repo,
        dry_run=True,
        suite="core",
        progress=messages.append,
    )

    joined = "\n".join(messages)
    assert "suite=core" in joined
    assert "qb" in joined
    assert "qa" not in joined


def test_expand_command_replaces_python() -> None:
    """{python} 应替换为当前解释器，避免 PATH 上的 python 与评估环境不一致。"""
    import sys

    from breeze_eval.harness import _expand_command

    expanded = _expand_command("{python} -m pytest -q")
    assert sys.executable in expanded
    assert "{python}" not in expanded


def test_run_eval_fails_when_protected_paths_change(
    git_repo: Path,
    fixtures_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent 新增受保护文件时应判失败并跳过 eval_command。"""
    from breeze_eval import harness

    def fake_run_task(task, llm, trace_dir):
        tests = settings.repo_root / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_hack.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        return _make_result(None, 0)

    monkeypatch.setattr(harness, "run_task", fake_run_task)

    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "fix01",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                        "protect": ["tests/"],
                        "eval_command": '{python} -c "import sys; sys.exit(0)"',
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "out.json"
    run_eval(
        tasks_path=tasks_path,
        output_path=output,
        repo_root=git_repo,
        trace_dir=tmp_path / "traces",
        fixtures_dir=fixtures_dir,
        llm_factory=FakeLLM,
    )

    r = json.loads(output.read_text(encoding="utf-8"))["results"][0]
    assert r["success"] is False
    assert r["auto_graded"] is True
    assert "protected paths modified" in r["notes"]
    assert "tests/" in r["notes"]
    assert r["passes"] == [False]


def test_run_eval_pass_metrics_single_run(
    git_repo: Path,
    fixtures_dir: Path,
    tmp_path: Path,
) -> None:
    """repeat=1 也填充按 run 的通过统计。"""
    import sys

    eval_cmd = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
    tasks_path = tmp_path / "tasks.yaml"
    tasks_path.write_text(
        yaml.safe_dump(
            {
                "tasks": [
                    {
                        "id": "fix01",
                        "type": "fix",
                        "question": "q",
                        "success_criteria": "c",
                        "eval_command": eval_cmd,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "out.json"
    run_eval(
        tasks_path=tasks_path,
        output_path=output,
        repo_root=git_repo,
        trace_dir=tmp_path / "traces",
        fixtures_dir=fixtures_dir,
        llm_factory=FakeLLM,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    r = data["results"][0]
    assert r["runs"] == 1
    assert r["passes"] == [True]
    assert r["pass_count"] == 1
    assert r["success_rate"] == 1.0
    assert r["pass_all"] is True
    assert data["summary"]["graded_tasks"] == 1
    assert data["summary"]["total_runs"] == 1
    assert data["summary"]["success_rate"] == 1.0
    assert data["summary"]["all_pass_tasks"] == 1


def test_summarize_reports_run_level_pass_rate() -> None:
    """summary 按 run 统计通过率与全通过任务数。"""
    from dataclasses import replace

    graded = replace(_make_result(True, 100), passes=[True, False, True], pass_count=2, success_rate=0.6667)
    graded.pass_all = False
    ungraded = _make_result(None, 50)

    s = summarize([graded, ungraded])
    assert s["graded_tasks"] == 1
    assert s["total_runs"] == 3
    assert s["passes"] == 2
    assert s["success_rate"] == 0.6667
    assert s["all_pass_tasks"] == 0


def test_quality_task_file_is_consistent() -> None:
    """仓库内的固定评估集应能加载，且每个档位任务都有自动判定与保护路径。"""
    path = Path(__file__).resolve().parents[1] / "tasks" / "quality.yaml"
    metadata, tasks = load_tasks(path)

    assert len(tasks) == 17
    assert set(metadata["suites"]) == {"core", "chat", "full"}

    by_id = {t.id: t for t in tasks}
    for suite, ids in metadata["suites"].items():
        for task_id in ids:
            task = by_id[task_id]
            assert task.eval_command, f"{suite}/{task_id} missing eval_command"
            assert task.protect, f"{suite}/{task_id} missing protect"

    for task in tasks:
        if task.eval_command and "pytest" in task.eval_command:
            assert "{python}" in task.eval_command, f"{task.id} should pin interpreter"

    assert len(by_id["chat01"].turns) == 2
    assert len(by_id["chat02"].turns) == 2
    assert set(metadata["suites"]["chat"]) == {"chat01", "chat02"}


def test_merge_manual_grades_skips_auto_graded(
    tmp_path: Path,
) -> None:
    """auto_graded 的任务跳过人工评分合并。"""
    from breeze_eval.harness import _merge_manual_grades

    old_path = tmp_path / "old.json"
    old_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "id": "fix01",
                        "success": False,
                        "notes": "human said fail",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    new_report = {
        "results": [
            {
                "id": "fix01",
                "success": True,
                "notes": "",
                "auto_graded": True,
            }
        ],
        "summary": {},
    }

    _merge_manual_grades(new_report, old_path)

    # auto_graded 跳过合并，保持 True
    assert new_report["results"][0]["success"] is True

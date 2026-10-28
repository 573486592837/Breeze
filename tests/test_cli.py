"""CLI 的单元测试。"""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from breeze import cli as cli_module
from breeze.cli import app
from breeze.llm import LLMResponse

runner = CliRunner()


class FakeLLMClient:
    """假 LLM，避免真实网络调用。"""

    def __init__(self, *args, **kwargs) -> None:
        self.model = kwargs.get("model") or "fake-model"

    def chat(self, messages, tools=None, temperature=0.0):
        return LLMResponse(
            content="fake answer",
            tool_calls=[],
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )


@pytest.fixture(autouse=True)
def patch_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """全局替换 LLMClient，避免真实 API 调用。"""
    monkeypatch.setattr(cli_module, "LLMClient", FakeLLMClient)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """构造一个空仓库目录。"""
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    return tmp_path


# ─── 正常路径 ───


def test_ask_basic(repo: Path, tmp_path: Path) -> None:
    """breeze ask 应返回 Agent 答案。"""
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert result.exit_code == 0
    assert "fake answer" in result.stdout


def test_ask_quiet_mode(repo: Path, tmp_path: Path) -> None:
    """--quiet 只输出答案，不显示 trace 摘要。"""
    result = runner.invoke(
        app,
        [
            "ask",
            "hello",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "traces"),
            "--quiet",
        ],
    )
    assert result.exit_code == 0
    assert "fake answer" in result.stdout
    assert "Trace Summary" not in result.stdout


def test_ask_shows_summary(repo: Path, tmp_path: Path) -> None:
    """默认应显示 trace 摘要。"""
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert result.exit_code == 0
    assert "Trace Summary" in result.stdout
    assert "total_tokens" in result.stdout


def test_ask_writes_trace_file(repo: Path, tmp_path: Path) -> None:
    """运行后应在 trace_dir 下生成 JSONL 文件。"""
    trace_dir = tmp_path / "traces"
    runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(trace_dir)],
    )
    files = list(trace_dir.glob("*.jsonl"))
    assert len(files) == 1


def test_ask_custom_max_steps(repo: Path, tmp_path: Path) -> None:
    """--max-steps 应传递给 Agent。"""
    captured = {}

    original_agent = cli_module.Agent

    class CapturingAgent:
        def __init__(self, llm, trace, max_steps):
            captured["max_steps"] = max_steps
            self._inner = original_agent(llm=llm, trace=trace, max_steps=max_steps)

        def run(self, question):
            return self._inner.run(question)

    cli_module.Agent = CapturingAgent
    try:
        result = runner.invoke(
            app,
            [
                "ask",
                "hello",
                "--repo",
                str(repo),
                "--trace-dir",
                str(tmp_path / "traces"),
                "--max-steps",
                "3",
            ],
        )
    finally:
        cli_module.Agent = original_agent

    assert result.exit_code == 0
    assert captured["max_steps"] == 3


def test_ask_custom_model(repo: Path, tmp_path: Path) -> None:
    """--model 应覆盖默认模型。"""
    result = runner.invoke(
        app,
        [
            "ask",
            "hello",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "traces"),
            "--model",
            "custom-model",
        ],
    )
    assert result.exit_code == 0
    assert "custom-model" in result.stdout


# ─── 错误路径 ───


def test_ask_invalid_repo(tmp_path: Path) -> None:
    """仓库路径无效时退出码为 1。"""
    bad = tmp_path / "not-exist"
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(bad)],
    )
    assert result.exit_code == 1
    assert "invalid" in result.stdout or "invalid" in (result.stderr or "")


def test_ask_missing_api_key(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """API key 缺失时退出码为 1。"""

    class FailingLLM:
        def __init__(self, *args, **kwargs):
            raise ValueError("SILICONFLOW_API_KEY 未设置")

    monkeypatch.setattr(cli_module, "LLMClient", FailingLLM)
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert result.exit_code == 1


def test_ask_runtime_error(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行时异常退出码为 2。"""

    class ExplodingAgent:
        def __init__(self, **kwargs):
            pass

        def run(self, question):
            raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "Agent", ExplodingAgent)
    result = runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert result.exit_code == 2


def test_ask_restores_repo_root(repo: Path, tmp_path: Path) -> None:
    """运行后 settings.repo_root 应恢复为原值。"""
    original = cli_module.settings.repo_root
    runner.invoke(
        app,
        ["ask", "hello", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
    )
    assert cli_module.settings.repo_root == original


def test_ask_uses_settings_max_steps_when_not_specified(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不传 --max-steps 时 Agent 使用 settings 的值。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "agent_max_steps", 4)

    captured: dict[str, Any] = {}
    original = cli_module.Agent

    class CapturingAgent:
        def __init__(self, llm, trace, max_steps=None, **kwargs):
            self._inner = original(llm=llm, trace=trace, max_steps=max_steps, **kwargs)
            captured["resolved_max_steps"] = self._inner.max_steps

        def run(self, question):
            return self._inner.run(question)

    monkeypatch.setattr(cli_module, "Agent", CapturingAgent)

    result = runner.invoke(
        app,
        [
            "ask",
            "hello",
            "--repo",
            str(repo),
            "--trace-dir",
            str(tmp_path / "traces"),
        ],
    )

    assert result.exit_code == 0
    assert captured["resolved_max_steps"] == 4


# ─── chat 命令 ───


def test_chat_runs_turns_until_exit(repo: Path, tmp_path: Path) -> None:
    """chat 逐回合执行并退出。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="hello\nagain\n/exit\n",
    )
    assert result.exit_code == 0
    assert result.stdout.count("fake answer") == 2
    assert "bye." in result.stdout


def test_chat_writes_one_trace_per_turn(repo: Path, tmp_path: Path) -> None:
    """每回合生成独立 trace 文件。"""
    trace_dir = tmp_path / "traces"
    runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(trace_dir)],
        input="hello\nagain\n/exit\n",
    )
    assert len(list(trace_dir.glob("*.jsonl"))) == 2


def test_chat_help_command(repo: Path, tmp_path: Path) -> None:
    """/help 输出命令说明且不进入回合。"""
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/help\n/exit\n",
    )
    assert result.exit_code == 0
    assert "/new" in result.stdout
    assert "fake answer" not in result.stdout


def test_chat_invalid_repo(tmp_path: Path) -> None:
    """仓库路径无效时退出码为 1。"""
    result = runner.invoke(app, ["chat", "--repo", str(tmp_path / "not-exist")], input="/exit\n")
    assert result.exit_code == 1


def test_chat_runtime_error(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回合内异常退出码为 2。"""

    class ExplodingAgent:
        def __init__(self, **kwargs):
            pass

        def reset(self) -> None:
            pass

        def run(self, question):
            raise RuntimeError("boom")

    monkeypatch.setattr(cli_module, "Agent", ExplodingAgent)
    result = runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="hello\n",
    )
    assert result.exit_code == 2


def test_chat_restores_repo_root(repo: Path, tmp_path: Path) -> None:
    """退出后 settings.repo_root 应恢复为原值。"""
    original = cli_module.settings.repo_root
    runner.invoke(
        app,
        ["chat", "--repo", str(repo), "--trace-dir", str(tmp_path / "traces")],
        input="/exit\n",
    )
    assert cli_module.settings.repo_root == original

"""breeze doctor 的单元测试。"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from breeze import doctor
from breeze.capabilities import Capabilities
from breeze.cli import app
from breeze.llm import LLMResponse

runner = CliRunner()


def _caps(**overrides: bool) -> Capabilities:
    values = {"git": True, "git_repo": True, "node": False, "rg": True, "python_project": True}
    values.update(overrides)
    return Capabilities(**values)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """隔离工作目录、配置和外部能力探测。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor, "find_dotenv_path", lambda *_: None)
    monkeypatch.setattr(doctor, "detect", lambda *_: _caps())
    monkeypatch.setattr(doctor.settings, "repo_root", tmp_path)
    monkeypatch.setattr(doctor.settings, "api_key", "sk-test-secret")
    monkeypatch.setattr(doctor.settings, "api_key_env", "BREEZE_TEST_KEY")
    monkeypatch.setattr(doctor.settings, "model", "openai/test-model")
    monkeypatch.setattr(doctor.settings, "api_base", "https://example.com/v1")
    monkeypatch.setattr(doctor.settings, "sessions_dir", tmp_path / "sessions")
    monkeypatch.setattr(doctor.settings, "trace_dir", "traces")


def _by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {check.name: check for check in checks}


def test_all_checks_pass() -> None:
    checks = doctor.run_checks()

    assert all(check.status != "fail" for check in checks)
    assert _by_name(checks)["API Key"].detail == "BREEZE_TEST_KEY 已设置"


def test_secret_value_is_not_in_checks() -> None:
    checks = doctor.run_checks()

    assert "sk-test-secret" not in repr(checks)


def test_missing_api_key_fails() -> None:
    doctor.settings.api_key = ""

    key = _by_name(doctor.run_checks())["API Key"]

    assert key.status == "fail"
    assert "BREEZE_TEST_KEY" in key.detail
    assert "不要" in key.hint


def test_siliconflow_model_without_prefix_warns() -> None:
    doctor.settings.api_base = "https://api.siliconflow.cn/v1"
    doctor.settings.model = "deepseek-ai/DeepSeek-V3.2"

    model = _by_name(doctor.run_checks())["模型"]

    assert model.status == "warn"
    assert "openai/" in model.hint


def test_non_git_directory_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "detect", lambda *_: _caps(git=False, git_repo=False))

    git = _by_name(doctor.run_checks())["Git"]

    assert git.status == "warn"
    assert "git_restore" in git.hint


def test_missing_ripgrep_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "detect", lambda *_: _caps(rg=False))

    rg = _by_name(doctor.run_checks())["ripgrep"]

    assert rg.status == "warn"
    assert "Python" in rg.hint


def test_dotenv_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    monkeypatch.setattr(doctor, "find_dotenv_path", lambda *_: dotenv)

    checks = _by_name(doctor.run_checks())
    env_check = checks[".env"]

    assert env_check.status == "ok"
    assert str(dotenv) in env_check.detail


def test_ping_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeLLM:
        def __init__(self, **kwargs) -> None:
            self.model = "fake-model"

        def chat(self, messages, **kwargs) -> LLMResponse:
            return LLMResponse(content="ok", total_tokens=3)

    monkeypatch.setattr(doctor, "LLMClient", FakeLLM)

    ping = _by_name(doctor.run_checks(ping=True))["模型连通性"]

    assert ping.status == "ok"
    assert "fake-model" in ping.detail


def test_ping_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingLLM:
        def __init__(self, **kwargs) -> None:
            pass

        def chat(self, messages, **kwargs) -> LLMResponse:
            raise RuntimeError(f"connection refused for {doctor.settings.api_key}")

    monkeypatch.setattr(doctor, "LLMClient", FailingLLM)

    ping = _by_name(doctor.run_checks(ping=True))["模型连通性"]

    assert ping.status == "fail"
    assert "connection refused" in ping.detail
    assert "sk-test-secret" not in ping.detail


def test_cli_exit_code_follows_failures() -> None:
    doctor.settings.api_key = ""

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL" in result.stdout
    assert "API Key" in result.stdout


def test_cli_does_not_print_secret() -> None:
    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "sk-test-secret" not in result.stdout
    assert "BREEZE_TEST_KEY" in result.stdout

"""版本更新检查的单元测试。"""

from pathlib import Path
from urllib.error import HTTPError

import pytest
from typer.testing import CliRunner

from breeze import cli, update_check
from breeze.update_check import UpdateResult

runner = CliRunner()


def test_version_key_accepts_v_prefix() -> None:
    assert update_check._version_key("v1.7.0") == (1, 7, 0)
    assert update_check._version_key("1.7.0") == (1, 7, 0)
    assert update_check._version_key("not-a-version") is None


def test_tag_fallback_when_release_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_json(url: str, _timeout: float):
        if url == update_check.LATEST_URL:
            raise HTTPError(url, 404, "Not Found", None, None)
        return [{"name": "v1.6.1"}, {"name": "v9.9.9"}, {"name": "nightly"}]

    monkeypatch.setattr(update_check, "_fetch_json", fake_json)

    latest, url = update_check._fetch_latest(1.0)

    assert latest == "9.9.9"
    assert url.endswith("/releases/tag/v9.9.9")


def test_newer_release_is_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(update_check, "_fetch_latest", lambda *_: ("v9.9.9", "https://example.com"))

    result = update_check.check_for_update(force=True, cache_path=tmp_path / "cache.json")

    assert result.available
    assert result.latest == "9.9.9"
    assert result.url == "https://example.com"
    assert not result.from_cache


def test_current_release_is_not_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(update_check, "_fetch_latest", lambda *_: (f"v{update_check.__version__}", ""))

    result = update_check.check_for_update(force=True, cache_path=tmp_path / "cache.json")

    assert not result.available
    assert not result.error


def test_cache_hit_skips_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(update_check, "_fetch_latest", lambda *_: ("v9.9.9", "https://example.com"))
    update_check.check_for_update(force=True, cache_path=cache, now=1_000)

    def fail(_timeout: float) -> tuple[str, str]:
        raise RuntimeError("should not fetch")

    monkeypatch.setattr(update_check, "_fetch_latest", fail)
    result = update_check.check_for_update(cache_path=cache, now=1_000 + 3600)

    assert result.from_cache
    assert result.latest == "9.9.9"
    assert not result.error


def test_stale_cache_is_used_when_network_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = tmp_path / "cache.json"
    monkeypatch.setattr(update_check, "_fetch_latest", lambda *_: ("v9.9.9", "https://example.com"))
    update_check.check_for_update(force=True, cache_path=cache, now=1_000)

    def fail(_timeout: float) -> tuple[str, str]:
        raise RuntimeError("offline")

    monkeypatch.setattr(update_check, "_fetch_latest", fail)
    result = update_check.check_for_update(cache_path=cache, now=1_000 + update_check.CACHE_TTL + 1)

    assert result.from_cache
    assert result.latest == "9.9.9"
    assert "offline" in result.error


def test_failure_without_cache_reports_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(_timeout: float) -> tuple[str, str]:
        raise RuntimeError("offline")

    monkeypatch.setattr(update_check, "_fetch_latest", fail)
    result = update_check.check_for_update(cache_path=tmp_path / "missing.json")

    assert not result.latest
    assert "offline" in result.error


def test_update_command_reports_newer_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "check_for_update",
        lambda **_: UpdateResult(latest="9.9.9", url="https://example.com/release"),
    )

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0
    assert "发现新版本" in result.stdout
    assert "https://example.com/release" in result.stdout


def test_update_command_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "check_for_update",
        lambda **_: UpdateResult(error="offline"),
    )

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 1
    assert "检查更新失败" in result.stdout or "检查更新失败" in result.stderr


def test_update_command_reports_current_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "check_for_update",
        lambda **_: UpdateResult(latest=update_check.__version__),
    )

    result = runner.invoke(cli.app, ["update"])

    assert result.exit_code == 0
    assert "当前已是最新版本" in result.stdout


def test_startup_notice_prints_new_version(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeConsole:
        is_terminal = True

        def __init__(self) -> None:
            self.lines: list[str] = []

        def print(self, text: str, **kwargs) -> None:
            self.lines.append(text)

    fake = FakeConsole()
    monkeypatch.delenv("BREEZE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(cli, "console", fake)
    monkeypatch.setattr(cli, "check_for_update", lambda **_: UpdateResult(latest="9.9.9"))

    cli._maybe_notify_update()

    assert "发现新版本" in fake.lines[0]


def test_startup_notice_respects_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setenv("BREEZE_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(cli, "check_for_update", lambda **_: called.append(True))

    cli._maybe_notify_update()

    assert called == []

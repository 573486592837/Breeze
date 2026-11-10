"""capabilities 模块的单元测试。"""

import subprocess
from pathlib import Path

import pytest

from breeze import capabilities as capabilities_module
from breeze.capabilities import Capabilities, detect, is_git_repo, is_python_project


def test_is_python_project_by_marker(tmp_path: Path) -> None:
    """存在 pyproject.toml 即视为 Python 项目。"""
    assert not is_python_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert is_python_project(tmp_path)


def test_is_python_project_by_tests_dir(tmp_path: Path) -> None:
    """tests/ 下有 .py 文件才算，其他文件不算。"""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    assert not is_python_project(tmp_path)

    (tests_dir / "test_a.py").write_text("", encoding="utf-8")
    assert is_python_project(tmp_path)


def test_is_python_project_by_dependency_file(tmp_path: Path) -> None:
    """requirements.txt / Pipfile 等依赖文件也是 Python 项目标记。"""
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")

    assert is_python_project(tmp_path)


def test_is_python_project_by_python_files(tmp_path: Path) -> None:
    """根目录或 src/ 下的 .py 文件足以判定为 Python 项目。"""
    (tmp_path / "main.py").write_text("", encoding="utf-8")
    assert is_python_project(tmp_path)

    (tmp_path / "main.py").unlink()
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "cli.py").write_text("", encoding="utf-8")
    assert is_python_project(tmp_path)


def test_is_python_project_by_venv(tmp_path: Path) -> None:
    """已有虚拟环境解释器时也视为 Python 项目。"""
    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")

    assert is_python_project(tmp_path)


def test_is_git_repo_uses_rev_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git rev-parse 返回 true 时判定为工作区。"""
    monkeypatch.setattr(capabilities_module.shutil, "which", lambda _name: "/fake/git")

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["git", "rev-parse"]
        return subprocess.CompletedProcess(cmd, 0, stdout="true\n", stderr="")

    monkeypatch.setattr(capabilities_module.subprocess, "run", fake_run)

    assert is_git_repo(tmp_path)


def test_is_git_repo_false_without_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 git 可执行文件时直接返回 False。"""
    monkeypatch.setattr(capabilities_module.shutil, "which", lambda _name: None)

    assert not is_git_repo(tmp_path)


def test_detect_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """detect 汇总命令可用性、仓库类型与项目类型。"""
    monkeypatch.setattr(
        capabilities_module.shutil,
        "which",
        lambda name: f"/fake/{name}" if name in {"git", "node"} else None,
    )
    monkeypatch.setattr(capabilities_module, "is_git_repo", lambda _root: False)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")

    caps = detect(tmp_path)

    assert caps.git
    assert caps.node
    assert not caps.rg
    assert not caps.git_repo
    assert not caps.git_tools
    assert caps.python_project


def test_git_tools_require_executable_and_repo() -> None:
    """git 工具需要可执行且位于工作区内。"""
    assert Capabilities(True, True, True, True, True).git_tools
    assert not Capabilities(True, False, True, True, True).git_tools
    assert not Capabilities(False, False, True, True, True).git_tools

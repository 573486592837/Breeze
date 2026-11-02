"""运行环境能力探测。

Agent 启动时探测一次，用于两件事：
- 只把当前环境可用的工具下发给模型，避免对 git_diff / run_tests 的无谓尝试；
- 让系统提示只描述真实可用的能力。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Capabilities:
    """当前仓库与外部命令的可用性。"""

    git: bool
    git_repo: bool
    node: bool
    rg: bool
    python_project: bool

    @property
    def git_tools(self) -> bool:
        """git_diff / git_restore 是否可用。"""
        return self.git and self.git_repo


def is_git_repo(root: Path) -> bool:
    """判断目录是否位于 git 工作区内。

    Args:
        root: 待检测目录。

    Returns:
        可执行 git 且 ``rev-parse --is-inside-work-tree`` 为 true 时返回 True。
    """
    if shutil.which("git") is None:
        return False
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


def is_python_project(root: Path) -> bool:
    """判断目录是否是 Python 项目。

    Args:
        root: 仓库根目录。

    Returns:
        存在 pyproject.toml、setup.py、pytest.ini、tox.ini、setup.cfg，
        或 tests/ 下存在 .py 文件时返回 True。
    """
    markers = ("pyproject.toml", "setup.py", "pytest.ini", "tox.ini", "setup.cfg")
    if any((root / name).is_file() for name in markers):
        return True
    tests_dir = root / "tests"
    return tests_dir.is_dir() and any(tests_dir.glob("*.py"))


def detect(root: Path) -> Capabilities:
    """探测仓库能力。

    Args:
        root: 仓库根目录。

    Returns:
        当前环境的能力快照。
    """
    return Capabilities(
        git=shutil.which("git") is not None,
        git_repo=is_git_repo(root),
        node=shutil.which("node") is not None,
        rg=shutil.which("rg") is not None,
        python_project=is_python_project(root),
    )

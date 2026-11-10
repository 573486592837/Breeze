"""edit 模块的单元测试。"""

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from breeze.config import settings
from breeze.tools import registry


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 settings.repo_root 指向临时目录。"""
    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)
    return root


@pytest.fixture
def python_repo(repo: Path) -> Path:
    """在 repo 基础上添加 Python 项目标记，供 run_tests 测试使用。"""
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    return repo


def _make_file(root: Path, name: str, content: str) -> Path:
    """在临时仓库中创建文件。"""
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="")
    return p


# ─── 基础功能 ───


def test_apply_patch_basic(repo: Path) -> None:
    """简单替换应成功。"""
    _make_file(repo, "foo.py", "def bar():\n    return 1\n")

    out = registry.execute(
        "apply_patch",
        {"path": "foo.py", "old": "    return 1", "new": "    return 2"},
    )

    assert out.startswith("Patched")
    assert (repo / "foo.py").read_text(encoding="utf-8") == ("def bar():\n    return 2\n")


def test_apply_patch_multiline(repo: Path) -> None:
    """多行替换。"""
    original = "def add(a, b):\n    x = a + b\n    return x\n"
    _make_file(repo, "foo.py", original)

    out = registry.execute(
        "apply_patch",
        {
            "path": "foo.py",
            "old": "    x = a + b\n    return x",
            "new": "    return a + b",
        },
    )

    assert out.startswith("Patched")
    assert (repo / "foo.py").read_text(encoding="utf-8") == ("def add(a, b):\n    return a + b\n")


def test_apply_patch_subdirectory(repo: Path) -> None:
    """子目录中的文件也应能修改。"""
    _make_file(repo, "src/pkg/mod.py", "x = 1\n")

    out = registry.execute(
        "apply_patch",
        {"path": "src/pkg/mod.py", "old": "x = 1", "new": "x = 2"},
    )

    assert out.startswith("Patched")
    assert (repo / "src/pkg/mod.py").read_text(encoding="utf-8") == "x = 2\n"


def test_apply_patch_delete_content(repo: Path) -> None:
    """new 为空字符串时应删除内容。"""
    _make_file(repo, "foo.py", "a = 1\nb = 2\nc = 3\n")

    out = registry.execute(
        "apply_patch",
        {"path": "foo.py", "old": "b = 2\n", "new": ""},
    )

    assert out.startswith("Patched")
    assert (repo / "foo.py").read_text(encoding="utf-8") == "a = 1\nc = 3\n"


# ─── 匹配失败 ───


def test_apply_patch_not_found(repo: Path) -> None:
    """old 未匹配应返回错误。"""
    _make_file(repo, "foo.py", "x = 1\n")

    out = registry.execute(
        "apply_patch",
        {"path": "foo.py", "old": "y = 2", "new": "y = 3"},
    )

    assert out.startswith("Error:")
    assert "not found" in out
    assert "re-read the target region" in out
    # 原文件不变
    assert (repo / "foo.py").read_text(encoding="utf-8") == "x = 1\n"


def test_apply_patch_not_found_long_old_hint(repo: Path) -> None:
    """old 超过 10 行且未匹配时，应提示改用 3-5 行小片段。"""
    _make_file(repo, "foo.py", "".join(f"line{i}\n" for i in range(20)))

    old = "".join(f"missing{i}\n" for i in range(12))
    out = registry.execute("apply_patch", {"path": "foo.py", "old": old, "new": "x"})

    assert "Try a smaller snippet" in out


def test_apply_patch_not_found_short_old_no_hint(repo: Path) -> None:
    """old 较短且未匹配时，不应出现小片段提示。"""
    _make_file(repo, "foo.py", "x = 1\n")

    out = registry.execute("apply_patch", {"path": "foo.py", "old": "y = 2", "new": "y = 3"})

    assert "Try a smaller snippet" not in out


def test_apply_patch_multiple_occurrences(repo: Path) -> None:
    """old 匹配多处应拒绝。"""
    _make_file(repo, "foo.py", "x = 1\ny = 1\n")

    out = registry.execute(
        "apply_patch",
        {"path": "foo.py", "old": "= 1", "new": "= 2"},
    )

    assert out.startswith("Error:")
    assert "2 locations" in out
    assert (repo / "foo.py").read_text(encoding="utf-8") == "x = 1\ny = 1\n"


# ─── 参数校验 ───


def test_apply_patch_empty_old(repo: Path) -> None:
    """old 为空应拒绝。"""
    _make_file(repo, "foo.py", "x = 1\n")
    out = registry.execute("apply_patch", {"path": "foo.py", "old": "", "new": "y = 2"})
    assert out.startswith("Error:")
    assert "empty" in out


def test_apply_patch_identical(repo: Path) -> None:
    """old == new 应拒绝。"""
    _make_file(repo, "foo.py", "x = 1\n")
    out = registry.execute("apply_patch", {"path": "foo.py", "old": "x = 1", "new": "x = 1"})
    assert out.startswith("Error:")
    assert "identical" in out


def test_apply_patch_old_too_large(repo: Path) -> None:
    """old 超过 20KB 应拒绝。"""
    _make_file(repo, "foo.py", "x = 1\n")
    big_old = "x" * 21_000
    out = registry.execute("apply_patch", {"path": "foo.py", "old": big_old, "new": "y"})
    assert out.startswith("Error:")
    assert "exceeds" in out


def test_apply_patch_line_diff_too_large(repo: Path) -> None:
    """行数差超过 200 应拒绝。"""
    _make_file(repo, "foo.py", "x = 1\n")
    out = registry.execute(
        "apply_patch",
        {
            "path": "foo.py",
            "old": "x = 1",
            "new": "\n".join(f"line {i}" for i in range(250)),
        },
    )
    assert out.startswith("Error:")
    assert "too large" in out
    assert "Split it into smaller apply_patch calls" in out


# ─── 路径校验 ───


def test_apply_patch_rejects_escape(repo: Path) -> None:
    """越界路径应拒绝。"""
    _make_file(repo, "foo.py", "x = 1\n")
    out = registry.execute(
        "apply_patch",
        {"path": "../outside.py", "old": "x = 1", "new": "x = 2"},
    )
    assert out.startswith("Error:")


def test_apply_patch_rejects_git_dir(repo: Path) -> None:
    """保护路径 .git/ 应拒绝。"""
    (repo / ".git").mkdir()
    _make_file(repo, ".git/config", "x = 1\n")
    out = registry.execute(
        "apply_patch",
        {"path": ".git/config", "old": "x = 1", "new": "x = 2"},
    )
    assert out.startswith("Error:")
    assert "protected" in out


def test_apply_patch_rejects_traces(repo: Path) -> None:
    """默认 trace 目录 traces/ 应拒绝。"""
    _make_file(repo, "traces/log.jsonl", "x = 1\n")
    out = registry.execute(
        "apply_patch",
        {"path": "traces/log.jsonl", "old": "x = 1", "new": "x = 2"},
    )
    assert out.startswith("Error:")
    assert "protected" in out


def test_apply_patch_protects_configured_trace_dir(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """保护的是配置的 trace 目录，而不是固定名称 traces/。"""
    monkeypatch.setattr(settings, "trace_dir", "runs")
    _make_file(repo, "runs/log.jsonl", "x = 1\n")
    _make_file(repo, "traces/log.jsonl", "x = 1\n")

    protected = registry.execute(
        "apply_patch",
        {"path": "runs/log.jsonl", "old": "x = 1", "new": "x = 2"},
    )
    allowed = registry.execute(
        "apply_patch",
        {"path": "traces/log.jsonl", "old": "x = 1", "new": "x = 2"},
    )

    assert "protected" in protected
    assert allowed.startswith("Patched")


def test_apply_patch_allows_reports(repo: Path) -> None:
    """reports 不再是全局保护目录。"""
    _make_file(repo, "reports/x.json", "x = 1\n")
    out = registry.execute(
        "apply_patch",
        {"path": "reports/x.json", "old": "x = 1", "new": "x = 2"},
    )
    assert out.startswith("Patched")


def test_apply_patch_rejects_env_file(repo: Path) -> None:
    """.env 应拒绝。"""
    _make_file(repo, ".env", "KEY=1\n")
    out = registry.execute(
        "apply_patch",
        {"path": ".env", "old": "KEY=1", "new": "KEY=2"},
    )
    assert out.startswith("Error:")
    assert "protected" in out


def test_apply_patch_rejects_env_variant(repo: Path) -> None:
    """.env.local 等派生文件也应拒绝。"""
    _make_file(repo, ".env.local", "KEY=1\n")
    out = registry.execute(
        "apply_patch",
        {"path": ".env.local", "old": "KEY=1", "new": "KEY=2"},
    )
    assert out.startswith("Error:")
    assert "protected" in out


def test_apply_patch_rejects_missing_file(repo: Path) -> None:
    """文件不存在应拒绝。"""
    out = registry.execute(
        "apply_patch",
        {"path": "missing.py", "old": "x = 1", "new": "x = 2"},
    )
    assert out.startswith("Error:")
    assert "not found" in out


def test_apply_patch_rejects_directory(repo: Path) -> None:
    """目标是目录应拒绝。"""
    (repo / "src").mkdir()
    out = registry.execute(
        "apply_patch",
        {"path": "src", "old": "x = 1", "new": "x = 2"},
    )
    assert out.startswith("Error:")
    assert "not a file" in out


# ─── 换行符处理 ───


def test_apply_patch_preserves_crlf(repo: Path) -> None:
    """CRLF 文件应保留 CRLF。"""
    p = repo / "foo.py"
    p.write_bytes(b"x = 1\r\ny = 2\r\n")

    out = registry.execute(
        "apply_patch",
        {"path": "foo.py", "old": "x = 1\ny = 2", "new": "x = 10\ny = 20"},
    )

    assert out.startswith("Patched")
    assert p.read_bytes() == b"x = 10\r\ny = 20\r\n"


def test_apply_patch_preserves_lf(repo: Path) -> None:
    """LF 文件应保留 LF。"""
    p = repo / "foo.py"
    p.write_bytes(b"x = 1\ny = 2\n")

    out = registry.execute(
        "apply_patch",
        {"path": "foo.py", "old": "x = 1\ny = 2", "new": "x = 10\ny = 20"},
    )

    assert out.startswith("Patched")
    assert p.read_bytes() == b"x = 10\ny = 20\n"


# ─── 原子性 ───


def test_apply_patch_leaves_file_on_error(repo: Path) -> None:
    """失败时原文件必须保持不变。"""
    original = "x = 1\ny = 1\n"
    _make_file(repo, "foo.py", original)

    registry.execute(
        "apply_patch",
        {"path": "foo.py", "old": "= 1", "new": "= 2"},
    )

    assert (repo / "foo.py").read_text(encoding="utf-8") == original


def test_apply_patch_no_tmp_left_on_success(repo: Path) -> None:
    """成功后不应残留临时文件。"""
    _make_file(repo, "foo.py", "x = 1\n")

    registry.execute(
        "apply_patch",
        {"path": "foo.py", "old": "x = 1", "new": "x = 2"},
    )

    tmp_files = list(repo.glob(".*.breeze_tmp"))
    assert tmp_files == []


# ─── create_file ───


def test_create_file_basic(repo: Path) -> None:
    """创建新文件应成功并自动建目录。"""
    out = registry.execute("create_file", {"path": "src/pkg/new.py", "content": "x = 1\n"})

    assert out.startswith("Created src/pkg/new.py")
    assert "6 bytes" in out
    assert (repo / "src/pkg/new.py").read_text(encoding="utf-8") == "x = 1\n"


def test_create_file_rejects_existing(repo: Path) -> None:
    """已存在文件应被拒且指向 apply_patch，原内容不变。"""
    _make_file(repo, "foo.py", "x = 1\n")

    out = registry.execute("create_file", {"path": "foo.py", "content": "y = 2\n"})

    assert out.startswith("Error:")
    assert "apply_patch" in out
    assert (repo / "foo.py").read_text(encoding="utf-8") == "x = 1\n"


def test_create_file_rejects_protected_paths(repo: Path) -> None:
    """保护路径下的文件应被拒。"""
    for p in ["traces/x.jsonl", ".git/config", ".env", ".env.local"]:
        out = registry.execute("create_file", {"path": p, "content": "x"})
        assert out.startswith("Error:"), p
        assert "protected" in out, p


def test_create_file_rejects_escape(repo: Path) -> None:
    """越界路径应被拒且不写出仓库。"""
    out = registry.execute("create_file", {"path": "../evil.py", "content": "x"})

    assert out.startswith("Error:")
    assert not (repo.parent / "evil.py").exists()


def test_create_file_rejects_oversized_content(repo: Path) -> None:
    """内容超过上限应被拒且不落盘。"""
    out = registry.execute("create_file", {"path": "big.txt", "content": "x" * 50_001})

    assert out.startswith("Error:")
    assert "bytes" in out
    assert not (repo / "big.txt").exists()


def test_create_file_preserves_newlines(repo: Path) -> None:
    """内容中的换行符应原样保留。"""
    content = "line1\nline2\n\nline4\n"

    out = registry.execute("create_file", {"path": "keep.py", "content": content})

    assert out.startswith("Created")
    assert (repo / "keep.py").read_text(encoding="utf-8") == content
    raw = (repo / "keep.py").read_bytes()
    assert raw == content.encode("utf-8")


def test_create_file_leaves_no_tmp(repo: Path) -> None:
    """成功后不应残留临时文件。"""
    registry.execute("create_file", {"path": "foo.py", "content": "x\n"})

    assert list(repo.glob(".*.breeze_tmp")) == []


# ─── git_restore 辅助 ───


def _init_git_repo(root: Path) -> None:
    """初始化一个 git 仓库并完成初始提交。"""
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env={**os.environ, **env})
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=root, check=True)


def _git_commit_all(root: Path, message: str = "init") -> None:
    """暂存所有改动并提交。"""
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=root, check=True)


# ─── git_restore ───


def test_git_restore_basic(repo: Path) -> None:
    """修改后回滚，文件应回到 HEAD 状态。"""
    _init_git_repo(repo)
    _make_file(repo, "foo.py", "x = 1\n")
    _git_commit_all(repo)

    (repo / "foo.py").write_text("x = 999\n", encoding="utf-8")
    assert (repo / "foo.py").read_text(encoding="utf-8") == "x = 999\n"

    out = registry.execute("git_restore", {"path": "foo.py"})
    assert out.startswith("Restored")
    assert (repo / "foo.py").read_text(encoding="utf-8") == "x = 1\n"


def test_git_restore_untracked_file(repo: Path) -> None:
    """未跟踪的文件应拒绝。"""
    _init_git_repo(repo)
    _make_file(repo, "committed.py", "x = 1\n")
    _git_commit_all(repo)

    _make_file(repo, "untracked.py", "y = 2\n")

    out = registry.execute("git_restore", {"path": "untracked.py"})
    assert out.startswith("Error:")
    assert "not tracked" in out


def test_git_restore_not_a_repo(repo: Path) -> None:
    """非 git 仓库应拒绝。"""
    _make_file(repo, "foo.py", "x = 1\n")

    out = registry.execute("git_restore", {"path": "foo.py"})
    assert out.startswith("Error:")
    assert "not a git repository" in out


def test_git_restore_rejects_escape(repo: Path) -> None:
    """越界路径应拒绝。"""
    _init_git_repo(repo)
    _make_file(repo, "foo.py", "x = 1\n")
    _git_commit_all(repo)

    out = registry.execute("git_restore", {"path": "../outside"})
    assert out.startswith("Error:")


def test_git_restore_rejects_protected(repo: Path) -> None:
    """保护路径应拒绝。"""
    _init_git_repo(repo)

    for protected in [".git/config", "traces/log.jsonl", ".env.local"]:
        out = registry.execute("git_restore", {"path": protected})
        assert out.startswith("Error:")
        assert "protected" in out


def test_git_restore_missing_file(repo: Path) -> None:
    """文件不存在应拒绝。"""
    _init_git_repo(repo)
    _make_file(repo, "foo.py", "x = 1\n")
    _git_commit_all(repo)

    out = registry.execute("git_restore", {"path": "missing.py"})
    assert out.startswith("Error:")
    assert "not found" in out


def test_git_restore_directory(repo: Path) -> None:
    """目标是目录应拒绝。"""
    _init_git_repo(repo)
    _make_file(repo, "foo.py", "x = 1\n")
    _git_commit_all(repo)

    (repo / "subdir").mkdir()

    out = registry.execute("git_restore", {"path": "subdir"})
    assert out.startswith("Error:")
    assert "not a file" in out


def test_git_restore_clean_file(repo: Path) -> None:
    """文件未修改时回滚应成功但不改变内容。"""
    _init_git_repo(repo)
    _make_file(repo, "foo.py", "x = 1\n")
    _git_commit_all(repo)

    out = registry.execute("git_restore", {"path": "foo.py"})
    assert out.startswith("Restored")
    assert (repo / "foo.py").read_text(encoding="utf-8") == "x = 1\n"


# ─── run_tests ───


@pytest.fixture
def fake_pytest(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """替换 run_command，返回可配置的结果。"""
    captured: dict[str, Any] = {
        "tokens": None,
        "cwd": None,
        "timeout": None,
        "returncode": 0,
        "stdout": "",
        "stderr": "",
    }

    def _fake(tokens: list[str], cwd: Path, timeout: int = 10) -> tuple[int, str, str]:
        captured["tokens"] = tokens
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        return (
            captured["returncode"],
            captured["stdout"],
            captured["stderr"],
        )

    monkeypatch.setattr("breeze.tools.edit.run_command", _fake)
    yield captured


def test_run_tests_all_pass(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """全部通过时返回 passed 计数。"""
    fake_pytest["returncode"] = 0
    fake_pytest["stdout"] = "148 passed in 5.34s\n"

    out = registry.execute("run_tests", {})

    assert "exit_code=0" in out
    assert "148 passed" in out
    assert "failures" not in out


def test_run_tests_with_failures(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """有失败时列出失败节点 ID 和断言详情。"""
    fake_pytest["returncode"] = 1
    fake_pytest["stdout"] = (
        "...F....\n"
        "================================== FAILURES ===================================\n"
        "______________________ test_x ______________________\n"
        "\n"
        "    def test_x():\n"
        '        with pytest.raises(ValueError, match="expected msg"):\n'
        ">           do_something()\n"
        "E           Failed: DID NOT RAISE <class 'ValueError'>\n"
        "\n"
        "tests/test_fs.py:42: Failed\n"
        "=========================== short test summary info ===========================\n"
        "FAILED tests/test_fs.py::test_x - Failed: DID NOT RAISE <class 'ValueError'>\n"
        "FAILED tests/test_search.py::test_y - AssertionError: nope\n"
        "148 passed, 2 failed in 6.0s\n"
    )

    out = registry.execute("run_tests", {})

    assert "exit_code=1" in out
    assert "148 passed" in out
    assert "2 failed" in out
    assert "tests/test_fs.py::test_x" in out
    assert "tests/test_search.py::test_y" in out
    # 断言详情
    assert "DID NOT RAISE" in out
    # traceback 段
    assert "--- pytest traceback (truncated) ---" in out
    assert "expected msg" in out


def test_run_tests_traceback_truncated(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """FAILURES 段超过 3000 字节应截断。"""
    fake_pytest["returncode"] = 1
    big_traceback = "E   " + "x" * 5000
    fake_pytest["stdout"] = (
        "================================== FAILURES ===================================\n" + big_traceback + "\n"
        "==================== short test summary info ====================\n"
        "FAILED tests/test_fs.py::test_x - failed\n"
    )

    out = registry.execute("run_tests", {})

    assert "traceback truncated" in out


def test_run_tests_with_errors(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """errors 计数应被解析。"""
    fake_pytest["returncode"] = 1
    fake_pytest["stdout"] = "10 passed, 1 error in 2.0s\n"

    out = registry.execute("run_tests", {})
    assert "10 passed" in out
    assert "1 error" in out


def test_run_tests_pattern_passed(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """pattern 应作为额外参数传给 pytest。"""
    fake_pytest["returncode"] = 0
    fake_pytest["stdout"] = "5 passed in 0.5s\n"

    registry.execute("run_tests", {"pattern": "tests/test_fs.py"})

    assert fake_pytest["tokens"] == [sys.executable, "-m", "pytest", "-q", "tests/test_fs.py"]


def test_run_tests_node_id_pattern(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """节点 ID 形式应被接受。"""
    fake_pytest["returncode"] = 0
    fake_pytest["stdout"] = "1 passed in 0.1s\n"

    out = registry.execute("run_tests", {"pattern": "tests/test_fs.py::test_apply_patch_basic"})

    assert "exit_code=0" in out
    assert fake_pytest["tokens"][-1] == "tests/test_fs.py::test_apply_patch_basic"


def test_run_tests_empty_pattern_runs_all(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """空 pattern 等同于跑全量测试。"""
    fake_pytest["returncode"] = 0
    fake_pytest["stdout"] = "148 passed in 5.34s\n"

    out = registry.execute("run_tests", {"pattern": ""})

    assert "exit_code=0" in out
    assert "148 passed" in out
    # tokens 只含解释器、-m pytest 和 -q，没有空字符串
    assert fake_pytest["tokens"] == [sys.executable, "-m", "pytest", "-q"]


def test_run_tests_prefers_repo_venv(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """仓库内存在虚拟环境时优先使用它，避免依赖 PATH。"""
    scripts = python_repo / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    python.write_text("", encoding="utf-8")
    fake_pytest["stdout"] = "1 passed in 0.1s\n"

    registry.execute("run_tests", {})

    assert fake_pytest["tokens"][:3] == [str(python), "-m", "pytest"]


def test_run_tests_falls_back_to_path_pytest(python_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """当前解释器缺少 pytest 时，退回 PATH 上的 pytest。"""
    calls: list[list[str]] = []
    results = iter(
        [
            (1, "", "No module named pytest"),
            (0, "3 passed in 0.2s\n", ""),
        ]
    )

    def _fake(tokens: list[str], _cwd: Path, timeout: int = 10) -> tuple[int, str, str]:
        calls.append(tokens)
        return next(results)

    monkeypatch.setattr("breeze.tools.edit.run_command", _fake)

    out = registry.execute("run_tests", {})

    assert "exit_code=0" in out
    assert calls[0][:3] == [sys.executable, "-m", "pytest"]
    assert calls[1] == ["pytest", "-q"]


def test_run_tests_missing_pytest_reports_error(python_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """解释器与 PATH 都没有 pytest 时给出明确指引。"""
    results = iter(
        [
            (1, "", "No module named pytest"),
            (-1, "", "Command not found: pytest"),
        ]
    )

    def _fake(_tokens: list[str], _cwd: Path, timeout: int = 10) -> tuple[int, str, str]:
        return next(results)

    monkeypatch.setattr("breeze.tools.edit.run_command", _fake)

    out = registry.execute("run_tests", {})

    assert out.startswith("Error:")
    assert "pytest is not available" in out
    assert sys.executable in out


def test_run_tests_whitespace_pattern_rejected(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """纯空白 pattern 仍应被拒绝，不被视为空串。"""
    out = registry.execute("run_tests", {"pattern": "   "})
    assert out.startswith("Error:")
    assert "invalid pattern" in out


def test_run_tests_rejects_spaces(python_repo: Path) -> None:
    """pattern 含空格应拒绝。"""
    out = registry.execute("run_tests", {"pattern": "tests/foo.py -v"})
    assert out.startswith("Error:")
    assert "invalid" in out


def test_run_tests_rejects_shell_chars(python_repo: Path) -> None:
    """pattern 含 shell 字符应拒绝。"""
    for bad in ["foo; rm -rf /", "foo && bar", "foo | bar", "$(whoami)"]:
        out = registry.execute("run_tests", {"pattern": bad})
        assert out.startswith("Error:"), bad


def test_run_tests_timeout(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """超时应返回 Error。"""
    fake_pytest["returncode"] = -1
    fake_pytest["stderr"] = "Command timed out after 60 seconds."

    out = registry.execute("run_tests", {})
    assert out.startswith("Error:")
    assert "timed out" in out


def test_run_tests_no_summary(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """无法解析摘要时返回 (no summary)。"""
    fake_pytest["returncode"] = 5
    fake_pytest["stdout"] = "no tests ran\n"

    out = registry.execute("run_tests", {})
    assert "no summary" in out


def test_run_tests_truncates_large_output(python_repo: Path, fake_pytest: dict[str, Any]) -> None:
    """超过 8KB 应截断。"""
    fake_pytest["returncode"] = 1
    # 每条约 60 字节，250 条约 15KB，必然触发 8KB 截断
    fail_lines = "\n".join(
        f"FAILED tests/test_some_long_module_name.py::test_case_{i:04d} - AssertionError: expected True but got False"
        for i in range(250)
    )
    fake_pytest["stdout"] = f"{fail_lines}\n10 passed, 250 failed in 30s\n"

    out = registry.execute("run_tests", {})

    assert "truncated" in out
    assert len(out.encode("utf-8")) <= 8_100  # 允许少量余量


def test_run_tests_real_pytest(python_repo: Path) -> None:
    """真实 pytest 调用：验证端到端可用。"""
    if shutil.which("pytest") is None:
        pytest.skip("pytest not found on PATH")

    _make_file(python_repo, "test_sample.py", "def test_ok():\n    assert 1 == 1\n")

    out = registry.execute("run_tests", {"pattern": "test_sample.py"})

    assert "exit_code=0" in out
    assert "1 passed" in out


def test_run_tests_non_python_project(repo: Path) -> None:
    """非 Python 项目应返回明确错误。"""
    _make_file(repo, "main.js", "console.log('hi');\n")
    _make_file(repo, "package.json", '{"name": "demo"}\n')

    out = registry.execute("run_tests", {})

    assert out.startswith("Error:")
    assert "no Python project" in out
    assert "run_shell" in out


# ─── delete_path ───


def test_delete_path_file(repo: Path) -> None:
    """删除普通文件。"""
    target = _make_file(repo, "old.py", "x = 1\n")

    out = registry.execute("delete_path", {"path": "old.py"})

    assert out.startswith("Deleted")
    assert not target.exists()


def test_delete_path_empty_directory(repo: Path) -> None:
    """空目录无需 recursive。"""
    target = repo / "empty"
    target.mkdir()

    out = registry.execute("delete_path", {"path": "empty"})

    assert out.startswith("Deleted")
    assert not target.exists()


def test_delete_path_non_empty_requires_recursive(repo: Path) -> None:
    """非空目录必须先传 recursive=true。"""
    _make_file(repo, "pkg/a.py", "x = 1\n")

    out = registry.execute("delete_path", {"path": "pkg"})
    assert out.startswith("Error:")
    assert "recursive=true" in out
    assert (repo / "pkg" / "a.py").exists()

    out = registry.execute("delete_path", {"path": "pkg", "recursive": True})
    assert out.startswith("Deleted")
    assert not (repo / "pkg").exists()


def test_delete_path_protected(repo: Path) -> None:
    """保护路径拒绝删除。"""
    for rel in (".env", ".env.local", ".git/config", "traces/run.jsonl"):
        target = _make_file(repo, rel, "secret")

        out = registry.execute("delete_path", {"path": rel})

        assert "protected" in out
        assert target.exists()


def test_delete_path_refuses_repository_root(repo: Path) -> None:
    """仓库根目录不可删除。"""
    for path in (".", ""):
        out = registry.execute("delete_path", {"path": path})

        assert out.startswith("Error:")
        assert "repository root" in out
    assert repo.exists()


def test_delete_path_missing(repo: Path) -> None:
    """不存在的路径返回明确错误。"""
    out = registry.execute("delete_path", {"path": "nope.txt"})

    assert out.startswith("Error:")
    assert "no such file" in out


def test_delete_path_out_of_bounds(repo: Path, tmp_path: Path) -> None:
    """越界路径拒绝删除。"""
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")

    out = registry.execute("delete_path", {"path": "../outside.txt"})

    assert "out of bounds" in out
    assert outside.exists()
    outside.unlink()


def test_delete_path_symlink_keeps_target(repo: Path) -> None:
    """符号链接只删除链接本身，不删除目标。"""
    target = _make_file(repo, "real.txt", "keep me\n")
    link = repo / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink not supported in this environment")

    out = registry.execute("delete_path", {"path": "link.txt"})

    assert "symlink" in out
    assert not link.exists()
    assert target.exists()

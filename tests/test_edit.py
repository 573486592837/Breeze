"""edit 模块的单元测试。"""

import os
import subprocess
from pathlib import Path

import pytest

from breeze.config import settings
from breeze.tools import registry


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 settings.repo_root 指向临时目录。"""
    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)
    return root


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
    # 原文件不变
    assert (repo / "foo.py").read_text(encoding="utf-8") == "x = 1\n"


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
    """保护路径 traces/ 应拒绝。"""
    _make_file(repo, "traces/log.jsonl", "x = 1\n")
    out = registry.execute(
        "apply_patch",
        {"path": "traces/log.jsonl", "old": "x = 1", "new": "x = 2"},
    )
    assert out.startswith("Error:")
    assert "protected" in out


def test_apply_patch_rejects_reports(repo: Path) -> None:
    """保护路径 reports/ 应拒绝。"""
    _make_file(repo, "reports/x.json", "x = 1\n")
    out = registry.execute(
        "apply_patch",
        {"path": "reports/x.json", "old": "x = 1", "new": "x = 2"},
    )
    assert out.startswith("Error:")
    assert "protected" in out


def test_apply_patch_rejects_env_file(repo: Path) -> None:
    """.env 应拒绝。"""
    _make_file(repo, ".env", "KEY=1\n")
    out = registry.execute(
        "apply_patch",
        {"path": ".env", "old": "KEY=1", "new": "KEY=2"},
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


# ─── git_diff ───


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


def test_git_diff_clean(repo: Path) -> None:
    """干净工作区应返回提示。"""
    _init_git_repo(repo)
    _make_file(repo, "foo.py", "x = 1\n")
    _git_commit_all(repo)

    out = registry.execute("git_diff", {})
    assert "clean" in out


def test_git_diff_with_changes(repo: Path) -> None:
    """有改动时应返回 diff。"""
    _init_git_repo(repo)
    _make_file(repo, "foo.py", "x = 1\n")
    _git_commit_all(repo)

    (repo / "foo.py").write_text("x = 2\n", encoding="utf-8")

    out = registry.execute("git_diff", {})
    assert "-x = 1" in out
    assert "+x = 2" in out


def test_git_diff_path_filter(repo: Path) -> None:
    """path 参数应限制输出到指定文件。"""
    _init_git_repo(repo)
    _make_file(repo, "a.py", "x = 1\n")
    _make_file(repo, "b.py", "y = 1\n")
    _git_commit_all(repo)

    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")

    out = registry.execute("git_diff", {"path": "a.py"})
    assert "a.py" in out
    assert "b.py" not in out


def test_git_diff_path_filter_clean(repo: Path) -> None:
    """指定文件无改动时返回提示。"""
    _init_git_repo(repo)
    _make_file(repo, "a.py", "x = 1\n")
    _make_file(repo, "b.py", "y = 1\n")
    _git_commit_all(repo)

    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")

    out = registry.execute("git_diff", {"path": "a.py"})
    assert "clean" in out


def test_git_diff_not_a_repo(repo: Path) -> None:
    """非 git 仓库应返回错误。"""
    _make_file(repo, "foo.py", "x = 1\n")
    out = registry.execute("git_diff", {})
    assert out.startswith("Error:")
    assert "not a git repository" in out


def test_git_diff_rejects_escape(repo: Path) -> None:
    """越界路径应被拒。"""
    _init_git_repo(repo)
    _make_file(repo, "foo.py", "x = 1\n")
    _git_commit_all(repo)

    out = registry.execute("git_diff", {"path": "../outside"})
    assert out.startswith("Error:")


def test_git_diff_truncates_large_output(repo: Path) -> None:
    """输出超过 8KB 应截断。"""
    _init_git_repo(repo)
    _make_file(repo, "big.txt", "x\n" * 50)
    _git_commit_all(repo)

    (repo / "big.txt").write_text("y\n" * 5000, encoding="utf-8")

    out = registry.execute("git_diff", {})
    assert "truncated" in out

"""fs 模块的单元测试。"""

from pathlib import Path

import pytest

from breeze.config import settings
from breeze.tools import registry
from breeze.tools.safety import safe_path


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 settings.repo_root 指向临时目录，供工具使用。"""
    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)
    return root


def _make_tree(root: Path) -> None:
    """构造一个包含源码、忽略目录的示例目录树。"""
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("print('b')\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "c.pyc").write_bytes(b"\x00")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("", encoding="utf-8")


# ─── list_files ───


def test_list_files_basic(repo: Path) -> None:
    """基础功能：返回相对路径，忽略 __pycache__ 与 .git。"""
    _make_tree(repo)
    out = registry.execute("list_files", {"path": ".", "max_depth": 1})

    assert "src/" in out
    assert "README.md" in out
    assert "__pycache__" not in out
    assert ".git/" not in out


def test_list_files_max_entries(repo: Path) -> None:
    """max_entries 应截断结果并附加提示。"""
    for i in range(20):
        (repo / f"f{i:02d}.txt").write_text("x", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 1, "max_entries": 5})
    body = [ln for ln in out.splitlines() if not ln.startswith("...")]

    assert len(body) == 5
    assert "truncated" in out


def test_list_files_empty_dir(repo: Path) -> None:
    """空目录应返回提示文本。"""
    out = registry.execute("list_files", {"path": ".", "max_depth": 1})
    assert "(empty)" in out


# ─── read_file ───


def test_read_file_line_numbers(repo: Path) -> None:
    """read_file 返回带行号的内容。"""
    (repo / "x.py").write_text("line1\nline2\nline3\n", encoding="utf-8")

    out = registry.execute("read_file", {"path": "x.py"})
    lines = out.splitlines()

    assert lines[0].startswith("1 | line1")
    assert lines[1].startswith("2 | line2")
    assert lines[2].startswith("3 | line3")


def test_read_file_range(repo: Path) -> None:
    """start_line / end_line 应只返回指定范围。"""
    content = "\n".join(f"L{i}" for i in range(1, 11))
    (repo / "x.py").write_text(content, encoding="utf-8")

    out = registry.execute("read_file", {"path": "x.py", "start_line": 3, "end_line": 5})

    assert "3 | L3" in out
    assert "5 | L5" in out
    assert "L2" not in out
    assert "L6" not in out


def test_read_file_byte_truncation(repo: Path) -> None:
    """max_bytes 超限时应截断并提示。"""
    content = "\n".join("x" * 100 for _ in range(20))
    (repo / "big.txt").write_text(content, encoding="utf-8")

    out = registry.execute("read_file", {"path": "big.txt", "max_bytes": 150})
    assert "truncated" in out or "..." in out


def test_read_file_rejects_dir(repo: Path) -> None:
    """对目录调用 read_file 应抛 ValueError。"""
    (repo / "src").mkdir()
    with pytest.raises(ValueError):
        registry.execute("read_file", {"path": "src"})


def test_read_file_rejects_escape(repo: Path) -> None:
    """越界路径应被 safe_path 拦截。"""
    (repo / "marker.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        registry.execute("read_file", {"path": "../outside.txt"})


# ─── safe_path ───


def test_safe_path_rejects_escape(repo: Path) -> None:
    """safe_path 应拒绝各种越界路径。"""
    for bad in ["..", "../outside.txt", "src/../../outside.txt"]:
        with pytest.raises(ValueError):
            safe_path(bad, root=repo)


def test_safe_path_allows_inside(repo: Path) -> None:
    """safe_path 应允许仓库内路径，并返回解析后的绝对路径。"""
    (repo / "a.txt").write_text("x", encoding="utf-8")
    resolved = safe_path("a.txt", root=repo)
    assert resolved == (repo / "a.txt").resolve()
    assert resolved.is_absolute()

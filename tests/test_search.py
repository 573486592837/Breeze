"""search 模块的单元测试。"""

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


@pytest.fixture
def sample(repo: Path) -> Path:
    """构造搜索用的示例仓库。"""
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text(
        "def foo():\n    return 1\n\ndef bar():\n    return foo()\n",
        encoding="utf-8",
    )
    (repo / "src" / "b.py").write_text(
        "import os\n\ndef baz():\n    return os.getcwd()\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# demo\nfoo bar baz\n", encoding="utf-8")
    return repo


# ─── grep 基础行为 ───


def test_grep_basic(sample: Path) -> None:
    """基础搜索应返回 相对路径:行号:内容 格式。"""
    out = registry.execute("grep", {"pattern": "foo", "path": "."})
    assert "src/a.py" in out
    assert "def foo" in out


def test_grep_glob(sample: Path) -> None:
    """glob 过滤应只命中匹配的文件名。"""
    out = registry.execute("grep", {"pattern": "foo", "path": ".", "glob": "*.py", "max_results": 20})
    assert "README.md" not in out
    assert "src/a.py" in out


def test_grep_max_results(sample: Path) -> None:
    """max_results 应限制结果行数。"""
    out = registry.execute("grep", {"pattern": "return", "path": ".", "max_results": 2})
    lines = [ln for ln in out.splitlines() if not ln.startswith("...")]
    assert len(lines) == 2


def test_grep_no_match(sample: Path) -> None:
    """无匹配时应返回提示文本。"""
    out = registry.execute("grep", {"pattern": "nonexistent", "path": "."})
    assert "No matches found." in out


def test_grep_search_single_file(sample: Path) -> None:
    """搜索单个文件应返回正确结果。"""
    out = registry.execute("grep", {"pattern": "foo", "path": "README.md"})
    assert "README.md" in out


# ─── grep 异常处理 ───


def test_grep_invalid_regex(sample: Path) -> None:
    """非法正则应抛 ValueError。"""
    with pytest.raises(ValueError):
        registry.execute("grep", {"pattern": "[unclosed", "path": "."})


def test_grep_escape_path(sample: Path) -> None:
    """路径越界应抛 ValueError。"""
    with pytest.raises(ValueError):
        registry.execute("grep", {"pattern": "x", "path": "../outside.py"})


# ─── grep Python 回退 ───


def test_grep_python_fallback(
    sample: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """强制关闭 ripgrep，验证 Python 回退路径。"""
    monkeypatch.setattr("breeze.tools.search.shutil.which", lambda _name: None)

    out = registry.execute("grep", {"pattern": "def foo", "path": "."})
    assert "src/a.py" in out
    assert "def foo" in out


def test_grep_python_fallback_invalid_regex(
    sample: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python 回退路径下，非法正则应抛 ValueError。"""
    monkeypatch.setattr("breeze.tools.search.shutil.which", lambda _name: None)

    with pytest.raises(ValueError):
        registry.execute("grep", {"pattern": "[unclosed", "path": "."})


def test_grep_python_fallback_glob(
    sample: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Python 回退路径下，glob 过滤应只命中匹配的文件名。"""
    monkeypatch.setattr("breeze.tools.search.shutil.which", lambda _name: None)

    out = registry.execute("grep", {"pattern": "foo", "path": ".", "glob": "*.py", "max_results": 20})
    assert "README.md" not in out
    assert "src/a.py" in out

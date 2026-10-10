"""repo_map 工具的单元测试。"""

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


def _make_project(root: Path) -> None:
    """构造一个含类和函数的示例项目。"""
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text(
        "class Foo:\n"
        "    def bar(self, x: int) -> str:\n"
        "        return str(x)\n"
        "\n"
        "def baz(a, b=1):\n"
        "    return a + b\n"
        "\n"
        "MAX = 10\n",
        encoding="utf-8",
    )
    (root / "src" / "b.py").write_text(
        "def simple() -> None:\n    pass\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# demo\n", encoding="utf-8")


def test_repo_map_basic(repo: Path) -> None:
    """repo_map 应列出文件、类、函数与常量。"""
    _make_project(repo)
    out = registry.execute("repo_map", {"path": "."})

    assert "src/a.py" in out
    assert "class Foo" in out
    assert "bar(self, x)" in out
    assert "baz(a, b)" in out
    assert "MAX" in out
    assert "src/b.py" in out
    assert "simple()" in out


def test_repo_map_ignores_non_python(repo: Path) -> None:
    """非 Python 文件不应出现在输出中。"""
    _make_project(repo)
    out = registry.execute("repo_map", {"path": "."})
    assert "README.md" not in out


def test_repo_map_ignores_pycache(repo: Path) -> None:
    """IGNORED_DIRS 中的目录应被跳过。"""
    _make_project(repo)
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "c.py").write_text("x = 1\n", encoding="utf-8")

    out = registry.execute("repo_map", {"path": "."})
    assert "__pycache__" not in out


def test_repo_map_handles_syntax_error(repo: Path) -> None:
    """语法错误的文件应标记为解析失败，而不是抛异常。"""
    (repo / "broken.py").write_text("def (", encoding="utf-8")

    out = registry.execute("repo_map", {"path": "."})
    assert "broken.py" in out
    assert "resolve failed" in out


def test_repo_map_max_bytes_truncation(repo: Path) -> None:
    """超过 max_bytes 时应截断。"""
    for i in range(20):
        (repo / f"f{i:02d}.py").write_text(
            f"def func_{i}(a, b, c):\n    return a\n",
            encoding="utf-8",
        )

    out = registry.execute("repo_map", {"path": ".", "max_bytes": 100})
    assert "limit" in out


def test_repo_map_max_files(repo: Path) -> None:
    """max_files 应限制文件数量。"""
    for i in range(10):
        (repo / f"f{i:02d}.py").write_text("x = 1\n", encoding="utf-8")

    out = registry.execute("repo_map", {"path": ".", "max_files": 3})
    file_lines = [ln for ln in out.splitlines() if ln.startswith("f")]
    assert len(file_lines) <= 3


def test_repo_map_empty(repo: Path) -> None:
    """空目录应返回提示。"""
    out = registry.execute("repo_map", {"path": "."})
    assert "no python files" in out


def test_repo_map_escape_path(repo: Path) -> None:
    """越界路径应被 safe_path 拦截。"""
    with pytest.raises(ValueError):
        registry.execute("repo_map", {"path": "../"})


def test_repo_map_ignores_traces(repo: Path) -> None:
    """traces 目录下的 .py 文件不应出现。"""
    (repo / "traces").mkdir()
    (repo / "traces" / "fake.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "real.py").write_text("y = 2\n", encoding="utf-8")

    out = registry.execute("repo_map", {"path": "."})
    assert "traces" not in out
    assert "real.py" in out


def test_repo_map_respects_gitignore(repo: Path) -> None:
    """.gitignore 中声明的目录应被忽略。"""
    (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
    (repo / "generated").mkdir()
    (repo / "generated" / "auto.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "main.py").write_text("y = 2\n", encoding="utf-8")

    out = registry.execute("repo_map", {"path": "."})
    assert "auto.py" not in out
    assert "main.py" in out
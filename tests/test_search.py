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


# ─── grep rg 输出归一化 ───


def test_grep_rg_line_normalizes_windows_separators() -> None:
    """rg 在 Windows 输出的反斜杠路径应归一化为正斜杠并去掉 ./ 前缀。"""
    from breeze.tools.search import _normalize_rg_line

    assert _normalize_rg_line(".\\src\\a.py:1:def foo():") == "src/a.py:1:def foo():"
    assert _normalize_rg_line("src\\sub\\b.py:12:x = 1") == "src/sub/b.py:12:x = 1"
    assert _normalize_rg_line("README.md:2:foo bar") == "README.md:2:foo bar"
    assert _normalize_rg_line("no line number") == "no line number"


def test_grep_rg_output_normalized(
    sample: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """强制走 rg 分支并用伪造的 Windows 输出，验证最终结果格式。"""
    import subprocess

    monkeypatch.setattr("breeze.tools.search.shutil.which", lambda _name: "/fake/rg")

    def _fake(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=".\\src\\a.py:1:def foo():\n.\\README.md:2:foo bar\n",
            stderr="",
        )

    monkeypatch.setattr("breeze.tools.search.subprocess.run", _fake)

    out = registry.execute("grep", {"pattern": "foo", "path": "."})
    assert "src/a.py:1:def foo():" in out
    assert "README.md:2:foo bar" in out
    assert "\\" not in out


# ─── grep 异常处理 ───


def test_grep_invalid_regex(sample: Path) -> None:
    """非法正则应抛 ValueError，并带上 re 给出的具体原因。"""
    with pytest.raises(ValueError) as excinfo:
        registry.execute("grep", {"pattern": "[unclosed", "path": "."})

    message = str(excinfo.value)
    assert "Invalid regex pattern '[unclosed'" in message
    assert "unterminated" in message


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


def test_grep_caps_long_lines(repo: Path) -> None:
    """超长匹配行应被截断并标记。"""
    (repo / "long.txt").write_text("x" * 1000, encoding="utf-8")

    out = registry.execute("grep", {"pattern": "xxx", "path": ".", "glob": "*.txt"})

    assert "…[line truncated]" in out
    max_len = max(len(line) for line in out.splitlines())
    assert max_len <= 200 + len(" …[line truncated]")


def test_grep_respects_output_budget(repo: Path) -> None:
    """累计输出超过工具结果预算时应丢弃剩余行。"""
    content = "\n".join(f"match {'y' * 180}{i}" for i in range(300))
    (repo / "many.txt").write_text(content, encoding="utf-8")

    out = registry.execute("grep", {"pattern": "match", "path": ".", "glob": "*.txt"})

    assert "output budget" in out
    assert len(out.encode("utf-8")) <= settings.context_tool_result_max_bytes + 128


def test_grep_python_fallback_respects_gitignore_from_root(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """从仓库根搜索时，Python 回退应遵循 .gitignore。"""
    monkeypatch.setattr("breeze.tools.search.shutil.which", lambda _name: None)
    (repo / ".gitignore").write_text("traces/\n", encoding="utf-8")
    (repo / "traces").mkdir()
    (repo / "traces" / "run.jsonl").write_text("needle\n", encoding="utf-8")
    (repo / "README.md").write_text("needle\n", encoding="utf-8")

    out = registry.execute("grep", {"pattern": "needle", "path": "."})

    assert "README.md" in out
    assert "run.jsonl" not in out


def test_grep_explicit_subdir_bypasses_gitignore(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式指定被忽略的子目录时，应搜索其中的文件。"""
    monkeypatch.setattr("breeze.tools.search.shutil.which", lambda _name: None)
    (repo / ".gitignore").write_text("traces/\n", encoding="utf-8")
    (repo / "traces").mkdir()
    (repo / "traces" / "run.jsonl").write_text("needle\n", encoding="utf-8")

    out = registry.execute("grep", {"pattern": "needle", "path": "traces"})

    assert "run.jsonl" in out
    assert "needle" in out


def test_grep_skips_env_from_root(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """即使没有被 gitignore，也不应搜索 .env 内容。"""
    monkeypatch.setattr("breeze.tools.search.shutil.which", lambda _name: None)
    (repo / ".env").write_text("SECRET_TOKEN=1\n", encoding="utf-8")
    (repo / "README.md").write_text("SECRET_TOKEN\n", encoding="utf-8")

    out = registry.execute("grep", {"pattern": "SECRET_TOKEN", "path": "."})

    assert "README.md" in out
    assert ".env" not in out


def test_grep_rejects_env_target(repo: Path) -> None:
    """显式搜索 .env 也应被拒绝。"""
    (repo / ".env").write_text("SECRET_TOKEN=1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="protected"):
        registry.execute("grep", {"pattern": "SECRET", "path": ".env"})

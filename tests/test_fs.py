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
    """对目录调用 read_file 应抛 ValueError，并指向 list_files。"""
    (repo / "src").mkdir()
    with pytest.raises(ValueError, match="list_files"):
        registry.execute("read_file", {"path": "src"})


def test_read_file_rejects_escape(repo: Path) -> None:
    """越界路径应被 safe_path 拦截。"""
    (repo / "marker.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        registry.execute("read_file", {"path": "../outside.txt"})


def test_read_file_rejects_env_files(repo: Path) -> None:
    """.env 及其派生文件可能含密钥，禁止读取。"""
    for name in (".env", ".env.local", ".env.production"):
        (repo / name).write_text("SECRET=1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="protected"):
            registry.execute("read_file", {"path": name})


def test_read_file_allows_env_template(repo: Path) -> None:
    """模板文件不含真实密钥，可以读取。"""
    (repo / ".env.example").write_text("SECRET=\n", encoding="utf-8")

    out = registry.execute("read_file", {"path": ".env.example"})

    assert "SECRET=" in out


def test_read_file_rejects_git_internals(repo: Path) -> None:
    """.git 内部数据不应进入 prompt。"""
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    with pytest.raises(ValueError, match="protected"):
        registry.execute("read_file", {"path": ".git/config"})


def test_list_files_hides_env_files(repo: Path) -> None:
    """列目录时也不暴露 .env 文件。"""
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (repo / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    (repo / "README.md").write_text("x", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 1})
    lines = out.splitlines()

    assert ".env" not in lines
    assert ".env.example" in lines


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


@pytest.fixture
def read_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """构造仓库与仓库外的只读参考目录。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "note.txt").write_text("hello from reference\n", encoding="utf-8")
    monkeypatch.setattr(settings, "repo_root", repo.resolve())
    monkeypatch.setattr(settings, "tools_read_roots", (ref.resolve(),))
    return repo, ref


def test_read_file_allows_configured_read_root(read_root: tuple[Path, Path]) -> None:
    """读工具可以访问只读参考目录。"""
    out = registry.execute("read_file", {"path": "../ref/note.txt"})
    assert "hello from reference" in out


def test_list_files_allows_configured_read_root(read_root: tuple[Path, Path]) -> None:
    """list_files 可以列出只读参考目录。"""
    out = registry.execute("list_files", {"path": "../ref", "max_depth": 1})
    assert "note.txt" in out


def test_grep_allows_configured_read_root(
    read_root: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """grep 可以搜索只读参考目录。"""
    monkeypatch.setattr("breeze.tools.search.shutil.which", lambda _name: None)

    out = registry.execute("grep", {"pattern": "reference", "path": "../ref"})
    assert "hello from reference" in out


def test_write_tools_still_reject_read_root(read_root: tuple[Path, Path]) -> None:
    """只读参考目录只能读，写工具仍被限制在仓库内。"""
    repo, ref = read_root

    with pytest.raises(ValueError):
        safe_path("../ref/note.txt", root=repo)

    out = registry.execute("create_file", {"path": "../ref/new.txt", "content": "x"})
    assert out.startswith("Error:")
    assert not (ref / "new.txt").exists()


def test_list_files_hides_gitignored_traces_from_root(repo: Path) -> None:
    """从仓库根列出时，被 gitignore 的 traces 不应淹没有用信息。"""
    (repo / ".gitignore").write_text("traces/\n", encoding="utf-8")
    (repo / "traces").mkdir()
    (repo / "traces" / "20261008.jsonl").write_text("{}", encoding="utf-8")
    (repo / "README.md").write_text("x", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 2})
    assert "traces" not in out
    assert "20261008.jsonl" not in out


def test_list_files_explicit_subdir_bypasses_gitignore(repo: Path) -> None:
    """显式指定子目录时，应能看到被 gitignore 的文件。"""
    (repo / ".gitignore").write_text("traces/\n", encoding="utf-8")
    (repo / "traces").mkdir()
    (repo / "traces" / "20261008.jsonl").write_text("{}", encoding="utf-8")

    out = registry.execute("list_files", {"path": "traces", "max_depth": 2})
    assert "20261008.jsonl" in out


def test_list_files_reports_dir_not_special(repo: Path) -> None:
    """reports 不再是全局忽略目录。"""
    (repo / "reports").mkdir()
    (repo / "reports" / "baseline.json").write_text("{}", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 2})
    assert "reports/" in out
    assert "baseline.json" in out


def test_list_files_ignores_egg_info_pattern(repo: Path) -> None:
    """*.egg-info 应作为 glob 模式匹配。"""
    (repo / "demo.egg-info").mkdir()
    (repo / "demo.egg-info" / "PKG-INFO").write_text("x", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 2})
    assert "demo.egg-info" not in out


def test_list_files_respects_gitignore(repo: Path) -> None:
    """.gitignore 中的模式应被忽略。"""
    (repo / ".gitignore").write_text("data/\n*.log\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "dump.csv").write_text("x", encoding="utf-8")
    (repo / "debug.log").write_text("x", encoding="utf-8")
    (repo / "README.md").write_text("x", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 2})
    assert "data/" not in out
    assert "debug.log" not in out
    assert "README.md" in out


def test_list_files_ignores_gitignore_comments(repo: Path) -> None:
    """.gitignore 中的注释和空行应被忽略。"""
    (repo / ".gitignore").write_text("# a comment\n\n*.tmp\n", encoding="utf-8")
    (repo / "a.tmp").write_text("x", encoding="utf-8")
    (repo / "b.txt").write_text("x", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 1})
    assert "a.tmp" not in out
    assert "b.txt" in out


def test_list_files_without_gitignore(repo: Path) -> None:
    """没有 .gitignore 时不应报错。"""
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 2})
    assert "src/" in out
    assert "a.py" in out


def test_read_file_reports_total_lines(repo: Path) -> None:
    """read_file 应返回文件总行数。"""
    content = "\n".join(f"L{i}" for i in range(1, 21))
    (repo / "x.py").write_text(content, encoding="utf-8")

    out = registry.execute("read_file", {"path": "x.py", "start_line": 1, "end_line": 5})

    assert "file has 20 lines total" in out


def test_read_file_suggests_next_symbol(repo: Path) -> None:
    """读取部分范围时，应指出下一符号位置。"""
    lines = [
        "def first():",
        "    pass",
        "",
        "def second():",
        "    pass",
        "",
        "class Third:",
        "    pass",
    ]
    (repo / "x.py").write_text("\n".join(lines), encoding="utf-8")

    out = registry.execute("read_file", {"path": "x.py", "start_line": 1, "end_line": 2})

    assert "next symbol at line 4" in out
    assert "def second" in out


def test_read_file_no_next_symbol_at_end(repo: Path) -> None:
    """读到文件末尾时不提示下一符号。"""
    content = "def foo():\n    pass\n"
    (repo / "x.py").write_text(content, encoding="utf-8")

    out = registry.execute("read_file", {"path": "x.py"})

    assert "next symbol" not in out
    assert "file has 2 lines total" in out


def test_read_file_truncation_includes_total(repo: Path) -> None:
    """截断时也返回总行数。"""
    content = "\n".join("x" * 200 for _ in range(50))
    (repo / "big.py").write_text(content, encoding="utf-8")

    out = registry.execute("read_file", {"path": "big.py", "max_bytes": 500})

    assert "truncated" in out
    assert "file has 50 lines total" in out


def test_list_files_limits_per_dir_files(repo: Path) -> None:
    """单目录下文件超过 max_files_per_dir 时省略。"""
    for i in range(50):
        (repo / f"log_{i:03d}.txt").write_text("x", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 1, "max_files_per_dir": 10})

    # 只应显示 10 个
    shown = [ln for ln in out.splitlines() if ln.startswith("log_")]
    assert len(shown) == 10
    # 应提示省略
    assert "more files" in out
    assert "40 more" in out


def test_list_files_per_dir_default_20(repo: Path) -> None:
    """默认每目录显示 20 个文件。"""
    for i in range(30):
        (repo / f"f{i:02d}.txt").write_text("x", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 1})

    shown = [ln for ln in out.splitlines() if ln.startswith("f")]
    assert len(shown) == 20
    assert "10 more" in out


def test_list_files_directories_still_listed(repo: Path) -> None:
    """有大量文件的目录，其子目录仍应列出。"""
    (repo / "sub").mkdir()
    (repo / "sub" / "a.py").write_text("x", encoding="utf-8")
    for i in range(50):
        (repo / f"file_{i}.txt").write_text("x", encoding="utf-8")

    out = registry.execute("list_files", {"path": ".", "max_depth": 1, "max_files_per_dir": 5})

    assert "sub/" in out
    assert "more files" in out


def test_read_file_default_cap_matches_context_budget() -> None:
    """read_file 默认上限与工具结果预算对齐，兜底压缩不应触发。"""
    from breeze.tools.fs import DEFAULT_MAX_BYTES

    assert settings.context_tool_result_max_bytes == DEFAULT_MAX_BYTES

"""AGENTS.md 加载与注入的单元测试。"""

from pathlib import Path

from breeze.instructions import (
    AGENTS_FILENAME,
    MAX_INSTRUCTIONS_CHARS,
    load_project_instructions,
    render_project_instructions,
)


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    """仓库根目录没有 AGENTS.md 时不产生指引。"""
    text, path = load_project_instructions(tmp_path)

    assert text == ""
    assert path is None


def test_loads_file_content_stripped(tmp_path: Path) -> None:
    """读取正文并去掉首尾空白。"""
    (tmp_path / AGENTS_FILENAME).write_text("  用 pytest 跑测试\n", encoding="utf-8")

    text, path = load_project_instructions(tmp_path)

    assert text == "用 pytest 跑测试"
    assert path == tmp_path / AGENTS_FILENAME


def test_empty_file_returns_path_but_no_text(tmp_path: Path) -> None:
    """空文件视为没有指引，但仍返回路径便于提示。"""
    (tmp_path / AGENTS_FILENAME).write_text("  \n\n", encoding="utf-8")

    text, path = load_project_instructions(tmp_path)

    assert text == ""
    assert path == tmp_path / AGENTS_FILENAME


def test_long_file_is_truncated(tmp_path: Path) -> None:
    """超长指引按上限截断并标注，避免挤占系统提示预算。"""
    (tmp_path / AGENTS_FILENAME).write_text("x" * (MAX_INSTRUCTIONS_CHARS + 500), encoding="utf-8")

    text, _ = load_project_instructions(tmp_path)

    assert len(text) <= MAX_INSTRUCTIONS_CHARS
    assert text.endswith("[AGENTS.md truncated]")


def test_render_wraps_with_header_and_safety_note() -> None:
    """渲染结果说明来源，并强调不能放宽安全限制。"""
    rendered = render_project_instructions("规则 A")

    assert f"[项目指引 — {AGENTS_FILENAME}]" in rendered
    assert "安全规则为准" in rendered
    assert "规则 A" in rendered

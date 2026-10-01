"""文件系统工具。

提供 list_files 和 read_file，两者均通过 safe_path 限定在仓库根目录内。
"""

from pathlib import Path

from breeze.tools.registry import register
from breeze.tools.safety import safe_path

IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        ".env",
        ".ruff_cache",
        "pytest_cache",
        ".mypy_cache",
        "node_modules",
        "build",
        "dist",
        ".idea",
        ".vscode",
    }
)

DEFAULT_MAX_ENTRIES = 200
DEFAULT_MAX_BYTES = 200_000


@register(
    name="list_files",
    description=("列出仓库内的文件和目录。返回相对路径，每行一个。用于了解项目结构。可通过 max_depth 控制递归深度。"),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "起始目录，相对于仓库根目录。默认为 '.'。",
                "default": ".",
            },
            "max_depth": {
                "type": "integer",
                "description": "最大递归深度。1 表示只列出起始目录的直接子项。默认为 2。",
                "default": 2,
            },
            "max_entries": {
                "type": "integer",
                "description": "返回条目数上限。默认为 200。",
                "default": DEFAULT_MAX_ENTRIES,
            },
        },
        "required": [],
    },
)
def list_files(
    path: str = ".",
    max_depth: int = 2,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> str:
    """列出目录内容。

    Args:
        path: 起始目录，相对于仓库根目录。
        max_depth: 最大递归深度，至少为 1。
        max_entries: 返回条目数上限，至少为 1。

    Returns:
        每行一个相对路径的字符串。超限时追加截断提示。
    """
    root = safe_path(path)
    max_depth = max(1, max_depth)
    max_entries = max(1, max_entries)

    entries: list[str] = []
    truncated = False

    def walk(current: Path, depth: int) -> None:
        nonlocal truncated
        if truncated or depth > max_depth:
            return

        try:
            children = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except (PermissionError, OSError):
            return

        for child in children:
            if truncated:
                return
            if child.is_dir():
                if child.name in IGNORED_DIRS:
                    continue
                if len(entries) >= max_entries:
                    truncated = True
                    return
                rel = child.relative_to(root).as_posix()
                entries.append(f"{rel}/")
                walk(child, depth + 1)
            else:
                if len(entries) >= max_entries:
                    truncated = True
                    return
                rel = child.relative_to(root).as_posix()
                entries.append(rel)

    walk(root, 1)

    result = "\n".join(entries) if entries else "(empty)"
    if truncated:
        result += f"\n... (truncated to {max_entries} entries)"
    return result


@register(
    name="read_file",
    description=("读取文件内容，带行号。可通过 start_line 和 end_line 读取指定行范围。文件过大时会被截断。"),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径，相对于仓库根目录。",
            },
            "start_line": {
                "type": "integer",
                "description": "起始行号，从 1 开始。默认为 1。",
                "default": 1,
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号（含）。省略时读到文件末尾。",
            },
            "max_bytes": {
                "type": "integer",
                "description": "返回内容的字节上限。默认为 200000。",
                "default": DEFAULT_MAX_BYTES,
            },
        },
        "required": ["path"],
    },
)
def read_file(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """读取文件内容，带行号。

    Args:
        path: 文件路径，相对于仓库根目录。
        start_line: 起始行号，从 1 开始。
        end_line: 结束行号（含），None 表示读到末尾。
        max_bytes: 返回内容的字节上限。

    Returns:
        每行格式为 ``行号 | 内容`` 的字符串。

    Raises:
        ValueError: 路径越界或目标不是文件。
    """
    target = safe_path(path)
    if not target.is_file():
        raise ValueError(f"Path is not a file: {path}")

    max_bytes = max(1, int(max_bytes))
    start_line = max(1, int(start_line))
    if end_line is not None:
        end_line = max(start_line, int(end_line))

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Failed to read file: {path}") from exc

    lines = text.splitlines()
    total_lines = len(lines)

    slice_start = start_line - 1
    slice_end = total_lines if end_line is None else min(end_line, total_lines)
    selected = lines[slice_start:slice_end]

    width = len(str(slice_end) if slice_end > 0 else 1)
    rendered_lines: list[str] = []
    current_bytes = 0
    truncated = False

    for offset, line in enumerate(selected):
        line_no = start_line + offset
        rendered = f"{line_no:>{width}} | {line}"
        rendered_bytes = len(rendered.encode("utf-8"))
        if current_bytes + rendered_bytes > max_bytes:
            truncated = True
            break
        rendered_lines.append(rendered)
        current_bytes += rendered_bytes

    if not rendered_lines:
        return "(empty)"

    result = "\n".join(rendered_lines)
    if truncated:
        result += f"\n... (truncated to {max_bytes} bytes)"
    elif end_line is not None and end_line < total_lines:
        result += f"\n... ({total_lines - end_line} more lines not shown)"
    return result

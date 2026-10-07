"""仓库地图工具。

用 AST 提取 Python 文件的顶层符号，生成紧凑的项目结构概览。
"""

from __future__ import annotations

import ast
from pathlib import Path

from breeze.tools.fs import IGNORED_DIRS
from breeze.tools.registry import register
from breeze.tools.safety import safe_path

DEFAULT_MAX_FILES = 200
DEFAULT_MAX_BYTES = 20_000
PY_EXTENSIONS = {".py"}


@register(
    name="repo_map",
    description=(
        "生成仓库结构概览。每个文件列出顶层类和函数签名。"
        "用于快速了解项目结构，替代逐个读取文件。"
        "返回紧凑文本，适合在探索阶段使用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "起始目录，相对于仓库根目录。默认为 '.'。",
                "default": ".",
            },
            "max_files": {
                "type": "integer",
                "description": "最多列出的文件数。默认为 200。",
                "default": DEFAULT_MAX_FILES,
            },
            "max_bytes": {
                "type": "integer",
                "description": "返回内容的字节上限。默认为 20000。",
                "default": DEFAULT_MAX_BYTES,
            },
        },
        "required": [],
    },
)
def repo_map(
    path: str = ".",
    max_files: int = DEFAULT_MAX_FILES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """生成仓库结构概览。

    Args:
        path: 起始目录，相对于仓库根目录。
        max_files: 最多列出的文件数，至少为 1。
        max_bytes: 返回内容的字节上限，至少为 1。

    Returns:
        紧凑的项目结构文本。
    """
    root = safe_path(path)
    max_files = max(1, int(max_files))
    max_bytes = max(1, int(max_bytes))

    py_files = _collect_python_files(root, max_files)

    sections: list[str] = []
    total_bytes = 0
    truncated = False

    for fpath in py_files:
        rel = _relative_to_repo(fpath, root)
        section = _render_file(fpath, rel)
        if not section:
            continue

        section_bytes = len(section.encode("utf-8"))
        if total_bytes + section_bytes > max_bytes:
            truncated = True
            break
        sections.append(section)
        total_bytes += section_bytes

    if not sections:
        return "(no python files)"

    result = "\n".join(sections)
    if truncated:
        result += f"\n... (reached {max_bytes} bytes limit, truncated)"
    return result


def _collect_python_files(root: Path, max_files: int) -> list[Path]:
    """递归收集 Python 文件，忽略 IGNORED_DIRS。"""
    files: list[Path] = []

    def walk(current: Path) -> None:
        if len(files) >= max_files:
            return
        try:
            children = sorted(
                current.iterdir(),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except (PermissionError, OSError):
            return

        for child in children:
            if len(files) >= max_files:
                return
            if child.is_dir():
                if child.name in IGNORED_DIRS:
                    continue
                walk(child)
            elif child.suffix in PY_EXTENSIONS:
                files.append(child)

    walk(root)
    return files


def _relative_to_repo(fpath: Path, root: Path) -> str:
    """计算相对于仓库根目录的路径。"""
    try:
        return fpath.relative_to(root).as_posix()
    except ValueError:
        return fpath.as_posix()


def _render_file(fpath: Path, rel: str) -> str:
    """渲染单个 Python 文件的符号列表。

    Args:
        fpath: 文件的绝对路径。
        rel: 用于显示的相对路径。

    Returns:
        渲染后的文本。解析失败或无符号时返回空字符串。
    """
    try:
        source = fpath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(fpath))
    except (OSError, SyntaxError):
        return f"# {rel}\n (resolve failed)\n"

    lines: list[str] = [rel]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            lines.append(f"  class {node.name}")
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines.append(f"    {_render_function(item)}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.append(f"  {_render_function(node)}")
        elif isinstance(node, ast.Assign):
            names = _assign_names(node)
            if names:
                lines.append(f"  {', '.join(names)}")

    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


def _render_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """渲染函数签名。"""
    args = _render_args(node.args)
    returns = _render_annotation(node.returns)
    if returns:
        return f"{node.name}({args}) -> {returns}"
    return f"{node.name}({args})"


def _render_args(args: ast.arguments) -> str:
    """渲染参数列表，只保留参数名。"""
    names: list[str] = []
    for arg in args.posonlyargs:
        names.append(arg.arg)
    if args.posonlyargs:
        names.append("/")
    for arg in args.args:
        names.append(arg.arg)
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    elif args.kwonlyargs:
        names.append("*")
    for arg in args.kwonlyargs:
        names.append(arg.arg)
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return ", ".join(names)


def _render_annotation(node: ast.expr | None) -> str:
    """把返回类型注解渲染为简短字符串。"""
    if node is None:
        return ""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Attribute):
        base = _render_annotation(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Subscript):
        base = _render_annotation(node.value)
        return f"{base}[...]"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _render_annotation(node.left)
        right = _render_annotation(node.right)
        return f"{left} | {right}"
    return "..."


def _assign_names(node: ast.Assign) -> list[str]:
    """提取赋值语句左侧的名字，用于显示常量或全局变量。"""
    names: list[str] = []
    for target in node.targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
    return names

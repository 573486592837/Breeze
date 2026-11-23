"""代码搜索工具。

提供 grep，优先使用 ripgrep，不可用时回退到 Python 实现。
两种实现支持相同的输出模式（content / files_with_matches / count），
content 模式可附带匹配行前后的上下文。
"""

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from breeze.config import settings
from breeze.tools.fs import (
    is_ignored,
    is_ignored_dir_name,
    load_gitignore_patterns,
)
from breeze.tools.registry import register
from breeze.tools.safety import ensure_readable, is_read_protected, safe_read_path

DEFAULT_MAX_RESULTS = 100
MAX_LINE_CHARS = 200
MAX_CONTEXT_LINES = 5
RG_TIMEOUT_SECONDS = 30
OUTPUT_MODES = ("content", "files_with_matches", "count")

_RG_LINE_RE = re.compile(r"^(.*?):(\d+):(.*)$")
_RG_COUNT_RE = re.compile(r"^(.*?):(\d+)$")
_RG_NULL_BODY_RE = re.compile(r"^(\d+)([:-])(.*)$")


def _skipped_ignored_dirs(root: Path) -> list[str]:
    """列出仓库根下存在但被跳过搜索的顶层目录。

    用于在根扫描结果末尾提示 Agent 这些目录确实存在、可用 ``path`` 进入，
    避免它们因为被过滤而从感知中彻底消失。

    Args:
        root: 仓库根目录。

    Returns:
        形如 ``node_modules/`` 的目录名列表，按名称排序。
    """
    patterns = load_gitignore_patterns(root)
    names: list[str] = []
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return names
    for child in children:
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if not is_dir or is_read_protected(child):
            continue
        if is_ignored_dir_name(child.name) or is_ignored(child, root, patterns):
            names.append(f"{child.name}/")
    return names


def _normalize_rg_path(path: str) -> str:
    """把 rg 输出的路径归一化为正斜杠并去掉 ./ 前缀。"""
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _normalize_rg_line(line: str) -> str:
    """把 rg 输出行的路径部分归一化为正斜杠。

    ripgrep 在 Windows 下输出 .\\src\\a.py 形式，与 Python 回退实现的
    src/a.py 不一致。统一为相对正斜杠路径，保证两种实现输出格式一致。

    Args:
        line: 形如 ``path:lineno:content`` 的一行输出。

    Returns:
        路径归一化后的行。无法解析行号时原样返回。
    """
    m = _RG_LINE_RE.match(line)
    if not m:
        return line
    return f"{_normalize_rg_path(m.group(1))}:{m.group(2)}:{m.group(3)}"


def _split_rg_null(line: str) -> tuple[str, str] | None:
    """拆分 rg --null 输出的路径与剩余部分。"""
    path, found, rest = line.partition("\x00")
    if not found:
        return None
    return path, rest


def _format_rg_content_line(line: str, prefix: str) -> str:
    """格式化 rg content 输出：匹配行用 ``:``，上下文行用 ``-``。"""
    if line == "--":
        return line
    parts = _split_rg_null(line)
    if parts is None:
        return _normalize_rg_line(line)
    path, rest = parts
    m = _RG_NULL_BODY_RE.match(rest)
    normalized = f"{prefix}{_normalize_rg_path(path)}"
    if not m:
        return f"{normalized}:{rest}"
    lineno, sep, content = m.group(1), m.group(2), m.group(3)
    return f"{normalized}{sep}{lineno}{sep}{content}"


def _format_rg_count_line(line: str, prefix: str) -> str:
    """格式化 rg count 输出为 ``path:count``。"""
    parts = _split_rg_null(line)
    if parts is not None:
        path, rest = parts
        return f"{prefix}{_normalize_rg_path(path)}:{rest.strip()}"
    m = _RG_COUNT_RE.match(line)
    if not m:
        return line
    return f"{prefix}{_normalize_rg_path(m.group(1))}:{m.group(2)}"


def _format_rg_file_line(line: str, prefix: str) -> str:
    """格式化 rg files-with-matches 输出为单个路径。"""
    parts = _split_rg_null(line)
    path = parts[0] if parts is not None else line
    return f"{prefix}{_normalize_rg_path(path)}"


@register(
    name="grep",
    description=(
        "在仓库或只读参考目录内搜索匹配正则表达式的行。"
        "output_mode 选择返回内容：content（默认，文件路径:行号:内容）、"
        "files_with_matches（只返回文件路径）、count（每个文件的匹配行数）。"
        "定位阶段建议用 files_with_matches 或 count 缩小范围，再对少数文件用 content。"
        "content 模式可用 context 附带匹配行前后各 N 行（0-5）。"
        "从仓库根搜索时遵循 .gitignore，结果末尾会提示被跳过的噪音目录；"
        "显式指定子目录时搜索其全部文件，起点即使属于噪音目录也会被搜索"
        "（其内部的噪音目录仍跳过）。"
        "定位符号后用 read_file 的 start_line/end_line 精确读取，比通读整个文件更省上下文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "正则表达式。",
            },
            "path": {
                "type": "string",
                "description": (
                    "搜索起始路径，相对于仓库根目录或只读参考目录。建议用 '.' 从仓库根搜索，配合 glob 限定范围。"
                ),
                "default": ".",
            },
            "glob": {
                "type": "string",
                "description": ("文件名过滤，如 '*.py'。强烈建议指定，如 '*.py' 或 '*.{js,ts}'，避免扫描无关文件。"),
            },
            "output_mode": {
                "type": "string",
                "enum": list(OUTPUT_MODES),
                "description": ("content：路径:行号:内容；files_with_matches：只返回文件路径；count：路径:匹配行数。"),
                "default": "content",
            },
            "context": {
                "type": "integer",
                "minimum": 0,
                "maximum": MAX_CONTEXT_LINES,
                "description": (f"content 模式下匹配行前后各显示的行数（0-{MAX_CONTEXT_LINES}）。默认为 0。"),
                "default": 0,
            },
            "max_results": {
                "type": "integer",
                "description": "结果上限：content 模式为匹配行数，files/count 模式为文件数。默认为 100。",
                "default": DEFAULT_MAX_RESULTS,
            },
        },
        "required": ["pattern"],
    },
)
def grep(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    output_mode: str = "content",
    context: int = 0,
) -> str:
    """在仓库内搜索匹配的行。

    Args:
        pattern: 正则表达式。
        path: 搜索起始路径，相对于仓库根目录或只读参考目录。
        glob: 可选的 glob 过滤，如 ``*.py``。
        max_results: 结果上限；content 模式按匹配行计，其余按文件计。
        output_mode: content / files_with_matches / count。
        context: content 模式下匹配行前后显示的行数，0 到 5。

    Returns:
        按 output_mode 格式化的结果字符串。无匹配时返回提示文本。

    Raises:
        ValueError: 路径越界、正则非法或 output_mode 非法。
    """
    if output_mode not in OUTPUT_MODES:
        raise ValueError(f"Invalid output_mode {output_mode!r}; choose from {', '.join(OUTPUT_MODES)}.")
    target = safe_read_path(path)
    ensure_readable(target)
    explicit_scope = target != settings.repo_root.resolve()
    max_results = max(1, int(max_results))
    context = max(0, min(MAX_CONTEXT_LINES, int(context)))

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern {pattern!r}: {exc}") from exc

    if shutil.which("rg"):
        lines, limit_hit = _grep_with_rg(pattern, target, glob, max_results, explicit_scope, output_mode, context)
    else:
        lines, limit_hit = _grep_with_python(regex, target, glob, max_results, explicit_scope, output_mode, context)

    skipped = [] if explicit_scope else _skipped_ignored_dirs(target)

    if not lines:
        result = "No matches found."
        if skipped:
            result += _skipped_note(skipped)
        return result

    budget = settings.context_tool_result_max_bytes
    out: list[str] = []
    used = 0
    dropped = 0
    for line in lines:
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " …[line truncated]"
        cost = len(line.encode("utf-8")) + 1
        if used + cost > budget:
            dropped = len(lines) - len(out)
            break
        out.append(line)
        used += cost

    result = "\n".join(out)
    if dropped:
        result += f"\n... (dropped {dropped} lines: output budget {budget} bytes reached)"
    elif limit_hit:
        result += f"\n... (max results reached: {max_results})"
    if skipped:
        result += _skipped_note(skipped)
    return result


def _skipped_note(skipped: list[str]) -> str:
    """渲染被跳过目录的提示行，最多列出 3 个。"""
    shown = ", ".join(skipped[:3])
    if len(skipped) > 3:
        shown += f" and {len(skipped) - 3} more"
    return f"\n(skipped ignored dirs: {shown}; pass path='<dir>' to search inside)"


def _grep_with_rg(
    pattern: str,
    target: Path,
    glob_pattern: str | None,
    max_results: int,
    explicit_scope: bool,
    output_mode: str,
    context: int,
) -> tuple[list[str], bool]:
    """使用 ripgrep 搜索。

    Args:
        pattern: 正则表达式，交由 rg 解释。
        target: 已校验的绝对路径。
        glob_pattern: 可选的文件名过滤。
        max_results: 结果上限；content 按匹配行计，其余按文件计。
        explicit_scope: 是否由调用方显式指定了子目录或参考目录。
        output_mode: content / files_with_matches / count。
        context: content 模式下匹配行前后的上下文行数。

    Returns:
        (结果行列表, 是否达到 max_results)。执行出错时返回单元素错误说明。
    """
    root = settings.repo_root.resolve()
    # 显式 scope 以 target 为工作目录执行：rg 的 --glob '!node_modules/'
    # 只匹配相对工作目录的路径，这样搜索起点本身（即使就叫 node_modules）
    # 不会被自己的排除规则挡掉，而后代同名目录仍会被跳过。
    base = target if target.is_dir() else target.parent
    search_arg = "." if target.is_dir() else target.name
    try:
        rel_base = base.relative_to(root).as_posix()
    except ValueError:
        rel_base = base.as_posix()
    prefix = "" if rel_base in ("", ".") else f"{rel_base.rstrip('/')}/"

    # --null 让路径与行号之间出现 NUL，避免上下文行的 '-' 与文件名中的
    # '-'/' :' 混淆；两种模式共用同一套路径归一化。
    cmd = ["rg", "--null", "--no-heading", "--color", "never"]
    if output_mode == "content":
        cmd.extend(["--line-number", "--with-filename"])
        if context > 0:
            cmd.extend(["-C", str(context)])
    elif output_mode == "files_with_matches":
        cmd.append("--files-with-matches")
    else:
        cmd.extend(["--count", "--with-filename"])
    if explicit_scope:
        # rg 默认遵循 .gitignore；显式指定范围时应看到被忽略的文件。
        cmd.append("--no-ignore")
    for ignored in settings.tools_ignore_dirs:
        cmd.extend(["--glob", f"!{ignored}/"])
    if target.is_dir():
        cmd.extend(["--glob", "!.env", "--glob", "!.env.*"])
    if glob_pattern:
        cmd.extend(["--glob", glob_pattern])
    cmd.extend(["--", pattern, search_arg])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(base),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RG_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return ["ripgrep timed out, please check the pattern or try a smaller scope."], False
    except OSError as exc:
        return [f"Failed to execute ripgrep: {exc}"], False

    if proc.returncode == 2:
        return [f"ripgrep error: {proc.stderr.strip()[:500]}"], False
    if proc.returncode not in (0, 1):
        return [f"ripgrep exited with code {proc.returncode}: {proc.stderr.strip()[:500]}"], False

    raw_lines = proc.stdout.strip().splitlines()
    if output_mode == "files_with_matches":
        # --files-with-matches 与 --null 组合时用 NUL 代替换行分隔记录，
        # 因此不能按行切分。
        entries = [chunk.strip("\r\n") for chunk in proc.stdout.split("\x00") if chunk.strip("\r\n")]
        lines = [_format_rg_file_line(entry, prefix) for entry in entries[:max_results]]
        return lines, len(entries) >= max_results
    if output_mode == "count":
        lines = [_format_rg_count_line(line, prefix) for line in raw_lines[:max_results]]
        return lines, len(raw_lines) >= max_results

    lines = []
    matches = 0
    for raw in raw_lines:
        if raw != "--":
            parts = _split_rg_null(raw)
            body = _RG_NULL_BODY_RE.match(parts[1]) if parts is not None else None
            if body is not None and body.group(2) == ":":
                if matches >= max_results:
                    break
                matches += 1
        lines.append(_format_rg_content_line(raw, prefix))
    return lines, matches >= max_results


def _grep_with_python(
    regex: re.Pattern[str],
    target: Path,
    glob_pattern: str | None,
    max_results: int,
    explicit_scope: bool,
    output_mode: str,
    context: int,
) -> tuple[list[str], bool]:
    """纯 Python 回退实现。

    Args:
        regex: 已编译的正则。
        target: 已校验的绝对路径。
        glob_pattern: 可选的文件名过滤。
        max_results: 结果上限；content 按匹配行计，其余按文件计。
        explicit_scope: 是否由调用方显式指定了子目录或参考目录。
        output_mode: content / files_with_matches / count。
        context: content 模式下匹配行前后的上下文行数。

    Returns:
        (结果行列表, 是否达到 max_results)。
    """
    root = settings.repo_root.resolve()
    patterns = [] if explicit_scope else load_gitignore_patterns(root)

    if target.is_file():
        files: list[Path] = [target]
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = [
                name
                for name in dirnames
                if not is_ignored_dir_name(name)
                and not is_read_protected(Path(dirpath) / name)
                and (explicit_scope or not is_ignored(Path(dirpath) / name, root, patterns))
            ]
            for name in filenames:
                if name.startswith(".env"):
                    continue
                fpath = Path(dirpath) / name
                if is_read_protected(fpath):
                    continue
                if not explicit_scope and is_ignored(fpath, root, patterns):
                    continue
                files.append(fpath)

    results: list[str] = []
    matches = 0
    for fpath in files:
        if glob_pattern and not fnmatch.fnmatch(fpath.name, glob_pattern):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = fpath.relative_to(root).as_posix()
        except ValueError:
            rel = fpath.as_posix()

        file_lines = text.splitlines()
        line_matches = [lineno for lineno, line in enumerate(file_lines, 1) if regex.search(line)]
        if not line_matches:
            continue

        if output_mode == "files_with_matches":
            results.append(rel)
        elif output_mode == "count":
            results.append(f"{rel}:{len(line_matches)}")
        else:
            selected = line_matches[: max(0, max_results - matches)]
            matches += len(selected)
            results.extend(_content_lines(rel, file_lines, selected, regex, context))

        if output_mode == "content":
            if matches >= max_results:
                break
        elif len(results) >= max_results:
            break

    if output_mode == "content":
        return results, matches >= max_results
    return results, len(results) >= max_results


def _content_lines(
    rel: str,
    file_lines: list[str],
    selected: list[int],
    regex: re.Pattern[str],
    context: int,
) -> list[str]:
    """渲染 content 模式的匹配行与上下文，合并重叠区间。"""
    ranges: list[tuple[int, int]] = []
    for lineno in selected:
        start = max(1, lineno - context)
        end = min(len(file_lines), lineno + context)
        if ranges and start <= ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))

    rendered: list[str] = []
    for start, end in ranges:
        for lineno in range(start, end + 1):
            line = file_lines[lineno - 1]
            sep = ":" if regex.search(line) else "-"
            rendered.append(f"{rel}{sep}{lineno}{sep}{line}")
    return rendered

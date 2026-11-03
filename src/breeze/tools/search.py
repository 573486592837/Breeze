"""代码搜索工具。

提供 grep，优先使用 ripgrep，不可用时回退到 Python 实现。
两种实现的输出格式一致：相对路径:行号:内容。
"""

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from breeze.config import settings
from breeze.tools.fs import (
    IGNORED_DIRS,
    IGNORED_PATTERNS,
    is_ignored,
    is_ignored_dir_name,
    load_gitignore_patterns,
)
from breeze.tools.registry import register
from breeze.tools.safety import ensure_readable, is_read_protected, safe_read_path

DEFAULT_MAX_RESULTS = 100
MAX_LINE_CHARS = 200
RG_TIMEOUT_SECONDS = 30

_RG_LINE_RE = re.compile(r"^(.*?):(\d+):(.*)$")


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
    path = m.group(1).replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    return f"{path}:{m.group(2)}:{m.group(3)}"


@register(
    name="grep",
    description=(
        "在仓库或只读参考目录内搜索匹配正则表达式的行。返回 文件路径:行号:内容 格式的结果。"
        "从仓库根搜索时遵循 .gitignore；显式指定子目录时搜索其全部文件"
        "（缓存目录仍跳过）。"
        "定位符号的首选工具：先用 grep 得到 文件:行号，再用 read_file 的 "
        "start_line/end_line 精确读取该范围，比通读整个文件更省上下文。"
        "建议用 path='.' 和合适的 glob（如 '*.py'、'*.js'、'*.ts'、'*.go'）"
        "这比猜测子目录更可靠。"
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
                    "搜索起始路径，相对于仓库根目录或只读参考目录。"
                    "建议用 '.' 从仓库根搜索，配合 glob 限定范围。"
                ),
                "default": ".",
            },
            "glob": {
                "type": "string",
                "description": ("文件名过滤，如 '*.py'。强烈建议指定，如 '*.py' 或 '*.{js,ts}'，避免扫描无关文件。"),
            },
            "max_results": {
                "type": "integer",
                "description": "结果行数上限。默认为 100。",
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
) -> str:
    """在仓库内搜索匹配的行。

    Args:
        pattern: 正则表达式。
        path: 搜索起始路径，相对于仓库根目录或只读参考目录。
        glob: 可选的 glob 过滤，如 ``*.py``。
        max_results: 结果行数上限，至少为 1。

    Returns:
        每行格式为 ``相对路径:行号:内容`` 的字符串。无匹配时返回提示文本。

    Raises:
        ValueError: 路径越界或正则非法。
    """
    target = safe_read_path(path)
    ensure_readable(target)
    explicit_scope = target != settings.repo_root.resolve()
    max_results = max(1, int(max_results))

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern: {pattern}") from exc

    if shutil.which("rg"):
        lines = _grep_with_rg(pattern, target, glob, max_results, explicit_scope)
    else:
        lines = _grep_with_python(regex, target, glob, max_results, explicit_scope)

    if not lines:
        return "No matches found."

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
    elif len(lines) >= max_results:
        result += f"\n... (max results reached: {max_results})"
    return result


def _grep_with_rg(
    pattern: str,
    target: Path,
    glob_pattern: str | None,
    max_results: int,
    explicit_scope: bool,
) -> list[str]:
    """使用 ripgrep 搜索。

    Args:
        pattern: 正则表达式，交由 rg 解释。
        target: 已校验的绝对路径。
        glob_pattern: 可选的文件名过滤。
        max_results: 结果上限。
        explicit_scope: 是否由调用方显式指定了子目录或参考目录。

    Returns:
        匹配行列表。执行出错时返回单元素列表，内容为错误说明。
    """
    root = settings.repo_root.resolve()
    try:
        rel_target = target.relative_to(root).as_posix()
    except ValueError:
        rel_target = target.as_posix()

    cmd = ["rg", "--line-number", "--no-heading", "--with-filename", "--color", "never"]
    if explicit_scope:
        # rg 默认遵循 .gitignore；显式指定范围时应看到被忽略的文件。
        cmd.append("--no-ignore")
    for ignored in IGNORED_DIRS:
        cmd.extend(["--glob", f"!{ignored}/"])
    for ignored in IGNORED_PATTERNS:
        cmd.extend(["--glob", f"!{ignored}/"])
    if target.is_dir():
        cmd.extend(["--glob", "!.env", "--glob", "!.env.*"])
    if glob_pattern:
        cmd.extend(["--glob", glob_pattern])
    cmd.extend(["--", pattern, rel_target])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RG_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return ["ripgrep timed out, please check the pattern or try a smaller scope."]
    except OSError as exc:
        return [f"Failed to execute ripgrep: {exc}"]

    if proc.returncode == 2:
        return [f"ripgrep error: {proc.stderr.strip()[:500]}"]
    if proc.returncode not in (0, 1):
        return [f"ripgrep exited with code {proc.returncode}: {proc.stderr.strip()[:500]}"]

    return [_normalize_rg_line(ln) for ln in proc.stdout.strip().splitlines()[:max_results]]


def _grep_with_python(
    regex: re.Pattern[str],
    target: Path,
    glob_pattern: str | None,
    max_results: int,
    explicit_scope: bool,
) -> list[str]:
    """纯 Python 回退实现。

    Args:
        regex: 已编译的正则。
        target: 已校验的绝对路径。
        glob_pattern: 可选的文件名过滤。
        max_results: 结果上限。
        explicit_scope: 是否由调用方显式指定了子目录或参考目录。

    Returns:
        匹配行列表。
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
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append(f"{rel}:{lineno}:{line}")
                if len(results) >= max_results:
                    return results
    return results

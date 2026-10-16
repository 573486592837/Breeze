"""代码编辑工具。

apply_patch 通过单次 search-replace 修改文件，
用于让 Agent 在受控范围内编辑代码。
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from breeze.config import settings
from breeze.tools.registry import register
from breeze.tools.safety import safe_path
from breeze.tools.shell import run_command

MAX_PATCH_BYTES = 20_000
MAX_PATCH_LINES = 200
MAX_DIFF_BYTES = 8_000

PROTECTED_PREFIXES = (".git/", "traces/", "reports/")
PROTECTED_FILES = frozenset({".env"})


def _is_protected(rel_path: str) -> bool:
    """判断相对路径是否被保护。

    Args:
        rel_path: 相对仓库根目录的路径，正斜杠分隔。

    Returns:
        若路径被保护则返回 True。
    """
    normalized = rel_path.lstrip("/")
    if normalized in PROTECTED_FILES:
        return True
    return any(normalized.startswith(p) for p in PROTECTED_PREFIXES)


def _is_git_repo(root: Path) -> bool:
    """检查 root 是否在 git 工作区内。

    Args:
        root: 要检查的目录。

    Returns:
        若是 git 仓库则返回 True。
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            shell=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


@register(
    name="git_diff",
    description=("返回工作区未提交的改动。用于确认自己修改了什么，或检查 patch 是否生效。可指定单个文件缩小范围。"),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "可选，限制到单个文件，相对于仓库根目录。",
            },
        },
        "required": [],
    },
)
def git_diff(path: str | None = None) -> str:
    """返回工作区未提交的改动。

    Args:
        path: 可选，限制到单个文件。

    Returns:
        成功时返回 unified diff 文本或以 ``(working tree clean)`` 开头。
        失败时以 ``Error:`` 开头。
    """
    root = settings.repo_root.resolve()

    if not _is_git_repo(root):
        return "Error: not a git repository"

    tokens = ["git", "diff", "--no-color", "--no-ext-diff", "--"]

    if path is not None:
        try:
            target = safe_path(path)
        except ValueError as exc:
            return f"Error: {exc}"

        try:
            rel = target.relative_to(root).as_posix()
        except ValueError:
            return f"Error: path escapes repository: {path}"

        tokens.append(rel)

    returncode, stdout, stderr = run_command(tokens, root, timeout=15)

    if returncode == -1:
        return f"Error: {stderr}"

    if returncode != 0:
        return f"Error: git diff failed: {stderr.strip()[:500]}"

    if not stdout.strip():
        return "(working tree clean)"

    encoded = stdout.encode("utf-8")
    if len(encoded) > MAX_DIFF_BYTES:
        truncated = encoded[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
        return truncated + f"\n... (diff truncated at {MAX_DIFF_BYTES} bytes)"

    return stdout


@register(
    name="apply_patch",
    description=("修改单个文件中的一处代码。old 必须与文件内容精确匹配，且在整个文件中唯一出现。不支持创建新文件。"),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径，相对于仓库根目录。",
            },
            "old": {
                "type": "string",
                "description": "要被替换的原文，需包含足够上下文以保证唯一。",
            },
            "new": {
                "type": "string",
                "description": "替换后的新内容。",
            },
        },
        "required": ["path", "old", "new"],
    },
)
def apply_patch(path: str, old: str, new: str) -> str:
    """修改单个文件中的一处代码。

    Args:
        path: 文件路径，相对于仓库根目录。
        old: 要被替换的原文，必须唯一匹配。
        new: 替换后的新内容。

    Returns:
        成功时以 ``Patched`` 开头，失败时以 ``Error:`` 开头。
        所有失败均为字符串返回，不抛异常。
    """
    # ─── 参数校验 ───
    if not old:
        return "Error: old text is empty"
    if old == new:
        return "Error: old and new are identical"
    if len(old.encode("utf-8")) > MAX_PATCH_BYTES:
        return f"Error: old text exceeds {MAX_PATCH_BYTES} bytes"
    if len(new.encode("utf-8")) > MAX_PATCH_BYTES:
        return f"Error: new text exceeds {MAX_PATCH_BYTES} bytes"

    old_lines = len(old.splitlines())
    new_lines = len(new.splitlines())
    if abs(new_lines - old_lines) > MAX_PATCH_LINES:
        return f"Error: patch is too large (>{MAX_PATCH_LINES} lines diff)"

    try:
        target = safe_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    root = settings.repo_root.resolve()
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        return f"Error: path escapes repository: {path}"

    if _is_protected(rel):
        return f"Error: path is protected: {rel}"

    if not target.exists():
        return f"Error: file not found: {path} (creating files is not supported yet)"
    if not target.is_file():
        return f"Error: not a file: {path}"

    try:
        with target.open("r", encoding="utf-8", errors="replace", newline="") as f:
            content = f.read()
    except OSError as exc:
        return f"Error: cannot read file: {exc}"

    newline = "\r\n" if "\r\n" in content else "\n"
    old_norm = old.replace("\r\n", "\n").replace("\n", newline)
    new_norm = new.replace("\r\n", "\n").replace("\n", newline)

    count = content.count(old_norm)
    if count == 0:
        return (
            f"Error: old text not found in {path}\n"
            f"Searched for:\n---\n{old}\n---\n"
            f"Hint: use read_file to verify the current content."
        )
    if count > 1:
        return f"Error: old text matches {count} locations in {path}, add more context to make it unique"

    new_content = content.replace(old_norm, new_norm, 1)

    tmp = target.parent / f".{target.name}.breeze_tmp"
    try:
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(new_content)
        tmp.replace(target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return f"Error: cannot write file: {exc}"

    return f"Patched {rel} (1 replacement)"

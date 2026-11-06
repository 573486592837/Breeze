"""代码编辑工具。

apply_patch 通过单次 search-replace 修改文件，
用于让 Agent 在受控范围内编辑代码。
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

from breeze.capabilities import is_git_repo, is_python_project
from breeze.config import settings
from breeze.tools.registry import register
from breeze.tools.safety import safe_path
from breeze.tools.shell import run_command

MAX_PATCH_BYTES = 20_000
MAX_PATCH_LINES = 200
MAX_CREATE_BYTES = 50_000
MAX_DIFF_BYTES = 8_000
MAX_TESTS_OUTPUT_BYTES = 8_000
# 全量测试通常比单文件慢得多（本仓库约 90 秒），60 秒会让 Agent 误判超时。
TESTS_TIMEOUT_SECONDS = 240

_PATTERN_RE = re.compile(r"^[A-Za-z0-9_./:=\-\[\]]+$")
_PASSED_RE = re.compile(r"(\d+) passed")
_FAILED_RE = re.compile(r"(\d+) failed")
_ERROR_RE = re.compile(r"(\d+) errors?")

PROTECTED_PREFIXES = (".git/",)


def _trace_dir_prefix() -> str | None:
    """返回配置的 trace 目录相对仓库根的路径前缀。

    只保护仓库内的 trace 目录；目录在仓库外或配置为仓库根时返回 None。
    """
    try:
        trace_dir = Path(settings.trace_dir).expanduser()
        if not trace_dir.is_absolute():
            trace_dir = settings.repo_root / trace_dir
        rel = trace_dir.resolve().relative_to(settings.repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None
    if rel in ("", "."):
        return None
    return f"{rel.rstrip('/')}/"


def _is_protected(rel_path: str) -> bool:
    """判断相对路径是否被保护。

    Args:
        rel_path: 相对仓库根目录的路径，正斜杠分隔。

    Returns:
        若路径被保护则返回 True。
    """
    normalized = rel_path.lstrip("/")
    name = normalized.rsplit("/", 1)[-1]
    if name.startswith(".env") and name not in {".env.example", ".env.sample"}:
        return True
    prefixes = PROTECTED_PREFIXES
    trace_prefix = _trace_dir_prefix()
    if trace_prefix is not None:
        prefixes = (*prefixes, trace_prefix)
    return any(normalized.startswith(p) for p in prefixes)


@register(
    name="git_diff",
    description=(
        "返回工作区未提交的改动。适用于查看多次修改后的整体变化，或回滚前确认当前状态。可指定单个文件缩小范围。"
    ),
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

    if not is_git_repo(root):
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
    name="git_restore",
    description=("回滚单个文件到最近一次提交的状态，丢弃该文件的所有未提交改动。仅支持 git 仓库。不可撤销。"),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要回滚的文件路径，相对于仓库根目录。",
            },
        },
        "required": ["path"],
    },
)
def git_resotre(path: str) -> str:
    """回滚单个文件到 HEAD 状态。

    Args:
        path: 要回滚的文件路径，相对于仓库根目录。

    Returns:
        成功时以 ``Restored`` 开头，失败时以 ``Error:`` 开头。
    """
    root = settings.repo_root.resolve()

    if not is_git_repo(root):
        return "Error: not a git repository"

    try:
        target = safe_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        return f"Error: path escapes repository: {path}"

    if _is_protected(rel):
        return f"Error: path is protected: {rel}"

    if not target.exists():
        return f"Error: file not found: {path}"

    if not target.is_file():
        return f"Error: not a file: {path}"

    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            shell=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return f"Error: git ls-files failed: {exc}"

    if proc.returncode != 0:
        return f"Error: file is not tracked by git: {rel}"

    returncode, stdout, stderr = run_command(["git", "checkout", "--", rel], root, timeout=10)

    if returncode == -1:
        return f"Error: {stderr}"
    if returncode != 0:
        return f"Error: git checkout failed: {stderr.strip()[:500]}"

    return f"Restored {rel} to last commit"


@register(
    name="apply_patch",
    description=(
        "修改单个文件中的一处代码。old 必须与文件内容精确匹配，且在整个文件中唯一出现。"
        "不支持创建新文件，创建请用 create_file。"
    ),
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
        return (
            f"Error: patch is too large (>{MAX_PATCH_LINES} lines diff). "
            "Split it into smaller apply_patch calls: replace the placeholder with "
            "the imports and first section, then patch in the remaining sections. "
            "Do not paste a whole new file into one call."
        )

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
        return f"Error: file not found: {path} (to create a new file, use create_file)"
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
        hint = ""
        if old.count("\n") > 10:
            hint = (
                f"\nHint: the old text is {old.count(chr(10))} lines long. "
                f"Try a smaller snippet (3-5 lines around the change)."
            )
        return (
            f"Error: old text not found in {path}\n"
            f"Searched for:\n---\n{old}\n---\n"
            f"Hint: re-read the target region with read_file now and copy old "
            f"verbatim from that output before retrying; do not rewrite old from "
            f"memory.{hint}"
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


@register(
    name="create_file",
    description=(
        "创建新文件并写入完整内容。path 必须尚不存在；目录会自动创建。修改已存在的文件请用 apply_patch，不支持覆盖。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "新文件路径，相对于仓库根目录。",
            },
            "content": {
                "type": "string",
                "description": "文件的完整内容。",
            },
        },
        "required": ["path", "content"],
    },
)
def create_file(path: str, content: str) -> str:
    """创建新文件并写入内容。

    Args:
        path: 新文件路径，相对于仓库根目录。
        content: 文件完整内容。

    Returns:
        成功时以 ``Created`` 开头，失败时以 ``Error:`` 开头。
        所有失败均为字符串返回，不抛异常。
    """
    if len(content.encode("utf-8")) > MAX_CREATE_BYTES:
        return f"Error: content exceeds {MAX_CREATE_BYTES} bytes"

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

    if target.exists():
        return f"Error: file already exists: {path}. Use apply_patch to modify it."
    if target.parent.exists() and not target.parent.is_dir():
        return f"Error: parent path is not a directory: {target.parent.name}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Error: cannot create directory: {exc}"

    tmp = target.parent / f".{target.name}.breeze_tmp"
    try:
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(content)
        tmp.replace(target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        return f"Error: cannot write file: {exc}"

    return f"Created {rel} ({len(content.encode('utf-8'))} bytes)"


@register(
    name="delete_path",
    description=(
        "删除仓库内的文件或目录。非空目录必须显式传 recursive=true。"
        "受保护路径（.git、.env* 与配置的 trace 目录）会被拒绝，仓库根目录不可删除。"
        "符号链接只删除链接本身，不会删除它指向的目标。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "待删除的路径，相对于仓库根目录。",
            },
            "recursive": {
                "type": "boolean",
                "description": "删除非空目录时设为 true。默认为 false。",
                "default": False,
            },
        },
        "required": ["path"],
    },
)
def delete_path(path: str, recursive: bool = False) -> str:
    """删除文件或目录。

    Args:
        path: 相对仓库根目录的路径。
        recursive: 非空目录必须为 True。

    Returns:
        成功时以 ``Deleted`` 开头，失败时以 ``Error:`` 开头。
    """
    root = settings.repo_root.resolve()
    try:
        target = safe_path(path)
    except ValueError as exc:
        return f"Error: {exc}"

    if target == root:
        return "Error: refusing to delete the repository root"

    raw = root / path
    try:
        raw_parent = raw.parent.resolve()
        raw_parent.relative_to(root)
    except ValueError:
        return f"Error: path out of bounds: {path}"

    if raw.is_symlink():
        link_rel = (raw_parent / raw.name).relative_to(root).as_posix()
        if _is_protected(link_rel):
            return f"Error: path is protected: {link_rel}"
        try:
            raw.unlink()
        except OSError as exc:
            return f"Error: cannot delete symlink: {exc}"
        return f"Deleted {link_rel} (symlink)"

    rel = target.relative_to(root).as_posix()
    if _is_protected(rel):
        return f"Error: path is protected: {rel}"

    if not target.exists():
        return f"Error: no such file or directory: {path}"

    try:
        if target.is_dir():
            if not recursive and any(target.iterdir()):
                return (
                    f"Error: directory not empty: {rel}. "
                    "Pass recursive=true to delete it."
                )
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
            return f"Deleted {rel} (directory)"
        target.unlink()
    except OSError as exc:
        return f"Error: cannot delete {rel}: {exc}"
    return f"Deleted {rel}"


def _test_python(root: Path) -> str:
    """选择运行 pytest 的解释器：优先仓库内虚拟环境，否则当前解释器。"""
    candidates = (
        root / ".venv" / "Scripts" / "python.exe",
        root / "venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _pytest_missing(output: str) -> bool:
    """判断输出是否是 pytest 模块缺失。"""
    return "No module named pytest" in output or "No module named 'pytest'" in output


@register(
    name="run_tests",
    description=(
        "运行 pytest 并返回结果摘要。"
        "可指定单个测试文件或节点 ID，如 'tests/test_fs.py' 或 'tests/test_fs.py::test_x'。"
        "省略 pattern 或传入空字符串时运行全部测试。"
        "不支持额外的 pytest 参数。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": ("可选的测试目标，如 'tests/test_fs.py'。省略或传空字符串时运行全部测试。"),
            },
        },
        "required": [],
    },
)
def run_tests(pattern: str | None = None) -> str:
    """运行 pytest 并返回结构化摘要。

    Args:
        pattern: 可选的测试目标，不含空格。

    Returns:
        成功时以 ``exit_code=`` 开头，包含 summary 和 failures。
        参数非法或执行失败时以 ``Error:`` 开头。
    """
    root = settings.repo_root.resolve()

    pattern_args: list[str] = []
    if pattern:
        if not _PATTERN_RE.match(pattern):
            return f"Error: invalid pattern characters: {pattern}"
        pattern_args.append(pattern)

    if not is_python_project(root):
        return (
            "Error: no Python project detected in this repository. "
            "run_tests only supports Python projects with pytest. "
            "Use run_shell to run your project's test command instead, "
            "for example 'npm test' or 'cargo test'."
        )

    python = _test_python(root)
    tokens = [python, "-m", "pytest", "-q", *pattern_args]
    returncode, stdout, stderr = run_command(tokens, root, timeout=TESTS_TIMEOUT_SECONDS)

    if _pytest_missing(stdout + stderr):
        # 当前解释器没装 pytest 时，退回 PATH 上的 pytest（常见于已激活的项目环境）。
        tokens = ["pytest", "-q", *pattern_args]
        returncode, stdout, stderr = run_command(tokens, root, timeout=TESTS_TIMEOUT_SECONDS)
        if _pytest_missing(stdout + stderr) or (returncode == -1 and "Command not found" in stderr):
            return (
                "Error: pytest is not available. Install pytest in the Python "
                f"environment used by run_tests ({python}), or use run_shell with "
                "your project's test command."
            )

    if returncode == -1:
        return f"Error: {stderr.strip()}"

    combined = stdout + "\n" + stderr
    summary = _parse_pytest_summary(combined)
    failures = _extract_pytest_failures(combined)

    lines: list[str] = [f"exit_code={returncode}", f"summary: {summary}"]
    if failures:
        lines.append("failures:")
        for f in failures:
            line = f"- {f['name']}"
            if f["detail"]:
                line += f" — {f['detail']}"
            lines.append(line)

    if returncode == 0:
        lines.append("status: verified — all tests pass. No further validation needed.")
    else:
        lines.append("status: failed — investigate the failing test, then fix the code.")
        section = _extract_failures_section(combined, max_bytes=3000)
        if section:
            lines.append("")
            lines.append("--- pytest traceback (truncated) ---")
            lines.append(section)
            lines.append("--- end traceback ---")

    output = "\n".join(lines)

    encoded = output.encode("utf-8")
    if len(encoded) > MAX_TESTS_OUTPUT_BYTES:
        truncated = encoded[:MAX_TESTS_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        output = truncated + "\n... (output truncated)"

    return output


def _parse_pytest_summary(text: str) -> str:
    """从 pytest 输出中提取通过/失败/错误计数。

    Args:
        text: pytest 输出文本。

    Returns:
        形如 ``148 passed, 2 failed, 1 error`` 的摘要。
        无法解析时返回 ``(no summary)``。
    """
    parts: list[str] = []

    if m := _PASSED_RE.search(text):
        parts.append(f"{m.group(1)} passed")
    if m := _FAILED_RE.search(text):
        parts.append(f"{m.group(1)} failed")
    if m := _ERROR_RE.search(text):
        parts.append(f"{m.group(1)} error")

    return ", ".join(parts) if parts else "(no summary)"


def _extract_pytest_failures(text: str) -> list[dict[str, str]]:
    """提取失败测试的节点 ID 和简要错误信息。

    Args:
        text: pytest 输出文本。

    Returns:
        list of {"name": str, "detail": str}。
        detail 从 FAILED 行的 " - " 后缀提取，可能为空。
    """
    failures: list[dict[str, str]] = []
    for line in text.splitlines():
        if not line.startswith("FAILED "):
            continue
        rest = line[len("FAILED ") :].strip()
        if " - " in rest:
            name, detail = rest.split(" - ", 1)
            failures.append({"name": name.strip(), "detail": detail.strip()[:300]})
        else:
            failures.append({"name": rest, "detail": ""})
    return failures


def _extract_failures_section(text: str, max_bytes: int = 3000) -> str:
    """从 pytest 输出提取 FAILURES 段并截断。

    Args:
        text: pytest 的 stdout + stderr。
        max_bytes: 返回内容的字节上限。

    Returns:
        FAILURES 段的文本。找不到时返回空字符串。
    """
    # pytest 8+ 用 "============ FAILURES ============"
    # 旧版本用 "================================== FAILURES ============================"
    markers = ("FAILURES", "==== FAILURES ====")
    start = -1
    for marker in markers:
        start = text.find(marker)
        if start >= 0:
            break
    if start < 0:
        return ""

    # 结束标志：short test summary 或下一个大分隔线
    end = text.find("short test summary", start)
    if end < 0:
        # 找 "=====" 大分隔线（至少 5 个 =）
        search_from = start + len(marker) + 5
        for i in range(search_from, len(text) - 4):
            if text[i : i + 5] == "=====":
                end = i
                break
    if end < 0:
        end = len(text)

    section = text[start:end].strip()
    if not section:
        return ""

    encoded = section.encode("utf-8")
    if len(encoded) > max_bytes:
        truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return truncated + "\n... (traceback truncated)"
    return section

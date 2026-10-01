"""Shell 执行工具。

run_shell 通过可执行文件白名单、子命令白名单、shell=False 和 cwd 限制，
在可控范围内为 Agent 提供命令执行能力。
"""

import shlex
import shutil
import subprocess

from breeze.tools.registry import register
from breeze.tools.safety import safe_path

DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_MAX_BYTES = 20_000

ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "git",
        "pytest",
        "rg",
        "python",
        "python3",
        "where",
    }
)

ALLOWED_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "branch",
        "rev-parse",
        "ls-files",
        "blame",
        "describe",
    }
)

ALLOWED_PYTHON_MODULES: frozenset[str] = frozenset({"pytest", "ruff", "unittest"})


@register(
    name="run_shell",
    description=(
        "在仓库内执行受限的 shell 命令。支持 git、pytest、rg、ls、cat 等只读命令，"
        "不支持管道、重定向和任意命令。返回 exit_code、stdout、stderr。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的命令字符串，如 'git status'。",
            },
            "cwd": {
                "type": "string",
                "description": "命令执行的工作目录，相对于仓库根目录。默认为 '.'。",
                "default": ".",
            },
            "timeout": {
                "type": "integer",
                "description": "命令执行超时时间（秒）。默认为 10。",
                "default": DEFAULT_TIMEOUT_SECONDS,
            },
            "max_bytes": {
                "type": "integer",
                "description": "stdout/stderr 最大字节数。默认为 20000。",
                "default": DEFAULT_MAX_BYTES,
            },
        },
        "required": ["command"],
    },
)
def run_shell(
    command: str,
    cwd: str = ".",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """执行受限命令并返回结果。

    Args:
        command: 命令行字符串，会被 shlex 拆分为参数列表。
        cwd: 工作目录，相对于仓库根目录。
        timeout: 超时秒数，至少为 1。
        max_bytes: 输出字节上限，至少为 1。

    Returns:
        包含 exit_code、stdout、stderr 的文本。

    Raises:
        ValueError: 命令为空、不在白名单、或 cwd 越界。
    """
    tokens = shlex.split(command, posix=True)
    if not tokens:
        raise ValueError("Command is empty.")

    _check_allowed(tokens)

    workdir = safe_path(cwd)
    if not workdir.is_dir():
        raise ValueError(f"Path is not a directory: {cwd}")

    timeout = max(1, int(timeout))
    max_bytes = max(1, int(max_bytes))

    try:
        proc = subprocess.run(
            tokens,
            cwd=str(workdir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return f"exit_code: -1\nstdout: \nstderr: Command timed out after {timeout} seconds."
    except FileNotFoundError:
        return f"exit_code: -1\nstdout: \nstderr: Command not found: {tokens[0]}"
    except OSError as exc:
        return f"exit_code: -1\nstdout: \nstderr: Failed to execute command: {exc}"

    return _render(proc.returncode, proc.stdout, proc.stderr, max_bytes)


def _check_allowed(tokens: list[str]) -> None:
    """校验命令是否在白名单内。

    Args:
        tokens: 拆分后的命令参数。

    Raises:
        ValueError: 可执行文件或子命令不在白名单。
    """
    exe = tokens[0].lower()
    # Windows 上 shlex.split(posix=False) 会保留引号，去除两侧引号
    exe = exe.strip("\"'")

    if exe not in ALLOWED_EXECUTABLES:
        raise ValueError(f"Executable not allowed: {exe}")

    if shutil.which(exe) is None:
        raise ValueError(f"Executable not found on PATH: {exe}")

    if exe == "git":
        if len(tokens) < 2:
            raise ValueError("Git command requires a subcommand.")
        sub = tokens[1].strip("\"'").lower()
        if sub not in ALLOWED_GIT_SUBCOMMANDS:
            raise ValueError(f"Git subcommand not allowed: {sub}")

    if exe in {"python", "python3"}:
        if len(tokens) < 3 or tokens[1] != "-m":
            raise ValueError("Python command must use '-m <module>'.")
        module = tokens[2].strip("\"'").lower()
        if module not in ALLOWED_PYTHON_MODULES:
            raise ValueError(f"Python module not allowed: {module}")


def _render(
    exit_code: int,
    stdout: str,
    stderr: str,
    max_bytes: int,
) -> str:
    """渲染执行结果。

    Args:
        exit_code: 进程返回码。
        stdout: 标准输出。
        stderr: 标准错误。
        max_bytes: 每个流的字节上限。

    Returns:
        结构化文本。
    """
    parts = [f"exit_code: {exit_code}"]
    stdout_trimmed, stdout_note = _truncate(stdout, max_bytes)
    stderr_trimmed, stderr_note = _truncate(stderr, max_bytes)

    if stdout_trimmed.strip():
        parts.append("--- stdout ---")
        parts.append(stdout_trimmed.rstrip())
        if stdout_note:
            parts.append(stdout_note)

    if stderr_trimmed.strip():
        parts.append("--- stderr ---")
        parts.append(stderr_trimmed.rstrip())
        if stderr_note:
            parts.append(stderr_note)

    if len(parts) == 1:
        parts.append("(no output)")
    return "\n".join(parts)


def _truncate(text: str, max_bytes: int) -> tuple[str, str]:
    """按字节截断文本。

    Args:
        text: 原始文本。
        max_bytes: 字节上限。

    Returns:
        (截断后文本, 提示文本)。未截断时提示为空字符串。
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, ""
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, f"[truncated to {max_bytes} bytes]"

"""Shell 执行工具。

run_shell 通过可执行文件白名单、子命令白名单、shell=False 和 cwd 限制，
在可控范围内为 Agent 提供命令执行能力。
"""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from breeze.tools.registry import register
from breeze.tools.safety import safe_path

DEFAULT_TIMEOUT_SECONDS = 60
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

FORBIDDEN_GIT_OPTIONS: frozenset[str] = frozenset(
    {
        "--no-index",
        "--exec-path",
        "-c",
        "--git-dir",
        "--work-tree",
        "--namespace",
    }
)

FORBIDDEN_RG_OPTIONS: frozenset[str] = frozenset(
    {
        "--pre",
        "--pre-glob",
        "--hostname-bin",
        "--sort",
        "--sortr",
        "--engine",
        "--follow",
        "-L",
        "--search-zip",
        "-z",
        "--decompress",
        "--unrestricted",
        "-u",
        "--no-ignore",
        "--no-ignore-parent",
        "--no-ignore-vcs",
        "--no-ignore-dot",
        "--no-ignore-global",
        "--no-ignore-files",
        "--type-add",
        "--type-clear",
    }
)


def _strip_quotes(token: str) -> str:
    """去掉 token 两端成对的引号。

    Args:
        token: 单个命令 token。

    Returns:
        去掉外层引号后的 token。无成对引号时原样返回。
    """
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _split_command(command: str) -> list[str]:
    """按平台选择分词模式，并统一去引号。

    Args:
        command: 原始命令行字符串。

    Returns:
        token 列表，已去引号。

    Raises:
        ValueError: 命令的引号不匹配或转义无效。
    """
    posix = os.name != "nt"
    try:
        raw = shlex.split(command, posix=posix)
    except ValueError as exc:
        raise ValueError(
            f"Unable to parse command: {exc}. "
            "The command may contain unbalanced quotes or invalid escapes. "
            "Prefer simple commands without nested quotes. "
            "For inline Python, use 'python -m <module>' instead of '-c'."
        ) from exc
    return [_strip_quotes(t) for t in raw]


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
                "description": "命令执行超时时间（秒）。默认为 60。",
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
    tokens = _split_command(command)
    if not tokens:
        raise ValueError("Command is empty.")

    _check_allowed(tokens)

    workdir = safe_path(cwd)
    if not workdir.is_dir():
        raise ValueError(f"Path is not a directory: {cwd}")

    timeout = max(1, int(timeout))
    max_bytes = max(1, int(max_bytes))

    returncode, stdout, stderr = run_command(tokens, workdir, timeout)
    if returncode == -1:
        return f"exit_code: -1\nstdout: \nstderr: {stderr}"
    return _render(returncode, stdout, stderr, max_bytes)


def run_command(
    tokens: list[str],
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """执行命令。不校验白名单，调用方负责。

    Args:
        tokens: 命令参数列表。
        cwd: 工作目录。
        timeout: 超时秒数。

    Returns:
        (returncode, stdout, stderr)。
        执行失败时 returncode 为 -1，stderr 包含错误信息。
    """
    try:
        proc = subprocess.run(
            tokens,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds."
    except FileNotFoundError:
        return -1, "", f"Command not found: {tokens[0]}"
    except OSError as exc:
        return -1, "", f"Failed to execute command: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


def _check_allowed(tokens: list[str]) -> None:
    """校验命令是否在白名单内。

    Args:
        tokens: 拆分后的命令参数。

    Raises:
        ValueError: 可执行文件或子命令不在白名单。
    """
    exe = tokens[0].lower()

    if exe not in ALLOWED_EXECUTABLES:
        allowed = ", ".join(sorted(ALLOWED_EXECUTABLES))
        extra = ""
        if exe in {"cd", "pushd", "popd"}:
            extra = " (cwd is set via the cwd parameter, not via cd)"
        elif exe in {"sh", "bash", "cmd", "powershell", "zsh", "pwsh"}:
            extra = " (shells are not allowed; commands run directly with shell=False)"
        elif exe in {"cat", "head", "tail", "less", "more", "tee"}:
            extra = " (file reading should use read_file; Breeze does not support file creation or shell redirection)"
        elif exe in {"echo", "printf"}:
            extra = (
                " (Breeze does not support file creation or shell redirection; "
                "use apply_patch to modify existing files)"
            )
        elif exe in {"rm", "del", "mv", "cp", "ln"}:
            extra = " (file modifications should use apply_patch)"
        raise ValueError(f"Executable not allowed: {exe}{extra}. Allowed commands: {allowed}.")

    if shutil.which(exe) is None:
        raise ValueError(f"Executable not found on PATH: {exe}")

    if exe == "git":
        if len(tokens) < 2:
            raise ValueError("Git command requires a subcommand.")
        sub = tokens[1].lower()
        if sub not in ALLOWED_GIT_SUBCOMMANDS:
            raise ValueError(f"Git subcommand forbidden: {sub}")

    if exe in {"python", "python3"}:
        if len(tokens) < 3 or tokens[1] != "-m":
            raise ValueError(
                "Python command must use '-m <module>'. To verify behavior, use run_tests with a specific test. "
            )
        module = tokens[2].strip("\"'").lower()
        if module not in ALLOWED_PYTHON_MODULES:
            raise ValueError(
                f"Python module not allowed: {module}. "
                f"Allowed: {sorted(ALLOWED_PYTHON_MODULES)}. "
                f"Use run_tests to run pytest."
            )

    _check_options(tokens, exe)
    _check_paths(tokens, exe)


def _check_options(tokens: list[str], exe: str) -> None:
    """检查命令的选项是否在黑名单内。

    Args:
        tokens: 拆分后的命令参数。
        exe: 已归一化的可执行文件名。

    Raises:
        ValueError: 使用了被禁止的选项。
    """
    if exe == "git":
        forbidden = FORBIDDEN_GIT_OPTIONS
    elif exe == "rg":
        forbidden = FORBIDDEN_RG_OPTIONS
    else:
        return

    for token in tokens[1:]:
        if not token.startswith("-"):
            continue
        opt = token.split("=", 1)[0]
        if opt in forbidden:
            raise ValueError(f"{exe} option is forbidden: {opt}")


def _check_paths(tokens: list[str], exe: str) -> None:
    """检查参数中的路径是否越界。

    跳过以 '-' 开头的选项。对 git 跳过子命令位置。

    Args:
        tokens: 拆分后的命令参数。
        exe: 已归一化的可执行文件名。

    Raises:
        ValueError: 参数中包含绝对路径或路径遍历序列。
    """
    start = 2 if exe == "git" else 1
    for token in tokens[start:]:
        if not token or token.startswith("-"):
            continue

        if _is_absolute_like(token):
            raise ValueError(f"Absolute paths are not allowed: {token}")

        segments = re.split(r"[/\\:]", token)
        if ".." in segments:
            raise ValueError(f"Path traversal is not allowed: {token}")


def _is_absolute_like(token: str) -> bool:
    """判断 token 是否表现为绝对路径。

    覆盖以下形式：
    - C:/foo、C:\\foo —— 标准 Windows 绝对路径
    - /etc/passwd     —— POSIX 根路径
    - \\\\server\\share —— UNC 路径
    - HEAD:C:/foo     —— rev:path 语法中嵌入盘符
    - HEAD:/etc/passwd —— rev:path 语法中嵌入根路径

    Args:
        token: 单个命令参数。

    Returns:
        若表现为绝对路径则返回 True。
    """
    if Path(token).is_absolute():
        return True
    if token.startswith("/") or token.startswith("\\"):
        return True
    # 任意位置出现盘符后跟分隔符
    if re.search(r"[A-Za-z]:[/\\]", token):
        return True
    # 冒号后紧跟分隔符，覆盖 rev:path 语法
    return bool(re.search(r":[/\\]", token))


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

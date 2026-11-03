"""Shell 执行工具。

run_shell 通过可执行文件白名单、子命令白名单、shell=False 和 cwd 限制，
在可控范围内为 Agent 提供命令执行能力。
"""

import locale
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from breeze.config import settings
from breeze.tools.registry import register
from breeze.tools.safety import safe_path

# 测试与构建命令可能耗时一至数分钟，60 秒会让 Agent 误判超时。
DEFAULT_TIMEOUT_SECONDS = 180
# stdout 与 stderr 各按此上限截断，两者合计不超过上下文工具结果预算，
# 保证 _compact_tool_result 兜底不被触发。
DEFAULT_MAX_BYTES = settings.context_tool_result_max_bytes // 2

ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "git",
        "node",
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


FORBIDDEN_NODE_OPTIONS: frozenset[str] = frozenset(
    {
        "-e",
        "--eval",
        "-p",
        "--print",
        "--input-type",
        "-r",
        "--require",
        "--import",
        "--loader",
        "--experimental-loader",
        "-i",
        "--interactive",
        "--inspect",
        "--inspect-brk",
        "--inspect-wait",
        "--inspect-port",
        "--watch",
        "--watch-path",
    }
)

# 短选项允许把值直接拼在后面（如 -e"code"），需要按前缀拦截。
FORBIDDEN_NODE_SHORT_OPTION = re.compile(r"^-[eipr].*")


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


SHELL_OPERATORS: frozenset[str] = frozenset(
    {"&&", "||", ";", "|", "&", ">", ">>", "<", "<<", "2>", "2>>", "2>&1", "&>"}
)


def _check_shell_operators(tokens: list[str]) -> None:
    """拒绝命令串联、管道与重定向。

    命令以 shell=False 执行，这些符号不会被解释，而是作为参数传给可执行
    文件，产生 exit 129 一类误导性错误（试跑中模型因此浪费了数步）。正则
    里的 ``|`` 只要还在引号内就不会被当作独立 token，不受影响。

    Args:
        tokens: 拆分后的命令参数。

    Raises:
        ValueError: 命令包含 shell 操作符。
    """
    for token in tokens:
        if token in SHELL_OPERATORS:
            raise ValueError(
                f"Shell operators are not supported: {token!r}. "
                "Run one command per call, e.g. 'git status --short' then "
                "'git log --oneline -5'."
            )
        if ";" in token:
            raise ValueError(
                "Shell command separators are not supported. Run one command per call."
            )


@register(
    name="run_shell",
    description=(
        "在仓库内执行受限的 shell 命令。支持 git、rg 等只读命令，以及 node（运行 JS 测试或脚本），"
        "不支持管道、重定向和任意命令。运行 Python 测试请用 run_tests（若当前环境提供）；"
        "JS 测试用 'node --test'；仅当需要特殊 pytest 参数时用 'python -m pytest'。"
        "返回 exit_code、stdout、stderr。"
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
                "description": f"命令执行超时时间（秒）。默认为 {DEFAULT_TIMEOUT_SECONDS}。",
                "default": DEFAULT_TIMEOUT_SECONDS,
            },
            "max_bytes": {
                "type": "integer",
                "description": f"stdout 与 stderr 各自的字节上限。默认为 {DEFAULT_MAX_BYTES}（工具结果预算的一半）。",
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
    tokens, workdir = _prepare(command, cwd, whitelist=True)

    timeout = max(1, int(timeout))
    max_bytes = max(1, int(max_bytes))

    returncode, stdout, stderr = run_command(tokens, workdir, timeout)
    return _format_result(returncode, stdout, stderr, max_bytes)


def check_command(command: str, cwd: str = ".") -> None:
    """按严格白名单校验命令，但不执行。

    供权限确认前预检使用；校验内容与 run_shell 一致。

    Args:
        command: 命令行字符串。
        cwd: 工作目录，相对于仓库根目录。

    Raises:
        ValueError: 命令为空、含 shell 操作符、不在白名单或 cwd 越界。
    """
    _prepare(command, cwd, whitelist=True)


def run_unlisted(
    command: str,
    cwd: str = ".",
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """跳过可执行文件白名单执行命令，仅供 open / confirm 模式使用。

    仍保留 shell 操作符、cwd 边界、超时与输出上限检查；命令始终以
    ``shell=False`` 执行。

    Args:
        command: 命令行字符串。
        cwd: 工作目录，相对于仓库根目录。
        timeout: 超时秒数，至少为 1。
        max_bytes: 输出字节上限，至少为 1。

    Returns:
        包含 exit_code、stdout、stderr 的文本。

    Raises:
        ValueError: 命令为空、含 shell 操作符或 cwd 越界。
    """
    tokens, workdir = _prepare(command, cwd, whitelist=False)

    timeout = max(1, int(timeout))
    max_bytes = max(1, int(max_bytes))

    returncode, stdout, stderr = run_command(tokens, workdir, timeout)
    return _format_result(returncode, stdout, stderr, max_bytes)


def _prepare(command: str, cwd: str, *, whitelist: bool) -> tuple[list[str], Path]:
    """分词并完成白名单、操作符与 cwd 校验。"""
    tokens = _split_command(command)
    if not tokens:
        raise ValueError("Command is empty.")

    _check_shell_operators(tokens)
    if whitelist:
        _check_allowed(tokens)

    workdir = safe_path(cwd)
    if not workdir.is_dir():
        raise ValueError(f"Path is not a directory: {cwd}")
    return tokens, workdir


def _format_result(returncode: int, stdout: str, stderr: str, max_bytes: int) -> str:
    """把执行结果渲染为工具输出。"""
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
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds."
    except FileNotFoundError:
        return -1, "", f"Command not found: {tokens[0]}"
    except OSError as exc:
        return -1, "", f"Failed to execute command: {exc}"
    return proc.returncode, decode_process_output(proc.stdout), decode_process_output(proc.stderr)


def decode_process_output(data: bytes | str) -> str:
    """解码子进程输出。

    优先按 UTF-8 解码，失败时回退到系统首选编码（Windows 上通常是
    cp936）。pytest 等工具在 Windows 会按控制台编码输出，直接按 UTF-8
    解码会把中文文档串变成替换字符。

    Args:
        data: 子进程的 stdout 或 stderr。

    Returns:
        解码后的文本。
    """
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            return data.decode("utf-8", errors="replace")


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
        if exe == "pytest":
            extra = " (use run_tests; for special pytest arguments use 'python -m pytest')"
        elif exe in {"npm", "npx", "yarn", "pnpm", "bun", "deno"}:
            extra = (
                " (package managers are not allowed; run JS tests with 'node --test'"
                " and project scripts with 'node <script>')"
            )
        elif exe in {"vitest", "jest", "mocha", "tsx", "ts-node"}:
            extra = " (use 'node --test' for JS tests; Breeze cannot invoke npm-installed test runners)"
        elif exe in {"cd", "pushd", "popd"}:
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
        elif exe in {"find", "ls", "dir", "tree"}:
            extra = " (use list_files to browse the repository; the grep tool locates code)"
        elif exe == "grep":
            extra = " (use the grep tool; it falls back to pure Python when ripgrep is unavailable)"
        raise ValueError(f"Executable not allowed: {exe}{extra}. Allowed commands: {allowed}.")

    if shutil.which(exe) is None:
        missing_hint = ""
        if exe == "rg":
            missing_hint = " (ripgrep is not installed here; use the grep tool instead, it needs no external binary)"
        elif exe == "node":
            missing_hint = " (Node.js is not installed here; JS tests and scripts cannot be run)"
        raise ValueError(f"Executable not found on PATH: {exe}{missing_hint}")

    if exe == "git":
        if len(tokens) < 2:
            raise ValueError("Git command requires a subcommand.")
        sub = tokens[1].lower()
        if sub not in ALLOWED_GIT_SUBCOMMANDS:
            raise ValueError(f"Git subcommand forbidden: {sub}")

    if exe == "node" and len(tokens) < 2:
        raise ValueError(
            "Node command requires a script or '--test'. "
            "Run JS tests with 'node --test' or 'node --test <test-file>'."
        )

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
    elif exe == "node":
        forbidden = FORBIDDEN_NODE_OPTIONS
    else:
        return

    for token in tokens[1:]:
        if not token.startswith("-"):
            continue
        opt = token.split("=", 1)[0]
        if opt in forbidden:
            raise ValueError(f"{exe} option is forbidden: {opt}")
        if exe == "node" and FORBIDDEN_NODE_SHORT_OPTION.match(token):
            raise ValueError(f"{exe} option is forbidden: {token}")


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

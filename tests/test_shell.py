"""shell 模块的单元测试。

所有执行类测试均通过 monkeypatch 替换 subprocess.run，
避免真实调用子进程，保证测试快速、稳定、与环境无关。
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from breeze.config import settings
from breeze.tools import registry, shell


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 settings.repo_root 指向临时目录。"""
    root = tmp_path.resolve()
    monkeypatch.setattr(settings, "repo_root", root)
    return root


@pytest.fixture
def whitelisted_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "breeze.tools.shell.shutil.which",
        lambda name: f"/fake/{name}",
    )


@pytest.fixture
def fake_run(
    monkeypatch: pytest.MonkeyPatch,
    whitelisted_which: None,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def _fake(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="hello\n", stderr="")

    monkeypatch.setattr("breeze.tools.shell.subprocess.run", _fake)
    return captured


# ─── 参数校验：命令解析 ───


def test_run_shell_empty_command(repo: Path) -> None:
    """空命令或纯空白应抛 ValueError。"""
    for cmd in ["", "   ", "\t\n"]:
        with pytest.raises(ValueError, match="Command is empty."):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_non_whitelisted_executable(repo: Path) -> None:
    """非白名单可执行文件应被拒。"""
    for cmd in ["curl http://example.com", "bash -c ls", "powershell Get_ChildItem"]:
        with pytest.raises(ValueError, match="Executable not allowed"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_unix_only_commands(repo: Path) -> None:
    """Windows 上无独立可执行文件的命令应被拒。"""
    for cmd in ["echo hello", "ls -la", "cat file.txt", "head -n 1 f", "tail -n 1 f", "wc -l f"]:
        with pytest.raises(ValueError, match="Executable not allowed"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_git_without_subcommand(repo: Path) -> None:
    """只有 'git' 无子命令应被拒。"""
    with pytest.raises(ValueError, match="Git command requires a subcommand."):
        registry.execute("run_shell", {"command": "git"})


def test_run_shell_rejects_git_write_subcommands(repo: Path) -> None:
    """git 写操作子命令应被拒。"""
    for cmd in ["git push", "git commit -m x", "git reset --hard", "git checkout main", "git clean -fd"]:
        with pytest.raises(ValueError, match="Git subcommand forbidden"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_allows_git_read_subcommands(repo: Path, fake_run: dict[str, Any]) -> None:
    """git 只读子命令应通过校验。"""
    for cmd in ["git status", "git diff", "git log", "git rev-parse HEAD"]:
        out = registry.execute("run_shell", {"command": cmd})
        assert "exit_code: 0" in out


def test_run_shell_rejects_python_without_dash_m(repo: Path) -> None:
    """python 未使用 -m 形式应被拒。"""
    for cmd in ["python script.py", "python -c 'print(1)'", "python", "python -v"]:
        with pytest.raises(ValueError, match="Python command must use '-m <module>'."):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_python_module_not_in_whitelist(repo: Path) -> None:
    """python -m 后模块不在白名单应被拒。"""
    for cmd in ["python -m http.server", "python -m pip install x", "python -m venv .venv"]:
        with pytest.raises(ValueError, match="Python module not allowed:"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_allows_python_whitelisted_modules(repo: Path, fake_run: dict[str, Any]) -> None:
    """python -m pytest / ruff / unittest 应通过校验。"""
    for cmd in ["python -m pytest -q", "python -m ruff check .", "python -m unittest discover"]:
        out = registry.execute("run_shell", {"command": cmd})
        assert "exit_code: 0" in out


def test_run_shell_rejects_bare_pytest(repo: Path) -> None:
    """裸 pytest 命令应被拒，拒绝消息指向 run_tests。"""
    with pytest.raises(ValueError, match="Executable not allowed: pytest"):
        registry.execute("run_shell", {"command": "pytest -q"})


def test_run_shell_bare_pytest_hint(repo: Path) -> None:
    """pytest 被拒时应提示使用 run_tests 或 python -m pytest。"""
    with pytest.raises(ValueError, match="run_tests"):
        registry.execute("run_shell", {"command": "pytest tests/test_fs.py"})
    with pytest.raises(ValueError, match="python -m pytest"):
        registry.execute("run_shell", {"command": "pytest tests/test_fs.py"})


# ─── 安全：node ───


def test_run_shell_allows_node_tests_and_scripts(
    repo: Path, fake_run: dict[str, Any]
) -> None:
    """node --test 与仓库内脚本应通过校验。"""
    for cmd in [
        "node --test",
        "node --test test/game.test.mjs",
        "node scripts/build.js",
        "node --version",
    ]:
        out = registry.execute("run_shell", {"command": cmd})
        assert "exit_code: 0" in out


def test_run_shell_rejects_bare_node(repo: Path, whitelisted_which: None) -> None:
    """无参数的 node 会进入 REPL，应被拒。"""
    with pytest.raises(ValueError, match="Node command requires a script"):
        registry.execute("run_shell", {"command": "node"})


def test_run_shell_rejects_node_inline_eval(
    repo: Path, whitelisted_which: None
) -> None:
    """node 的内联执行与模块加载选项可绕过仓库限制，必须被拒。"""
    for cmd in [
        'node -e "require(\'fs\').writeFileSync(\'x\', \'y\')"',
        "node --eval=1+1",
        'node -p "1+1"',
        "node --print=1+1",
        'node -ep "1"',
        "node -r ./hook.js test.js",
        "node --require=./hook.js test.js",
        "node --import=./hook.js test.js",
        "node --loader=./hook.mjs test.js",
    ]:
        with pytest.raises(ValueError, match="forbidden"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_node_debugger_and_watch(
    repo: Path, whitelisted_which: None
) -> None:
    """调试端口与 watch 模式应被拒。"""
    for cmd in [
        "node --inspect test.js",
        "node --inspect-brk=0.0.0.0:9229 test.js",
        "node --watch test.js",
    ]:
        with pytest.raises(ValueError, match="forbidden"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_node_path_escape(
    repo: Path, whitelisted_which: None
) -> None:
    """node 脚本参数同样受路径越界检查约束。"""
    with pytest.raises(ValueError, match="Path traversal"):
        registry.execute("run_shell", {"command": "node ../outside.js"})
    with pytest.raises(ValueError, match="Absolute paths"):
        registry.execute("run_shell", {"command": "node C:/Windows/x.js"})


def test_run_shell_rejects_package_managers_with_node_hint(repo: Path) -> None:
    """包管理器被拒时应提示用 node --test。"""
    for cmd in ["npm test", "npx vitest", "pnpm test", "yarn test"]:
        with pytest.raises(ValueError, match="node --test"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_missing_node_hint(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """node 未安装时应明确提示。"""
    monkeypatch.setattr("breeze.tools.shell.shutil.which", lambda _name: None)
    with pytest.raises(ValueError, match="Node.js is not installed"):
        registry.execute("run_shell", {"command": "node --test"})


# ─── 参数校验：cwd ───


def test_run_shell_rejects_cwd_escape(repo: Path) -> None:
    """cwd 越界应被 safe_path 拦截。"""
    for cwd in ["..", "../", "src/../../"]:
        with pytest.raises(ValueError, match="Path out of bounds:"):
            registry.execute("run_shell", {"command": "git status", "cwd": cwd})


def test_run_shell_rejects_cwd_not_dir(repo: Path) -> None:
    """cwd 指向普通文件应被拒。"""
    (repo / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Path is not a directory:"):
        registry.execute("run_shell", {"command": "git status", "cwd": "file.txt"})


# ─── 参数校验：可执行文件存在性 ───


def test_run_shell_rejects_missing_executable(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """白名单内但 PATH 中不存在的可执行文件应被拒。"""
    monkeypatch.setattr("breeze.tools.shell.shutil.which", lambda _name: None)
    with pytest.raises(ValueError, match="Executable not found"):
        registry.execute("run_shell", {"command": "git status"})


# ─── 执行路径：成功 ───


def test_run_shell_success_returns_stdout(repo: Path, fake_run: dict[str, Any]) -> None:
    """成功执行应返回 exit_code 与 stdout。"""
    out = registry.execute("run_shell", {"command": "git status"})

    assert "exit_code: 0" in out
    assert "hello" in out
    assert "--- stdout ---" in out


def test_decode_process_output_prefers_utf8() -> None:
    """UTF-8 输出按 UTF-8 解码。"""
    assert shell.decode_process_output("通过".encode()) == "通过"


def test_decode_process_output_falls_back_to_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 控制台编码输出的中文应能正确回退解码。"""
    monkeypatch.setattr(shell.locale, "getpreferredencoding", lambda _do_setlocale=True: "gbk")
    assert shell.decode_process_output("失败".encode("gbk")) == "失败"


def test_run_shell_uses_shell_false(repo: Path, fake_run: dict[str, Any]) -> None:
    """subprocess.run 必须以 shell=False 调用。"""
    registry.execute("run_shell", {"command": "git status"})

    assert fake_run["kwargs"]["shell"] is False
    assert fake_run["cmd"] == ["git", "status"]


def test_run_shell_passes_timeout(repo: Path, fake_run: dict[str, Any]) -> None:
    """timeout 参数应传递给 subprocess.run。"""
    registry.execute("run_shell", {"command": "git status", "timeout": 7})
    assert fake_run["kwargs"]["timeout"] == 7


def test_run_shell_cwd_resolved_to_repo(repo: Path, fake_run: dict[str, Any]) -> None:
    """cwd 应解析为仓库内的绝对路径。"""
    registry.execute("run_shell", {"command": "git status", "cwd": "."})
    assert Path(fake_run["kwargs"]["cwd"]) == repo


# ─── 执行路径：超时与错误 ───


def test_run_shell_timeout_returns_structured_message(
    repo: Path, whitelisted_which: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超时应返回结构化文本而非抛异常。"""

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr("breeze.tools.shell.subprocess.run", _raise)

    out = registry.execute("run_shell", {"command": "git status", "timeout": 1})
    assert "exit_code: -1" in out
    assert "Command timed out" in out


def test_run_shell_file_not_found_returns_message(
    repo: Path, whitelisted_which: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FileNotFoundError 应被包装为结构化文本。"""

    def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("not found")

    monkeypatch.setattr("breeze.tools.shell.subprocess.run", _raise)

    out = registry.execute("run_shell", {"command": "git status"})
    assert "exit_code: -1" in out


# ─── 执行路径：输出截断 ───


def test_run_shell_truncates_stdout(repo: Path, whitelisted_which: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """stdout 超过 max_bytes 应截断并提示。"""

    def _fake(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="x" * 500, stderr="")

    monkeypatch.setattr("breeze.tools.shell.subprocess.run", _fake)

    out = registry.execute("run_shell", {"command": "git status", "max_bytes": 100})
    assert "truncated" in out
    assert "x" * 500 not in out


def test_run_shell_truncates_stderr(repo: Path, whitelisted_which: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """stderr 超过 max_bytes 应截断并提示。"""

    def _fake(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="e" * 500)

    monkeypatch.setattr("breeze.tools.shell.subprocess.run", _fake)

    out = registry.execute("run_shell", {"command": "git status", "max_bytes": 100})
    assert "exit_code: 1" in out
    assert "--- stderr ---" in out
    assert "truncated" in out


def test_run_shell_no_output(repo: Path, whitelisted_which: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """无输出时应给出明确提示。"""

    def _fake(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("breeze.tools.shell.subprocess.run", _fake)

    out = registry.execute("run_shell", {"command": "git status"})
    assert "exit_code: 0" in out
    assert "no output" in out


# ─── 安全：rg 危险选项 ───


def test_run_shell_rejects_rg_pre(repo: Path, whitelisted_which: None) -> None:
    """rg --pre 可执行任意命令，必须被拒。"""
    for cmd in [
        "rg --pre=bash pattern .",
        "rg --pre bash pattern .",
        "rg --pre-glob=*.py --pre=cat pattern .",
    ]:
        with pytest.raises(ValueError, match="forbidden"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_rg_follow(repo: Path, whitelisted_which: None) -> None:
    """rg --follow 可跟随符号链接逃逸仓库。"""
    for cmd in ["rg --follow pattern .", "rg -L pattern ."]:
        with pytest.raises(ValueError, match="forbidden"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_rg_unrestricted(repo: Path, whitelisted_which: None) -> None:
    """rg 的 ignore 绕过选项应被拒。"""
    for cmd in [
        "rg -u pattern .",
        "rg --unrestricted pattern .",
        "rg --no-ignore pattern .",
        "rg --no-ignore-vcs pattern .",
    ]:
        with pytest.raises(ValueError, match="forbidden"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_rg_hostname_bin(repo: Path, whitelisted_which: None) -> None:
    """rg --hostname-bin 会执行外部命令。"""
    with pytest.raises(ValueError, match="forbidden"):
        registry.execute("run_shell", {"command": "rg --hostname-bin=calc pattern ."})


def test_run_shell_allows_safe_rg_options(repo: Path, fake_run: dict[str, Any]) -> None:
    """常用安全选项不应被误伤。"""
    for cmd in [
        "rg -n pattern .",
        "rg --line-number --no-heading pattern .",
        "rg -i pattern .",
        "rg --glob '*.py' pattern .",
        "rg --max-count 5 pattern .",
    ]:
        out = registry.execute("run_shell", {"command": cmd})
        assert "exit_code: 0" in out


# ─── 安全：git 危险选项 ───


def test_run_shell_rejects_git_no_index(repo: Path, whitelisted_which: None) -> None:
    """git diff --no-index 可读取仓库外任意文件。"""
    with pytest.raises(ValueError, match="forbidden"):
        registry.execute(
            "run_shell",
            {"command": "git diff --no-index a.txt b.txt"},
        )


def test_run_shell_rejects_git_config_injection(repo: Path, whitelisted_which: None) -> None:
    """git -c 可注入配置。"""
    with pytest.raises(ValueError, match="forbidden"):
        registry.execute(
            "run_shell",
            {"command": "git -c core.pager=calc log"},
        )


def test_run_shell_rejects_git_git_dir(repo: Path, whitelisted_which: None) -> None:
    """git --git-dir 可指向任意仓库。"""
    for cmd in [
        "git --git-dir=/tmp/other.git status",
        "git --work-tree=/ status",
    ]:
        with pytest.raises(ValueError, match="forbidden"):
            registry.execute("run_shell", {"command": cmd})


# ─── 安全：路径遍历与绝对路径 ───


def test_run_shell_rejects_parent_traversal(repo: Path, whitelisted_which: None) -> None:
    """参数中的 .. 路径遍历应被拒。"""
    for cmd in [
        "rg pattern ../outside",
        "rg pattern src/../../outside",
        "git show HEAD:../secret.txt",
        "git log ../",
    ]:
        with pytest.raises(ValueError, match="Path traversal"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_absolute_paths(repo: Path, whitelisted_which: None) -> None:
    """参数中的绝对路径应被拒。"""
    import os

    abs_path = "C:\\Windows\\System32" if os.name == "nt" else "/etc/passwd"
    for cmd in [
        f"rg pattern {abs_path}",
        f"git show HEAD:{abs_path}",
    ]:
        with pytest.raises(ValueError, match="Absolute paths"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_rejects_git_rev_with_absolute_path(repo: Path, whitelisted_which: None) -> None:
    """git 的 rev:path 语法中嵌入绝对路径应被拒。"""
    for cmd in [
        "git show HEAD:C:/Windows/System32",
        "git show HEAD:C:\\Windows\\System32",
        "git show HEAD:/etc/passwd",
    ]:
        with pytest.raises(ValueError, match="Absolute paths"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_allows_relative_paths(repo: Path, fake_run: dict[str, Any]) -> None:
    """仓库内相对路径不应被误伤。"""
    for cmd in [
        "rg pattern src/",
        "rg pattern src/breeze",
        "git log src/",
    ]:
        out = registry.execute("run_shell", {"command": cmd})
        assert "exit_code: 0" in out


def test_run_shell_unbalanced_quotes(repo: Path) -> None:
    """引号不配对应返回清晰的错误。"""
    with pytest.raises(ValueError, match="Unable to parse command"):
        registry.execute(
            "run_shell",
            {"command": 'git "unclosed'},
        )


def test_run_shell_unbalanced_quotes_message(
    repo: Path,
) -> None:
    """错误消息应包含有用的替代建议。"""
    with pytest.raises(ValueError, match="python -m <module>"):
        registry.execute(
            "run_shell",
            {"command": 'git "unclosed'},
        )


def test_run_shell_rejection_lists_allowed_commands(repo: Path) -> None:
    """非白名单命令的拒绝消息应列出允许的命令。"""
    with pytest.raises(ValueError, match="Allowed commands:"):
        registry.execute("run_shell", {"command": "cat file.txt"})


def test_run_shell_cd_hint(repo: Path) -> None:
    """cd 被拒时应提示使用 cwd 参数。"""
    with pytest.raises(ValueError, match="cwd parameter"):
        registry.execute("run_shell", {"command": "cd /tmp"})


def test_run_shell_cat_hint(repo: Path) -> None:
    """cat 被拒时应提示不支持文件创建。"""
    with pytest.raises(ValueError, match="file creation"):
        registry.execute("run_shell", {"command": "cat file.txt"})


def test_run_shell_ls_hint(repo: Path) -> None:
    """浏览类命令被拒时应提示改用 list_files。"""
    for cmd in ["ls", "find . -name '*.py'", "tree"]:
        with pytest.raises(ValueError, match="list_files"):
            registry.execute("run_shell", {"command": cmd})


def test_run_shell_grep_hint(repo: Path) -> None:
    """grep 被拒时应提示改用 grep 工具。"""
    with pytest.raises(ValueError, match="grep tool"):
        registry.execute("run_shell", {"command": "grep -rn foo ."})


def test_run_shell_missing_rg_hint(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rg 未安装时应提示改用 grep 工具。"""
    monkeypatch.setattr("breeze.tools.shell.shutil.which", lambda _name: None)
    with pytest.raises(ValueError, match="use the grep tool"):
        registry.execute("run_shell", {"command": "rg pattern ."})


def test_run_shell_missing_other_executable_has_no_grep_hint(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """其他可执行文件缺失时不应误加 grep 提示。"""
    monkeypatch.setattr("breeze.tools.shell.shutil.which", lambda _name: None)
    with pytest.raises(ValueError, match="Executable not found") as exc:
        registry.execute("run_shell", {"command": "git status"})
    assert "grep tool" not in str(exc.value)


def test_run_shell_default_cap_is_half_budget() -> None:
    """run_shell 双流各自截断，合计不超过工具结果预算。"""
    from breeze.tools.shell import DEFAULT_MAX_BYTES

    assert settings.context_tool_result_max_bytes // 2 == DEFAULT_MAX_BYTES

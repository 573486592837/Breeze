"""环境诊断命令。

默认只读：检查 Python、配置、密钥、模型、外部命令与目录权限，不发起网络
请求。``ping=True`` 时额外调用一次最小模型请求，用于验证 API Key、端点和
模型名，会产生少量费用。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from breeze import __version__
from breeze.capabilities import Capabilities, detect
from breeze.config import find_dotenv_path, settings
from breeze.llm import LLMClient

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


@dataclass(frozen=True)
class Check:
    """一条诊断结果。"""

    name: str
    status: str
    detail: str
    hint: str = ""


def _check_python() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if (sys.version_info.major, sys.version_info.minor) < (3, 11):
        return Check("Python", STATUS_FAIL, version, "Breeze 需要 Python 3.11 或更高版本。")
    return Check("Python", STATUS_OK, version)


def _check_repo() -> Check:
    root = settings.repo_root
    if not root.is_dir():
        return Check("仓库", STATUS_FAIL, str(root), "当前目录不存在，请先 cd 到仓库根目录。")
    if not os.access(root, os.W_OK):
        return Check("仓库", STATUS_FAIL, str(root), "当前目录不可写，Agent 无法修改文件。")
    return Check("仓库", STATUS_OK, str(root))


def _check_dotenv() -> Check:
    path = find_dotenv_path(Path.cwd())
    if path is not None:
        return Check(".env", STATUS_OK, f"已找到 {path}")
    return Check(
        ".env",
        STATUS_WARN,
        "未找到 .env",
        "可在仓库根目录复制 .env.example 为 .env，或直接设置环境变量。",
    )


def _check_api_key() -> Check:
    if settings.api_key.strip():
        return Check("API Key", STATUS_OK, f"{settings.api_key_env} 已设置")
    return Check(
        "API Key",
        STATUS_FAIL,
        f"{settings.api_key_env} 未设置",
        "写入 .env 或设置环境变量；不要把密钥提交到仓库。",
    )


def _check_model() -> Check:
    model = settings.model.strip()
    if not model:
        return Check("模型", STATUS_FAIL, "未设置", "在 breeze.toml 或 BREEZE_MODEL 中配置模型名。")
    if "siliconflow" in settings.api_base.lower() and not model.startswith("openai/"):
        return Check(
            "模型",
            STATUS_WARN,
            model,
            "OpenAI 兼容端点通常需要在模型名前加 openai/，例如 openai/deepseek-ai/...。",
        )
    return Check("模型", STATUS_OK, model)


def _check_api_base() -> Check:
    base = settings.api_base.strip()
    if not base:
        return Check("API Base", STATUS_WARN, "未设置", "将使用 provider 默认地址；自定义端点请配置 api_base。")
    parsed = urlparse(base)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return Check("API Base", STATUS_OK, base)
    return Check("API Base", STATUS_WARN, base, "地址应以 http:// 或 https:// 开头。")


def _writable_dir(path: Path) -> bool:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current.is_dir() and os.access(current, os.W_OK)


def _check_sessions() -> Check:
    path = settings.sessions_dir
    if _writable_dir(path):
        return Check("会话目录", STATUS_OK, str(path))
    return Check("会话目录", STATUS_WARN, str(path), "目录不可写，breeze chat 无法保存会话。")


def _check_traces() -> Check:
    path = Path(settings.trace_dir)
    if not path.is_absolute():
        path = Path.cwd() / path
    if _writable_dir(path):
        return Check("Trace 目录", STATUS_OK, str(path))
    return Check("Trace 目录", STATUS_WARN, str(path), "目录不可写，运行记录无法写入。")


def _check_git(caps: Capabilities) -> Check:
    if not caps.git:
        return Check("Git", STATUS_WARN, "未找到 git", "git_restore 不可用；安装 Git 后可启用回滚。")
    if not caps.git_repo:
        return Check(
            "Git",
            STATUS_WARN,
            "当前目录不是 git 工作区",
            "git_restore 不可用；建议在 git 仓库中使用，便于检查和回滚改动。",
        )
    return Check("Git", STATUS_OK, "可用（当前目录是 git 工作区）")


def _check_ripgrep(caps: Capabilities) -> Check:
    if caps.rg:
        return Check("ripgrep", STATUS_OK, "可用")
    return Check("ripgrep", STATUS_WARN, "未找到 rg", "grep 工具会回退到 Python 实现，速度较慢。")


def _check_python_project(caps: Capabilities) -> Check:
    if caps.python_project:
        return Check("Python 项目", STATUS_OK, "检测到 Python 项目，run_tests 可用")
    return Check("Python 项目", STATUS_OK, "未检测到 Python 项目，run_tests 不会下发")


def _check_ping() -> Check:
    started = time.perf_counter()
    try:
        client = LLMClient(timeout=20.0, connect_timeout=5.0, max_retries=0)
        response = client.chat(
            [{"role": "user", "content": "Reply with exactly: ok"}],
            temperature=0.0,
        )
    except Exception as exc:
        message = str(exc)
        if settings.api_key:
            message = message.replace(settings.api_key, "<redacted>")
        return Check(
            "模型连通性",
            STATUS_FAIL,
            message,
            "检查 API Key、api_base、模型名和网络；可先用 breeze doctor 查看配置。",
        )

    elapsed = time.perf_counter() - started
    detail = f"{client.model} · {elapsed:.1f}s · {response.total_tokens} tokens"
    if (response.content or "").strip():
        return Check("模型连通性", STATUS_OK, detail)
    return Check("模型连通性", STATUS_WARN, f"{detail} · 响应为空")


def run_checks(*, ping: bool = False) -> list[Check]:
    """执行全部诊断项。

    Args:
        ping: 是否额外发起一次最小模型请求。

    Returns:
        诊断结果列表，顺序固定。
    """
    caps = detect(Path.cwd())
    checks = [
        Check("Breeze", STATUS_OK, __version__),
        _check_python(),
        _check_repo(),
        _check_dotenv(),
        _check_api_key(),
        _check_model(),
        _check_api_base(),
        _check_git(caps),
        _check_ripgrep(caps),
        _check_python_project(caps),
        _check_sessions(),
        _check_traces(),
    ]
    if ping:
        checks.append(_check_ping())
    return checks

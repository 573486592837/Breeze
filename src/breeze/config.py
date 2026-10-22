"""Breeze 配置管理。

配置按优先级从低到高合并：

1. 代码默认值（DEFAULTS）
2. 用户级 ``~/.breeze/config.toml``
3. 仓库根 ``breeze.toml``
4. 环境变量 ``BREEZE_*``

安全参数（``ALLOWED_EXECUTABLES``、``FORBIDDEN_*_OPTIONS``、
``PROTECTED_PREFIXES``、补丁与输出大小限制等）以及工具 schema 中
声明的默认值不开放配置。它们属于防线，不应通过配置文件调整。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# 避免 LiteLLM 启动时尝试从 GitHub 拉取模型成本映射
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

load_dotenv()

DEFAULTS: dict[str, dict[str, Any]] = {
    "llm": {
        "model": "openai/Pro/deepseek-ai/DeepSeek-V3.2",
        "api_base": "https://api.siliconflow.cn/v1",
        "temperature": 0.0,
    },
    "agent": {
        "max_steps": 10,
        "max_edit_attempts": 5,
    },
    "context": {
        "enable": True,
        "max_recent_tokens": 3000,
        "min_recent_pairs": 1,
        "max_recent_pairs": 3,
        "arguments_preview": 60,
        "summary_findings_preview": 240,
        "summary_max_findings": 12,
        "tool_result_max_bytes": 16000,
        "tool_result_head_bytes": 8000,
        "tool_result_tail_bytes": 8000,
    },
    "trace": {
        "dir": "traces",
    },
}

ENV_MAP: dict[str, tuple[str, str]] = {
    "BREEZE_MODEL": ("llm", "model"),
    "BREEZE_API_BASE": ("llm", "api_base"),
    "BREEZE_TEMPERATURE": ("llm", "temperature"),
    "BREEZE_MAX_STEPS": ("agent", "max_steps"),
    "BREEZE_MAX_EDIT_ATTEMPTS": ("agent", "max_edit_attempts"),
    "BREEZE_CONTEXT_ENABLE": ("context", "enable"),
    "BREEZE_CONTEXT_MAX_RECENT_TOKENS": ("context", "max_recent_tokens"),
    "BREEZE_TRACE_DIR": ("trace", "dir"),
}

USER_CONFIG_PATH = Path.home() / ".breeze" / "config.toml"
REPO_CONFIG_NAME = "breeze.toml"


def _deep_copy(obj: dict[str, Any]) -> dict[str, Any]:
    return {section: dict(values) for section, values in obj.items()}


def _read_toml(path: Path) -> dict[str, Any]:
    """读取 TOML 文件。

    Args:
        path: 文件路径。

    Returns:
        解析后的 dict。文件不存在时返回空 dict。
        语法错误时抛出 ValueError。

    Raises:
        ValueError: TOML 语法错误。
    """
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {path}: {exc}") from exc


def _deep_update(base: dict[str, Any], overlay: dict[str, Any]) -> None:
    """把 overlay 合并进 base，原地修改。

    只合并 base 中已有的 section 和字段。overlay 中的未知键会被
    忽略，避免配置文件拼写错误导致难以排查的问题。

    Args:
        base: 目标 dict，原地修改。
        overlay: 来源 dict。
    """
    for section, values in overlay.items():
        if section not in base or not isinstance(values, dict):
            continue
        for key, value in values.items():
            if key in base[section]:
                base[section][key] = value


def _coerce(value: str, target_type: type) -> Any:
    """把环境变量字符串转为目标类型。"""
    if target_type is bool:
        return value.lower() in ("1", "true", "yes", "on")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _apply_env_overrides(cfg: dict[str, Any]) -> None:
    """应用 BREEZE_* 环境变量，原地修改 cfg。

    Raises:
        ValueError: 环境变量的值无法转换为目标类型。
    """
    for env_key, (section, field) in ENV_MAP.items():
        raw = os.getenv(env_key)
        if raw is None:
            continue
        target_type = type(DEFAULTS[section][field])
        try:
            cfg[section][field] = _coerce(raw, target_type)
        except ValueError as exc:
            raise ValueError(f"invalid value for {env_key}={raw!r}: {exc}") from exc


def load_config(repo_root: Path | None = None) -> dict[str, Any]:
    """按优先级加载并合并配置。

    Args:
        repo_root: 仓库根目录。None 时使用当前工作目录。

    Returns:
        合并后的配置 dict，结构与 DEFAULTS 一致。
    """
    merged = _deep_copy(DEFAULTS)

    _deep_update(merged, _read_toml(USER_CONFIG_PATH))

    root = (repo_root or Path.cwd()).resolve()
    _deep_update(merged, _read_toml(root / REPO_CONFIG_NAME))

    _apply_env_overrides(merged)

    return merged


class Settings:
    """从配置文件和環境变量读取运行参数。

    单例在模块 import 时按当前工作目录初始化。CLI 的 --repo 参数
    只修改 repo_root，不重新加载配置。若需对不同仓库使用不同配置，
    请先 cd 到目标仓库再启动 breeze。
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = (repo_root or Path.cwd()).resolve()
        cfg = load_config(self.repo_root)

        self.model: str = cfg["llm"]["model"]
        self.api_base: str = cfg["llm"]["api_base"]
        self.llm_temperature: float = float(cfg["llm"]["temperature"])
        self.api_key: str = os.getenv("SILICONFLOW_API_KEY", "")

        self.agent_max_steps: int = int(cfg["agent"]["max_steps"])
        self.agent_max_edit_attempts: int = int(cfg["agent"]["max_edit_attempts"])

        self.context_enable: bool = bool(cfg["context"]["enable"])
        self.context_max_recent_tokens: int = int(cfg["context"]["max_recent_tokens"])
        self.context_min_recent_pairs: int = int(cfg["context"]["min_recent_pairs"])
        self.context_max_recent_pairs: int = int(cfg["context"]["max_recent_pairs"])
        self.context_arguments_preview: int = int(cfg["context"]["arguments_preview"])
        self.context_summary_findings_preview: int = int(cfg["context"]["summary_findings_preview"])
        self.context_summary_max_findings: int = int(cfg["context"]["summary_max_findings"])
        self.context_tool_result_max_bytes: int = int(cfg["context"]["tool_result_max_bytes"])
        self.context_tool_result_head_bytes: int = int(cfg["context"]["tool_result_head_bytes"])
        self.context_tool_result_tail_bytes: int = int(cfg["context"]["tool_result_tail_bytes"])

        self.trace_dir: str = cfg["trace"]["dir"]

        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未设置，请检查 .env 文件或环境变量")

    def reload(self, repo_root: Path | None = None) -> None:
        """按新的 repo_root 重新加载配置。

        Args:
            repo_root: 新的仓库根目录。None 时使用当前工作目录。
        """
        self.__init__(repo_root)


settings = Settings()

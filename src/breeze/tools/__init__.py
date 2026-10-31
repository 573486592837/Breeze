"""Breeze 工具包。"""

from breeze.config import settings
from breeze.tools import edit, fs, registry, search, shell

# 关闭时不注册该工具：工具列表与提示保持一致，避免模型反复调用不存在的工具。
if settings.tools_repo_map:
    from breeze.tools import repo_map as _repo_map  # noqa: F401

__all__ = ["edit", "fs", "registry", "search", "shell"]

"""仓库级用户指引（AGENTS.md）。

用户在仓库根目录放一份 AGENTS.md，描述本项目的协作约定、常用命令与
代码风格。内容在会话启动时读取并拼进 system prompt，因此始终位于
上下文头部，不会被轮数累计或回合内压缩挤掉。

指引只补充行为约定：工具白名单、路径校验等安全限制由代码强制执行，
不因文件内容而放宽。
"""

from __future__ import annotations

from pathlib import Path

from breeze.config import settings

AGENTS_FILENAME = "AGENTS.md"
MAX_INSTRUCTIONS_CHARS = 8000
_TRUNCATED_MARKER = "\n... [AGENTS.md truncated]"


def load_project_instructions(root: Path | None = None) -> tuple[str, Path | None]:
    """读取仓库根目录的 AGENTS.md。

    Args:
        root: 仓库根目录，None 时使用 settings.repo_root。

    Returns:
        (指引正文, 文件路径)。文件不存在、为空或读取失败时正文为空字符串。
    """
    root = Path(root if root is not None else settings.repo_root).resolve()
    path = root / AGENTS_FILENAME
    if not path.is_file():
        return "", None

    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "", None
    if not text:
        return "", path

    if len(text) > MAX_INSTRUCTIONS_CHARS:
        keep = MAX_INSTRUCTIONS_CHARS - len(_TRUNCATED_MARKER)
        text = text[:keep].rstrip() + _TRUNCATED_MARKER
    return text, path


def render_project_instructions(text: str) -> str:
    """把指引正文包装成 system prompt 中的一段。"""
    return (
        f"[项目指引 — {AGENTS_FILENAME}]\n"
        "以下是用户放在仓库根目录的项目级要求。遵守这些约定，但与安全规则冲突时"
        "以安全规则为准；工具白名单与路径限制不受其影响。\n\n"
        f"{text.strip()}"
    )

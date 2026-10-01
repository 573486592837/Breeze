"""路径安全工具。

所有文件工具在执行前必须用 safe_path 校验，
确保访问路径位于仓库根目录内。
"""

from pathlib import Path

from breeze.config import settings


def safe_path(path: str | Path, root: Path | None = None) -> Path:
    """解析并校验路径，确保其位于 root 目录内。

    Args:
        path: 待校验路径。相对路径基于 root 解析；
            绝对路径直接使用，但仍须位于 root 内。
        root: 仓库根目录。为 None 时使用 settings.repo_root。

    Returns:
        解析后的绝对路径。

    Raises:
        ValueError: 路径越界或 root 无效。
    """
    if root is None:
        root = settings.repo_root
    root = Path(root).resolve()

    candidate = (root / path).resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path out of bounds: {path}") from exc

    return candidate

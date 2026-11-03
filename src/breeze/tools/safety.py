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


def safe_read_path(path: str | Path, root: Path | None = None) -> Path:
    """解析并校验只读路径，允许访问仓库根与配置的只读参考目录。

    写操作仍必须使用 ``safe_path``，因此参考目录只能读、不能改。

    Args:
        path: 待校验路径。相对路径基于仓库根解析。
        root: 仓库根目录。为 None 时使用 settings.repo_root。

    Returns:
        解析后的绝对路径。

    Raises:
        ValueError: 路径不在仓库根或任何只读参考目录内。
    """
    if root is None:
        root = settings.repo_root
    root = Path(root).resolve()

    candidate = (root / path).resolve()
    allowed_roots = (root, *(Path(extra).resolve() for extra in settings.tools_read_roots))
    for allowed in allowed_roots:
        try:
            candidate.relative_to(allowed)
        except ValueError:
            continue
        return candidate
    raise ValueError(f"Path out of bounds: {path}")

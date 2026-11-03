"""路径安全工具。

所有文件工具在执行前必须用 safe_path 校验，
确保访问路径位于仓库根目录内。
"""

from pathlib import Path

from breeze.config import settings

_ENV_TEMPLATE_FILES = frozenset({".env.example", ".env.sample"})


def is_read_protected(path: Path) -> bool:
    """判断路径是否禁止读取。

    ``.env`` 及其派生文件可能含密钥；``.git`` 内部数据也不应进入
    prompt。模板文件（``.env.example`` / ``.env.sample``）例外。

    Args:
        path: 已解析的绝对路径。

    Returns:
        命中保护规则时返回 True。
    """
    name = path.name
    if name.startswith(".env") and name not in _ENV_TEMPLATE_FILES:
        return True
    return ".git" in path.parts


def ensure_readable(path: Path) -> None:
    """读取前置检查。

    Raises:
        ValueError: 路径属于不可读保护范围。
    """
    if is_read_protected(path):
        raise ValueError(f"Path is protected: {path.name}")


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

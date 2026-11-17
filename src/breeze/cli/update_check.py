"""GitHub Releases 版本检查。

只读取最新 release 并提示，不下载、不执行任何升级命令。检查结果缓存
24 小时，避免每次启动都访问网络。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from breeze import __version__

REPO = "573486592837/Breeze"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TAGS_URL = f"https://api.github.com/repos/{REPO}/tags?per_page=30"
CACHE_PATH = Path.home() / ".breeze" / "update_check.json"
CACHE_TTL = 24 * 60 * 60
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class UpdateResult:
    """一次版本检查的结果。"""

    current: str = __version__
    latest: str = ""
    url: str = ""
    from_cache: bool = False
    error: str = ""

    @property
    def available(self) -> bool:
        """是否存在比当前版本更新的 release。"""
        current = _version_key(self.current)
        latest = _version_key(self.latest)
        return bool(current and latest and latest > current)


def _version_key(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _fetch_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"breeze/{__version__}",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _fetch_latest(timeout: float) -> tuple[str, str]:
    """查询 GitHub Releases API，返回 (版本号, 页面地址)。

    仓库还没有发布 Release 时兜底读取 tags，避免检查功能在仅有 tag 的
    阶段完全不可用。
    """
    try:
        data: dict[str, Any] = _fetch_json(LATEST_URL, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        tags = _fetch_json(TAGS_URL, timeout)
        names = [str(item.get("name") or "") for item in tags if isinstance(item, dict)]
        candidates = [name for name in names if _version_key(name) is not None]
        if not candidates:
            raise ValueError("no semantic version tag found") from exc
        tag = max(candidates, key=_version_key)
        return tag.lstrip("v"), f"https://github.com/{REPO}/releases/tag/{tag}"

    if data.get("draft") or data.get("prerelease"):
        raise ValueError("latest release is a draft or prerelease")
    tag = str(data.get("tag_name") or "").strip()
    if _version_key(tag) is None:
        raise ValueError(f"unexpected release tag: {tag!r}")
    url = str(data.get("html_url") or f"https://github.com/{REPO}/releases")
    return tag.lstrip("v"), url


def _load_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _save_cache(path: Path, latest: str, url: str, checked_at: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"checked_at": checked_at, "latest": latest, "url": url}),
            encoding="utf-8",
        )
    except OSError:
        return


def _short_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return message[:200] or exc.__class__.__name__


def check_for_update(
    *,
    force: bool = False,
    timeout: float = 2.0,
    cache_path: Path | None = None,
    now: float | None = None,
) -> UpdateResult:
    """检查是否有新 release。

    Args:
        force: 忽略缓存，强制查询网络。
        timeout: 网络超时秒数。
        cache_path: 缓存文件路径；默认 ``~/.breeze/update_check.json``。
        now: 当前时间戳，仅用于测试。

    Returns:
        检查结果。网络失败且无缓存时 ``error`` 非空、``latest`` 为空；
        有缓存时优先返回缓存结果并在 ``error`` 中保留失败原因。
    """
    path = CACHE_PATH if cache_path is None else cache_path
    timestamp = time.time() if now is None else now
    cached = _load_cache(path)

    if not force and cached is not None:
        checked_at = cached.get("checked_at")
        if isinstance(checked_at, (int, float)) and timestamp - checked_at < CACHE_TTL:
            return UpdateResult(
                latest=str(cached.get("latest") or ""),
                url=str(cached.get("url") or ""),
                from_cache=True,
            )

    try:
        latest, url = _fetch_latest(timeout)
        latest = latest.lstrip("v")
    except Exception as exc:
        error = _short_error(exc)
        if cached is not None and cached.get("latest"):
            return UpdateResult(
                latest=str(cached["latest"]),
                url=str(cached.get("url") or ""),
                from_cache=True,
                error=error,
            )
        return UpdateResult(error=error)

    _save_cache(path, latest, url, timestamp)
    return UpdateResult(latest=latest, url=url)

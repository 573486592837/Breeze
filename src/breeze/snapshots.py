"""跨回合工作集快照。

回合压缩会丢弃上一回合的 read_file 原文，只留下路径与行范围。模型在新回合
里要改同一个文件时，只能再次读取——M5 v3 中 4/5 个回合都重读了上一回合的
全部文件。这里在回合结束时把少量关键文件的**当前磁盘内容**存成快照，下一
回合启动时作为不可降级的 user 消息注入。

有界性由三层保证：

- 只保留 ``snapshot_max_files`` 个文件，按“本回合编辑 > 计划提及 > 跨回合
  读取频次 > 最近触碰”排序；
- 单文件内容与整块渲染都受 ``snapshot_max_chars`` 约束；
- 注入前用 stat（mtime_ns + size）校验，文件在回合间被改动就丢弃，避免把
  过期内容当成 apply_patch 的 old。

快照与回合归档一样落在会话旁的 sidecar 文件，``--resume`` 时可恢复。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from breeze.config import settings
from breeze.tools.safety import is_read_protected, safe_read_path

SNAPSHOT_VERSION = 1
_PLAN_PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9]{1,8}\b")
_MIN_RENDER_CHARS = 120

_HEADER = (
    "[Working set — file contents captured after the last change]\n"
    "These are current on-disk contents from earlier turns. Use them as the exact source "
    "for apply_patch `old`.\n"
    "Do not re-read a file listed here unless you need a range that is not shown."
)
_FOOTER = "[End working set — re-read only ranges that are not shown above]"


@dataclass
class FileSnapshot:
    """一个文件的回合间快照。

    Attributes:
        path: 相对仓库根的 POSIX 路径（仓库外文件为绝对 POSIX 路径）。
        content: 捕获时的文件内容（超长时按字符预算截断）。
        captured_turn: 捕获发生在第几个用户回合。
        source: 入选原因：edit / plan / frequent / recent。
        mtime_ns: 捕获时的 st_mtime_ns，用于注入前校验。
        size: 捕获时的 st_size。
        truncated: 内容是否被截断。
    """

    path: str
    content: str
    captured_turn: int = 0
    source: str = "recent"
    mtime_ns: int = 0
    size: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好的字典。"""
        return {
            "path": self.path,
            "content": self.content,
            "captured_turn": self.captured_turn,
            "source": self.source,
            "mtime_ns": self.mtime_ns,
            "size": self.size,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: Any) -> FileSnapshot | None:
        """从序列化字典恢复；字段不合法时返回 None。"""
        if not isinstance(data, dict):
            return None
        path = data.get("path")
        content = data.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return None
        return cls(
            path=path,
            content=content,
            captured_turn=int(data.get("captured_turn") or 0),
            source=str(data.get("source") or "recent"),
            mtime_ns=int(data.get("mtime_ns") or 0),
            size=int(data.get("size") or 0),
            truncated=bool(data.get("truncated")),
        )


def _normalize(path: str, root: Path) -> str | None:
    """把工具参数里的路径规范化为稳定的快照键。

    仓库内文件用相对 POSIX 路径，仓库外（只读参考目录）用绝对 POSIX 路径。
    越界或无法解析的路径返回 None。
    """
    try:
        resolved = safe_read_path(path, root)
    except (OSError, ValueError):
        return None
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def _normalize_many(paths: Iterable[str], root: Path) -> list[str]:
    """批量规范化并去重，保持输入顺序。"""
    seen: dict[str, None] = {}
    for path in paths:
        key = _normalize(path, root)
        if key is not None:
            seen.setdefault(key, None)
    return list(seen)


def _same_or_under(candidate: str, root: str) -> bool:
    """判断 candidate 是否等于 root 或位于 root 目录下。"""
    return candidate == root or candidate.startswith(root.rstrip("/") + "/")


def _matches_any(path: str, patterns: frozenset[str]) -> bool:
    """判断路径是否命中计划中的路径（允许只写文件名后缀）。"""
    return any(
        path == pattern or path.endswith("/" + pattern) or pattern.endswith("/" + path)
        for pattern in patterns
    )


def _plan_paths(plan: str, root: Path) -> frozenset[str]:
    """从计划文本中提取可能涉及的文件路径。"""
    found: set[str] = set()
    for match in _PLAN_PATH_RE.finditer(plan or ""):
        key = _normalize(match.group(0), root)
        if key is not None:
            found.add(key)
    return frozenset(found)


def select_paths(
    candidates: Iterable[str],
    *,
    edited: frozenset[str] = frozenset(),
    plan_paths: frozenset[str] = frozenset(),
    read_hits: Mapping[str, int] | None = None,
    order: Iterable[str] = (),
    limit: int = 4,
) -> list[str]:
    """按工作集优先级选出至多 limit 个文件。

    优先级：本回合编辑 > 计划提及 > 跨回合读取频次 > 最近触碰。
    只影响选择，不读取磁盘。

    Args:
        candidates: 候选快照键。
        edited: 本回合被编辑的路径。
        plan_paths: 计划文本中出现的路径。
        read_hits: 路径 -> 跨回合读取次数。
        order: 本回合触碰顺序（越靠后越新）。
        limit: 最多保留的文件数。

    Returns:
        选中的路径列表，按优先级从高到低。
    """
    hits = read_hits or {}
    recency = {path: idx for idx, path in enumerate(order)}
    scored: dict[str, int] = {}
    for path in candidates:
        if path in scored:
            continue
        score = 0
        if path in edited:
            score += 100
        if _matches_any(path, plan_paths):
            score += 30
        score += min(int(hits.get(path, 0)), 5) * 4
        if path in recency:
            score += recency[path] + 1
        scored[path] = score

    ranked = sorted(scored.items(), key=lambda item: (-item[1], item[0]))
    return [path for path, _ in ranked[:limit]]


class SnapshotStore:
    """有界的跨回合文件内容快照。"""

    def __init__(
        self,
        root: Path | None = None,
        max_files: int | None = None,
        max_chars: int | None = None,
    ) -> None:
        """初始化。

        Args:
            root: 仓库根目录，None 时使用 settings.repo_root。
            max_files: 最多保留的文件数，None 时使用配置值；0 表示禁用。
            max_chars: 单文件与整块渲染的字符预算，None 时使用配置值。
        """
        self.root = Path(root or settings.repo_root).resolve()
        self.max_files = (
            settings.context_snapshot_max_files if max_files is None else max_files
        )
        self.max_chars = (
            settings.context_snapshot_max_chars if max_chars is None else max_chars
        )
        self.files: dict[str, FileSnapshot] = {}

    @property
    def enabled(self) -> bool:
        """是否启用快照。"""
        return self.max_files > 0 and self.max_chars > 0

    def __bool__(self) -> bool:
        return bool(self.files)

    def clear(self) -> None:
        """清空全部快照。"""
        self.files.clear()

    def paths(self) -> list[str]:
        """返回当前快照路径，按渲染优先级。"""
        return list(self.files)

    def invalidate(self, path: str) -> None:
        """编辑或删除后丢弃同一路径（含目录下文件）的快照。"""
        key = _normalize(path, self.root)
        if key is None:
            return
        self.files = {
            item: snap
            for item, snap in self.files.items()
            if not _same_or_under(item, key)
        }

    def capture(
        self,
        paths: Iterable[str],
        *,
        turn: int,
        edited: Iterable[str] = (),
        plan: str = "",
        read_hits: Mapping[str, int] | None = None,
    ) -> None:
        """从磁盘捕获当前工作集。

        候选包括已有快照与本回合触碰过的路径；选中后重新读取磁盘，
        文件已删除或不可读时自然淘汰。

        Args:
            paths: 本回合触碰过的路径（读或写都算）。
            turn: 当前用户回合序号。
            edited: 本回合成功编辑的路径。
            plan: 当前计划文本，用于给提及的文件加权。
            read_hits: SessionState.read_hits，跨回合读取次数。
        """
        if not self.enabled:
            return

        touched = _normalize_many(paths, self.root)
        edited_keys = frozenset(_normalize_many(edited, self.root))
        candidates = [*self.files, *touched]
        order = [*self.files, *reversed(touched)]

        normalized_hits: dict[str, int] = {}
        for raw, count in (read_hits or {}).items():
            key = _normalize(str(raw), self.root)
            if key is not None:
                normalized_hits[key] = normalized_hits.get(key, 0) + int(count)

        # 上回合的快照若源于编辑，与新编辑同权，避免跨回合被读频次挤掉。
        prior_edits = {path for path, snap in self.files.items() if snap.source == "edit"}
        selected = select_paths(
            candidates,
            edited=edited_keys | frozenset(prior_edits),
            plan_paths=_plan_paths(plan, self.root),
            read_hits=normalized_hits,
            order=order,
            limit=self.max_files,
        )

        refreshed: dict[str, FileSnapshot] = {}
        for key in selected:
            snap = self._read(key, turn)
            if snap is None:
                continue
            if key in edited_keys:
                snap.source = "edit"
            elif _matches_any(key, _plan_paths(plan, self.root)):
                snap.source = "plan"
            elif normalized_hits.get(key, 0) >= 2:
                snap.source = "frequent"
            else:
                snap.source = "recent"
            refreshed[key] = snap
        self.files = refreshed

    def render(self) -> str:
        """渲染工作集注入块；没有可用快照时返回空字符串。

        逐文件用 stat 校验，回合间被改动的文件不会进入提示词。
        """
        if not self.enabled or not self.files:
            return ""

        budget = self.max_chars - len(_HEADER) - len(_FOOTER) - 2
        blocks: list[str] = []
        used = 0
        marker = "\n... [truncated to fit working-set budget]\n"
        for path, snap in self.files.items():
            current = self._stat(path)
            if current is None or current != (snap.mtime_ns, snap.size):
                continue
            header = self._file_header(path, snap)
            # +1 是 join 时的换行；预留截断标记的空间。
            remaining = budget - used - len(header) - 1 - len(marker)
            if remaining < _MIN_RENDER_CHARS:
                break
            content = snap.content
            cut = len(content) > remaining
            if cut:
                content = _clip_lines(content, remaining)
            if content and not content.endswith("\n"):
                content += "\n"
            block = header + content + (marker if cut else "")
            blocks.append(block)
            used += len(block) + 1
            if cut:
                break

        if not blocks:
            return ""
        return "\n".join([_HEADER, *blocks, _FOOTER])

    def to_dict(self) -> dict[str, Any]:
        """序列化为 sidecar JSON。"""
        return {
            "version": SNAPSHOT_VERSION,
            "files": [snap.to_dict() for snap in self.files.values()],
        }

    def restore(self, data: Any) -> None:
        """从 sidecar 数据恢复快照；坏数据按空处理。"""
        self.files.clear()
        if not isinstance(data, dict):
            return
        items = data.get("files")
        if not isinstance(items, list):
            return
        for raw in items:
            snap = FileSnapshot.from_dict(raw)
            if snap is not None:
                self.files[snap.path] = snap

    @classmethod
    def from_dict(cls, data: Any, **kwargs: Any) -> SnapshotStore:
        """按 sidecar 数据构造 SnapshotStore。"""
        store = cls(**kwargs)
        store.restore(data)
        return store

    def _read(self, path: str, turn: int) -> FileSnapshot | None:
        """读取磁盘上的当前内容；不可读、越界或二进制时返回 None。"""
        try:
            resolved = safe_read_path(path, self.root)
        except (OSError, ValueError):
            return None
        if not resolved.is_file() or is_read_protected(resolved):
            return None
        try:
            with resolved.open("r", encoding="utf-8", errors="replace") as f:
                text = f.read(self.max_chars + 1)
            stat = resolved.stat()
        except OSError:
            return None
        if "\x00" in text[:1000]:
            return None
        truncated = len(text) > self.max_chars
        if truncated:
            text = _clip_lines(text[: self.max_chars], self.max_chars)
        return FileSnapshot(
            path=path,
            content=text,
            captured_turn=turn,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            truncated=truncated,
        )

    def _stat(self, path: str) -> tuple[int, int] | None:
        """返回文件当前的 (mtime_ns, size)；不存在时返回 None。"""
        try:
            resolved = safe_read_path(path, self.root)
            stat = resolved.stat()
        except (OSError, ValueError):
            return None
        if not resolved.is_file():
            return None
        return stat.st_mtime_ns, stat.st_size

    @staticmethod
    def _file_header(path: str, snap: FileSnapshot) -> str:
        """生成单文件分隔头。"""
        lines = snap.content.count("\n") + (1 if snap.content and not snap.content.endswith("\n") else 0)
        parts = [f"turn {snap.captured_turn}", f"{lines} lines"]
        if snap.truncated:
            parts.append(f"truncated at {len(snap.content)} chars")
        return f"--- {path} ({', '.join(parts)}) ---\n"


def _clip_lines(text: str, limit: int) -> str:
    """按字符上限截断，尽量停在完整行边界。"""
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    newline = clipped.rfind("\n")
    if newline > limit // 2:
        clipped = clipped[:newline]
    return clipped

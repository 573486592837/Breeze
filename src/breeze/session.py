"""会话持久化。

SessionStore 把一次 `breeze chat` 会话写成 append-only 的 JSONL：
每个回合结束时落盘该回合的问答归档、触碰文件、Token 摘要与状态快照。

resume 时同时重建 TurnLedger 与 SessionState：前者提供上一回合的结论，
后者提供符号索引与读取范围。只恢复问答结论会让接续后的第一句追问
重新通读文件——那正是单回合内已验证过的失忆成本。

工作集快照单独存放在 `<session>.snapshots.json` sidecar：它体积大、
可整体失效，和 append-only 的事件流分开写更安全，也不会进入 --list。

选择 JSONL 而非 pickle/SQLite：与 trace 同族、零依赖、人可读，
损坏也只坏一行。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from breeze.config import settings
from breeze.state import SessionState, TurnLedger, TurnRecord

LATEST = "latest"


def _default_dir() -> Path:
    """未指定目录时的会话根目录。"""
    return Path(settings.sessions_dir)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_session_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}_{uuid.uuid4().hex[:6]}"


@dataclass
class SessionInfo:
    """会话列表用的摘要。

    Attributes:
        session_id: 会话 ID，也是文件名主干。
        path: JSONL 文件路径。
        created_at: 建会话时间（ISO8601）。
        repo_root: 会话绑定的仓库根目录。
        model: 会话使用的模型名。
        turns: 已归档的回合数。
        first_question: 首问摘要，供人辨认会话。
        total_tokens: 各回合 Token 合计。
    """

    session_id: str
    path: Path
    created_at: str
    repo_root: str
    model: str
    turns: int
    first_question: str
    total_tokens: int


def _resolve_dir(session_dir: str | Path | None) -> Path:
    """解析会话目录，None 时使用配置值。"""
    return Path(session_dir).expanduser() if session_dir else _default_dir()


class SessionStore:
    """一个会话的 JSONL 存储。

    Usage:
        store = SessionStore(repo_root=repo, model=llm.model)
        store.record_turn(agent.ledger.records[-1], agent.state, trace.summary())

        store = SessionStore.resume(LATEST)
        ledger, state = store.load()
    """

    def __init__(
        self,
        session_dir: str | Path | None = None,
        session_id: str | None = None,
        repo_root: str | Path = "",
        model: str = "",
    ) -> None:
        """绑定会话文件，文件直到第一个回合落盘时才创建。

        只 /help、直接退出、或跑空的会话不会留下 0 回合的垃圾文件——
        它们会污染 --list，也会被 --resume latest 选中。

        Args:
            session_dir: 会话目录，None 时使用 sessions.dir 配置。
            session_id: 会话 ID，None 时生成"时间戳_短哈希"。
            repo_root: 写入 session_start 头事件的仓库根目录。
            model: 写入 session_start 头事件的模型名。
        """
        self.session_dir = _resolve_dir(session_dir)
        self.session_id = session_id or _new_session_id()
        self.path = self.session_dir / f"{self.session_id}.jsonl"
        self.snapshots_path = self.session_dir / f"{self.session_id}.snapshots.json"
        self._repo_root = str(repo_root)
        self._model = model
        self._pending_config: dict[str, Any] = {}

    @classmethod
    def resume(
        cls,
        session_id: str | None = LATEST,
        session_dir: str | Path | None = None,
    ) -> SessionStore:
        """接续既有会话继续追加写入。

        Args:
            session_id: 会话 ID；None 或 "latest" 时取最近一个。
            session_dir: 会话目录，None 时使用默认。

        Returns:
            绑定到该会话文件的 SessionStore。

        Raises:
            FileNotFoundError: 目录为空或指定 ID 不存在。
        """
        path = _resolve_path(session_dir, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(session_dir=path.parent, session_id=path.stem)

    def record_turn(self, record: TurnRecord, state: SessionState, summary: dict[str, Any]) -> None:
        """归档一个回合：问答结论、触碰文件、Token 摘要与状态快照。

        Args:
            record: 该回合的 TurnRecord。
            state: 该回合结束时的 SessionState。
            summary: 该回合 trace 的 summary()。
        """
        self._ensure_started()
        self._append(
            {
                "event": "turn",
                "ts": _now_iso(),
                "trace_path": summary.get("trace_path", ""),
                "llm_calls": summary.get("llm_calls", 0),
                "tool_calls": summary.get("tool_calls", 0),
                "total_tokens": summary.get("total_tokens", 0),
                "state": state.to_dict(),
                **asdict(record),
            }
        )

    def record_config(self, **changes: Any) -> None:
        """记录运行时配置变更（模型、trace 前缀、参数等）。

        会话文件尚未创建时先暂存，等首个回合写出 session_start 后补写；
        这样只跑过 /model 就退出的会话不会留下空文件。

        Args:
            **changes: 配置键值，如 model="..."、trace_prefix="..."。
        """
        if not changes:
            return
        self._pending_config.update(changes)
        if self.path.exists():
            merged = dict(self._pending_config)
            self._pending_config.clear()
            self._append_config(merged)

    def saved_config(self) -> dict[str, Any]:
        """读回最后一个 config 事件合并后的运行配置。

        Returns:
            配置 dict；没有 config 事件时为空 dict。
        """
        config: dict[str, Any] = {}
        if not self.path.exists():
            return config
        for event in self._events():
            if event.get("event") == "config":
                config.update({k: v for k, v in event.items() if k not in ("event", "ts")})
        return config

    def record_reset(self) -> None:
        """记录 /new：resume 时该事件之前的回合与状态不参与恢复。

        会话还没有落盘过任何回合时不写文件——空会话不留痕；工作集快照
        sidecar 一并删除。
        """
        self.snapshots_path.unlink(missing_ok=True)
        if self.path.exists():
            self._append({"event": "reset", "ts": _now_iso()})

    def load_snapshots(self) -> dict[str, Any]:
        """读回工作集快照 sidecar；不存在或损坏时返回空 dict。"""
        if not self.snapshots_path.is_file():
            return {}
        try:
            data = json.loads(self.snapshots_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_snapshots(self, data: dict[str, Any]) -> None:
        """原子写入工作集快照 sidecar；没有文件时删除旧 sidecar。

        Args:
            data: SnapshotStore.to_dict() 的结果。
        """
        files = data.get("files") if isinstance(data, dict) else None
        if not files:
            self.snapshots_path.unlink(missing_ok=True)
            return
        self.session_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshots_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.snapshots_path)

    def load(self) -> tuple[TurnLedger, SessionState]:
        """读回会话，重建回合归档与会话状态。

        只认最后一个 reset 之后的内容，损坏行跳过。

        Returns:
            (TurnLedger, SessionState)。
        """
        ledger = TurnLedger()
        state = SessionState()
        if not self.path.exists():
            return ledger, state

        for event in self._events():
            kind = event.get("event")
            if kind == "reset":
                ledger.records.clear()
                state = SessionState()
            elif kind == "turn":
                clarifications: list[tuple[str, str]] = []
                for item in event.get("clarifications") or []:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        clarifications.append((str(item[0]), str(item[1])))
                ledger.records.append(
                    TurnRecord(
                        index=event["index"],
                        question=event["question"],
                        answer=event["answer"],
                        touched=list(event.get("touched") or []),
                        clarifications=clarifications,
                    )
                )
                state = SessionState.from_dict(event.get("state") or {})

        return ledger, state

    def info(self) -> SessionInfo:
        """返回本会话的列表摘要。"""
        return _info_from(self.session_id, self.path)[0]

    @staticmethod
    def list_sessions(
        session_dir: str | Path | None = None,
        keyword: str | None = None,
    ) -> list[SessionInfo]:
        """列出目录下的会话，按 ID（时间戳前缀）从新到旧。

        Args:
            session_dir: 会话目录，None 时使用默认。
            keyword: 可选的关键词，不区分大小写地匹配 reset 之后的
                提问与回答文本；None 时不过滤。

        Returns:
            SessionInfo 列表，目录不存在时为空。
        """
        root = _resolve_dir(session_dir)
        if not root.is_dir():
            return []

        needle = keyword.lower() if keyword else None
        infos: list[SessionInfo] = []
        for path in sorted(root.glob("*.jsonl"), reverse=True):
            info, search_text = _info_from(path.stem, path)
            if needle is not None and needle not in search_text.lower():
                continue
            infos.append(info)
        return infos

    def _append(self, event: dict[str, Any]) -> None:
        """追加一行事件。"""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def _ensure_started(self) -> None:
        """会话文件不存在时先写 session_start 头事件。"""
        if self.path.exists():
            return

        self.session_dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "event": "session_start",
                        "session_id": self.session_id,
                        "ts": _now_iso(),
                        "repo_root": self._repo_root,
                        "model": self._model,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
        if self._pending_config:
            pending = dict(self._pending_config)
            self._pending_config.clear()
            self._append_config(pending)

    def _append_config(self, changes: dict[str, Any]) -> None:
        """追加一条 config 事件。"""
        self._append({"event": "config", "ts": _now_iso(), **changes})

    def _events(self) -> list[dict[str, Any]]:
        """读取全部可解析的事件行，非法行与坏行跳过。"""
        events: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    events.append(parsed)
        return events


def _resolve_path(session_dir: str | Path | None, session_id: str | None) -> Path:
    """把会话 ID 解析成文件路径，latest 取时间戳最大的。"""
    root = _resolve_dir(session_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"no sessions directory: {root}")

    if session_id in (None, "", LATEST):
        candidates = sorted(root.glob("*.jsonl"), key=lambda p: p.stem, reverse=True)
        if not candidates:
            raise FileNotFoundError(f"no saved sessions in {root}")
        return candidates[0]

    if session_id.endswith(".jsonl"):
        session_id = session_id[: -len(".jsonl")]

    path = root / f"{session_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no such session: {session_id} (try `breeze chat --list`)")
    return path


def _info_from(session_id: str, path: Path) -> tuple[SessionInfo, str]:
    """从会话文件抽取列表摘要与可检索文本；坏文件按空摘要返回。

    Returns:
        (SessionInfo, 最后一个 reset 之后的提问与回答拼接文本)。
    """
    created_at = ""
    repo_root = ""
    model = ""
    first_question = ""
    turns = 0
    total_tokens = 0
    search_parts: list[str] = []

    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                if event.get("event") == "session_start":
                    created_at = event.get("ts") or ""
                    repo_root = event.get("repo_root") or ""
                    model = event.get("model") or ""
                elif event.get("event") == "config":
                    model = event.get("model") or model
                elif event.get("event") == "turn":
                    turns += 1
                    total_tokens += int(event.get("total_tokens") or 0)
                    question = str(event.get("question") or "")
                    if not first_question:
                        first_question = question
                    search_parts.extend((question, str(event.get("answer") or "")))
                elif event.get("event") == "reset":
                    turns = 0
                    total_tokens = 0
                    first_question = ""
                    search_parts.clear()

    return (
        SessionInfo(
            session_id=session_id,
            path=path,
            created_at=created_at,
            repo_root=repo_root,
            model=model,
            turns=turns,
            first_question=first_question,
            total_tokens=total_tokens,
        ),
        "\n".join(search_parts),
    )

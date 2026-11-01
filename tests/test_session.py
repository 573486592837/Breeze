"""会话持久化的单元测试。"""

import json
from pathlib import Path

import pytest

from breeze.session import SessionStore
from breeze.state import SessionState, TurnRecord


def _store(tmp_path: Path, session_id: str = "s1") -> SessionStore:
    return SessionStore(
        tmp_path / "sessions",
        session_id,
        repo_root=tmp_path,
        model="fake-model",
    )


def _record(index: int, question: str = "q", answer: str = "a") -> TurnRecord:
    return TurnRecord(index=index, question=question, answer=answer, touched=["a.py"])


def _summary(run_id: str = "r1") -> dict:
    return {"trace_path": f"traces/{run_id}.jsonl", "llm_calls": 2, "tool_calls": 1, "total_tokens": 300}


def _state() -> SessionState:
    st = SessionState()
    st.update("read_file", {"path": "a.py"}, "1 | def target():\n--- (file has 3 lines total) ---", 0)
    st.update("read_file", {"path": "a.py", "start_line": 1, "end_line": 2}, "1 | def target():", 1)
    st.update("read_file", {"path": "a.py", "start_line": 1, "end_line": 2}, "1 | def target():", 2)
    st.update("run_tests", {}, "exit_code=0\nsummary: 3 passed", 3)
    st.plan = "1. inspect\n2. fix"
    return st


def _events(store: SessionStore) -> list[dict]:
    return [json.loads(ln) for ln in store.path.read_text(encoding="utf-8").splitlines()]


# ─── 落盘时机 ───


def test_empty_session_writes_no_file(tmp_path: Path) -> None:
    """没有回合的会话不落盘：只 /help 就退出、或测试跑空都不留垃圾文件。"""
    store = _store(tmp_path)
    store.record_reset()

    assert not store.path.exists()


def test_first_turn_writes_start_event_then_turn(tmp_path: Path) -> None:
    """第一个回合先写 session_start，再写 turn。"""
    store = _store(tmp_path)
    store.record_turn(_record(1, "为什么失败", "因为 x"), _state(), _summary())

    events = _events(store)

    assert [e["event"] for e in events] == ["session_start", "turn"]
    assert events[0]["session_id"] == "s1"
    assert events[0]["model"] == "fake-model"
    assert events[0]["repo_root"] == str(tmp_path)


def test_start_event_written_once(tmp_path: Path) -> None:
    """同 ID 反复写回合只保留一个头事件。"""
    _store(tmp_path).record_turn(_record(1), _state(), _summary())
    _store(tmp_path).record_turn(_record(2), _state(), _summary("r2"))

    text = _store(tmp_path).path.read_text(encoding="utf-8")

    assert text.count('"event": "session_start"') == 1
    assert text.count('"event": "turn"') == 2


# ─── 恢复 ───


def test_round_trip_restores_ledger_and_state(tmp_path: Path) -> None:
    """落盘两回合后恢复，归档与状态逐字段一致。"""
    store = _store(tmp_path)
    state = _state()
    store.record_turn(_record(1, "为什么失败", "因为 x"), state, _summary("r1"))
    state.update("apply_patch", {"path": "a.py"}, "Patched a.py (1 replacement)", 4)
    store.record_turn(_record(2, "改好了吗", "改好了"), state, _summary("r2"))

    restored = SessionStore(tmp_path / "sessions", "s1")
    ledger, restored_state = restored.load()

    assert [(r.index, r.question, r.answer, r.touched) for r in ledger.records] == [
        (1, "为什么失败", "因为 x", ["a.py"]),
        (2, "改好了吗", "改好了", ["a.py"]),
    ]
    assert restored_state.symbols == state.symbols
    assert restored_state.reads == state.reads
    assert restored_state.edits == state.edits
    assert restored_state.tests == state.tests
    assert restored_state.plan == state.plan
    assert restored_state.read_counts == state.read_counts


def test_next_record_index_continues(tmp_path: Path) -> None:
    """恢复后新回合的序号接着历史，而不是从 1 重来。"""
    store = _store(tmp_path)
    store.record_turn(_record(1), _state(), _summary())

    ledger, _ = SessionStore(tmp_path / "sessions", "s1").load()
    ledger.record("q3", "a3", [])

    assert [r.index for r in ledger.records] == [1, 2]


def test_reset_event_drops_earlier_turns(tmp_path: Path) -> None:
    """/new 之后的恢复只保留重置点以后的回合。"""
    store = _store(tmp_path)
    store.record_turn(_record(1, "旧会话"), _state(), _summary())
    store.record_reset()
    store.record_turn(_record(2, "新会话"), _state(), _summary("r2"))

    ledger, _ = SessionStore(tmp_path / "sessions", "s1").load()

    assert [r.question for r in ledger.records] == ["新会话"]


def test_corrupt_lines_are_skipped(tmp_path: Path) -> None:
    """坏行（截断/非法 JSON）跳过，其余回合仍可恢复。"""
    store = _store(tmp_path)
    store.record_turn(_record(1, "第一问"), _state(), _summary())
    with store.path.open("a", encoding="utf-8") as f:
        f.write('{"event": "turn", "index": 2, "ques\n')
        f.write("not json at all\n")
    store.record_turn(_record(3, "第三问"), _state(), _summary("r3"))

    ledger, _ = SessionStore(tmp_path / "sessions", "s1").load()

    assert [r.question for r in ledger.records] == ["第一问", "第三问"]


def test_load_without_file_returns_empty(tmp_path: Path) -> None:
    """会话文件不存在时恢复为空归档。"""
    ledger, state = SessionStore(tmp_path / "sessions", "never-written").load()

    assert ledger.records == []
    assert state.render() == ""


# ─── 运行时配置事件 ───


def test_config_waits_for_first_turn(tmp_path: Path) -> None:
    """/model 后立即退出不落空文件；首个回合写出时补写 config。"""
    store = _store(tmp_path)
    store.record_config(model="new-model")

    assert not store.path.exists()

    store.record_turn(_record(1), _state(), _summary())

    events = _events(store)
    assert [e["event"] for e in events] == ["session_start", "config", "turn"]
    assert events[1]["model"] == "new-model"


def test_config_events_merge_on_resume(tmp_path: Path) -> None:
    """多个 config 事件按时间顺序合并，后写的覆盖先写的。"""
    store = _store(tmp_path)
    store.record_turn(_record(1), _state(), _summary())
    store.record_config(model="m2", trace_prefix="run-a")
    store.record_config(profile="short", max_steps=4)

    config = SessionStore(tmp_path / "sessions", "s1").saved_config()

    assert config == {
        "model": "m2",
        "trace_prefix": "run-a",
        "profile": "short",
        "max_steps": 4,
    }


def test_saved_config_empty_without_file(tmp_path: Path) -> None:
    assert _store(tmp_path).saved_config() == {}


def test_config_model_updates_listed_model(tmp_path: Path) -> None:
    """会话列表展示切换后的模型。"""
    store = _store(tmp_path)
    store.record_turn(_record(1), _state(), _summary())
    store.record_config(model="switched-model")

    infos = SessionStore.list_sessions(tmp_path / "sessions")

    assert infos[0].model == "switched-model"


# ─── resume 解析 ───


def test_resume_latest_picks_newest(tmp_path: Path) -> None:
    """latest 取时间戳前缀最大的会话。"""
    SessionStore(tmp_path / "sessions", "20260101T000000_aaa").record_turn(_record(1, "旧"), _state(), _summary())
    SessionStore(tmp_path / "sessions", "20260201T000000_bbb").record_turn(_record(1, "新"), _state(), _summary())

    store = SessionStore.resume(session_dir=tmp_path / "sessions")

    assert store.session_id == "20260201T000000_bbb"
    ledger, _ = store.load()
    assert ledger.records[0].question == "新"


def test_resume_by_id_accepts_suffix(tmp_path: Path) -> None:
    """传入带 .jsonl 的 ID 也能解析。"""
    SessionStore(tmp_path / "sessions", "20260101T000000_aaa").record_turn(_record(1), _state(), _summary())

    store = SessionStore.resume("20260101T000000_aaa.jsonl", tmp_path / "sessions")

    assert store.session_id == "20260101T000000_aaa"


def test_resume_unknown_id_raises(tmp_path: Path) -> None:
    """ID 不存在时报错并提示 --list。"""
    SessionStore(tmp_path / "sessions", "20260101T000000_aaa").record_turn(_record(1), _state(), _summary())

    with pytest.raises(FileNotFoundError, match="--list"):
        SessionStore.resume("20990101T000000_zzz", tmp_path / "sessions")


def test_resume_empty_dir_raises(tmp_path: Path) -> None:
    """目录里没有任何会话时报错。"""
    (tmp_path / "sessions").mkdir()

    with pytest.raises(FileNotFoundError):
        SessionStore.resume("latest", tmp_path / "sessions")


def test_generated_id_is_timestamp_prefixed(tmp_path: Path) -> None:
    """自动生成的 ID 以时间戳开头，保证按名字排序即按时间排序。"""
    store = SessionStore(tmp_path / "sessions")

    assert len(store.session_id.split("_")) == 2
    assert store.session_id[:8].isdigit()


# ─── 列表摘要 ───


def test_list_sessions_summarizes(tmp_path: Path) -> None:
    """列表输出回合数、Token 合计与首问。"""
    store = SessionStore(tmp_path / "sessions", "20260101T000000_aaa", repo_root=tmp_path, model="m")
    store.record_turn(_record(1, "首个问题", "答"), _state(), _summary())
    store.record_turn(TurnRecord(2, "第二问", "答", []), _state(), {**_summary("r2"), "total_tokens": 700})

    infos = SessionStore.list_sessions(tmp_path / "sessions")

    assert len(infos) == 1
    info = infos[0]
    assert info.session_id == "20260101T000000_aaa"
    assert info.turns == 2
    assert info.total_tokens == 1000
    assert info.first_question == "首个问题"
    assert info.model == "m"


def test_list_sessions_counts_turns_after_reset(tmp_path: Path) -> None:
    """reset 之后回合数与 Token 从头计。"""
    store = SessionStore(tmp_path / "sessions", "20260101T000000_aaa")
    store.record_turn(_record(1), _state(), _summary())
    store.record_reset()
    store.record_turn(_record(2, "重置后的首问"), _state(), _summary("r2"))

    info = SessionStore.list_sessions(tmp_path / "sessions")[0]

    assert info.turns == 1
    assert info.total_tokens == 300
    assert info.first_question == "重置后的首问"


def test_list_sessions_sorted_newest_first(tmp_path: Path) -> None:
    """按 ID 从新到旧排列；只有写过回合的会话才存在于目录里。"""
    for sid in ("20260101T000000_aaa", "20260201T000000_bbb", "20260301T000000_ccc"):
        SessionStore(tmp_path / "sessions", sid).record_turn(_record(1), _state(), _summary())

    infos = SessionStore.list_sessions(tmp_path / "sessions")

    assert [i.session_id for i in infos] == [
        "20260301T000000_ccc",
        "20260201T000000_bbb",
        "20260101T000000_aaa",
    ]


def test_list_sessions_missing_dir(tmp_path: Path) -> None:
    """目录不存在时返回空列表而不是报错。"""
    assert SessionStore.list_sessions(tmp_path / "nope") == []


def test_list_sessions_filters_by_keyword(tmp_path: Path) -> None:
    """keyword 不区分大小写，同时匹配提问与回答。"""
    first = SessionStore(tmp_path / "sessions", "20260101T000000_aaa")
    first.record_turn(_record(1, "修复 Safe_Path 越界", "已修改 safety.py"), _state(), _summary())
    second = SessionStore(tmp_path / "sessions", "20260201T000000_bbb")
    second.record_turn(_record(1, "解释 registry", "工具注册表在 tools/registry.py"), _state(), _summary())

    by_question = SessionStore.list_sessions(tmp_path / "sessions", keyword="safe_path")
    assert [i.session_id for i in by_question] == ["20260101T000000_aaa"]

    by_answer = SessionStore.list_sessions(tmp_path / "sessions", keyword="工具注册表")
    assert [i.session_id for i in by_answer] == ["20260201T000000_bbb"]

    assert SessionStore.list_sessions(tmp_path / "sessions", keyword="不存在的词") == []
    assert len(SessionStore.list_sessions(tmp_path / "sessions")) == 2


def test_list_sessions_keyword_ignores_turns_before_reset(tmp_path: Path) -> None:
    """reset 之前的问答不参与关键词匹配。"""
    store = SessionStore(tmp_path / "sessions", "20260101T000000_aaa")
    store.record_turn(_record(1, "旧话题 alpha"), _state(), _summary())
    store.record_reset()
    store.record_turn(_record(2, "新话题 beta"), _state(), _summary("r2"))

    assert SessionStore.list_sessions(tmp_path / "sessions", keyword="alpha") == []
    assert len(SessionStore.list_sessions(tmp_path / "sessions", keyword="beta")) == 1


def test_info_of_current_session(tmp_path: Path) -> None:
    """info() 读取本会话文件用于 repo/model 提示。"""
    store = _store(tmp_path)
    store.record_turn(_record(1, "问"), _state(), _summary())

    info = store.info()

    assert info.repo_root == str(tmp_path)
    assert info.turns == 1


def test_default_dir_comes_from_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未传目录时用 sessions.dir 配置。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "sessions_dir", tmp_path / "configured")

    store = SessionStore(session_id="20260101T000000_aaa")
    store.record_turn(_record(1), _state(), _summary())

    assert store.path == tmp_path / "configured" / "20260101T000000_aaa.jsonl"
    assert SessionStore.list_sessions()[0].session_id == "20260101T000000_aaa"

"""SessionState 与 TurnLedger 的单元测试。"""

import pytest

from breeze.state import SessionState, TurnLedger


def _state(max_entries: int = 8) -> SessionState:
    return SessionState(max_entries=max_entries)


# ─── update 解析 ───


def test_update_read_file_records_range_and_symbols() -> None:
    """read_file 结果应记录行范围并提取 def 符号位置。"""
    st = _state()
    result = "289 | def _check_options(tokens, exe):\n290 |     pass\n--- (file has 418 lines total) ---"
    st.update(
        "read_file",
        {"path": "src/breeze/tools/shell.py", "start_line": 289, "end_line": 312},
        result,
        3,
    )

    assert st.reads["src/breeze/tools/shell.py"] == "lines 289-312 of 418 (step 3)"
    assert st.symbols["_check_options"] == "src/breeze/tools/shell.py:289"


def test_update_read_file_full_read() -> None:
    """不带范围时记录 full 描述。"""
    st = _state()
    st.update("read_file", {"path": "a.py"}, "1 | def foo():\n--- (file has 5 lines total) ---", 0)

    assert st.reads["a.py"].startswith("full 5 lines")
    assert st.symbols["foo"] == "a.py:1"


def test_update_grep_records_definition_locations() -> None:
    """grep 输出中的 def/class 行应记入符号位置。"""
    st = _state()
    result = "src/breeze/tools/safety.py:12:def safe_path(path: str | Path) -> Path:\nother.py:5:x = 1"
    st.update("grep", {"pattern": "def safe_path"}, result, 1)

    assert st.symbols["safe_path"] == "src/breeze/tools/safety.py:12"
    assert "x" not in st.symbols


def test_update_apply_patch_success_and_failure() -> None:
    """apply_patch 成功与失败都应记录到 edits。"""
    st = _state()
    st.update("apply_patch", {"path": "x.py"}, "Patched x.py (1 replacement)", 5)
    assert st.edits["x.py"] == "patched at step 5"

    st.update("apply_patch", {"path": "x.py"}, "Error: old text not found in x.py\nSearched for:", 6)
    assert st.edits["x.py"] == "failed at step 6"


def test_update_run_tests_records_summary_and_failure() -> None:
    """run_tests 应记录摘要与首个失败用例。"""
    st = _state()
    result = (
        "exit_code=1\nsummary: 21 passed, 1 failed\nfailures:\n"
        "- tests/test_fs.py::test_safe_path_rejects_escape — Failed: DID NOT RAISE\nstatus: failed"
    )
    st.update("run_tests", {"pattern": ""}, result, 2)

    assert "21 passed, 1 failed" in st.tests
    assert "test_fs.py::test_safe_path_rejects_escape" in st.tests
    assert "(step 2)" in st.tests


def test_update_plan_replaces_previous_plan() -> None:
    """update_plan 用完整的新计划覆盖旧计划。"""
    st = _state()
    st.update("update_plan", {"content": "1. 读代码"}, "Plan updated (9 chars).", 0)
    st.update("update_plan", {"content": "1. 改代码\n2. 跑测试"}, "Plan updated (17 chars).", 1)

    assert st.plan == "1. 改代码\n2. 跑测试"


def test_to_dict_round_trip_keeps_plan() -> None:
    """序列化与反序列化保留计划。"""
    st = _state()
    st.plan = "1. 读代码\n2. 改代码"

    restored = SessionState.from_dict(st.to_dict())

    assert restored.plan == st.plan


def test_update_ignores_unknown_and_missing_summary() -> None:
    """未知工具与无 summary 的结果不应产生状态。"""
    st = _state()
    st.update("list_files", {"path": "."}, "a.py\nb.py", 0)
    st.update("run_tests", {"pattern": ""}, "exit_code=0\nno summary line", 1)

    assert st.render() == ""


# ─── render ───


def test_render_empty_state() -> None:
    """空状态渲染为空字符串。"""
    assert _state().render() == ""


def test_render_sections_and_footer() -> None:
    """渲染应包含各 section 与重读提醒。"""
    st = _state()
    st.update("grep", {}, "src/a.py:12:def target():", 1)
    block = st.render()

    assert "[Session state" in block
    assert "Symbols located:" in block
    assert "target: src/a.py:12" in block
    assert "otherwise edit from the latest copy" in block


def test_render_caps_entries_per_section() -> None:
    """每个 section 只保留最近 max_entries 条。"""
    st = _state(max_entries=2)
    for i in range(5):
        st.update("read_file", {"path": f"f{i}.py"}, f"1 | def g{i}():\n--- (file has 2 lines total) ---", i)

    block = st.render()
    assert "f4.py" in block
    assert "f3.py" in block
    assert "f0.py" not in block
    assert "f1.py" not in block
    assert "f2.py" not in block


def test_repeated_read_flagged_in_render() -> None:
    """同一范围第二次读取应在状态块中标注致忆信号。"""
    st = _state()
    result = "292 | def f():\n--- (file has 421 lines total) ---"
    st.update("read_file", {"path": "s.py", "start_line": 292, "end_line": 315}, result, 8)
    st.update("read_file", {"path": "s.py", "start_line": 292, "end_line": 315}, result, 13)

    assert st.read_counts[("s.py", 292, 315)] == 2
    block = st.render()
    assert "Repeated reads" in block
    assert "read 2 times" in block


def test_single_read_not_flagged() -> None:
    """只读过一次的不应出现重复警示。"""
    st = _state()
    st.update("read_file", {"path": "s.py", "start_line": 1, "end_line": 5}, "1 | x", 0)

    assert "Repeated reads" not in st.render()


def test_repeated_read_wording_does_not_claim_compression() -> None:
    """未压缩的回合里，重复读取提示不能声称旧副本已被丢弃。"""
    st = _state()
    result = "1 | x"
    st.update("read_file", {"path": "s.py"}, result, 0)
    st.update("read_file", {"path": "s.py"}, result, 1)

    block = st.render()
    assert "compressed away" not in block
    assert "edit from it instead of reading again" in block


def test_reread_moves_entry_to_end() -> None:
    """重读旧文件后，Latest reads 应把它视为最近读取而非旧条目。"""
    st = _state(max_entries=2)
    for i in range(4):
        st.update("read_file", {"path": f"f{i}.py"}, "1 | x\n--- (file has 1 lines total) ---", i)
    st.update("read_file", {"path": "f0.py"}, "1 | x\n--- (file has 1 lines total) ---", 9)

    block = st.render()
    assert "f0.py" in block
    assert "f3.py" in block
    assert "f1.py" not in block
    assert "f2.py" not in block


def test_start_turn_clears_read_counts() -> None:
    """开始新回合后，上一回合的重复读取计数不再影响状态块。"""
    st = _state()
    st.update("read_file", {"path": "s.py"}, "1 | x", 0)
    st.update("read_file", {"path": "s.py"}, "1 | x", 1)
    assert st.read_counts

    st.start_turn()

    assert st.read_counts == {}
    assert "Repeated reads" not in st.render()


def test_read_counts_not_persisted() -> None:
    """读取计数只在回合内有效，序列化与恢复都不应保留。"""
    st = _state()
    st.update("read_file", {"path": "s.py"}, "1 | x", 0)

    data = st.to_dict()
    assert "read_counts" not in data
    restored = SessionState.from_dict({**data, "read_counts": [["s.py", None, None, 5]]})
    assert restored.read_counts == {}
    assert restored.reads == st.reads


# ─── 符号索引的完整性与用途 ───


def test_symbols_of_one_file_are_never_truncated() -> None:
    """单个文件的符号不受 max_entries 截断，避免"列一遍"被迫重读全文。"""
    st = _state(max_entries=3)
    lines = "\n".join(f"{i * 10 + 1} | def f{i}():" for i in range(10))
    st.update("read_file", {"path": "big.py"}, f"{lines}\n--- (file has 100 lines total) ---", 0)

    block = st.render()
    assert len(st.symbols) == 10
    for i in range(10):
        assert f"- f{i}: big.py:{i * 10 + 1}" in block


def test_symbols_cap_drops_oldest_files() -> None:
    """超上限时按文件丢弃最旧的，而不是丢弃文件内的尾部符号。"""
    st = _state(max_entries=2)
    st.update("read_file", {"path": "old.py"}, "1 | def a():\n--- (file has 2 lines total) ---", 0)
    st.update("read_file", {"path": "mid.py"}, "1 | def b():\n--- (file has 2 lines total) ---", 1)
    st.update("read_file", {"path": "new.py"}, "1 | def c():\n2 | def d():\n--- (file has 3 lines total) ---", 2)

    block = st.render()
    assert "- c: new.py:1" in block
    assert "- d: new.py:2" in block
    assert "- b: mid.py:1" in block
    assert "a: old.py" not in block


def test_state_block_answers_locate_questions() -> None:
    """状态块应说明它是定位/列举类问题的权威来源，缺失时才重读。"""
    st = _state()
    st.update("grep", {}, "src/a.py:12:def target():", 1)

    block = st.render()
    assert "authoritative for locate/list questions" in block
    assert "read_file when a needed fact is missing" in block
    assert "otherwise edit from the latest copy" in block


# ─── TurnLedger 回合归档 ───


def _ledger(answer_preview: int = 1500, max_records: int = 8, max_chars: int = 6000) -> TurnLedger:
    return TurnLedger(answer_preview=answer_preview, max_records=max_records, max_chars=max_chars)


def test_record_indexes_from_one_and_dedups_touched() -> None:
    """回合序号从 1 开始，触碰文件按首次出现去重。"""
    ledger = _ledger()
    ledger.record("q1", "a1", ["b.py", "a.py", "b.py"])
    ledger.record("q2", "a2", ["a.py", "c.py"])

    assert [r.index for r in ledger.records] == [1, 2]
    assert ledger.records[0].touched == ["b.py", "a.py"]
    assert ledger.touched_files() == ["b.py", "a.py", "c.py"]


def test_record_folds_question_to_single_line() -> None:
    """多行问题折叠为一行，避免破坏归档的行结构。"""
    ledger = _ledger()
    ledger.record("first line\n   second line", "answer", [])

    assert ledger.records[0].question == "first line second line"


def test_record_truncates_long_answer() -> None:
    """超长答案按 answer_preview 截断并加省略号。"""
    ledger = _ledger(answer_preview=10)
    ledger.record("q", "x" * 40, [])

    answer = ledger.records[0].answer
    assert len(answer) == 10
    assert answer.endswith("...")


def test_render_empty_returns_blank() -> None:
    """无记录时渲染为空字符串。"""
    assert _ledger().render() == ""


def test_render_contains_q_a_and_touched() -> None:
    """渲染文本含问答与触碰文件行。"""
    ledger = _ledger()
    ledger.record("why does it fail?", "because of x", ["src/a.py"])

    text = ledger.render()
    assert text.startswith("[Completed turns")
    assert "Turn 1 Q: why does it fail?" in text
    assert "Turn 1 A: because of x" in text
    assert "Turn 1 touched: src/a.py" in text


def test_render_keeps_most_recent_records() -> None:
    """超过 max_records 时丢弃最旧回合，标题说明保留数量。"""
    ledger = _ledger(max_records=2)
    for i in range(1, 6):
        ledger.record(f"q{i}", f"a{i}", [])

    text = ledger.render()
    assert "[Completed turns — 2 most recent of 5]" in text
    assert "Turn 5 Q: q5" in text
    assert "Turn 4 Q: q4" in text
    assert "Turn 1 Q: q1" not in text


def test_render_caps_touched_files_with_more_marker() -> None:
    """单个回合触碰文件过多时只列前 8 个并标注剩余数量。"""
    ledger = _ledger()
    ledger.record("q", "a", [f"f{i}.py" for i in range(11)])

    line = next(ln for ln in ledger.render().splitlines() if "touched" in ln)
    assert " (+3 more)" in line
    assert "f8.py" not in line


def test_answer_keeps_line_structure() -> None:
    """答案归档保留换行与列表结构，只按长度截断。"""
    ledger = _ledger()
    answer = "## 校验逻辑\n1. _check_allowed\n2. _check_options\n"
    ledger.record("q", answer, [])

    assert ledger.records[0].answer == "## 校验逻辑\n1. _check_allowed\n2. _check_options"
    assert "\n" in ledger.render()


def test_long_answer_still_clipped() -> None:
    """多行答案超长时同样按 answer_preview 截断。"""
    ledger = _ledger(answer_preview=20)
    ledger.record("q", "line one\nline two\nline three", [])

    assert ledger.records[0].answer == "line one\nline two..."


def test_render_respects_total_char_budget() -> None:
    """整块归档超过 max_chars 时丢弃更早的回合，最新回合始终保留。"""
    ledger = _ledger(max_chars=200)
    for i in range(5):
        ledger.record(f"q{i}", "x" * 150, [])

    text = ledger.render()
    assert "[Completed turns — 1 most recent of 5]" in text
    assert "Turn 5 Q: q4" in text
    assert "Turn 4 Q: q3" not in text
    assert len(text) < 200 + 60


def test_ledger_defaults_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """归档预算未显式传值时用 chat.* 配置。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "chat_answer_preview", 30)
    monkeypatch.setattr(settings, "chat_max_records", 2)
    monkeypatch.setattr(settings, "chat_max_chars", 90)

    ledger = TurnLedger()

    assert (ledger.answer_preview, ledger.max_records, ledger.max_chars) == (30, 2, 90)

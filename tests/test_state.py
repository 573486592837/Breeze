"""SessionState 的单元测试。"""

from breeze.state import SessionState


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
    assert "copy old verbatim" in block


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

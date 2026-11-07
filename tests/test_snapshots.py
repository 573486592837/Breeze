"""跨回合工作集快照的单元测试。"""

from pathlib import Path

from breeze.snapshots import SNAPSHOT_VERSION, SnapshotStore, select_paths


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_select_prioritizes_edit_then_plan_then_frequency() -> None:
    """选择顺序：本回合编辑 > 计划提及 > 读取频次 > 最近触碰。"""
    selected = select_paths(
        ["a.py", "b.py", "c.py", "d.py"],
        edited=frozenset({"b.py"}),
        plan_paths=frozenset({"c.py"}),
        read_hits={"d.py": 3},
        order=["a.py", "b.py", "c.py", "d.py"],
        limit=3,
    )

    assert selected == ["b.py", "c.py", "d.py"]


def test_select_is_bounded() -> None:
    """max_files 限制候选数量。"""
    selected = select_paths([f"f{i}.py" for i in range(10)], limit=4)

    assert len(selected) == 4


def test_capture_and_render_includes_current_content(tmp_path: Path) -> None:
    """捕获后渲染应带路径与原始内容。"""
    _write(tmp_path, "src/a.py", "value = 1\n")
    store = SnapshotStore(root=tmp_path, max_files=2, max_chars=4000)

    store.capture(["src/a.py"], turn=2, edited=["src/a.py"])
    rendered = store.render()

    assert "[Working set" in rendered
    assert "src/a.py" in rendered
    assert "value = 1" in rendered
    assert store.files["src/a.py"].source == "edit"


def test_render_drops_snapshot_when_file_changed(tmp_path: Path) -> None:
    """回合间文件被改动时，旧快照不进入提示词。"""
    _write(tmp_path, "a.py", "value = 1\n")
    store = SnapshotStore(root=tmp_path, max_files=2, max_chars=4000)
    store.capture(["a.py"], turn=1)
    store.files["a.py"].mtime_ns -= 1

    assert store.render() == ""


def test_render_drops_snapshot_when_file_deleted(tmp_path: Path) -> None:
    """文件被删除后不再渲染。"""
    path = _write(tmp_path, "a.py", "value = 1\n")
    store = SnapshotStore(root=tmp_path, max_files=2, max_chars=4000)
    store.capture(["a.py"], turn=1)
    path.unlink()

    assert store.render() == ""


def test_render_respects_char_budget(tmp_path: Path) -> None:
    """整块渲染不超过字符预算，并标注截断。"""
    _write(tmp_path, "big.py", "\n".join(f"line_{i} = {i}" for i in range(400)))
    store = SnapshotStore(root=tmp_path, max_files=1, max_chars=1200)

    store.capture(["big.py"], turn=1)
    rendered = store.render()

    assert rendered
    assert len(rendered) <= 1200
    assert "truncated" in rendered


def test_capture_respects_max_files(tmp_path: Path) -> None:
    """捕获的文件数不超过 max_files。"""
    for i in range(5):
        _write(tmp_path, f"f{i}.py", f"v{i} = {i}\n")
    store = SnapshotStore(root=tmp_path, max_files=2, max_chars=4000)

    store.capture([f"f{i}.py" for i in range(5)], turn=1)

    assert len(store.files) == 2


def test_disabled_store_keeps_nothing(tmp_path: Path) -> None:
    """max_files=0 时禁用捕获与渲染。"""
    _write(tmp_path, "a.py", "value = 1\n")
    store = SnapshotStore(root=tmp_path, max_files=0, max_chars=4000)

    store.capture(["a.py"], turn=1)

    assert store.files == {}
    assert store.render() == ""


def test_invalidate_removes_path_and_children(tmp_path: Path) -> None:
    """编辑目录时其下文件的快照一并失效。"""
    _write(tmp_path, "src/a.py", "value = 1\n")
    _write(tmp_path, "other.py", "x = 1\n")
    store = SnapshotStore(root=tmp_path, max_files=4, max_chars=4000)
    store.capture(["src/a.py", "other.py"], turn=1)

    store.invalidate("src")

    assert store.paths() == ["other.py"]


def test_round_trip_serialization(tmp_path: Path) -> None:
    """to_dict / restore 保留路径、内容与捕获回合。"""
    _write(tmp_path, "a.py", "value = 1\n")
    store = SnapshotStore(root=tmp_path, max_files=2, max_chars=4000)
    store.capture(["a.py"], turn=3, edited=["a.py"])

    data = store.to_dict()
    restored = SnapshotStore.from_dict(data, root=tmp_path, max_files=2, max_chars=4000)

    assert data["version"] == SNAPSHOT_VERSION
    assert restored.paths() == ["a.py"]
    assert restored.files["a.py"].content == "value = 1\n"
    assert restored.files["a.py"].captured_turn == 3


def test_restore_ignores_bad_entries(tmp_path: Path) -> None:
    """损坏的 sidecar 条目被跳过。"""
    store = SnapshotStore.from_dict(
        {"version": 1, "files": [{"path": "a.py"}, {"path": 1, "content": "x"}, "bad"]},
        root=tmp_path,
    )

    assert store.files == {}


def test_plan_mention_keeps_unread_file(tmp_path: Path) -> None:
    """计划提及的已有快照在预算紧张时优先保留。"""
    _write(tmp_path, "planned.py", "planned = True\n")
    _write(tmp_path, "other.py", "other = True\n")
    store = SnapshotStore(root=tmp_path, max_files=2, max_chars=4000)
    store.capture(["planned.py", "other.py"], turn=1)
    # 两个候选都在已有快照里；计划指向 other.py 时应优先保留它。
    store.max_files = 1
    store.capture([], turn=2, plan="1. 修改 other.py")

    assert store.paths() == ["other.py"]

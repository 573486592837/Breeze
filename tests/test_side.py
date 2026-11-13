"""内存侧任务树的单元测试。"""

from __future__ import annotations

import pytest

from breeze.side import SideTree


def test_spawn_from_main_and_nested() -> None:
    """侧任务从当前活动节点派生，形成父子关系。"""
    tree = SideTree()

    first = tree.spawn("看看配置加载", "agent")
    second = tree.spawn("看看 env 解析", "agent")

    assert first.id == "s1"
    assert first.parent_id is None
    assert first.agent == "agent"
    assert second.id == "s2"
    assert second.parent_id == "s1"
    assert tree.active is second


def test_back_pauses_and_returns_parent() -> None:
    """/back 只暂停，不丢弃节点。"""
    tree = SideTree()
    first = tree.spawn("q1", "agent")
    tree.spawn("q2", "agent")

    assert tree.back() == "s1"
    assert tree.active is first
    assert [task.id for task in tree.tasks()] == ["s1", "s2"]

    assert tree.back() is None
    assert tree.active is None
    assert len(tree.tasks()) == 2


def test_switch_between_paused_tasks() -> None:
    """可在已暂停的侧任务与主对话之间切换。"""
    tree = SideTree()
    tree.spawn("q1", "agent")
    tree.spawn("q2", "agent")
    tree.switch("main")

    tree.switch("s1")
    assert tree.active is not None
    assert tree.active.id == "s1"

    tree.switch("s2")
    assert tree.active is not None
    assert tree.active.id == "s2"


def test_close_discards_subtree() -> None:
    """关闭父节点会连同其子树一起丢弃，且回到父节点的父节点。"""
    tree = SideTree()
    tree.spawn("q1", "agent")
    tree.spawn("q2", "agent")
    tree.spawn("q3", "agent")

    removed = tree.close()

    assert removed == ["s3"]
    assert tree.active is not None
    assert tree.active.id == "s2"

    removed = tree.close()

    assert removed == ["s2"]
    assert tree.active is not None
    assert tree.active.id == "s1"

    removed = tree.close()

    assert removed == ["s1"]
    assert tree.active is None
    assert tree.tasks() == ()


def test_close_or_back_at_main_is_rejected() -> None:
    """主对话不是可关闭/返回的侧任务。"""
    tree = SideTree()

    with pytest.raises(ValueError):
        tree.back()
    with pytest.raises(ValueError):
        tree.close()


def test_switch_unknown_task_is_rejected() -> None:
    """切换到不存在的节点应失败。"""
    tree = SideTree()

    with pytest.raises(KeyError):
        tree.switch("s9")


def test_render_marks_active_and_paused() -> None:
    """树形渲染包含主线、嵌套关系与状态。"""
    tree = SideTree()
    tree.spawn("看看配置加载", "agent")
    tree.spawn("看看 env 解析", "agent")
    tree.back()

    text = tree.render()

    assert "main (paused)" in text
    assert "s1 (active) 看看配置加载" in text
    assert "  s2 (paused) 看看 env 解析" in text


def test_empty_tree_renders_main_only() -> None:
    """没有侧任务时只显示主对话。"""
    tree = SideTree()

    assert tree.render() == "main (active)"

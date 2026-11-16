"""内存侧任务分支的单元测试。"""

from __future__ import annotations

import pytest

from breeze.side import SideTree


def test_branches_are_always_spawned_from_main() -> None:
    """侧任务只能从主对话派生，即使已有活动分支也不再嵌套。"""
    tree = SideTree()

    first = tree.spawn("看看配置加载", "agent")
    second = tree.spawn("看看 env 解析", "agent")

    assert first.id == "s1"
    assert first.agent == "agent"
    assert second.id == "s2"
    assert tree.active is second


def test_back_pauses_and_returns_main() -> None:
    """/back 只暂停当前分支，不丢弃节点。"""
    tree = SideTree()
    first = tree.spawn("q1", "agent")
    tree.spawn("q2", "agent")

    tree.back()
    assert tree.active is None
    assert [task.id for task in tree.tasks()] == ["s1", "s2"]

    tree.switch("s1")
    assert tree.active is first


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


def test_close_discards_only_current_branch() -> None:
    """关闭当前分支不受其他兄弟分支影响，且回到主对话。"""
    tree = SideTree()
    tree.spawn("q1", "agent")
    tree.spawn("q2", "agent")

    removed = tree.close()

    assert removed == ["s2"]
    assert tree.active is None
    assert [task.id for task in tree.tasks()] == ["s1"]

    tree.switch("s1")
    assert tree.close() == ["s1"]
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


def test_render_marks_active_and_paused_without_nesting() -> None:
    """渲染包含主线与平级分支的状态，不出现缩进层级。"""
    tree = SideTree()
    tree.spawn("看看配置加载", "agent")
    tree.spawn("看看 env 解析", "agent")
    tree.back()

    text = tree.render()

    assert "main (active)" in text
    assert "  s1 (paused) 看看配置加载" in text
    assert "  s2 (paused) 看看 env 解析" in text
    assert "    s2" not in text


def test_empty_tree_renders_main_only() -> None:
    """没有侧任务时只显示主对话。"""
    tree = SideTree()

    assert tree.render() == "main (active)"

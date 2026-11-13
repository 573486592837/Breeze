"""内存中的只读侧任务树。

侧任务从当前活动节点派生，可嵌套成树；同一时刻只有一个活动节点，
其余节点保持 paused。侧任务只存在于当前进程内存中：不写会话文件、
没有 resume 接口，`close` 或退出即丢弃。

这里只维护树结构与生命周期，不负责构造 Agent、读取输入或渲染答案；
这些由 chat/CLI 层注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SideTask:
    """一个侧任务节点。

    Attributes:
        id: 当前进程内唯一 ID（s1、s2…）。
        parent_id: 父侧任务 ID；None 表示直接挂在主对话下。
        question: 创建侧任务时的问题。
        agent: 独立的只读 Agent，持有自己的消息与状态。
        turns: 已完成的侧任务回合数。
    """

    id: str
    parent_id: str | None
    question: str
    agent: Any
    turns: int = 0


class SideTree:
    """侧任务的创建、切换与销毁。

    `active_id` 为 None 表示当前在主对话。所有操作都在内存中完成；
    关闭节点会连同其子树一起丢弃。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, SideTask] = {}
        self._counter = 0
        self.active_id: str | None = None

    @property
    def active(self) -> SideTask | None:
        """当前活动的侧任务；None 表示主对话。"""
        if self.active_id is None:
            return None
        return self._tasks.get(self.active_id)

    def new_id(self) -> str:
        """预分配一个任务 ID，供调用方先创建 trace/Agent 再挂载。"""
        self._counter += 1
        return f"s{self._counter}"

    def spawn(self, question: str, agent: Any, task_id: str | None = None) -> SideTask:
        """以当前活动节点为父节点创建侧任务并切入。

        Args:
            question: 侧任务的起始问题。
            agent: 独立的只读 Agent。
            task_id: 预分配的 ID；None 时自动分配。

        Returns:
            新建的 SideTask。

        Raises:
            ValueError: 问题为空，或 ID 已存在。
        """
        text = question.strip()
        if not text:
            raise ValueError("side task question must not be empty")
        task_id = task_id or self.new_id()
        if task_id in self._tasks:
            raise ValueError(f"side task id already exists: {task_id}")
        task = SideTask(task_id, self.active_id, text, agent)
        self._tasks[task_id] = task
        self.active_id = task_id
        return task

    def back(self) -> str | None:
        """暂停当前侧任务并回到父节点。

        Returns:
            新的活动节点 ID；None 表示主对话。

        Raises:
            ValueError: 当前已经在主对话。
        """
        task = self.active
        if task is None:
            raise ValueError("already at main conversation")
        self.active_id = task.parent_id
        return self.active_id

    def close(self) -> list[str]:
        """彻底关闭当前侧任务及其子树，并回到父节点。

        Returns:
            被丢弃的节点 ID（含当前节点）。

        Raises:
            ValueError: 当前已经在主对话。
        """
        task = self.active
        if task is None:
            raise ValueError("already at main conversation")
        removed = self._subtree_ids(task.id)
        for task_id in removed:
            self._tasks.pop(task_id, None)
        self.active_id = task.parent_id
        return removed

    def switch(self, task_id: str) -> None:
        """切换到指定节点；``main`` 表示回主对话。

        Raises:
            KeyError: 节点不存在。
        """
        if task_id in ("", "main"):
            self.active_id = None
            return
        if task_id not in self._tasks:
            raise KeyError(task_id)
        self.active_id = task_id

    def tasks(self) -> tuple[SideTask, ...]:
        """按创建顺序返回所有保留的侧任务。"""
        return tuple(self._tasks.values())

    def render(self) -> str:
        """渲染树形列表，主对话作为根节点。"""
        children: dict[str | None, list[SideTask]] = {}
        for task in self._tasks.values():
            children.setdefault(task.parent_id, []).append(task)

        main_state = "active" if self.active_id is None else "paused"
        lines = [f"main ({main_state})"]

        def walk(parent_id: str | None, depth: int) -> None:
            for task in children.get(parent_id, []):
                state = "active" if task.id == self.active_id else "paused"
                question = " ".join(task.question.split())
                if len(question) > 40:
                    question = question[:37] + "..."
                lines.append(f"{'  ' * depth}{task.id} ({state}) {question}")
                walk(task.id, depth + 1)

        walk(None, 1)
        return "\n".join(lines)

    def _subtree_ids(self, root_id: str) -> list[str]:
        """返回 root 及其所有后代的 ID。"""
        children: dict[str, list[str]] = {}
        for task in self._tasks.values():
            if task.parent_id is not None:
                children.setdefault(task.parent_id, []).append(task.id)
        stack = [root_id]
        removed: list[str] = []
        while stack:
            current = stack.pop()
            removed.append(current)
            stack.extend(children.get(current, []))
        return removed

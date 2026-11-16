"""内存中的只读侧任务分支。

侧任务只能从主对话派生，不支持再嵌套；同一时刻只有一个活动节点，其余
分支保持 paused。分支创建时复制主对话的上下文，之后与主对话互不影响。
侧任务只存在于当前进程内存中：不写会话文件、没有 resume 接口，`close`
或进程退出即丢弃。

这里只维护分支结构与生命周期，不负责构造 Agent、读取输入或渲染答案；
这些由 chat/CLI 层注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SideTask:
    """一个从主对话派生的侧任务分支。

    Attributes:
        id: 当前进程内唯一 ID（s1、s2…）。
        question: 创建侧任务时的问题。
        agent: 独立的只读 Agent，持有自己的消息与状态。
        turns: 已完成的侧任务回合数。
    """

    id: str
    question: str
    agent: Any
    turns: int = 0


class SideTree:
    """主对话下的一层侧任务分支。

    `active_id` 为 None 表示当前在主对话。所有操作都在内存中完成；
    关闭分支后无法恢复。
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
        """从主对话创建一个侧任务分支并切入。

        Args:
            question: 侧任务的起始问题。
            agent: 独立的只读 Agent，通常已复制主对话上下文。
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
        task = SideTask(task_id, text, agent)
        self._tasks[task_id] = task
        self.active_id = task_id
        return task

    def back(self) -> None:
        """暂停当前侧任务并回到主对话。

        Raises:
            ValueError: 当前已经在主对话。
        """
        if self.active is None:
            raise ValueError("already at main conversation")
        self.active_id = None

    def close(self) -> list[str]:
        """彻底关闭当前侧任务分支并回到主对话。

        Returns:
            被丢弃的节点 ID（仅当前节点），关闭后不可恢复。

        Raises:
            ValueError: 当前已经在主对话。
        """
        task = self.active
        if task is None:
            raise ValueError("already at main conversation")
        self._tasks.pop(task.id, None)
        self.active_id = None
        return [task.id]

    def switch(self, task_id: str) -> None:
        """切换到指定分支；``main`` 表示回主对话。

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
        """渲染分支列表，主对话作为根节点。"""
        main_state = "active" if self.active_id is None else "paused"
        lines = [f"main ({main_state})"]
        for task in self._tasks.values():
            state = "active" if task.id == self.active_id else "paused"
            question = " ".join(task.question.split())
            if len(question) > 40:
                question = question[:37] + "..."
            lines.append(f"  {task.id} ({state}) {question}")
        return "\n".join(lines)

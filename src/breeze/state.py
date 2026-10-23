"""结构化会话状态。

SessionState 由 Agent 循环在每次工具执行后从真实返回值中维护，
记录符号位置、每个文件最近读取的行范围、编辑结果与最新测试状态。
轮压缩发生时，它随压缩摘要一起渲染成状态块：关键事实不再依赖
模型记忆，也不被 _render_summary 丢弃，从源头减少“凭记忆写
apply_patch 的 old”导致的失配。

状态只来自代码可解析的工具结果（参数与输出文本），不来自模型自述。
"""

from __future__ import annotations

import re
from typing import Any

from breeze.config import settings

_GREP_DEF_RE = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+):.*?\b(?:def|class)\s+(?P<name>\w+)")
_READ_DEF_RE = re.compile(r"^\s*(?P<line>\d+) \| (?:async )?(?:def|class) (?P<name>\w+)", re.M)
_TOTAL_LINES_RE = re.compile(r"file has (\d+) lines total")
_TESTS_SUMMARY_RE = re.compile(r"summary: (.+)")
_FIRST_FAILURE_RE = re.compile(r"^- (\S+::\S+)", re.M)


class SessionState:
    """跨压缩保留的会话事实集合。

    Attributes:
        symbols: 符号名 -> "file:line"，来自 grep 命中与 read 内容。
        reads: 文件路径 -> 最近读取描述，如 "lines 289-312 of 418"。
        edits: 文件路径 -> 最近一次编辑结果描述。
        tests: 最近一次 run_tests 的一行摘要。
    """

    def __init__(self, max_entries: int | None = None) -> None:
        """初始化。

        Args:
            max_entries: 每个 section 渲染的条目上限，None 时使用配置值。
        """
        self.max_entries = max_entries or settings.context_state_max_entries
        self.symbols: dict[str, str] = {}
        self.reads: dict[str, str] = {}
        self.edits: dict[str, str] = {}
        self.tests: str = ""

    def update(self, tool: str, arguments: dict[str, Any], result: str, step: int) -> None:
        """从一次工具执行结果更新状态。

        Args:
            tool: 工具名。
            arguments: 解析后的调用参数。
            result: 工具返回文本。
            step: Agent 循环步数。
        """
        if result.startswith("Error") or result.startswith("Tool execution failed"):
            if tool == "apply_patch":
                path = str(arguments.get("path", "?"))
                self.edits[path] = f"failed at step {step}"
            return

        if tool == "read_file":
            self._update_read(arguments, result, step)
        elif tool == "grep":
            self._update_grep(result)
        elif tool in ("apply_patch", "create_file"):
            path = str(arguments.get("path", "?"))
            verb = "created" if tool == "create_file" else "patched"
            self.edits[path] = f"{verb} at step {step}"
        elif tool == "run_tests":
            self._update_tests(result, step)

    def _update_read(self, arguments: dict[str, Any], result: str, step: int) -> None:
        """记录文件读取范围与其中的 def/class 符号。"""
        path = str(arguments.get("path", "?"))
        start = arguments.get("start_line")
        end = arguments.get("end_line")
        total_match = _TOTAL_LINES_RE.search(result)
        total = total_match.group(1) if total_match else "?"

        if start and end:  # noqa: SIM108
            desc = f"lines {start}-{end} of {total} (step {step})"
        else:
            desc = f"full {total} lines (step {step})"
        self.reads[path] = desc

        for match in _READ_DEF_RE.finditer(result):
            self.symbols.setdefault(match.group("name"), f"{path}:{match.group('line')}")

    def _update_grep(self, result: str) -> None:
        """从 grep 输出行提取 定义符号 -> file:line。"""
        for line in result.splitlines():
            match = _GREP_DEF_RE.match(line)
            if match:
                self.symbols.setdefault(match.group("name"), f"{match.group('path')}:{match.group('line')}")

    def _update_tests(self, result: str, step: int) -> None:
        """记录最近一次测试摘要与首个失败用例。"""
        summary = _TESTS_SUMMARY_RE.search(result)
        if not summary:
            return
        text = summary.group(1).strip()
        failure = _FIRST_FAILURE_RE.search(result)
        if failure:
            text += f" — failing: {failure.group(1)}"
        self.tests = f"{text} (step {step})"

    def render(self) -> str:
        """渲染紧凑状态块。空状态返回空字符串。

        Returns:
            多行文本，供压缩摘要拼接注入。
        """
        sections: list[str] = []

        if self.symbols:
            items = list(self.symbols.items())[-self.max_entries :]
            body = "\n".join(f"- {name}: {loc}" for name, loc in items)
            sections.append(f"Symbols located:\n{body}")

        if self.reads:
            items = list(self.reads.items())[-self.max_entries :]
            body = "\n".join(f"- {path}: {desc}" for path, desc in items)
            sections.append(f"Latest reads:\n{body}")

        if self.edits:
            items = list(self.edits.items())[-self.max_entries :]
            body = "\n".join(f"- {path}: {desc}" for path, desc in items)
            sections.append(f"Edits:\n{body}")

        if self.tests:
            sections.append(f"Tests: {self.tests}")

        if not sections:
            return ""

        lines = ["[Session state — maintained from real tool results]"]
        lines.extend(sections)
        lines.append(
            "Before apply_patch, re-read the target range above with read_file and copy old verbatim from that output."
        )
        return "\n".join(lines)

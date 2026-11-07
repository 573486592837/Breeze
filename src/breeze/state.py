"""结构化会话状态。

SessionState 由 Agent 循环在每次工具执行后从真实返回值中维护，
记录符号位置、每个文件最近读取的行范围、编辑结果与最新测试状态。
轮压缩发生时，它随压缩摘要一起渲染成状态块：关键事实不再依赖
模型记忆，也不被 _render_summary 丢弃，从源头减少“凭记忆写
apply_patch 的 old”导致的失配。

TurnLedger 记录已完成的问答回合（问题折叠为一行、答案保留原文换行并受
字符预算约束），供多回合会话在系统提示之后注入：上一回合的工具过程被
归档丢弃，只有结论保留——这正是回合间压缩的对象。

状态只来自代码可解析的工具结果（参数与输出文本），不来自模型自述。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from breeze.config import settings

PLAN_MAX_CHARS = 2000
_GREP_DEF_RE = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+):.*?\b(?:def|class)\s+(?P<name>\w+)")
_READ_DEF_RE = re.compile(r"^\s*(?P<line>\d+) \| (?:async )?(?:def|class) (?P<name>\w+)", re.M)
_TOTAL_LINES_RE = re.compile(r"file has (\d+) lines total")
_TESTS_SUMMARY_RE = re.compile(r"summary: (.+)")
_FIRST_FAILURE_RE = re.compile(r"^- (\S+::\S+)", re.M)
_EDIT_TOOLS = ("apply_patch", "create_file", "delete_path", "git_restore")
_EDIT_VERBS = {
    "apply_patch": "patched",
    "create_file": "created",
    "delete_path": "deleted",
    "git_restore": "restored",
}


class SessionState:
    """跨压缩保留的会话事实集合。

    Attributes:
        symbols: 符号名 -> "file:line"，来自 grep 命中与 read 内容。
        reads: 文件路径 -> 最近读取描述，按最近一次读取排序。
        read_counts: 本回合内的读取次数，用于重复读取提示，不跨回合持久化。
        read_hits: 文件路径 -> 跨回合读取次数，用于工作集快照选择。
        edits: 文件路径 -> 最近一次编辑结果描述。
        tests: 最近一次 run_tests 的一行摘要。
        plan: Agent 通过 update_plan 维护的短期计划。
    """

    def __init__(self, max_entries: int | None = None) -> None:
        """初始化。

        Args:
            max_entries: 每个 section 渲染的条目上限，None 时使用配置值。
        """
        self.max_entries = max_entries or settings.context_state_max_entries
        self.symbols: dict[str, str] = {}
        self.reads: dict[str, str] = {}
        self.read_counts: dict[tuple[str, Any, Any], int] = {}
        self.read_hits: dict[str, int] = {}
        self.edits: dict[str, str] = {}
        self.tests: str = ""
        self.tests_stale = False
        self.plan: str = ""

    def start_turn(self) -> None:
        """开始新的用户回合：清空回合内的读取计数。

        重复读取提示只对本回合的上下文有效；跨回合保留会让“已被压缩”
        的结论错误地延续到没有压缩发生的新回合。
        """
        self.read_counts.clear()

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
        elif tool in _EDIT_TOOLS:
            path = str(arguments.get("path", "?"))
            self.edits[path] = f"{_EDIT_VERBS[tool]} at step {step}"
            self._invalidate_path(path)
        elif tool == "run_tests":
            self._update_tests(result, step)
        elif tool == "update_plan":
            self.plan = _clip(str(arguments.get("content", "")), PLAN_MAX_CHARS)

    def _invalidate_path(self, path: str) -> None:
        """编辑或删除文件后丢弃该路径已失效的读取、符号与重读计数。

        文件内容已变，旧的行范围和符号位置不再可信；保留它们会让模型
        基于旧行号改代码或误以为无需重读。删除目录时其下文件一并失效。
        测试结果也随编辑变为过期。
        """
        self.reads = {
            p: desc for p, desc in self.reads.items() if not _same_or_under(p, path)
        }
        self.symbols = {
            name: loc
            for name, loc in self.symbols.items()
            if not _same_or_under(loc.rsplit(":", 1)[0], path)
        }
        self.read_counts = {
            key: count
            for key, count in self.read_counts.items()
            if not _same_or_under(key[0], path)
        }
        self.read_hits = {
            candidate: count
            for candidate, count in self.read_hits.items()
            if not _same_or_under(candidate, path)
        }
        if self.tests:
            self.tests_stale = True

    def _update_read(self, arguments: dict[str, Any], result: str, step: int) -> None:
        """记录文件读取范围与其中的 def/class 符号。"""
        path = str(arguments.get("path", "?"))
        start = arguments.get("start_line")
        end = arguments.get("end_line")
        total_match = _TOTAL_LINES_RE.search(result)
        total = total_match.group(1) if total_match else "?"

        self.read_counts[(path, start, end)] = self.read_counts.get((path, start, end), 0) + 1
        self.read_hits[path] = self.read_hits.get(path, 0) + 1

        if start and end:  # noqa: SIM108
            desc = f"lines {start}-{end} of {total} (step {step})"
        else:
            desc = f"full {total} lines (step {step})"
        self.reads.pop(path, None)
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
        self.tests_stale = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 友好的字典，供会话持久化使用。

        `read_counts` 只服务于本回合的重复读取提示，不落盘：恢复会话
        时开始的是新回合，历史计数没有意义且会误导模型。

        Returns:
            含 symbols/reads/edits/tests/plan 的字典。
        """
        return {
            "symbols": dict(self.symbols),
            "reads": dict(self.reads),
            "read_hits": dict(self.read_hits),
            "edits": dict(self.edits),
            "tests": self.tests,
            "tests_stale": self.tests_stale,
            "plan": self.plan,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        """从 to_dict 的字典恢复状态。

        Args:
            data: 序列化后的状态字典，缺字段按空处理。

        Returns:
            恢复出的 SessionState。
        """
        st = cls()
        st.symbols.update(data.get("symbols") or {})
        st.reads.update(data.get("reads") or {})
        st.read_hits.update(data.get("read_hits") or {})
        st.edits.update(data.get("edits") or {})
        st.tests = data.get("tests") or ""
        st.tests_stale = bool(data.get("tests_stale"))
        st.plan = data.get("plan") or ""
        return st

    def render(self, *, turn_start: bool = False) -> str:
        """渲染紧凑状态块。空状态返回空字符串。

        Args:
            turn_start: 是否为回合启动注入。此时事实来自之前的回合，
                文件内容不在当前上下文中，提示语要避免让模型误以为
                “最新副本已在上下文里”。

        Returns:
            多行文本，供上下文管理器拼接注入。
        """
        sections: list[str] = []

        if self.symbols:
            sections.append(f"Symbols located:\n{self._render_symbols()}")

        if self.reads:
            items = list(self.reads.items())[-self.max_entries :]
            body = "\n".join(f"- {path}: {desc}" for path, desc in items)
            sections.append(f"Latest reads:\n{body}")

        if self.edits:
            items = list(self.edits.items())[-self.max_entries :]
            body = "\n".join(f"- {path}: {desc}" for path, desc in items)
            sections.append(f"Edits:\n{body}")

        repeated = self._repeated_reads_lines()
        if repeated:
            sections.append("Repeated reads:\n" + "\n".join(repeated))

        if self.tests:
            text = self.tests
            if self.tests_stale:
                text += " (stale: files edited after this run; rerun verification)"
            sections.append(f"Tests: {text}")

        if not sections:
            return ""

        if turn_start:
            header = "[Session state — carried from previous turns]"
            footer = [
                "Facts above come from earlier tool results; their file contents are not in this context.",
                "Use this index for locate/list questions. Before editing, re-read only the ranges you need.",
                "Facts for an edited or restored file were discarded; re-read that file before editing it.",
            ]
        else:
            header = "[Session state — maintained from real tool results]"
            footer = [
                "This index is authoritative for locate/list questions: answer from it, and only re-read with "
                "read_file when a needed fact is missing here.",
                "If a needed range is no longer in context or the file changed, re-read only that range with "
                "read_file; otherwise edit from the latest copy already in context.",
            ]
        return "\n".join([header, *sections, *footer])

    def render_repeated(self) -> str:
        """只渲染本回合的重复读取警示；没有重复时返回空字符串。

        状态块只在压缩发生时注入，但重复读取本身就会消耗步数；
        单独暴露这一段，让上下文管理器无需压缩也能提醒模型。
        """
        lines = self._repeated_reads_lines()
        if not lines:
            return ""
        return "\n".join(
            ["[Repeated reads this turn — do not re-read an unchanged file]", *lines]
        )

    def _repeated_reads_lines(self) -> list[str]:
        """生成重复读取提醒行。"""
        rereads = [(key, count) for key, count in self.read_counts.items() if count >= 2]
        if not rereads:
            return []
        lines: list[str] = []
        for (path, start, end), count in rereads[-self.max_entries :]:
            rng = f"lines {start}-{end}" if start and end else "full file"
            lines.append(
                f"- {path} {rng} read {count} times this turn. "
                f"If the latest copy is still in context, edit from it instead of reading again."
            )
        return lines

    def _render_symbols(self) -> str:
        """按文件最近出现顺序保留 max_entries 个文件，文件内符号全部列出。

        单文件的符号不因上限被截断——"把函数列一遍"这类追问需要完整清单，
        截断会迫使模型重新通读文件。

        Returns:
            每行 "- 符号名: 文件:行号" 的正文文本。
        """
        grouped: dict[str, list[str]] = {}
        recency: dict[str, int] = {}
        for idx, (name, loc) in enumerate(self.symbols.items()):
            path = loc.rsplit(":", 1)[0]
            grouped.setdefault(path, []).append(f"- {name}: {loc}")
            recency[path] = idx

        recent = sorted(grouped, key=lambda path: recency[path])[-self.max_entries :]
        return "\n".join(line for path in recent for line in grouped[path])


def _same_or_under(candidate: str, root: str) -> bool:
    """判断路径是否等于 root 或位于 root 目录下。"""
    if candidate == root:
        return True
    return candidate.startswith(root + "/") or candidate.startswith(root + "\\")


def _fold(text: str, limit: int) -> str:
    """折叠为单行并按 limit 截断。

    Args:
        text: 任意文本。
        limit: 最大字符数。

    Returns:
        单行文本；超长时截断并以省略号结尾。
    """
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 3)] + "..."


def _clip(text: str, limit: int) -> str:
    """保留换行结构地按 limit 截断文本。

    Args:
        text: 任意文本。
        limit: 最大字符数。

    Returns:
        去除首尾空白后的文本；超长时截断并以省略号结尾。
    """
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return stripped[: max(0, limit - 3)] + "..."


@dataclass
class TurnRecord:
    """一个已完成回合的归档条目。

    Attributes:
        index: 回合序号（从 1 开始）。
        question: 用户问题。
        answer: 该回合的最终答案。
        touched: 该回合触碰过的文件路径列表。
    """

    index: int
    question: str
    answer: str
    touched: list[str]


class TurnLedger:
    """跨回合的问答归档。

    每个用户回合结束时记录问题、最终答案与触碰文件；下一回合开始时，
    把已有记录渲染为一条注入消息（放在系统提示之后、当前回合之前）。
    这样"上一回合的工具调用结果"不会污染当前回合的窗口——只有其结论
    保留，正是回合间压缩的对象。

    答案保留原文的换行结构（列表、小节标题），并设两道预算：单条答案
    answer_preview 字符上限，整块归档 max_chars 字符上限——从最新回合
    往前累加，装不下的旧回合丢弃，避免长会话里归档吃满回合内预算。

    Attributes:
        records: 已完成的回合记录，从旧到新。
        answer_preview: 归档中答案的最大字符数。
        max_records: 渲染时保留的最近回合数上限。
        max_chars: 归档整块的最大字符数。
    """

    def __init__(
        self,
        answer_preview: int | None = None,
        max_records: int | None = None,
        max_chars: int | None = None,
    ) -> None:
        """初始化。

        Args:
            answer_preview: 单条答案归档的最大字符数，None 时使用配置值。
            max_records: 渲染时保留的最近回合数，None 时使用配置值。
            max_chars: 归档渲染的总字符数上限，None 时使用配置值。
        """
        self.records: list[TurnRecord] = []
        self.answer_preview = answer_preview or settings.chat_answer_preview
        self.max_records = max_records or settings.chat_max_records
        self.max_chars = max_chars or settings.chat_max_chars

    def record(self, question: str, answer: str, touched: list[str]) -> None:
        """归档一个刚完成的回合。

        Args:
            question: 该回合的用户问题。
            answer: 该回合的最终答案。
            touched: 该回合读过或改过的文件路径。
        """
        self.records.append(
            TurnRecord(
                index=len(self.records) + 1,
                question=_fold(question, 200),
                answer=_clip(answer, self.answer_preview),
                touched=list(dict.fromkeys(touched)),
            )
        )

    def render(self) -> str:
        """渲染回合归档文本块。无记录时返回空字符串。

        Returns:
            多行文本，供 ContextManager 注入到当前回合之前。
        """
        if not self.records:
            return ""

        blocks: list[str] = []
        used = 0
        for rec in reversed(self.records[-self.max_records :]):
            block = self._block(rec)
            if blocks and used + len(block) > self.max_chars:
                break
            blocks.append(block)
            used += len(block)

        header = f"[Completed turns — {len(blocks)} most recent of {len(self.records)}]"
        return "\n".join([header, *reversed(blocks)])

    @staticmethod
    def _block(rec: TurnRecord) -> str:
        """渲染单条回合记录。"""
        lines = [f"Turn {rec.index} Q: {rec.question}", f"Turn {rec.index} A: {rec.answer}"]
        if rec.touched:
            files = ", ".join(rec.touched[:8])
            more = f" (+{len(rec.touched) - 8} more)" if len(rec.touched) > 8 else ""
            lines.append(f"Turn {rec.index} touched: {files}{more}")
        return "\n".join(lines)

    def touched_files(self) -> list[str]:
        """返回历史回合触碰过的全部文件（去重、按首次出现）。

        Returns:
            文件路径列表。
        """
        seen: dict[str, None] = {}
        for rec in self.records:
            for path in rec.touched:
                seen.setdefault(path, None)
        return list(seen)

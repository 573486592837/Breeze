"""Agent Loop 实现。

ReAct 风格的代码仓库 Agent，使用 LLM 的 function calling 能力多步调用工具。
"""

import contextlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from breeze.capabilities import Capabilities, detect
from breeze.config import PERMISSION_MODES, settings
from breeze.context import ContextManager
from breeze.instructions import load_project_instructions, render_project_instructions
from breeze.llm import LLMClient, LLMResponse
from breeze.state import SessionState, TurnLedger
from breeze.tools import registry, shell
from breeze.trace import RunTrace

# Deprecated: use settings.agent_max_steps instead.
# Kept for backward compatibility with code that imports this constant.
DEFAULT_MAX_STEPS = 10

_EDIT_TOOLS = frozenset({"apply_patch", "create_file", "delete_path", "git_restore"})
_WRITE_TOOLS = frozenset({"apply_patch", "create_file", "delete_path"})

# 真实试跑中出现过：模型只读文件就断言"已修复"，并粘贴从未写入的代码。
# 这里只匹配第一人称的完成声明，避免误伤对既有改动的解释。
_EDIT_CLAIM_RE = re.compile(
    r"我[^。\n]{0,30}?(?:已经?|完成了?|做好了?)[^。\n]{0,20}?"
    r"(?:修改|修复|实现|添加|创建|新增|删除|更新|调整|对齐|替换|写入)"
    r"|我[^。\n]{0,10}?(?:修改|修复|实现|添加|创建|新增|删除|更新|调整|对齐|替换)了"
    r"|\bI\s+(?:have\s+)?(?:fixed|implemented|added|created|updated|modified|changed|refactored)\b",
    re.IGNORECASE,
)

_EDIT_CLAIM_NUDGE = (
    "你的回答声称已经完成代码修改，但本轮没有任何成功的 apply_patch / "
    "create_file / git_restore 调用。请现在实际执行编辑；如果确认不需要修改，"
    "明确说明“本轮未修改”及原因。不要把未写入文件的代码当作已完成。"
)

_EMPTY_ANSWER_NUDGE = (
    "你刚才的回复没有内容。请用简洁的中文直接给出最终答案："
    "准确说明已经完成和尚未完成的部分，不要再调用工具。"
)


def _claims_completed_edit(text: str) -> bool:
    """判断最终回答是否声称本轮完成了代码修改。

    Args:
        text: 模型给出的最终回答。

    Returns:
        若包含第一人称的完成声明则返回 True。
    """
    return bool(_EDIT_CLAIM_RE.search(text))


def _environment_note(capabilities: Capabilities) -> str:
    """生成运行环境说明：仓库根、外部命令与项目类型。

    模型常在缺少外部命令时反复试错（例如机器没装 ripgrep 却用 run_shell 调
    rg，再改用被白名单拒绝的 find）。提前告诉它哪些命令可用，可以省掉这些步数。

    Args:
        capabilities: 启动时探测到的环境能力。

    Returns:
        多行说明文本，且不含可变的时间戳等内容。
    """
    command_fields = (("git", capabilities.git), ("rg", capabilities.rg), ("node", capabilities.node))
    available = [name for name, ok in command_fields if ok]
    missing = [name for name, ok in command_fields if not ok]
    lines = [f"仓库根目录：{settings.repo_root}"]
    if settings.tools_read_roots:
        roots = "、".join(str(path) for path in settings.tools_read_roots)
        lines.append(f"只读参考目录：{roots}（可读，不可写）")
    if missing:
        lines.append(
            f"外部命令可用：{'、'.join(available) if available else '无'}；"
            f"缺失：{'、'.join(missing)}。缺失的命令不要用 run_shell 尝试："
            "代码搜索用 grep 工具（纯 Python 回退），浏览文件用 list_files。"
        )
    else:
        lines.append(f"外部命令可用：{'、'.join(available)}。")
    if not capabilities.git_tools:
        lines.append("当前目录不是 git 工作区：git_diff / git_restore 不可用，不要尝试调用。")
    if not capabilities.python_project:
        lines.append("未检测到 Python 项目：run_tests 不可用；测试请用 run_shell（如 node --test）。")
    return "运行环境：\n" + "\n".join(f"- {line}" for line in lines)


_GIT_DIFF_RULE = "3. 需要确认整体改动时用 git_diff，例如多次修改后或回滚前。单次修改可直接继续。"
_GIT_DIFF_FALLBACK = "3. 需要确认整体改动时重新读取相关文件；当前目录不是 git 工作区，git_diff 不可用。"
_GIT_RESTORE_RULE = "5. 如果修改有误，用 git_restore 回滚单个文件后重试。"
_GIT_RESTORE_FALLBACK = (
    "5. 如果修改有误，重新读取文件后用 apply_patch 修正；当前目录不是 git 工作区，"
    "git_restore 不可用。"
)


def _adapt_git_rules(prompt: str, capabilities: Capabilities) -> str:
    """非 git 工作区时，把提示词里的 git 工具规则替换为替代方案。"""
    if capabilities.git_tools:
        return prompt
    return prompt.replace(_GIT_DIFF_RULE, _GIT_DIFF_FALLBACK).replace(
        _GIT_RESTORE_RULE, _GIT_RESTORE_FALLBACK
    )


_SHELL_RULE_STRICT = "12. run_shell 的可执行文件仅限 git / node / python / python3 / rg / where。"
_SHELL_RULE_CONFIRM = (
    "12. run_shell 默认只允许 git / node / python / python3 / rg / where；"
    "其他命令会先询问用户是否执行，用户拒绝后不要反复尝试。"
)
_SHELL_RULE_OPEN = (
    "12. run_shell 当前为 open 模式，可执行任意命令；命令仍以 shell=False "
    "直接执行，一次只跑一条命令。"
)


def _adapt_shell_rule(prompt: str, permission_mode: str) -> str:
    """按权限模式改写 run_shell 规则，避免 open/confirm 下模型自我封锁。"""
    if permission_mode == "confirm":
        return prompt.replace(_SHELL_RULE_STRICT, _SHELL_RULE_CONFIRM)
    if permission_mode == "open":
        return prompt.replace(_SHELL_RULE_STRICT, _SHELL_RULE_OPEN)
    return prompt


def _enabled_tool_names(capabilities: Capabilities) -> frozenset[str]:
    """按环境能力裁剪工具集。"""
    names = set(registry.TOOLS)
    if not capabilities.git_tools:
        names -= {"git_diff", "git_restore"}
    if not capabilities.python_project:
        names.discard("run_tests")
    return frozenset(names)


def _shell_limits(arguments: dict[str, Any]) -> dict[str, Any]:
    """提取 run_shell 的可选执行限制，避免把 None 传给执行函数。"""
    limits: dict[str, Any] = {}
    for key in ("timeout", "max_bytes"):
        value = arguments.get(key)
        if value is not None:
            limits[key] = value
    return limits


_EDIT_LIMIT_RULE = (
    "8. 单次任务中的 apply_patch 调用次数有限（默认 5 次）。\n"
    "   超过后会被拒绝，此时应报告失败或回滚。"
)

_TEST_RULE_PY = (
    "4. 如果需要运行测试：Python 项目优先使用 run_tests（需要特殊 pytest 参数时\n"
    "   改用 run_shell 的 'python -m pytest'）；JS 项目用 run_shell 的 'node --test'。\n"
    "   不要用 npm / npx / yarn 等包管理器。"
)
_TEST_RULE_NO_PY = (
    "4. 当前未检测到 Python 项目，run_tests 不可用。JS 项目用 run_shell 的 'node --test'；\n"
    "   其他项目使用项目自带的测试命令。不要用 npm / npx / yarn 等包管理器。"
)
_TEST_RULE_9_PY = (
    "9. 修复类任务在最后一次编辑后，必须重新运行此前失败的测试确认通过。\n"
    "   全量测试用 run_tests（省略 pattern），超时不等于通过；确需特殊 pytest\n"
    "   参数时再用 run_shell 的 python -m pytest。任务要求全量测试通过时，"
)
_TEST_RULE_9_NO_PY = (
    "9. 修复类任务在最后一次编辑后，必须重新运行此前失败的测试确认通过。\n"
    "   全量测试使用项目自带的测试命令（JS 项目用 node --test）；超时不等于通过。"
    "任务要求全量测试通过时，"
)


def _adapt_test_rules(prompt: str, capabilities: Capabilities) -> str:
    """非 Python 项目时，改写依赖 run_tests 的规则。"""
    if capabilities.python_project:
        return prompt
    return prompt.replace(_TEST_RULE_PY, _TEST_RULE_NO_PY).replace(
        _TEST_RULE_9_PY, _TEST_RULE_9_NO_PY
    )


def _edit_limit_rule(limit: int) -> str:
    """生成当前 max_edit_failures 对应的提示文本。"""
    return (
        f"8. 每次用户回合允许连续写操作失败 {limit} 次"
        "（apply_patch / create_file / delete_path）；成功一次后计数清零。"
        "达到上限后这些工具会被拒绝，应停止编辑并准确报告已完成和未完成的部分。"
    )


def default_system_prompt(
    capabilities: Capabilities | None = None,
    permission_mode: str | None = None,
    max_edit_failures: int | None = None,
) -> str:
    """默认系统提示，附带当前运行环境说明与仓库级 AGENTS.md 指引。

    Args:
        capabilities: 环境能力；None 时探测 settings.repo_root。
        permission_mode: run_shell 权限模式；None 时使用配置值。
        max_edit_failures: 连续编辑失败上限；None 时使用配置值。
    """
    caps = capabilities if capabilities is not None else detect(settings.repo_root)
    mode = (permission_mode or settings.shell_permission).lower()
    limit = settings.agent_max_edit_failures if max_edit_failures is None else max_edit_failures
    prompt = _adapt_shell_rule(_adapt_git_rules(DEFAULT_SYSTEM_PROMPT, caps), mode)
    prompt = _adapt_test_rules(prompt, caps)
    prompt = prompt.replace(_EDIT_LIMIT_RULE, _edit_limit_rule(limit))
    parts = [prompt, _environment_note(caps)]
    instructions, _ = load_project_instructions()
    if instructions:
        parts.append(render_project_instructions(instructions))
    return "\n\n".join(parts)


DEFAULT_SYSTEM_PROMPT = """你是 Breeze，一个代码仓库 Agent。
你可以使用工具查看文件、搜索代码、运行命令、修改代码。

工作规则：
1. 回答必须基于工具观察，不要编造文件内容。
2. 定位代码遵循固定流程：先 grep 搜索符号名（如 "def <符号名>"、常量名、字符串），
   得到 文件:行号 后用 read_file 的 start_line/end_line 精确读取目标范围。
   任务已给出符号名时，禁止通读整个大文件，也禁止反复小范围试探。
   read_file 结果会显示文件总行数；总行数不超过约 400 行时，改为省略行号
   直接整文件读取，不要再用 grep + 分段读取反复试探。
3. 互不依赖的工具调用应在同一轮一次性发起（例如同时 grep 定位多个符号、
   并行读取多个已定位的文件）；只有后一个调用需要前一个调用的结果时才分轮。
4. 调用工具前先判断是否已有足够信息；只需读取一个文件就能回答时，不要扩大范围；
   一旦能回答就停止调用工具并给出答案。
5. 遇到工具错误时，简要说明原因，不要反复重试同一工具。
6. 最终答案应简洁、直接，引用关键文件路径和函数名。

编辑代码时：
1. 修改前用 read_file 确认目标位置和上下文。
2. 修改已存在的文件用 apply_patch，old 必须逐字来自最近一次 read_file 的输出
   并唯一匹配文件中的原文；目标区域已不在上下文或不确定时先重新读取，
   禁止凭记忆编写 old。创建新文件用 create_file。
3. 需要确认整体改动时用 git_diff，例如多次修改后或回滚前。单次修改可直接继续。
4. 如果需要运行测试：Python 项目优先使用 run_tests（需要特殊 pytest 参数时
   改用 run_shell 的 'python -m pytest'）；JS 项目用 run_shell 的 'node --test'。
   不要用 npm / npx / yarn 等包管理器。
5. 如果修改有误，用 git_restore 回滚单个文件后重试。
6. 不要修改 .git 与 .env*；其他受保护路径会在工具层拒绝。删除文件或目录用 delete_path，
   不要用 run_shell 的 rm / del；非空目录必须传 recursive=true。
7. 不要删除或弱化测试来让测试通过；新增或更新测试覆盖新行为是允许的。
8. 单次任务中的 apply_patch 调用次数有限（默认 5 次）。
   超过后会被拒绝，此时应报告失败或回滚。
9. 修复类任务在最后一次编辑后，必须重新运行此前失败的测试确认通过。
   全量测试用 run_tests（省略 pattern），超时不等于通过；特殊 pytest 参数
   改用 run_shell 的 python -m pytest。任务要求全量测试通过时，最后一条
   测试命令必须退出码为 0；仍有失败就继续修复，或在答案中明确报告
   "未完成及原因"，不得以"与本次任务无关"为由跳过。
10. 任务包含互斥的条件分支时，先判断命中哪个分支，只执行该分支的工作。
11. 要求修改代码的任务，必须在本次任务内用 apply_patch 或 create_file 实际完成
   全部编辑并运行验证，不得以方案、计划或建议结尾。确实无法完成时，
   明确说明哪些编辑未执行以及原因。最终答案里的“已修改 / 已修复 / 已创建”
   只能对应本轮成功的编辑工具调用；只读文件不算完成，不得粘贴未写入文件的代码。
12. run_shell 的可执行文件仅限 git / node / python / python3 / rg / where。
    一次只跑一条命令，不用 && / || / ; / 管道 / 重定向串联；列目录用
    list_files，不要用 ls / dir；搜索用 grep，不要用 run_shell 调 grep / rg / find；
    不要用 cd 切换目录，需要时传 cwd 参数；内联 Python 用 `python -m <module>`，
    不要用 `python -c`；node 只用 `node --test` 或 `node <脚本路径>`，
    不用 `node -e` / `--eval` / `-p` 或包管理器。
13. 多步任务开始时用 update_plan 写一份 3-7 步计划；每完成一步或方向发生
    变化后用完整的新计划覆盖更新。计划保持简短，不要粘贴工具输出，
    回答中不必重复。
14. 同一文件已在本回合完整读取且未被修改时，不要重复整文件读取；
    直接基于最近一次读取编辑。只有目标区域不在上下文或文件已被修改时才重读。
15. apply_patch 成功后先用返回结果和测试验证；不要立刻再次整文件读取，
    只有需要继续修改同一文件的其它区域时才用 start_line/end_line 定点重读。
"""


@dataclass
class _ToolCall:
    """内部工具调用表示。"""

    id: str
    name: str
    arguments_json: str


@dataclass
class AgentEvent:
    """运行过程中的进度事件。

    仅用于 CLI 展示，不进入 prompt，也不写入 trace：事件回调抛出的异常
    会被吞掉，因此展示层的问题不会改变任务结果。

    Attributes:
        kind: "llm_start" / "llm_end" / "tool_start" / "tool_end"。
        step: 从 0 开始的循环步数。
        tool: 工具名，tool_* 事件才有值。
        tokens: 本次 LLM 调用的 total_tokens。
        tool_calls: 本次 LLM 返回的工具调用数量。
        duration_ms: 工具执行耗时。
        ok: 工具是否执行成功。
        detail: 错误摘要等补充信息。
    """

    kind: str
    step: int
    tool: str = ""
    tokens: int = 0
    tool_calls: int = 0
    duration_ms: float = 0.0
    ok: bool = True
    detail: str = ""


class Agent:
    """ReAct 风格的代码仓库 Agent。

    每步调用 LLM，若有工具调用则执行并追加结果，否则返回最终答案。
    """

    def __init__(
        self,
        llm: LLMClient,
        trace: RunTrace,
        max_steps: int | None = None,
        max_edit_failures: int | None = None,
        system_prompt: str | None = None,
        context: ContextManager | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        capabilities: Capabilities | None = None,
        permission_mode: str | None = None,
        confirm_command: Callable[[str], bool] | None = None,
    ) -> None:
        """初始化 Agent。

        Args:
            llm: LLM 客户端。
            trace: 运行追踪记录器。
            max_steps: 最大循环步数。
            max_edit_failures: 单次运行允许的连续编辑失败次数；成功编辑后清零。
            system_prompt: 系统提示。None 时使用默认提示并附当前运行环境说明。
            context: 上下文管理器。
            on_event: 进度回调，仅用于展示；不影响 prompt 与 trace。
            capabilities: 环境能力；None 时探测 settings.repo_root。
            permission_mode: run_shell 权限模式（strict / confirm / open）。
            confirm_command: confirm 模式下询问用户是否允许命令的回调。
        """
        self.llm = llm
        self.trace = trace
        self.on_event = on_event
        self._custom_capabilities = capabilities is not None
        self.capabilities = (
            capabilities if capabilities is not None else detect(settings.repo_root)
        )
        self.tool_names = _enabled_tool_names(self.capabilities)
        mode = (permission_mode or settings.shell_permission).lower()
        if mode not in PERMISSION_MODES:
            raise ValueError(
                f"Invalid permission mode: {mode}. Choose from {', '.join(PERMISSION_MODES)}."
            )
        self.permission_mode = mode
        self.confirm_command = confirm_command
        self.max_steps = max_steps if max_steps is not None else settings.agent_max_steps
        self.max_edit_failures = (
            max_edit_failures if max_edit_failures is not None else settings.agent_max_edit_failures
        )
        self._custom_system_prompt = system_prompt
        self.system_prompt = (
            system_prompt
            if system_prompt is not None
            else default_system_prompt(
                self.capabilities,
                self.permission_mode,
                max_edit_failures=self.max_edit_failures,
            )
        )
        self.context = context or ContextManager()
        self.state = SessionState()
        self.ledger = TurnLedger()
        self.messages: list[dict[str, Any]] = []

    def run(self, question: str) -> str:
        """执行一个用户回合，返回最终答案。

        可在同一 Agent 上重复调用以支持多回合会话：历史回合的完整
        消息日志保留在 self.messages 供调试，但只有问答结论经
        TurnLedger 归档注入下一回合，工具过程不进入下一轮窗口。

        Args:
            question: 用户问题。

        Returns:
            最终答案字符串。达到最大步数或出错时返回提示文本。
        """
        self._refresh_capabilities()
        self._refresh_default_prompt()
        if not self.messages:
            self.messages = [{"role": "system", "content": self.system_prompt}]
        else:
            self.messages[0] = {"role": "system", "content": self.system_prompt}
        self.messages.append({"role": "user", "content": question})
        turn_start = len(self.messages) - 1

        edits_this_turn = 0
        edit_failures = 0
        edit_claim_nudged = False

        for step in range(self.max_steps):
            remaining = self.max_steps - step

            if self.max_steps >= 8 and step == self.max_steps // 2 and edits_this_turn == 0:
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "你已经用完一半的步数但还没有执行任何编辑。调研必须收敛："
                            "从现在起，每读完一个待改区域就立即 apply_patch 并运行验证，"
                            "不要再扩大阅读范围。"
                        ),
                    }
                )
            elif remaining == 2:
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "你还有 1 次工具调用机会。如果已有足够信息回答用户问题，"
                            "请立即给出最终答案，不要再调用工具。"
                        ),
                    }
                )
            elif remaining == 1:
                self.messages.append(
                    {
                        "role": "user",
                        "content": ("这是最后一步。请基于已获取的信息直接给出最终答案，不要再调用工具。"),
                    }
                )

            tools = registry.schemas(self.tool_names)
            tool_choice = None if remaining > 1 else "none"
            view = self._llm_view(turn_start)
            llm_messages, prompt_stats = self.context.build_with_stats(
                view,
                state=self.state,
                archive=self._turn_archive(),
                plan=self.state.plan,
            )

            self._emit(AgentEvent("llm_start", step))

            try:
                resp, latency_ms = self._call_llm(llm_messages, tools, tool_choice)
            except Exception as exc:
                self.trace.log_error(step, f"LLM call failed: {exc}")
                self._emit(AgentEvent("llm_end", step, ok=False, detail=str(exc)))
                return self._finish_turn(question, f"LLM call failed: {exc}", turn_start)

            tool_calls = _convert_tool_calls(resp.tool_calls)

            self.trace.log_llm(
                step=step,
                model=getattr(self.llm, "model", "unknown"),
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
                total_tokens=resp.total_tokens,
                latency_ms=latency_ms,
                content=resp.content,
                tool_calls=[{"name": tc.name, "arguments": tc.arguments_json} for tc in tool_calls],
                cache_hit_tokens=resp.cache_hit_tokens,
                cache_miss_tokens=resp.cache_miss_tokens,
                cost_usd=resp.cost_usd,
                prompt_breakdown=prompt_stats.breakdown(),
                cacheable_prefix_tokens=prompt_stats.cacheable_prefix_tokens,
                messages=llm_messages,
                tools=tools,
                tool_choice=tool_choice,
                temperature=getattr(self.llm, "temperature", settings.llm_temperature),
            )
            self._emit(
                AgentEvent(
                    "llm_end",
                    step,
                    tokens=resp.total_tokens,
                    tool_calls=len(tool_calls),
                )
            )

            if remaining == 1 and tool_calls:
                names = ", ".join(tc.name for tc in tool_calls)
                self.trace.log_guard(step, f"tool calls ignored at final step: {names}")
                answer = (resp.content or "").strip()
                if answer:
                    return self._finish_turn(question, answer, turn_start)
                return self._finish_turn(
                    question,
                    f"Reached max steps ({self.max_steps}), task not completed.",
                    turn_start,
                )

            if not tool_calls:
                answer = resp.content or ""
                if not answer.strip():
                    answer = self._recover_empty_answer(step, turn_start)
                if (
                    _claims_completed_edit(answer)
                    and edits_this_turn == 0
                    and not edit_claim_nudged
                ):
                    edit_claim_nudged = True
                    self.messages.append({"role": "assistant", "content": answer})
                    self.messages.append({"role": "user", "content": _EDIT_CLAIM_NUDGE})
                    self.trace.log_guard(
                        step,
                        "final answer claimed edits but no edit tool succeeded; re-prompting",
                    )
                    continue
                return self._finish_turn(question, answer, turn_start)

            self.messages.append(
                {
                    "role": "assistant",
                    "content": resp.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": tc.arguments_json,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tc in tool_calls:
                if tc.name in _WRITE_TOOLS and edit_failures >= self.max_edit_failures:
                    error_msg = (
                        f"Error: max_edit_failures ({self.max_edit_failures}) reached "
                        f"for this turn after {edit_failures} consecutive failed edits. "
                        f"Stop editing and report the completed and remaining changes "
                        f"accurately; do not claim the task is complete."
                    )
                    self.trace.log_tool(
                        step=step,
                        tool=tc.name,
                        arguments={"raw": tc.arguments_json},
                        result=error_msg,
                        latency_ms=0.0,
                        error=error_msg,
                    )
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": error_msg,
                        }
                    )
                    continue

                result = self._execute_and_append(tc, step)
                succeeded = result is not None and not result.startswith(
                    ("Error", "Tool execution failed", "Failed")
                )
                if tc.name in _EDIT_TOOLS and succeeded:
                    edits_this_turn += 1
                if tc.name in _WRITE_TOOLS:
                    if succeeded:
                        edit_failures = 0
                        self._refresh_capabilities_after_write()
                    else:
                        edit_failures += 1

        return self._finish_turn(question, f"Reached max steps ({self.max_steps}), task not completed.", turn_start)

    def _refresh_capabilities(self) -> bool:
        """重新探测环境能力；变化时同步工具集。"""
        if self._custom_capabilities:
            return False
        caps = detect(settings.repo_root)
        if caps == self.capabilities:
            return False
        self.capabilities = caps
        self.tool_names = _enabled_tool_names(caps)
        return True

    def _refresh_capabilities_after_write(self) -> None:
        """写操作可能创建 Python 项目标记，必要时立即刷新工具集。

        只在当前缺少 Python 项目时探测，避免每次编辑都跑
        ``git rev-parse``。能力变化时同步 system prompt，使下一步的
        LLM 调用就能看到新工具。
        """
        if self.capabilities.python_project:
            return
        if not self._refresh_capabilities():
            return
        if self._custom_system_prompt is not None:
            return
        self._refresh_default_prompt()
        if self.messages:
            self.messages[0] = {"role": "system", "content": self.system_prompt}

    def _refresh_default_prompt(self) -> None:
        """重建默认 system prompt，使配置变更在下一回合生效。"""
        if self._custom_system_prompt is not None:
            return
        self.system_prompt = default_system_prompt(
            self.capabilities,
            self.permission_mode,
            max_edit_failures=self.max_edit_failures,
        )

    def reset(self) -> None:
        """清空消息日志、回合归档与会话状态，开始新会话。

        使用默认系统提示时顺带重读 AGENTS.md，用户改完指引后 /new 即可生效。
        """
        self.messages = []
        self.ledger = TurnLedger()
        self.state = SessionState()
        self.context.reset()
        self._refresh_default_prompt()

    def _recover_empty_answer(self, step: int, turn_start: int) -> str:
        """最终回答为空时，再请求一次总结。

        推理模型在最后一步（工具已收起）偶尔只产出推理内容、content 为空；
        直接把 "(empty response)" 交给用户，会看起来像任务没有结果。

        Args:
            step: 触发恢复的循环步数，用于 trace。
            turn_start: 本回合在消息日志中的起始下标。

        Returns:
            恢复调用得到的回答；仍为空时返回 "(empty response)"。
        """
        self.messages.append({"role": "assistant", "content": "(empty response)"})
        self.messages.append({"role": "user", "content": _EMPTY_ANSWER_NUDGE})
        self.trace.log_guard(step, "empty final answer; asking for a summary once")

        view = self._llm_view(turn_start)
        llm_messages, prompt_stats = self.context.build_with_stats(
            view,
            state=self.state,
            archive=self._turn_archive(),
            plan=self.state.plan,
        )

        self._emit(AgentEvent("llm_start", step))
        try:
            resp, latency_ms = self._call_llm(llm_messages, [])
        except Exception as exc:
            self.trace.log_error(step, f"LLM call failed: {exc}")
            self._emit(AgentEvent("llm_end", step, ok=False, detail=str(exc)))
            return "(empty response)"

        self.trace.log_llm(
            step=step,
            model=getattr(self.llm, "model", "unknown"),
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            total_tokens=resp.total_tokens,
            latency_ms=latency_ms,
            content=resp.content,
            tool_calls=[],
            cache_hit_tokens=resp.cache_hit_tokens,
            cache_miss_tokens=resp.cache_miss_tokens,
            cost_usd=resp.cost_usd,
            prompt_breakdown=prompt_stats.breakdown(),
            cacheable_prefix_tokens=prompt_stats.cacheable_prefix_tokens,
            messages=llm_messages,
            tools=[],
            temperature=getattr(self.llm, "temperature", settings.llm_temperature),
        )
        self._emit(AgentEvent("llm_end", step, tokens=resp.total_tokens, tool_calls=0))
        return (resp.content or "").strip() or "(empty response)"

    def _finish_turn(self, question: str, answer: str, turn_start: int) -> str:
        """把本回合的问答结论与触碰文件归档，返回答案。

        Args:
            question: 本回合的用户问题。
            answer: 本回合最终答案。
            turn_start: 本回合在消息日志中的起始下标。

        Returns:
            答案原文（便于调用点直接 return）。
        """
        self.messages.append({"role": "assistant", "content": answer})

        touched: list[str] = []
        for msg in self.messages[turn_start:]:
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                raw = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                path = args.get("path") if isinstance(args, dict) else None
                if isinstance(path, str) and path not in touched:
                    touched.append(path)

        self.ledger.record(question, answer, touched)
        return answer

    def _llm_view(self, turn_start: int) -> list[dict[str, Any]]:
        """选择发给上下文管理器的消息视图。

        ``archive`` 模式只发当前回合；``append`` 模式保留全部历史，
        用于和“只累计不压缩”的方案做对照。
        """
        if self.context.config.cross_turn == "append":
            return list(self.messages)
        return [self.messages[0], *self.messages[turn_start:]]

    def _turn_archive(self) -> str | None:
        """append 模式下历史已完整保留，不再重复注入回合归档。"""
        if self.context.config.cross_turn == "append":
            return None
        return self.ledger.render()

    def _emit(self, event: AgentEvent) -> None:
        """触发进度回调。回调异常不得影响任务执行。"""
        if self.on_event is None:
            return
        with contextlib.suppress(Exception):
            self.on_event(event)

    def _call_llm(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str | None = None,
    ) -> tuple[LLMResponse, float]:
        """调用 LLM 并计时。

        Args:
            messages: 消息列表。
            tools: 工具 schema 列表。
            tool_choice: 工具选择策略；``"none"`` 用于最后一步。

        Returns:
            (LLMResponse, 耗时毫秒)。
        """
        start = time.perf_counter()
        if tool_choice is None:
            resp = self.llm.chat(messages, tools=tools)
        else:
            resp = self.llm.chat(messages, tools=tools, tool_choice=tool_choice)
        latency_ms = (time.perf_counter() - start) * 1000
        return resp, latency_ms

    def _execute_and_append(
        self,
        tc: _ToolCall,
        step: int,
    ) -> str | None:
        """执行单个工具调用并把结果追加到 self.messages。

        Args:
            tc: 工具调用。
            step: Agent 循环步数。

        Returns:
            工具结果文本；参数解析失败或执行抛异常时返回 None。
        """
        self._emit(AgentEvent("tool_start", step, tool=tc.name))

        try:
            arguments = json.loads(tc.arguments_json) if tc.arguments_json else {}
        except json.JSONDecodeError as exc:
            result = f"Failed to parse tool arguments: {exc}"
            self.trace.log_tool(
                step=step,
                tool=tc.name,
                arguments={"raw": tc.arguments_json},
                result=result,
                latency_ms=0.0,
                error=str(exc),
            )
            self._emit(AgentEvent("tool_end", step, tool=tc.name, ok=False, detail=str(exc)))
            self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            return None

        if not isinstance(arguments, dict):
            result = f"Tool arguments must be a JSON object, got: {type(arguments).__name__}"
            self.trace.log_tool(
                step=step,
                tool=tc.name,
                arguments={"raw": tc.arguments_json},
                result=result,
                latency_ms=0.0,
                error="invalid arguments type",
            )
            self._emit(AgentEvent("tool_end", step, tool=tc.name, ok=False, detail="invalid arguments type"))
            self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            return None

        start = time.perf_counter()
        try:
            if tc.name == "run_shell":
                result = self._execute_shell(arguments, step)
            else:
                result = registry.execute(tc.name, arguments)
            error = None
        except Exception as exc:
            result = f"Tool execution failed: {exc}"
            error = str(exc)
        latency_ms = (time.perf_counter() - start) * 1000

        self.trace.log_tool(
            step=step,
            tool=tc.name,
            arguments=arguments,
            result=result,
            latency_ms=latency_ms,
            error=error,
        )
        self._emit(
            AgentEvent(
                "tool_end",
                step,
                tool=tc.name,
                duration_ms=latency_ms,
                ok=error is None,
                detail=error or "",
            )
        )

        self.state.update(tc.name, arguments, result, step)

        self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        return None if error is not None else result

    def _execute_shell(self, arguments: dict[str, Any], step: int) -> str:
        """按权限模式执行 run_shell。

        strict 保持原白名单校验；confirm 在命令不合规时询问用户；
        open 跳过白名单直接执行。三种模式都保留 cwd 边界、超时与输出上限。
        """
        if self.permission_mode == "strict":
            return registry.execute("run_shell", arguments)

        command = arguments.get("command")
        if not isinstance(command, str):
            return registry.execute("run_shell", arguments)

        cwd = arguments.get("cwd")
        cwd_value = cwd if isinstance(cwd, str) else "."
        limits = _shell_limits(arguments)

        try:
            shell.check_command(command, cwd_value)
            return registry.execute("run_shell", arguments)
        except ValueError as exc:
            if self.permission_mode == "open":
                self.trace.log_permission(step, command, "allowed", "open")
                return shell.run_unlisted(command, cwd_value, **limits)

            if self.confirm_command is None:
                self.trace.log_permission(step, command, "denied", "confirm")
                raise ValueError(
                    "Command requires confirmation, but no interactive user is "
                    f"available: {exc}"
                ) from exc

            try:
                approved = bool(self.confirm_command(command))
            except (EOFError, KeyboardInterrupt):
                approved = False
            self.trace.log_permission(
                step, command, "allowed" if approved else "denied", "confirm"
            )
            if not approved:
                raise ValueError(f"Command denied by user: {command}") from exc
            return shell.run_unlisted(command, cwd_value, **limits)


def _convert_tool_calls(tool_calls: list[Any]) -> list[_ToolCall]:
    """把 LLM 返回的 tool_calls 转成内部表示。

    兼容 LiteLLM 对象和 OpenAI 格式的 dict。

    Args:
        tool_calls: LLMResponse.tool_calls。

    Returns:
        _ToolCall 列表。
    """
    result: list[_ToolCall] = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            tc_id = tc.get("id", "")
            func = tc.get("function", {})
            name = func.get("name", "")
            args_json = func.get("arguments", "{}")
        else:
            tc_id = tc.id
            func = tc.function
            name = func.name
            args_json = func.arguments

        result.append(_ToolCall(id=tc_id, name=name, arguments_json=args_json))

    return result

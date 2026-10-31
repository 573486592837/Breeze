"""Agent Loop 实现。

ReAct 风格的代码仓库 Agent，使用 LLM 的 function calling 能力多步调用工具。
"""

import json
import shutil
import time
from dataclasses import dataclass
from typing import Any

from breeze.config import settings
from breeze.context import ContextManager
from breeze.llm import LLMClient, LLMResponse
from breeze.state import SessionState, TurnLedger
from breeze.tools import registry
from breeze.trace import RunTrace

# Deprecated: use settings.agent_max_steps instead.
# Kept for backward compatibility with code that imports this constant.
DEFAULT_MAX_STEPS = 10
DEFAULT_MAX_EDIT_ATTEMPTS = 5

_EXTERNAL_TOOLS = ("git", "rg", "node")


def _environment_note() -> str:
    """生成运行环境说明：仓库根与外部命令可用性。

    模型常在缺少外部命令时反复试错（例如机器没装 ripgrep 却用 run_shell 调
    rg，再改用被白名单拒绝的 find）。提前告诉它哪些命令可用，可以省掉这些步数。

    Returns:
        多行说明文本，且不含可变的时间戳等内容。
    """
    available = [name for name in _EXTERNAL_TOOLS if shutil.which(name)]
    missing = [name for name in _EXTERNAL_TOOLS if name not in available]
    lines = [f"仓库根目录：{settings.repo_root}"]
    if missing:
        lines.append(
            f"外部命令可用：{'、'.join(available) if available else '无'}；"
            f"缺失：{'、'.join(missing)}。缺失的命令不要用 run_shell 尝试："
            "代码搜索用 grep 工具（纯 Python 回退），浏览文件用 list_files。"
        )
    else:
        lines.append(f"外部命令可用：{'、'.join(available)}。")
    return "运行环境：\n" + "\n".join(f"- {line}" for line in lines)


def default_system_prompt() -> str:
    """默认系统提示，附带当前运行环境说明。"""
    return f"{DEFAULT_SYSTEM_PROMPT}\n\n{_environment_note()}"


DEFAULT_SYSTEM_PROMPT = """你是 Breeze，一个代码仓库 Agent。
你可以使用工具查看文件、搜索代码、运行命令、修改代码。

工作规则：
1. 回答必须基于工具观察，不要编造文件内容。
2. 定位代码遵循固定流程：先 grep 搜索符号名（如 "def <符号名>"、常量名、字符串），
   得到 文件:行号 后用 read_file 的 start_line/end_line 精确读取目标范围。
   任务已给出符号名时，禁止通读整个大文件，也禁止反复小范围试探。
3. 互不依赖的工具调用应在同一轮一次性发起（例如同时 grep 定位多个符号、
   并行读取多个已定位的文件）；只有后一个调用需要前一个调用的结果时才分轮。
4. 在调用工具前，先判断是否已有足够信息回答用户问题。
5. 一旦能回答问题，立即停止调用工具并给出答案。
6. 如果只需读取一个文件就能回答，就不要读取更多文件。
7. 遇到工具错误时，简要说明原因，不要反复重试同一工具。
8. 最终答案应简洁、直接，引用关键文件路径和函数名。

编辑代码时：
1. 修改前用 read_file 确认目标位置和上下文。
2. 修改已存在的文件用 apply_patch 提交，old 必须唯一匹配文件中的原文。
   创建新文件用 create_file。
3. 需要确认整体改动时用 git_diff，例如多次修改后或回滚前。单次修改可直接继续。
4. 如果需要运行测试，优先使用 run_tests（仅支持 Python 项目）。
   需要传递特殊 pytest 参数时，改用 run_shell 的 'python -m pytest'。
5. 如果修改有误，用 git_restore 回滚单个文件后重试。
6. 不要修改 .git、traces、reports 目录下的内容。
7. 不要修改测试文件来让测试通过，只修改被测代码。
8. 单次任务中的 apply_patch 调用次数有限（默认 5 次）。
   超过后会被拒绝，此时应报告失败或回滚。
9. 修复类任务在最后一次编辑后，必须重新运行此前失败的测试确认通过。
   最终答案只能陈述已执行的操作与观察，不得把未执行的编辑写成已完成。
10. 任务包含互斥的条件分支时，先判断命中哪个分支，只执行该分支的工作。
11. 要求修改代码的任务，必须在本次任务内用 apply_patch 或 create_file 实际完成
   全部编辑并运行验证，不得以方案、计划或建议结尾。确实无法完成时，
   明确说明哪些编辑未执行以及原因。
12. apply_patch 的 old 必须逐字来自最近一次 read_file 的输出。若目标区域已不在
   上下文（历史被压缩为摘要）或对原文不确定，先重新读取该区域再编辑，
   禁止凭记忆编写 old。
"""


@dataclass
class _ToolCall:
    """内部工具调用表示。"""

    id: str
    name: str
    arguments_json: str


class Agent:
    """ReAct 风格的代码仓库 Agent。

    每步调用 LLM，若有工具调用则执行并追加结果，否则返回最终答案。
    """

    def __init__(
        self,
        llm: LLMClient,
        trace: RunTrace,
        max_steps: int | None = None,
        max_edit_attempts: int | None = None,
        system_prompt: str | None = None,
        context: ContextManager | None = None,
    ) -> None:
        """初始化 Agent。

        Args:
            llm: LLM 客户端。
            trace: 运行追踪记录器。
            max_steps: 最大循环步数。
            max_edit_attempts: 单次运行允许的 apply_patch 调用上限。
            system_prompt: 系统提示。None 时使用默认提示并附当前运行环境说明。
            context: 上下文管理器。
        """
        self.llm = llm
        self.trace = trace
        self.max_steps = max_steps if max_steps is not None else settings.agent_max_steps
        self.max_edit_attempts = (
            max_edit_attempts if max_edit_attempts is not None else settings.agent_max_edit_attempts
        )
        self.system_prompt = system_prompt if system_prompt is not None else default_system_prompt()
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
        if not self.messages:
            self.messages = [{"role": "system", "content": self.system_prompt}]
        self.messages.append({"role": "user", "content": question})
        turn_start = len(self.messages) - 1

        edit_count = 0

        for step in range(self.max_steps):
            remaining = self.max_steps - step

            if self.max_steps >= 8 and step == self.max_steps // 2 and edit_count == 0:
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

            tools = registry.schemas() if remaining > 1 else []
            view = [self.messages[0], *self.messages[turn_start:]]
            llm_messages = self.context.build(view, state=self.state, archive=self.ledger.render())

            try:
                resp, latency_ms = self._call_llm(llm_messages, tools)
            except Exception as exc:
                self.trace.log_error(step, f"LLM call failed: {exc}")
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
            )

            if not tool_calls:
                return self._finish_turn(question, resp.content or "(empty response)", turn_start)

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
                if tc.name == "apply_patch":
                    edit_count += 1
                    if edit_count > self.max_edit_attempts:
                        error_msg = (
                            f"Error: exceeded max_edit_attempts "
                            f"({self.max_edit_attempts}). "
                            f"Use git_restore to revert changes or report the failure."
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

                self._execute_and_append(tc, step)

        return self._finish_turn(question, f"Reached max steps ({self.max_steps}), task not completed.", turn_start)

    def reset(self) -> None:
        """清空消息日志、回合归档与会话状态，开始新会话。"""
        self.messages = []
        self.ledger = TurnLedger()
        self.state = SessionState()

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

    def _call_llm(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[LLMResponse, float]:
        """调用 LLM 并计时。

        Args:
            messages: 消息列表。
            tools: 工具 schema 列表。

        Returns:
            (LLMResponse, 耗时毫秒)。
        """
        start = time.perf_counter()
        resp = self.llm.chat(messages, tools=tools)
        latency_ms = (time.perf_counter() - start) * 1000
        return resp, latency_ms

    def _execute_and_append(
        self,
        tc: _ToolCall,
        step: int,
    ) -> None:
        """执行单个工具调用并把结果追加到 self.messages。

        Args:
            tc: 工具调用。
            step: Agent 循环步数。
        """
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
            self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            return

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
            self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            return

        start = time.perf_counter()
        try:
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

        self.state.update(tc.name, arguments, result, step)

        self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})


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

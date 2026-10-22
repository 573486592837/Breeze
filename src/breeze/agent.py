"""Agent Loop 实现。

ReAct 风格的代码仓库 Agent，使用 LLM 的 function calling 能力多步调用工具。
"""

import json
import time
from dataclasses import dataclass
from typing import Any

from breeze.config import settings
from breeze.context import ContextManager
from breeze.llm import LLMClient, LLMResponse
from breeze.tools import registry
from breeze.trace import RunTrace

# Deprecated: use settings.agent_max_steps instead.
# Kept for backward compatibility with code that imports this constant.
DEFAULT_MAX_STEPS = 10
DEFAULT_MAX_EDIT_ATTEMPTS = 5

DEFAULT_SYSTEM_PROMPT = """你是 Breeze，一个代码仓库 Agent。
你可以使用工具查看文件、搜索代码、运行命令、修改代码。

工作规则：
1. 回答必须基于工具观察，不要编造文件内容。
2. 在调用工具前，先判断是否已有足够信息回答用户问题。
3. 一旦能回答问题，立即停止调用工具并给出答案。
4. 如果只需读取一个文件就能回答，就不要读取更多文件。
5. 遇到工具错误时，简要说明原因，不要反复重试同一工具。
6. 最终答案应简洁、直接，引用关键文件路径和函数名。

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
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        context: ContextManager | None = None,
    ) -> None:
        """初始化 Agent。

        Args:
            llm: LLM 客户端。
            trace: 运行追踪记录器。
            max_steps: 最大循环步数。
            max_edit_attempts: 单次运行允许的 apply_patch 调用上限。
            system_prompt: 系统提示。
            context: 上下文管理器。
        """
        self.llm = llm
        self.trace = trace
        self.max_steps = max_steps if max_steps is not None else settings.agent_max_steps
        self.max_edit_attempts = (
            max_edit_attempts if max_edit_attempts is not None else settings.agent_max_edit_attempts
        )
        self.system_prompt = system_prompt
        self.context = context or ContextManager()

    def run(self, question: str) -> str:
        """执行 Agent，返回最终答案。

        Args:
            question: 用户问题。

        Returns:
            最终答案字符串。达到最大步数或出错时返回提示文本。
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": question},
        ]

        edit_count = 0

        for step in range(self.max_steps):
            remaining = self.max_steps - step

            if remaining == 2:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "你还有 1 次工具调用机会。如果已有足够信息回答用户问题，"
                            "请立即给出最终答案，不要再调用工具。"
                        ),
                    }
                )
            elif remaining == 1:
                messages.append(
                    {
                        "role": "user",
                        "content": ("这是最后一步。请基于已获取的信息直接给出最终答案，不要再调用工具。"),
                    }
                )

            tools = registry.schemas() if remaining > 1 else []
            llm_messages = self.context.build(messages)

            try:
                resp, latency_ms = self._call_llm(llm_messages, tools)
            except Exception as exc:
                self.trace.log_error(step, f"LLM call failed: {exc}")
                return f"LLM call failed: {exc}"

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
                return resp.content or "(empty response)"

            messages.append(
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
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": error_msg,
                            }
                        )
                        continue

                self._execute_and_append(messages, tc, step)

        return f"Reached max steps ({self.max_steps}), task not completed."

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
        messages: list[dict[str, Any]],
        tc: _ToolCall,
        step: int,
    ) -> None:
        """执行单个工具调用并把结果追加到 messages。

        Args:
            messages: 消息列表，原地追加 tool 消息。
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
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
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
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
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

        messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})


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

"""breeze chat 的运行时命令。

命令统一使用 ``/`` 前缀，与 /new、/help、/exit 同一命名空间。
控制器只负责解释命令与更新运行时状态，渲染仍由 CLI 决定；
配置变更通过 SessionStore 写成 config 事件，--resume 时可恢复。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from breeze.config import (
    CONTEXT_PROFILES,
    CUSTOM_PROFILE,
    PERMISSION_MODES,
    context_profile_values,
    settings,
)
from breeze.context import ContextConfig, ContextManager
from breeze.trace import normalize_trace_name

_ALLOWED_PROFILES = ", ".join([*CONTEXT_PROFILES, CUSTOM_PROFILE])
_SET_KEYS = ("max_steps", "max_edit_failures", "temperature", "permission")
_SET_USAGE = f"用法：/set <{'|'.join(_SET_KEYS)}> <值>"


def _pick(explicit: Any, preset: dict[str, int] | None, key: str, fallback: int) -> int:
    """按“显式参数 > 档位 preset > 配置默认值”取一个预算值。"""
    if explicit is not None:
        return explicit
    if preset is not None:
        return preset[key]
    return fallback


class ChatController:
    """维护 chat 会话的运行时配置并处理设置类命令。

    Attributes:
        llm: 当前 LLM 客户端；切换模型后指向新实例。
        trace_prefix: 后续回合 trace 文件名前缀。
        max_steps: 每回合最大步数。
        max_edit_failures: 每回合连续编辑失败次数上限。
        temperature: 采样温度。
        profile: 压缩档位。
        intra_turn_budget: 单回合内上下文的 Token 预算。
        answer_preview: 归档时保留的答案字符数。
        max_records / max_chars: 归档注入的回合数与字符上限。
    """

    def __init__(
        self,
        *,
        llm: Any,
        llm_factory: Callable[[str, float], Any],
        session: Any | None = None,
        repo_root: str = "",
        max_steps: int | None = None,
        max_edit_failures: int | None = None,
        temperature: float | None = None,
        profile: str | None = None,
        trace_prefix: str = "trace",
        intra_turn_budget: int | None = None,
        answer_preview: int | None = None,
        max_records: int | None = None,
        max_chars: int | None = None,
        permission: str | None = None,
    ) -> None:
        """初始化控制器。

        Args:
            llm: 当前 LLM 客户端。
            llm_factory: 按 (model, temperature) 构造新的 LLM 客户端。
            session: SessionStore；提供时把配置变更写成 config 事件。
            repo_root: 仓库根目录，仅用于 /config 展示。
            max_steps: 每回合最大步数，None 时使用配置值。
            max_edit_failures: 连续编辑失败上限，None 时使用配置值。
            temperature: 采样温度，None 时使用配置值。
            profile: 压缩档位，None 时使用配置值。
            trace_prefix: trace 文件名前缀。
            intra_turn_budget: 回合预算，None 时按档位或配置取默认。
            answer_preview: 归档答案字符上限，None 时按档位或配置取默认。
            max_records: 归档回合数上限，None 时按档位或配置取默认。
            max_chars: 归档字符上限，None 时按档位或配置取默认。
            permission: run_shell 权限模式，None 时使用配置值。
        """
        self.llm = llm
        self.llm_factory = llm_factory
        self.session = session
        self.repo_root = repo_root
        self.max_steps = max_steps if max_steps is not None else settings.agent_max_steps
        self.max_edit_failures = (
            max_edit_failures if max_edit_failures is not None else settings.agent_max_edit_failures
        )
        self.temperature = temperature if temperature is not None else settings.llm_temperature
        self.permission = (permission or settings.shell_permission).lower()
        if self.permission not in PERMISSION_MODES:
            self.permission = settings.shell_permission
        self.profile = (profile or settings.context_profile).lower()

        preset = context_profile_values(self.profile) if profile and self.profile != CUSTOM_PROFILE else None
        self.intra_turn_budget = _pick(
            intra_turn_budget, preset, "intra_turn_budget", settings.context_intra_turn_budget
        )
        self.answer_preview = _pick(answer_preview, preset, "answer_preview", settings.chat_answer_preview)
        self.max_records = _pick(max_records, preset, "max_records", settings.chat_max_records)
        self.max_chars = _pick(max_chars, preset, "max_chars", settings.chat_max_chars)

        self.trace_prefix = normalize_trace_name(trace_prefix)

    def handle(self, line: str, agent: Any | None) -> str | None:
        """解释一条以 / 开头的命令。

        Args:
            line: 用户输入。
            agent: 已创建的 Agent；首个回合前为 None。

        Returns:
            要打印的响应；非命令（不以 / 开头）返回 None。
        """
        if not line.startswith("/"):
            return None

        command, _, argument = line.partition(" ")
        name = command[1:].lower()
        argument = argument.strip()

        if name == "model":
            return self._model(argument, agent)
        if name == "trace":
            return self._trace(argument)
        if name == "profile":
            return self._profile(argument, agent)
        if name == "set":
            return self._set(argument, agent)
        if name == "config":
            return self._config()
        if name == "plan":
            return self._plan(agent)
        return f"未知命令 {command}；输入 /help 查看可用命令。"

    def build_context(self) -> ContextManager:
        """按当前回合预算构造 ContextManager。"""
        return ContextManager(ContextConfig(intra_turn_budget=self.intra_turn_budget))

    def apply_to_agent(self, agent: Any) -> None:
        """把控制器状态应用到刚创建或恢复的 Agent。"""
        agent.llm = self.llm
        agent.max_steps = self.max_steps
        agent.max_edit_failures = self.max_edit_failures
        agent.permission_mode = self.permission
        agent.context = self.build_context()
        agent.ledger.answer_preview = self.answer_preview
        agent.ledger.max_records = self.max_records
        agent.ledger.max_chars = self.max_chars

    def _model(self, argument: str, agent: Any | None) -> str:
        """查看或切换模型。"""
        if not argument:
            return f"当前模型：{self.llm.model}"

        try:
            new_llm = self.llm_factory(argument, self.temperature)
        except ValueError as exc:
            return f"模型切换失败：{exc}"

        previous = self.llm.model
        self.llm = new_llm
        if agent is not None:
            agent.llm = new_llm
        self._record(model=new_llm.model)
        return f"模型已切换：{previous} → {new_llm.model}"

    def _trace(self, argument: str) -> str:
        """查看或设置 trace 前缀。"""
        if not argument:
            return f"当前 trace 前缀：{self.trace_prefix}"

        prefix = normalize_trace_name(argument)
        self.trace_prefix = prefix
        self._record(trace_prefix=prefix)
        return f"trace 前缀已设为：{prefix}（从下一回合起生效）"

    def _profile(self, argument: str, agent: Any | None) -> str:
        """查看或切换压缩档位。"""
        if not argument:
            return (
                f"当前压缩档位：{self.profile} "
                f"(intra_turn_budget={self.intra_turn_budget}, max_records={self.max_records})"
            )

        profile = argument.lower()
        if profile != CUSTOM_PROFILE and profile not in CONTEXT_PROFILES:
            return f"无效档位：{argument}（可选 {_ALLOWED_PROFILES}）"

        self.profile = profile
        if profile != CUSTOM_PROFILE:
            values = context_profile_values(profile)
            self.intra_turn_budget = values["intra_turn_budget"]
            self.answer_preview = values["answer_preview"]
            self.max_records = values["max_records"]
            self.max_chars = values["max_chars"]
            self._apply_profile_to_agent(agent)
        self._record(
            profile=profile,
            intra_turn_budget=self.intra_turn_budget,
            answer_preview=self.answer_preview,
            max_records=self.max_records,
            max_chars=self.max_chars,
        )
        return f"压缩档位已切换：{profile} (intra_turn_budget={self.intra_turn_budget}, max_records={self.max_records})"

    def _set(self, argument: str, agent: Any | None) -> str:
        """调整单个运行时参数。"""
        key, _, raw = argument.partition(" ")
        key = key.strip()
        raw = raw.strip()
        if not key or not raw:
            return _SET_USAGE
        if key not in _SET_KEYS:
            return f"未知参数：{key}；可选 {', '.join(_SET_KEYS)}"

        if key == "permission":
            value = raw.lower()
            if value not in PERMISSION_MODES:
                return f"permission 可选：{', '.join(PERMISSION_MODES)}"
            self.permission = value
            if agent is not None:
                agent.permission_mode = value
            self._record(permission=value)
            return f"permission 已设为：{value}"

        if key == "temperature":
            try:
                value = float(raw)
            except ValueError:
                return f"temperature 需要数字，收到：{raw}"
            if not 0.0 <= value <= 2.0:
                return "temperature 需在 0 到 2 之间"
            self.temperature = value
            self.llm.temperature = value
            self._record(temperature=value)
            return f"temperature 已设为：{value:g}"

        try:
            value = int(raw)
        except ValueError:
            return f"{key} 需要整数，收到：{raw}"

        if key == "max_steps":
            if not 1 <= value <= 50:
                return "max_steps 需在 1 到 50 之间"
            self.max_steps = value
            if agent is not None:
                agent.max_steps = value
            self._record(max_steps=value)
            return f"max_steps 已设为：{value}"

        if not 1 <= value <= 50:
            return "max_edit_failures 需在 1 到 50 之间"
        self.max_edit_failures = value
        if agent is not None:
            agent.max_edit_failures = value
        self._record(max_edit_failures=value)
        return f"max_edit_failures 已设为：{value}"

    def _plan(self, agent: Any | None) -> str:
        """查看 Agent 维护的当前会话计划。"""
        if agent is None:
            return "当前计划：(尚未创建)"
        plan = getattr(getattr(agent, "state", None), "plan", "")
        if not plan:
            return "当前计划：(空)"
        return f"当前计划：\n{plan}"

    def _config(self) -> str:
        """返回当前运行时配置。"""
        session_id = getattr(self.session, "session_id", "") or "-"
        return "\n".join(
            [
                "运行配置：",
                f"  repo: {self.repo_root}",
                f"  model: {self.llm.model}",
                f"  session: {session_id}",
                f"  trace 前缀: {self.trace_prefix}",
                f"  max_steps: {self.max_steps}",
                f"  max_edit_failures: {self.max_edit_failures}",
                f"  permission: {self.permission}",
                f"  temperature: {self.temperature:g}",
                f"  profile: {self.profile} (intra_turn_budget={self.intra_turn_budget})",
            ]
        )

    def _apply_profile_to_agent(self, agent: Any | None) -> None:
        """档位变更后同步 Agent 的上下文与归档预算。"""
        if agent is None:
            return
        agent.context = self.build_context()
        agent.ledger.answer_preview = self.answer_preview
        agent.ledger.max_records = self.max_records
        agent.ledger.max_chars = self.max_chars

    def _record(self, **changes: Any) -> None:
        """把变更写入会话；无 session 时忽略。"""
        if self.session is not None:
            self.session.record_config(**changes)

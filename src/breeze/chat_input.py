"""breeze chat 的终端输入。

prompt_toolkit 启用 bracketed paste：粘贴的多行文本整块留在缓冲区里，
按一次回车才提交，不会被拆成多个回合。stdin 不是终端（管道、测试）
时回退到内置 input。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.shortcuts import CompleteStyle
except ImportError:  # pragma: no cover - 依赖缺失时保留逐行输入
    PromptSession = None
    Completer = None
    Completion = None
    CompleteStyle = None

from breeze.chat_commands import COMMANDS, CommandSpec

if Completer is not None:

    class SlashCommandCompleter(Completer):
        """只在输入以 / 开头且尚未输入参数时补全会话命令。"""

        def __init__(self, commands: tuple[CommandSpec, ...] = COMMANDS) -> None:
            self.commands = commands

        def get_completions(self, document, _complete_event):
            text = document.text_before_cursor
            if not text.startswith("/") or any(char.isspace() for char in text):
                return
            prefix = text[1:].lower()
            for spec in self.commands:
                if spec.name.startswith(prefix):
                    yield Completion(
                        spec.name,
                        start_position=-len(prefix),
                        display=spec.usage,
                        display_meta=spec.description,
                    )


def make_chat_input(
    input_fn: Callable[[str], str] = input,
    stream: Any | None = None,
) -> Callable[[str], str]:
    """返回读取一条提问的函数。

    Args:
        input_fn: 非交互环境使用的逐行读取函数。
        stream: 用于判断是否为终端的 stdin，默认 sys.stdin。

    Returns:
        交互终端下使用 PromptSession（支持多行粘贴）；否则返回 input_fn。
    """
    stream = sys.stdin if stream is None else stream
    if PromptSession is None or not getattr(stream, "isatty", lambda: False)():
        return input_fn

    session = PromptSession(
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        complete_style=CompleteStyle.MULTI_COLUMN,
    )

    def read(prompt: str) -> str:
        return session.prompt(prompt)

    return read

"""breeze chat 终端输入的单元测试。"""

from __future__ import annotations

from typing import Any

import pytest

from breeze import chat_input


class _Stream:
    """仅提供 isatty 的假 stdin。"""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class _FakeSession:
    """记录 prompt 文本并返回多行结果。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def prompt(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "line1\nline2"


def test_non_tty_falls_back_to_input() -> None:
    """管道与测试环境保持逐行 input，不启动 PromptSession。"""
    calls: list[str] = []

    def fallback(prompt: str) -> str:
        calls.append(prompt)
        return "answer"

    read = chat_input.make_chat_input(fallback, stream=_Stream(False))

    assert read("You> ") == "answer"
    assert calls == ["You> "]


def test_missing_prompt_toolkit_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """依赖缺失时退回逐行输入，而不是让 chat 启动失败。"""

    def fallback(prompt: str) -> str:
        return "answer"

    monkeypatch.setattr(chat_input, "PromptSession", None)

    read = chat_input.make_chat_input(fallback, stream=_Stream(True))

    assert read("You> ") == "answer"


def test_tty_uses_prompt_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """终端下通过 PromptSession 读取，多行内容原样返回。"""
    session = _FakeSession()
    monkeypatch.setattr(chat_input, "PromptSession", lambda: session)

    read = chat_input.make_chat_input(stream=_Stream(True))

    assert read("You> ") == "line1\nline2"
    assert session.prompts == ["You> "]


def test_bracketed_paste_arrives_as_one_question() -> None:
    """bracketed paste 中的换行留在缓冲区，回车才整体提交。"""
    pytest.importorskip("prompt_toolkit")
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        session = chat_input.PromptSession(input=pipe, output=DummyOutput())
        pipe.send_text("\x1b[200~line1\r\nline2\x1b[201~\r")
        result: Any = session.prompt("You> ")

    assert result == "line1\nline2"

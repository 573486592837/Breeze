"""ContextManager 的单元测试。"""

from typing import Any

from breeze.context import ContextConfig, ContextManager


def _system(content: str = "sys") -> dict[str, Any]:
    return {"role": "system", "content": content}


def _user(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant(
    content: str | None,
    calls: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """构造 assistant 消息。

    Args:
        content: 助手文本。
        calls: (id, name, arguments_json) 三元组列表。
    """
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
            for cid, name, args in calls
        ],
    }


def _tool(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _make_history(n_rounds: int) -> list[dict[str, Any]]:
    """构造一个有 n_rounds 轮工具调用的历史。"""
    messages: list[dict[str, Any]] = [_system(), _user("question")]
    for i in range(n_rounds):
        messages.append(
            _assistant(
                f"step {i}",
                [(f"c{i}", f"tool_{i}", f'{{"arg": {i}}}')],
            )
        )
        messages.append(_tool(f"c{i}", f"result {i}"))
    return messages


# ─── 不压缩场景 ───


def test_disabled_returns_original() -> None:
    """enable=False 时不做任何压缩。"""
    cm = ContextManager(ContextConfig(enable=False))
    messages = _make_history(10)
    result = cm.build(messages)
    assert result == messages


def test_below_threshold_returns_full() -> None:
    """轮数不超过阈值时返回完整列表。"""
    cm = ContextManager(ContextConfig(max_recent_pairs=3))
    messages = _make_history(2)
    result = cm.build(messages)
    assert len(result) == len(messages)
    assert result == messages


def test_empty_messages() -> None:
    """空列表不应崩溃。"""
    cm = ContextManager()
    assert cm.build([]) == []


def test_only_head() -> None:
    """只有 system 和 user 时原样返回。"""
    cm = ContextManager()
    messages = [_system(), _user("q")]
    assert cm.build(messages) == messages


# ─── 压缩场景 ───


def test_compresses_old_rounds() -> None:
    """轮数超过阈值时压缩旧轮。"""
    cm = ContextManager(ContextConfig(max_recent_pairs=2))
    messages = _make_history(5)
    result = cm.build(messages)

    # 头部保留
    assert result[0] == messages[0]
    assert result[1] == messages[1]

    # 第 2 条应该是压缩摘要
    assert result[2]["role"] == "user"
    assert "Compressed history" in result[2]["content"]
    assert "3 earlier rounds" in result[2]["content"]

    # 后面保留最近 2 轮，共 4 条消息（2 轮 × 2 条）
    assert len(result) == 3 + 4


def test_summary_lists_each_call() -> None:
    """摘要里应逐条列出被压缩轮的工具调用。"""
    cm = ContextManager(ContextConfig(max_recent_pairs=1))
    messages = _make_history(4)
    result = cm.build(messages)

    summary = result[2]["content"]
    assert "tool_0" in summary
    assert "tool_1" in summary
    assert "tool_2" in summary
    assert "tool_3" not in summary  # 最近一轮不压缩


def test_summary_truncates_arguments() -> None:
    """参数预览应被截断。"""
    long_args = '{"path": "' + "x" * 200 + '"}'
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "read_file", long_args)]),
        _tool("c0", "result"),
        _assistant("", [("c1", "read_file", '{"path": "y"}')]),
        _tool("c1", "result"),
        _assistant("", [("c2", "read_file", '{"path": "z"}')]),
        _tool("c2", "result"),
    ]

    cm = ContextManager(ContextConfig(max_recent_pairs=1, arguments_preview=30))
    result = cm.build(messages)

    summary = result[2]["content"]
    assert "..." in summary


def test_summary_mentions_reinvoke() -> None:
    """摘要应提示可以重新调用工具。"""
    cm = ContextManager(ContextConfig(max_recent_pairs=1))
    messages = _make_history(3)
    result = cm.build(messages)
    assert "Re-invoke" in result[2]["content"]


def test_recent_rounds_kept_intact() -> None:
    """最近 K 轮的消息应与原始一致。"""
    cm = ContextManager(ContextConfig(max_recent_pairs=2))
    messages = _make_history(5)
    result = cm.build(messages)

    original_tail = messages[-4:]
    assert result[-4:] == original_tail


# ─── 边界情况 ───


def test_assistant_without_tool_calls_kept() -> None:
    """assistant 消息没有 tool_calls 时也应作为一轮处理。"""
    messages = [
        _system(),
        _user("q"),
        _assistant("thinking", []),
        _assistant("", [("c0", "t", "{}")]),
        _tool("c0", "r"),
        _assistant("", [("c1", "t", "{}")]),
        _tool("c1", "r"),
        _assistant("", [("c2", "t", "{}")]),
        _tool("c2", "r"),
    ]

    cm = ContextManager(ContextConfig(max_recent_pairs=1))
    result = cm.build(messages)
    assert result[0] == messages[0]
    assert result[1] == messages[1]
    assert "Compressed history" in result[2]["content"]


def test_multiple_tool_calls_per_assistant() -> None:
    """一个 assistant 调用多个工具时应作为一轮压缩。"""
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "a", "{}"), ("c1", "b", "{}")]),
        _tool("c0", "r0"),
        _tool("c1", "r1"),
        _assistant("", [("c2", "c", "{}")]),
        _tool("c2", "r2"),
        _assistant("", [("c3", "d", "{}")]),
        _tool("c3", "r3"),
    ]

    cm = ContextManager(ContextConfig(max_recent_pairs=1))
    result = cm.build(messages)
    summary = result[2]["content"]
    assert "step 0: a(" in summary
    assert "step 0: b(" in summary


def test_original_messages_unchanged() -> None:
    """build 不应修改传入的 messages 列表。"""
    cm = ContextManager(ContextConfig(max_recent_pairs=1))
    messages = _make_history(5)
    original_snapshot = [m.copy() for m in messages]

    cm.build(messages)

    assert messages == original_snapshot


def test_build_returns_new_list() -> None:
    """即使不压缩，也应返回新列表，避免调用方误改内部状态。"""
    cm = ContextManager()
    messages = _make_history(1)
    result = cm.build(messages)
    assert result is not messages

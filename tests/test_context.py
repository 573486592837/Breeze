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


# ─── 工具结果压缩 ───


def test_small_tool_result_unchanged() -> None:
    """未超过阈值的工具结果不应被修改。"""
    cm = ContextManager(ContextConfig(tool_result_max_bytes=1000))
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "read_file", "{}")]),
        _tool("c0", "short content"),
    ]
    result = cm.build(messages)
    tool_msgs = [m for m in result if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "short content"
    assert tool_msgs[0] is messages[3]


def test_large_tool_result_compacted() -> None:
    """超过阈值的工具结果应被压缩。"""
    cm = ContextManager(
        ContextConfig(
            max_recent_pairs=5,
            tool_result_max_bytes=200,
            tool_result_head_bytes=80,
            tool_result_tail_bytes=80,
        )
    )
    big = "A" * 1000
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "read_file", "{}")]),
        _tool("c0", big),
    ]
    result = cm.build(messages)
    tool_msgs = [m for m in result if m.get("role") == "tool"]
    content = tool_msgs[0]["content"]

    assert len(content.encode("utf-8")) < len(big)
    assert "bytes omitted" in content
    assert content.startswith("A")
    assert content.endswith("A")


def test_compaction_preserves_head_and_tail() -> None:
    """压缩后应保留指定的头部和尾部。"""
    cm = ContextManager(
        ContextConfig(
            max_recent_pairs=5,
            tool_result_max_bytes=200,
            tool_result_head_bytes=50,
            tool_result_tail_bytes=50,
        )
    )
    big = "HEAD" + "M" * 500 + "TAIL"
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "read_file", "{}")]),
        _tool("c0", big),
    ]
    result = cm.build(messages)
    content = result[-1]["content"]

    assert content.startswith("HEAD")
    assert content.endswith("TAIL")
    assert "M" * 500 not in content


def test_compaction_omitted_byte_count() -> None:
    """省略标记里的字节数应正确。"""
    cm = ContextManager(
        ContextConfig(
            max_recent_pairs=5,
            tool_result_max_bytes=100,
            tool_result_head_bytes=40,
            tool_result_tail_bytes=40,
        )
    )
    big = "x" * 1000
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "t", "{}")]),
        _tool("c0", big),
    ]
    result = cm.build(messages)
    content = result[-1]["content"]

    # 1000 - 40 - 40 = 920
    assert "920 bytes omitted" in content


def test_compaction_does_not_mutate_original() -> None:
    """压缩不应修改原始 messages。"""
    cm = ContextManager(
        ContextConfig(
            max_recent_pairs=5,
            tool_result_max_bytes=50,
            tool_result_head_bytes=20,
            tool_result_tail_bytes=20,
        )
    )
    big = "y" * 500
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "t", "{}")]),
        _tool("c0", big),
    ]
    cm.build(messages)
    assert messages[3]["content"] == big


def test_compaction_disabled() -> None:
    """enable=False 时不应压缩工具结果。"""
    cm = ContextManager(ContextConfig(enable=False))
    big = "z" * 1000
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "t", "{}")]),
        _tool("c0", big),
    ]
    result = cm.build(messages)
    assert result[3]["content"] == big


def test_compaction_only_tool_role() -> None:
    """非 tool 角色的长消息不应被压缩。"""
    cm = ContextManager(ContextConfig(tool_result_max_bytes=50))
    big = "a" * 500
    messages = [
        _system(),
        _user(big),  # 长 user 消息
        _assistant("b" * 500, []),  # 长 assistant 消息
    ]
    result = cm.build(messages)
    assert result[1]["content"] == big
    assert result[2]["content"] == "b" * 500


def test_compaction_utf8_boundary() -> None:
    """多字节 UTF-8 字符不应被从中间截断。"""
    cm = ContextManager(
        ContextConfig(
            max_recent_pairs=5,
            tool_result_max_bytes=100,
            tool_result_head_bytes=50,
            tool_result_tail_bytes=50,
        )
    )
    # 中文：每个字符 3 字节
    big = "中" * 500
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "t", "{}")]),
        _tool("c0", big),
    ]
    result = cm.build(messages)
    content = result[-1]["content"]

    assert "\ufffd" not in content

    parts = content.split("\n\n...")
    assert parts[0].endswith("中")
    tail_part = parts[-1].split("...\n\n", 1)[-1]
    assert tail_part.startswith("中")


def test_compaction_in_recent_rounds() -> None:
    """压缩应在最近轮中同样生效。"""
    cm = ContextManager(
        ContextConfig(
            max_recent_pairs=2,
            tool_result_max_bytes=100,
            tool_result_head_bytes=40,
            tool_result_tail_bytes=40,
        )
    )
    big1 = "1" * 500
    big2 = "2" * 500
    big3 = "3" * 500
    messages = [
        _system(),
        _user("q"),
        _assistant("", [("c0", "t", "{}")]),
        _tool("c0", big1),
        _assistant("", [("c1", "t", "{}")]),
        _tool("c1", big2),
        _assistant("", [("c2", "t", "{}")]),
        _tool("c2", big3),
    ]
    result = cm.build(messages)

    # 找到所有 tool 消息
    tool_contents = [m["content"] for m in result if m.get("role") == "tool"]
    # 最近 2 轮都应该被压缩
    for c in tool_contents:
        assert len(c.encode("utf-8")) < 500


def test_safe_head_boundary() -> None:
    """_safe_head 应回退到字符边界。"""
    from breeze.context import _safe_head

    # "中" 是 3 字节 e4 b8 ad
    encoded = "中".encode()
    assert _safe_head(encoded, 1) == b""
    assert _safe_head(encoded, 2) == b""
    assert _safe_head(encoded, 3) == encoded


def test_safe_tail_boundary() -> None:
    """_safe_tail 应前进到字符边界。"""
    from breeze.context import _safe_tail

    encoded = "中".encode()
    assert _safe_tail(encoded, 1) == b""
    assert _safe_tail(encoded, 2) == b""
    assert _safe_tail(encoded, 3) == encoded

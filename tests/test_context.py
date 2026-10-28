"""ContextManager v2（回合内分级保留）的单元测试。"""

from typing import Any

import pytest

from breeze.context import ContextConfig, ContextManager, _count_text_tokens


@pytest.fixture(autouse=True)
def _force_context_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试假设上下文管理默认开启，隔离外部配置。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "context_enable", True)


def _system(content: str = "sys") -> dict[str, Any]:
    return {"role": "system", "content": content}


def _user(content: str) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant(call_id: str, name: str, args: str, content: str | None = None) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
        ],
    }


def _tool(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _round(call_id: str, name: str, args: str, result: str) -> list[dict[str, Any]]:
    return [_assistant(call_id, name, args), _tool(call_id, result)]


def _code_lines(n: int, prefix: str = "def f") -> str:
    return "\n".join(f"{prefix}{i}(): pass" for i in range(n))


def _state(symbols: dict[str, str] | None = None):
    from breeze.state import SessionState

    st = SessionState(max_entries=8)
    for name, loc in (symbols or {}).items():
        st.symbols[name] = loc
    return st


# ─── 透传场景 ───


def test_disabled_returns_original() -> None:
    """enable=False 时原样透传。"""
    cm = ContextManager(ContextConfig(enable=False))
    messages = [_system(), _user("q"), *_round("c1", "grep", '{"pattern": "x"}', _code_lines(50))]
    result = cm.build(messages, state=_state())
    assert result == messages


def test_head_only_returns_original() -> None:
    """只有 system+user 时原样返回。"""
    cm = ContextManager()
    messages = [_system(), _user("q")]
    assert cm.build(messages) == messages


# ─── 定位类恒降级 ───


def test_pointer_tools_always_demoted() -> None:
    """grep/list_files/repo_map 结果应始终替换为一行指针。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    for tool, args in (("grep", '{"pattern": "def f"}'), ("list_files", '{"path": "."}')):
        messages = [_system(), _user("q"), *_round("c1", tool, args, _code_lines(50))]
        result = cm.build(messages)

        tool_msg = next(m for m in result if m["role"] == "tool")
        assert tool_msg["content"].startswith(f"[{tool} compressed]")
        assert "session state" in tool_msg["content"]


def test_content_tools_kept_within_budget() -> None:
    """预算内的 read_file 原文保留。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    body = _code_lines(100)
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', body)]
    result = cm.build(messages)

    tool_msg = next(m for m in result if m["role"] == "tool")
    assert tool_msg["content"] == body


def test_unknown_tools_kept() -> None:
    """未知工具结果不在降级类别内，原样保留。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system(), _user("q"), *_round("c1", "fake_thing", "{}", "some result")]
    result = cm.build(messages)

    tool_msg = next(m for m in result if m["role"] == "tool")
    assert tool_msg["content"] == "some result"


# ─── 预算溢出分级 ───


def test_overflow_demotes_oldest_read_first() -> None:
    """溢出时最旧 read_file 先降级为指针，最新的保留原文。"""
    big = _code_lines(600)
    cm = ContextManager(ContextConfig(intra_turn_budget=4_500))
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "read_file", '{"path": "a.py"}', big),
        *_round("c2", "read_file", '{"path": "b.py"}', big),
    ]
    result = cm.build(messages)

    contents = [m.get("content", "") for m in result if m["role"] == "tool"]
    assert any(c.startswith("[read_file compressed] a.py") for c in contents)
    assert any(c == big for c in contents)


def test_pointer_after_demotion_carries_range() -> None:
    """read_file 指针应携带路径与行范围，指导定点重读。"""
    big = _code_lines(600)
    cm = ContextManager(ContextConfig(intra_turn_budget=500))
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "read_file", '{"path": "s.py", "start_line": 292, "end_line": 315}', big),
    ]
    result = cm.build(messages)

    contents = [m.get("content", "") for m in result if m["role"] == "tool"]
    pointer = next(c for c in contents if c.startswith("[read_file compressed]"))
    assert "s.py (lines 292-315)" in pointer
    assert "re-read" in pointer


def test_extreme_overflow_demotes_spill_tools_too() -> None:
    """read_file 降完后仍溢出时，run_tests 等可再生结果也降级。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=500))
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(600)),
        *_round("c2", "run_tests", '{"pattern": ""}', _code_lines(600, "test f")),
    ]
    result = cm.build(messages)

    contents = [m.get("content", "") for m in result if m["role"] == "tool"]
    assert all(c.startswith("[") for c in contents)


# ─── 状态块注入 ───


def test_state_block_injected_only_with_demotion() -> None:
    """无降级时不注入状态块；有降级时紧随 head。"""
    state = _state({"target": "a.py:1"})
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    kept = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(10))]
    assert not any("[Session state" in str(m.get("content", "")) for m in cm.build(kept, state=state))

    cm_low = ContextManager(ContextConfig(intra_turn_budget=200))
    spill = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(600))]
    result = cm_low.build(spill, state=state)
    assert result[2]["role"] == "user"
    assert "[Session state" in result[2]["content"]
    assert "target: a.py:1" in result[2]["content"]


def test_no_state_block_without_state() -> None:
    """未提供 state 时不注入状态块。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=200))
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(600))]
    result = cm.build(messages)
    assert not any("[Session state" in str(m.get("content", "")) for m in result)


# ─── 兜底压缩 ───


def test_single_result_fallback_compaction() -> None:
    """单条超 16KB 的工具结果被头尾兜底压缩（预算内也会）。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    huge = _code_lines(2000)
    assert len(huge.encode("utf-8")) > 16_000
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', huge)]
    result = cm.build(messages)

    tool_msg = next(m for m in result if m["role"] == "tool")
    assert "bytes omitted" in tool_msg["content"]


# ─── Token 计量 ───


def test_count_text_tokens_prefers_tiktoken() -> None:
    """tiktoken 可用时给出真实计数；不可用时回退粗估。"""
    import breeze.context as ctx

    tokens = _count_text_tokens("def _check_options(tokens):")
    assert tokens > 0

    if ctx._get_encoding() is not None:
        # 中文 token 密度与 4 字符粗估明显不同
        text = "重构拆分检查函数并验证测试通过"
        assert _count_text_tokens(text) != len(text) // 4


def test_count_tokens_fallback_without_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """编码器不可用时回退为 4 字符估算。"""
    import breeze.context as ctx

    monkeypatch.setattr(ctx, "_encoding", None)
    monkeypatch.setattr(ctx, "_encoding_failed", True)
    assert ctx._count_text_tokens("x" * 40) == 10


def test_overflow_spill_uses_fallback_when_monkeypatched(tmp_path, monkeypatch) -> None:
    """兜底估算路径下分级逻辑同样生效。"""
    import breeze.context as ctx

    monkeypatch.setattr(ctx, "_encoding", None)
    monkeypatch.setattr(ctx, "_encoding_failed", True)

    big = _code_lines(600)
    cm = ContextManager(ContextConfig(intra_turn_budget=500))
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', big)]
    result = cm.build(messages)

    tool_msg = next(m for m in result if m["role"] == "tool")
    assert tool_msg["content"].startswith("[read_file compressed]")


# ─── 回合归档注入 ───


def test_archive_injected_between_system_and_question() -> None:
    """归档文本作为一条 user 消息注入在 system 之后、当前问题之前。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system(), _user("current question")]
    result = cm.build(messages, state=_state(), archive="[Completed turns — 1 most recent of 1]\nTurn 1 Q: old")

    assert [m["role"] for m in result] == ["system", "user", "user"]
    assert "Completed turns" in result[1]["content"]
    assert result[2]["content"] == "current question"


def test_state_block_injected_when_archive_present() -> None:
    """有归档时即使未发生降级也注入状态块，让模型看到跨回合索引。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(5))]
    result = cm.build(messages, state=_state({"target": "a.py:1"}), archive="Turn 1 A: done")

    joined = " ".join(str(m.get("content", "")) for m in result)
    assert "[Session state" in joined


def test_no_archive_keeps_layout() -> None:
    """无归档时不注入额外消息。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(5))]
    result = cm.build(messages, state=_state({"target": "a.py:1"}))

    assert [m["role"] for m in result] == ["system", "user", "assistant", "tool"]


def test_archive_counted_in_budget() -> None:
    """归档占用计入预算：预算不足时回合内的 read_file 应降级为指针。"""
    body = _code_lines(200)
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', body)]

    budget = _count_text_tokens(body) + 300
    archive = "archive note " * 200
    assert _count_text_tokens(archive) > 300

    without = ContextManager(ContextConfig(intra_turn_budget=budget))
    assert [m["content"] for m in without.build(messages) if m["role"] == "tool"] == [body]

    with_archive = ContextManager(ContextConfig(intra_turn_budget=budget))
    result = with_archive.build(messages, archive=archive)
    tool_msg = next(m for m in result if m["role"] == "tool")
    assert tool_msg["content"].startswith("[read_file compressed]")

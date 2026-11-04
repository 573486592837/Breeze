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


def test_cross_turn_defaults_to_archive() -> None:
    """默认跨回合策略是 archive。"""
    assert ContextConfig().cross_turn == "archive"
    assert ContextConfig(cross_turn="append").cross_turn == "append"


def test_cross_turn_invalid_raises() -> None:
    """非法跨回合策略立即报错。"""
    with pytest.raises(ValueError, match="Invalid cross_turn mode"):
        ContextConfig(cross_turn="both")


def test_head_only_returns_original() -> None:
    """只有 system+user 时原样返回。"""
    cm = ContextManager()
    messages = [_system(), _user("q")]
    assert cm.build(messages) == messages


# ─── 定位类结果 ───


def test_small_pointer_results_kept() -> None:
    """小体积的 grep/list_files 结果原样保留，模型不必重跑同一工具。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    body = ".gitignore\nAgents.md\nREADME.md"
    for tool, args in (("grep", '{"pattern": "def f"}'), ("list_files", '{"path": "."}')):
        messages = [_system(), _user("q"), *_round("c1", tool, args, body)]
        result = cm.build(messages)

        tool_msg = next(m for m in result if m["role"] == "tool")
        assert tool_msg["content"] == body


def test_large_pointer_results_keep_bounded_preview() -> None:
    """大结果保留预览与总行数，不再声称信息在 session state 里。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    body = "\n".join(f"src/f{i}.py:{i}:match line" for i in range(1, 301))
    messages = [_system(), _user("q"), *_round("c1", "grep", '{"pattern": "match"}', body)]
    result = cm.build(messages)

    content = next(m for m in result if m["role"] == "tool")["content"]
    assert content.startswith("[grep compressed]")
    assert "300 lines" in content
    assert "src/f1.py:1:match line" in content
    assert "truncated" in content
    assert "session state" not in content


def test_large_list_files_preview() -> None:
    """大目录列表同样保留头部条目，而不是整块丢弃。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    body = "\n".join(f"src/pkg/module_{i}.py" for i in range(200))
    messages = [_system(), _user("q"), *_round("c1", "list_files", '{"path": "src"}', body)]
    result = cm.build(messages)

    content = next(m for m in result if m["role"] == "tool")["content"]
    assert content.startswith("[list_files compressed]")
    assert "200 lines" in content
    assert "src/pkg/module_0.py" in content
    assert "truncated" in content


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


def test_soft_budget_keeps_read_file() -> None:
    """软预算溢出时 read_file 原文保留，不迫使模型重读。"""
    big = _code_lines(600)
    cm = ContextManager(ContextConfig(intra_turn_budget=4_500, hard_budget=100_000))
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "read_file", '{"path": "a.py"}', big),
        *_round("c2", "read_file", '{"path": "b.py"}', big),
    ]
    result = cm.build(messages)

    contents = [m.get("content", "") for m in result if m["role"] == "tool"]
    assert contents == [big, big]


def test_hard_budget_demotes_largest_read_first() -> None:
    """只有超过硬上限才降级 read_file，并保护最近一次读取。"""
    big = _code_lines(600)
    cm = ContextManager(ContextConfig(intra_turn_budget=4_500, hard_budget=4_500))
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "read_file", '{"path": "a.py"}', big),
        *_round("c2", "read_file", '{"path": "b.py"}', big),
    ]
    result = cm.build(messages)

    contents = [m.get("content", "") for m in result if m["role"] == "tool"]
    assert contents[0].startswith("[read_file compressed] a.py")
    assert contents[1] == big


def test_soft_budget_keeps_prefix_reusable() -> None:
    """软预算内只追加消息，下一次调用的前缀仍可完整复用。"""
    big = _code_lines(600)
    cm = ContextManager(ContextConfig(intra_turn_budget=4_500, hard_budget=100_000))
    base = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', big)]
    _, first = cm.build_with_stats(base)

    messages = [*base, *_round("c2", "read_file", '{"path": "b.py"}', big)]
    result, second = cm.build_with_stats(messages)

    assert [m["content"] for m in result if m["role"] == "tool"] == [big, big]
    assert second.cacheable_prefix_tokens == first.total_tokens


def test_hard_budget_never_below_soft() -> None:
    """硬上限低于软预算时按软预算处理，避免软阶段反而先降级 read_file。"""
    config = ContextConfig(intra_turn_budget=100_000, hard_budget=1)
    assert config.hard_budget == 100_000


def test_pointer_after_demotion_carries_range() -> None:
    """read_file 指针应携带路径与行范围，指导定点重读。"""
    big = _code_lines(600)
    cm = ContextManager(ContextConfig(intra_turn_budget=500, hard_budget=500))
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
    """软预算先降级可再生结果；硬上限再降级 read_file。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=500, hard_budget=500))
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(600)),
        *_round("c2", "run_tests", '{"pattern": ""}', _code_lines(600, "test f")),
    ]
    result = cm.build(messages)

    contents = [m.get("content", "") for m in result if m["role"] == "tool"]
    assert all(c.startswith("[") for c in contents)


# ─── 计划注入 ───


def test_plan_appended_after_content_without_demotion() -> None:
    """计划追加在消息末尾，无需等待降级，也不改变前面历史的缓存前缀。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system("SYS"), _user("q")]

    result = cm.build(messages, plan="1. 读代码\n2. 改代码")

    assert result[0]["content"] == "SYS"
    assert result[1]["content"] == "q"
    assert result[-1]["role"] == "user"
    assert result[-1]["content"].startswith("[Current plan")
    assert "2. 改代码" in result[-1]["content"]


def test_empty_plan_not_injected() -> None:
    """空计划不产生额外消息。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system(), _user("q")]

    assert cm.build(messages, plan="  \n") == messages
    assert cm.build(messages, plan=None) == messages


def test_plan_appended_after_archive_and_question() -> None:
    """注入顺序为 system、回合归档、当前问题、计划。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system("SYS"), _user("q")]

    result = cm.build(messages, archive="[Completed turns — 1 most recent of 1]", plan="1. step")

    contents = [m["content"] for m in result]
    assert contents[0] == "SYS"
    assert contents[1].startswith("[Completed turns")
    assert contents[2] == "q"
    assert contents[3].startswith("[Current plan")


def test_plan_counts_toward_hard_budget() -> None:
    """计划占用预算：软预算内保留 read_file，超过硬上限才降级。"""
    body = _code_lines(120)
    budget = _count_text_tokens(body) + 50
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', body)]
    plan = "step " * 200

    soft_cm = ContextManager(ContextConfig(intra_turn_budget=budget, hard_budget=100_000))
    kept = soft_cm.build(messages, plan=plan)
    assert next(m for m in kept if m["role"] == "tool")["content"] == body

    hard_cm = ContextManager(ContextConfig(intra_turn_budget=budget, hard_budget=budget))
    spilled = hard_cm.build(messages, plan=plan)
    assert next(m for m in spilled if m["role"] == "tool")["content"].startswith("[read_file compressed] a.py")


# ─── 状态块注入 ───


def test_state_block_injected_only_with_demotion() -> None:
    """无降级时不注入状态块；有降级时追加在消息末尾。"""
    state = _state({"target": "a.py:1"})
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    kept = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(10))]
    assert not any("[Session state" in str(m.get("content", "")) for m in cm.build(kept, state=state))

    cm_low = ContextManager(ContextConfig(intra_turn_budget=200, hard_budget=200))
    spill = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(600))]
    result = cm_low.build(spill, state=state)
    assert result[-1]["role"] == "user"
    assert "[Session state" in result[-1]["content"]
    assert "target: a.py:1" in result[-1]["content"]


def test_no_state_block_without_state() -> None:
    """未提供 state 时不注入状态块。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=200, hard_budget=200))
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
    cm = ContextManager(ContextConfig(intra_turn_budget=500, hard_budget=500))
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

    with_archive = ContextManager(ContextConfig(intra_turn_budget=budget, hard_budget=budget))
    result = with_archive.build(messages, archive=archive)
    tool_msg = next(m for m in result if m["role"] == "tool")
    assert tool_msg["content"].startswith("[read_file compressed]")


# ─── 组成与前缀复用统计 ───


def test_stats_breakdown_covers_each_injection_slot() -> None:
    """stats 按 system/plan/archive/user/state/body 分类且总和一致。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', "content")]
    _, stats = cm.build_with_stats(
        messages,
        state=_state({"target": "a.py:1"}),
        archive="Turn 1 A: done",
        plan="1. edit a.py",
    )

    assert stats.system_tokens > 0
    assert stats.plan_tokens > 0
    assert stats.archive_tokens > 0
    assert stats.user_tokens > 0
    assert stats.state_tokens > 0
    assert stats.body_tokens > 0
    assert stats.total_tokens == sum(stats.breakdown().values())
    assert stats.cacheable_prefix_tokens == 0


def test_stats_prefix_reuse_for_append_only_turn() -> None:
    """回合内追加工具结果时，上一次调用的全部消息都可复用。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    base = [_system(), _user("q")]
    _, first = cm.build_with_stats(base)

    messages = [*base, *_round("c1", "read_file", '{"path": "a.py"}', "content")]
    _, second = cm.build_with_stats(messages)

    assert second.cacheable_prefix_tokens == first.total_tokens
    assert second.total_tokens > first.total_tokens


def test_stats_prefix_reuse_keeps_content_when_plan_changes() -> None:
    """计划在末尾时，它变化不会再切断 system、user 等前面消息。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    base = [_system(), _user("q")]
    _, first = cm.build_with_stats(base, plan="1. step A")
    _, second = cm.build_with_stats(base, plan="1. step B")

    assert second.cacheable_prefix_tokens == first.total_tokens - first.plan_tokens
    assert second.cacheable_prefix_tokens > first.system_tokens


def test_stats_prefix_reuse_keeps_body_when_state_changes() -> None:
    """state 块在末尾时，它变化只让自身不可复用，前面的 body 仍可复用。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    state = _state({"target": "a.py:1"})
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', "content")]

    _, first = cm.build_with_stats(messages, state=state, archive="Turn 1 A: done")
    state.symbols["other"] = "b.py:2"
    _, second = cm.build_with_stats(messages, state=state, archive="Turn 1 A: done")

    assert second.cacheable_prefix_tokens == first.total_tokens - first.state_tokens
    assert second.cacheable_prefix_tokens > first.system_tokens


def test_stats_reset_clears_prefix_baseline() -> None:
    """reset 后不再把上一次会话的消息算作可复用前缀。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    base = [_system(), _user("q")]
    cm.build_with_stats(base)
    cm.reset()

    _, stats = cm.build_with_stats(base)

    assert stats.cacheable_prefix_tokens == 0

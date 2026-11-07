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


def test_head_only_keeps_original_and_adds_budget() -> None:
    """只有 system+user 时原消息不变，仅追加预算提示。"""
    cm = ContextManager()
    messages = [_system(), _user("q")]
    result = cm.build(messages)

    assert result[:2] == messages
    assert result[2]["role"] == "system"
    assert "[Context budget" in result[2]["content"]


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
    assert (
        second.cacheable_prefix_tokens
        == first.total_tokens - first.budget_tokens - first.continuation_tokens
    )


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
    plan_msg = next(m for m in result if str(m.get("content", "")).startswith("[Current plan"))
    assert plan_msg["role"] == "system"
    assert "2. 改代码" in plan_msg["content"]
    assert result[-2]["content"].startswith("[Context budget")
    assert result[-1] == {"role": "user", "content": "继续当前任务。"}


def test_empty_plan_not_injected() -> None:
    """空计划不产生额外计划消息。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system(), _user("q")]

    assert cm.build(messages, plan="  \n") == cm.build(messages, plan=None)
    assert not any("[Current plan" in str(m.get("content", "")) for m in cm.build(messages, plan="  \n"))


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
    state_msgs = [m for m in result if "[Session state" in str(m.get("content", ""))]
    assert len(state_msgs) == 1
    assert state_msgs[0]["role"] == "system"
    assert "target: a.py:1" in state_msgs[0]["content"]


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

    assert [m["role"] for m in result] == ["system", "system", "user", "system", "user"]
    assert "Completed turns" in result[1]["content"]
    assert result[2]["content"] == "current question"


def test_state_block_skipped_with_archive_when_no_demotion() -> None:
    """有归档但本回合未降级时不注入状态块：原始结果仍在上下文中。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(5))]
    result = cm.build(messages, state=_state({"target": "a.py:1"}), archive="Turn 1 A: done")

    joined = " ".join(str(m.get("content", "")) for m in result)
    assert "Turn 1 A: done" in joined
    assert "[Session state" not in joined


def test_no_archive_keeps_layout() -> None:
    """无归档时只追加预算提示，不注入状态块。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', _code_lines(5))]
    result = cm.build(messages, state=_state({"target": "a.py:1"}))

    assert [m["role"] for m in result] == ["system", "user", "assistant", "tool", "system", "user"]


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
    # grep 结果超过指针阈值会降级，状态块随之注入。
    messages = [_system(), _user("q"), *_round("c1", "grep", '{"pattern": "x"}', _code_lines(200))]
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

    assert (
        second.cacheable_prefix_tokens
        == first.total_tokens - first.budget_tokens - first.continuation_tokens
    )
    assert second.total_tokens > first.total_tokens


def test_stats_prefix_reuse_keeps_content_when_plan_changes() -> None:
    """计划在末尾时，它变化不会再切断 system、user 等前面消息。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    base = [_system(), _user("q")]
    _, first = cm.build_with_stats(base, plan="1. step A")
    _, second = cm.build_with_stats(base, plan="1. step B")

    assert (
        second.cacheable_prefix_tokens
        == first.total_tokens - first.plan_tokens - first.budget_tokens - first.continuation_tokens
    )
    assert second.cacheable_prefix_tokens > first.system_tokens


def test_stats_prefix_reuse_keeps_body_when_state_changes() -> None:
    """state 块在末尾时，它变化只让自身不可复用，前面的 body 仍可复用。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    state = _state({"target": "a.py:1"})
    # 需要一次降级才会注入状态块。
    messages = [_system(), _user("q"), *_round("c1", "grep", '{"pattern": "x"}', _code_lines(200))]

    _, first = cm.build_with_stats(messages, state=state, archive="Turn 1 A: done")
    state.symbols["other"] = "b.py:2"
    _, second = cm.build_with_stats(messages, state=state, archive="Turn 1 A: done")

    assert (
        second.cacheable_prefix_tokens
        == first.total_tokens
        - first.state_tokens
        - first.budget_tokens
        - first.continuation_tokens
    )
    assert second.cacheable_prefix_tokens > first.system_tokens


def test_stats_reset_clears_prefix_baseline() -> None:
    """reset 后不再把上一次会话的消息算作可复用前缀。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    base = [_system(), _user("q")]
    cm.build_with_stats(base)
    cm.reset()

    _, stats = cm.build_with_stats(base)

    assert stats.cacheable_prefix_tokens == 0


# ─── 降级结果的可读性与关联 ───


def test_parallel_results_carry_batch_header() -> None:
    """同一步并行调用时，每条结果带上序号与工具参数。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [
        _system(),
        _user("q"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "r1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
                {"id": "r2", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}},
            ],
        },
        _tool("r1", "alpha content"),
        _tool("r2", "beta content"),
    ]
    result = cm.build(messages)

    contents = [m["content"] for m in result if m["role"] == "tool"]
    assert contents[0] == "[call 1/2 read_file: a.py]\nalpha content"
    assert contents[1] == "[call 2/2 read_file: b.py]\nbeta content"


def test_compressed_shell_result_keeps_command_and_output() -> None:
    """run_shell 降级后仍保留命令、退出码与有信息量的输出行。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=10, hard_budget=10))
    result_text = (
        "exit_code: 0\n"
        "--- stdout ---\n"
        " src/a.py | 2 +-\n"
        " 1 file changed, 1 insertion(+), 1 deletion(-)\n"
        "--- stderr ---\n"
    )
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "run_shell", '{"command": "git diff --stat"}', result_text),
    ]
    result = cm.build(messages)

    content = next(m["content"] for m in result if m["role"] == "tool")
    assert content.startswith("[run_shell compressed] git diff --stat")
    assert "exit_code: 0" in content
    assert "1 file changed" in content


def test_compressed_shell_results_are_distinguishable() -> None:
    """并行 run_shell 使用不同命令时，指针必须能区分调用。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=10, hard_budget=10))
    messages = [
        _system(),
        _user("q"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "s1",
                    "type": "function",
                    "function": {"name": "run_shell", "arguments": '{"command": "git status"}'},
                },
                {
                    "id": "s2",
                    "type": "function",
                    "function": {"name": "run_shell", "arguments": '{"command": "git diff --stat"}'},
                },
            ],
        },
        _tool("s1", "exit_code: 0\n--- stdout ---\n M a.py\n--- stderr ---\n"),
        _tool("s2", "exit_code: 0\n--- stdout ---\n 1 file changed\n--- stderr ---\n"),
    ]
    result = cm.build(messages)

    contents = [m["content"] for m in result if m["role"] == "tool"]
    assert "git status" in contents[0] and "git diff --stat" not in contents[0]
    assert "git diff --stat" in contents[1] and "git status" not in contents[1]
    assert "exit_code: 0" in contents[0]
    assert "exit_code: 0" in contents[1]


def test_compressed_parallel_shell_keeps_error_body() -> None:
    """并行调用里的 run_shell 错误不能被批次头掩盖后压缩掉。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=10, hard_budget=10))
    error_text = (
        "Tool execution failed: Shell operators are not supported: '&&'. "
        "Run one command per call, e.g. 'git status --short'."
    )
    messages = [
        _system(),
        _user("q"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "s1",
                    "type": "function",
                    "function": {"name": "run_shell", "arguments": '{"command": "git status --short"}'},
                },
                {
                    "id": "s2",
                    "type": "function",
                    "function": {"name": "run_shell", "arguments": '{"command": "git status && git log"}'},
                },
            ],
        },
        _tool("s1", "exit_code: 0\n--- stdout ---\n(no output)\n--- stderr ---\n"),
        _tool("s2", error_text),
    ]
    result = cm.build(messages)

    contents = [m["content"] for m in result if m["role"] == "tool"]
    assert contents[1].startswith("[run_shell error] git status && git log")
    assert "Shell operators are not supported" in contents[1]
    assert "full output dropped" not in contents[1]


def test_compressed_ruff_result_keeps_all_error_codes() -> None:
    """ruff 的多个错误码都要保留，不能只留首尾把 E501 藏掉。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=10, hard_budget=10))
    ruff_text = (
        "exit_code: 1\n"
        "--- stdout ---\n"
        "B905 `zip()` without an explicit `strict=` parameter\n"
        "  --> src/prefix.py:78:18\n"
        "   |\n"
        "78 |     for items in zip(*seqs):\n"
        "E501 Line too long (132 > 120)\n"
        "  --> tests/test_prefix.py:134:121\n"
        "   |\n"
        "Found 2 errors.\n"
        "--- stderr ---\n"
    )
    messages = [
        _system(),
        _user("q"),
        *_round(
            "c1",
            "run_shell",
            '{"command": "python -m ruff check src tests"}',
            ruff_text,
        ),
    ]
    result = cm.build(messages)

    content = next(m["content"] for m in result if m["role"] == "tool")
    assert "exit_code: 1" in content
    assert "B905" in content
    assert "E501" in content
    assert "Found 2 errors" in content


def test_compressed_tests_result_keeps_summary_and_failures() -> None:
    """run_tests 降级后保留 pytest 汇总与失败项。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=10, hard_budget=10))
    result_text = (
        "exit_code=1\n"
        "summary: 6 failed, 23 passed in 2.41s\n"
        "failures:\n"
        "- tests/test_prefix.py::test_x — TypeError\n"
        "status: failed — investigate the failing test, then fix the code.\n"
    )
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "run_tests", '{"pattern": ""}', result_text),
    ]
    result = cm.build(messages)

    content = next(m["content"] for m in result if m["role"] == "tool")
    assert "6 failed, 23 passed" in content
    assert "test_prefix.py::test_x" in content


def test_error_results_are_not_collapsed() -> None:
    """工具错误保留原文，不压成只有 exit_code 的一行。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=10, hard_budget=10))
    error_text = (
        r"Tool execution failed: Executable not allowed: .venv\Scripts\python.exe. "
        "Allowed commands: git, node, python, python3, rg, where. "
        "Use run_tests or 'python -m pytest'."
    )
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "run_shell", '{"command": ".venv\\Scripts\\python.exe -m pytest"}', error_text),
    ]
    result = cm.build(messages)

    content = next(m["content"] for m in result if m["role"] == "tool")
    assert content.startswith("[run_shell error]")
    assert "Executable not allowed" in content
    assert "Use run_tests" in content
    assert "full output dropped" not in content


def test_update_plan_call_kept_without_plan_body() -> None:
    """update_plan 的调用与确认保留，但计划正文只在尾部注入一次。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [
        _system(),
        _user("q"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "p1",
                    "type": "function",
                    "function": {"name": "update_plan", "arguments": '{"content": "1. a"}'},
                },
                {
                    "id": "r1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                },
            ],
        },
        _tool("p1", "Plan updated (3 chars)."),
        _tool("r1", "alpha"),
    ]
    result = cm.build(messages, plan="1. a")

    joined = " ".join(str(m.get("content", "")) for m in result)
    assert "Plan updated" in joined
    assert "alpha" in joined
    assert joined.count("1. a") == 1

    tool_ids = {m.get("tool_call_id") for m in result if m.get("role") == "tool"}
    assert tool_ids == {"p1", "r1"}
    assistant = next(m for m in result if m.get("role") == "assistant")
    plan_call = next(tc for tc in assistant["tool_calls"] if tc["id"] == "p1")
    assert "omitted" in plan_call["function"]["arguments"]
    assert "1. a" not in plan_call["function"]["arguments"]


def test_update_plan_only_message_kept_as_ack() -> None:
    """只含 update_plan 的 assistant 消息保留，模型能看到调用已执行。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))
    messages = [
        _system(),
        _user("q"),
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "p1",
                    "type": "function",
                    "function": {"name": "update_plan", "arguments": '{"content": "1. a"}'},
                }
            ],
        },
        _tool("p1", "Plan updated (3 chars)."),
    ]
    result = cm.build(messages, plan="1. a")

    assert sum(1 for m in result if m.get("role") == "assistant") == 1
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "p1" for m in result)


def test_update_plan_error_result_is_preserved() -> None:
    """计划超长的错误必须原样返回，不能被压缩掉。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=10, hard_budget=10))
    messages = [
        _system(),
        _user("q"),
        *_round("p1", "update_plan", '{"content": "1. a"}', "Error: plan is 9000 chars; keep it under 4000."),
    ]
    result = cm.build(messages, plan="1. a")

    joined = " ".join(str(m.get("content", "")) for m in result)
    assert "Error: plan is 9000 chars" in joined


def test_recent_reasoning_kept_and_older_trimmed() -> None:
    """最近两步思考保留全文，更早的只留摘录，控制提示词膨胀。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000, hard_budget=100_000))
    messages = [_system(), _user("q")]
    for i in range(5):
        assistant = _assistant(f"r{i}", "read_file", '{"path": "a.py"}')
        assistant["reasoning_content"] = f"step {i} " + "x" * 1000
        messages.extend([assistant, _tool(f"r{i}", "content")])

    result = cm.build(messages)
    assistants = [m for m in result if m.get("role") == "assistant"]

    assert assistants[-1]["reasoning_content"].startswith("step 4")
    assert "truncated" not in assistants[-1]["reasoning_content"]
    assert assistants[-2]["reasoning_content"].startswith("step 3")
    assert "truncated" not in assistants[-2]["reasoning_content"]
    assert assistants[0]["reasoning_content"].startswith("step 0")
    assert assistants[0]["reasoning_content"].endswith("[earlier reasoning truncated]")


def test_reasoning_counts_toward_prompt_tokens() -> None:
    """思考过程会实际发给模型，必须计入预算测量。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000, hard_budget=100_000))
    base = [_system(), _user("q"), _assistant("r0", "read_file", '{"path": "a.py"}')]
    plain = cm.build_with_stats([*base, _tool("r0", "content")])[1]
    with_reasoning = [dict(base[2], reasoning_content="思考" * 200)]
    reasoned = cm.build_with_stats([base[0], base[1], *with_reasoning, _tool("r0", "content")])[1]

    assert reasoned.total_tokens > plain.total_tokens


# ─── 跨回合状态注入 ───


def _prior_state():
    from breeze.state import SessionState

    st = SessionState(max_entries=4)
    st.update(
        "read_file",
        {"path": "x.py"},
        "1 | def helper():\n--- (file has 2 lines total) ---",
        2,
    )
    st.update("run_tests", {"pattern": ""}, "exit_code=1\nsummary: 7 passed, 2 failed\n", 3)
    st.update("apply_patch", {"path": "y.py"}, "Patched y.py (1 replacement)", 4)
    return st


def test_include_state_injects_prior_turn_index() -> None:
    """include_state=True 时即使没有压缩也注入上一回合的状态索引。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000, hard_budget=100_000))
    state = _prior_state()

    result, stats = cm.build_with_stats([_system(), _user("继续")], state=state, include_state=True)

    text = "\n".join(str(msg.get("content")) for msg in result)
    assert "[Session state — carried from previous turns]" in text
    assert "x.py" in text
    assert "y.py" in text
    assert stats.state_tokens > 0


def test_state_not_injected_without_turn_start_flag() -> None:
    """普通步骤不重复注入上一回合状态，避免每步增加固定开销。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000, hard_budget=100_000))
    state = _prior_state()

    result = cm.build([_system(), _user("继续")], state=state)

    text = "\n".join(str(msg.get("content")) for msg in result)
    assert "[Session state" not in text


def test_repeated_reads_injected_without_compression() -> None:
    """未触发压缩时也会注入重复读取警示。"""
    from breeze.state import SessionState

    cm = ContextManager(ContextConfig(intra_turn_budget=100_000, hard_budget=100_000))
    state = SessionState()
    state.update("read_file", {"path": "a.py"}, "1 | x", 0)
    state.update("read_file", {"path": "a.py"}, "1 | x", 1)

    result, stats = cm.build_with_stats([_system(), _user("q")], state=state)

    text = "\n".join(str(msg.get("content")) for msg in result)
    assert "[Repeated reads this turn" in text
    assert stats.state_tokens > 0


def test_snapshot_block_injected_and_counted() -> None:
    """工作集快照作为独立 user 消息注入，并计入组成统计。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000, hard_budget=100_000))
    block = "[Working set — file contents]\nvalue = 1"

    result, stats = cm.build_with_stats([_system(), _user("继续")], snapshots=block)

    snapshot_msg = next(m for m in result if m.get("content") == block)
    assert snapshot_msg == {"role": "system", "content": block}
    assert result[-2]["content"].startswith("[Context budget")
    assert result[-1]["role"] == "user"
    assert stats.snapshot_tokens > 0
    assert stats.breakdown()["snapshot"] == stats.snapshot_tokens


def test_snapshot_survives_budget_spill() -> None:
    """快照不可降级：预算紧张时仍保留完整内容。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=1, hard_budget=1))
    block = "[Working set — file contents]\nkeep_me = True"
    messages = [
        _system(),
        _user("q"),
        *_round("c1", "run_shell", '{"command": "git status"}', "exit_code: 0\n" + "x" * 4000),
    ]

    result = cm.build(messages, snapshots=block)

    assert any(msg.get("content") == block for msg in result)


def test_hard_limit_drops_oldest_read_before_newest() -> None:
    """硬上限兜底时先丢旧读取，刚读到的内容不能被更大的旧结果挤掉。"""
    old = _code_lines(40, prefix="old")
    new = _code_lines(120, prefix="new")
    messages = [
        _system(),
        _user("q"),
        *_round("c_old", "read_file", '{"path": "old.py"}', old),
        *_round("c_new", "read_file", '{"path": "new.py"}', new),
    ]
    probe = ContextManager(ContextConfig(intra_turn_budget=1, hard_budget=10**9))
    total = probe._measure(messages[:2], messages[2:], None)
    cm = ContextManager(ContextConfig(intra_turn_budget=1, hard_budget=total - 1))

    result = cm.build(messages)

    by_id = {msg.get("tool_call_id"): msg.get("content") for msg in result if msg.get("role") == "tool"}
    assert by_id["c_old"].startswith("[read_file compressed] old.py")
    assert "new119" in by_id["c_new"]


def test_hard_limit_keeps_latest_read_of_same_file() -> None:
    """同一文件多次读取时，只降级更早的范围，最新一次保持原文。"""
    old = _code_lines(40, prefix="old")
    new = _code_lines(120, prefix="new")
    messages = [
        _system(),
        _user("q"),
        *_round("c_old", "read_file", '{"path": "a.py", "start_line": 1, "end_line": 40}', old),
        *_round("c_new", "read_file", '{"path": "a.py", "start_line": 1, "end_line": 120}', new),
    ]
    probe = ContextManager(ContextConfig(intra_turn_budget=1, hard_budget=10**9))
    total = probe._measure(messages[:2], messages[2:], None)
    cm = ContextManager(ContextConfig(intra_turn_budget=1, hard_budget=total - 1))

    result, stats = cm.build_with_stats(messages)

    by_id = {msg.get("tool_call_id"): msg.get("content") for msg in result if msg.get("role") == "tool"}
    assert by_id["c_old"].startswith("[read_file superseded] a.py (lines 1-40)")
    assert "new119" in by_id["c_new"]
    assert stats.demoted_read_ids == frozenset({"c_old"})


# ─── 元状态角色分离 ───


def test_metadata_blocks_use_system_role() -> None:
    """归档、快照、计划、状态、AGENTS.md 均以 system 注入，只有用户问题保留 user。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000, hard_budget=100_000))
    messages = [_system("SYS"), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', "content")]

    result = cm.build(
        messages,
        state=_state({"target": "a.py:1"}),
        archive="Turn 1 A: done",
        plan="1. a",
        include_state=True,
        snapshots="[Working set]",
        instructions="[项目指引]",
    )

    assert [m["role"] for m in result] == [
        "system",
        "system",
        "system",
        "user",
        "system",
        "assistant",
        "tool",
        "system",
        "system",
        "system",
        "user",
    ]
    user_msgs = [m for m in result if m["role"] == "user"]
    assert [m["content"] for m in user_msgs] == ["q", "继续当前任务。"]
    metadata = "\n".join(m["content"] for m in result if m["role"] == "system")
    for marker in (
        "[项目指引]",
        "Turn 1 A: done",
        "[Working set]",
        "[Current plan",
        "[Session state",
        "[Context budget",
    ):
        assert marker in metadata


def test_instructions_injected_as_separate_system_message() -> None:
    """AGENTS.md 块单独成条 system 消息，不混进主 system prompt。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000, hard_budget=100_000))
    block = "[项目指引 — AGENTS.md]\n总是先跑 tests"

    result, stats = cm.build_with_stats([_system("SYS"), _user("q")], instructions=block)

    assert result[0] == {"role": "system", "content": "SYS"}
    assert result[1] == {"role": "system", "content": block}
    assert stats.instructions_tokens > 0
    assert stats.breakdown()["instructions"] == stats.instructions_tokens


# ─── 显式预算状态 ───


def test_budget_note_reports_remaining_reads_and_eviction_order() -> None:
    """每轮预算提示包含剩余量、可见读取文件与旧读取先丢弃的规则。"""
    body = _code_lines(60)
    messages = [_system(), _user("q"), *_round("c1", "read_file", '{"path": "a.py"}', body)]
    cm = ContextManager(ContextConfig(intra_turn_budget=10_000, hard_budget=10_000))

    result, stats = cm.build_with_stats(messages)

    note = result[-2]
    assert note["role"] == "system"
    assert "[Context budget" in note["content"]
    assert "10000" in note["content"]
    assert "left" in note["content"]
    assert "a.py" in note["content"]
    assert "1 read(s)" in note["content"]
    assert "oldest" in note["content"]
    assert result[-1] == {"role": "user", "content": "继续当前任务。"}
    assert stats.budget_tokens > 0
    assert stats.breakdown()["budget"] == stats.budget_tokens
    assert stats.continuation_tokens > 0
    assert stats.breakdown()["continuation"] == stats.continuation_tokens


def test_budget_note_present_from_first_call() -> None:
    """还没有读取任何文件时也注入预算提示，让模型从第一步就规划。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=10_000, hard_budget=10_000))

    result = cm.build([_system(), _user("q")])

    note = result[-2]
    assert note["role"] == "system"
    assert "10000" in note["content"]
    assert "none yet" in note["content"]
    assert result[-1] == {"role": "user", "content": "继续当前任务。"}


def test_budget_note_lists_only_visible_reads() -> None:
    """被压缩成指针的旧读取不再算作当前可见读取，只提示已压缩数量。"""
    old = _code_lines(40, prefix="old")
    new = _code_lines(120, prefix="new")
    messages = [
        _system(),
        _user("q"),
        *_round("c_old", "read_file", '{"path": "old.py"}', old),
        *_round("c_new", "read_file", '{"path": "new.py"}', new),
    ]
    probe = ContextManager(ContextConfig(intra_turn_budget=1, hard_budget=10**9))
    total = probe._measure(messages[:2], messages[2:], None)
    cm = ContextManager(ContextConfig(intra_turn_budget=1, hard_budget=total - 1))

    result = cm.build(messages)

    note = str(result[-2].get("content", ""))
    assert "new.py" in note
    assert "old.py" not in note
    assert "1 older read(s) already compressed" in note


def test_prompt_never_ends_with_system_message() -> None:
    """末尾是 system 元状态时补一条 user 续跑消息，避免网关直接返回空。"""
    cm = ContextManager(ContextConfig(intra_turn_budget=100_000))

    result = cm.build(
        [
            _system(),
            _user("q"),
            {"role": "assistant", "content": "(empty response)"},
            {"role": "system", "content": "请直接给出最终答案。"},
        ]
    )

    assert result[-1] == {"role": "user", "content": "继续当前任务。"}
    assert result[-2]["role"] == "system"


def test_disabled_context_still_avoids_system_tail() -> None:
    """关闭上下文管理时也不让请求以 system 结尾。"""
    cm = ContextManager(ContextConfig(enable=False))

    result = cm.build([_system(), _user("q"), {"role": "system", "content": "系统提醒"}])

    assert result[-1] == {"role": "user", "content": "继续当前任务。"}

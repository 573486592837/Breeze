"""LLMClient 并行工具调用参数的单元测试。"""

import types
from typing import Any

import pytest

from breeze.llm import LLMClient


def _ok_response(*args: Any, **kwargs: Any) -> types.SimpleNamespace:
    msg = types.SimpleNamespace(content="ok", tool_calls=[])
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=msg)],
        usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


@pytest.fixture
def client() -> LLMClient:
    return LLMClient(model="test/model", api_key="k", api_base="http://x")


def test_parallel_flag_sent_with_tools(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """带 tools 的请求应携带 parallel_tool_calls=True。"""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> types.SimpleNamespace:
        calls.append(kwargs)
        return _ok_response()

    monkeypatch.setattr("breeze.llm.litellm.completion", fake)
    client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])

    assert calls[0]["parallel_tool_calls"] is True


def test_no_parallel_flag_without_tools(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """无 tools 的请求不应携带 parallel_tool_calls。"""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> types.SimpleNamespace:
        calls.append(kwargs)
        return _ok_response()

    monkeypatch.setattr("breeze.llm.litellm.completion", fake)
    client.chat([{"role": "user", "content": "hi"}])

    assert "parallel_tool_calls" not in calls[0]


def test_fallback_retry_without_flag(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 拒绝该参数时去掉参数重试一次。"""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> types.SimpleNamespace:
        calls.append(kwargs)
        if "parallel_tool_calls" in kwargs:
            raise RuntimeError("unexpected keyword: parallel_tool_calls")
        return _ok_response()

    monkeypatch.setattr("breeze.llm.litellm.completion", fake)
    resp = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])

    assert len(calls) == 2
    assert "parallel_tool_calls" not in calls[1]
    assert resp.content == "ok"


def test_disabled_client_never_sends_flag(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式关闭后不发送参数，且异常直接上抛不重试。"""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> types.SimpleNamespace:
        calls.append(kwargs)
        raise RuntimeError("boom")

    client.parallel_tool_calls = False
    monkeypatch.setattr("breeze.llm.litellm.completion", fake)

    with pytest.raises(RuntimeError):
        client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])

    assert len(calls) == 1
    assert "parallel_tool_calls" not in calls[0]


def test_network_error_is_not_treated_as_param_fallback(
    client: LLMClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """断网/超时类异常不应去掉参数再请求一次。"""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> types.SimpleNamespace:
        calls.append(kwargs)
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr("breeze.llm.litellm.completion", fake)

    with pytest.raises(RuntimeError, match="connection reset"):
        client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])

    assert len(calls) == 1


def test_client_passes_timeout_and_retries(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """请求携带读取/连接超时与重试次数。"""
    calls: list[dict[str, Any]] = []
    client.timeout = 600.0
    client.connect_timeout = 15.0
    client.max_retries = 2

    def fake(**kwargs: Any) -> types.SimpleNamespace:
        calls.append(kwargs)
        return _ok_response()

    monkeypatch.setattr("breeze.llm.litellm.completion", fake)
    client.chat([{"role": "user", "content": "hi"}])

    assert calls[0]["num_retries"] == 2
    timeout = calls[0]["timeout"]
    assert getattr(timeout, "connect", None) == 15.0
    assert getattr(timeout, "read", None) == 600.0


def test_response_shape_parsed(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """正常响应映射为 LLMResponse。"""

    monkeypatch.setattr("breeze.llm.litellm.completion", _ok_response)
    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.content == "ok"
    assert resp.total_tokens == 2
    assert resp.tool_calls == []


def test_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥校验在构造时发生：缺 key 立即报错，而不是等到第一次调用。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "api_key_env", "CUSTOM_API_KEY")

    with pytest.raises(ValueError, match="CUSTOM_API_KEY"):
        LLMClient(model="test/model")


def test_client_error_names_configured_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """报错信息指向 [llm] api_key_env 配置的变量名。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "api_key", "")
    monkeypatch.setattr(settings, "api_key_env", "OPENAI_API_KEY")

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        LLMClient(model="test/model")


def test_explicit_api_key_bypasses_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式传 api_key 时不依赖配置里的值。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "api_key", "")

    client = LLMClient(model="test/model", api_key="explicit-key")

    assert client.api_key == "explicit-key"


def _usage_response(usage: types.SimpleNamespace, hidden: dict | None = None) -> types.SimpleNamespace:
    msg = types.SimpleNamespace(content="ok", tool_calls=[])
    resp = types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)
    if hidden is not None:
        resp._hidden_params = hidden
    return resp


def test_extracts_deepseek_cache_tokens(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepSeek 原生字段直接映射到 cache_hit/miss。"""
    usage = types.SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        prompt_cache_hit_tokens=80,
        prompt_cache_miss_tokens=20,
    )
    monkeypatch.setattr("breeze.llm.litellm.completion", lambda **_: _usage_response(usage))

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.cache_hit_tokens == 80
    assert resp.cache_miss_tokens == 20


def test_extracts_openai_cached_tokens(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI 兼容接口的 cached_tokens 视为命中，未命中由总量推导。"""
    usage = types.SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        prompt_tokens_details=types.SimpleNamespace(cached_tokens=60),
    )
    monkeypatch.setattr("breeze.llm.litellm.completion", lambda **_: _usage_response(usage))

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.cache_hit_tokens == 60
    assert resp.cache_miss_tokens == 40


def test_extracts_anthropic_cache_read_tokens(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Anthropic 的 cache_read_input_tokens 也视为命中。"""
    usage = types.SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        cache_read_input_tokens=30,
    )
    monkeypatch.setattr("breeze.llm.litellm.completion", lambda **_: _usage_response(usage))

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.cache_hit_tokens == 30
    assert resp.cache_miss_tokens == 70


def test_cache_fields_none_when_provider_omits_them(
    client: LLMClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider 不返回缓存字段时保持 None，不能当成 0 命中。"""
    usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105)
    monkeypatch.setattr("breeze.llm.litellm.completion", lambda **_: _usage_response(usage))

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.cache_hit_tokens is None
    assert resp.cache_miss_tokens is None


def test_extracts_response_cost(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """LiteLLM 的 response_cost 写入 cost_usd。"""
    usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=5, total_tokens=105)
    resp_obj = _usage_response(usage, hidden={"response_cost": 0.00123})
    monkeypatch.setattr("breeze.llm.litellm.completion", lambda **_: resp_obj)

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.cost_usd == pytest.approx(0.00123)


def test_tool_choice_sent_with_tools(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """传 tool_choice 时应随 tools 一起发出。"""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> types.SimpleNamespace:
        calls.append(kwargs)
        return _ok_response()

    monkeypatch.setattr("breeze.llm.litellm.completion", fake)
    client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], tool_choice="none")

    assert calls[0]["tool_choice"] == "none"
    assert calls[0]["tools"] == [{"type": "function"}]


def test_tool_choice_ignored_without_tools(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 tools 时不发送 tool_choice。"""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> types.SimpleNamespace:
        calls.append(kwargs)
        return _ok_response()

    monkeypatch.setattr("breeze.llm.litellm.completion", fake)
    client.chat([{"role": "user", "content": "hi"}], tool_choice="none")

    assert "tool_choice" not in calls[0]


def test_tool_choice_fallback_retry(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """provider 拒绝 tool_choice 时去掉该参数重试一次。"""
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> types.SimpleNamespace:
        calls.append(kwargs)
        if "tool_choice" in kwargs:
            raise RuntimeError("unexpected keyword: tool_choice")
        return _ok_response()

    monkeypatch.setattr("breeze.llm.litellm.completion", fake)
    resp = client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}], tool_choice="none")

    assert len(calls) == 2
    assert "tool_choice" not in calls[1]
    assert "parallel_tool_calls" not in calls[1]
    assert resp.content == "ok"


def test_extracts_reasoning_content(client: LLMClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """推理模型的 thinking 原文进入 LLMResponse，供后续请求回传。"""
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    msg = types.SimpleNamespace(content=None, tool_calls=[], reasoning_content="先读文件再改")
    resp_obj = types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)
    monkeypatch.setattr("breeze.llm.litellm.completion", lambda **_: resp_obj)

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.reasoning_content == "先读文件再改"


def test_extracts_reasoning_from_provider_fields(
    client: LLMClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """网关把 thinking 放进 provider_specific_fields 时也要取到。"""
    usage = types.SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    msg = types.SimpleNamespace(
        content=None,
        tool_calls=[],
        provider_specific_fields={"reasoning_content": "plan"},
    )
    resp_obj = types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=usage)
    monkeypatch.setattr("breeze.llm.litellm.completion", lambda **_: resp_obj)

    resp = client.chat([{"role": "user", "content": "hi"}])

    assert resp.reasoning_content == "plan"

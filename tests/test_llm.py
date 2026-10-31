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

    with pytest.raises(ValueError, match="SILICONFLOW_API_KEY"):
        LLMClient(model="test/model")


def test_explicit_api_key_bypasses_missing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式传 api_key 时不依赖配置里的值。"""
    from breeze.config import settings

    monkeypatch.setattr(settings, "api_key", "")

    client = LLMClient(model="test/model", api_key="explicit-key")

    assert client.api_key == "explicit-key"

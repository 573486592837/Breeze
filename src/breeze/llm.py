import os
from dataclasses import dataclass, field
from typing import Any

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # noqa: E402

from breeze.config import settings


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[Any] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    cost_usd: float | None = None
    raw: Any = None


def _usage_field(usage: Any, name: str) -> Any:
    """读取 usage 字段，兼容对象与 dict。"""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(name)
    return getattr(usage, name, None)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_cache_tokens(usage: Any, prompt_tokens: int) -> tuple[int | None, int | None]:
    """提取缓存命中/未命中的输入 Token。

    DeepSeek 原生接口用 ``prompt_cache_hit_tokens``，OpenAI 兼容接口
    用 ``prompt_tokens_details.cached_tokens``，Anthropic 用
    ``cache_read_input_tokens``。多数聚合服务不返回这些字段。
    """
    hit = _int_or_none(_usage_field(usage, "prompt_cache_hit_tokens"))
    miss = _int_or_none(_usage_field(usage, "prompt_cache_miss_tokens"))
    if hit is None:
        hit = _int_or_none(_usage_field(usage, "cache_read_input_tokens"))

    details = _usage_field(usage, "prompt_tokens_details")
    cached = _int_or_none(_usage_field(details, "cached_tokens"))
    if cached is not None:
        hit = cached

    if hit is None and miss is not None and prompt_tokens > 0:
        hit = max(prompt_tokens - miss, 0)
    if miss is None and hit is not None and prompt_tokens > 0:
        miss = max(prompt_tokens - hit, 0)
    return hit, miss


def _extract_cost(resp: Any) -> float | None:
    """读取 LiteLLM 为本次调用估算的费用（美元）。"""
    hidden = getattr(resp, "_hidden_params", None)
    value = hidden.get("response_cost") if isinstance(hidden, dict) else None
    if value is None:
        value = getattr(resp, "response_cost", None)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class LLMClient:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float | None = None,
        parallel_tool_calls: bool = True,
    ) -> None:
        """初始化 LLM 客户端。

        Args:
            model: 模型名。None 时从配置读取。
            api_key: API 密钥。None 时从配置读取。
            api_base: API 基础地址。None 时从配置读取。
            temperature: 温度。None 时从配置读取。
            parallel_tool_calls: 请求模型在同一轮返回多个工具调用。

        Raises:
            ValueError: 未配置 ``[llm] api_key_env`` 指定的环境变量。
        """
        self.model = model or settings.model
        self.api_key = api_key or settings.api_key
        self.api_base = api_base or settings.api_base
        self.api_key_env = settings.api_key_env
        self.temperature = temperature if temperature is not None else settings.llm_temperature
        self.parallel_tool_calls = parallel_tool_calls

        if not self.api_key:
            raise ValueError(f"{self.api_key_env} 未设置，请检查 .env 文件或环境变量")

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """发起一次 chat completion 调用。

        Args:
            messages: 消息列表。
            tools: 可选的工具 schema 列表。
            temperature: 温度。None 时使用实例配置。

        Returns:
            LLMResponse。
        """
        temp = temperature if temperature is not None else self.temperature
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "api_key": self.api_key,
            "api_base": self.api_base,
            "temperature": temp,
        }
        if tools:
            kwargs["tools"] = tools

        use_parallel = self.parallel_tool_calls and bool(tools)
        if use_parallel:
            kwargs["parallel_tool_calls"] = True

        try:
            resp = litellm.completion(**kwargs)
        except Exception:
            if not use_parallel:
                raise
            kwargs.pop("parallel_tool_calls")
            resp = litellm.completion(**kwargs)

        choice = resp.choices[0]
        usage = resp.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        cache_hit_tokens, cache_miss_tokens = _extract_cache_tokens(usage, prompt_tokens)

        return LLMResponse(
            content=choice.message.content,
            tool_calls=getattr(choice.message, "tool_calls", None) or [],
            prompt_tokens=prompt_tokens,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            cost_usd=_extract_cost(resp),
            raw=resp,
        )

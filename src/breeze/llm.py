import os
from dataclasses import dataclass, field
from typing import Any

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # noqa: E402

from breeze.config import settings

_PARAM_ERROR_HINTS = (
    "tool_choice",
    "parallel_tool_calls",
    "unexpected keyword",
    "unsupported parameter",
    "unknown parameter",
    "invalid parameter",
)


def _build_timeout(timeout: float, connect_timeout: float) -> Any:
    """构造 httpx 超时对象；不可用时退回整体超时秒数。"""
    try:
        import httpx

        return httpx.Timeout(timeout, connect=connect_timeout)
    except Exception:
        return timeout


def _is_param_error(exc: Exception) -> bool:
    """判断异常是否像“provider 不认识某个请求参数”。

    只有这类错误才值得去掉 tool_choice / parallel_tool_calls 重试；
    断网、超时、鉴权失败等不应触发参数回退，否则会重复请求。
    """
    if isinstance(exc, TypeError):
        return True
    text = str(exc).lower()
    if any(hint in text for hint in _PARAM_ERROR_HINTS):
        return True
    status = getattr(exc, "status_code", None)
    return status == 400 and any(hint in text for hint in _PARAM_ERROR_HINTS)


@dataclass
class LLMResponse:
    content: str | None = None
    reasoning_content: str | None = None
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


def _extract_reasoning(message: Any) -> str | None:
    """提取推理模型的思考过程。

    DeepSeek 原生字段是 ``reasoning_content``，部分网关放在
    ``provider_specific_fields`` 里；拿不到时返回 None。
    """
    value = getattr(message, "reasoning_content", None)
    if value is None:
        fields = getattr(message, "provider_specific_fields", None)
        if isinstance(fields, dict):
            value = fields.get("reasoning_content")
    return value if isinstance(value, str) and value else None


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
        timeout: float | None = None,
        connect_timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        """初始化 LLM 客户端。

        Args:
            model: 模型名。None 时从配置读取。
            api_key: API 密钥。None 时从配置读取。
            api_base: API 基础地址。None 时从配置读取。
            temperature: 温度。None 时从配置读取。
            parallel_tool_calls: 请求模型在同一轮返回多个工具调用。
            timeout: 单次请求读取超时秒数，None 时从配置读取。
            connect_timeout: 连接超时秒数，None 时从配置读取。
            max_retries: 瞬时网络/API 错误重试次数，None 时从配置读取。

        Raises:
            ValueError: 未配置 ``[llm] api_key_env`` 指定的环境变量。
        """
        self.model = model or settings.model
        self.api_key = api_key or settings.api_key
        self.api_base = api_base or settings.api_base
        self.api_key_env = settings.api_key_env
        self.temperature = temperature if temperature is not None else settings.llm_temperature
        self.parallel_tool_calls = parallel_tool_calls
        self.timeout = settings.llm_timeout if timeout is None else timeout
        self.connect_timeout = (
            settings.llm_connect_timeout if connect_timeout is None else connect_timeout
        )
        self.max_retries = settings.llm_max_retries if max_retries is None else max_retries

        if not self.api_key:
            raise ValueError(f"{self.api_key_env} 未设置，请检查 .env 文件或环境变量")

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        """发起一次 chat completion 调用。

        Args:
            messages: 消息列表。
            tools: 可选的工具 schema 列表。
            temperature: 温度。None 时使用实例配置。
            tool_choice: 可选的工具选择策略；最后一步用 ``"none"`` 保留
                schema 以维持缓存前缀，同时禁止模型调用工具。

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
            "timeout": _build_timeout(self.timeout, self.connect_timeout),
            "num_retries": self.max_retries,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice

        use_parallel = self.parallel_tool_calls and bool(tools)
        if use_parallel:
            kwargs["parallel_tool_calls"] = True

        try:
            resp = litellm.completion(**kwargs)
        except Exception as exc:
            if not _is_param_error(exc):
                raise
            dropped = False
            if "tool_choice" in kwargs:
                kwargs.pop("tool_choice")
                dropped = True
            if use_parallel and "parallel_tool_calls" in kwargs:
                kwargs.pop("parallel_tool_calls")
                dropped = True
            if not dropped:
                raise
            resp = litellm.completion(**kwargs)

        choice = resp.choices[0]
        usage = resp.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        cache_hit_tokens, cache_miss_tokens = _extract_cache_tokens(usage, prompt_tokens)

        return LLMResponse(
            content=choice.message.content,
            reasoning_content=_extract_reasoning(choice.message),
            tool_calls=getattr(choice.message, "tool_calls", None) or [],
            prompt_tokens=prompt_tokens,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            cost_usd=_extract_cost(resp),
            raw=resp,
        )

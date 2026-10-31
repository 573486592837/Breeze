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
    raw: Any = None


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
            ValueError: 未配置 SILICONFLOW_API_KEY。
        """
        self.model = model or settings.model
        self.api_key = api_key or settings.api_key
        self.api_base = api_base or settings.api_base
        self.temperature = temperature if temperature is not None else settings.llm_temperature
        self.parallel_tool_calls = parallel_tool_calls

        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未设置，请检查 .env 文件或环境变量")

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

        return LLMResponse(
            content=choice.message.content,
            tool_calls=getattr(choice.message, "tool_calls", None) or [],
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            raw=resp,
        )

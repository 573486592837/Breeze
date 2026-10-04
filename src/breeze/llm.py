from dataclasses import dataclass, field
from typing import Any

import litellm

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
    ) -> None:
        self.model = model or settings.model
        self.api_key = api_key or settings.api_key
        self.api_base = api_base or settings.api_base

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "api_key": self.api_key,
            "api_base": self.api_base,
            "temperature": temperature,
        }

        if tools:
            kwargs["tools"] = tools

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

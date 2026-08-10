"""Base provider interface for all AI model providers."""

from abc import ABC, abstractmethod
from typing import Any


class BaseProvider(ABC):
    """Abstract base for all AI model providers."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._client = None

    @abstractmethod
    async def chat(self, system_prompt: str, messages: list[dict],
                   **kwargs) -> dict:
        """Send a chat completion request.

        Returns: {"content": str, "finish_reason": str, "usage": {...}}
        """
        ...

    @abstractmethod
    async def chat_with_tools(self, system_prompt: str, messages: list[dict],
                              tools: list[dict], **kwargs) -> dict:
        """Send a chat completion with tool calling support.

        Returns: {
            "content": str,
            "tool_calls": [{"id": str, "type": str, "function": {"name": str, "arguments": str}}],
            "finish_reason": str,
            "usage": {...}
        }
        """
        ...

    def get_usage(self, response: dict) -> dict:
        return response.get("usage", {})

    @staticmethod
    def normalize_usage(usage: Any) -> dict:
        """Normalize OpenAI-compatible usage, including prompt-cache hits."""
        if usage is None:
            return {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
            }

        def value(source: Any, name: str, default: Any = 0) -> Any:
            if isinstance(source, dict):
                return source.get(name, default)
            return getattr(source, name, default)

        def count(raw: Any) -> int:
            try:
                return max(0, int(raw or 0))
            except (TypeError, ValueError, OverflowError):
                return 0

        input_tokens = count(
            value(usage, "prompt_tokens", value(usage, "input_tokens", 0))
        )
        output_tokens = count(
            value(
                usage, "completion_tokens",
                value(usage, "output_tokens", 0),
            )
        )
        details = value(
            usage, "prompt_tokens_details",
            value(usage, "input_tokens_details", None),
        )
        cached_input_tokens = max(
            count(value(details, "cached_tokens", 0)),
            count(value(usage, "cached_input_tokens", 0)),
            count(value(usage, "cache_read_input_tokens", 0)),
            count(value(usage, "prompt_cache_hit_tokens", 0)),
        )
        cache_miss_tokens = count(value(usage, "prompt_cache_miss_tokens", 0))
        if input_tokens <= 0 and (cached_input_tokens or cache_miss_tokens):
            input_tokens = cached_input_tokens + cache_miss_tokens
        cached_input_tokens = min(cached_input_tokens, input_tokens)
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
        }

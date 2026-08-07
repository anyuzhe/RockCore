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
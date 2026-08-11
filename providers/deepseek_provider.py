"""DeepSeek provider implementation (Worker role)."""

import json
import logging
from typing import Any

from .base import BaseProvider

logger = logging.getLogger(__name__)


class DeepSeekProvider(BaseProvider):
    """Provider for DeepSeek V4 models used by the Worker role."""

    DEFAULT_MODEL = "deepseek-v4-pro"
    BASE_URL = "https://api.deepseek.com/v1"
    MAX_OUTPUT_TOKENS = 16_384

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.api_key = self.config.get("api_key", "")
        self.model = self.config.get("model", self.DEFAULT_MODEL)
        self.base_url = self.config.get("base_url", self.BASE_URL)
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
            )
        return self._client

    @staticmethod
    def _resolve_tool_choice(tools: list[dict], requested: Any) -> Any:
        """Return a tool choice supported by DeepSeek thinking mode.

        The thinking API accepts automatic tool selection but rejects the
        OpenAI-compatible ``required`` mode. Keep the tools available and let
        the model select one instead of sending a request that will fail with
        HTTP 400.
        """
        if not tools:
            return None
        if requested == "required":
            logger.info(
                "DeepSeek thinking mode does not support tool_choice=required; "
                "using auto"
            )
            return "auto"
        return requested or "auto"

    async def chat(self, system_prompt: str, messages: list[dict],
                   **kwargs) -> dict:
        client = self._get_client()
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        response = await client.chat.completions.create(
            model=kwargs.get("model", self.model),
            messages=full_messages,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 8192),
            response_format=kwargs.get("response_format"),
        )

        choice = response.choices[0]
        return {
            "content": choice.message.content or "",
            "finish_reason": choice.finish_reason or "stop",
            "usage": self.normalize_usage(response.usage),
            "raw": response,
        }

    async def chat_with_tools(self, system_prompt: str, messages: list[dict],
                              tools: list[dict], **kwargs) -> dict:
        client = self._get_client()
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        tool_choice = self._resolve_tool_choice(
            tools, kwargs.get("tool_choice", "auto")
        )

        response = await client.chat.completions.create(
            model=kwargs.get("model", self.model),
            messages=full_messages,
            tools=tools if tools else None,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 16384),
            parallel_tool_calls=kwargs.get("parallel_tool_calls", True),
            tool_choice=tool_choice,
        )

        choice = response.choices[0]
        msg = choice.message
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })

        return {
            "content": msg.content or "",
            "tool_calls": tool_calls,
            "finish_reason": choice.finish_reason or "stop",
            "usage": self.normalize_usage(response.usage),
            "raw": response,
        }

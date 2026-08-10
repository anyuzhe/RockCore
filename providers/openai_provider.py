"""OpenAI / GPT provider implementation."""

import json
import logging
from typing import Any

from .base import BaseProvider

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    """Provider for OpenAI GPT models (GPT-5.6 Terra, etc.)."""

    DEFAULT_MODEL = "gpt-5.6-terra"
    BASE_URL = "https://api.openai.com/v1"

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

    async def chat(self, system_prompt: str, messages: list[dict],
                   **kwargs) -> dict:
        client = self._get_client()
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        response = await client.chat.completions.create(
            model=kwargs.get("model", self.model),
            messages=full_messages,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 4096),
            response_format=kwargs.get("response_format"),
        )

        choice = response.choices[0]
        return {
            "content": choice.message.content or "",
            "finish_reason": choice.finish_reason or "stop",
            "usage": {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            },
            "raw": response,
        }

    async def chat_with_tools(self, system_prompt: str, messages: list[dict],
                              tools: list[dict], **kwargs) -> dict:
        client = self._get_client()
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        response = await client.chat.completions.create(
            model=kwargs.get("model", self.model),
            messages=full_messages,
            tools=tools,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 8192),
            parallel_tool_calls=kwargs.get("parallel_tool_calls", True),
            tool_choice=kwargs.get("tool_choice", "auto") if tools else None,
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
            "usage": {
                "input_tokens": response.usage.prompt_tokens if response.usage else 0,
                "output_tokens": response.usage.completion_tokens if response.usage else 0,
            },
            "raw": response,
        }

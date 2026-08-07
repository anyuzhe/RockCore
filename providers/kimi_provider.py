"""Kimi (Moonshot) provider implementation."""

import json
import logging
from typing import Any

from .base import BaseProvider

logger = logging.getLogger(__name__)


class KimiProvider(BaseProvider):
    """Provider for Kimi models used by the Planner role."""

    DEFAULT_MODEL = "kimi-k2.6"
    BASE_URL = "https://api.moonshot.cn/v1"

    # Per-model temperature constraints
    MODEL_TEMPERATURES = {
        "kimi-k3": 1.0,
        "kimi-k2.6": 1.0,   # API only accepts 1
        "kimi-k2.5": 1.0,
        "moonshot-v1-8k": 0.3,
        "moonshot-v1-32k": 0.3,
        "moonshot-v1-128k": 0.3,
    }

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.api_key = self.config.get("api_key", "")
        self.model = self.config.get("model", self.DEFAULT_MODEL)
        self.base_url = self.config.get("base_url", self.BASE_URL)
        self._client = None

    def _resolve_temperature(self, **kwargs) -> float:
        """Return model-appropriate temperature. Explicit kwargs take priority."""
        if "temperature" in kwargs:
            return kwargs["temperature"]
        return self.MODEL_TEMPERATURES.get(self.model, 1.0)

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
            model=self.model,
            messages=full_messages,
            temperature=self._resolve_temperature(**kwargs),
            max_tokens=kwargs.get("max_tokens", 8192),
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

        # Kimi supports OpenAI-compatible tool calling
        full_messages = [{"role": "system", "content": system_prompt}] + messages

        response = await client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            tools=tools if tools else None,
            temperature=self._resolve_temperature(**kwargs),
            max_tokens=kwargs.get("max_tokens", 8192),
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

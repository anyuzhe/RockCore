"""Kimi model selection tests."""

import asyncio
from types import SimpleNamespace

from orchestrator.agent_config import PROVIDER_MODELS
from providers.kimi_provider import KimiProvider


def test_kimi_k3_is_available_for_project_configuration():
    assert "kimi-k3" in PROVIDER_MODELS["kimi"]


def test_kimi_provider_accepts_k3_configuration():
    provider = KimiProvider({"api_key": "test", "model": "kimi-k3"})

    assert provider.model == "kimi-k3"
    assert provider._resolve_temperature() == 1.0


def test_kimi_forwards_required_tool_choice_only_for_tool_chat():
    class _Completions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            message = SimpleNamespace(content="", tool_calls=[])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=None,
            )

    completions = _Completions()
    provider = KimiProvider({"api_key": "test", "model": "kimi-k3"})
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )

    asyncio.run(provider.chat("system", [{"role": "user", "content": "hi"}]))
    asyncio.run(provider.chat_with_tools(
        "system",
        [{"role": "user", "content": "edit"}],
        [{"type": "function", "function": {"name": "apply_patch"}}],
        tool_choice="required",
    ))

    assert "tool_choice" not in completions.calls[0]
    assert completions.calls[1]["tool_choice"] == "required"

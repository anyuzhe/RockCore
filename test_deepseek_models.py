"""DeepSeek model compatibility tests."""

import asyncio
from types import SimpleNamespace

from orchestrator.agent_config import PROVIDER_MODELS, ProjectAgentConfig
from orchestrator.cost_engine import CostEngine
from providers.deepseek_provider import DeepSeekProvider


def test_deepseek_v4_pro_is_available_for_configuration():
    assert "deepseek-v4-pro" in PROVIDER_MODELS["deepseek"]

    provider = DeepSeekProvider({"api_key": "test", "model": "deepseek-v4-pro"})
    assert provider.model == "deepseek-v4-pro"


def test_deepseek_v4_pro_is_the_default_worker_model():
    assert DeepSeekProvider.DEFAULT_MODEL == "deepseek-v4-pro"
    assert DeepSeekProvider({"api_key": "test"}).model == "deepseek-v4-pro"
    assert ProjectAgentConfig().worker.model == "deepseek-v4-pro"
    assert ProjectAgentConfig.standard_preset().worker.model == "deepseek-v4-pro"
    assert CostEngine.DEFAULT_MODEL_BY_PROVIDER["deepseek"] == "deepseek-v4-pro"
    assert CostEngine.DEFAULT_MODEL_BY_AGENT["worker"] == "deepseek-v4-pro"


def test_deepseek_downgrades_required_tool_choice_to_auto():
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
    provider = DeepSeekProvider({"api_key": "test"})
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )

    asyncio.run(provider.chat_with_tools(
        "system",
        [{"role": "user", "content": "edit"}],
        [{"type": "function", "function": {"name": "apply_patch"}}],
        tool_choice="required",
    ))

    assert completions.calls[0]["tool_choice"] == "auto"


def test_deepseek_preserves_supported_tool_choice():
    assert DeepSeekProvider._resolve_tool_choice([{"type": "function"}], "auto") == "auto"
    assert DeepSeekProvider._resolve_tool_choice([], "required") is None

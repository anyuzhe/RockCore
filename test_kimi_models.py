"""Kimi model selection tests."""

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.agent_config import PROVIDER_MODELS, ProjectAgentConfig
from orchestrator.event_bus import EventBus
from orchestrator.model_router import ModelRouter
from providers.kimi_provider import KimiProvider


def test_kimi_k3_is_available_for_project_configuration():
    assert "kimi-k3" in PROVIDER_MODELS["kimi"]


def test_kimi_k27_is_available_and_uses_k2_temperature_rules():
    assert "kimi-k2.7-code" in PROVIDER_MODELS["kimi"]
    assert "kimi-k2.7" not in PROVIDER_MODELS["kimi"]

    provider = KimiProvider({"api_key": "test", "model": "kimi-k2.7-code"})
    assert provider.model == "kimi-k2.7-code"
    assert provider._resolve_temperature() == 1.0


def test_kimi_provider_migrates_legacy_k27_alias_before_api_call():
    provider = KimiProvider({"api_key": "test", "model": "kimi-k2.7"})

    assert provider.model == "kimi-k2.7-code"


def test_project_config_migrates_legacy_k27_aliases():
    config = ProjectAgentConfig.from_dict({
        "config_version": 6,
        "planner": {
            "provider": "kimi",
            "model": "kimi-k2.7",
        },
        "worker": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "fallback_provider": "kimi",
            "fallback_model": "kimi-k2.7",
        },
    })

    assert config.config_version == 8
    assert config.planner.model == "kimi-k2.7-code"
    assert config.worker.fallback_model == "kimi-k2.7-code"


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


def test_router_downgrades_unavailable_kimi_model_and_caches_result():
    class Provider:
        model = "kimi-k2.7-code"
        authentication_mode = "api"
        MAX_OUTPUT_TOKENS = 8_192

        def __init__(self):
            self.calls = []

        @staticmethod
        def fallback_models(failed_model):
            return [
                model for model in ("kimi-k2.6", "kimi-k2.5")
                if model != failed_model
            ]

        async def chat_with_tools(self, *_args, **kwargs):
            self.calls.append({
                "model": kwargs.get("model"),
                "max_tokens": kwargs.get("max_tokens"),
            })
            if kwargs.get("model") == "kimi-k2.7-code":
                raise RuntimeError(
                    "Error code: 404 - Not found the model kimi-k2.7-code "
                    "or Permission denied (resource_not_found_error)"
                )
            return {
                "content": "ok",
                "tool_calls": [],
                "usage": {},
            }

    async def scenario():
        provider = Provider()
        events = EventBus()
        router = ModelRouter(event_bus=events)
        router.register_provider("kimi", provider)
        router.set_job_id("JOB-KIMI-DOWNGRADE")

        first = await router.chat_with_tools(
            "worker", "system", [], [],
            provider_override="kimi",
            model="kimi-k2.7-code",
            max_tokens=12_288,
            allow_provider_fallback=False,
        )
        second = await router.chat_with_tools(
            "worker", "system", [], [],
            provider_override="kimi",
            model="kimi-k2.7-code",
            max_tokens=12_288,
            allow_provider_fallback=False,
        )

        assert first["content"] == second["content"] == "ok"
        assert [call["model"] for call in provider.calls] == [
            "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.6",
        ]
        assert all(call["max_tokens"] == 8_192 for call in provider.calls)
        fallback_events = events.get_history("task_model_fallback")
        assert len(fallback_events) == 2
        assert fallback_events[0]["data"]["from_model"] == "kimi-k2.7-code"
        assert fallback_events[0]["data"]["to_model"] == "kimi-k2.6"
        success_events = events.get_history("task_model_fallback_succeeded")
        assert len(success_events) == 2
        assert success_events[0]["data"]["to_model"] == "kimi-k2.6"

    asyncio.run(scenario())


def test_router_tries_every_kimi_candidate_before_reporting_unavailable():
    class Provider:
        model = "kimi-k2.7-code"
        authentication_mode = "api"
        MAX_OUTPUT_TOKENS = 8_192

        def __init__(self):
            self.calls = []

        @staticmethod
        def fallback_models(failed_model):
            return [
                model for model in ("kimi-k2.6", "kimi-k2.5")
                if model != failed_model
            ]

        async def chat_with_tools(self, *_args, **kwargs):
            model = kwargs.get("model")
            self.calls.append(model)
            raise RuntimeError(
                f"Error code: 404 - Not found the model {model} "
                "or Permission denied (resource_not_found_error)"
            )

    async def scenario():
        provider = Provider()
        events = EventBus()
        router = ModelRouter(event_bus=events)
        router.register_provider("kimi", provider)

        with pytest.raises(RuntimeError, match="resource_not_found_error"):
            await router.chat_with_tools(
                "worker", "system", [], [], provider_override="kimi",
                model="kimi-k2.7-code", allow_provider_fallback=False,
            )

        assert provider.calls == [
            "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5",
        ]
        assert len(events.get_history("task_model_fallback")) == 2
        assert not events.get_history("task_model_fallback_succeeded")

    asyncio.run(scenario())

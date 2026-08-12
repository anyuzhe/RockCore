"""Regression tests for task-local, single-owner model fallback policy."""

import asyncio
from types import SimpleNamespace

import pytest

from agents.worker import WorkerAgent
from orchestrator.event_bus import EventBus
from orchestrator.model_router import ModelRouter, SAME_PROVIDER_RETRIES


class _StaticBroker:
    policy = None

    def get_tool_definitions(self):
        return []


def _task(task_id="T001", task_type="analysis"):
    return SimpleNamespace(
        task_id=task_id,
        task_type=task_type,
        title="Inspect project",
        description="Return a concrete report.",
        allowed_paths=["**/*"],
        acceptance_command="",
    )


@pytest.mark.parametrize("error", [
    "Connection error",
    "HTTP 503 Service Unavailable",
    "Error code: 500 - internal server error",
    "Provider returned an invalid response object",
])
def test_network_and_5xx_retry_same_provider_without_fallback(error):
    class Primary:
        model = "deepseek-v4-pro"

        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError(error)

    class Alternate:
        model = "kimi-k2.7-code"

        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            return {"content": "unexpected", "tool_calls": [], "usage": {}}

    async def scenario():
        primary = Primary()
        alternate = Alternate()
        events = EventBus()
        router = ModelRouter(
            provider_map={"worker": "deepseek"}, event_bus=events
        )
        router.register_provider("deepseek", primary)
        router.register_provider("kimi", alternate)
        router.set_job_id("JOB-NETWORK")

        with pytest.raises(RuntimeError, match=error.split()[0]):
            await router.chat_with_tools(
                "worker", "system", [], [], task=_task()
            )

        assert primary.calls == SAME_PROVIDER_RETRIES + 1
        assert alternate.calls == 0
        assert not events.get_history("task_provider_fallback")

    asyncio.run(scenario())


def test_balance_error_requires_user_action_without_model_switch():
    class Primary:
        model = "deepseek-v4-pro"
        calls = 0

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("Error code: 402 - Insufficient Balance")

    class Alternate:
        model = "kimi-k2.7-code"
        calls = 0

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            return {"content": "unexpected", "tool_calls": [], "usage": {}}

    async def scenario():
        primary = Primary()
        alternate = Alternate()
        events = EventBus()
        router = ModelRouter(
            provider_map={"worker": "deepseek"}, event_bus=events
        )
        router.register_provider("deepseek", primary)
        router.register_provider("kimi", alternate)

        with pytest.raises(RuntimeError, match="Insufficient Balance"):
            await router.chat_with_tools(
                "worker", "system", [], [], task=_task()
            )

        assert primary.calls == 1
        assert alternate.calls == 0
        assert not events.get_history("task_provider_fallback")

    asyncio.run(scenario())


def test_provider_circuit_is_isolated_between_tasks_in_same_job():
    router = ModelRouter(provider_map={"worker": "deepseek"})
    router.set_job_id("JOB-PARALLEL")

    router._record_provider_failure(
        "deepseek", "Request timed out", "JOB-PARALLEL", "T001"
    )

    assert router._circuit_is_open("deepseek", "JOB-PARALLEL", "T001")
    assert not router._circuit_is_open("deepseek", "JOB-PARALLEL", "T002")
    assert not router._circuit_is_open("deepseek", "JOB-OTHER", "T001")


def test_unavailable_model_cache_is_isolated_between_tasks():
    class Provider:
        model = "kimi-k2.7-code"

        @staticmethod
        def fallback_models(_failed_model):
            return ["kimi-k2.6"]

    router = ModelRouter()
    router.register_provider("kimi", Provider())
    router._mark_model_unavailable(
        "kimi", "kimi-k2.7-code", "404 model not found", "JOB-1", "T001"
    )

    assert router._model_fallback(
        "kimi", "kimi-k2.7-code", "JOB-1", "T001"
    ) == "kimi-k2.6"
    assert router._model_key(
        "kimi", "kimi-k2.7-code", "JOB-1", "T002"
    ) not in router._unavailable_models


def test_worker_disables_router_fallback_so_engine_is_single_owner():
    class Router:
        def __init__(self):
            self.kwargs = None

        async def chat_with_tools(self, *_args, **kwargs):
            self.kwargs = kwargs
            return {
                "content": "Inspection report is complete.",
                "tool_calls": [],
                "usage": {},
            }

    async def scenario():
        router = Router()
        result = await WorkerAgent(
            router, _StaticBroker(), max_turns=2
        ).run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert router.kwargs["allow_provider_fallback"] is False
        assert router.kwargs["allow_model_fallback"] is False

    asyncio.run(scenario())

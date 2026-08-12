"""Providers must let the remote model choose its own output capacity."""

import asyncio
from types import SimpleNamespace

from orchestrator.model_router import ModelRouter
from providers.codex_provider import CodexProvider
from providers.openai_provider import OpenAIProvider


class _ChatCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content="ok", tool_calls=[])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=None,
        )


def test_openai_provider_never_sends_an_output_limit():
    completions = _ChatCompletions()
    provider = OpenAIProvider({"api_key": "test"})
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )

    asyncio.run(provider.chat(
        "system", [{"role": "user", "content": "hi"}], max_tokens=1234
    ))
    asyncio.run(provider.chat_with_tools(
        "system", [{"role": "user", "content": "edit"}], [],
        max_tokens=5678,
    ))

    assert all("max_tokens" not in call for call in completions.calls)


def test_codex_platform_provider_never_sends_an_output_limit(tmp_path):
    class _Responses:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                output_text="ok", status="completed", usage=None
            )

    completions = _ChatCompletions()
    responses = _Responses()
    client = SimpleNamespace(
        responses=responses,
        chat=SimpleNamespace(completions=completions),
    )
    provider = CodexProvider(
        {"api_key": "test", "wire_api": "responses", "model": "gpt-test"},
        auth_path=tmp_path / "auth.json",
        environ={"PATH": ""},
    )
    provider._clients["default"] = client

    asyncio.run(provider.chat(
        "system", [{"role": "user", "content": "hi"}], max_tokens=1234
    ))
    provider.wire_api = "chat_completions"
    asyncio.run(provider.chat(
        "system", [{"role": "user", "content": "hi"}], max_tokens=5678
    ))

    assert "max_output_tokens" not in responses.calls[0]
    assert "max_completion_tokens" not in completions.calls[0]
    assert "max_tokens" not in completions.calls[0]


def test_router_strips_all_output_limit_parameters_before_provider_call():
    class Provider:
        model = "remote-model"
        authentication_mode = "api"

        def __init__(self):
            self.calls = []

        async def chat(self, *_args, **kwargs):
            self.calls.append(kwargs)
            return {"content": "ok", "finish_reason": "stop", "usage": {}}

        async def chat_with_tools(self, *_args, **kwargs):
            self.calls.append(kwargs)
            return {
                "content": "ok", "finish_reason": "stop",
                "tool_calls": [], "usage": {},
            }

    async def scenario():
        provider = Provider()
        router = ModelRouter(provider_map={"planner": "remote"})
        router.register_provider("remote", provider)

        await router.chat(
            "planner", "system", [],
            max_tokens=100, max_output_tokens=200,
            estimated_output_tokens=300,
        )
        await router.chat_with_tools(
            "planner", "system", [], [],
            max_tokens=400, max_output_tokens=500,
            estimated_output_tokens=600,
        )

        for call in provider.calls:
            assert "max_tokens" not in call
            assert "max_output_tokens" not in call
            assert "estimated_output_tokens" not in call

    asyncio.run(scenario())

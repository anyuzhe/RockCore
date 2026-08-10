"""Credential resolution tests for the local Codex provider."""

import asyncio
import json
import sys
from types import SimpleNamespace

from providers.codex_provider import (
    CodexProvider,
    _detect_chatgpt_login,
    _detect_proxy,
    _load_codex_runtime_config,
    _load_codex_token,
    get_codex_auth_status,
)


def test_reads_current_top_level_codex_auth_format(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"OPENAI_API_KEY": "local-token"}))

    token, source = _load_codex_token(auth_path, environ={})

    assert token == "local-token"
    assert source == "auth.json:OPENAI_API_KEY"
    status = get_codex_auth_status(
        auth_path,
        environ={"CODEX_BINARY": sys.executable, "PATH": ""},
        login_status_runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="Not logged in"
        ),
    )
    assert status["authenticated"]
    assert status["authentication_mode"] == "platform_api"
    assert status["platform_api_configured"]


def test_never_uses_nested_chatgpt_access_token_as_platform_key(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"tokens": {"access_token": "oauth-token"}}))

    token, source = _load_codex_token(auth_path, environ={})

    assert token == ""
    assert source == "unavailable"


def test_environment_credentials_take_precedence(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"OPENAI_API_KEY": "file-token"}))

    token, source = _load_codex_token(
        auth_path, environ={"OPENAI_API_KEY": "environment-token"}
    )

    assert token == "environment-token"
    assert source == "environment:OPENAI_API_KEY"


def test_chatgpt_login_is_detected_through_codex_status_without_reading_token():
    def runner(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="Logged in using ChatGPT\n",
            stderr="",
        )

    authenticated, source, binary = _detect_chatgpt_login(
        environ={"CODEX_BINARY": sys.executable, "PATH": ""},
        runner=runner,
    )

    assert authenticated
    assert source == "codex login status: ChatGPT"
    assert binary == sys.executable


def test_provider_uses_codex_exec_for_chatgpt_login(tmp_path):
    def runner(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="Logged in using ChatGPT\n",
            stderr="",
        )

    provider = CodexProvider(
        {},
        auth_path=tmp_path / "auth.json",
        environ={"CODEX_BINARY": sys.executable, "PATH": ""},
        login_status_runner=runner,
    )
    captured = {}

    async def fake_exec(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        output = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "test"}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"result":"pass"}'},
            }),
            json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 12, "output_tokens": 4},
            }),
        ])
        return output, "", 0

    provider._run_codex_exec = fake_exec
    response = asyncio.run(provider.chat(
        "Return JSON.",
        [{"role": "user", "content": "Review this"}],
        agent_type="reviewer",
        project_root=str(tmp_path),
        model="gpt-5.6-sol",
        reasoning_effort="high",
    ))

    assert provider.authentication_mode == "chatgpt_cli"
    assert provider.api_key == ""
    assert response["content"] == '{"result":"pass"}'
    assert response["usage"] == {"input_tokens": 12, "output_tokens": 4}
    assert captured["sandbox_mode"] == "read-only"
    assert captured["cwd"] == str(tmp_path)
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["reasoning_effort"] == "high"
    assert "Review this" in captured["prompt"]


def test_explicit_platform_key_takes_the_api_channel_even_when_chatgpt_is_logged_in():
    provider = CodexProvider(
        {"api_key": "platform-key"},
        environ={"CODEX_BINARY": sys.executable, "PATH": ""},
        login_status_runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Logged in using ChatGPT", stderr=""
        ),
    )

    assert provider.authentication_mode == "platform_api"
    assert provider.api_key == "platform-key"
    assert provider.chatgpt_authenticated


def test_reads_active_codex_provider_and_wire_protocol(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model_provider = "custom"\n'
        'model = "gpt-test"\n'
        '[model_providers.custom]\n'
        'base_url = "https://example.test/v1"\n'
        'wire_api = "responses"\n'
    )

    runtime = _load_codex_runtime_config(config_path)

    assert runtime["provider"] == "custom"
    assert runtime["base_url"] == "https://example.test/v1"
    assert runtime["wire_api"] == "responses"
    assert runtime["model"] == "gpt-test"


def test_environment_proxy_is_detected_without_exposing_credentials():
    proxy, source = _detect_proxy({"HTTPS_PROXY": "http://127.0.0.1:6864"})

    assert proxy == "http://127.0.0.1:6864"
    assert source == "environment:HTTPS_PROXY"

"""Credential resolution tests for the local Codex provider."""

import json

from providers.codex_provider import (
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
    status = get_codex_auth_status(auth_path)
    assert status["authenticated"]


def test_reads_nested_codex_auth_format(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"tokens": {"access_token": "oauth-token"}}))

    token, source = _load_codex_token(auth_path, environ={})

    assert token == "oauth-token"
    assert source == "auth.json:tokens.access_token"


def test_environment_credentials_take_precedence(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"OPENAI_API_KEY": "file-token"}))

    token, source = _load_codex_token(
        auth_path, environ={"OPENAI_API_KEY": "environment-token"}
    )

    assert token == "environment-token"
    assert source == "environment:OPENAI_API_KEY"


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

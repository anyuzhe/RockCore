"""Kimi model selection tests."""

from orchestrator.agent_config import PROVIDER_MODELS
from providers.kimi_provider import KimiProvider


def test_kimi_k3_is_available_for_project_configuration():
    assert "kimi-k3" in PROVIDER_MODELS["kimi"]


def test_kimi_provider_accepts_k3_configuration():
    provider = KimiProvider({"api_key": "test", "model": "kimi-k3"})

    assert provider.model == "kimi-k3"
    assert provider._resolve_temperature() == 1.0

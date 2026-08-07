"""Codex SDK provider — auto-detects local Codex CLI auth for Governor/Reviewer."""

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.request import getproxies

from .base import BaseProvider

logger = logging.getLogger(__name__)

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
AUTH_PATH = CODEX_HOME / "auth.json"
CONFIG_PATH = CODEX_HOME / "config.toml"


def _load_codex_runtime_config(config_path: Path | None = None) -> dict:
    """Read the active model provider from Codex's TOML configuration."""
    path = config_path or CONFIG_PATH
    defaults = {
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "wire_api": "chat_completions",
        "model": "gpt-4o",
        "config_path": str(path),
    }
    if not path.exists():
        return defaults
    try:
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        provider_name = config.get("model_provider", "openai")
        provider = (config.get("model_providers") or {}).get(provider_name, {})
        return {
            "provider": provider_name,
            "base_url": provider.get("base_url", defaults["base_url"]),
            "wire_api": provider.get("wire_api", defaults["wire_api"]),
            "model": config.get("model", defaults["model"]),
            "config_path": str(path),
        }
    except (OSError, ValueError, TypeError) as error:
        logger.warning(f"Failed to read Codex config: {error}")
        return defaults


def _detect_proxy(environ: dict | None = None) -> tuple[str, str]:
    """Resolve environment or macOS system proxy for HTTPX."""
    environment = os.environ if environ is None else environ
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
                 "HTTP_PROXY", "http_proxy"):
        value = environment.get(name, "")
        if value:
            return value, f"environment:{name}"
    if environ is not None:
        return "", "direct"
    proxies = getproxies()
    value = proxies.get("https") or proxies.get("http") or proxies.get("all") or ""
    return (value, "system") if value else ("", "direct")


def _load_codex_token(auth_path: Path | None = None,
                      environ: dict | None = None) -> tuple[str, str]:
    """Resolve Codex credentials without exposing their value."""
    environment = os.environ if environ is None else environ
    for name in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY"):
        token = environment.get(name, "")
        if token:
            return token, f"environment:{name}"

    path = auth_path or AUTH_PATH
    try:
        if path.exists():
            auth = json.loads(path.read_text())
            candidates = (
                (auth.get("OPENAI_API_KEY", ""), "auth.json:OPENAI_API_KEY"),
                (auth.get("OPENAI_ADMIN_KEY", ""), "auth.json:OPENAI_ADMIN_KEY"),
                (auth.get("api_key", ""), "auth.json:api_key"),
                (auth.get("tokens", {}).get("access_token", ""),
                 "auth.json:tokens.access_token"),
            )
            for token, source in candidates:
                if token:
                    return token, source
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read Codex auth: {e}")
    return "", "unavailable"


def get_codex_auth_status(auth_path: Path | None = None) -> dict:
    """Return non-sensitive authentication diagnostics for the UI."""
    token, source = _load_codex_token(auth_path=auth_path)
    path = auth_path or AUTH_PATH
    runtime = _load_codex_runtime_config()
    proxy, proxy_source = _detect_proxy()
    return {
        "authenticated": bool(token),
        "source": source,
        "auth_path": str(path),
        "auth_file_exists": path.exists(),
        "provider": runtime["provider"],
        "base_url": runtime["base_url"],
        "wire_api": runtime["wire_api"],
        "model": runtime["model"],
        "proxy_enabled": bool(proxy),
        "proxy_source": proxy_source,
    }


class CodexProvider(BaseProvider):
    """Provider for OpenAI Codex SDK.

    Auto-detects authentication from the local Codex CLI installation
    (~/.codex/auth.json). No manual API key configuration needed.

    Handles two roles:
    - Governor (constitution) — chat only, read-only sandbox
    - Reviewer (code review) — read-only sandbox
    """

    DEFAULT_MODEL = "gpt-4o"

    def __init__(self, config: dict | None = None):
        super().__init__(config or {})
        runtime = _load_codex_runtime_config()
        configured_key = self.config.get("api_key", "")
        detected_key, detected_source = _load_codex_token()
        self.api_key = configured_key or detected_key
        self.auth_source = "config" if configured_key else detected_source
        self.base_url = self.config.get("base_url") or runtime["base_url"]
        self.wire_api = self.config.get("wire_api") or runtime["wire_api"]
        self.model = self.config.get("model") or runtime["model"]
        self.model_provider = runtime["provider"]
        self.proxy, self.proxy_source = _detect_proxy()
        self.max_retries = int(self.config.get("max_retries", 5))
        self.timeout = float(self.config.get("timeout", 60))
        self._clients: dict[str, Any] = {}
        self._sandboxes: dict[str, Any] = {}

    @property
    def is_authenticated(self) -> bool:
        return bool(self.api_key)

    async def _get_client(self, agent_type: str = "default"):
        if not self.api_key:
            self.api_key, self.auth_source = _load_codex_token()
        if not self.api_key:
            raise RuntimeError(
                f"Codex login credentials were not found in {AUTH_PATH} or the "
                "OPENAI_API_KEY/OPENAI_ADMIN_KEY environment variables"
            )
        if agent_type not in self._clients:
            from openai import AsyncOpenAI
            import httpx

            http_client = httpx.AsyncClient(
                proxy=self.proxy or None,
                timeout=self.timeout,
            )
            self._clients[agent_type] = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                http_client=http_client,
                max_retries=self.max_retries,
            )
        return self._clients[agent_type]

    async def _get_sandbox(self, agent_type: str, cwd: str | None = None):
        """Create per-agent sandbox with appropriate permissions."""
        if agent_type not in self._sandboxes:
            sandbox_mode = self._get_sandbox_mode(agent_type)
            try:
                from codex import Sandbox

                sandbox = Sandbox.create(
                    mode=sandbox_mode,
                    cwd=cwd or ".",
                )
                self._sandboxes[agent_type] = sandbox
            except ImportError:
                logger.warning(
                    "codex sandbox not available — "
                    "falling back to chat-only mode"
                )
                self._sandboxes[agent_type] = None
        return self._sandboxes[agent_type]

    def _get_sandbox_mode(self, agent_type: str) -> str:
        modes = {
            "governor": "read_only",
            "reviewer": "read_only",
            "emergency_coder": "workspace_write",
        }
        return modes.get(agent_type, "read_only")

    def get_allowed_sandbox_modes(self) -> list[str]:
        return ["read_only", "workspace_write", "full_access"]

    async def chat(self, system_prompt: str, messages: list[dict],
                   **kwargs) -> dict:
        agent_type = kwargs.get("agent_type", "default")
        client = await self._get_client(agent_type)

        requested_model = kwargs.get("model")
        model = self.model if not requested_model or requested_model == "codex-sdk" else requested_model
        if self.wire_api == "responses":
            response = await client.responses.create(
                model=model,
                instructions=system_prompt,
                input=messages,
                max_output_tokens=kwargs.get("max_tokens", 4096),
            )
            usage = getattr(response, "usage", None)
            return {
                "content": response.output_text or "",
                "finish_reason": "stop" if response.status == "completed" else response.status,
                "usage": {
                    "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                    "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
                },
                "raw": response,
            }

        full_messages = [{"role": "system", "content": system_prompt}] + messages
        response = await client.chat.completions.create(
            model=model,
            messages=full_messages,
            temperature=kwargs.get("temperature", 0.2),
            max_tokens=kwargs.get("max_tokens", 4096),
        )
        choice = response.choices[0]
        return {
            "content": choice.message.content or "",
            "finish_reason": choice.finish_reason or "stop",
            "usage": {
                "input_tokens": (
                    response.usage.prompt_tokens if response.usage else 0
                ),
                "output_tokens": (
                    response.usage.completion_tokens
                    if response.usage else 0
                ),
            },
            "raw": response,
        }

    async def chat_with_tools(self, system_prompt: str, messages: list[dict],
                              tools: list[dict], **kwargs) -> dict:
        return await self.chat(system_prompt, messages, **kwargs)

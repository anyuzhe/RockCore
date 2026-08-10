"""Codex provider with separate ChatGPT and Platform API auth channels."""

import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.request import getproxies

from app.paths import application_dir, resolve_working_dir
from app.subprocess_utils import (
    decode_process_output,
    no_window_creation_flags,
    run_process,
    utf8_environment,
)
from .base import BaseProvider

logger = logging.getLogger(__name__)

CODEX_HOME = Path(os.path.expandvars(os.fspath(
    os.environ.get("CODEX_HOME", Path.home() / ".codex")
))).expanduser()
AUTH_PATH = CODEX_HOME / "auth.json"
CONFIG_PATH = CODEX_HOME / "config.toml"
REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "max"}


def _load_codex_runtime_config(config_path: Path | None = None) -> dict:
    """Read non-secret model settings from Codex's TOML configuration."""
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
        logger.warning("Failed to read Codex config: %s", error)
        return defaults


def _detect_proxy(environ: dict | None = None) -> tuple[str, str]:
    """Resolve environment or macOS system proxy for HTTPX."""
    environment = os.environ if environ is None else environ
    for name in (
        "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
        "HTTP_PROXY", "http_proxy",
    ):
        value = environment.get(name, "")
        if value:
            return value, f"environment:{name}"
    if environ is not None:
        return "", "direct"
    proxies = getproxies()
    value = proxies.get("https") or proxies.get("http") or proxies.get("all") or ""
    return (value, "system") if value else ("", "direct")


def _load_platform_api_key(auth_path: Path | None = None,
                           environ: dict | None = None) -> tuple[str, str]:
    """Resolve only a Platform API key, never a ChatGPT OAuth access token."""
    environment = os.environ if environ is None else environ
    token = environment.get("OPENAI_API_KEY", "")
    if token:
        return token, "environment:OPENAI_API_KEY"

    path = auth_path or AUTH_PATH
    try:
        if path.exists():
            auth = json.loads(path.read_text(encoding="utf-8"))
            token = auth.get("OPENAI_API_KEY", "")
            if token:
                return token, "auth.json:OPENAI_API_KEY"
    except (json.JSONDecodeError, OSError, TypeError) as error:
        logger.warning("Failed to read Codex auth: %s", error)
    return "", "unavailable"


def _load_codex_token(auth_path: Path | None = None,
                      environ: dict | None = None) -> tuple[str, str]:
    """Backward-compatible alias that now returns Platform API keys only."""
    return _load_platform_api_key(auth_path=auth_path, environ=environ)


def _find_codex_binary(environ: dict | None = None) -> str:
    """Find Codex even when a desktop launch did not inherit the shell PATH."""
    environment = os.environ if environ is None else environ
    configured = environment.get("CODEX_BINARY", "")
    candidates = [
        configured,
        shutil.which("codex", path=environment.get("PATH")),
    ]
    windows_environment = sys.platform == "win32" or any(
        environment.get(name) for name in ("APPDATA", "LOCALAPPDATA")
    )
    if windows_environment:
        appdata = environment.get("APPDATA", "")
        localappdata = environment.get("LOCALAPPDATA", "")
        userprofile = environment.get("USERPROFILE", "")
        candidates.extend([
            str(Path(appdata) / "npm" / "codex.cmd") if appdata else "",
            str(Path(appdata) / "npm" / "codex.exe") if appdata else "",
            str(Path(localappdata) / "Programs" / "ChatGPT" / "resources" / "codex.exe")
            if localappdata else "",
            str(Path(localappdata) / "Microsoft" / "WindowsApps" / "codex.exe")
            if localappdata else "",
            str(Path(userprofile) / ".local" / "bin" / "codex.exe")
            if userprofile else "",
        ])
    else:
        candidates.extend([
            "/Applications/ChatGPT.app/Contents/Resources/codex",
            "/opt/homebrew/bin/codex",
            "/usr/local/bin/codex",
        ])
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if not path.is_file():
            continue
        if sys.platform == "win32" or path.suffix.lower() in {
            ".exe", ".cmd", ".bat", ".ps1",
        } or os.access(path, os.X_OK):
            return str(path)
    return ""


def _prepare_codex_command(binary: str, arguments: list[str],
                           environ: dict | None = None,
                           platform: str | None = None) -> list[str]:
    """Wrap npm ``.cmd``/PowerShell launchers correctly on Windows."""
    platform_name = sys.platform if platform is None else platform
    if platform_name != "win32":
        return [binary, *arguments]
    environment = os.environ if environ is None else environ
    suffix = Path(binary).suffix.lower()
    if suffix in {".cmd", ".bat"}:
        comspec = environment.get("COMSPEC") or "cmd.exe"
        invocation = subprocess.list2cmdline([binary, *arguments])
        return [comspec, "/d", "/s", "/c", invocation]
    if suffix == ".ps1":
        powershell = (
            shutil.which("pwsh", path=environment.get("PATH"))
            or shutil.which("powershell.exe", path=environment.get("PATH"))
            or "powershell.exe"
        )
        return [
            powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", binary, *arguments,
        ]
    return [binary, *arguments]


def _detect_chatgpt_login(
    auth_path: Path | None = None,
    environ: dict | None = None,
    runner: Callable[..., Any] | None = None,
) -> tuple[bool, str, str]:
    """Ask Codex which login mode is active without reading or exposing tokens."""
    environment = os.environ if environ is None else environ
    binary = _find_codex_binary(environ)
    if not binary:
        return False, "codex CLI unavailable", ""

    run = runner or run_process
    try:
        command = _prepare_codex_command(
            binary, ["login", "status"], environ=environment
        )
        result = run(
            command,
            capture_output=True,
            timeout=5,
            creationflags=no_window_creation_flags(),
        )
        output = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".strip()
        normalized = output.lower()
        if getattr(result, "returncode", 1) == 0 and "chatgpt" in normalized:
            return True, "codex login status: ChatGPT", binary
        if getattr(result, "returncode", 1) == 0 and output:
            return False, f"codex login status: {output[:120]}", binary
        return False, "codex login status: not logged in", binary
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Failed to query Codex login status: %s", error)
        return False, "codex login status unavailable", binary


def get_codex_auth_status(
    auth_path: Path | None = None,
    *,
    configured_api_key: str = "",
    environ: dict | None = None,
    login_status_runner: Callable[..., Any] | None = None,
) -> dict:
    """Return non-sensitive, channel-specific authentication diagnostics."""
    platform_key, platform_source = _load_platform_api_key(
        auth_path=auth_path, environ=environ
    )
    if configured_api_key:
        platform_key, platform_source = configured_api_key, "RockCore 设置"
    chatgpt_authenticated, chatgpt_source, binary = _detect_chatgpt_login(
        auth_path=auth_path, environ=environ, runner=login_status_runner
    )
    if platform_key:
        mode = "platform_api"
        source = platform_source
    elif chatgpt_authenticated:
        mode = "chatgpt_cli"
        source = chatgpt_source
    else:
        mode = "unavailable"
        source = "unavailable"

    path = auth_path or AUTH_PATH
    runtime = _load_codex_runtime_config()
    proxy, proxy_source = _detect_proxy(environ)
    return {
        "authenticated": mode != "unavailable",
        "authentication_mode": mode,
        "source": source,
        "transport": "platform_api" if mode == "platform_api" else (
            "codex_exec" if mode == "chatgpt_cli" else "unavailable"
        ),
        "chatgpt_authenticated": chatgpt_authenticated,
        "chatgpt_source": chatgpt_source,
        "codex_binary": binary,
        "platform_api_configured": bool(platform_key),
        "platform_api_source": platform_source,
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
    """Use ChatGPT through ``codex exec`` or an explicit Platform API key."""

    DEFAULT_MODEL = "gpt-4o"

    def __init__(
        self,
        config: dict | None = None,
        *,
        auth_path: Path | None = None,
        environ: dict | None = None,
        login_status_runner: Callable[..., Any] | None = None,
    ):
        super().__init__(config or {})
        runtime = _load_codex_runtime_config()
        configured_key = str(self.config.get("api_key", "") or "").strip()
        detected_key, detected_source = _load_platform_api_key(
            auth_path=auth_path, environ=environ
        )
        self.api_key = configured_key or detected_key
        self.platform_api_source = "RockCore 设置" if configured_key else detected_source
        logged_in, login_source, binary = _detect_chatgpt_login(
            auth_path=auth_path,
            environ=environ,
            runner=login_status_runner,
        )
        self.chatgpt_authenticated = logged_in
        self.chatgpt_source = login_source
        self.codex_binary = binary
        self._process_environment = utf8_environment(
            None if environ is None else environ
        )
        if self.api_key:
            self.authentication_mode = "platform_api"
            self.auth_source = self.platform_api_source
        elif self.chatgpt_authenticated:
            self.authentication_mode = "chatgpt_cli"
            self.auth_source = self.chatgpt_source
        else:
            self.authentication_mode = "unavailable"
            self.auth_source = "unavailable"

        self.base_url = self.config.get("base_url") or runtime["base_url"]
        self.wire_api = self.config.get("wire_api") or runtime["wire_api"]
        self.model = self.config.get("model") or runtime["model"]
        self.model_provider = runtime["provider"]
        self.proxy, self.proxy_source = _detect_proxy(environ)
        self.max_retries = int(self.config.get("max_retries", 5))
        self.timeout = float(self.config.get("timeout", 60))
        self.cli_timeout = float(self.config.get("cli_timeout", 600))
        self._clients: dict[str, Any] = {}

    @property
    def is_authenticated(self) -> bool:
        return self.authentication_mode != "unavailable"

    async def _get_client(self, agent_type: str = "default"):
        """Create a public API client only when a Platform API key exists."""
        if not self.api_key:
            raise RuntimeError(
                "未配置 OPENAI_API_KEY，不能使用 Platform API 通道；"
                "ChatGPT 登录应通过本机 codex exec 调用"
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

    def _get_sandbox_mode(self, agent_type: str) -> str:
        modes = {
            "governor": "read-only",
            "reviewer": "read-only",
            "emergency_coder": "workspace-write",
        }
        configured = str(self.config.get("sandbox_mode", "") or "").replace("_", "-")
        return modes.get(agent_type, configured or "read-only")

    def get_allowed_sandbox_modes(self) -> list[str]:
        return ["read_only", "workspace_write", "full_access"]

    @staticmethod
    def _reasoning_effort(value: Any) -> str:
        effort = str(value or "").lower()
        return effort if effort in REASONING_EFFORTS else ""

    @staticmethod
    def _format_cli_prompt(system_prompt: str, messages: list[dict]) -> str:
        parts = [
            "SYSTEM INSTRUCTIONS:\n" + system_prompt.strip(),
            "CONVERSATION:",
        ]
        for message in messages or []:
            role = str(message.get("role", "user")).upper()
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            parts.append(f"{role}:\n{content}")
        parts.append(
            "Follow the system instructions and return only the requested final "
            "response. Do not include progress commentary."
        )
        return "\n\n".join(parts)

    @staticmethod
    def _parse_codex_jsonl(output: str) -> tuple[str, dict, int]:
        """Extract the final agent message and usage from Codex JSONL events."""
        messages: list[str] = []
        errors: list[str] = []
        usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }
        event_count = 0
        for line in (output or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_count += 1
            event_type = str(event.get("type", ""))
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if event_type == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    messages.append(text.strip())
            elif event_type in {"turn.failed", "error"}:
                error = event.get("error") or event.get("message")
                if isinstance(error, dict):
                    error = error.get("message") or json.dumps(error, ensure_ascii=False)
                if error:
                    errors.append(str(error))

            event_usage = event.get("usage")
            if isinstance(event_usage, dict):
                usage["input_tokens"] = max(
                    usage["input_tokens"],
                    int(event_usage.get("input_tokens", 0) or 0),
                )
                usage["output_tokens"] = max(
                    usage["output_tokens"],
                    int(event_usage.get("output_tokens", 0) or 0),
                )
                usage["cached_input_tokens"] = max(
                    usage["cached_input_tokens"],
                    int(
                        event_usage.get("cached_input_tokens", 0)
                        or event_usage.get("cache_read_input_tokens", 0)
                        or 0
                    ),
                )

        usage["cached_input_tokens"] = min(
            usage["cached_input_tokens"], usage["input_tokens"]
        )

        if messages:
            return messages[-1], usage, event_count
        if errors:
            raise RuntimeError("Codex CLI 执行失败：" + "；".join(errors)[:500])
        raise RuntimeError("Codex CLI 未返回最终回复")

    async def _run_codex_exec(
        self,
        prompt: str,
        *,
        cwd: str,
        sandbox_mode: str,
        model: str = "",
        reasoning_effort: str = "",
        image_paths: list[str] | None = None,
    ) -> tuple[str, str, int]:
        arguments = [
            "exec",
            "--json",
            "--ephemeral",
            "--color", "never",
            "--sandbox", sandbox_mode,
            "--skip-git-repo-check",
            "-C", cwd,
        ]
        if model and model != "codex-sdk":
            arguments.extend(["--model", model])
        effort = self._reasoning_effort(reasoning_effort)
        if effort:
            arguments.extend([
                "--config", f'model_reasoning_effort="{effort}"',
            ])
        for image_path in image_paths or []:
            arguments.extend(["--image", image_path])
        arguments.append("-")
        command = _prepare_codex_command(
            self.codex_binary,
            arguments,
            environ=self._process_environment,
        )

        # qasync uses a selector loop on Windows, whose asyncio subprocess
        # methods are unsupported. Popen + to_thread keeps the GUI responsive
        # while retaining cancellation and timeout cleanup.
        if sys.platform == "win32":
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=self._process_environment,
                creationflags=no_window_creation_flags(),
            )
            try:
                stdout, stderr = await asyncio.to_thread(
                    process.communicate,
                    prompt.encode("utf-8"),
                    timeout=self.cli_timeout,
                )
            except asyncio.CancelledError:
                process.kill()
                await asyncio.to_thread(process.communicate)
                raise
            except subprocess.TimeoutExpired as error:
                process.kill()
                await asyncio.to_thread(process.communicate)
                raise RuntimeError(
                    f"Codex CLI 执行超时（{self.cli_timeout:g} 秒）"
                ) from error
            return (
                decode_process_output(stdout),
                decode_process_output(stderr),
                int(process.returncode or 0),
            )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=self._process_environment,
        )
        try:
            stdout, stderr = await process.communicate(prompt.encode("utf-8"))
        except asyncio.CancelledError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            raise
        stdout_text = decode_process_output(stdout)
        stderr_text = decode_process_output(stderr)
        return stdout_text, stderr_text, process.returncode

    @staticmethod
    def _resolve_cwd(kwargs: dict) -> str:
        project_root = kwargs.get("project_root") or kwargs.get("cwd")
        task = kwargs.get("task")
        if not project_root and task:
            job = getattr(task, "job", None)
            project = getattr(job, "project", None) if job else None
            project_root = getattr(project, "root_path", "") if project else ""
        path = Path(project_root or os.getcwd()).expanduser()
        configured = path if path.is_dir() else None
        return str(resolve_working_dir(
            configured,
            install_dir=(
                application_dir() if getattr(sys, "frozen", False) else None
            ),
        ))

    @staticmethod
    def _resolve_image_paths(kwargs: dict) -> list[str]:
        """Resolve only prevalidated image attachment paths."""
        records = kwargs.get("attachments")
        if records is None:
            task = kwargs.get("task")
            job = getattr(task, "job", None) if task else None
            records = getattr(job, "attachments", None) if job else None
        paths: list[str] = []
        for record in list(records or []):
            if not isinstance(record, dict):
                continue
            path = Path(str(record.get("path", ""))).expanduser()
            if path.is_file() and path.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
            }:
                paths.append(str(path.resolve()))
        return paths[:8]

    @staticmethod
    def _messages_with_images(
        messages: list[dict], image_paths: list[str], *, responses_api: bool
    ) -> list[dict]:
        """Attach local images to the final user message for Platform API calls."""
        if not image_paths:
            return list(messages)
        enriched = [dict(message) for message in messages]
        user_index = next(
            (index for index in range(len(enriched) - 1, -1, -1)
             if enriched[index].get("role") == "user"),
            None,
        )
        if user_index is None:
            enriched.append({"role": "user", "content": "Inspect the attached images."})
            user_index = len(enriched) - 1
        original = enriched[user_index].get("content", "")
        text_type = "input_text" if responses_api else "text"
        image_type = "input_image" if responses_api else "image_url"
        content = [{"type": text_type, "text": str(original)}]
        for path_text in image_paths:
            path = Path(path_text)
            mime_type = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif",
                ".bmp": "image/bmp",
            }.get(path.suffix.lower(), "image/png")
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            data_url = f"data:{mime_type};base64,{encoded}"
            if responses_api:
                content.append({"type": image_type, "image_url": data_url})
            else:
                content.append({
                    "type": image_type,
                    "image_url": {"url": data_url},
                })
        enriched[user_index]["content"] = content
        return enriched

    async def _chat_via_codex_exec(
        self, system_prompt: str, messages: list[dict], **kwargs
    ) -> dict:
        if not self.codex_binary or not self.chatgpt_authenticated:
            raise RuntimeError(
                "未检测到有效的 ChatGPT/Codex 登录；请先运行 codex login"
            )
        prompt = self._format_cli_prompt(system_prompt, messages)
        requested_model = str(kwargs.get("model", "") or "")
        reasoning_effort = self._reasoning_effort(
            kwargs.get("reasoning_effort")
        )
        exec_options = {
            "cwd": self._resolve_cwd(kwargs),
            "sandbox_mode": self._get_sandbox_mode(kwargs.get("agent_type", "default")),
            "model": requested_model,
            "reasoning_effort": reasoning_effort,
        }
        image_paths = self._resolve_image_paths(kwargs)
        if image_paths:
            exec_options["image_paths"] = image_paths
        stdout, stderr, returncode = await self._run_codex_exec(
            prompt,
            **exec_options,
        )
        if returncode != 0:
            detail = stderr.strip() or stdout.strip() or f"exit code {returncode}"
            raise RuntimeError(f"Codex CLI 执行失败：{detail[:500]}")
        content, usage, event_count = self._parse_codex_jsonl(stdout)
        return {
            "content": content,
            "finish_reason": "stop",
            "usage": usage,
            "raw": {
                "transport": "codex_exec",
                "event_count": event_count,
                "stderr": stderr.strip()[:500],
            },
        }

    async def chat(self, system_prompt: str, messages: list[dict],
                   **kwargs) -> dict:
        agent_type = kwargs.get("agent_type", "default")
        if self.authentication_mode == "chatgpt_cli":
            cli_kwargs = dict(kwargs)
            cli_kwargs.setdefault("agent_type", agent_type)
            return await self._chat_via_codex_exec(
                system_prompt, messages, **cli_kwargs
            )
        if self.authentication_mode != "platform_api":
            raise RuntimeError(
                "未找到可用认证：请登录 ChatGPT/Codex，或配置 OPENAI_API_KEY"
            )

        client = await self._get_client(agent_type)
        requested_model = kwargs.get("model")
        reasoning_effort = self._reasoning_effort(
            kwargs.get("reasoning_effort")
        )
        model = (
            self.model
            if not requested_model or requested_model == "codex-sdk"
            else requested_model
        )
        image_paths = self._resolve_image_paths(kwargs)
        if self.wire_api == "responses":
            request_options = {
                "model": model,
                "instructions": system_prompt,
                "input": self._messages_with_images(
                    messages, image_paths, responses_api=True
                ),
                "max_output_tokens": kwargs.get("max_tokens", 4096),
            }
            if reasoning_effort:
                request_options["reasoning"] = {"effort": reasoning_effort}
            response = await client.responses.create(
                **request_options,
            )
            usage = self.normalize_usage(getattr(response, "usage", None))
            return {
                "content": response.output_text or "",
                "finish_reason": (
                    "stop" if response.status == "completed" else response.status
                ),
                "usage": usage,
                "raw": response,
            }

        full_messages = [{"role": "system", "content": system_prompt}] + (
            self._messages_with_images(
                messages, image_paths, responses_api=False
            )
        )
        request_options = {
            "model": model,
            "messages": full_messages,
        }
        if str(model).startswith("gpt-5.6"):
            request_options["max_completion_tokens"] = kwargs.get(
                "max_tokens", 4096
            )
        else:
            request_options["temperature"] = kwargs.get("temperature", 0.2)
            request_options["max_tokens"] = kwargs.get("max_tokens", 4096)
        if reasoning_effort:
            request_options["reasoning_effort"] = reasoning_effort
        response = await client.chat.completions.create(**request_options)
        choice = response.choices[0]
        return {
            "content": choice.message.content or "",
            "finish_reason": choice.finish_reason or "stop",
            "usage": self.normalize_usage(response.usage),
            "raw": response,
        }

    async def chat_with_tools(self, system_prompt: str, messages: list[dict],
                              tools: list[dict], **kwargs) -> dict:
        return await self.chat(system_prompt, messages, **kwargs)

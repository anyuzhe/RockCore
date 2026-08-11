"""Minimal cross-platform MCP stdio JSON-RPC client.

The implementation intentionally uses ``subprocess.Popen`` plus worker threads.
That works with qasync on Windows, where asyncio's subprocess transport is not
available under every GUI event-loop policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from app.subprocess_utils import no_window_creation_flags, utf8_environment
from orchestrator.agent_config import MCPServerConfig

logger = logging.getLogger(__name__)

ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
SUPPORTED_PROTOCOL_VERSIONS = {
    "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05",
}
DEFAULT_PROTOCOL_VERSION = "2025-11-25"


class MCPClientError(RuntimeError):
    pass


def prepare_stdio_command(command: str, args: list[str], *,
                          platform: str | None = None,
                          environ: dict[str, str] | None = None) -> list[str]:
    """Build a shell-free command, wrapping Windows batch launchers safely."""
    platform_name = sys.platform if platform is None else platform
    environment = os.environ if environ is None else environ
    executable = shutil.which(command, path=environment.get("PATH")) or command
    values = [str(executable), *[str(value) for value in args]]
    if platform_name == "win32" and Path(executable).suffix.lower() in {
        ".cmd", ".bat",
    }:
        comspec = environment.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c", subprocess.list2cmdline(values)]
    return values


def resolve_server_environment(values: dict[str, str],
                               environ: dict[str, str] | None = None) -> dict:
    """Resolve ${NAME} references without persisting secrets in project JSON."""
    source = dict(os.environ if environ is None else environ)
    result = utf8_environment(source)
    for key, raw_value in values.items():
        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in source:
                raise MCPClientError(
                    f"MCP 环境变量 {name} 未设置（服务环境项 {key}）"
                )
            return source[name]
        result[str(key)] = ENV_REFERENCE.sub(replace, str(raw_value))
    return result


class MCPStdioClient:
    """A sequential JSON-RPC client for one persistent stdio server."""

    def __init__(self, config: MCPServerConfig, project_root: str | Path):
        self.config = config
        self.project_root = Path(project_root).resolve()
        self.process: subprocess.Popen | None = None
        self.server_info: dict[str, Any] = {}
        self.capabilities: dict[str, Any] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._stderr_lines: deque[str] = deque(maxlen=30)
        self._stderr_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    async def start(self):
        if self.is_running:
            return
        await asyncio.to_thread(self._spawn_sync)
        try:
            result = await self._request_started("initialize", {
                "protocolVersion": DEFAULT_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "RockCore", "version": "1"},
            })
            negotiated_version = str(result.get("protocolVersion") or "")
            if negotiated_version not in SUPPORTED_PROTOCOL_VERSIONS:
                raise MCPClientError(
                    f"MCP 服务 {self.config.name} 返回不支持的协议版本："
                    f"{negotiated_version or '<empty>'}"
                )
            self.server_info = dict(result.get("serverInfo") or {})
            self.capabilities = dict(result.get("capabilities") or {})
            await self.notify("notifications/initialized", {})
        except Exception:
            await self.close()
            raise

    async def request(self, method: str, params: dict | None = None) -> Any:
        if not self.is_running:
            await self.start()
        return await self._request_started(method, params or {})

    async def _request_started(self, method: str, params: dict) -> Any:
        async with self._lock:
            request_id = self._next_id
            self._next_id += 1
            message = {
                "jsonrpc": "2.0", "id": request_id,
                "method": method, "params": params,
            }
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._exchange_sync, message, request_id
                    ),
                    timeout=self.config.timeout_seconds,
                )
            except asyncio.TimeoutError as error:
                await self.close()
                raise MCPClientError(
                    f"MCP 服务 {self.config.name} 请求 {method} 超时"
                ) from error
            if response.get("error"):
                detail = response["error"]
                raise MCPClientError(
                    f"MCP 服务 {self.config.name} 调用 {method} 失败：{detail}"
                )
            return response.get("result") or {}

    async def notify(self, method: str, params: dict | None = None):
        if not self.is_running:
            return
        async with self._lock:
            await asyncio.to_thread(self._write_sync, {
                "jsonrpc": "2.0", "method": method, "params": params or {},
            })

    async def list_tools(self) -> list[dict]:
        result = await self.request("tools/list", {})
        tools = result.get("tools") or []
        return [item for item in tools if isinstance(item, dict)]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        result = await self.request("tools/call", {
            "name": name, "arguments": arguments,
        })
        return dict(result) if isinstance(result, dict) else {"content": result}

    async def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        await asyncio.to_thread(self._close_sync, process)

    def _spawn_sync(self):
        environment = resolve_server_environment(self.config.env)
        command = prepare_stdio_command(
            self.config.command, self.config.args, environ=environment
        )
        kwargs: dict[str, Any] = {
            "cwd": str(self.project_root),
            "env": environment,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "bufsize": 0,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = no_window_creation_flags()
        try:
            self.process = subprocess.Popen(command, **kwargs)
        except OSError as error:
            raise MCPClientError(
                f"无法启动 MCP 服务 {self.config.name}：{error}"
            ) from error
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(self.process,),
            name=f"mcp-{self.config.name}-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def _exchange_sync(self, message: dict, request_id: int) -> dict:
        self._write_sync(message)
        while True:
            response = self._read_sync()
            if response.get("id") == request_id:
                return response
            # MCP servers may ask clients for optional capabilities. RockCore
            # does not grant roots, sampling, or elicitation implicitly.
            if response.get("id") is not None and response.get("method"):
                self._write_sync({
                    "jsonrpc": "2.0", "id": response["id"],
                    "error": {"code": -32601, "message": "Unsupported client method"},
                })

    def _write_sync(self, message: dict):
        process = self.process
        if process is None or process.poll() is not None or process.stdin is None:
            raise MCPClientError(self._stopped_message())
        payload = json.dumps(
            message, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise MCPClientError(self._stopped_message()) from error

    def _read_sync(self) -> dict:
        process = self.process
        if process is None or process.stdout is None:
            raise MCPClientError(self._stopped_message())
        first = process.stdout.readline()
        if not first:
            raise MCPClientError(self._stopped_message())
        if first.lower().startswith(b"content-length:"):
            try:
                length = int(first.split(b":", 1)[1].strip())
            except (ValueError, IndexError) as error:
                raise MCPClientError("MCP Content-Length 响应无效") from error
            while True:
                header = process.stdout.readline()
                if header in {b"\r\n", b"\n", b""}:
                    break
            payload = process.stdout.read(length)
        else:
            payload = first.strip()
        try:
            value = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise MCPClientError(
                f"MCP 服务 {self.config.name} 返回了无效 JSON"
            ) from error
        if not isinstance(value, dict):
            raise MCPClientError("MCP JSON-RPC 响应必须是对象")
        return value

    def _drain_stderr(self, process: subprocess.Popen):
        if process.stderr is None:
            return
        try:
            for line in iter(process.stderr.readline, b""):
                self._stderr_lines.append(
                    line.decode("utf-8", errors="replace").strip()
                )
        except (OSError, ValueError):
            return

    def _stopped_message(self) -> str:
        detail = " | ".join(self._stderr_lines)[-1000:]
        suffix = f"：{detail}" if detail else ""
        return f"MCP 服务 {self.config.name} 已停止{suffix}"

    @staticmethod
    def _close_sync(process: subprocess.Popen):
        if process.poll() is not None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

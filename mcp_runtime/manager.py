"""MCP server lifecycle, tool discovery, namespacing, and permissions."""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.agent_config import MCPConfig, MCPServerConfig

from .client import MCPStdioClient

logger = logging.getLogger(__name__)

READ_PREFIXES = (
    "get", "list", "read", "search", "find", "fetch", "query", "inspect",
    "lookup", "view", "show", "status", "download",
)
MUTATION_WORDS = (
    "create", "write", "update", "edit", "delete", "remove", "send",
    "publish", "merge", "close", "move", "upload", "execute", "run",
)


@dataclass(frozen=True)
class MCPTool:
    public_name: str
    server_name: str
    remote_name: str
    description: str
    input_schema: dict
    read_only: bool


class MCPManager:
    """Expose healthy MCP tools without making an unavailable server fatal."""

    def __init__(self, project_root: str | Path = "."):
        self.project_root = Path(project_root).resolve()
        self.config = MCPConfig()
        self._clients: dict[str, MCPStdioClient] = {}
        self._tools: dict[str, MCPTool] = {}
        self._status: dict[str, dict[str, Any]] = {}

    async def configure(self, project_root: str | Path,
                        config: MCPConfig | None = None) -> dict[str, dict]:
        await self.close()
        self.project_root = Path(project_root).resolve()
        self.config = config or MCPConfig()
        self._tools = {}
        self._status = {}
        if not self.config.enabled:
            return self.statuses()

        for server in self.config.servers:
            if not server.enabled:
                self._status[server.name or "unnamed"] = {
                    "status": "disabled", "tools": 0,
                }
                continue
            client = MCPStdioClient(server, self.project_root)
            try:
                await client.start()
                remote_tools = await client.list_tools()
                self._clients[server.name] = client
                count = self._register_tools(server, remote_tools)
                self._status[server.name] = {
                    "status": "connected", "tools": count,
                    "server_info": client.server_info,
                }
            except Exception as error:
                await client.close()
                self._status[server.name] = {
                    "status": "unavailable", "tools": 0,
                    "error": str(error)[:1000],
                }
                logger.warning("MCP server %s unavailable: %s", server.name, error)
        return self.statuses()

    def tool_definitions(self, task_type: str | None = None) -> list[dict]:
        definitions = []
        read_only_task = task_type in {"analysis", "review", "testing"}
        for tool in sorted(self._tools.values(), key=lambda item: item.public_name):
            if read_only_task and not tool.read_only:
                continue
            definitions.append({
                "type": "function",
                "function": {
                    "name": tool.public_name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            })
        return definitions

    def has_tool(self, public_name: str) -> bool:
        return public_name in self._tools

    def tool_is_read_only(self, public_name: str) -> bool:
        tool = self._tools.get(public_name)
        return bool(tool and tool.read_only)

    async def call_tool(self, public_name: str, arguments: dict) -> dict:
        tool = self._tools.get(public_name)
        if not tool:
            return {"status": "error", "error": f"Unknown MCP tool: {public_name}"}
        client = self._clients.get(tool.server_name)
        if not client:
            return {
                "status": "error",
                "error": f"MCP service unavailable: {tool.server_name}",
            }
        result = await client.call_tool(tool.remote_name, arguments)
        return {
            "status": "error" if result.get("isError") else "success",
            "server": tool.server_name,
            "remote_tool": tool.remote_name,
            **result,
        }

    def statuses(self) -> dict[str, dict]:
        return {name: dict(value) for name, value in self._status.items()}

    async def close(self):
        clients, self._clients = list(self._clients.values()), {}
        self._tools = {}
        for client in clients:
            try:
                await client.close()
            except Exception as error:
                logger.warning("Could not close MCP server: %s", error)

    def _register_tools(self, server: MCPServerConfig,
                        remote_tools: list[dict]) -> int:
        count = 0
        for raw_tool in remote_tools:
            remote_name = str(raw_tool.get("name") or "").strip()
            if not remote_name or not any(
                fnmatch.fnmatch(remote_name, pattern)
                for pattern in server.allow_tools
            ):
                continue
            read_only = self._is_read_only(raw_tool)
            if server.read_only and not read_only:
                logger.info(
                    "MCP read-only server %s hid mutating tool %s",
                    server.name, remote_name,
                )
                continue
            public_name = self._public_name(server.name, remote_name)
            if public_name in self._tools:
                logger.warning("Duplicate MCP public tool name: %s", public_name)
                continue
            schema = raw_tool.get("inputSchema") or {
                "type": "object", "properties": {},
            }
            if not isinstance(schema, dict):
                schema = {"type": "object", "properties": {}}
            description = str(raw_tool.get("description") or remote_name)
            self._tools[public_name] = MCPTool(
                public_name=public_name,
                server_name=server.name,
                remote_name=remote_name,
                description=f"[MCP: {server.name}] {description}"[:1000],
                input_schema=schema,
                read_only=read_only,
            )
            count += 1
        return count

    @staticmethod
    def _is_read_only(raw_tool: dict) -> bool:
        annotations = raw_tool.get("annotations") or {}
        if isinstance(annotations, dict) and "readOnlyHint" in annotations:
            return annotations.get("readOnlyHint") is True
        name = str(raw_tool.get("name") or "").lower()
        words = [part for part in re.split(r"[^a-z0-9]+", name) if part]
        if any(word in MUTATION_WORDS for word in words):
            return False
        return bool(words and words[0] in READ_PREFIXES)

    @staticmethod
    def _public_name(server_name: str, remote_name: str) -> str:
        base = "mcp__" + re.sub(
            r"[^A-Za-z0-9_-]", "_", server_name
        ) + "__" + re.sub(r"[^A-Za-z0-9_-]", "_", remote_name)
        if len(base) <= 64:
            return base
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        return f"{base[:55]}_{digest}"

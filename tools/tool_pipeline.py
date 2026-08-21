"""Composable middleware for local and MCP tool execution."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass
class ToolExecutionContext:
    task: Any
    tool_name: str
    arguments: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


class ToolMiddleware(Protocol):
    async def before(self, context: ToolExecutionContext) -> None: ...
    async def after(self, context: ToolExecutionContext,
                    result: dict[str, Any]) -> None: ...


class ToolPipeline:
    def __init__(self):
        self._middlewares: list[ToolMiddleware] = []

    def add(self, middleware: ToolMiddleware) -> None:
        self._middlewares.append(middleware)

    def clear(self) -> None:
        self._middlewares.clear()

    async def execute(
        self,
        context: ToolExecutionContext,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        entered: list[ToolMiddleware] = []
        try:
            for middleware in self._middlewares:
                outcome = middleware.before(context)
                if inspect.isawaitable(outcome):
                    await outcome
                entered.append(middleware)
            result = await operation()
        except Exception as error:
            result = {
                "status": "error",
                "error": str(error),
                "tool": context.tool_name,
            }
        for middleware in reversed(entered):
            try:
                outcome = middleware.after(context, result)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                # Audit/post-processing must not replace the concrete tool result.
                continue
        return result


class SessionToolMiddleware:
    MUTATING = {
        "write_file", "apply_patch", "insert_before", "insert_after",
        "write_docx", "write_pptx", "write_pdf", "write_temp_file",
        "promote_artifact", "run_command", "run_tests",
    }

    def __init__(self, session_runtime):
        self.session_runtime = session_runtime

    async def before(self, context: ToolExecutionContext) -> None:
        task_id = str(getattr(context.task, "task_id", "") or "")
        payload = {
            "tool": context.tool_name,
            "path": str(
                context.arguments.get("path")
                or context.arguments.get("target_path") or ""
            ),
            "argument_keys": sorted(context.arguments),
        }
        if context.tool_name in self.MUTATING or context.metadata.get("mutating"):
            await self.session_runtime.barrier(
                "tool_call_prepared", task_id=task_id, **payload,
            )
        else:
            await self.session_runtime.record(
                "tool_call_prepared", task_id=task_id, **payload,
            )

    async def after(self, context: ToolExecutionContext,
                    result: dict[str, Any]) -> None:
        task_id = str(getattr(context.task, "task_id", "") or "")
        await self.session_runtime.record(
            "tool_call_completed",
            task_id=task_id,
            tool=context.tool_name,
            path=str(result.get("path") or context.arguments.get("path") or ""),
            status=str(result.get("status") or ("error" if result.get("error") else "success")),
            error=str(result.get("error") or "")[:1000],
            source_version=str(result.get("source_version") or ""),
        )

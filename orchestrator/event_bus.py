"""Event-driven communication bus for the orchestrator."""

import asyncio
import logging
from contextvars import ContextVar, Token
from typing import Callable, Coroutine, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class EventBus:
    """Simple async event bus for decoupled communication."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable[..., Coroutine]]] = defaultdict(list)
        self._history: list[dict] = []
        self._max_history = 1000
        self._job_context: ContextVar[str] = ContextVar(
            "rockcore_event_job_id", default=""
        )

    def bind_job(self, job_id: str) -> Token:
        """Bind events in the current async task tree to one Job."""
        return self._job_context.set(str(job_id or ""))

    def reset_job(self, token: Token):
        self._job_context.reset(token)

    def subscribe(self, event_type: str, handler: Callable[..., Coroutine] | None = None):
        """Register a handler for an event type. Can be used as decorator."""
        if handler is not None:
            self._subscribers[event_type].append(handler)
            return handler

        def decorator(fn: Callable[..., Coroutine]):
            self._subscribers[event_type].append(fn)
            return fn
        return decorator

    def unsubscribe(self, event_type: str, handler: Callable[..., Coroutine]):
        if handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)

    async def publish(self, event_type: str, **data):
        await self._publish(event_type, data, include_wildcards=True)

    async def publish_transient(self, event_type: str, **data):
        """Notify normal subscribers/UI without writing to wildcard audit sinks."""
        await self._publish(event_type, data, include_wildcards=False)

    async def _publish(self, event_type: str, data: dict,
                       *, include_wildcards: bool):
        if "job_id" not in data:
            job_id = self._job_context.get()
            if job_id:
                data["job_id"] = job_id
        event = {"type": event_type, "data": data}
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        handlers = (
            list(self._subscribers.get("*", ())) if include_wildcards else []
        )
        handlers.extend(self._subscribers.get(event_type, ()))
        # Wildcard subscribers run first so durable audit sinks have accepted
        # the event before a terminal handler generates a report from it.
        logger.debug(f"Event: {event_type} -> {len(handlers)} handlers")
        seen: set[int] = set()
        for handler in handlers:
            marker = id(handler)
            if marker in seen:
                continue
            seen.add(marker)
            try:
                await handler(event_type, **data)
            except Exception as e:
                logger.error(f"Handler error for {event_type}: {e}")

    def get_history(self, event_type: str | None = None,
                    limit: int = 50) -> list[dict]:
        if event_type:
            return [e for e in self._history[-limit:]
                    if e["type"] == event_type]
        return list(self._history[-limit:])

    def clear_history(self):
        self._history.clear()

    def drain_history(self, limit: int | None = None) -> list[dict]:
        """Atomically take queued UI events without clearing newly published ones."""
        if limit is None or limit >= len(self._history):
            events = self._history
            self._history = []
            return events
        events = self._history[:limit]
        del self._history[:limit]
        return events

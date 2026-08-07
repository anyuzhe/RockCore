"""Event-driven communication bus for the orchestrator."""

import asyncio
import logging
from typing import Callable, Coroutine, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class EventBus:
    """Simple async event bus for decoupled communication."""

    def __init__(self):
        self._subscribers: dict[str, list[Callable[..., Coroutine]]] = defaultdict(list)
        self._history: list[dict] = []
        self._max_history = 1000

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
        event = {"type": event_type, "data": data}
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history.pop(0)
        logger.debug(f"Event: {event_type} -> {len(self._subscribers[event_type])} handlers")
        for handler in self._subscribers[event_type]:
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

"""WebSocket manager for real-time event streaming to the GUI."""

import json
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connections for real-time UI updates."""

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self._connections.append(websocket)
        logger.info(f"WebSocket connected: {len(self._connections)} total")

    async def disconnect(self, websocket: WebSocket):
        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info(f"WebSocket disconnected: {len(self._connections)} remaining")

    async def broadcast(self, event_type: str, **data):
        message = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception as e:
                logger.warning(f"WebSocket send error: {e}")
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    async def broadcast_event(self, event_type: str, **data):
        await self.broadcast(event_type, **data)

    @property
    def count(self) -> int:
        return len(self._connections)
"""Append-only execution events and deterministic state projections."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


EVENT_LOG_VERSION = 1


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False, default=str)
        return value
    except (TypeError, ValueError, OverflowError):
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class SessionEvent:
    version: int
    seq: int
    session_id: str
    job_id: str
    task_id: str
    event_type: str
    timestamp: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionEventStore:
    """One job-scoped JSONL journal with ordered, durable appends."""

    def __init__(self, path: str | os.PathLike[str], *, session_id: str,
                 job_id: str):
        self.path = Path(path)
        self.session_id = str(session_id or job_id)
        self.job_id = str(job_id)
        self._lock = asyncio.Lock()
        self._seq = self._recover_sequence()

    def _recover_sequence(self) -> int:
        if not self.path.is_file():
            return 0
        last = 0
        valid_end = 0
        try:
            with self.path.open("rb") as stream:
                for line in stream:
                    try:
                        payload = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        # A process may have stopped during the final append.
                        # The next valid event continues from the last full line.
                        break
                    last = max(last, int(payload.get("seq") or 0))
                    valid_end = stream.tell()
            if valid_end < self.path.stat().st_size:
                with self.path.open("r+b") as stream:
                    stream.truncate(valid_end)
        except OSError:
            return 0
        return last

    async def append(self, event_type: str, *, task_id: str = "",
                     data: dict[str, Any] | None = None,
                     durable: bool = False) -> SessionEvent:
        async with self._lock:
            self._seq += 1
            event = SessionEvent(
                version=EVENT_LOG_VERSION,
                seq=self._seq,
                session_id=self.session_id,
                job_id=self.job_id,
                task_id=str(task_id or ""),
                event_type=str(event_type),
                timestamp=datetime.now().astimezone().isoformat(),
                data=dict(_json_safe(data or {})),
            )
            line = json.dumps(
                event.as_dict(), ensure_ascii=False, separators=(",", ":"),
                default=str,
            ) + "\n"
            await asyncio.to_thread(self._append_sync, line, durable)
            return event

    def _append_sync(self, line: str, durable: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())

    async def flush(self) -> None:
        """Wait for all prior appends; each append already flushes its stream."""
        async with self._lock:
            return

    def read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        events: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        events.append(payload)
        except OSError:
            return events
        return events


class SessionProjection:
    """Build small views from the full journal without mutating history."""

    TERMINAL_EVENTS = {"job_finished", "job_cancelled", "job_failed"}

    @staticmethod
    def audit(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [dict(event) for event in events]

    @staticmethod
    def ui(events: Iterable[dict[str, Any]], *, limit: int = 8) -> dict[str, Any]:
        items = list(events)
        latest = items[-1] if items else {}
        task_states: dict[str, str] = {}
        for event in items:
            task_id = str(event.get("task_id") or "")
            data = dict(event.get("data") or {})
            status = str(data.get("status") or data.get("new_state") or "")
            if task_id and status:
                task_states[task_id] = status
        return {
            "event_count": len(items),
            "latest": latest,
            "recent": items[-max(1, limit):],
            "task_states": task_states,
            "terminal": str(latest.get("event_type") or "")
            in SessionProjection.TERMINAL_EVENTS,
        }

    @staticmethod
    def model_surface(events: Iterable[dict[str, Any]], *, limit: int = 24,
                      max_chars: int = 8_000) -> str:
        items = list(events)
        compact = []
        for event in items[-max(1, limit):]:
            data = dict(event.get("data") or {})
            selected = {
                key: data[key] for key in (
                    "status", "phase", "tool", "path", "summary", "error",
                    "failure", "new_state", "old_state", "changed_files",
                ) if key in data and data[key] not in (None, "", [], {})
            }
            compact.append({
                "seq": event.get("seq"),
                "task_id": event.get("task_id", ""),
                "type": event.get("event_type", ""),
                "data": selected,
            })
        text = json.dumps(compact, ensure_ascii=False, default=str)
        if len(text) > max_chars:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            text = text[-max_chars:]
            return f"[surface tail; sha256={digest}]" + text
        return text


class AgentInbox:
    """Checkpointable input queue consumed only at safe turn boundaries."""

    def __init__(self, entries: Iterable[dict[str, Any]] | None = None):
        self._entries = [dict(item) for item in (entries or [])]
        self._next_id = max(
            [int(item.get("id") or 0) for item in self._entries] or [0]
        ) + 1

    def enqueue(self, content: str, *, source: str = "user") -> int:
        entry_id = self._next_id
        self._next_id += 1
        self._entries.append({
            "id": entry_id,
            "source": str(source),
            "content": str(content),
            "consumed": False,
        })
        return entry_id

    def drain(self) -> list[dict[str, Any]]:
        pending = [item for item in self._entries if not item.get("consumed")]
        for item in pending:
            item["consumed"] = True
        return [dict(item) for item in pending]

    def checkpoint(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._entries[-100:]]

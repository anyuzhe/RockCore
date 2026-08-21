"""Composition root for one job's durable runtime services.

Engine coordinates workflow states; this module owns append/replay durability,
tool middleware attachment, and the compact surfaces derived from that history.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .session_runtime import SessionRuntime


@dataclass
class WorkflowRuntimeServices:
    job_id: str
    project_root: str
    session: SessionRuntime

    @classmethod
    def create(cls, project_root: str, job_id: str,
               *, session_id: str = "") -> "WorkflowRuntimeServices":
        return cls(
            job_id=str(job_id),
            project_root=str(project_root),
            session=SessionRuntime(
                project_root, job_id, session_id=session_id or job_id,
            ),
        )

    def attach_tool_broker(self, broker: Any) -> None:
        if broker is not None and hasattr(broker, "set_session_runtime"):
            broker.set_session_runtime(self.session)

    async def record_bus_event(self, event_type: str, **data: Any) -> None:
        payload = dict(data)
        payload.pop("job_id", None)
        await self.session.record(
            event_type,
            task_id=str(payload.pop("task_id", "") or ""),
            **payload,
        )

    async def provider_barrier(self, event_type: str, **data: Any) -> None:
        payload = dict(data)
        payload.pop("job_id", None)
        await self.session.barrier(
            event_type,
            task_id=str(payload.pop("task_id", "") or ""),
            **payload,
        )

    def replay_snapshot(self) -> dict[str, Any]:
        events = self.session.replay()
        canonical = json.dumps(
            events, ensure_ascii=False, sort_keys=True, default=str,
            separators=(",", ":"),
        )
        return {
            "job_id": self.job_id,
            "event_count": len(events),
            "last_sequence": int(events[-1].get("seq") or 0) if events else 0,
            "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "ui": self.session.ui_projection(),
            "model_surface": self.session.model_surface(),
        }

    async def close(self) -> None:
        await self.session.close()

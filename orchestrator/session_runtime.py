"""Job-scoped event runtime and semantic durability barriers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.paths import project_state_dir
from .session_events import SessionEventStore, SessionProjection


class SessionRuntime:
    def __init__(self, project_root: str, job_id: str,
                 *, session_id: str = ""):
        state = project_state_dir(project_root)
        safe_job_id = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in str(job_id)
        )
        path = Path(state) / "runtime" / "sessions" / f"{safe_job_id}.jsonl"
        self.store = SessionEventStore(
            path, session_id=session_id or job_id, job_id=job_id,
        )

    @property
    def path(self) -> Path:
        return self.store.path

    async def record(self, event_type: str, *, task_id: str = "",
                     durable: bool = False, **data: Any):
        return await self.store.append(
            event_type, task_id=task_id, data=data, durable=durable,
        )

    async def barrier(self, event_type: str, *, task_id: str = "",
                      **data: Any):
        """Persist intent before a provider request or external mutation."""
        return await self.record(
            event_type, task_id=task_id, durable=True, **data,
        )

    def replay(self) -> list[dict[str, Any]]:
        return self.store.read()

    def model_surface(self) -> str:
        return SessionProjection.model_surface(self.replay())

    def ui_projection(self) -> dict[str, Any]:
        return SessionProjection.ui(self.replay())

    async def close(self) -> None:
        await self.store.flush()

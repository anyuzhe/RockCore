"""Deterministic project lifecycle hooks configured in .ai/agents.json."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path

from app.subprocess_utils import run_process


HOOK_EVENTS = (
    "before_job", "after_write", "before_test", "before_commit", "after_job",
)


class HookRunner:
    """Run explicit project commands without spending model tokens."""

    def __init__(self, event_bus):
        self.event_bus = event_bus

    async def run(self, event: str, *, job_id: str, project_root: str,
                  commands: list[str], task_id: str = "") -> list[dict]:
        if event not in HOOK_EVENTS:
            raise ValueError(f"Unsupported hook event: {event}")
        results = []
        for command in commands:
            command = str(command or "").strip()
            if not command:
                continue
            await self.event_bus.publish(
                "hook_started", job_id=job_id, task_id=task_id,
                hook=event, command=command,
            )
            try:
                args = shlex.split(command, posix=sys.platform != "win32")
                if sys.platform == "win32":
                    args = [
                        value[1:-1]
                        if len(value) >= 2 and value[0] == value[-1] == '"'
                        else value
                        for value in args
                    ]
                completed = await asyncio.to_thread(
                    run_process, args, cwd=str(Path(project_root).resolve()),
                    capture_output=True, timeout=120,
                    env={
                        **os.environ,
                        "ROCKCORE_HOOK_EVENT": event,
                        "ROCKCORE_JOB_ID": job_id,
                        "ROCKCORE_TASK_ID": task_id,
                        "ROCKCORE_PROJECT_ROOT": str(
                            Path(project_root).resolve()
                        ),
                    },
                )
                result = {
                    "hook": event, "command": command,
                    "status": "passed" if completed.returncode == 0 else "failed",
                    "returncode": completed.returncode,
                    "output": (completed.stdout + "\n" + completed.stderr).strip()[-4000:],
                }
            except Exception as error:
                result = {
                    "hook": event, "command": command, "status": "failed",
                    "returncode": -1, "output": str(error)[:4000],
                }
            results.append(result)
            await self.event_bus.publish(
                "hook_completed", job_id=job_id, task_id=task_id, **result,
            )
            if result["status"] != "passed":
                break
        return results

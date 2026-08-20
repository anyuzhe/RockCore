"""Shutdown and child-process cleanup regression tests."""

import asyncio
import subprocess

from app import subprocess_utils
from orchestrator.engine import Engine


def test_windows_process_tree_cleanup_uses_taskkill(monkeypatch):
    calls = []

    class Process:
        pid = 4321

        def kill(self):
            calls.append("fallback")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
    monkeypatch.setattr(subprocess_utils.subprocess, "run", fake_run)
    monkeypatch.setattr(
        subprocess_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000,
        raising=False,
    )

    assert subprocess_utils.terminate_process_tree(Process()) is True
    assert calls[0][0] == ["taskkill.exe", "/PID", "4321", "/T", "/F"]
    assert calls[0][1]["creationflags"] == 0x08000000
    assert "fallback" not in calls


def test_engine_stop_cancels_and_waits_for_active_job_task(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        finished = asyncio.Event()

        async def active_job():
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        task = asyncio.create_task(active_job(), name="job-shutdown-test")
        engine._job_tasks["JOB-SHUTDOWN"] = task
        await asyncio.sleep(0)
        await engine.stop()

        assert task.done()
        assert finished.is_set()
        # The UI and qasync both may call stop during a normal close.  It is
        # deliberately idempotent and must not touch an already-closed broker.
        await engine.stop()

    asyncio.run(scenario())

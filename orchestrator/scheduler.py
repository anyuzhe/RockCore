"""DAG-aware task scheduler — manages parallel execution with dependency resolution."""

import asyncio
import logging
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class Scheduler:
    """DAG-aware scheduler that runs tasks in parallel when dependencies are met."""

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max_concurrent
        self._semaphore: asyncio.Semaphore | None = None
        self._running: dict[str, asyncio.Task] = {}
        self._pending: list[dict] = []
        self._completed: set[str] = set()
        self._failed: set[str] = set()
        self._paused = False
        self._stopped = False
        self._resume_event: asyncio.Event | None = None

    @property
    def resume_event(self) -> asyncio.Event:
        if self._resume_event is None:
            self._resume_event = asyncio.Event()
            if not self._paused:
                self._resume_event.set()
        return self._resume_event

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        return self._semaphore

    def get_ready_tasks(self, tasks: list[dict]) -> list[dict]:
        """Return tasks whose dependencies are all satisfied."""
        ready = []
        for t in tasks:
            tid = t.get("task_id", "")
            if tid in self._running or tid in self._completed or tid in self._failed:
                continue
            deps = t.get("dependencies", [])
            if all(d in self._completed for d in deps):
                ready.append(t)
        return ready

    def all_done(self, tasks: list[dict]) -> bool:
        """Check if all tasks are completed or failed."""
        tids = {t.get("task_id", "") for t in tasks}
        return tids.issubset(self._completed | self._failed)

    async def run_dag(self, tasks: list[dict],
                      task_runner: Callable[..., Coroutine[Any, Any, Any]],
                      **shared_kwargs) -> dict[str, Any]:
        """Execute a DAG of tasks, running ready tasks in parallel.

        Args:
            tasks: List of task dicts with task_id, dependencies keys.
            task_runner: Async callable(task_id, task_data, **shared_kwargs).
            shared_kwargs: Extra kwargs passed to every task_runner call.

        Returns:
            Dict mapping task_id to result.
        """
        results: dict[str, Any] = {}
        self._stopped = False
        self._completed.clear()
        self._failed.clear()
        self._running.clear()

        while not self.all_done(tasks):
            if self._stopped:
                break
            await self.resume_event.wait()
            if self._stopped:
                break
            ready = self.get_ready_tasks(tasks)
            if not ready and not self._running:
                # Propagate dependency failures so downstream tasks do not stay
                # pending forever after an upstream task fails.
                unresolved = [
                    t for t in tasks
                    if t.get("task_id", "") not in self._completed | self._failed
                ]
                while unresolved:
                    newly_blocked = []
                    for task in unresolved:
                        dependencies = task.get("dependencies", [])
                        failed_dependencies = [
                            dependency for dependency in dependencies
                            if dependency in self._failed
                        ]
                        if failed_dependencies:
                            newly_blocked.append((task, failed_dependencies))
                    if not newly_blocked:
                        break
                    for task, failed_dependencies in newly_blocked:
                        task_id = task.get("task_id", "")
                        self._failed.add(task_id)
                        results[task_id] = {
                            "status": "blocked",
                            "error": (
                                "Blocked by failed dependencies: "
                                + ", ".join(failed_dependencies)
                            ),
                            "blocked_by": failed_dependencies,
                        }
                    unresolved = [
                        task for task in unresolved
                        if task.get("task_id", "") not in self._failed
                    ]

                # Any remainder has missing or cyclic dependencies. Surface it
                # as blocked instead of silently ending with pending tasks.
                for task in unresolved:
                    task_id = task.get("task_id", "")
                    dependencies = task.get("dependencies", [])
                    self._failed.add(task_id)
                    results[task_id] = {
                        "status": "blocked",
                        "error": (
                            "Blocked by unresolved dependencies: "
                            + ", ".join(dependencies)
                        ),
                        "blocked_by": dependencies,
                    }
                break

            # Launch ready tasks (subject to semaphore)
            pending_tasks = []
            for t in ready:
                task_id = t["task_id"]
                async def _run(tid=task_id, tdata=t):
                    async with self.semaphore:
                        self._running[tid] = asyncio.current_task()
                        try:
                            r = await task_runner(tid, tdata, **shared_kwargs)
                            results[tid] = r
                            self._completed.add(tid)
                        except Exception as e:
                            logger.error(f"Task {tid} failed: {e}")
                            self._failed.add(tid)
                            results[tid] = {"error": str(e)}
                        finally:
                            self._running.pop(tid, None)
                pending_tasks.append(_run())

            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            else:
                # No tasks ready but still running — wait a bit
                await asyncio.sleep(0.1)

        return results

    async def run_task(self, task_id: str,
                       coro_factory: Callable[..., Coroutine[Any, Any, Any]],
                       **kwargs) -> Any:
        """Run a single task with concurrency control (legacy API)."""
        if self._stopped:
            raise RuntimeError("Scheduler is stopped")
        if self._paused:
            self._pending.append({"task_id": task_id, "coro_factory": coro_factory, "kwargs": kwargs})
            return None

        async with self.semaphore:
            self._running[task_id] = asyncio.current_task()
            try:
                result = await coro_factory(**kwargs)
                return result
            finally:
                self._running.pop(task_id, None)

    def pause(self):
        self._paused = True
        self.resume_event.clear()
        logger.info("Scheduler paused")

    def resume(self):
        self._paused = False
        self.resume_event.set()
        pending = list(self._pending)
        self._pending.clear()
        logger.info(f"Scheduler resumed with {len(pending)} pending tasks")
        return pending

    def stop(self):
        self._stopped = True
        self._paused = False
        self.resume_event.set()
        for tid, task in self._running.items():
            task.cancel()
        self._running.clear()
        self._pending.clear()
        logger.info("Scheduler stopped")

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_stopped(self) -> bool:
        return self._stopped

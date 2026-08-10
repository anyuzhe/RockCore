"""Regression tests for read-only analysis tasks and dependency failures."""

import asyncio
import subprocess
import sys
from types import SimpleNamespace

from app.ui.status_constants import STATUS_STYLE
from orchestrator.engine import Engine
from orchestrator.scheduler import Scheduler
from orchestrator.state_machine import JobState


def test_status_metadata_can_load_without_qt_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from app.ui.status_constants import STATUS_STYLE; "
                "assert STATUS_STYLE['blocked']['text'] == '已阻塞'; "
                "assert 'PyQt6' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


class _AnalysisWorker:
    def scoped_to(self, _project_root):
        return self

    async def run(self, _task, **_kwargs):
        return {
            "status": "completed",
            "content": "The project is empty and has no existing technical stack.",
            "turns": 2,
        }


class _NoChangeCodingWorker:
    def scoped_to(self, _project_root):
        return self

    async def run(self, _task, **_kwargs):
        return {"status": "completed", "content": "No changes made.", "turns": 0}


class _ConditionalNoChangeCodingWorker:
    def scoped_to(self, _project_root):
        return self

    async def run(self, _task, **_kwargs):
        return {
            "status": "completed",
            "content": "检查完成，未发现需要修复的问题。",
            "no_changes": True,
            "turns": 1,
        }


def test_analysis_report_succeeds_without_file_changes(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project_root = tmp_path / "project"
        project_root.mkdir()
        try:
            project = repos["project"].create("Empty", str(project_root))
            job = repos["job"].create("JOB-ANALYSIS", project.id, "Inspect project")
            task = repos["task"].create(
                "T001",
                job.id,
                "Inspect project structure",
                task_type="analysis",
                allowed_paths=["*"],
            )
            engine.register_agent("worker", _AnalysisWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job,
                repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            assert task.status == "done"
            done_events = engine.event_bus.get_history("task_done")
            assert done_events[-1]["data"]["result"]["output"].startswith(
                "The project is empty"
            )
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_conditional_coding_task_succeeds_without_file_changes(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project_root = tmp_path / "project"
        project_root.mkdir()
        try:
            project = repos["project"].create("Empty", str(project_root))
            job = repos["job"].create("JOB-CONDITIONAL", project.id, "Audit project")
            task = repos["task"].create(
                "T001",
                job.id,
                "修复现有问题（如有问题）",
                description="仅当发现问题时修复；若未发现则跳过。",
                task_type="coding",
                allowed_paths=["*"],
            )
            engine.register_agent("worker", _ConditionalNoChangeCodingWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job,
                repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            assert task.status == "done"
            assert not engine.event_bus.get_history("task_failed")
            done_result = engine.event_bus.get_history("task_done")[-1]["data"]["result"]
            assert done_result["no_changes"] is True
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_scheduler_marks_transitive_dependents_as_blocked():
    async def scenario():
        scheduler = Scheduler(max_concurrent=1)
        executed = []

        async def runner(task_id, _task_data):
            executed.append(task_id)
            raise RuntimeError("upstream failed")

        results = await scheduler.run_dag(
            [
                {"task_id": "T001", "dependencies": []},
                {"task_id": "T002", "dependencies": ["T001"]},
                {"task_id": "T003", "dependencies": ["T002"]},
            ],
            runner,
        )

        assert executed == ["T001"]
        assert results["T002"]["status"] == "blocked"
        assert results["T002"]["blocked_by"] == ["T001"]
        assert results["T003"]["status"] == "blocked"
        assert results["T003"]["blocked_by"] == ["T002"]
        assert scheduler._completed == set()
        assert scheduler._failed == {"T001", "T002", "T003"}

    asyncio.run(scenario())


def test_execution_summary_does_not_count_blocked_tasks_as_done(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project_root = tmp_path / "project"
        project_root.mkdir()
        try:
            project = repos["project"].create("Empty", str(project_root))
            job = repos["job"].create("JOB-BLOCKED", project.id, "Build app")
            first = repos["task"].create(
                "T001", job.id, "Create app", task_type="coding"
            )
            second = repos["task"].create(
                "T002", job.id, "Add behavior", dependencies=["T001"], order=1
            )
            third = repos["task"].create(
                "T003", job.id, "Test app", task_type="testing",
                dependencies=["T002"], order=2,
            )
            engine.register_agent("worker", _NoChangeCodingWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job,
                repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            for task in (first, second, third):
                repos["_session"].refresh(task)
            assert first.status == "failed"
            assert second.status == "blocked"
            assert third.status == "blocked"

            summary = engine.event_bus.get_history("phase_summary")[-1]["data"]
            assert summary["details"] == {"done": 0, "failed": 1, "blocked": 2}
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_blocked_tasks_have_a_distinct_user_facing_status():
    assert STATUS_STYLE["blocked"]["text"] == "已阻塞"


def test_read_only_report_budget_leaves_a_final_report_turn(tmp_path):
    (tmp_path / "game.js").write_text("const state = 'playing';\n" * 450)
    (tmp_path / "index.html").write_text("<button id='restart'>Restart</button>\n" * 180)
    task = SimpleNamespace(
        task_type="analysis",
        allowed_paths=["game.js", "index.html"],
        dependencies=[],
        description="审核游戏结束与重新开始流程",
    )

    budget = Engine._estimate_task_budget(
        task, str(tmp_path), base_turns=8, base_exploration=4, mode="fast"
    )

    assert budget["max_turns"] == 12
    assert budget["exploration_turns"] == 4

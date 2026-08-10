"""Regression coverage for file-grounded plans and analysis data flow."""

import asyncio
from pathlib import Path

from memory.context_manager import ContextManager
from memory.repo_map import RepoMap
from orchestrator.engine import Engine
from orchestrator.state_machine import JobState


def test_static_project_repository_map_is_loaded_and_lists_real_files(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<h1>IG</h1>", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "site.test.js").write_text("// tests", encoding="utf-8")

    repo_map = RepoMap(str(tmp_path))
    repo_map.update()

    assert repo_map.is_loaded
    summary = repo_map.get_context_summary()
    assert "site/index.html [markup]" in summary
    assert "tests/site.test.js [source]" in summary

    context = ContextManager(str(tmp_path)).get_full_context()
    assert "site/index.html" in context


def test_same_project_context_refreshes_files_added_by_previous_job(tmp_path):
    context = ContextManager(str(tmp_path))
    asyncio.run(context.initialize())
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<h1>IG</h1>", encoding="utf-8")

    asyncio.run(context.switch_project(str(tmp_path)))

    assert "site/index.html" in context.get_full_context()


class _AnalysisAwareWorker:
    max_turns = 16
    max_exploration_turns = 4
    context_manager = None

    def __init__(self, root: Path):
        self.root = root
        self.coding_allowed_paths = []
        self.coding_description = ""

    def scoped_to(self, _project_root: str):
        return self

    async def run(self, task, **_kwargs):
        if task.task_type == "analysis":
            return {
                "status": "completed",
                "content": (
                    "The project is a single-page static site. Championship data "
                    "and UI rendering both live in site/index.html."
                ),
            }
        self.coding_allowed_paths = list(task.allowed_paths or [])
        self.coding_description = task.description
        target = self.root / "site" / "index.html"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n<section>IG matches</section>",
            encoding="utf-8",
        )
        return {"status": "completed", "content": "Updated the IG match section."}


def test_analysis_report_refines_dependent_task_paths_before_execution(tmp_path):
    async def scenario():
        project_root = tmp_path / "project"
        (project_root / "site").mkdir(parents=True)
        (project_root / "site" / "index.html").write_text(
            "<h1>Champions</h1>", encoding="utf-8"
        )
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Static Site", str(project_root))
            job_info = await engine.create_job(
                project.id, "Show IG match details", str(project_root)
            )
            job = repos["job"].get_by_id(job_info["job_id"])
            repos["constitution"].create(
                job_id=job.id,
                goal="Show IG match details",
                constraints=[],
                acceptance_criteria=[],
                protected_paths=[],
            )
            repos["task"].create(
                task_id="T001", job_id=job.id, title="Locate UI",
                task_type="analysis", description="Find the real UI file",
                allowed_paths=["**/*"], dependencies=[], order=0,
            )
            repos["task"].create(
                task_id="T002", job_id=job.id, title="Render IG matches",
                task_type="coding", description="Modify the identified UI",
                allowed_paths=["src/components/**/*", "pages/**/*"],
                dependencies=["T001"], order=1,
            )
            for state in (
                JobState.GOVERNING, JobState.GOVERNED, JobState.PLANNING,
                JobState.PLAN_CHECK, JobState.READY,
            ):
                engine.state_machine.transition(job.job_id, state)

            worker = _AnalysisAwareWorker(project_root)
            engine.register_agent("worker", worker)
            result = await engine._run_execution(
                job,
                repos,
                engine.test_manager.capture_snapshot(project_root),
            )

            assert result["status"] == "completed"
            assert worker.coding_allowed_paths == ["site/index.html"]
            assert "Verified prerequisite analysis" in worker.coding_description
            assert "site/index.html" in worker.coding_description
            stored = repos["task"].list_by_job(job.id)[1]
            assert stored.allowed_paths == ["site/index.html"]
            assert stored.status == "done"
            refined = engine.event_bus.get_history("task_refined")
            assert refined[-1]["data"]["paths_changed"] is True
        finally:
            repos["_session"].close()

    asyncio.run(scenario())

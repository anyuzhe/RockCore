"""Regression coverage for terminal output verification."""

import asyncio

from orchestrator.engine import Engine
from orchestrator.state_machine import JobState


def test_project_output_discovery_includes_nested_files_and_ignores_metadata(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<h1>done</h1>", encoding="utf-8")
    (tmp_path / ".ai").mkdir()
    (tmp_path / ".ai" / "repository_map.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".DS_Store").write_bytes(b"")

    assert Engine._project_output_files(str(tmp_path)) == ["site/index.html"]


def test_finalize_does_not_overwrite_reviewed_done_status_for_empty_analysis_project(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        project_root = tmp_path / "project"
        project_root.mkdir()
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(project_root))
            job = repos["job"].create("JOB-1", project.id, "分析现有项目")
            repos["job"].update_status(job.job_id, "done")
            engine.state_machine._states[job.job_id] = JobState.DONE

            await engine._finalize(job, repos)

            repos["_session"].refresh(job)
            assert job.status == "done"
            assert engine.state_machine.get_state(job.job_id) == JobState.DONE
            finished = engine.event_bus.get_history("job_finished")
            assert finished[-1]["data"]["status"] == "done"
        finally:
            repos["_session"].close()

    asyncio.run(scenario())

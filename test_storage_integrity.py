"""Regression tests for persisted task output ownership."""

from datetime import datetime, timezone

from orchestrator.engine import Engine
from storage.models import TestRun as StoredTestRun


def test_reused_task_primary_key_does_not_inherit_stale_test_runs(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        job = repos["job"].create("JOB-1", project.id, "创建页面")
        repos["test_run"].session.add(StoredTestRun(
            task_id=1,
            command="test stale.html",
            status="failed",
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        ))
        repos["test_run"].session.commit()

        task = repos["task"].create("T001", job.id, "创建新页面")

        assert task.id == 1
        assert repos["test_run"].list_by_task(task.id) == []
    finally:
        repos["_session"].close()

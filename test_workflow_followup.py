"""Regression tests for explicit follow-up workflow and task isolation."""

import asyncio

from orchestrator.engine import Engine


def test_explicit_followup_persists_and_builds_context(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            source = await engine.create_job(
                project.id, "创建结果页", str(tmp_path)
            )
            repos["job"].update_status(source["job_id"], "done")
            repos["task"].create(
                "T001", source["pk"], "创建页面", allowed_paths=["result.html"]
            )

            followup = await engine.create_job(
                project.id,
                "颜色换成蓝色",
                str(tmp_path),
                source_job_id=source["job_id"],
            )
            followup_job = repos["job"].get_by_id(followup["job_id"])
            assert followup_job.source_job_id == source["job_id"]

            context = engine._build_continuation_context(followup_job, repos)
            assert "创建结果页" in context
            assert "result.html" in context
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_followup_inherits_conversation_workflow_override(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        source = await engine.create_job(
            project.id, "原需求", str(tmp_path), workflow_override={
                "mode": "strict",
                "main_agent": {"provider": "kimi", "model": "kimi-k3"},
            },
        )
        followup = await engine.create_job(
            project.id, "继续修改", str(tmp_path),
            source_job_id=source["job_id"],
        )
        repos = engine._get_repos()
        try:
            job = repos["job"].get_by_id(followup["job_id"])
            override = (job.last_checkpoint or {}).get("workflow_override")
            assert override["mode"] == "strict"
            assert override["main_agent"]["model"] == "kimi-k3"
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_followup_source_must_belong_to_project(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            first = repos["project"].create("First", str(tmp_path / "first"))
            second = repos["project"].create("Second", str(tmp_path / "second"))
            source = await engine.create_job(first.id, "原需求", str(tmp_path))

            try:
                await engine.create_job(
                    second.id, "后续需求", str(tmp_path), source_job_id=source["job_id"]
                )
            except ValueError as error:
                assert "source job" in str(error)
            else:
                raise AssertionError("Cross-project follow-up should be rejected")
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_followup_language_does_not_implicitly_select_history(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        old_job = repos["job"].create("JOB-1", project.id, "创建页面")
        repos["job"].update_status(old_job.job_id, "done")
        new_job = repos["job"].create("JOB-2", project.id, "再把颜色改一下")
        assert engine._build_continuation_context(new_job, repos) == ""
    finally:
        repos["_session"].close()


def test_task_status_updates_are_scoped_by_database_id(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        first = repos["job"].create("JOB-1", project.id, "first")
        second = repos["job"].create("JOB-2", project.id, "second")
        first_task = repos["task"].create("T001", first.id, "first task")
        second_task = repos["task"].create("T001", second.id, "second task")

        repos["task"].update_status_by_pk(second_task.id, "running")
        repos["_session"].refresh(first_task)
        repos["_session"].refresh(second_task)
        assert first_task.status == "pending"
        assert second_task.status == "running"
    finally:
        repos["_session"].close()


def test_scheduler_can_run_again_after_stop():
    async def scenario():
        from orchestrator.scheduler import Scheduler

        scheduler = Scheduler(max_concurrent=1)
        scheduler.stop()

        async def runner(task_id, task_data):
            return task_id

        results = await scheduler.run_dag(
            [{"task_id": "T001", "dependencies": []}], runner
        )
        assert results == {"T001": "T001"}
        assert not scheduler.is_stopped

    asyncio.run(scenario())

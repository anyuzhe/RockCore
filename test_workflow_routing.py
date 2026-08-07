"""Regression tests for explicit workflow routing."""

import asyncio

from orchestrator.agent_config import (
    ProjectAgentConfig,
    load_project_config,
    save_project_config,
)
from orchestrator.engine import Engine
from orchestrator.state_machine import JobState


def test_auto_mode_keeps_full_workflow_for_simple_requests(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        result = await engine.create_job(project.id, "创建一个简单 HTML 页面", str(tmp_path))

        calls = []

        async def record(name):
            calls.append(name)

        async def governor(job, repos, config=None):
            await record("governor")

        async def planner(job, repos, config=None):
            await record("planner")

        async def execution(job, repos, baseline=None, **_kwargs):
            await record("worker")

        async def reviewer(job, repos):
            await record("reviewer")

        async def simple(job, repos, config=None):
            await record("simple")

        async def finalize(job, repos):
            pass

        engine._run_governor = governor
        engine._run_planner = planner
        engine._run_execution = execution
        engine._run_reviewer = reviewer
        engine._run_simple = simple
        engine._finalize = finalize

        assert engine._classify_request("创建一个简单 HTML 页面") == "simple"
        await engine.run_job(result["job_id"], str(tmp_path))

        assert calls == ["governor", "planner", "worker", "reviewer"]

    asyncio.run(scenario())


def test_fast_mode_is_the_only_direct_worker_route(tmp_path):
    async def scenario():
        save_project_config(str(tmp_path), ProjectAgentConfig.fast_preset())
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        result = await engine.create_job(project.id, "创建页面", str(tmp_path))

        calls = []

        async def simple(job, repos, config=None):
            calls.append("simple")

        async def unexpected(*args, **kwargs):
            calls.append("unexpected")

        async def finalize(job, repos):
            pass

        engine._run_simple = simple
        engine._run_governor = unexpected
        engine._run_planner = unexpected
        engine._run_execution = unexpected
        engine._run_reviewer = unexpected
        engine._finalize = finalize

        await engine.run_job(result["job_id"], str(tmp_path))

        assert calls == ["simple"]

    asyncio.run(scenario())


def test_missing_planner_creates_an_executable_fallback_plan(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            result = await engine.create_job(project.id, "创建页面", str(tmp_path))
            job = repos["job"].get_by_id(result["job_id"])
            engine.state_machine.transition(job.job_id, JobState.GOVERNING)
            engine.state_machine.transition(job.job_id, JobState.GOVERNED)

            await engine._run_planner(job, repos)

            tasks = repos["task"].list_by_job(job.id)
            assert len(tasks) == 1
            assert tasks[0].task_id == "T001"
            assert tasks[0].status == "pending"
            assert repos["plan"].get_by_job(job.id).raw_output["tasks"]
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_named_modes_cannot_persist_contradictory_phase_flags(tmp_path):
    standard = ProjectAgentConfig.standard_preset()
    standard.governor.enabled = False
    save_project_config(str(tmp_path), standard)

    loaded = load_project_config(str(tmp_path))

    assert loaded.mode == "standard"
    assert loaded.governor.enabled
    assert loaded.planner.enabled
    assert loaded.reviewer.enabled

    loaded.mode = "custom"
    loaded.governor.enabled = False
    save_project_config(str(tmp_path), loaded)
    assert not load_project_config(str(tmp_path)).governor.enabled


def test_overlapping_task_paths_are_serialized():
    plan = {
        "tasks": [
            {"id": "T004", "allowed_paths": ["src/app.js"], "dependencies": ["T003"]},
            {
                "id": "T005",
                "allowed_paths": ["index.html", "styles.css", "src/app.js"],
                "dependencies": ["T002"],
            },
            {
                "id": "T006",
                "allowed_paths": ["src/**/*.js"],
                "dependencies": [],
            },
        ]
    }

    Engine._serialize_overlapping_tasks(plan)

    assert "T004" in plan["tasks"][1]["dependencies"]
    assert "T004" in plan["tasks"][2]["dependencies"]
    assert "T005" in plan["tasks"][2]["dependencies"]


def test_transitive_dependencies_are_pruned_after_serialization():
    plan = {
        "tasks": [
            {"id": "T001", "allowed_paths": ["game.js"], "dependencies": []},
            {"id": "T002", "allowed_paths": ["game.js"], "dependencies": []},
            {"id": "T003", "allowed_paths": ["game.js"], "dependencies": []},
            {"id": "T004", "allowed_paths": ["game.js"], "dependencies": []},
        ]
    }

    Engine._serialize_overlapping_tasks(plan)
    Engine._prune_transitive_dependencies(plan)

    assert [task["dependencies"] for task in plan["tasks"]] == [
        [], ["T001"], ["T002"], ["T003"]
    ]


def test_unavailable_reviewer_cannot_auto_pass_a_job(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            result = await engine.create_job(project.id, "创建页面", str(tmp_path))
            job = repos["job"].get_by_id(result["job_id"])
            for state in (
                JobState.GOVERNING, JobState.GOVERNED, JobState.PLANNING,
                JobState.PLAN_CHECK, JobState.READY, JobState.EXECUTING,
                JobState.TESTING,
            ):
                engine.state_machine.transition(job.job_id, state)

            class FailedReviewer:
                async def run(self, job):
                    raise RuntimeError("credentials unavailable")

            engine.register_agent("reviewer", FailedReviewer())
            await engine._run_reviewer(job, repos)

            repos["_session"].refresh(job)
            assert job.status == "failed"
            review = repos["review"].list_by_job(job.id)[0]
            assert review.result == "error"
            assert "credentials unavailable" in review.summary
        finally:
            repos["_session"].close()

    asyncio.run(scenario())

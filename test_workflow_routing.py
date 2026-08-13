"""Regression tests for explicit workflow routing."""

import asyncio
import json

import pytest

from agents.planner import PlannerAgent, PlannerOutputTruncatedError
from agents.governor import GovernorAgent
from orchestrator.agent_config import (
    ProjectAgentConfig,
    load_project_config,
    save_project_config,
)
from orchestrator.engine import Engine
from orchestrator.state_machine import JobState


def test_governor_returns_structured_risk_score_and_reasons():
    class Router:
        async def chat(self, *_args, **_kwargs):
            return {"content": json.dumps({
                "goal": "修正文案",
                "constraints": [],
                "acceptance_criteria": ["文字正确"],
                "risk": "low",
                "risk_score": 12,
                "risk_reasons": ["只修改静态文案，不影响程序行为"],
                "protected_paths": [],
                "requires_final_review": False,
            })}

    result = asyncio.run(GovernorAgent(Router()).run("删除页面中的错别字"))

    assert result["risk"] == "low"
    assert result["risk_score"] == 12
    assert result["risk_reasons"] == ["只修改静态文案，不影响程序行为"]


def test_auto_mode_routes_low_risk_request_directly_to_worker(tmp_path):
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

        async def governor(*_args, **_kwargs):
            await record("governor")
            return {
                "risk": "low", "risk_score": 20,
                "risk_reasons": ["仅修改静态页面"], "source": "governor",
            }

        async def planner(job, repos, config=None):
            await record("planner")

        async def execution(job, repos, baseline=None, **_kwargs):
            await record("worker")

        async def reviewer(job, repos, **_kwargs):
            await record("reviewer")

        async def simple(*_args, **_kwargs):
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

        assert calls == ["simple"]
        summaries = engine.event_bus.get_history("phase_summary")
        assert any(
            item["data"].get("phase") == "governor"
            and "确定性" in item["data"].get("summary", "")
            for item in summaries
        )

    asyncio.run(scenario())


def test_image_scope_promotes_broad_low_risk_work_to_planned_route(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        result = await engine.create_job(
            project.id,
            "请分析附加图片并完成图片中表达的需求。",
            str(tmp_path),
        )
        repos = engine._get_repos()
        try:
            job = repos["job"].get_by_id(result["job_id"])
            job.attachments = [{"name": "需求.png", "path": "managed.png"}]
            repos["_session"].commit()
        finally:
            repos["_session"].close()

        calls = []

        async def governor(job, repos, *_args, **_kwargs):
            calls.append("governor")
            repos["constitution"].create(
                job_id=job.id,
                goal="分析项目中的所有 PDF，提炼精华并导出 PDF",
                constraints=[],
                acceptance_criteria=["生成最终 PDF"],
                risk="low",
                protected_paths=[],
                requires_final_review=True,
                raw_output={
                    "image_observations": [
                        "分析目录里的 PDF 文件并提炼精华内容",
                        "将结果导出为 PDF",
                    ]
                },
            )
            return {
                "risk": "low",
                "risk_score": 20,
                "risk_reasons": ["只读分析并生成新文档"],
                "source": "governor",
            }

        async def planner(*_args, **_kwargs):
            calls.append("planner")

        async def execution(*_args, **_kwargs):
            calls.append("worker")

        async def skip_review(*_args, **_kwargs):
            calls.append("skip-review")

        async def simple(*_args, **_kwargs):
            calls.append("simple")

        async def finalize(*_args, **_kwargs):
            pass

        engine._run_governor = governor
        engine._run_planner = planner
        engine._run_execution = execution
        engine._skip_review = skip_review
        engine._run_simple = simple
        engine._finalize = finalize

        await engine.run_job(result["job_id"], str(tmp_path))

        # With no Codex provider registered, the model Main Agent degrades to
        # the legacy vision-capable Governor before continuing the plan.
        assert calls == ["governor", "planner", "worker", "skip-review"]

    asyncio.run(scenario())


def test_auto_mode_routes_medium_risk_through_planner_without_reviewer(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        result = await engine.create_job(
            project.id, "修复 dashboard.py 的数据加载错误", str(tmp_path)
        )
        calls = []

        async def governor(*_args, **_kwargs):
            calls.append("governor")
            return {
                "risk": "medium", "risk_score": 45,
                "risk_reasons": ["局部功能修复"], "source": "governor",
            }

        async def planner(*_args, **_kwargs):
            calls.append("planner")

        async def execution(*_args, **_kwargs):
            calls.append("worker")

        async def skip_review(*_args, **_kwargs):
            calls.append("skip-reviewer")

        async def finalize(*_args, **_kwargs):
            pass

        engine._run_governor = governor
        engine._run_planner = planner
        engine._run_execution = execution
        engine._skip_review = skip_review
        engine._finalize = finalize
        await engine.run_job(result["job_id"], str(tmp_path))

        assert calls == ["planner", "worker", "skip-reviewer"]

    asyncio.run(scenario())


def test_auto_mode_routes_high_risk_request_through_full_pipeline(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        result = await engine.create_job(
            project.id, "修改数据库认证迁移和登录安全策略", str(tmp_path)
        )
        calls = []

        async def record(name):
            calls.append(name)

        async def governor(*_args, **_kwargs):
            await record("governor")
            return {
                "risk": "high", "risk_score": 85,
                "risk_reasons": ["认证和数据库迁移"], "source": "governor",
            }

        async def planner(*_args, **_kwargs):
            await record("planner")

        async def execution(*_args, **_kwargs):
            await record("worker")

        async def reviewer(*_args, **_kwargs):
            await record("reviewer")

        async def finalize(*_args, **_kwargs):
            pass

        engine._run_governor = governor
        engine._run_planner = planner
        engine._run_execution = execution
        engine._run_reviewer = reviewer
        engine._finalize = finalize
        await engine.run_job(result["job_id"], str(tmp_path))

        assert calls == ["governor", "planner", "worker", "reviewer"]

    asyncio.run(scenario())


def test_governor_assessment_overrides_the_rule_precheck_in_auto_mode(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        result = await engine.create_job(
            project.id, "修改数据库认证迁移和登录安全策略", str(tmp_path)
        )
        calls = []

        async def governor(*_args, **_kwargs):
            calls.append("governor")
            return {
                "risk": "low", "risk_score": 25,
                "risk_reasons": ["裁决者确认只改说明文本"],
                "source": "governor",
            }

        async def simple(*_args, **_kwargs):
            calls.append("simple")

        async def finalize(*_args, **_kwargs):
            pass

        engine._run_governor = governor
        engine._run_simple = simple
        engine._finalize = finalize
        await engine.run_job(result["job_id"], str(tmp_path))

        assert calls == ["governor", "simple"]
        repos = engine._get_repos()
        try:
            job = repos["job"].get_by_id(result["job_id"])
            assert job.risk_level == "low"
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_rule_precheck_is_used_only_when_governor_is_unavailable(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        result = await engine.create_job(
            project.id, "修改数据库认证迁移和登录安全策略", str(tmp_path)
        )
        calls = []

        async def planner(*_args, **_kwargs):
            calls.append("planner")

        async def execution(*_args, **_kwargs):
            calls.append("worker")

        async def reviewer(*_args, **_kwargs):
            calls.append("reviewer")

        async def finalize(*_args, **_kwargs):
            pass

        engine._run_planner = planner
        engine._run_execution = execution
        engine._run_reviewer = reviewer
        engine._finalize = finalize
        await engine.run_job(result["job_id"], str(tmp_path))

        assert calls == ["planner", "worker", "reviewer"]
        event = engine.event_bus.get_history("governor_risk_assessed")[-1]["data"]
        assert event["source"] == "rules_fallback"
        assert event["risk_level"] == "high"

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

        async def simple(*_args, **_kwargs):
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


def test_planner_rejects_a_provider_truncated_response():
    class Router:
        async def chat(self, *_args, **_kwargs):
            return {
                "content": '{"summary":"plan","tasks":[',
                "finish_reason": "length",
                "usage": {"output_tokens": 8192},
            }

    job = type("Job", (), {
        "job_id": "JOB-TRUNCATED",
        "user_request": "Build a page",
        "attachments": [],
    })()

    with pytest.raises(
        PlannerOutputTruncatedError, match="8192 tokens"
    ):
        asyncio.run(PlannerAgent(Router()).run(job))


def test_truncated_plan_pauses_before_tasks_are_created(tmp_path):
    class TruncatedPlanner:
        async def run(self, *_args, **_kwargs):
            raise PlannerOutputTruncatedError(
                "策划者输出被服务端截断，未生成完整计划"
            )

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            created = await engine.create_job(
                project.id, "创建一个完整页面", str(tmp_path)
            )
            job = repos["job"].get_by_id(created["job_id"])
            repos["constitution"].create(
                job_id=job.id, goal=job.user_request,
                constraints=[], acceptance_criteria=[], protected_paths=[],
            )
            engine.register_agent("planner", TruncatedPlanner())
            engine.state_machine.transition(job.job_id, JobState.GOVERNING)
            engine.state_machine.transition(job.job_id, JobState.GOVERNED)

            await engine._run_planner(job, repos)
            repos["_session"].refresh(job)

            assert job.status == "interrupted"
            assert repos["task"].list_by_job(job.id) == []
            paused = engine.event_bus.get_history("job_interrupted")[-1]["data"]
            assert paused["failure_stage"] == "planner_output_truncated"
            assert job.last_checkpoint["phase"] == "planner"
            summary = engine.event_bus.get_history("phase_summary")[-1]["data"]
            assert summary["status"] == "interrupted"
            assert "截断" in summary["summary"]
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_structurally_invalid_plan_falls_back_to_direct_execution(tmp_path):
    class InvalidPlanner:
        async def run(self, *_args, **_kwargs):
            return {
                "summary": "invalid dangling dependency",
                "tasks": [{
                    "id": "T002",
                    "title": "Implement page",
                    "type": "coding",
                    "description": "依据 T001 报告完成页面",
                    "dependencies": ["T001"],
                    "allowed_paths": ["index.html"],
                    "acceptance_command": "",
                }],
            }

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            created = await engine.create_job(
                project.id, "创建一个页面", str(tmp_path)
            )
            job = repos["job"].get_by_id(created["job_id"])
            repos["constitution"].create(
                job_id=job.id, goal=job.user_request,
                constraints=[], acceptance_criteria=[],
                protected_paths=[],
            )
            engine.register_agent("planner", InvalidPlanner())
            engine.state_machine.transition(job.job_id, JobState.GOVERNING)
            engine.state_machine.transition(job.job_id, JobState.GOVERNED)

            await engine._run_planner(job, repos)

            tasks = repos["task"].list_by_job(job.id)
            assert len(tasks) == 1
            assert tasks[0].task_id == "T001"
            assert tasks[0].dependencies == []
            assert engine.event_bus.get_history("plan_recovered")
            assert not engine.event_bus.get_history("plan_rejected")
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
            assert job.status == "needs_attention"
            review = repos["review"].list_by_job(job.id)[0]
            assert review.result == "error"
            assert "credentials unavailable" in review.summary
        finally:
            repos["_session"].close()

    asyncio.run(scenario())

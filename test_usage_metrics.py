"""Regression coverage for persisted per-task model usage metrics."""

import asyncio
from pathlib import Path

from orchestrator.cost_engine import CostEngine
from orchestrator.engine import Engine


def test_cost_estimate_uses_provider_rate():
    deepseek = CostEngine.estimate_cost("worker", 1000, 1000, provider="deepseek")
    kimi = CostEngine.estimate_cost("worker", 1000, 1000, provider="kimi")

    assert deepseek == 0.0025
    assert kimi == 0.01


def test_model_chat_usage_is_persisted_for_job_and_task(tmp_path: Path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project = repos["project"].create("Usage Demo", str(tmp_path))
        job = repos["job"].create("JOB-USAGE", project.id, "demo")
        task = repos["task"].create("T001", job.id, "code")
        repos["_session"].close()

        await engine.event_bus.publish(
            "model_chat",
            job_id="JOB-USAGE",
            task_id="T001",
            agent_type="worker",
            provider="deepseek",
            model_name="deepseek-v4-flash",
            input_tokens=1200,
            output_tokens=300,
            estimated_cost=0.0012,
            error=None,
        )

        repos = engine._get_repos()
        stored_job = repos["job"].get_by_id("JOB-USAGE")
        runs = repos["agent_run"].list_by_task(task.id)
        try:
            assert stored_job.usage_input_tokens == 1200
            assert stored_job.usage_output_tokens == 300
            assert stored_job.usage_calls == 1
            assert stored_job.usage_cost == 0.0012
            assert len(runs) == 1
            assert runs[0].model_name == "deepseek-v4-flash"
            assert runs[0].input_tokens == 1200
            assert runs[0].output_tokens == 300
            assert runs[0].cost == 0.0012
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_usage_event_uses_a_separate_session_from_job_lifecycle(tmp_path: Path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project = repos["project"].create("Session Demo", str(tmp_path))
        job = repos["job"].create("JOB-SESSION", project.id, "demo")

        try:
            await engine.event_bus.publish(
                "model_chat",
                job_id=job.job_id,
                agent_type="governor",
                provider="codex",
                input_tokens=10,
                output_tokens=5,
                estimated_cost=0.001,
            )
            # The event handler must not close or detach this lifecycle object.
            repos["_session"].refresh(job)
            assert job.usage_calls == 1
        finally:
            repos["_session"].close()

    asyncio.run(scenario())

"""Regression coverage for persisted per-task model usage metrics."""

import asyncio
import sqlite3
from pathlib import Path

from orchestrator.cost_engine import CostEngine, JobBudget
from orchestrator.event_bus import EventBus
from orchestrator.model_router import ModelRouter
from storage.database import init_database
from orchestrator.engine import Engine


def test_cost_estimate_uses_provider_rate():
    deepseek = CostEngine.estimate_cost("worker", 1000, 1000, provider="deepseek")
    kimi = CostEngine.estimate_cost("worker", 1000, 1000, provider="kimi")

    assert deepseek == 0.0025
    assert kimi == 0.01


def test_chatgpt_login_has_equivalent_cost_but_no_billable_api_cost():
    async def scenario():
        engine = CostEngine(JobBudget(max_cost_usd=0.01))
        await engine.record_usage(
            "JOB-CHATGPT",
            "reviewer",
            input_tokens=100_000,
            provider="codex",
            billing_mode="chatgpt_cli",
        )

        ok, message = await engine.check_budget("JOB-CHATGPT")
        usage = engine.get_usage_summary("JOB-CHATGPT")

        assert ok, message
        assert usage["equivalent_cost"] == 0.5
        assert usage["billable_cost"] == 0.0

    asyncio.run(scenario())


def test_platform_api_cost_still_enforces_dollar_budget():
    async def scenario():
        engine = CostEngine(JobBudget(max_cost_usd=0.01))
        await engine.record_usage(
            "JOB-PLATFORM",
            "reviewer",
            input_tokens=3_000,
            provider="codex",
            billing_mode="platform_api",
        )

        ok, message = await engine.check_budget("JOB-PLATFORM")

        assert not ok
        assert "Billable API cost exceeded" in message

    asyncio.run(scenario())


def test_router_propagates_chatgpt_billing_mode_to_usage_events():
    class ChatGPTProvider:
        model = "codex-sdk"
        authentication_mode = "chatgpt_cli"

        async def chat(self, *_args, **_kwargs):
            return {
                "content": "ok",
                "usage": {"input_tokens": 100_000, "output_tokens": 1_000},
            }

    async def scenario():
        event_bus = EventBus()
        router = ModelRouter(
            cost_engine=CostEngine(JobBudget(max_cost_usd=0.01)),
            provider_map={"reviewer": "codex"},
            event_bus=event_bus,
        )
        router.register_provider("codex", ChatGPTProvider())
        router.set_job_id("JOB-ROUTER-CHATGPT")

        # The first call has a large API-price-equivalent value. A second call
        # must still be allowed because the transport is the ChatGPT CLI.
        await router.chat("reviewer", "system", [])
        await router.chat("reviewer", "system", [])

        events = event_bus.get_history("model_chat")
        assert len(events) == 2
        assert events[-1]["data"]["billing_mode"] == "chatgpt_cli"
        assert events[-1]["data"]["estimated_cost"] > 0.5
        assert events[-1]["data"]["billable_cost"] == 0.0

    asyncio.run(scenario())


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
            billable_cost=0.0012,
            billing_mode="api",
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
            assert stored_job.usage_billable_cost == 0.0012
            assert len(runs) == 1
            assert runs[0].model_name == "deepseek-v4-flash"
            assert runs[0].input_tokens == 1200
            assert runs[0].output_tokens == 300
            assert runs[0].cost == 0.0012
            assert runs[0].billable_cost == 0.0012
            assert runs[0].billing_mode == "api"
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_chatgpt_usage_persists_equivalent_and_billable_cost_separately(tmp_path: Path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project = repos["project"].create("ChatGPT Usage", str(tmp_path))
        repos["job"].create("JOB-CHATGPT-USAGE", project.id, "demo")
        repos["_session"].close()

        await engine.event_bus.publish(
            "model_chat",
            job_id="JOB-CHATGPT-USAGE",
            agent_type="reviewer",
            provider="codex",
            input_tokens=100_000,
            output_tokens=1_000,
            estimated_cost=1.03,
            billable_cost=0.0,
            billing_mode="chatgpt_cli",
        )

        repos = engine._get_repos()
        try:
            job = repos["job"].get_by_id("JOB-CHATGPT-USAGE")
            assert job.usage_cost == 1.03
            assert job.usage_billable_cost == 0.0
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_legacy_database_keeps_old_cost_classification_unknown(tmp_path: Path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jobs ("
            "id INTEGER PRIMARY KEY, job_id VARCHAR(64), "
            "project_id INTEGER, user_request TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs (id, job_id, project_id, user_request) "
            "VALUES (1, 'JOB-LEGACY', 1, 'demo')"
        )
        connection.execute(
            "CREATE TABLE agent_runs ("
            "id INTEGER PRIMARY KEY, task_id INTEGER, agent_type VARCHAR(32))"
        )
        connection.execute(
            "INSERT INTO agent_runs (id, task_id, agent_type) "
            "VALUES (1, 1, 'worker')"
        )

    migrated = init_database(str(database))
    with migrated.connect() as connection:
        job_row = connection.exec_driver_sql(
            "SELECT usage_billable_cost FROM jobs WHERE id = 1"
        ).one()
        run_row = connection.exec_driver_sql(
            "SELECT billable_cost, billing_mode FROM agent_runs WHERE id = 1"
        ).one()

    assert job_row.usage_billable_cost is None
    assert run_row.billable_cost is None
    assert run_row.billing_mode == "unclassified"


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


def test_repeated_task_ids_are_scoped_to_the_current_job(tmp_path: Path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project = repos["project"].create("Scoped Usage", str(tmp_path))
        first_job = repos["job"].create("JOB-FIRST", project.id, "first")
        second_job = repos["job"].create("JOB-SECOND", project.id, "second")
        first_task = repos["task"].create("T001", first_job.id, "first task")
        second_task = repos["task"].create("T001", second_job.id, "second task")
        repos["_session"].close()

        await engine.event_bus.publish(
            "model_chat",
            job_id="JOB-SECOND",
            task_id="T001",
            agent_type="worker",
            provider="deepseek",
            input_tokens=7,
            output_tokens=3,
            estimated_cost=0.001,
        )

        repos = engine._get_repos()
        try:
            assert repos["agent_run"].list_by_task(first_task.id) == []
            second_runs = repos["agent_run"].list_by_task(second_task.id)
            assert len(second_runs) == 1
            assert second_runs[0].input_tokens == 7
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_review_and_repair_budget_reservations_are_idempotent():
    engine = CostEngine()

    review = engine.reserve_review_budget("JOB-RESERVE", 0)
    repeated_review = engine.reserve_review_budget("JOB-RESERVE", 0)
    repair = engine.reserve_repair_budget("JOB-RESERVE", 1)
    repeated_repair = engine.reserve_repair_budget("JOB-RESERVE", 1)

    assert review is repeated_review is repair is repeated_repair
    assert repair.max_input_tokens == 950_000
    assert repair.max_output_tokens == 250_000
    assert repair.max_api_calls == 155
    assert repair.max_cost_usd == 1.25

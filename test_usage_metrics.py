"""Regression coverage for persisted per-task model usage metrics."""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from providers.base import BaseProvider
from orchestrator.cost_engine import BudgetExceededError, CostEngine, JobBudget
from orchestrator.event_bus import EventBus
from orchestrator.model_router import ModelRouter
from storage.database import init_database
from orchestrator.engine import Engine


def test_cost_estimate_uses_model_specific_rmb_rate():
    assert CostEngine.estimate_cost(
        "worker", 1_000_000, 1_000_000,
        provider="deepseek", model_name="deepseek-v4-flash",
    ) == 3.0
    assert CostEngine.estimate_cost(
        "worker", 1_000_000, 1_000_000,
        provider="deepseek", model_name="deepseek-v4-pro",
    ) == 9.0
    assert CostEngine.estimate_cost(
        "planner", 1_000_000, 1_000_000,
        provider="kimi", model_name="kimi-k2.6",
    ) == 33.5
    assert CostEngine.estimate_cost(
        "planner", 1_000_000, 1_000_000,
        provider="kimi", model_name="kimi-k2.7-code",
    ) == 33.5
    assert CostEngine.estimate_cost(
        "planner", 1_000_000, 1_000_000,
        provider="kimi", model_name="kimi-k3",
    ) == 120.0


def test_cached_input_uses_the_discounted_rmb_rate():
    flash = CostEngine.estimate_cost(
        "worker", 1_000_000, 0,
        provider="deepseek", model_name="deepseek-v4-flash-0731",
        cached_input_tokens=1_000_000,
    )
    pro = CostEngine.estimate_cost(
        "worker", 1_000_000, 0,
        provider="deepseek", model_name="deepseek-v4-pro",
        cached_input_tokens=1_000_000,
    )
    k27 = CostEngine.estimate_cost(
        "worker", 1_000_000, 0,
        provider="kimi", model_name="kimi-k2.7-code",
        cached_input_tokens=1_000_000,
    )
    k3 = CostEngine.estimate_cost(
        "planner", 1_000_000, 0,
        provider="kimi", model_name="kimi-k3",
        cached_input_tokens=1_000_000,
    )

    assert flash == 0.02
    assert pro == 0.025
    assert k27 == 1.30
    assert k3 == 2.0


def test_provider_usage_normalization_reads_cache_hit_tokens():
    standard = BaseProvider.normalize_usage(SimpleNamespace(
        prompt_tokens=1_000,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=800),
    ))
    deepseek = BaseProvider.normalize_usage(SimpleNamespace(
        prompt_tokens=1_000,
        completion_tokens=20,
        prompt_cache_hit_tokens=750,
        prompt_cache_miss_tokens=250,
    ))

    assert standard == {
        "input_tokens": 1_000,
        "cached_input_tokens": 800,
        "output_tokens": 20,
    }
    assert deepseek["cached_input_tokens"] == 750


def test_legacy_usd_budget_limit_is_converted_to_rmb():
    budget = CostEngine.budget_from_config({"max_cost_usd": 0.50})

    assert budget.max_cost_cny == 3.60


def test_chatgpt_login_has_equivalent_cost_but_no_billable_api_cost():
    async def scenario():
        engine = CostEngine(JobBudget(max_cost_cny=0.10))
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
        assert usage["equivalent_cost"] == 3.6
        assert usage["billable_cost"] == 0.0
        assert usage["currency"] == "CNY"

    asyncio.run(scenario())


def test_platform_api_cost_still_enforces_rmb_budget():
    async def scenario():
        engine = CostEngine(JobBudget(max_cost_cny=0.10))
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
            cost_engine=CostEngine(JobBudget(max_cost_cny=0.10)),
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


def test_router_prices_the_requested_fallback_model_not_provider_default():
    class KimiProvider:
        model = "kimi-k3"
        authentication_mode = "api"

        async def chat(self, *_args, **_kwargs):
            return {
                "content": "ok",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
            }

    async def scenario():
        event_bus = EventBus()
        router = ModelRouter(event_bus=event_bus)
        router.register_provider("kimi", KimiProvider())
        router.set_job_id("JOB-K27")

        await router.chat(
            "worker", "system", [], provider_override="kimi",
            model="kimi-k2.7-code",
        )

        event = event_bus.get_history("model_chat")[-1]["data"]
        assert event["model_name"] == "kimi-k2.7-code"
        assert event["estimated_cost"] == 6.5
        assert event["cost_currency"] == "CNY"

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
            cached_input_tokens=200,
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
            assert stored_job.usage_cached_input_tokens == 200
            assert stored_job.usage_output_tokens == 300
            assert stored_job.usage_calls == 1
            assert stored_job.usage_cost == 0.0012
            assert stored_job.usage_billable_cost == 0.0012
            assert stored_job.usage_cost_currency == "CNY"
            assert len(runs) == 1
            assert runs[0].model_name == "deepseek-v4-flash"
            assert runs[0].input_tokens == 1200
            assert runs[0].cached_input_tokens == 200
            assert runs[0].output_tokens == 300
            assert runs[0].cost == 0.0012
            assert runs[0].billable_cost == 0.0012
            assert runs[0].billing_mode == "api"
            assert runs[0].cost_currency == "CNY"
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
            "SELECT usage_billable_cost, usage_cost_currency, "
            "usage_cached_input_tokens FROM jobs WHERE id = 1"
        ).one()
        run_row = connection.exec_driver_sql(
            "SELECT billable_cost, billing_mode, cost_currency, "
            "cached_input_tokens FROM agent_runs WHERE id = 1"
        ).one()

    assert job_row.usage_billable_cost is None
    assert job_row.usage_cost_currency == "CNY"
    assert job_row.usage_cached_input_tokens == 0
    assert run_row.billable_cost is None
    assert run_row.billing_mode == "unclassified"
    assert run_row.cost_currency == "CNY"
    assert run_row.cached_input_tokens == 0


def test_legacy_usd_cost_rows_are_converted_to_rmb_once(tmp_path: Path):
    database = tmp_path / "legacy-cost.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jobs ("
            "id INTEGER PRIMARY KEY, job_id VARCHAR(64), "
            "project_id INTEGER, user_request TEXT, "
            "usage_cost FLOAT, usage_billable_cost FLOAT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES "
            "(1, 'JOB-LEGACY-COST', 1, 'demo', 2.0, 0.5)"
        )
        connection.execute(
            "CREATE TABLE agent_runs ("
            "id INTEGER PRIMARY KEY, task_id INTEGER, agent_type VARCHAR(32), "
            "cost FLOAT, billable_cost FLOAT)"
        )
        connection.execute(
            "INSERT INTO agent_runs VALUES (1, 1, 'worker', 1.0, 0.25)"
        )

    migrated = init_database(str(database))
    with migrated.connect() as connection:
        job_row = connection.exec_driver_sql(
            "SELECT usage_cost, usage_billable_cost, usage_cost_currency "
            "FROM jobs WHERE id = 1"
        ).one()
        run_row = connection.exec_driver_sql(
            "SELECT cost, billable_cost, cost_currency "
            "FROM agent_runs WHERE id = 1"
        ).one()

    assert job_row == (14.4, 3.6, "CNY")
    assert run_row == (7.2, 1.8, "CNY")


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
    assert repair.max_input_tokens == 5_450_000
    assert repair.max_output_tokens == 5_150_000
    assert repair.max_api_calls == 555
    assert repair.max_cost_cny == 10.0


def test_cached_input_uses_weighted_soft_budget_accounting():
    async def scenario():
        engine = CostEngine(JobBudget(cached_input_weight=0.15))
        await engine.record_usage(
            "JOB-CACHE", "worker",
            input_tokens=100_000,
            cached_input_tokens=80_000,
            output_tokens=1_000,
        )

        task = engine.get_task_usage("JOB-CACHE", "")
        summary = engine.get_usage_summary("JOB-CACHE")

        assert task["effective_input_tokens"] == 32_000
        assert summary["effective_input"] == 32_000
        assert summary["total_input"] == 100_000

    asyncio.run(scenario())


def test_atomic_request_reservation_protects_hard_cost_limit():
    async def scenario():
        engine = CostEngine(JobBudget(max_cost_cny=0.01))
        first = await engine.admit_request(
            "JOB-ATOMIC",
            estimated_input_tokens=1_000,
            max_output_tokens=1_000,
            estimated_billable_cost=0.006,
        )

        try:
            await engine.admit_request(
                "JOB-ATOMIC",
                estimated_input_tokens=1_000,
                max_output_tokens=1_000,
                estimated_billable_cost=0.006,
            )
        except Exception as error:
            assert "hard cost limit" in str(error).lower()
        else:
            raise AssertionError("second reservation should exceed hard cost")
        snapshot = engine.get_budget_snapshot("JOB-ATOMIC")
        assert snapshot["reserved_tokens"] == 2_000
        assert snapshot["hard_cost_limit_cny"] == 0.01
        await engine.release_request(
            "JOB-ATOMIC", first["reservation_id"]
        )

    asyncio.run(scenario())


def test_router_enforces_hard_cost_before_calling_paid_provider():
    class Provider:
        model = "deepseek-v4-flash"
        authentication_mode = "api"
        calls = 0

        async def chat(self, *_args, **_kwargs):
            self.calls += 1
            return {"content": "should not run", "usage": {}}

    async def scenario():
        events = EventBus()
        router = ModelRouter(
            cost_engine=CostEngine(JobBudget(max_cost_cny=0.0001)),
            provider_map={"worker": "deepseek"},
            event_bus=events,
        )
        provider = Provider()
        router.register_provider("deepseek", provider)
        router.set_job_id("JOB-HARD-COST")

        try:
            await router.chat("worker", "system", [])
        except BudgetExceededError as error:
            assert "hard cost limit" in str(error).lower()
        else:
            raise AssertionError("hard cost limit should stop provider call")

        assert provider.calls == 0
        assert events.get_history("budget_continuation_required")

    asyncio.run(scenario())


def test_workflow_budget_exposes_and_releases_protected_phase_capacity():
    engine = CostEngine()

    budget = engine.reserve_workflow_budget(
        "JOB-WORKFLOW",
        task_input_tokens=1_000_000,
        required_api_calls=100,
        required_output_tokens=200_000,
    )
    reserved = engine.get_budget_snapshot("JOB-WORKFLOW")
    engine.release_workflow_reservations("JOB-WORKFLOW")
    released = engine.get_budget_snapshot("JOB-WORKFLOW")

    assert reserved["protected_phase_tokens"] == 1_350_000
    assert reserved["protected_phase_calls"] == 80
    assert released["reserved_tokens"] == 0
    assert released["reserved_calls"] == 0
    assert budget.max_cost_cny == 10.0


def test_resumed_budget_restores_only_usage_missing_from_live_memory():
    async def scenario():
        engine = CostEngine()
        await engine.record_usage(
            "JOB-RESUMED", "worker",
            input_tokens=100_000,
            cached_input_tokens=20_000,
            output_tokens=10_000,
            provider="deepseek",
            model_name="deepseek-v4-pro",
        )
        engine.restore_persisted_usage(
            "JOB-RESUMED",
            input_tokens=160_000,
            cached_input_tokens=30_000,
            output_tokens=15_000,
            calls=2,
            billable_cost=0.34,
        )
        snapshot = engine.get_budget_snapshot("JOB-RESUMED")

        assert snapshot["used_effective_input_tokens"] == 134_500
        assert snapshot["used_output_tokens"] == 15_000
        assert snapshot["used_calls"] == 2
        assert snapshot["billable_cost"] == 0.34

    asyncio.run(scenario())

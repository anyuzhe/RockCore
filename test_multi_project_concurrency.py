"""Regression coverage for concurrent, project-isolated Job runtimes."""

import asyncio
from types import SimpleNamespace

from memory.context_manager import ContextManager
from orchestrator.engine import Engine
from tools.tool_broker import ToolBroker


def test_two_projects_run_concurrently_without_runtime_or_file_cross_talk(tmp_path):
    async def scenario():
        first_root = tmp_path / "first"
        second_root = tmp_path / "second"
        base_root = tmp_path / "base"
        for root in (first_root, second_root, base_root):
            root.mkdir()

        engine = Engine(db_path=str(tmp_path / "studio.db"))
        engine.tool_broker = ToolBroker(base_root, engine.policy_engine)
        base_context = ContextManager(str(base_root))
        await base_context.initialize()
        engine.register_agent("worker", SimpleNamespace(
            model_router=engine.model_router,
            tool_broker=engine.tool_broker,
            context_manager=base_context,
            skill_manager=None,
        ))

        entered = set()
        both_entered = asyncio.Event()
        release = asyncio.Event()
        captures = {}

        async def isolated_pipeline(job_id, project_root):
            engine.model_router.set_job_id(job_id)
            runtime = engine._active_runtime()
            worker = engine.get_agent("worker")
            captures[job_id] = {
                "scheduler": id(engine.scheduler),
                "merge_manager": id(engine.merge_manager),
                "tool_broker": id(engine.tool_broker),
                "tool_root": str(engine.tool_broker.project_root),
                "context": str(worker.context_manager.project_root),
                "router_before": engine.model_router._current_job_id,
            }
            result = await engine.tool_broker.file_tools.write_file(
                "job.txt", job_id
            )
            assert result["status"] == "written"
            await engine.event_bus.publish("runtime_probe")
            entered.add(job_id)
            if len(entered) == 2:
                both_entered.set()
            await release.wait()
            captures[job_id]["router_after"] = (
                engine.model_router._current_job_id
            )
            captures[job_id]["runtime_job"] = runtime.job_id
            return {"status": "done"}

        engine._run_job_pipeline = isolated_pipeline
        first = asyncio.create_task(engine.run_job("JOB-FIRST", str(first_root)))
        second = asyncio.create_task(engine.run_job("JOB-SECOND", str(second_root)))
        await asyncio.wait_for(both_entered.wait(), timeout=2)

        assert set(engine._job_runtimes) == {"JOB-FIRST", "JOB-SECOND"}
        await engine.pause_job("JOB-FIRST")
        assert engine._job_runtimes["JOB-FIRST"].scheduler._paused is True
        assert engine._job_runtimes["JOB-SECOND"].scheduler._paused is False
        await engine.resume_job("JOB-FIRST")

        release.set()
        await asyncio.gather(first, second)

        assert captures["JOB-FIRST"]["scheduler"] != captures["JOB-SECOND"]["scheduler"]
        assert captures["JOB-FIRST"]["merge_manager"] != captures["JOB-SECOND"]["merge_manager"]
        assert captures["JOB-FIRST"]["tool_broker"] != captures["JOB-SECOND"]["tool_broker"]
        assert captures["JOB-FIRST"]["tool_root"] == str(first_root.resolve())
        assert captures["JOB-SECOND"]["tool_root"] == str(second_root.resolve())
        assert captures["JOB-FIRST"]["context"] == str(first_root.resolve())
        assert captures["JOB-SECOND"]["context"] == str(second_root.resolve())
        for job_id in ("JOB-FIRST", "JOB-SECOND"):
            assert captures[job_id]["router_before"] == job_id
            assert captures[job_id]["router_after"] == job_id
            assert captures[job_id]["runtime_job"] == job_id
        assert (first_root / "job.txt").read_text(encoding="utf-8") == "JOB-FIRST"
        assert (second_root / "job.txt").read_text(encoding="utf-8") == "JOB-SECOND"
        assert not engine._job_runtimes

        probe_jobs = {
            event["data"].get("job_id")
            for event in engine.event_bus.get_history("runtime_probe", limit=10)
        }
        assert probe_jobs == {"JOB-FIRST", "JOB-SECOND"}

    asyncio.run(scenario())


def test_jobs_for_the_same_project_are_serialized(tmp_path):
    async def scenario():
        project_root = tmp_path / "shared"
        project_root.mkdir()
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        active = 0
        maximum_active = 0

        async def isolated_pipeline(_job_id, _project_root):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            await asyncio.sleep(0.03)
            active -= 1
            return {"status": "done"}

        engine._run_job_pipeline = isolated_pipeline
        await asyncio.gather(
            engine.run_job("JOB-ONE", str(project_root)),
            engine.run_job("JOB-TWO", str(project_root)),
        )
        assert maximum_active == 1

    asyncio.run(scenario())

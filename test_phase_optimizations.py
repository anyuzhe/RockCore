"""Regression coverage for the phase-one/two workflow optimizations."""

import asyncio
from types import SimpleNamespace

from orchestrator.cost_engine import CostEngine
from orchestrator.engine import Engine
from orchestrator.model_router import ModelRouter
from orchestrator.test_manager import TestManager
from orchestrator.policy_engine import PolicyEngine
from orchestrator.state_machine import JobState
from tools.tool_broker import ToolBroker


def test_visible_total_token_budget_has_no_hidden_component_cap():
    budget = CostEngine.budget_from_config({
        "max_total_tokens": 1_000_000,
        "max_api_calls": 250,
        "max_cost_cny": 36,
    })

    assert budget.max_total_tokens == 1_000_000
    assert budget.max_input_tokens == 1_000_000
    assert budget.max_output_tokens == 1_000_000
    assert budget.max_api_calls == 250
    assert budget.max_auto_total_tokens == 50_000_000
    assert budget.max_auto_api_calls == 5_000
    assert budget.cached_input_weight == 0.15


def test_runtime_settings_update_scheduler_and_default_budget(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"), max_concurrent_workers=3)

    engine.apply_runtime_config({
        "max_concurrent_workers": 1,
        "budget": {
            "max_total_tokens": 800_000,
            "max_api_calls": 77,
            "max_cost_cny": 21.6,
        },
        "agent_provider_map": {"worker": "kimi"},
    })

    budget = engine.model_router.cost_engine.get_budget("future-job")
    assert engine.scheduler.max_concurrent == 1
    assert budget.max_input_tokens == 800_000
    assert budget.max_api_calls == 77
    assert engine.model_router._provider_map["worker"] == "kimi"


def test_code_task_budget_scales_with_turns_and_keeps_finish_reserve(tmp_path):
    task = SimpleNamespace(
        task_type="coding", allowed_paths=[], dependencies=[],
        description="实现多个行为并完成测试；更新界面；处理错误；验证结果。",
    )

    budget = Engine._estimate_task_budget(
        task, str(tmp_path), base_turns=36,
        base_exploration=8, mode="auto",
    )

    assert budget["input_budget"] > 320_000
    assert budget["finalization_reserve"] >= 180_000
    assert budget["input_budget"] == (
        budget["processing_input_budget"] + budget["finalization_reserve"]
    )
    assert budget["max_auto_input_budget"] == 50_000_000


def test_simple_plan_collapses_analysis_and_overlapping_coding():
    plan = {
        "summary": "Update one page",
        "tasks": [
            {
                "id": "T001", "type": "analysis", "title": "Locate page",
                "description": "Find the existing IG row.",
                "allowed_paths": ["site/index.html"], "dependencies": [],
            },
            {
                "id": "T002", "type": "coding", "title": "Update content",
                "description": "Add the exact score.",
                "allowed_paths": ["site/index.html"], "dependencies": ["T001"],
            },
            {
                "id": "T003", "type": "coding", "title": "Update style",
                "description": "Keep the score on one line.",
                "allowed_paths": ["site/index.html"], "dependencies": ["T002"],
            },
            {
                "id": "T004", "type": "testing", "title": "Validate HTML",
                "description": "Check structure and content.",
                "allowed_paths": ["site/index.html"], "dependencies": ["T003"],
            },
        ],
    }

    Engine._optimize_plan(plan, "simple")

    assert [task["id"] for task in plan["tasks"]] == ["T002", "T004"]
    assert "Find the existing IG row" in plan["tasks"][0]["description"]
    assert "Keep the score on one line" in plan["tasks"][0]["description"]
    assert plan["tasks"][1]["dependencies"] == ["T002"]


def test_simple_plan_rewrites_removed_task_references_and_read_only_clause():
    plan = {
        "summary": "Update one page after T001",
        "tasks": [
            {
                "id": "T001", "type": "analysis", "title": "Locate page",
                "description": (
                    "只读分析现有页面，产出书面分析报告，"
                    "不创建或修改任何项目文件。"
                ),
                "allowed_paths": ["site/index.html"], "dependencies": [],
            },
            {
                "id": "T002", "type": "coding", "title": "Update content",
                "description": "依据 T001 报告修改比分。",
                "allowed_paths": ["site/index.html"], "dependencies": ["T001"],
            },
            {
                "id": "T003", "type": "testing", "title": "Validate HTML",
                "description": "验证 T002 的文件结果。",
                "allowed_paths": ["site/index.html"], "dependencies": ["T002"],
            },
        ],
    }

    assert Engine._optimize_plan(plan, "simple")

    assert [task["id"] for task in plan["tasks"]] == ["T002", "T003"]
    coding = plan["tasks"][0]["description"]
    assert "T001" not in coding
    assert "不创建或修改任何项目文件" not in coding
    assert "上述前置检查结论" in coding
    assert PolicyEngine().check_task_plan(plan, {}) == []


def test_conservative_complexity_classifier_keeps_ambiguous_builds_planned(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))

    assert engine._classify_request("创建一个简单 HTML 页面") == "simple"
    assert engine._classify_request("修改支付页面的一处错别字") == "simple"
    assert engine._classify_request("开发一个网页版坦克大战小游戏") == "normal"
    assert engine._classify_request("创建一个订单管理应用") == "normal"
    assert engine._classify_request("搭建一个完整的分布式支付系统") == "complex"


def test_shared_runtime_tasks_merge_into_one_worker_conversation():
    plan = {
        "summary": "Implement game after T001 and finish T004",
        "tasks": [
            {
                "id": "T001", "type": "analysis", "title": "Inspect game",
                "description": "检查 game.js 和 index.html 的现有结构。",
                "allowed_paths": ["game.js", "index.html"], "dependencies": [],
                "acceptance_command": "",
            },
            {
                "id": "T002", "type": "coding", "title": "Build game core",
                "description": "依据 T001 结论实现地图、玩家和子弹。",
                "allowed_paths": ["game.js", "index.html"],
                "dependencies": ["T001"], "acceptance_command": "node --check game.js",
            },
            {
                "id": "T003", "type": "coding", "title": "Add enemy behavior",
                "description": "继续在 game.js 中实现敌人和碰撞。",
                "allowed_paths": ["game.js"], "dependencies": ["T002"],
                "acceptance_command": "node --check game.js",
            },
            {
                "id": "T004", "type": "coding", "title": "Wire game state",
                "description": "连接 game.js 与 index.html 的状态和重启界面。",
                "allowed_paths": ["game.js", "index.html"],
                "dependencies": ["T003"], "acceptance_command": "node --check game.js",
            },
            {
                "id": "T005", "type": "testing", "title": "Validate game",
                "description": "验证 T004 的最终结果。",
                "allowed_paths": ["game.js", "index.html"],
                "dependencies": ["T004"], "acceptance_command": "node --check game.js",
            },
        ],
    }

    assert Engine._merge_shared_context_tasks(
        plan, {"active_files": ["game.js", "index.html"]}
    )

    assert [task["id"] for task in plan["tasks"]] == ["T002", "T005"]
    implementation, validation = plan["tasks"]
    assert "内部步骤 1 · Inspect game" in implementation["description"]
    assert "内部步骤 4 · Wire game state" in implementation["description"]
    assert implementation["acceptance_command"] == "node --check game.js"
    assert validation["dependencies"] == ["T002"]
    assert "T004" not in validation["description"]
    assert PolicyEngine().check_task_plan(plan, {}) == []


def test_shared_support_file_does_not_merge_independent_runtime_tasks():
    plan = {
        "summary": "Implement independent services",
        "tasks": [
            {
                "id": "T001", "type": "coding", "title": "Backend",
                "description": "Implement backend API.",
                "allowed_paths": ["server/api.py", "package.json"],
                "dependencies": [], "acceptance_command": "pytest -q",
            },
            {
                "id": "T002", "type": "coding", "title": "Frontend",
                "description": "Implement independent frontend.",
                "allowed_paths": ["web/app.js", "package.json"],
                "dependencies": [], "acceptance_command": "npm test",
            },
        ],
    }

    assert not Engine._merge_shared_context_tasks(
        plan, {
            "active_files": ["server/api.py", "web/app.js"],
            "support_files": ["package.json"],
        }
    )
    assert [task["id"] for task in plan["tasks"]] == ["T001", "T002"]


def test_transitive_overlap_does_not_merge_conflicting_acceptance_commands():
    plan = {
        "summary": "Keep independently validated stages",
        "tasks": [
            {
                "id": "T001", "type": "coding", "title": "Python stage",
                "description": "Update shared runtime.",
                "allowed_paths": ["shared.json", "service.py"],
                "dependencies": [], "acceptance_command": "pytest -q",
            },
            {
                "id": "T002", "type": "coding", "title": "Shared wiring",
                "description": "Wire the shared runtime.",
                "allowed_paths": ["shared.json"],
                "dependencies": ["T001"], "acceptance_command": "",
            },
            {
                "id": "T003", "type": "coding", "title": "Node stage",
                "description": "Update the browser runtime.",
                "allowed_paths": ["shared.json", "app.js"],
                "dependencies": ["T002"], "acceptance_command": "npm test",
            },
        ],
    }

    assert Engine._merge_shared_context_tasks(
        plan, {"active_files": ["shared.json", "service.py", "app.js"]}
    )
    assert len(plan["tasks"]) == 2
    assert {task["acceptance_command"] for task in plan["tasks"]} == {
        "pytest -q", "npm test",
    }
    assert PolicyEngine().check_task_plan(plan, {}) == []


def test_broad_multi_feature_plan_is_promoted_and_not_collapsed():
    tasks = [{
        "id": "T001", "type": "analysis", "title": "Analyze project",
        "description": "只读分析项目并形成报告，不创建或修改任何项目文件。",
        "allowed_paths": ["**/*"], "dependencies": [],
    }]
    for number in range(2, 7):
        tasks.append({
            "id": f"T{number:03d}", "type": "coding",
            "title": f"Feature {number}",
            "description": (
                "依据 T001 报告创建游戏功能。" if number == 2
                else "继续实现一个独立游戏功能。"
            ),
            "allowed_paths": ["**/*.html", "**/*.css", "**/*.js"],
            "dependencies": [f"T{number - 1:03d}"],
        })
    plan = {"summary": "Build browser game", "tasks": tasks}
    original_ids = [task["id"] for task in tasks]

    complexity = Engine._promote_complexity_from_plan("simple", plan)
    collapsed = Engine._optimize_plan(plan, complexity)

    assert complexity == "complex"
    assert not collapsed
    assert [task["id"] for task in plan["tasks"]] == original_ids
    assert "依据 T001 报告" in plan["tasks"][1]["description"]


def test_plan_policy_rejects_dangling_references_and_dependency_cycles():
    dangling = {
        "tasks": [{
            "id": "T002", "type": "coding", "title": "Implement",
            "description": "依据 T001 报告修改页面。",
            "allowed_paths": ["index.html"], "dependencies": ["T001"],
        }]
    }
    cyclic = {
        "tasks": [
            {
                "id": "T001", "type": "coding", "title": "One",
                "description": "Modify one file.",
                "allowed_paths": ["one.py"], "dependencies": ["T002"],
            },
            {
                "id": "T002", "type": "coding", "title": "Two",
                "description": "Modify another file.",
                "allowed_paths": ["two.py"], "dependencies": ["T001"],
            },
        ]
    }

    dangling_errors = PolicyEngine().check_task_plan(dangling, {})
    cycle_errors = PolicyEngine().check_task_plan(cyclic, {})

    assert any("dependency references missing task 'T001'" in error
               for error in dangling_errors)
    assert any("description references missing task 'T001'" in error
               for error in dangling_errors)
    assert any("dependency cycle" in error for error in cycle_errors)


def test_repair_plan_renames_prose_references_but_not_command_paths():
    plan = {
        "summary": "Repair after T001 report",
        "tasks": [
            {
                "id": "T001", "type": "analysis", "title": "Inspect issue",
                "description": "Find the exact cause.",
                "allowed_paths": ["src/app.py"], "dependencies": [],
            },
            {
                "id": "T002", "type": "coding", "title": "Fix issue",
                "description": "依据 T001 报告修改实现。",
                "allowed_paths": ["src/app.py"], "dependencies": ["T001"],
                "acceptance_command": "pytest tests/test_T001_regression.py",
            },
        ],
    }

    namespaced = Engine._namespace_repair_plan(plan, 2)

    assert [task["id"] for task in namespaced["tasks"]] == [
        "R02T001", "R02T002",
    ]
    assert namespaced["tasks"][1]["dependencies"] == ["R02T001"]
    assert "R02T001" in namespaced["tasks"][1]["description"]
    assert namespaced["tasks"][1]["acceptance_command"] == (
        "pytest tests/test_T001_regression.py"
    )
    assert PolicyEngine().check_task_plan(namespaced, {}) == []


def test_validation_only_testing_task_stays_local(tmp_path):
    baseline = TestManager.capture_snapshot(tmp_path)
    (tmp_path / "index.html").write_text(
        "<!doctype html><html><body><h1>ok</h1></body></html>",
        encoding="utf-8",
    )
    task = SimpleNamespace(
        task_type="testing", title="Validate HTML", description="Check structure",
        acceptance_command="", allowed_paths=["index.html"],
    )

    assert TestManager.should_validate_locally(task)
    assert not TestManager.is_test_authoring_task(task)
    assert TestManager.snapshot_diff(tmp_path, baseline)["added"] == ["index.html"]


def test_test_authoring_task_still_uses_worker():
    task = SimpleNamespace(
        task_type="testing", title="新增测试用例", description="补充边界测试",
        acceptance_command="pytest -q", allowed_paths=["tests/test_site.py"],
    )

    assert TestManager.is_test_authoring_task(task)
    assert not TestManager.should_validate_locally(task)


def test_tool_schema_is_pruned_by_task_type(tmp_path):
    broker = ToolBroker(tmp_path, PolicyEngine())
    analysis = {
        item["function"]["name"]
        for item in broker.get_tool_definitions("analysis")
    }
    coding = {
        item["function"]["name"]
        for item in broker.get_tool_definitions("coding")
    }

    assert "write_file" not in analysis
    assert "run_tests" not in analysis
    assert {"read_file", "search_code", "git_diff"}.issubset(analysis)
    assert {"write_file", "apply_patch", "run_tests"}.issubset(coding)


def test_tool_broker_ignores_provider_metadata_arguments(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("old", encoding="utf-8")
    broker = ToolBroker(tmp_path, PolicyEngine())
    task = SimpleNamespace(
        task_type="coding", allowed_paths=["note.txt"],
        title="Update note", description="", acceptance_command="",
    )

    result = asyncio.run(broker.execute(task, "apply_patch", {
        "path": "note.txt",
        "search": "old",
        "replace": "new",
        "note": "provider-only narration",
    }))

    assert result["status"] == "patched"
    assert result["ignored_arguments"] == ["note"]
    assert source.read_text(encoding="utf-8") == "new"


def test_provider_capability_error_immediately_falls_back():
    class BrokenProvider:
        model = "thinking-model"

        async def chat_with_tools(self, *_args, **_kwargs):
            raise RuntimeError("Thinking mode does not support this tool_choice")

    class WorkingProvider:
        model = "tool-model"

        async def chat_with_tools(self, *_args, **_kwargs):
            return {
                "content": "recovered", "tool_calls": [],
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }

    async def scenario():
        router = ModelRouter(
            provider_map={"worker": "deepseek"},
        )
        router.register_provider("deepseek", BrokenProvider())
        router.register_provider("kimi", WorkingProvider())

        response = await router.chat_with_tools(
            "worker", "system", [], [],
            task=SimpleNamespace(task_id="T001", task_type="coding"),
        )

        assert response["content"] == "recovered"
        assert router._circuit_is_open("deepseek")

    asyncio.run(scenario())


def test_project_routing_overrides_global_route_and_model():
    class Provider:
        model = "default"

        def __init__(self):
            self.models = []
            self.reasoning = []

        async def chat(self, *_args, **kwargs):
            self.models.append(kwargs.get("model"))
            self.reasoning.append(kwargs.get("reasoning_effort"))
            return {"content": "ok", "usage": {}}

    async def scenario():
        router = ModelRouter(
            provider_map={"planner": "deepseek"},
        )
        deepseek = Provider()
        kimi = Provider()
        router.register_provider("deepseek", deepseek)
        router.register_provider("kimi", kimi)
        router.set_job_id("JOB-1")
        router.set_job_routing(
            "JOB-1", {"planner": "kimi"}, {"planner": "kimi-project"},
            {"planner": "high"},
        )

        await router.chat("planner", "system", [])

        assert deepseek.models == []
        assert kimi.models == ["kimi-project"]
        assert kimi.reasoning == ["high"]

    asyncio.run(scenario())


def test_start_marks_stale_job_as_resumable_interruption(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            repos["job"].create("JOB-STALE", project.id, "unfinished")
        finally:
            repos["_session"].close()

        await engine.start()
        repos = engine._get_repos()
        try:
            job = repos["job"].get_by_id("JOB-STALE")
            assert job.status == "interrupted"
            assert job.failure_code == "process_interrupted"
            assert "继续" in job.recovery_hint
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_continuation_includes_task_failure_checkpoint(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        source = repos["job"].create("JOB-1", project.id, "build page")
        task = repos["task"].create(
            "T001", source.id, "Edit page", allowed_paths=["index.html"]
        )
        repos["task"].update_status_by_pk(task.id, "failed")
        repos["task"].update_result(
            task.id, summary="partial markup saved",
            failure_reason="validation failed",
        )
        repos["job"].update_status(source.job_id, "failed")
        repos["job"].set_failure(
            source.job_id, "validation_failed", "duplicate id",
            "fix the reported HTML issue",
        )
        followup = repos["job"].create(
            "JOB-2", project.id, "continue", source_job_id=source.job_id
        )

        context = engine._build_continuation_context(followup, repos)

        assert "T001 [failed]" in context
        assert "partial markup saved" in context
        assert "duplicate id" in context
        assert "Do NOT repeat tasks marked done" in context
    finally:
        repos["_session"].close()


def test_review_failure_without_user_action_is_failed(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            job = repos["job"].create("JOB-1", project.id, "change page")
            task = repos["task"].create("T001", job.id, "Edit page")
            repos["task"].update_status_by_pk(task.id, "done")
            engine.state_machine._states[job.job_id] = JobState.REVIEWING

            await engine._finish_review_failure(
                job, repos,
                {"issues": [{"problem": "one issue"}]},
                "The final check found one repairable issue.",
                status="rejected",
            )

            repos["_session"].refresh(job)
            assert job.status == "failed"
            assert job.failure_reason.startswith("The final check")
            assert engine.state_machine.get_state(job.job_id) == JobState.FAILED
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_early_pipeline_failure_still_emits_authoritative_finished_event(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        created = await engine.create_job(project.id, "change page", str(tmp_path))

        async def govern(job, repos, _config=None, **_kwargs):
            repos["constitution"].create(
                job_id=job.id, goal=job.user_request, constraints=[],
                acceptance_criteria=[], protected_paths=[],
            )
            engine.state_machine.transition(job.job_id, JobState.GOVERNING)
            engine.state_machine.transition(job.job_id, JobState.GOVERNED)
            return {
                "risk": "medium", "risk_score": 45,
                "risk_reasons": ["局部代码修改"], "source": "governor",
            }

        async def fail_plan(job, repos, _config=None):
            engine.state_machine.transition(job.job_id, JobState.PLANNING)
            repos["job"].update_status(job.job_id, "failed")
            repos["job"].set_failure(
                job.job_id, "plan_rejected", "unsafe plan", "adjust paths"
            )
            engine.state_machine.transition(job.job_id, JobState.FAILED)

        engine._run_governor = govern
        engine._run_planner = fail_plan

        await engine.run_job(created["job_id"], str(tmp_path))

        finished = engine.event_bus.get_history("job_finished")
        assert finished[-1]["data"] == {
            "job_id": created["job_id"], "status": "failed"
        }

    asyncio.run(scenario())

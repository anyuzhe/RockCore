"""Regression coverage for the Codex-style execution architecture."""

import asyncio
import json
from types import SimpleNamespace

from agents.worker import WorkerAgent
from memory.context_manager import ContextManager
from memory.instruction_resolver import InstructionResolver
from orchestrator.engine import Engine
from orchestrator.execution_session import (
    new_session,
    record_substep,
    render_fixed_context,
    update_checklist,
)
from orchestrator.main_agent import MainAgent


def test_layered_instructions_use_override_and_specificity(tmp_path):
    codex_home = tmp_path / "codex-home"
    project = tmp_path / "project"
    nested = project / "services" / "api"
    codex_home.mkdir()
    nested.mkdir(parents=True)
    (codex_home / "AGENTS.md").write_text("global-base", encoding="utf-8")
    (codex_home / "AGENTS.override.md").write_text(
        "global-override", encoding="utf-8"
    )
    (project / "AGENTS.md").write_text("project-base", encoding="utf-8")
    (nested / "AGENTS.md").write_text("nested-base", encoding="utf-8")
    (nested / "AGENTS.override.md").write_text(
        "nested-override", encoding="utf-8"
    )

    sources = InstructionResolver(
        project, codex_home=codex_home
    ).resolve(nested)

    assert [source.content for source in sources] == [
        "global-override", "project-base", "nested-override",
    ]


def test_instruction_limit_is_applied_to_combined_chain(tmp_path):
    codex_home = tmp_path / "codex-home"
    project = tmp_path / "project"
    nested = project / "nested"
    codex_home.mkdir()
    nested.mkdir(parents=True)
    (codex_home / "AGENTS.md").write_text("g" * 700, encoding="utf-8")
    (project / "AGENTS.md").write_text("p" * 700, encoding="utf-8")
    (nested / "AGENTS.md").write_text("n" * 700, encoding="utf-8")

    sources = InstructionResolver(
        project, max_bytes=1024, codex_home=codex_home
    ).resolve(nested)

    assert sum(len(source.content.encode("utf-8")) for source in sources) <= 1024
    assert sources[0].content == "g" * 700


def test_task_context_keeps_fixed_session_and_nested_instructions(tmp_path):
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("root-rule", encoding="utf-8")
    (nested / "AGENTS.md").write_text("feature-rule", encoding="utf-8")
    task = SimpleNamespace(
        task_id="T001",
        task_type="coding",
        allowed_paths=["src/feature/app.py"],
        _rockcore_fixed_context="FIXED-GOAL-" + "x" * 6000,
        _rockcore_project_surface={},
    )

    context = asyncio.run(ContextManager(str(tmp_path)).build_task_context(task))

    assert "FIXED-GOAL-" in context
    assert "root-rule" in context
    assert "feature-rule" in context


def test_execution_session_checklist_and_fixed_context_are_structured():
    session = new_session("SESSION-1", "Ship the feature")
    tasks = [
        SimpleNamespace(
            task_id="T001", title="Inspect", status="done",
            result_summary="entrypoint found",
        ),
        SimpleNamespace(
            task_id="T002", title="Implement", status="running",
            result_summary="",
        ),
    ]

    update_checklist(session, tasks)
    rendered = render_fixed_context(session)

    assert session["current_step"] == "T002"
    assert session["checklist"][0]["summary"] == "entrypoint found"
    assert "Ship the feature" in rendered
    assert '"id": "T002"' in rendered


def test_followup_job_inherits_execution_session(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        first = await engine.create_job(project.id, "Build page", str(tmp_path))
        second = await engine.create_job(
            project.id, "Make the title smaller", str(tmp_path),
            source_job_id=first["job_id"],
        )
        repos = engine._get_repos()
        try:
            root = repos["job"].get_by_id(first["job_id"])
            followup = repos["job"].get_by_id(second["job_id"])
            assert followup.execution_session_id == root.execution_session_id
            decisions = followup.last_checkpoint["execution_session"]["decisions"]
            assert decisions[-1]["kind"] == "follow_up"
            conversations = repos["conversation"].list_by_project(project.id)
            assert len(conversations) == 1
            assert len(repos["conversation"].list_turns(
                root.execution_session_id
            )) == 2
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_main_agent_uses_specialists_only_when_the_turn_needs_them():
    direct = MainAgent.decide_advisors(
        mode="auto", risk_route="low", complexity="simple",
        has_attachments=False, governor_enabled=True,
        planner_enabled=True, reviewer_enabled=True,
    )
    complex_turn = MainAgent.decide_advisors(
        mode="auto", risk_route="high", complexity="complex",
        has_attachments=False, governor_enabled=True,
        planner_enabled=True, reviewer_enabled=True,
    )

    assert not direct.governor and not direct.planner and not direct.reviewer
    assert complex_turn.governor and complex_turn.planner and complex_turn.reviewer


def test_model_main_agent_persists_structured_turn_decision(tmp_path):
    class Router:
        async def chat(self, agent_type, *_args, **_kwargs):
            assert agent_type == "main_agent"
            return {"content": json.dumps({
                "goal": "修复登录流程",
                "constraints": ["不修改数据库结构"],
                "acceptance_criteria": ["登录测试通过"],
                "risk": "medium",
                "risk_score": 48,
                "risk_reasons": ["影响认证行为"],
                "execution_strategy": "planned",
                "use_planner": True,
                "use_reviewer": False,
                "summary": "将先确认登录入口，再进行局部修复",
                "next_action": "让策划顾问生成聚焦计划",
            })}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        engine.model_router = Router()
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            created = repos["job"].create(
                "JOB-1", project.id, "修复登录流程"
            )
            result = await engine.main_agent.assess_turn(
                created, repos, fallback_risk="low"
            )
            job = repos["job"].get_by_id("JOB-1")
            session = job.last_checkpoint["execution_session"]
            assert result["use_planner"] is True
            assert session["constraints"] == ["不修改数据库结构"]
            assert session["advisor_history"][-1]["role"] == "main_agent"
            assert job.last_checkpoint["main_agent_assessment"][
                "execution_strategy"
            ] == "planned"
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_model_main_agent_cannot_lower_deterministic_risk_floor():
    result = MainAgent._normalize_assessment(
        {
            "risk": "low", "risk_score": 10,
            "execution_strategy": "direct", "use_reviewer": False,
        },
        fallback_risk="high", user_request="修改认证数据库迁移",
    )

    assert result["risk"] == "high"
    assert result["risk_score"] >= 61
    assert result["use_reviewer"] is True


def test_model_main_agent_failure_emits_fallback_without_failing_job(tmp_path):
    class Router:
        async def chat(self, *_args, **_kwargs):
            raise RuntimeError("Codex login unavailable")

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        engine.model_router = Router()
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            job = repos["job"].create("JOB-1", project.id, "修改页面")
            result = await engine.main_agent.assess_turn(
                job, repos, fallback_risk="medium"
            )
            assert result is None
            assert job.status == "created"
            event = engine.event_bus.get_history("main_agent_fallback")[-1]
            assert "确定性流程" in event["data"]["summary"]
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_model_main_agent_decision_controls_engine_specialists(tmp_path):
    class Provider:
        authentication_mode = "chatgpt_cli"
        model = "gpt-5.6-sol"

        async def chat(self, *_args, **kwargs):
            assert kwargs.get("agent_type") == "main_agent"
            return {
                "content": json.dumps({
                    "goal": "重构认证服务",
                    "constraints": ["保持公开接口兼容"],
                    "acceptance_criteria": ["认证测试通过"],
                    "risk": "high", "risk_score": 82,
                    "risk_reasons": ["涉及认证和数据库迁移"],
                    "execution_strategy": "planned",
                    "use_planner": True, "use_reviewer": True,
                    "summary": "认证变更需要先规划并独立审核",
                    "next_action": "生成基于实际入口的计划",
                }),
                "finish_reason": "stop", "usage": {},
            }

        async def chat_with_tools(self, *_args, **_kwargs):
            raise AssertionError("Main Agent must not receive write tools")

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        engine.model_router.register_provider("codex", Provider())
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
        finally:
            repos["_session"].close()
        created = await engine.create_job(
            project.id, "重构认证数据库迁移和登录服务", str(tmp_path)
        )
        calls = []

        async def unexpected_governor(*_args, **_kwargs):
            calls.append("legacy-governor")

        async def planner(*_args, **_kwargs):
            calls.append("planner")

        async def execution(*_args, **_kwargs):
            calls.append("worker")

        async def reviewer(*_args, **_kwargs):
            calls.append("reviewer")

        async def finalize(*_args, **_kwargs):
            pass

        engine._run_governor = unexpected_governor
        engine._run_planner = planner
        engine._run_execution = execution
        engine._run_reviewer = reviewer
        engine._finalize = finalize
        await engine.run_job(created["job_id"], str(tmp_path))

        assert calls == ["planner", "worker", "reviewer"]
        repos = engine._get_repos()
        try:
            job = repos["job"].get_by_id(created["job_id"])
            constitution = repos["constitution"].get_by_job(job.id)
            assert constitution.raw_output["source"] == "main_agent"
            assert job.last_checkpoint["main_agent_assessment"][
                "use_reviewer"
            ] is True
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_main_agent_summary_persists_text_without_changing_status(tmp_path):
    class Router:
        async def chat(self, agent_type, *_args, **_kwargs):
            assert agent_type == "main_agent_summary"
            return {"content": json.dumps({
                "summary": "页面已完成并通过语法检查",
                "completed": ["页面创建完成"],
                "remaining": [], "next_action": "",
            })}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        engine.model_router = Router()
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(tmp_path))
            job = repos["job"].create("JOB-1", project.id, "创建页面")
            repos["job"].update_status(job.job_id, "done")
            repos["_session"].refresh(job)
            summary = await engine.main_agent.summarize_turn(job, repos)
            repos["_session"].refresh(job)
            assert summary == "页面已完成并通过语法检查"
            assert job.status == "done"
            turn = job.last_checkpoint["execution_session"]["turns"][-1]
            assert turn["summary"] == summary
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_temporary_substeps_are_part_of_fixed_context_not_database_tasks():
    session = new_session("SESSION-1", "修复页面")
    record_substep(
        session, parent_task_id="T001", key="inspect-listeners",
        title="检查重复监听器", status="done", summary="没有重复绑定",
    )

    assert session["substeps"][0]["id"] == "T001:inspect-listeners"
    assert "检查重复监听器" in render_fixed_context(session)


def test_parallelization_requires_disjoint_concrete_nonruntime_paths():
    independent = {"tasks": [
        {"id": "T001", "type": "coding", "allowed_paths": ["docs/a.md"], "dependencies": []},
        {"id": "T002", "type": "coding", "allowed_paths": ["tests/b.py"], "dependencies": []},
    ]}
    Engine._serialize_overlapping_tasks(independent)
    assert independent["tasks"][1]["dependencies"] == []

    shared_runtime = {"tasks": [
        {"id": "T001", "type": "coding", "allowed_paths": ["src/a.py"], "dependencies": []},
        {"id": "T002", "type": "coding", "allowed_paths": ["src/b.py"], "dependencies": []},
    ]}
    Engine._serialize_overlapping_tasks(
        shared_runtime, {"active_files": ["src/a.py", "src/b.py"]}
    )
    assert shared_runtime["tasks"][1]["dependencies"] == ["T001"]

    analysis_chain = {"tasks": [
        {"id": "T001", "type": "analysis", "allowed_paths": ["docs/a.md"], "dependencies": []},
        {"id": "T002", "type": "coding", "allowed_paths": ["src/b.py"], "dependencies": []},
    ]}
    Engine._serialize_overlapping_tasks(analysis_chain)
    assert analysis_chain["tasks"][1]["dependencies"] == ["T001"]


def test_user_fixable_and_transient_model_errors_are_resumable():
    assert Engine._is_user_action_required("model does not exist: kimi-x")
    assert Engine._is_user_action_required("maximum context length exceeded")
    assert Engine._is_transient_provider_error("Connection error")
    assert Engine._execution_failure_summary({
        "T001": {
            "status": "needs_user_action",
            "error": "Insufficient Balance",
        }
    })["terminal_status"] == "needs_attention"
    assert Engine._execution_failure_summary({
        "T001": {
            "status": "needs_continuation",
            "error": "provider timeout",
        }
    })["terminal_status"] == "interrupted"


def test_compaction_keeps_fixed_context_and_summarizes_old_reads():
    messages = [
        {"role": "user", "content": "FIXED-SESSION\n" + "x" * 5000},
        {
            "role": "assistant", "content": "old read",
            "tool_calls": [{
                "id": "old", "function": {
                    "name": "read_file",
                    "arguments": '{"path":"src/app.py"}',
                },
            }],
        },
        {"role": "tool", "tool_call_id": "old", "content": "source" * 1000},
        {"role": "assistant", "content": "final check", "tool_calls": []},
    ]

    compacted = WorkerAgent._compact_messages(messages, max_chars=3000)

    assert compacted[0]["content"].startswith("FIXED-SESSION")
    assert any(
        "src/app.py" in str(message.get("content") or "")
        for message in compacted
    )

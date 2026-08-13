"""Regression coverage for the Codex-style execution architecture."""

import asyncio
from types import SimpleNamespace

from agents.worker import WorkerAgent
from memory.context_manager import ContextManager
from memory.instruction_resolver import InstructionResolver
from orchestrator.engine import Engine
from orchestrator.execution_session import (
    new_session,
    render_fixed_context,
    update_checklist,
)


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
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


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

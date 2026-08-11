"""Regression tests for task-scoped intermediate artifact isolation."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from agents.worker import WorkerAgent
from orchestrator.policy_engine import PolicyEngine
from orchestrator.test_manager import TestManager
from tools.tool_broker import ToolBroker


def _document_task():
    return SimpleNamespace(
        id=1,
        task_id="T001",
        title="Summarize PDF",
        description="Extract the PDF and create summary.md",
        task_type="coding",
        allowed_paths=["source.pdf", "summary.md"],
        protected_paths=[],
        acceptance_command="",
        dependencies=[],
        skills=[],
        _rockcore_document_profile={"level": "short"},
    )


def test_document_intermediates_are_redirected_and_final_output_stays_visible(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "source.pdf").write_bytes(b"pdf")
    broker = ToolBroker(project, PolicyEngine())
    checkpoint = broker.configure_task_runtime(
        project, "JOB-001", "T001", final_outputs=["summary.md"],
        input_paths=["source.pdf"], require_declared_outputs=True,
    )
    baseline = TestManager.capture_snapshot(project)
    task = _document_task()

    intermediate = asyncio.run(broker.execute(task, "write_file", {
        "path": "working-material.txt",
        "content": "extracted page text",
    }))

    assert intermediate["status"] == "written"
    assert intermediate["redirected_to_runtime"] is True
    assert intermediate["scope"] == "task_runtime"
    assert not (project / "working-material.txt").exists()
    assert TestManager.snapshot_diff(project, baseline)["changed"] == []
    assert Path(checkpoint["path"]).name.startswith("T001-")
    assert broker.shell_tools._command_environment()["TEMP"] == checkpoint["path"]

    reread = asyncio.run(broker.execute(task, "read_file", {
        "path": "working-material.txt",
    }))
    assert reread["content"] == "extracted page text"
    assert reread["scope"] == "task_runtime"

    final = asyncio.run(broker.execute(task, "write_file", {
        "path": "summary.md",
        "content": "final summary",
        "purpose": "final",
    }))
    assert final["status"] == "written"
    assert "redirected_to_runtime" not in final
    assert (project / "summary.md").read_text(encoding="utf-8") == "final summary"

    protected = asyncio.run(broker.execute(task, "write_file", {
        "path": "source.pdf", "content": "overwrite", "purpose": "final",
    }))
    assert protected["status"] == "rejected"
    assert (project / "source.pdf").read_bytes() == b"pdf"

    cleanup = broker.cleanup_task_runtime()
    assert cleanup["status"] == "cleaned"
    assert not (project / ".ai" / "runtime").exists()
    assert (project / "summary.md").exists()


def test_temp_artifact_can_be_atomically_promoted_only_to_declared_output(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    broker = ToolBroker(project, PolicyEngine())
    broker.configure_task_runtime(
        project, "JOB-002", "T002", final_outputs=["result.txt"]
    )
    task = SimpleNamespace(
        task_id="T002",
        task_type="coding",
        allowed_paths=["result.txt"],
        protected_paths=[],
        _rockcore_document_profile={"level": "short"},
    )

    asyncio.run(broker.execute(task, "write_temp_file", {
        "path": "draft.txt", "content": "ready",
    }))
    rejected = asyncio.run(broker.execute(task, "promote_artifact", {
        "temp_path": "draft.txt", "target_path": "other.txt",
    }))
    promoted = asyncio.run(broker.execute(task, "promote_artifact", {
        "temp_path": "draft.txt", "target_path": "result.txt",
    }))

    assert rejected["status"] == "rejected"
    assert promoted["status"] == "promoted"
    assert (project / "result.txt").read_text(encoding="utf-8") == "ready"
    assert broker.runtime_tools.has_temp_file("draft.txt")


def test_legacy_root_page_text_is_relocated_and_continuation_restores_it(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    task = _document_task()
    first = ToolBroker(project, PolicyEngine())
    first.configure_task_runtime(
        project, "JOB-SOURCE", "T001", final_outputs=["summary.md"]
    )
    (project / "pages-01-08.txt").write_text("checkpoint", encoding="utf-8")

    moved = first.relocate_task_intermediates(["pages-01-08.txt"])

    assert moved == [{
        "from": "pages-01-08.txt",
        "to": "pages-01-08.txt",
        "scope": "task_runtime",
    }]
    assert not (project / "pages-01-08.txt").exists()
    assert first.runtime_tools.has_temp_file("pages-01-08.txt")

    continued = ToolBroker(project, PolicyEngine())
    state = continued.configure_task_runtime(
        project, "JOB-CONTINUE", "T001",
        final_outputs=["summary.md"], source_job_id="JOB-SOURCE",
    )
    restored = asyncio.run(continued.execute(task, "read_temp_file", {
        "path": "pages-01-08.txt",
    }))

    assert state["resumed_from"]
    assert restored["content"] == "checkpoint"


def test_runtime_tools_are_available_only_after_task_scope_is_configured(tmp_path):
    broker = ToolBroker(tmp_path, PolicyEngine())
    before = {
        item["function"]["name"]
        for item in broker.get_tool_definitions("coding")
    }
    broker.configure_task_runtime(tmp_path, "JOB-003", "T003")
    after = {
        item["function"]["name"]
        for item in broker.get_tool_definitions("coding")
    }

    assert "write_temp_file" not in before
    assert {
        "write_temp_file", "read_temp_file", "list_temp_files",
        "promote_artifact",
    }.issubset(after)

    task = _document_task()
    traversal = asyncio.run(broker.execute(task, "write_temp_file", {
        "path": "../outside.txt", "content": "blocked",
    }))
    windows_absolute = asyncio.run(broker.execute(task, "write_temp_file", {
        "path": r"C:\\Users\\Public\\outside.txt", "content": "blocked",
    }))
    assert traversal["status"] == "error"
    assert windows_absolute["status"] == "error"
    assert not (tmp_path.parent / "outside.txt").exists()


def test_intermediate_redirect_does_not_satisfy_required_final_write(tmp_path):
    class Router:
        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "Saving an extracted chunk.",
                    "tool_calls": [{
                        "id": "temp-write",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "page-001.txt", "content": "chunk",
                            }),
                        },
                    }],
                    "usage": {},
                }
            if self.calls == 2:
                return {"content": "Done.", "tool_calls": [], "usage": {}}
            if self.calls == 3:
                return {
                    "content": "Publishing the actual result.",
                    "tool_calls": [{
                        "id": "promote-final",
                        "function": {
                            "name": "promote_artifact",
                            "arguments": json.dumps({
                                "temp_path": "page-001.txt",
                                "target_path": "summary.md",
                            }),
                        },
                    }],
                    "usage": {},
                }
            return {
                "content": "Final artifact created.",
                "tool_calls": [],
                "usage": {},
            }

    project = tmp_path / "project"
    project.mkdir()
    broker = ToolBroker(project, PolicyEngine())
    broker.configure_task_runtime(
        project, "JOB-004", "T001", final_outputs=["summary.md"]
    )
    router = Router()
    worker = WorkerAgent(router, broker, max_turns=6)

    result = asyncio.run(worker.run(_document_task(), project_root=str(project)))

    assert result["status"] == "completed"
    assert router.calls == 4
    assert (project / "summary.md").read_text(encoding="utf-8") == "chunk"

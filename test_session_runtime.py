import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from orchestrator.failures import FailureCode, classify_provider_failure
from orchestrator.policy_engine import PolicyEngine
from orchestrator.runtime_services import WorkflowRuntimeServices
from orchestrator.session_events import AgentInbox, SessionEventStore
from tools.tool_broker import ToolBroker


def run(coro):
    return asyncio.run(coro)


def test_failure_taxonomy_distinguishes_user_action_and_failover():
    balance = classify_provider_failure("Error code: 402 Insufficient Balance")
    missing = classify_provider_failure("404 model not found")
    server = classify_provider_failure("HTTP 503 service unavailable")

    assert balance.code == FailureCode.USER_ACTION_REQUIRED
    assert balance.requires_user and not balance.switch_provider
    assert missing.code == FailureCode.MODEL_UNAVAILABLE
    assert missing.switch_model and missing.switch_provider
    assert server.code == FailureCode.PROVIDER_SERVER_ERROR
    assert server.retryable and not server.switch_provider


def test_event_log_is_append_only_and_replays_after_partial_tail(tmp_path):
    path = tmp_path / "job.jsonl"
    store = SessionEventStore(path, session_id="session-1", job_id="job-1")
    run(store.append("job_started", data={"status": "running"}, durable=True))
    run(store.append(
        "tool_call_prepared", task_id="T001",
        data={"tool": "write_file"}, durable=True,
    ))
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"seq":')

    recovered = SessionEventStore(
        path, session_id="session-1", job_id="job-1"
    )
    event = run(recovered.append("job_finished", data={"status": "done"}))

    assert event.seq == 3
    assert [item["seq"] for item in recovered.read()] == [1, 2, 3]


def test_runtime_replay_projection_is_deterministic_and_job_scoped(tmp_path):
    first = WorkflowRuntimeServices.create(str(tmp_path), "job-A")
    second = WorkflowRuntimeServices.create(str(tmp_path), "job-B")
    run(first.session.record("task_running", task_id="T001", status="running"))
    run(first.session.record("task_completed", task_id="T001", status="completed"))
    run(second.session.record("task_running", task_id="T009", status="running"))

    snapshot = first.replay_snapshot()
    repeated = first.replay_snapshot()

    assert snapshot == repeated
    assert snapshot["event_count"] == 2
    assert snapshot["ui"]["task_states"] == {"T001": "completed"}
    assert len(second.session.replay()) == 1
    assert first.session.path != second.session.path


def test_agent_inbox_is_checkpointable_and_drains_once():
    inbox = AgentInbox()
    inbox.enqueue("keep the existing patch", source="user")
    assert [item["content"] for item in inbox.drain()] == [
        "keep the existing patch"
    ]
    assert inbox.drain() == []
    restored = AgentInbox(inbox.checkpoint())
    restored.enqueue("run the focused test", source="recovery")
    assert [item["content"] for item in restored.drain()] == [
        "run the focused test"
    ]


def test_tool_broker_rejects_stale_observed_file_without_overprotection(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("one\n", encoding="utf-8")
    broker = ToolBroker(str(tmp_path), PolicyEngine())
    task = SimpleNamespace(
        task_id="T001", task_type="coding", allowed_paths=["*", "**/*"]
    )

    observed = run(broker.execute(task, "read_file", {"path": "sample.txt"}))
    assert observed["source_version"]
    target.write_text("external\n", encoding="utf-8")
    conflict = run(broker.execute(task, "apply_patch", {
        "path": "sample.txt", "search": "one", "replace": "two",
    }))
    created = run(broker.execute(task, "write_file", {
        "path": "new.txt", "content": "new\n",
    }))

    assert conflict["status"] == "conflict"
    assert conflict["error_code"] == "stale_file_version"
    assert target.read_text(encoding="utf-8") == "external\n"
    assert created["status"] == "written"


def test_tool_middleware_persists_mutation_before_file_write(tmp_path):
    runtime = WorkflowRuntimeServices.create(str(tmp_path), "job-tools")
    broker = ToolBroker(str(tmp_path), PolicyEngine())
    runtime.attach_tool_broker(broker)
    task = SimpleNamespace(
        task_id="T002", task_type="coding", allowed_paths=["*", "**/*"]
    )

    result = run(broker.execute(task, "write_file", {
        "path": "output.txt", "content": "ok",
    }))
    events = runtime.session.replay()

    assert result["status"] == "written"
    assert [item["event_type"] for item in events] == [
        "tool_call_prepared", "tool_call_completed",
    ]
    assert events[0]["seq"] < events[1]["seq"]
    assert json.loads(runtime.session.path.read_text(encoding="utf-8").splitlines()[0])[
        "event_type"
    ] == "tool_call_prepared"

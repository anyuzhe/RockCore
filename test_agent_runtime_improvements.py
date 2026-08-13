"""Regression coverage for steering, replay, hooks, evals and skill learning."""

import asyncio
import json
import sys
import shlex
import subprocess
from types import SimpleNamespace

from agents.worker import WorkerAgent
from orchestrator.agent_config import HookConfig, ProjectAgentConfig, load_project_config, save_project_config
from orchestrator.engine import Engine
from orchestrator.failure_evals import FailureEvalStore
from orchestrator.hooks import HookRunner
from orchestrator.skill_learning import SkillLearningService
from storage.repositories import JobRepository, ProjectRepository, TaskRepository


class _SteeringRouter:
    def __init__(self):
        self.messages = []
        self.calls = 0

    async def chat_with_tools(self, *_args, **kwargs):
        self.messages.append(list(kwargs.get("messages") or _args[2]))
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "reading", "usage": {},
                "tool_calls": [{
                    "id": "read-1", "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "app.py"}),
                    },
                }],
            }
        return {"content": "report complete", "usage": {}, "tool_calls": []}


class _Broker:
    policy = None
    mcp_manager = None

    def get_tool_definitions(self):
        return []

    async def execute(self, _task, _name, _args):
        return {"status": "success", "content": "ok", "source_version": "v1"}


def _seed(tmp_path, *, status="failed"):
    root = tmp_path / "project"
    root.mkdir()
    engine = Engine(str(tmp_path / "studio.db"))
    session = engine._session_factory()
    project = ProjectRepository(session).create("Demo", str(root))
    job = JobRepository(session).create("JOB-RUNTIME-1", project.id, "fix app")
    task = TaskRepository(session).create(
        "T001", job.id, "Fix app", task_type="coding",
        allowed_paths=["app.py"],
    )
    TaskRepository(session).update_status_by_pk(task.id, status)
    JobRepository(session).update_status(job.job_id, status)
    JobRepository(session).set_failure(
        job.job_id, "provider_timeout", "Provider timed out", "retry",
    )
    JobRepository(session).update_checkpoint(job.job_id, {"saved": True})
    session.close()
    return engine, root


def test_worker_applies_live_guidance_after_complete_tool_batch(tmp_path):
    router = _SteeringRouter()
    queued = [[], [{"text": "只修改标题"}], []]
    task = SimpleNamespace(
        task_id="T001", title="Inspect", description="inspect app",
        task_type="analysis", allowed_paths=["app.py"], acceptance_command="",
        _rockcore_instruction_source=lambda: queued.pop(0) if queued else [],
    )
    result = asyncio.run(WorkerAgent(router, _Broker(), max_turns=4).run(
        task, project_root=str(tmp_path),
    ))

    assert result["status"] == "completed"
    second = router.messages[1]
    assistant = next(item for item in second if item.get("tool_calls"))
    index = second.index(assistant)
    assert second[index + 1]["role"] == "tool"
    assert any(
        item["role"] == "user" and "只修改标题" in item["content"]
        for item in second[index + 2:]
    )


def test_event_recording_can_replay_without_model(tmp_path):
    engine, _root = _seed(tmp_path)

    async def scenario():
        await engine.event_bus.publish(
            "task_running", job_id="JOB-RUNTIME-1", task_id="T001"
        )
        replayed = []

        async def collect(_event_type, **data):
            replayed.append(data["original_event"])

        engine.event_bus.subscribe("job_replay_event", collect)
        count = await engine.replay_job_events("JOB-RUNTIME-1")
        return count, replayed

    count, replayed = asyncio.run(scenario())
    assert count >= 1
    assert "task_running" in replayed


def test_failure_eval_is_deduplicated_and_deterministic(tmp_path):
    engine, root = _seed(tmp_path)
    store = FailureEvalStore(engine._session_factory)
    first = store.capture("JOB-RUNTIME-1")
    second = store.capture("JOB-RUNTIME-1")
    cases = store.load(root / ".ai" / "evals" / "failures.jsonl")

    assert first["id"] == second["id"]
    assert len(cases) == 1
    assert store.evaluate_case(cases[0], {
        "failure_reason": "request timed out", "checkpoint": {"saved": True},
    })["passed"]


def test_historical_failures_are_backfilled_without_model_calls(tmp_path):
    engine, root = _seed(tmp_path)

    captured = engine.failure_evals.sync_historical()

    assert [item["source_job_id"] for item in captured] == ["JOB-RUNTIME-1"]
    cases = engine.failure_evals.load(
        root / ".ai" / "evals" / "failures.jsonl"
    )
    assert len(cases) == 1
    assert cases[0]["failure_class"] == "provider_timeout"


def test_hooks_round_trip_and_execute_without_shell(tmp_path):
    config = ProjectAgentConfig()
    hook_command = shlex.join([
        sys.executable, "-c",
        "import os; print('hook-ok:' + os.environ['ROCKCORE_HOOK_EVENT'])",
    ])
    config.hooks = HookConfig(
        enabled=True,
        before_job=[hook_command],
    )
    save_project_config(str(tmp_path), config)
    loaded = load_project_config(str(tmp_path))
    assert loaded.hooks.before_job == [hook_command]

    class _Bus:
        async def publish(self, *_args, **_kwargs):
            return None

    results = asyncio.run(HookRunner(_Bus()).run(
        "before_job", job_id="JOB-1", project_root=str(tmp_path),
        commands=[hook_command],
    ))
    assert results[0]["status"] == "passed"
    assert "hook-ok:before_job" in results[0]["output"]


def test_windows_hook_parser_accepts_posix_and_native_serialization():
    arguments = [
        r"C:\Program Files\Python 311\python.exe",
        "-c",
        "print('hook-ok')",
    ]

    assert HookRunner.split_command(
        shlex.join(arguments), platform="win32"
    ) == arguments
    assert HookRunner.split_command(
        subprocess.list2cmdline(arguments), platform="win32"
    ) == arguments
    simple_executable = ["python", "-c", "print(1 + 2)"]
    assert HookRunner.split_command(
        shlex.join(simple_executable), platform="win32"
    ) == simple_executable


def test_skill_learning_suggests_only_after_repeated_success(tmp_path):
    engine, root = _seed(tmp_path, status="done")
    service = SkillLearningService(engine._session_factory, threshold=3)

    assert service.observe("JOB-RUNTIME-1") is None
    # Repeated terminal notifications for the same Job do not inflate learning.
    assert service.observe("JOB-RUNTIME-1") is None
    session = engine._session_factory()
    project = ProjectRepository(session).get_by_name("Demo")
    for index in (2, 3):
        job = JobRepository(session).create(
            f"JOB-RUNTIME-{index}", project.id, "fix app"
        )
        TaskRepository(session).create(
            "T001", job.id, "Fix app", task_type="coding",
            allowed_paths=["app.py"],
        )
        JobRepository(session).update_status(job.job_id, "done")
    session.close()
    assert service.observe("JOB-RUNTIME-2") is None
    suggestion = service.observe("JOB-RUNTIME-3")
    assert suggestion and suggestion["count"] == 3
    assert (root / ".ai" / "skill-learning.json").is_file()

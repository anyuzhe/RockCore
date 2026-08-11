"""Regression tests for Worker completion and recovery behavior."""

import asyncio
import json
from types import SimpleNamespace

from agents.worker import WorkerAgent
from orchestrator.agent_config import ProjectAgentConfig
from orchestrator.cost_engine import BudgetExceededError, JobBudget
from orchestrator.engine import Engine
from orchestrator.event_bus import EventBus
from orchestrator.model_router import ModelRouter


def _task(task_type="coding"):
    return SimpleNamespace(
        task_id="T007",
        title="Finish game state",
        description="Implement score, lives, win/loss, and restart behavior.",
        task_type=task_type,
        allowed_paths=["game.js", "index.html"],
        acceptance_command="",
    )


class _ToolSequenceRouter:
    def __init__(self):
        self.calls = 0

    async def chat_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= 5:
            name = "read_file"
            start = 1 + (self.calls - 1) * 80
            arguments = {"path": "game.js", "start": start, "end": start + 79}
        elif self.calls == 6:
            name = "apply_patch"
            arguments = {"path": "game.js", "search": "old", "replace": "new"}
        else:
            return {"content": "Task fully implemented.", "tool_calls": [], "usage": {}}
        return {
            "content": "Inspecting before implementation.",
            "tool_calls": [{
                "id": f"call-{self.calls}",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
            "usage": {},
        }


class _RecordingBroker:
    policy = None

    def __init__(self):
        self.executed = []

    def get_tool_definitions(self):
        return []

    async def execute(self, _task_value, name, _args):
        self.executed.append(name)
        return {"status": "success", "content": "ok"}


class _PrematureThenEditingRouter:
    def __init__(self):
        self.calls = 0
        self.tool_choices = []

    async def chat_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        self.tool_choices.append(_kwargs.get("tool_choice"))
        if self.calls == 1:
            return {"content": "The task looks complete.", "tool_calls": [], "usage": {}}
        if self.calls == 2:
            return {
                "content": "Applying the missing state change.",
                "tool_calls": [{
                    "id": "edit-1",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({
                            "path": "game.js", "search": "old", "replace": "new"
                        }),
                    },
                }],
                "usage": {},
            }
        return {"content": "Task fully implemented.", "tool_calls": [], "usage": {}}


class _AlreadySatisfiedRouter:
    def __init__(self):
        self.calls = 0

    async def chat_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "Checking the existing implementation.",
                "tool_calls": [{
                    "id": "verify-existing",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "game.js"}),
                    },
                }],
                "usage": {},
            }
        return {
            "content": (
                "[ALREADY_SATISFIED] The requested score is already present "
                "in game.js."
            ),
            "tool_calls": [],
            "usage": {},
        }


class _UnverifiedAlreadySatisfiedRouter:
    async def chat_with_tools(self, *_args, **_kwargs):
        return {
            "content": "[ALREADY_SATISFIED] It looks complete.",
            "tool_calls": [],
            "usage": {},
        }


class _ReviewSequenceRouter:
    def __init__(self):
        self.calls = 0

    async def chat_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= 4:
            return {
                "content": "Reading relevant evidence.",
                "tool_calls": [{
                    "id": f"read-{self.calls}",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({
                            "path": "game.js",
                            "start": (self.calls - 1) * 20 + 1,
                            "end": self.calls * 20,
                        }),
                    },
                }],
                "usage": {},
            }
        return {
            "content": "Review report: game-over blocks input and restart resets the full state.",
            "tool_calls": [],
            "usage": {},
        }


class _MalformedResponseRouter:
    def __init__(self):
        self.calls = 0

    async def chat_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return None
        return {"content": "A concise report", "tool_calls": [], "usage": {}}


class _ToolFailureRouter:
    def __init__(self):
        self.calls = 0

    async def chat_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "Applying the change.",
                "tool_calls": [{
                    "id": "bad-args",
                    "function": {"name": "apply_patch", "arguments": "{"},
                }],
                "usage": {},
            }
        if self.calls == 2:
            return {
                "content": "Applying with valid arguments.",
                "tool_calls": [{
                    "id": "good-edit",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({
                            "path": "game.js", "search": "old", "replace": "new"
                        }),
                    },
                }],
                "usage": {},
            }
        return {"content": "Done", "tool_calls": [], "usage": {}}


class _ToolExceptionRouter(_ToolFailureRouter):
    async def chat_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= 2:
            return {
                "content": "Applying with valid arguments.",
                "tool_calls": [{
                    "id": f"edit-{self.calls}",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({
                            "path": "game.js", "search": "old", "replace": "new"
                        }),
                    },
                }],
                "usage": {},
            }
        return {"content": "Done", "tool_calls": [], "usage": {}}


class _ThrowingBroker(_RecordingBroker):
    def __init__(self):
        super().__init__()
        self._thrown = False

    async def execute(self, _task_value, name, args):
        self.executed.append(name)
        if name == "apply_patch" and args.get("search") == "old" and not self._thrown:
            self._thrown = True
            raise PermissionError("read-only workspace")
        return {"status": "success", "content": "ok"}


class _InvalidResultBroker(_RecordingBroker):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def execute(self, _task_value, name, _args):
        self.executed.append(name)
        self.calls += 1
        return None if self.calls == 1 else {"status": "success", "content": "ok"}


class _InvalidResultRouter:
    def __init__(self):
        self.calls = 0

    async def chat_with_tools(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= 2:
            return {
                "content": "Retrying after tool failure.",
                "tool_calls": [{
                    "id": "invalid-result",
                    "function": {
                        "name": "apply_patch",
                        "arguments": json.dumps({
                            "path": "game.js", "search": "old", "replace": "new"
                        }),
                    },
                }],
                "usage": {},
            }
        return {"content": "Done", "tool_calls": [], "usage": {}}


class _RejectedPathBroker(_RecordingBroker):
    async def execute(self, _task_value, name, _args):
        self.executed.append(name)
        return {
            "status": "rejected",
            "error": "[allowed_path] Path not in allowed set: site/index.html",
        }


def test_coding_worker_corrects_premature_no_tool_completion():
    async def scenario():
        router = _PrematureThenEditingRouter()
        broker = _RecordingBroker()
        worker = WorkerAgent(router, broker, max_turns=6)

        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert router.calls == 3
        assert router.tool_choices == ["auto", "required", "auto"]
        assert broker.executed == ["apply_patch"]

    asyncio.run(scenario())


def test_coding_worker_accepts_verified_already_satisfied_state():
    async def scenario():
        router = _AlreadySatisfiedRouter()
        broker = _RecordingBroker()
        worker = WorkerAgent(router, broker, max_turns=4)

        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert result["no_changes"] is True
        assert "ALREADY_SATISFIED" not in result["content"]
        assert "already present" in result["content"]
        assert broker.executed == ["read_file"]

    asyncio.run(scenario())


def test_coding_worker_rejects_unverified_already_satisfied_state():
    async def scenario():
        worker = WorkerAgent(
            _UnverifiedAlreadySatisfiedRouter(), _RecordingBroker(), max_turns=3
        )

        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "failed"
        assert result["error"] == "Coding model ended without editing files"

    asyncio.run(scenario())


def test_conditional_coding_worker_can_complete_without_changes():
    async def scenario():
        class NoChangeRouter:
            calls = 0

            async def chat_with_tools(self, *_args, **_kwargs):
                self.calls += 1
                return {
                    "content": "检查完成，未发现需要修复的问题。",
                    "tool_calls": [],
                    "usage": {},
                }

        router = NoChangeRouter()
        task = _task()
        task.description = "仅当发现影响验收的问题时修复；若未发现则跳过。"
        worker = WorkerAgent(router, _RecordingBroker(), max_turns=4)

        result = await worker.run(task, project_root=".")

        assert result["status"] == "completed"
        assert result["no_changes"] is True
        assert result["content"] == "检查完成，未发现需要修复的问题。"
        assert router.calls == 1

    asyncio.run(scenario())


def test_read_only_review_stops_after_evidence_and_returns_report():
    async def scenario():
        router = _ReviewSequenceRouter()
        broker = _RecordingBroker()
        task = _task("analysis")
        task.title = "Audit game end and restart"
        worker = WorkerAgent(router, broker, max_turns=10, max_exploration_turns=4)

        result = await worker.run(task, project_root=".")

        assert result["status"] == "completed"
        assert "Review report" in result["content"]
        assert broker.executed == ["read_file"] * 4

    asyncio.run(scenario())


def test_worker_never_crashes_on_a_malformed_provider_response():
    async def scenario():
        worker = WorkerAgent(_MalformedResponseRouter(), _RecordingBroker(), max_turns=4)
        result = await worker.run(_task("analysis"), project_root=".")

        assert result["status"] == "failed"
        assert "invalid response object" in result["error"]

    asyncio.run(scenario())


def test_worker_returns_tool_argument_errors_to_the_model():
    async def scenario():
        router = _ToolFailureRouter()
        broker = _RecordingBroker()
        worker = WorkerAgent(router, broker, max_turns=5)
        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert broker.executed == ["apply_patch"]

    asyncio.run(scenario())


def test_worker_continues_after_a_tool_exception():
    async def scenario():
        router = _ToolExceptionRouter()
        broker = _ThrowingBroker()
        worker = WorkerAgent(router, broker, max_turns=5)
        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert broker.executed == ["apply_patch", "apply_patch"]

    asyncio.run(scenario())


def test_worker_converts_an_invalid_tool_result_to_recoverable_error():
    async def scenario():
        broker = _InvalidResultBroker()
        worker = WorkerAgent(_InvalidResultRouter(), broker, max_turns=3)
        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert broker.executed == ["apply_patch", "apply_patch"]

    asyncio.run(scenario())


def test_worker_surfaces_allowed_path_rejection_without_spending_all_turns():
    async def scenario():
        broker = _RejectedPathBroker()
        worker = WorkerAgent(_ToolExceptionRouter(), broker, max_turns=8)

        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "failed"
        assert "site/index.html" in result["error"]
        assert broker.executed == ["apply_patch"]

    asyncio.run(scenario())


def test_worker_enforces_edit_after_exploration_budget():
    async def scenario():
        router = _ToolSequenceRouter()
        broker = _RecordingBroker()
        worker = WorkerAgent(
            router, broker, max_turns=8, max_exploration_turns=4
        )

        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert broker.executed.count("read_file") == 4
        assert broker.executed.count("apply_patch") == 1
        assert result["content"] == "Task fully implemented."

    asyncio.run(scenario())


class _AlwaysPartialWorker:
    def __init__(self):
        self.calls = 0

    async def run(self, *_args, **_kwargs):
        self.calls += 1
        return {"status": "failed", "error": "Max turns (10) reached"}


def test_partial_changes_at_turn_limit_do_not_auto_pass(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        worker = _AlwaysPartialWorker()

        async def changes_exist(*_args, **_kwargs):
            return True

        engine._check_file_changes = changes_exist
        result = await engine._execute_single_task_with_escalation(
            _task(),
            SimpleNamespace(job_id="JOB-1", project=None),
            {},
            worker,
            str(tmp_path),
        )

        assert result["status"] == "pending_validation"
        assert worker.calls == 3
        assert len(engine.event_bus.get_history("task_continuing")) == 3

    asyncio.run(scenario())


class _RecoveringWorker:
    def __init__(self):
        self.calls = []

    async def run(self, *_args, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("provider_override") == "kimi":
            return {"status": "completed", "content": "Repair completed."}
        return {"status": "failed", "error": "Max turns (10) reached"}


class _TransientProviderWorker:
    def __init__(self):
        self.calls = []

    async def run(self, *_args, **kwargs):
        self.calls.append({
            "provider": kwargs.get("provider_override"),
            "model": kwargs.get("model_override"),
        })
        if kwargs.get("provider_override") == "kimi":
            return {"status": "completed", "content": "Recovered with Kimi."}
        return {"status": "failed", "error": "Connection error"}


class _ThrowingProviderWorker:
    def __init__(self):
        self.calls = []

    async def run(self, *_args, **kwargs):
        self.calls.append(kwargs.get("provider_override"))
        if kwargs.get("provider_override") == "kimi":
            return {"status": "completed", "content": "Recovered with Kimi."}
        raise RuntimeError("Missing credentials for DeepSeek")


def test_max_turn_failures_preserve_progress_after_primary_retries(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        worker = _RecoveringWorker()

        async def no_changes(*_args, **_kwargs):
            return False

        engine._check_file_changes = no_changes
        engine.model_router._providers["kimi"] = object()

        result = await engine._execute_single_task_with_escalation(
            _task(),
            SimpleNamespace(job_id="JOB-1", project=None),
            {},
            worker,
            str(tmp_path),
        )

        assert result["status"] == "needs_continuation"
        assert result["failure_stage"] == "budget_continuation"
        assert len(worker.calls) == 3
        assert all(call.get("provider_override") is None for call in worker.calls)
        assert "attempt 1 failed" in worker.calls[1]["recovery_context"]
        assert "final Worker attempt" in worker.calls[2]["recovery_context"]

    asyncio.run(scenario())


def test_emergency_runs_after_three_primary_worker_failures(tmp_path):
    class FailedWorker:
        def __init__(self):
            self.calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return {"status": "failed", "error": "type mismatch"}

    class Emergency:
        def __init__(self):
            self.calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return {"status": "completed", "fix_success": True}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        worker = FailedWorker()
        emergency = Emergency()
        engine.register_agent("emergency_coder", emergency)
        task = _task()
        task._rockcore_retry_count = 2
        task._rockcore_emergency_after_failures = 3

        result = await engine._execute_single_task_with_escalation(
            task,
            SimpleNamespace(job_id="JOB-EMERGENCY", project=None),
            {},
            worker,
            str(tmp_path),
        )

        assert result["status"] == "completed"
        assert worker.calls == 3
        assert emergency.calls == 1
        assert len(engine.event_bus.get_history("task_escalating")) == 1

    asyncio.run(scenario())


def test_connection_failure_switches_to_kimi_immediately(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        worker = _TransientProviderWorker()
        engine.model_router._providers["kimi"] = object()

        result = await engine._execute_single_task_with_escalation(
            _task(),
            SimpleNamespace(job_id="JOB-TRANSIENT", project=None),
            {},
            worker,
            str(tmp_path),
        )

        assert result["status"] == "completed"
        assert worker.calls == [
            {"provider": None, "model": None},
            {"provider": "kimi", "model": "kimi-k2.7"},
        ]

    asyncio.run(scenario())


def test_thrown_provider_failure_skips_same_provider_retries(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        worker = _ThrowingProviderWorker()
        engine.model_router._providers["kimi"] = object()

        result = await engine._execute_single_task_with_escalation(
            _task(),
            SimpleNamespace(job_id="JOB-CREDENTIALS", project=None),
            {},
            worker,
            str(tmp_path),
        )

        assert result["status"] == "completed"
        assert worker.calls == [None, "kimi"]

    asyncio.run(scenario())


def test_transient_provider_error_is_classified_for_fallback():
    assert Engine._is_provider_unavailable("Connection error")
    assert Engine._is_provider_unavailable("HTTP 503 Service Unavailable")
    assert Engine._is_provider_unavailable("Request timed out")


def test_tool_choice_capability_error_is_not_provider_unavailability():
    error = "Thinking mode does not support this tool_choice"

    assert Engine._is_provider_capability_error(error)
    assert not Engine._is_provider_unavailable(error)


def test_tool_choice_capability_error_switches_to_kimi_once(tmp_path):
    async def scenario():
        worker = _TransientProviderWorker()
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        engine.model_router._providers["kimi"] = object()

        async def run_with_capability_error(*_args, **kwargs):
            worker.calls.append({
                "provider": kwargs.get("provider_override"),
                "model": kwargs.get("model_override"),
            })
            if kwargs.get("provider_override") == "kimi":
                return {"status": "completed", "content": "Recovered with Kimi."}
            return {
                "status": "failed",
                "error": "Thinking mode does not support this tool_choice",
            }

        worker.run = run_with_capability_error
        result = await engine._execute_single_task_with_escalation(
            _task(),
            SimpleNamespace(job_id="JOB-CAPABILITY", project=None),
            {},
            worker,
            str(tmp_path),
        )

        assert result["status"] == "completed"
        assert worker.calls == [
            {"provider": None, "model": None},
            {"provider": "kimi", "model": "kimi-k2.7"},
        ]

    asyncio.run(scenario())


class _SlowProvider:
    async def chat(self, *_args, **_kwargs):
        await asyncio.sleep(0.05)
        return {"content": "late", "usage": {}}

    async def chat_with_tools(self, *_args, **_kwargs):
        await asyncio.sleep(0.05)
        return {"content": "late", "tool_calls": [], "usage": {}}


class _SyncMalformedProvider:
    def chat(self, *_args, **_kwargs):
        return None

    def chat_with_tools(self, *_args, **_kwargs):
        return {"content": None, "tool_calls": None, "usage": "invalid"}


def test_model_router_translates_provider_timeout():
    async def scenario():
        router = ModelRouter(provider_map={"worker": "deepseek"})
        router.register_provider("deepseek", _SlowProvider())
        router.request_timeout = 0.01

        try:
            await router.chat("worker", "system", [])
        except TimeoutError as error:
            assert "timed out" in str(error)
        else:
            raise AssertionError("expected provider timeout")

    asyncio.run(scenario())


def test_model_router_normalizes_sync_and_malformed_provider_output():
    async def scenario():
        router = ModelRouter(provider_map={"worker": "deepseek"})
        router.register_provider("deepseek", _SyncMalformedProvider())

        try:
            await router.chat("worker", "system", [])
        except RuntimeError as error:
            assert "invalid response object" in str(error)
        else:
            raise AssertionError("expected invalid response error")

        response = await router.chat_with_tools("worker", "system", [], [])
        assert response["content"] == ""
        assert response["tool_calls"] == []
        assert response["usage"] == {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
        }

    asyncio.run(scenario())


def test_default_worker_budgets_use_reliable_soft_limits():
    config = ProjectAgentConfig()

    assert config.complexity_turns == {
        "simple": 16,
        "normal": 24,
        "complex": 36,
    }
    assert config.complexity_exploration["simple"] == 4
    assert config.governor.model == "gpt-5.6-sol"
    assert config.governor.reasoning_effort == "high"
    assert config.planner.model == "kimi-k3"
    assert config.reviewer.provider == "codex"
    assert config.reviewer.reasoning_effort == "high"
    assert config.emergency_coder.reasoning_effort == "max"
    assert config.worker.emergency_after_failures == 3
    assert config.worker.fallback_model == "kimi-k2.7"


def test_legacy_role_defaults_are_upgraded_to_recommended_stack():
    config = ProjectAgentConfig.from_dict({
        "governor": {
            "provider": "codex", "model": "codex-sdk",
        },
        "planner": {
            "provider": "kimi", "model": "kimi-k2.6",
        },
        "reviewer": {
            "provider": "kimi", "model": "kimi-k2.6",
        },
    })

    assert config.config_version == 3
    assert config.governor.model == "gpt-5.6-sol"
    assert config.governor.reasoning_effort == "high"
    assert config.planner.model == "kimi-k3"
    assert config.reviewer.provider == "codex"
    assert config.reviewer.model == "gpt-5.6-sol"
    assert config.emergency_coder.reasoning_effort == "max"


def test_large_existing_task_receives_a_dynamic_budget(tmp_path):
    (tmp_path / "game.js").write_text("const x = 1;\n" * 600)
    (tmp_path / "index.html").write_text("<div></div>\n" * 170)
    task = _task()
    task.dependencies = ["T001", "T002", "T003", "T004", "T005", "T006"]
    task.description = "实现分数；生命；胜利；失败；重新开始；同步界面。"

    budget = Engine._estimate_task_budget(
        task, str(tmp_path), base_turns=16, base_exploration=4
    )

    assert budget["max_turns"] == 31
    assert budget["exploration_turns"] == 6
    assert budget["existing_files"] == 2
    assert budget["total_lines"] == 770


def test_worker_compacts_history_without_splitting_recent_tool_pair():
    messages = [
        {"role": "user", "content": "ORIGINAL REQUIREMENTS\n" + "x" * 8_000},
        {
            "role": "assistant",
            "content": "old exploration",
            "tool_calls": [{
                "id": "old-call",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "old-call", "content": "z" * 8_000},
        {
            "role": "assistant",
            "content": "recent check",
            "tool_calls": [{
                "id": "recent-call",
                "function": {"name": "git_diff", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "recent-call", "content": "verified"},
    ]

    compacted = WorkerAgent._compact_messages(messages, max_chars=5_000)
    serialized_size = sum(len(json.dumps(item, ensure_ascii=False)) for item in compacted)
    ids = {
        call["id"]
        for message in compacted
        for call in (message.get("tool_calls") or [])
    }
    tool_response_ids = {
        message.get("tool_call_id")
        for message in compacted
        if message.get("role") == "tool"
    }

    assert compacted[0]["content"].startswith("ORIGINAL REQUIREMENTS")
    assert "recent-call" in ids
    assert ids == tool_response_ids
    assert serialized_size <= 5_000


def test_model_router_auto_expands_soft_token_budget_before_provider_call():
    class Provider:
        calls = 0

        async def chat(self, *_args, **_kwargs):
            self.calls += 1
            return {"content": "unexpected", "usage": {}}

    async def scenario():
        router = ModelRouter(provider_map={"worker": "deepseek"})
        provider = Provider()
        router.register_provider("deepseek", provider)
        router.cost_engine.set_budget(
            "JOB-BUDGET", JobBudget(max_input_tokens=1)
        )
        await router.cost_engine.record_usage(
            "JOB-BUDGET", "worker", input_tokens=2
        )
        router.set_job_id("JOB-BUDGET")

        result = await router.chat("worker", "system", [])

        assert result["content"] == "unexpected"
        assert provider.calls == 1
        assert router.cost_engine.get_budget(
            "JOB-BUDGET"
        ).max_input_tokens > 1

    asyncio.run(scenario())


def test_budget_error_stops_worker_escalation_immediately(tmp_path):
    class BudgetWorker:
        def __init__(self):
            self.calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return {
                "status": "failed",
                "error": (
                    "RockCore job budget exceeded: "
                    "Input tokens exceeded: 500001/500000"
                ),
            }

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        worker = BudgetWorker()
        result = await engine._execute_single_task_with_escalation(
            _task(), SimpleNamespace(job_id="JOB-BUDGET", project=None),
            {}, worker, str(tmp_path),
        )

        assert result["status"] == "needs_continuation"
        assert result["failure_stage"] == "budget_continuation"
        assert "budget exceeded" in result["error"].lower()
        assert worker.calls == 1
        assert not engine.event_bus.get_history("task_replanning")
        assert not engine.event_bus.get_history("task_escalating")

    asyncio.run(scenario())


def test_existing_artifact_is_validated_before_provider_failure_is_terminal(
    tmp_path,
):
    class Worker:
        calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return {"status": "failed", "error": "Connection error"}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        engine._check_file_changes = lambda *_args, **_kwargs: asyncio.sleep(
            0, result=True
        )
        worker = Worker()

        result = await engine._execute_single_task_with_escalation(
            _task(), SimpleNamespace(job_id="JOB-ARTIFACT", project=None),
            {}, worker, str(tmp_path),
        )

        assert result["status"] == "pending_validation"
        assert result["failure_stage"] == "artifact_recovery"
        assert engine.event_bus.get_history("task_pending_validation")

    asyncio.run(scenario())


def test_worker_uses_real_token_ratio_to_enter_finalization():
    class Cost:
        @staticmethod
        def get_task_usage(_job_id, _task_id):
            return {"effective_input_tokens": 93}

    class Router:
        cost_engine = Cost()
        event_bus = EventBus()
        _current_job_id = "JOB-TOKEN-STAGES"

        async def chat_with_tools(self, *_args, **_kwargs):
            return {
                "content": "Concrete review findings.",
                "tool_calls": [],
                "usage": {},
            }

    async def scenario():
        task = _task("analysis")
        task._rockcore_input_budget = 100
        worker = WorkerAgent(
            Router(), _RecordingBroker(), max_turns=4,
            max_exploration_turns=3,
        )

        result = await worker.run(task, project_root=".")

        assert result["status"] == "completed"
        assert task._rockcore_finalization_mode is True
        assert Router.event_bus.get_history("task_budget_finalizing")

    asyncio.run(scenario())

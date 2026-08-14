"""Regression tests for Worker completion and recovery behavior."""

import asyncio
import json
from types import SimpleNamespace

from agents.worker import UNINFORMATIVE_READ_WARNING, WorkerAgent
from orchestrator.agent_config import ProjectAgentConfig
from orchestrator.cost_engine import BudgetExceededError, JobBudget
from orchestrator.engine import Engine
from orchestrator.event_bus import EventBus
from orchestrator.model_router import DEFAULT_REQUEST_TIMEOUT, ModelRouter
from orchestrator.state_machine import JobState


def _task(task_type="coding"):
    return SimpleNamespace(
        task_id="T007",
        title="Finish game state",
        description="Implement score, lives, win/loss, and restart behavior.",
        task_type=task_type,
        allowed_paths=["game.js", "index.html"],
        acceptance_command="",
        acceptance_commands=[],
        execution_group_id="runtime:game",
        internal_steps=[],
    )


def test_incremental_tool_result_references_unchanged_version():
    delivered = {}
    result = {
        "status": "success", "path": "game.js",
        "source_version": "v1", "content": "const ready = true;",
    }

    first = WorkerAgent._incremental_result_for_model(
        "read_file", {"path": "game.js"}, result, delivered
    )
    second = WorkerAgent._incremental_result_for_model(
        "read_file", {"path": "game.js"}, result, delivered
    )

    assert first["content"] == "const ready = true;"
    assert second["status"] == "unchanged"
    assert second["incremental"] is True
    assert "const ready = true" in second["summary"]


def test_incremental_file_result_returns_only_unseen_overlapping_lines():
    delivered = {}
    source_version = "v1"
    first_result = {
        "status": "success", "path": "game.js",
        "source_version": source_version,
        "content": "\n".join(f"line {index}" for index in range(1, 401)),
        "start_line": 1, "end_line": 400, "total_lines": 500,
        "has_more": True, "next_start": 401,
    }
    overlap_result = {
        "status": "success", "path": "game.js",
        "source_version": source_version,
        "content": "\n".join(f"line {index}" for index in range(200, 501)),
        "start_line": 200, "end_line": 500, "total_lines": 500,
        "has_more": False, "next_start": None,
    }

    first = WorkerAgent._incremental_result_for_model(
        "read_file", {"path": "game.js", "start": 1, "end": 400},
        first_result, delivered,
    )
    overlap = WorkerAgent._incremental_result_for_model(
        "read_file", {"path": "game.js", "start": 200, "end": 500},
        overlap_result, delivered,
    )
    repeated = WorkerAgent._incremental_result_for_model(
        "read_file", {"path": "game.js", "start": 250, "end": 350},
        {
            **overlap_result,
            "content": "\n".join(f"line {index}" for index in range(250, 351)),
            "start_line": 250, "end_line": 350,
        }, delivered,
    )

    assert first["content"].startswith("line 1\n")
    assert overlap["incremental"] is True
    assert overlap["new_line_ranges"] == [[401, 500]]
    assert overlap["start_line"] == 401
    assert overlap["content"].startswith("line 401\n")
    assert "line 400" not in overlap["content"]
    assert repeated["status"] == "unchanged"
    assert repeated["covered_lines"] == [250, 350]


def test_incremental_file_result_resets_coverage_after_file_change():
    delivered = {}
    old = {
        "status": "success", "path": "game.js", "source_version": "v1",
        "content": "old", "start_line": 1, "end_line": 1,
        "total_lines": 1,
    }
    new = {**old, "source_version": "v2", "content": "new"}

    WorkerAgent._incremental_result_for_model(
        "read_file", {"path": "game.js"}, old, delivered,
    )
    refreshed = WorkerAgent._incremental_result_for_model(
        "read_file", {"path": "game.js"}, new, delivered,
    )

    assert refreshed["content"] == "new"
    assert refreshed["new_line_ranges"] == [[1, 1]]


def test_worker_resumes_protocol_valid_conversation_from_same_group():
    class Router:
        def __init__(self):
            self.calls = 0
            self.message_batches = []

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            self.message_batches.append(json.loads(json.dumps(_args[2])))
            if self.calls == 1:
                return {
                    "content": "Reading once.",
                    "tool_calls": [{
                        "id": "read-once",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "game.js"}),
                        },
                    }],
                    "usage": {},
                }
            return {
                "content": "Concrete review report.",
                "tool_calls": [], "usage": {},
            }

    async def exercise():
        router = Router()
        worker = WorkerAgent(router, _RecordingBroker(), max_turns=4)
        task = _task("analysis")
        first = await worker.run(task)
        second = await worker.run(task, session_state=first["worker_session"])
        return router, first, second

    router, first, second = asyncio.run(exercise())

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    resumed_messages = router.message_batches[-1]
    assert any(message.get("tool_calls") for message in resumed_messages)
    assert any(message.get("role") == "tool" for message in resumed_messages)
    assert "Continue the same execution group" in resumed_messages[-1]["content"]


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


def test_worker_recovers_from_truncated_large_write_with_smaller_payload():
    class Router:
        def __init__(self):
            self.calls = 0
            self.max_tokens = []
            self.second_messages = []

        async def chat_with_tools(self, *_args, **kwargs):
            self.calls += 1
            self.max_tokens.append(kwargs.get("max_tokens"))
            if self.calls == 1:
                return {
                    "content": "Writing the complete game implementation.",
                    "tool_calls": [{
                        "id": "oversized-write",
                        "function": {
                            "name": "write_file",
                            "arguments": (
                                '{"path":"game.js","content":"'
                                + ("x" * 20_000)
                            ),
                        },
                    }],
                    "finish_reason": "length",
                    "usage": {"output_tokens": 4_096},
                }
            if self.calls == 2:
                self.second_messages = list(_args[2])
                return {
                    "content": "Switching to a small complete skeleton.",
                    "tool_calls": [{
                        "id": "small-write",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "game.js",
                                "content": "(() => { 'use strict'; })();\n",
                            }),
                        },
                    }],
                    "finish_reason": "tool_calls",
                    "usage": {},
                }
            return {
                "content": "The implementation is complete.",
                "tool_calls": [],
                "finish_reason": "stop",
                "usage": {},
            }

    async def scenario():
        router = Router()
        broker = _RecordingBroker()
        result = await WorkerAgent(
            router, broker, max_turns=5
        ).run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert broker.executed == ["write_file"]
        assert router.max_tokens[0] is None
        assert any(
            "under 12000 characters" in str(message.get("content", ""))
            for message in router.second_messages
        )
        malformed_history = next(
            message for message in router.second_messages
            if message.get("tool_calls")
        )
        recorded_arguments = malformed_history["tool_calls"][0]["function"][
            "arguments"
        ]
        assert json.loads(recorded_arguments)["_rockcore_recovery"] == (
            "tool payload was truncated"
        )

    asyncio.run(scenario())


def test_pdf_worker_rejects_custom_generator_and_stops_repeated_strategy():
    class Router:
        calls = 0

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            return {
                "content": "Building a custom PDF generator.",
                "tool_calls": [{
                    "id": f"custom-{self.calls}",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({
                            "path": "make_pdf.py",
                            "content": "# custom font parser",
                        }),
                    },
                }],
                "usage": {},
            }

    async def scenario():
        task = _task()
        task.title = "把 PDF 书籍精简成 summary.pdf"
        task.description = "读取 source.pdf 并生成最终 PDF"
        task.allowed_paths = ["*"]
        task._rockcore_artifact_manifest = {
            "kind": "pdf", "require_changed_output": True,
        }
        broker = _RecordingBroker()
        result = await WorkerAgent(
            Router(), broker, max_turns=10
        ).run(task, project_root=".")

        assert result["status"] == "failed"
        assert "REPEATED_TOOL_FAILURE" in result["error"]
        assert broker.executed == []

    asyncio.run(scenario())


def test_identical_unchanged_reads_only_trigger_a_strategy_warning():
    class Router:
        def __init__(self):
            self.calls = 0
            self.received_messages = []

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            self.received_messages.append(list(_args[2]))
            if self.calls <= 10:
                return {
                    "content": "Re-reading a previously truncated file.",
                    "tool_calls": [{
                        "id": f"read-{self.calls}",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "game.js"}),
                        },
                    }],
                    "usage": {},
                }
            return {
                "content": "Review report based on the completed reads.",
                "tool_calls": [],
                "usage": {},
            }

    async def scenario():
        router = Router()
        broker = _RecordingBroker()
        result = await WorkerAgent(
            router, broker, max_turns=12, max_exploration_turns=48
        ).run(_task("analysis"), project_root=".")

        assert result["status"] == "completed"
        # The first read is useful; after eight unchanged repeats the Worker
        # pauses exploration and the next hallucinated read is not executed.
        assert broker.executed == ["read_file"] * (
            UNINFORMATIVE_READ_WARNING + 1
        )
        assert any(
            "produced no new lines" in str(
                message.get("content", "")
            )
            for messages in router.received_messages
            for message in messages
        )

    asyncio.run(scenario())


def test_changed_source_or_result_is_not_an_uninformative_repeat():
    first = {
        "status": "success", "content": "same", "source_version": "v1",
    }
    changed_source = {
        "status": "success", "content": "same", "source_version": "v2",
    }
    changed_result = {
        "status": "success", "content": "changed", "source_version": "v2",
    }

    assert WorkerAgent._exploration_observation(first) != (
        WorkerAgent._exploration_observation(changed_source)
    )
    assert WorkerAgent._exploration_observation(changed_source) != (
        WorkerAgent._exploration_observation(changed_result)
    )


def test_search_evidence_ignores_keyword_changes_without_new_matches():
    first = WorkerAgent._search_evidence(
        "search_in_file", {"path": "main.py", "text": "price"}, {
            "path": "main.py", "source_version": "v1",
            "matches": [{"line": 42, "content": "item.setText(price)"}],
        },
    )
    renamed_query = WorkerAgent._search_evidence(
        "search_in_file", {"path": "main.py", "text": "setText"}, {
            "path": "main.py", "source_version": "v1",
            "matches": [{"line": 42, "content": "item.setText(price)"}],
        },
    )
    changed_file = WorkerAgent._search_evidence(
        "search_in_file", {"path": "main.py", "text": "setText"}, {
            "path": "main.py", "source_version": "v2",
            "matches": [{"line": 42, "content": "item.setText(price)"}],
        },
    )

    assert first == renamed_query
    assert first[0] != changed_file[0]


def test_worker_pauses_semantically_repeated_searches_and_finishes_report():
    class Router:
        def __init__(self):
            self.calls = 0
            self.tool_names = []

        async def chat_with_tools(self, *_args, **kwargs):
            self.calls += 1
            self.tool_names.append({
                item["function"]["name"] for item in kwargs.get("tools", [])
            })
            if self.calls <= UNINFORMATIVE_READ_WARNING + 1:
                return {
                    "content": "Searching another spelling.",
                    "tool_calls": [{
                        "id": f"search-{self.calls}",
                        "function": {
                            "name": "search_in_file",
                            "arguments": json.dumps({
                                "path": "main.py",
                                "text": f"price-{self.calls}",
                            }),
                        },
                    }],
                    "usage": {},
                }
            return {
                "content": "The repeated searches identify the same price row.",
                "tool_calls": [], "usage": {},
            }

    class Broker(_RecordingBroker):
        def get_tool_definitions(self):
            return [{
                "type": "function",
                "function": {
                    "name": "search_in_file", "parameters": {
                        "type": "object", "properties": {},
                    },
                },
            }]

        async def execute(self, _task_value, name, _args):
            self.executed.append(name)
            return {
                "status": "success", "path": "main.py",
                "source_version": "v1",
                "matches": [{"line": 42, "content": "item.setText(price)"}],
            }

    async def scenario():
        router = Router()
        broker = Broker()
        result = await WorkerAgent(
            router, broker, max_turns=14, max_exploration_turns=60,
        ).run(_task("analysis"), project_root=".")

        assert result["status"] == "completed"
        assert broker.executed.count("search_in_file") == (
            UNINFORMATIVE_READ_WARNING + 1
        )
        assert "search_in_file" not in router.tool_names[-1]

    asyncio.run(scenario())


def test_cache_metadata_does_not_change_exploration_observation():
    original = {
        "status": "success", "content": "same", "source_version": "v1",
        "tool": "read_file", "duration_ms": 12,
    }
    cached = {
        **original, "cache_hit": True, "duration_ms": 0,
    }

    assert WorkerAgent._exploration_observation(original) == (
        WorkerAgent._exploration_observation(cached)
    )


def test_paginated_or_truncated_results_are_allowed_to_continue():
    assert WorkerAgent._exploration_result_is_truncated({
        "status": "success", "has_more": True, "next_start": 81,
    }, "read_file")
    assert WorkerAgent._exploration_result_is_truncated({
        "status": "success", "truncated": True,
    }, "search_code")
    assert not WorkerAgent._exploration_result_is_truncated({
        "status": "success", "content": "complete", "has_more": False,
    }, "read_file")


def test_worker_reports_successful_tool_progress():
    class Router:
        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "Applying change.",
                    "tool_calls": [{
                        "id": "write-1",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "game.js", "content": "updated",
                            }),
                        },
                    }],
                    "usage": {},
                }
            return {"content": "Done", "tool_calls": [], "usage": {}}

    async def scenario():
        updates = []
        task = _task()
        task._rockcore_progress_callback = updates.append
        result = await WorkerAgent(
            Router(), _RecordingBroker(), max_turns=3
        ).run(task, project_root=".")

        assert result["status"] == "completed"
        assert [update["event_kind"] for update in updates] == [
            "tool_started", "tool_completed",
        ]
        assert updates[0] == {
            "phase": "正在修改文件",
            "tool": "write_file",
            "path": "game.js",
            "turn": 1,
            "max_turns": 3,
            "event_kind": "tool_started",
            "status": "started",
            "arguments": {"path": "game.js", "content": "updated"},
            "result": {},
            "duration_ms": 0,
        }
        assert updates[1] == {
            "phase": "正在修改文件",
            "tool": "write_file",
            "path": "game.js",
            "turn": 1,
            "max_turns": 3,
            "event_kind": "tool_completed",
            "status": "success",
            "arguments": {"path": "game.js", "content": "updated"},
            "result": {
                "status": "success",
                "content": "ok",
            },
            "duration_ms": 0,
        }

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


def test_worker_treats_exploration_budget_as_a_soft_threshold():
    async def scenario():
        router = _ToolSequenceRouter()
        broker = _RecordingBroker()
        worker = WorkerAgent(
            router, broker, max_turns=8, max_exploration_turns=4
        )

        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert broker.executed.count("read_file") == 5
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
        return {"status": "failed", "error": "Provider request timed out after 540s"}


class _ThrowingProviderWorker:
    def __init__(self):
        self.calls = []

    async def run(self, *_args, **kwargs):
        self.calls.append(kwargs.get("provider_override"))
        if kwargs.get("provider_override") == "kimi":
            return {"status": "completed", "content": "Recovered with Kimi."}
        raise RuntimeError("Missing credentials for DeepSeek")


def test_max_turn_failures_without_progress_pause_for_user_confirmation(tmp_path):
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

        assert result["status"] == "needs_user_action"
        assert result["failure_stage"] == "turn_limit_continuation"
        assert len(worker.calls) == 1
        assert not engine.event_bus.get_history("task_escalating")

    asyncio.run(scenario())


def test_max_turn_checkpoint_is_user_action_in_execution_pipeline(tmp_path):
    class TurnLimitedWorker:
        def scoped_to(self, _root):
            return self

        async def run(self, *_args, **_kwargs):
            return {
                "status": "needs_continuation",
                "error": "Max turns (120) reached",
                "tool_calls": [{"tool": "read_file", "status": "completed"}],
            }

    async def scenario():
        project_root = tmp_path / "project"
        project_root.mkdir()
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Turn limit", str(project_root))
            job = repos["job"].create("JOB-TURN-LIMIT", project.id, "检查项目")
            task = repos["task"].create(
                "T001", job.id, "读取项目并分析", task_type="analysis",
                allowed_paths=["*"],
            )
            engine.register_agent("worker", TurnLimitedWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            result = await engine._run_execution(
                job, repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(job)
            repos["_session"].refresh(task)
            assert result["status"] == "needs_attention"
            assert job.status == "needs_attention"
            assert task.status == "needs_attention"
            assert engine.event_bus.get_history("task_needs_user_action")
            assert engine.event_bus.get_history("job_needs_attention")
            assert not engine.event_bus.get_history("task_failed")
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_primary_worker_strategy_failures_do_not_trigger_emergency(tmp_path):
    class FailedWorker:
        def __init__(self):
            self.calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return {"status": "failed", "error": "type mismatch"}

    class Emergency:
        def __init__(self):
            self.calls = 0
            self.project_roots = []

        async def run(self, *_args, **kwargs):
            self.calls += 1
            self.project_roots.append(kwargs.get("project_root"))
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

        assert result["status"] == "failed"
        assert worker.calls == 3
        assert emergency.calls == 0
        assert emergency.project_roots == []
        assert not engine.event_bus.get_history("task_escalating")

    asyncio.run(scenario())


def test_timeout_switches_to_kimi_immediately(tmp_path):
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
            {"provider": "kimi", "model": "kimi-k2.7-code"},
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


def test_repeated_tool_strategy_stays_on_primary_and_validates_artifact(tmp_path):
    class StalledWorker:
        def __init__(self):
            self.calls = []

        async def run(self, *_args, **kwargs):
            self.calls.append(kwargs.get("provider_override"))
            return {
                "status": "failed",
                "error": "REPEATED_TOOL_FAILURE: identical read rejected six times",
            }

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        engine.model_router._providers["kimi"] = object()
        engine._check_file_changes = lambda *_args, **_kwargs: asyncio.sleep(
            0, result=True
        )
        worker = StalledWorker()

        result = await engine._execute_single_task_with_escalation(
            _task(), SimpleNamespace(job_id="JOB-STALL", project=None),
            {}, worker, str(tmp_path),
        )

        assert result["status"] == "pending_validation"
        assert result["failure_stage"] == "strategy_stall_validation"
        assert worker.calls == [None]
        assert not engine.event_bus.get_history("task_provider_fallback")
        assert not engine.event_bus.get_history("task_escalating")

    asyncio.run(scenario())


def test_transient_provider_error_is_classified_for_fallback():
    assert Engine._is_provider_unavailable("Request timed out")
    assert Engine._is_provider_unavailable("Error code: 429 - overloaded")
    assert Engine._is_provider_unavailable("Error code: 404 - model not found")
    assert Engine._is_provider_unavailable("Error code: 401 - invalid API key")
    assert not Engine._is_provider_unavailable("Connection error")
    assert not Engine._is_provider_unavailable("HTTP 503 Service Unavailable")


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
            {"provider": "kimi", "model": "kimi-k2.7-code"},
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


def test_model_router_default_timeout_allows_long_reasoning_requests():
    assert DEFAULT_REQUEST_TIMEOUT == 540
    assert ModelRouter().request_timeout == 540


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
        "simple": 60,
        "normal": 96,
        "complex": 144,
    }
    assert config.worker.max_turns == 96
    assert config.worker.max_exploration_turns == 60
    assert config.worker.patch_recovery_turns == 6
    assert config.complexity_exploration == {
        "simple": 36,
        "normal": 60,
        "complex": 96,
    }
    assert config.governor.model == "gpt-5.6-sol"
    assert config.governor.reasoning_effort == "high"
    assert config.planner.model == "kimi-k3"
    assert config.reviewer.provider == "codex"
    assert config.reviewer.reasoning_effort == "high"
    assert config.emergency_coder.reasoning_effort == "max"
    assert config.worker.emergency_after_failures == 3
    assert config.worker.fallback_model == "kimi-k2.7-code"


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

    assert config.config_version == 10
    assert config.governor.model == "gpt-5.6-sol"
    assert config.governor.reasoning_effort == "high"
    assert config.planner.model == "kimi-k3"
    assert config.worker.model == "deepseek-v4-pro"
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

    assert budget["max_turns"] == 61
    assert budget["exploration_turns"] == 42
    assert budget["existing_files"] == 2
    assert budget["total_lines"] == 770


def test_version_seven_project_limits_are_tripled_once():
    legacy = {
        "config_version": 7,
        "planner": {
            "enabled": True, "provider": "kimi", "model": "kimi-k3",
            "max_turns": 8,
        },
        "worker": {
            "enabled": True, "provider": "deepseek",
            "model": "deepseek-v4-flash", "max_turns": 50,
            "max_exploration_turns": 20, "patch_recovery_turns": 2,
        },
        "complexity_turns": {"simple": 20, "normal": 32, "complex": 48},
        "complexity_exploration": {
            "simple": 12, "normal": 20, "complex": 32,
        },
    }

    upgraded = ProjectAgentConfig.from_dict(legacy)
    reloaded = ProjectAgentConfig.from_dict(upgraded.to_dict())

    assert upgraded.config_version == 10
    assert upgraded.planner.max_turns == 24
    assert upgraded.worker.max_turns == 150
    assert upgraded.worker.model == "deepseek-v4-pro"
    assert upgraded.worker.max_exploration_turns == 60
    assert upgraded.worker.patch_recovery_turns == 6
    assert upgraded.complexity_turns == {
        "simple": 60, "normal": 96, "complex": 144,
    }
    assert upgraded.complexity_exploration == {
        "simple": 36, "normal": 60, "complex": 96,
    }
    assert reloaded.to_dict() == upgraded.to_dict()


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
    # Fixed requirements are immutable and may exceed the soft target by
    # themselves; old tool payloads are summarized while the newest pair stays.
    assert serialized_size < 10_000
    assert any(
        "COMPRESSED TOOL HISTORY" in str(message.get("content") or "")
        for message in compacted
    )


def test_worker_repairs_interleaved_parallel_tool_results():
    messages = [
        {"role": "user", "content": "inspect files"},
        {
            "role": "assistant",
            "content": "parallel reads",
            "tool_calls": [
                {"id": "read-1", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "read-2", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "read-3", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "read-1", "content": "one"},
        {"role": "user", "content": "soft exploration reminder"},
        {"role": "tool", "tool_call_id": "read-2", "content": "two"},
        {"role": "tool", "tool_call_id": "read-3", "content": "three"},
    ]

    repaired = WorkerAgent._repair_tool_message_sequence(messages)

    assert WorkerAgent._tool_message_integrity_errors(repaired) == []
    assert [item["role"] for item in repaired[1:]] == [
        "assistant", "tool", "tool", "tool", "user",
    ]
    assert repaired[-1]["content"] == "soft exploration reminder"


def test_parallel_read_soft_notice_is_sent_after_complete_tool_batch():
    class ParallelRouter:
        def __init__(self):
            self.calls = 0
            self.second_messages = []

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            messages = _args[2]
            if self.calls == 1:
                return {
                    "content": "Reading three relevant files.",
                    "tool_calls": [
                        {
                            "id": f"read-{number}",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": f"file-{number}.js"}),
                            },
                        }
                        for number in range(1, 4)
                    ],
                    "usage": {},
                }
            self.second_messages = list(messages)
            assert WorkerAgent._tool_message_integrity_errors(messages) == []
            return {
                "content": "Analysis report with complete findings.",
                "tool_calls": [],
                "usage": {},
            }

    async def scenario():
        router = ParallelRouter()
        worker = WorkerAgent(
            router, _RecordingBroker(), max_turns=4,
            max_exploration_turns=2,
        )
        result = await worker.run(_task("analysis"), project_root=".")

        assert result["status"] == "completed"
        roles = [message["role"] for message in router.second_messages]
        assistant_index = roles.index("assistant")
        assert roles[assistant_index + 1:assistant_index + 4] == [
            "tool", "tool", "tool",
        ]
        assert roles[assistant_index + 4] == "user"
        assert "soft threshold" in router.second_messages[-1]["content"]

    asyncio.run(scenario())


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


def test_budget_error_pauses_for_user_confirmation_without_escalation(tmp_path):
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

        assert result["status"] == "needs_user_action"
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


def test_valid_resumed_artifact_skips_another_model_execution(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Resume", str(tmp_path))
            job = repos["job"].create("JOB-RESUMED-ARTIFACT", project.id, "继续")
            task = repos["task"].create(
                "T004", job.id, "继续玩家系统", task_type="coding",
                allowed_paths=["game.js"],
            )
            (tmp_path / "game.js").write_text("const resumed = true;\n")
            result, validation = await engine._validate_resumed_artifact(
                task, job, repos, str(tmp_path), {}, ["game.js"],
            )

            assert result["status"] == "pending_validation"
            assert result["resumed_artifact"] is True
            assert "error" not in result
            assert result["completion_note"] == "恢复的任务产物已通过确定性验收"
            assert validation["status"] == "passed"
            event = engine.event_bus.get_history("task_pending_validation")[-1]
            assert event["data"]["resumed_files"] == ["game.js"]
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_completed_result_with_legacy_error_text_is_not_a_job_failure():
    summary = Engine._execution_failure_summary({
        "T001": {
            "status": "completed",
            "error": "恢复的任务产物已通过确定性验收",
        },
    })

    assert summary["failed"] == []
    assert summary["direct_failures"] == []


def test_resume_progress_layout_uses_original_plan_positions():
    tasks = [
        SimpleNamespace(task_id=f"T{index:03d}")
        for index in range(1, 11)
    ]

    positions, total = Engine._task_progress_layout(tasks, tasks[3:], 0)

    assert positions["T004"] == 4
    assert positions["T010"] == 10
    assert total == 10


def test_worker_uses_real_token_ratio_as_soft_pressure_not_a_read_ban():
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
        assert not getattr(task, "_rockcore_finalization_mode", False)
        event = Router.event_bus.get_history("task_budget_pressure")[-1]
        assert event["data"]["hard_blocked"] is False

    asyncio.run(scenario())

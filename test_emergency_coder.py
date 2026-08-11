"""Regression coverage for emergency-coder routing."""

import asyncio
from types import SimpleNamespace

from agents.emergency_coder import EmergencyCoderAgent


class _Router:
    def __init__(self):
        self.calls = []

    async def chat(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {
            "content": (
                '{"summary":"fixed","changes":[],"fix_success":true,'
                '"remaining_issues":[]}'
            ),
        }


def test_emergency_coder_passes_agent_type_only_once():
    async def scenario():
        router = _Router()
        agent = EmergencyCoderAgent(router)
        task = SimpleNamespace(
            task_id="T003", title="Repair UI", description="Update the page"
        )

        result = await agent.run(task, project=SimpleNamespace(root_path="/tmp/demo"))

        assert result["fix_success"] is True
        assert router.calls[0][0][0] == "emergency_coder"
        assert "agent_type" not in router.calls[0][1]

    asyncio.run(scenario())


def test_emergency_coder_prefers_explicit_task_worktree():
    async def scenario():
        router = _Router()
        agent = EmergencyCoderAgent(router)
        task = SimpleNamespace(
            task_id="T003", title="Repair UI", description="Update the page"
        )

        result = await agent.run(
            task,
            project=SimpleNamespace(root_path="/project/root"),
            project_root="/project/root/.ai/worktrees/T003",
        )

        assert result["fix_success"] is True
        args, kwargs = router.calls[0]
        assert kwargs["project_root"] == "/project/root/.ai/worktrees/T003"
        assert "/project/root/.ai/worktrees/T003" in args[2][0]["content"]

    asyncio.run(scenario())

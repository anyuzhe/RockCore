"""Codex Emergency Coder Agent — L3 escalation with workspace_write sandbox."""

import json
import logging
from typing import Any

from orchestrator.model_router import ModelRouter

logger = logging.getLogger(__name__)

EMERGENCY_SYSTEM_PROMPT = """You are the Emergency Coder agent in an AI Engineering Studio.
You operate through the Codex SDK with workspace_write sandbox mode.

Your role is to FIX failing tasks that the regular Worker could not complete.

You have WRITE access to the workspace. You can:
1. Read files to understand the issue
2. Write patches to fix bugs
3. Run tests to verify your fix
4. Check git diff to see your changes

Rules:
1. Read the error message and task description carefully
2. Understand what went wrong before making changes
3. Make minimal, targeted fixes
4. Run the acceptance command to verify your fix
5. If you cannot fix it, report clearly what's blocking

Output ONLY valid JSON with this structure:
{
  "summary": "What was fixed and how",
  "changes": [{"file": "path/to/file.py", "change": "what changed"}],
  "fix_success": true|false,
  "remaining_issues": ["issue1", "issue2"]
}
"""


class EmergencyCoderAgent:
    """Codex Emergency Coder: workspace_write access for L3 escalation."""

    def __init__(self, model_router: ModelRouter, tool_broker=None):
        self.model_router = model_router
        self.tool_broker = tool_broker
        self.agent_type = "emergency_coder"

    async def run(
        self,
        task,
        project=None,
        previous_error="",
        project_root: str | None = None,
    ) -> dict:
        """Run the Emergency Coder in the caller-selected task workspace."""
        logger.info(
            f"Emergency Coder: fixing task {task.task_id}: {task.title}"
        )

        # The engine passes the task worktree explicitly. Falling back to the
        # project root is retained only for standalone callers and older
        # integrations; task execution must never silently leave its worktree.
        effective_root = project_root or (
            project.root_path if project else "."
        )
        recovery_context = str(
            getattr(task, "_rockcore_recovery_context", "") or ""
        )[:16000]

        context = f"""
Task: {task.task_id} - {task.title}
Description: {task.description}
Previous Error: {previous_error}
Project Root: {effective_root}

Authoritative recovery context (constraints, prior reads/edits, validation,
and the exact remaining requirement):
{recovery_context or "(not available)"}

The regular Worker failed on this task. Fix the issue.
Read the relevant files, understand the error, and apply a fix.
Run the acceptance command to verify.
"""

        messages = [{"role": "user", "content": context}]

        try:
            response = await self.model_router.chat(
                self.agent_type,
                EMERGENCY_SYSTEM_PROMPT,
                messages,
                project_root=effective_root,
                attachments=(
                    getattr(getattr(task, "job", None), "attachments", None)
                    or []
                ),
            )

            content = response.get("content", "{}")
            result = self._parse_json(content)

            result.setdefault("summary", "Emergency fix applied")
            result.setdefault("changes", [])
            # Never claim success when the model omitted an explicit result.
            result.setdefault("fix_success", False)
            result.setdefault("remaining_issues", [])

            logger.info(
                f"Emergency Coder: fix_success={result['fix_success']}"
            )
            return {"status": "completed" if result["fix_success"] else "failed", **result}

        except Exception as e:
            logger.error(f"Emergency Coder failed: {e}")
            return {
                "status": "failed",
                "summary": f"Emergency coder error: {e}",
                "fix_success": False,
                "changes": [],
                "remaining_issues": [str(e)],
            }

    def _parse_json(self, content: str) -> dict:
        content = content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        return json.loads(content)

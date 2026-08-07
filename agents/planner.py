"""Kimi Planner Agent — creates task plans within Constitution constraints."""

import json
import logging
from typing import Any

from orchestrator.model_router import ModelRouter

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are the Planner agent in an AI Engineering Studio.
Your role is to create a detailed task plan that respects the Constitution.

Rules:
1. Each task must be concrete and executable
2. Task types: analysis, coding, testing, review
3. Tasks can depend on other tasks
4. Each task must specify allowed file paths
5. Do NOT suggest modifying protected paths
6. Keep tasks small and focused — one task should do one thing
7. Maximum 10 tasks per plan
8. ALL file paths MUST be relative to project_root. NEVER output absolute paths.
   Correct: "index.html", "src/main.py"
   WRONG: "/Users/xxx/project/index.html", "C:\\Users\\xxx\\..."
9. Do NOT create a separate review task just to read files, inspect a diff, check
   HTML structure, or run "git diff". The studio validates those locally.
10. Do NOT use git commands as acceptance_command. Use a real project test command
    when one exists; otherwise leave acceptance_command empty for local validation.
11. Analysis tasks are read-only by default. Their final written analysis is the
    deliverable, so do not require them to create or modify project files.

Output ONLY valid JSON with this structure:
{
  "summary": "Brief plan summary",
  "tasks": [
    {
      "id": "T001",
      "title": "Task title",
      "type": "analysis|coding|testing|review",
      "description": "What to do",
      "dependencies": [],
      "allowed_paths": ["relative/path/glob", "*.html", "src/**/*.py"],
      "acceptance_command": "pytest ..."
    }
  ]
}
"""


class PlannerAgent:
    """Kimi Planner: creates task DAG within Constitution bounds."""

    def __init__(self, model_router: ModelRouter, context_manager=None):
        self.model_router = model_router
        self.context_manager = context_manager
        self.agent_type = "planner"

    async def run(self, job, constitution=None, continuation_context: str = "") -> dict:
        """Run the Planner to produce a task plan."""
        logger.info(f"Planner: planning job {job.job_id}")

        # Inject project memory context
        memory_context = ""
        if self.context_manager:
            memory_context = self.context_manager.get_full_context()
            if memory_context:
                memory_context = f"\n\nProject Knowledge:\n{memory_context}\n"

        constitution_text = "No constitution defined."
        if constitution:
            constitution_text = f"""
Goal: {constitution.goal}
Constraints: {json.dumps(constitution.constraints, indent=2)}
Acceptance Criteria: {json.dumps(constitution.acceptance_criteria, indent=2)}
Risk: {constitution.risk}
Protected Paths: {json.dumps(constitution.protected_paths, indent=2)}
"""

        messages = [
            {
                "role": "user",
                "content": f"""Create a task plan for this job.

Job: {job.job_id}
User Request: {job.user_request}
{continuation_context}

Constitution:
{constitution_text}
{memory_context}
Create a plan with concrete, executable tasks.
Each task should have a clear purpose and acceptance criteria.
Tasks must NOT modify protected paths.
Analysis tasks should come before coding tasks.
Testing tasks should come after coding tasks.

Output ONLY valid JSON."""
            }
        ]

        try:
            response = await self.model_router.chat(
                self.agent_type,
                PLANNER_SYSTEM_PROMPT,
                messages,
                response_format={"type": "json_object"},
            )

            content = response.get("content", "{}")
            plan = self._parse_json(content)

            plan.setdefault("summary", "")
            plan.setdefault("tasks", [])

            # Assign IDs if missing
            for i, task in enumerate(plan["tasks"]):
                task.setdefault("id", f"T{i+1:03d}")
                task.setdefault("type", "coding")
                task.setdefault("dependencies", [])
                task.setdefault("allowed_paths", [])
                task.setdefault("acceptance_command", "")

            logger.info(f"Planner: created {len(plan['tasks'])} tasks")
            return plan

        except Exception as e:
            logger.error(f"Planner failed: {e}")
            return {
                "summary": f"Plan for: {job.user_request[:100]}",
                "tasks": [
                    {
                        "id": "T001",
                        "title": f"Implement: {job.user_request[:100]}",
                        "type": "coding",
                        "description": job.user_request,
                        "dependencies": [],
                        "allowed_paths": [],
                        "acceptance_command": "",
                    }
                ],
            }

    async def repair_plan(self, job, repair_context: dict) -> dict | None:
        """Create a repair plan for a failed task."""
        logger.info(f"Planner: repair plan for task {repair_context.get('failed_task_id')}")

        messages = [
            {
                "role": "user",
                "content": f"""A task failed and needs a repair plan.

Job: {job.job_id}
Failed Task: {repair_context.get('failed_task_id')} - {repair_context.get('failed_task_title')}
Original Description: {repair_context.get('original_description')}
Error: {repair_context.get('error')}

Create a minimal repair plan to fix this task. Focus on the specific error.
Output ONLY valid JSON with the same structure as a normal plan."""
            }
        ]

        try:
            response = await self.model_router.chat(
                self.agent_type,
                PLANNER_SYSTEM_PROMPT,
                messages,
                response_format={"type": "json_object"},
            )
            content = response.get("content", "{}")
            plan = self._parse_json(content)
            plan.setdefault("summary", "Repair plan")
            plan.setdefault("tasks", [])
            logger.info(f"Planner: repair plan has {len(plan['tasks'])} tasks")
            return plan
        except Exception as e:
            logger.error(f"Repair planner failed: {e}")
            return None

    def _parse_json(self, content: str) -> dict:
        content = content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        return json.loads(content)

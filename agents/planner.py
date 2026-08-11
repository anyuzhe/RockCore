"""Kimi Planner Agent — creates task plans within Constitution constraints."""

import json
import logging
from typing import Any

from orchestrator.cost_engine import BudgetExceededError
from orchestrator.model_router import ModelRouter

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are the Planner agent in an AI Engineering Studio.
Your role is to create a detailed task plan that respects the Constitution.

Rules:
1. Each task must be concrete and executable
2. Task types: analysis, coding, testing, review, action. Use action only for
   an explicitly requested external side effect through MCP (for example,
   creating an issue or PR); it must not stand in for a local code edit.
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
12. Keep each coding task to at most 2-3 independently verifiable behaviors. Split
    state logic, UI wiring, persistence/restart, and integration when they are
    substantial, even if they touch the same file.
13. Dependencies must list only direct prerequisites. Do not repeat every transitive
    dependency on all later tasks.
14. Use at most one read-only analysis task for a small or single-page project.
    Searching and reading the identified code sections belong in the same task.
15. Ground allowed_paths in the Project Knowledge file inventory. Never invent
    conventional src/, app/, components/, pages/, or data/ directories when the
    repository map shows a different structure.
16. If a coding task depends on an analysis task because its exact target is not
    known yet, keep its initial allowed_paths broad enough to include existing
    project files. The orchestrator will narrow them using the analysis report.
17. For a simple, single-file, or already-targeted request, do not create a
    standalone analysis task. Put the focused lookup and edit in one coding task.
18. Do not split HTML/content and CSS/presentation changes when they touch the
    same feature and files; one coding task should implement and verify them.
19. A testing task must either author tests or name a real executable test
    command. Do not create a model task merely to restate local validation.
20. Dependencies are authoritative. If a title or description mentions a task ID,
    that ID must still exist in the final tasks array. Never refer to a removed,
    merged, or renumbered task by its old ID.
21. Select zero to three Skills from the supplied Skill Catalog for each task.
    Do not invent skill names. Skills describe the execution SOP, not dependencies.

Output ONLY valid JSON with this structure:
{
  "summary": "Brief plan summary",
  "tasks": [
    {
      "id": "T001",
      "title": "Task title",
      "type": "analysis|coding|testing|review|action",
      "description": "What to do",
      "dependencies": [],
      "skills": ["simple-edit"],
      "allowed_paths": ["relative/path/glob", "*.html", "src/**/*.py"],
      "acceptance_command": "pytest ..."
    }
  ]
}
"""

REVIEW_REPAIR_SYSTEM_PROMPT = """You are the Planner responsible for deciding
whether issues reported by a read-only code reviewer can be repaired safely.

Decide repairability from the concrete review findings, the original request,
and the project Constitution.

Rules:
1. Return repairable=true when the findings describe concrete local code or test
   changes that a coding worker can make and verify.
2. Return repairable=false when completion requires unavailable credentials,
   missing external information, user-only decisions, protected-path changes,
   or mutually contradictory requirements. Give a specific reason.
3. Set requires_user_action=true only when the user must provide credentials,
   information, authorization, or a product decision. Otherwise set it false.
4. Do not reject a repair merely because it is difficult. Prefer a small,
   focused repair plan when the findings are actionable.
5. When repairable=true, include one to ten executable tasks. Use relative paths,
   direct dependencies only, and real test commands when available.
6. Do not repeat the original implementation. Plan only the changes needed to
   address the review findings and verify them.

Output ONLY valid JSON with this structure:
{
  "repairable": true,
  "requires_user_action": false,
  "reason": "Why the findings can or cannot be completed",
  "plan": {
    "summary": "Brief repair plan summary",
    "tasks": [
      {
        "id": "T001",
        "title": "Task title",
        "type": "coding|testing|analysis",
        "description": "Concrete repair work",
        "dependencies": [],
        "skills": ["bug-fix"],
        "allowed_paths": ["relative/path/or/glob"],
        "acceptance_command": ""
      }
    ]
  }
}
"""


class PlannerAgent:
    """Kimi Planner: creates task DAG within Constitution bounds."""

    def __init__(self, model_router: ModelRouter, context_manager=None,
                 skill_manager=None):
        self.model_router = model_router
        self.context_manager = context_manager
        self.agent_type = "planner"
        self.skill_manager = skill_manager

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
Image Observations: {json.dumps(
    (constitution.raw_output or {}).get("image_observations", []),
    ensure_ascii=False,
    indent=2,
)}
"""

        from app.image_attachments import attachment_context

        image_context = attachment_context(getattr(job, "attachments", None))
        skill_catalog = (
            self.skill_manager.catalog_text()
            if self.skill_manager else "(no skills enabled)"
        )
        messages = [
            {
                "role": "user",
                "content": f"""Create a task plan for this job.

Job: {job.job_id}
User Request: {job.user_request}{image_context}
{continuation_context}

Constitution:
{constitution_text}
{memory_context}
Skill Catalog:
{skill_catalog}

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
                attachments=getattr(job, "attachments", None) or [],
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
                task.setdefault("skills", [])
                task.setdefault("acceptance_command", "")

            logger.info(f"Planner: created {len(plan['tasks'])} tasks")
            return plan

        except BudgetExceededError:
            raise
        except Exception as e:
            logger.error(f"Planner failed: {e}")
            return {
                "summary": f"Plan for: {job.user_request[:100]}",
                "tasks": [
                    {
                        "id": "T001",
                        "title": f"Implement: {job.user_request[:100]}",
                        "type": "coding",
                        "description": job.user_request + image_context,
                        "dependencies": [],
                        "allowed_paths": [],
                        "skills": [],
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

Skill Catalog:
{self.skill_manager.catalog_text() if self.skill_manager else '(no skills enabled)'}

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
                attachments=getattr(job, "attachments", None) or [],
            )
            content = response.get("content", "{}")
            plan = self._parse_json(content)
            plan.setdefault("summary", "Repair plan")
            plan.setdefault("tasks", [])
            logger.info(f"Planner: repair plan has {len(plan['tasks'])} tasks")
            return plan
        except BudgetExceededError:
            raise
        except Exception as e:
            logger.error(f"Repair planner failed: {e}")
            return None

    async def plan_review_repair(self, job, review_result: dict,
                                 constitution=None,
                                 round_number: int = 1) -> dict:
        """Assess a rejected review and produce a focused repair plan."""
        logger.info(
            "Planner: assessing review repair for %s round %s",
            job.job_id, round_number,
        )

        constitution_text = "No constitution defined."
        if constitution:
            constitution_text = json.dumps({
                "goal": constitution.goal,
                "constraints": constitution.constraints or [],
                "acceptance_criteria": constitution.acceptance_criteria or [],
                "protected_paths": constitution.protected_paths or [],
            }, ensure_ascii=False, indent=2)

        messages = [{
            "role": "user",
            "content": f"""Assess whether this rejected review can be repaired.

Job: {job.job_id}
Repair Round: {round_number}
Original Request: {job.user_request}

Review Summary: {review_result.get('summary', '')}
Review Issues:
{json.dumps(review_result.get('issues', []), ensure_ascii=False, indent=2)}
Constraint Violations:
{json.dumps(review_result.get('constraint_violations', []), ensure_ascii=False, indent=2)}
Suggested Actions:
{json.dumps(review_result.get('suggested_actions', []), ensure_ascii=False, indent=2)}

Constitution:
{constitution_text}

Return the repairability decision and, when repairable, a minimal repair plan.
Output ONLY valid JSON.""",
        }]

        try:
            response = await self.model_router.chat(
                self.agent_type,
                REVIEW_REPAIR_SYSTEM_PROMPT,
                messages,
                response_format={"type": "json_object"},
                attachments=getattr(job, "attachments", None) or [],
            )
            decision = self._parse_json(response.get("content", "{}"))
            repairable_value = decision.get("repairable", False)
            if isinstance(repairable_value, str):
                repairable_value = repairable_value.strip().lower() == "true"
            repairable = bool(repairable_value)
            user_action_value = decision.get("requires_user_action", False)
            if isinstance(user_action_value, str):
                user_action_value = (
                    user_action_value.strip().lower() == "true"
                )
            requires_user_action = bool(user_action_value) and not repairable
            reason = str(decision.get("reason") or "").strip()
            plan = decision.get("plan") or {}
            if not isinstance(plan, dict):
                plan = {}
            # Tolerate providers that put the normal plan fields at the top level.
            if not plan.get("tasks") and isinstance(decision.get("tasks"), list):
                plan = {
                    "summary": decision.get("summary", "Review repair plan"),
                    "tasks": decision["tasks"],
                }
            plan.setdefault("summary", "Review repair plan")
            plan.setdefault("tasks", [])

            for index, task in enumerate(plan["tasks"]):
                task.setdefault("id", f"T{index + 1:03d}")
                task.setdefault("type", "coding")
                task.setdefault("dependencies", [])
                task.setdefault("allowed_paths", [])
                task.setdefault("acceptance_command", "")

            if repairable and not plan["tasks"]:
                repairable = False
                reason = (
                    reason + "；" if reason else ""
                ) + "策划者判断问题可修复，但没有给出可执行的修复步骤"
            if not reason:
                reason = (
                    "审核问题具体且可通过本地修改与验证完成"
                    if repairable else
                    "策划者未提供能够安全完成修改的依据"
                )

            return {
                "repairable": repairable,
                "requires_user_action": requires_user_action,
                "reason": reason,
                "plan": plan,
            }
        except BudgetExceededError:
            raise
        except Exception as error:
            logger.error("Review repair assessment failed: %s", error)
            return {
                "repairable": False,
                "requires_user_action": False,
                "reason": f"策划者无法完成可修复性判断：{error}",
                "plan": {"summary": "", "tasks": []},
                "assessment_error": True,
            }

    def _parse_json(self, content: str) -> dict:
        content = content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        return json.loads(content)

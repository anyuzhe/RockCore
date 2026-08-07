"""Codex Governor Agent — defines the Constitution via Codex SDK."""

import json
import logging
from typing import Any

from orchestrator.model_router import ModelRouter

logger = logging.getLogger(__name__)

GOVERNOR_SYSTEM_PROMPT = """You are the Governor agent in an AI Engineering Studio.
You operate through the Codex SDK.

Your role is to define the CONSTITUTION for a software engineering task.

The Constitution defines:
1. GOAL: What the user wants to accomplish
2. CONSTRAINTS: What must NOT be done (max 10 items)
3. ACCEPTANCE_CRITERIA: How to verify success (max 8 items)
4. RISK: low / medium / high
5. PROTECTED_PATHS: File paths that must NOT be modified
6. REQUIRES_FINAL_REVIEW: Whether a final review is needed

Output ONLY valid JSON with this exact structure:
{
  "goal": "string",
  "constraints": ["string", ...],
  "acceptance_criteria": ["string", ...],
  "risk": "low|medium|high",
  "protected_paths": ["string", ...],
  "requires_final_review": true|false
}

Be conservative: if unsure about risk, choose higher. If unsure about protected paths, include them.
"""


class GovernorAgent:
    """Codex Governor: defines constraints and acceptance criteria."""

    def __init__(self, model_router: ModelRouter):
        self.model_router = model_router
        self.agent_type = "governor"

    async def run(self, user_request: str, project=None) -> dict:
        """Run the Governor to produce a Constitution."""
        logger.info(f"Governor (Codex): analyzing request: {user_request[:100]}...")

        project_context = ""
        if project:
            project_context = f"""
Project: {project.name}
Root: {project.root_path}
Description: {project.description}
"""

        messages = [
            {
                "role": "user",
                "content": f"""Analyze this software engineering request and produce a Constitution.

{project_context}

User Request: {user_request}

Think about:
1. What is the core goal?
2. What constraints should we impose?
3. How do we verify success?
4. What files/paths should be protected from modification?
5. What is the risk level?

Output ONLY valid JSON."""
            }
        ]

        try:
            response = await self.model_router.chat(
                self.agent_type,
                GOVERNOR_SYSTEM_PROMPT,
                messages,
            )

            content = response.get("content", "{}")
            constitution = self._parse_json(content)

            constitution.setdefault("goal", user_request)
            constitution.setdefault("constraints", [])
            constitution.setdefault("acceptance_criteria", ["All tests pass"])
            constitution.setdefault("risk", "medium")
            constitution.setdefault("protected_paths", [])
            constitution.setdefault("requires_final_review", True)

            logger.info(f"Governor: constitution created (risk={constitution['risk']})")
            return constitution

        except Exception as e:
            logger.error(f"Governor (Codex) failed: {e}")
            raise

    def _parse_json(self, content: str) -> dict:
        content = content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        return json.loads(content)

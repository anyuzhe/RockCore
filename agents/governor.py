"""Codex Governor Agent — defines the Constitution via Codex SDK."""

import json
import logging
from typing import Any

from orchestrator.model_router import ModelRouter

logger = logging.getLogger(__name__)

GOVERNOR_SYSTEM_PROMPT = """You are the Governor agent in an AI Engineering Studio.
You operate through the Codex SDK.

Your role is to define the CONSTITUTION for a software engineering task.

The Constitution and workflow risk assessment define:
1. GOAL: What the user wants to accomplish
2. CONSTRAINTS: What must NOT be done (max 10 items)
3. ACCEPTANCE_CRITERIA: How to verify success (max 8 items)
4. RISK: low / medium / high
5. RISK_SCORE: 0-100
6. RISK_REASONS: Concrete reasons for the classification
7. PROTECTED_PATHS: File paths that must NOT be modified
8. REQUIRES_FINAL_REVIEW: Whether a final review is needed

Output ONLY valid JSON with this exact structure:
{
  "goal": "string",
  "constraints": ["string", ...],
  "acceptance_criteria": ["string", ...],
  "risk": "low|medium|high",
  "risk_score": 0,
  "risk_reasons": ["string", ...],
  "protected_paths": ["string", ...],
  "requires_final_review": true|false
}

Risk rubric:
- low (0-30): documentation, copy, styling, or a narrow behavior-preserving edit
- medium (31-60): localized feature or bug fix with bounded behavioral impact
- high (61-100): auth/security, database/schema/migration, billing/payment,
  public API compatibility, dependencies/build, concurrency, destructive actions,
  broad refactors, or unclear changes with a large blast radius

Assess semantic impact, not isolated keywords. For example, deleting one typo is
not destructive system behavior. Be conservative when the actual blast radius is
unclear. If unsure about protected paths, include them.
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
6. What concrete facts justify the risk score and level?

Output ONLY valid JSON."""
            }
        ]

        try:
            response = await self.model_router.chat(
                self.agent_type,
                GOVERNOR_SYSTEM_PROMPT,
                messages,
                project_root=project.root_path if project else ".",
            )

            content = response.get("content", "{}")
            constitution = self._parse_json(content)

            constitution.setdefault("goal", user_request)
            constitution.setdefault("constraints", [])
            constitution.setdefault("acceptance_criteria", ["All tests pass"])
            constitution.setdefault("risk", "medium")
            constitution.setdefault(
                "risk_score",
                {"low": 20, "medium": 50, "high": 80}.get(
                    str(constitution.get("risk", "medium")).lower(), 50
                ),
            )
            constitution.setdefault("risk_reasons", [
                "未提供明确风险说明，按中风险处理"
            ])
            constitution.setdefault("protected_paths", [])
            constitution.setdefault("requires_final_review", True)
            self._normalize_risk_assessment(constitution)

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

    @staticmethod
    def _normalize_risk_assessment(constitution: dict):
        """Keep risk score and level coherent, choosing the safer result."""
        try:
            score = max(0, min(100, int(constitution.get("risk_score", 50))))
        except (TypeError, ValueError, OverflowError):
            score = 50
        score_level = "low" if score <= 30 else "medium" if score <= 60 else "high"
        explicit = str(constitution.get("risk", "medium") or "medium").lower()
        if explicit not in {"low", "medium", "high"}:
            explicit = "medium"
        order = {"low": 0, "medium": 1, "high": 2}
        level = max((explicit, score_level), key=order.get)
        if level == "medium":
            score = max(score, 31)
        elif level == "high":
            score = max(score, 61)
        reasons = constitution.get("risk_reasons")
        if not isinstance(reasons, list):
            reasons = [str(reasons)] if reasons else []
        constitution["risk"] = level
        constitution["risk_score"] = score
        constitution["risk_reasons"] = [
            str(reason)[:300] for reason in reasons if str(reason).strip()
        ][:6] or ["裁决者按变更影响范围评估"]
        if level == "high":
            constitution["requires_final_review"] = True

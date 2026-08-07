"""Risk Engine — scores task risk from 0-100 for V6 smart routing."""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HIGH_RISK_PATHS = [
    "db", "database", "migration", "schema",
    "auth", "security", "oauth", "login",
    "payment", "billing", "checkout",
    "core", "kernel", "engine",
]

HIGH_RISK_EXTENSIONS = {".py", ".js", ".ts", ".java", ".go", ".rs"}


class RiskEngine:
    """Evaluates task risk based on file changes, module type, and history.

    Risk score (0-100):
    - 0-30: Low risk (docs, config, tests)
    - 31-60: Medium risk (feature code, non-core changes)
    - 61-80: High risk (core modules, database)
    - 81-100: Critical risk (auth, security, payment)
    """

    def __init__(self):
        self._history: dict[str, list[dict]] = {}

    async def evaluate_task(self, task) -> int:
        """Score a task's risk level from 0-100."""
        score = 0
        reasons = []

        # Factor 1: Task type (0-30)
        type_risk = {
            "analysis": 5,
            "testing": 10,
            "review": 5,
            "coding": 20,
            "refactor": 25,
            "database": 30,
        }
        score += type_risk.get(task.task_type, 15)
        reasons.append(f"type={task.task_type}")

        # Factor 2: Allowed paths (0-30)
        for path in (task.allowed_paths or []):
            path_lower = path.lower()
            for keyword in HIGH_RISK_PATHS:
                if keyword in path_lower:
                    score += 15
                    reasons.append(f"path_risk={path}")
                    break
            ext = Path(path).suffix.lower()
            if ext in HIGH_RISK_EXTENSIONS:
                score += 5
                reasons.append(f"source_code={path}")

        # Factor 3: File count (0-20)
        file_count = len(task.allowed_paths or [])
        if file_count > 10:
            score += 15
            reasons.append(f"many_files={file_count}")
        elif file_count > 5:
            score += 10
            reasons.append(f"moderate_files={file_count}")

        # Factor 4: Historical failure rate (0-20)
        task_id = getattr(task, "task_id", str(id(task)))
        if task_id in self._history:
            failed = sum(1 for r in self._history[task_id] if r.get("status") == "failed")
            total = len(self._history[task_id])
            if total > 0:
                fail_rate = failed / total
                score += int(fail_rate * 20)
                reasons.append(f"history_fail_rate={fail_rate:.0%}")

        final_score = min(score, 100)
        logger.debug(f"Risk score {final_score} for {task_id}: {reasons}")
        return final_score

    async def record_result(self, task_id: str, result: dict):
        """Record task execution result for future risk scoring."""
        self._history.setdefault(task_id, []).append(result)

    def get_risk_level(self, score: int) -> str:
        if score <= 30:
            return "low"
        elif score <= 60:
            return "medium"
        elif score <= 80:
            return "high"
        return "critical"

    def get_suggested_model(self, score: int) -> str:
        """Suggest a model based on risk score."""
        if score <= 30:
            return "worker"  # Fast Flash
        elif score <= 60:
            return "worker"  # Flash with retry
        elif score <= 80:
            return "planner"  # Kimi for complex tasks
        return "emergency_coder"  # Codex for critical
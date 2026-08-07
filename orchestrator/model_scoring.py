"""Model Scoring — tracks model success rates across task types for V6."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCORE_PATH = Path.home() / ".ai_engineering_studio" / "model_scores.json"


class ModelScoring:
    """Records and reports model performance across task types.

    Tracks per (model, task_type):
    - Success/failure counts
    - Token usage
    - Execution time
    - Review issues found
    """

    def __init__(self):
        self._data = self._load()

    def _load(self) -> dict:
        if SCORE_PATH.exists():
            try:
                return json.loads(SCORE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"models": {}, "task_types": {}}

    def _save(self):
        SCORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCORE_PATH.write_text(json.dumps(self._data, indent=2))

    async def record_run(self, agent_type: str, task_type: str,
                         success: bool, input_tokens: int = 0,
                         output_tokens: int = 0, duration_ms: int = 0,
                         review_issues: int = 0):
        """Record a model run result."""
        models = self._data["models"]
        if agent_type not in models:
            models[agent_type] = {
                "runs": 0, "successes": 0, "failures": 0,
                "total_input_tokens": 0, "total_output_tokens": 0,
                "total_duration_ms": 0, "total_review_issues": 0,
                "by_task_type": {},
            }

        m = models[agent_type]
        m["runs"] += 1
        if success:
            m["successes"] += 1
        else:
            m["failures"] += 1
        m["total_input_tokens"] += input_tokens
        m["total_output_tokens"] += output_tokens
        m["total_duration_ms"] += duration_ms
        m["total_review_issues"] += review_issues

        # Per task type breakdown
        if task_type not in m["by_task_type"]:
            m["by_task_type"][task_type] = {
                "runs": 0, "successes": 0, "failures": 0,
            }
        tt = m["by_task_type"][task_type]
        tt["runs"] += 1
        if success:
            tt["successes"] += 1
        else:
            tt["failures"] += 1

        self._data["last_updated"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def get_success_rate(self, agent_type: str, task_type: str | None = None) -> float:
        """Get success rate for a model, optionally filtered by task type."""
        model = self._data["models"].get(agent_type)
        if not model or model["runs"] == 0:
            return 0.0

        if task_type:
            tt = model["by_task_type"].get(task_type)
            if not tt or tt["runs"] == 0:
                return 0.0
            return tt["successes"] / tt["runs"]

        return model["successes"] / model["runs"]

    def get_best_model(self, task_type: str) -> str | None:
        """Get the model with the highest success rate for a task type."""
        best_agent = None
        best_rate = 0.0

        for agent_type, model in self._data["models"].items():
            tt = model.get("by_task_type", {}).get(task_type)
            if tt and tt["runs"] >= 3:  # Minimum sample size
                rate = tt["successes"] / tt["runs"]
                if rate > best_rate:
                    best_rate = rate
                    best_agent = agent_type

        return best_agent

    def get_summary(self) -> str:
        """Get formatted summary of all model scores."""
        lines = ["Model Performance Summary:"]
        for agent_type, model in self._data["models"].items():
            rate = (model["successes"] / model["runs"] * 100) if model["runs"] > 0 else 0
            avg_tokens = (model["total_input_tokens"] + model["total_output_tokens"]) / max(model["runs"], 1)
            avg_duration = model["total_duration_ms"] / max(model["runs"], 1)
            lines.append(
                f"  {agent_type}: {model['runs']} runs, "
                f"{rate:.0f}% success, "
                f"{avg_tokens:.0f} tokens/run, "
                f"{avg_duration:.0f}ms avg"
            )

            for tt, stats in model.get("by_task_type", {}).items():
                trate = (stats["successes"] / stats["runs"] * 100) if stats["runs"] > 0 else 0
                lines.append(f"    - {tt}: {stats['runs']} runs, {trate:.0f}% success")

        return "\n".join(lines) if len(lines) > 1 else "No data yet"
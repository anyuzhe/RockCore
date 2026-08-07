"""Cost Engine — budget control for V6 smart scheduling."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class JobBudget:
    """Budget limits for a single job."""
    max_total_tokens: int = 1_000_000
    max_input_tokens: int = 500_000
    max_output_tokens: int = 100_000
    max_api_calls: int = 100
    max_cost_usd: float = 0.50


@dataclass
class UsageRecord:
    agent_type: str
    input_tokens: int
    output_tokens: int
    timestamp: str = ""


class CostEngine:
    """Tracks token usage and enforces budget limits per job.

    Model costs (approximate per 1K tokens):
    - Codex (GPT-4o): $0.01 input / $0.03 output
    - Kimi K2.6: $0.002 input / $0.008 output
    - DeepSeek V4 Flash: $0.0005 input / $0.002 output
    """

    MODEL_COSTS = {
        "governor": {"input": 0.010, "output": 0.030},
        "reviewer": {"input": 0.010, "output": 0.030},
        "emergency_coder": {"input": 0.010, "output": 0.030},
        "planner": {"input": 0.002, "output": 0.008},
        "worker": {"input": 0.0005, "output": 0.002},
    }

    def __init__(self):
        self._budgets: dict[str, JobBudget] = {}
        self._usage: dict[str, list[UsageRecord]] = {}
        self._default_budget = JobBudget()

    def set_budget(self, job_id: str, budget: JobBudget):
        self._budgets[job_id] = budget
        self._usage[job_id] = []

    def get_budget(self, job_id: str) -> JobBudget:
        return self._budgets.get(job_id, self._default_budget)

    async def record_usage(self, job_id: str, agent_type: str,
                           input_tokens: int = 0, output_tokens: int = 0):
        """Record token usage for a job."""
        if job_id not in self._usage:
            self._usage[job_id] = []
        self._usage[job_id].append(UsageRecord(
            agent_type=agent_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        logger.debug(f"Usage: {job_id} {agent_type} +{input_tokens}i/{output_tokens}o")

    async def check_budget(self, job_id: str) -> tuple[bool, str]:
        """Check if job is within budget. Returns (ok, reason)."""
        budget = self.get_budget(job_id)
        usage = self._usage.get(job_id, [])

        total_input = sum(r.input_tokens for r in usage)
        total_output = sum(r.output_tokens for r in usage)
        total_api = len(usage)

        # Calculate cost
        total_cost = 0.0
        for record in usage:
            costs = self.MODEL_COSTS.get(record.agent_type, {"input": 0.01, "output": 0.03})
            total_cost += (record.input_tokens / 1000) * costs["input"]
            total_cost += (record.output_tokens / 1000) * costs["output"]

        if total_input > budget.max_input_tokens:
            return False, f"Input tokens exceeded: {total_input}/{budget.max_input_tokens}"
        if total_output > budget.max_output_tokens:
            return False, f"Output tokens exceeded: {total_output}/{budget.max_output_tokens}"
        if total_api > budget.max_api_calls:
            return False, f"API calls exceeded: {total_api}/{budget.max_api_calls}"
        if total_cost > budget.max_cost_usd:
            return False, f"Cost exceeded: ${total_cost:.4f}/${budget.max_cost_usd:.4f}"

        return True, f"OK: {total_input}i/{total_output}o, ${total_cost:.4f}"

    def get_usage_summary(self, job_id: str) -> dict:
        """Get usage summary for a job."""
        usage = self._usage.get(job_id, [])
        by_agent: dict[str, dict] = {}
        for record in usage:
            if record.agent_type not in by_agent:
                by_agent[record.agent_type] = {"input": 0, "output": 0, "calls": 0}
            by_agent[record.agent_type]["input"] += record.input_tokens
            by_agent[record.agent_type]["output"] += record.output_tokens
            by_agent[record.agent_type]["calls"] += 1

        total_cost = 0.0
        for agent, data in by_agent.items():
            costs = self.MODEL_COSTS.get(agent, {"input": 0.01, "output": 0.03})
            total_cost += (data["input"] / 1000) * costs["input"]
            total_cost += (data["output"] / 1000) * costs["output"]

        return {
            "by_agent": by_agent,
            "total_input": sum(r.input_tokens for r in usage),
            "total_output": sum(r.output_tokens for r in usage),
            "total_calls": len(usage),
            "total_cost": round(total_cost, 4),
        }
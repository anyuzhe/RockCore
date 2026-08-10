"""Cost Engine — budget control for V6 smart scheduling."""

import logging
from dataclasses import dataclass, replace
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


class BudgetExceededError(RuntimeError):
    """Raised when RockCore's local per-job budget blocks a model call."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"RockCore job budget exceeded: {reason}")


@dataclass
class UsageRecord:
    agent_type: str
    input_tokens: int
    output_tokens: int
    provider: str = ""
    billing_mode: str = "api"
    task_id: str = ""
    timestamp: str = ""


class CostEngine:
    """Tracks token usage and enforces budget limits per job.

    API-price-equivalent costs (approximate per 1K tokens):
    - Codex (GPT-5.6 Sol): $0.005 input / $0.03 output
    - Kimi planner equivalent: $0.002 input / $0.008 output
    - DeepSeek V4 Flash: $0.0005 input / $0.002 output

    The equivalent estimate is shown for all calls. Only usage whose
    ``billing_mode`` represents a separately billed API is enforced against
    ``max_cost_usd``.
    """

    MODEL_COSTS = {
        "governor": {"input": 0.005, "output": 0.030},
        "reviewer": {"input": 0.005, "output": 0.030},
        "emergency_coder": {"input": 0.005, "output": 0.030},
        "planner": {"input": 0.002, "output": 0.008},
        "worker": {"input": 0.0005, "output": 0.002},
    }
    PROVIDER_COSTS = {
        "codex": {"input": 0.005, "output": 0.030},
        "openai": {"input": 0.005, "output": 0.030},
        "kimi": {"input": 0.002, "output": 0.008},
        "deepseek": {"input": 0.0005, "output": 0.002},
    }

    def __init__(self, default_budget: JobBudget | None = None):
        self._budgets: dict[str, JobBudget] = {}
        self._usage: dict[str, list[UsageRecord]] = {}
        self._default_budget = replace(default_budget or JobBudget())
        self._repair_reservations: set[str] = set()
        self._review_reservations: set[str] = set()

    def set_default_budget(self, budget: JobBudget):
        """Apply the user-visible default to future jobs."""
        self._default_budget = replace(budget)

    @staticmethod
    def budget_from_config(config: dict | None) -> JobBudget:
        """Build one coherent budget without hidden input/output ceilings."""
        values = config or {}
        total = max(10_000, int(values.get("max_total_tokens", 1_000_000)))
        return JobBudget(
            max_total_tokens=total,
            # The UI exposes one token limit. Derive component limits from it
            # so a hidden 500k input ceiling cannot contradict a 1m setting.
            max_input_tokens=max(
                10_000, int(values.get("max_input_tokens", total))
            ),
            max_output_tokens=max(
                10_000, int(values.get("max_output_tokens", total))
            ),
            max_api_calls=max(1, int(values.get("max_api_calls", 100))),
            max_cost_usd=max(0.01, float(values.get("max_cost_usd", 0.50))),
        )

    def set_budget(self, job_id: str, budget: JobBudget):
        self._budgets[job_id] = budget
        self._usage[job_id] = []
        self._repair_reservations = {
            key for key in self._repair_reservations
            if not key.startswith(f"{job_id}:repair:")
        }
        self._review_reservations = {
            key for key in self._review_reservations
            if not key.startswith(f"{job_id}:review:")
        }

    def reserve_repair_budget(self, job_id: str, repair_round: int) -> JobBudget:
        """Reserve bounded capacity for one review-repair round."""
        budget = self._ensure_job_budget(job_id)
        reservation_key = f"{job_id}:repair:{repair_round}"
        if reservation_key not in self._repair_reservations:
            budget.max_total_tokens += 400_000
            budget.max_input_tokens += 300_000
            budget.max_output_tokens += 100_000
            budget.max_api_calls += 40
            budget.max_cost_usd += 0.50
            self._repair_reservations.add(reservation_key)
        return budget

    def reserve_review_budget(self, job_id: str, review_round: int) -> JobBudget:
        """Reserve capacity before review so execution cannot starve it."""
        budget = self._ensure_job_budget(job_id)
        reservation_key = f"{job_id}:review:{review_round}"
        if reservation_key not in self._review_reservations:
            budget.max_total_tokens += 200_000
            budget.max_input_tokens += 150_000
            budget.max_output_tokens += 50_000
            budget.max_api_calls += 15
            budget.max_cost_usd += 0.25
            self._review_reservations.add(reservation_key)
        return budget

    def _ensure_job_budget(self, job_id: str) -> JobBudget:
        budget = self._budgets.get(job_id)
        if budget is None:
            budget = replace(self._default_budget)
            self._budgets[job_id] = budget
        return budget

    def get_budget(self, job_id: str) -> JobBudget:
        return self._budgets.get(job_id, self._default_budget)

    async def record_usage(self, job_id: str, agent_type: str,
                           input_tokens: int = 0, output_tokens: int = 0,
                           provider: str = "", task_id: str = "",
                           billing_mode: str = "api"):
        """Record token usage for a job."""
        if job_id not in self._usage:
            self._usage[job_id] = []
        self._usage[job_id].append(UsageRecord(
            agent_type=agent_type,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider=provider or "",
            billing_mode=billing_mode or "api",
            task_id=task_id or "",
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

        # Dollar limits apply only to calls that can be billed through a
        # provider API. ChatGPT-authenticated ``codex exec`` calls still count
        # towards token/call limits and equivalent-cost reporting, but they do
        # not consume the Platform API cost budget.
        billable_cost = sum(
            self.estimate_billable_cost(
                record.agent_type,
                record.input_tokens,
                record.output_tokens,
                record.provider,
                record.billing_mode,
            )
            for record in usage
        )

        total_tokens = total_input + total_output
        if total_tokens > budget.max_total_tokens:
            return False, (
                f"Total tokens exceeded: {total_tokens}/{budget.max_total_tokens}"
            )
        if total_input > budget.max_input_tokens:
            return False, f"Input tokens exceeded: {total_input}/{budget.max_input_tokens}"
        if total_output > budget.max_output_tokens:
            return False, f"Output tokens exceeded: {total_output}/{budget.max_output_tokens}"
        if total_api > budget.max_api_calls:
            return False, f"API calls exceeded: {total_api}/{budget.max_api_calls}"
        if billable_cost > budget.max_cost_usd:
            return False, (
                "Billable API cost exceeded: "
                f"${billable_cost:.4f}/${budget.max_cost_usd:.4f}"
            )

        return True, (
            f"OK: {total_input}i/{total_output}o, "
            f"${billable_cost:.4f} billable API estimate"
        )

    async def check_task_budget(self, job_id: str, task_id: str,
                                max_input_tokens: int = 0) -> tuple[bool, str]:
        """Bound one task so it cannot consume the entire job budget."""
        if not task_id or max_input_tokens <= 0:
            return True, "No task budget"
        used = sum(
            record.input_tokens
            for record in self._usage.get(job_id, [])
            if record.task_id == task_id
        )
        if used > max_input_tokens:
            return False, f"Task input tokens exceeded: {used}/{max_input_tokens}"
        return True, f"Task OK: {used}/{max_input_tokens}"

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

        equivalent_cost = sum(
            self.estimate_cost(
                record.agent_type,
                record.input_tokens,
                record.output_tokens,
                record.provider,
            )
            for record in usage
        )
        billable_cost = sum(
            self.estimate_billable_cost(
                record.agent_type,
                record.input_tokens,
                record.output_tokens,
                record.provider,
                record.billing_mode,
            )
            for record in usage
        )

        return {
            "by_agent": by_agent,
            "total_input": sum(r.input_tokens for r in usage),
            "total_output": sum(r.output_tokens for r in usage),
            "total_calls": len(usage),
            # Keep total_cost as a backwards-compatible alias for the
            # equivalent estimate used by existing callers.
            "total_cost": round(equivalent_cost, 4),
            "equivalent_cost": round(equivalent_cost, 4),
            "billable_cost": round(billable_cost, 4),
        }

    @classmethod
    def _cost_rates(cls, agent_type: str, provider: str = "") -> dict:
        return cls.PROVIDER_COSTS.get(
            (provider or "").lower(),
            cls.MODEL_COSTS.get(agent_type, {"input": 0.01, "output": 0.03}),
        )

    @classmethod
    def estimate_cost(cls, agent_type: str, input_tokens: int = 0,
                      output_tokens: int = 0, provider: str = "") -> float:
        """Estimate an API-price-equivalent USD value for model usage."""
        rates = cls._cost_rates(agent_type, provider)
        return round(
            (max(0, input_tokens) / 1000) * rates["input"]
            + (max(0, output_tokens) / 1000) * rates["output"],
            6,
        )

    @staticmethod
    def is_billable_api_mode(billing_mode: str = "api") -> bool:
        """Whether a successful call consumes a separately billed API."""
        return (billing_mode or "api").lower() != "chatgpt_cli"

    @classmethod
    def estimate_billable_cost(cls, agent_type: str,
                               input_tokens: int = 0,
                               output_tokens: int = 0,
                               provider: str = "",
                               billing_mode: str = "api") -> float:
        """Estimate cost that should count against the paid-API budget."""
        if not cls.is_billable_api_mode(billing_mode):
            return 0.0
        return cls.estimate_cost(
            agent_type, input_tokens, output_tokens, provider
        )

"""Cost Engine — budget control for V6 smart scheduling."""

import asyncio
import logging
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class JobBudget:
    """Adaptive safety limits plus one user-authorized hard cost ceiling."""
    max_total_tokens: int = 5_000_000
    max_input_tokens: int = 5_000_000
    max_output_tokens: int = 5_000_000
    max_api_calls: int = 500
    max_auto_total_tokens: int = 50_000_000
    max_auto_input_tokens: int = 50_000_000
    max_auto_output_tokens: int = 50_000_000
    max_auto_api_calls: int = 5_000
    cached_input_weight: float = 0.15
    max_cost_cny: float = 10.00


class BudgetExceededError(RuntimeError):
    """Raised when RockCore's local per-job budget blocks a model call."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"RockCore job budget exceeded: {reason}")


@dataclass
class UsageRecord:
    agent_type: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    provider: str = ""
    model_name: str = ""
    billing_mode: str = "api"
    task_id: str = ""
    timestamp: str = ""


class CostEngine:
    """Tracks token usage and enforces budget limits per job.

    Prices are CNY per one million tokens and distinguish cached input,
    ordinary input, and output. Kimi and DeepSeek prices follow the user-
    supplied RMB price table. Codex is a RMB equivalent converted from its
    USD reference price; ChatGPT-authenticated calls remain non-billable.

    The equivalent estimate is shown for all calls. Only usage whose
    ``billing_mode`` represents a separately billed API is enforced against
    ``max_cost_cny``.
    """

    CURRENCY = "CNY"
    LEGACY_USD_TO_CNY = 7.20
    DEFAULT_MAX_COST_CNY = 10.00
    BEIJING_TIMEZONE = timezone(timedelta(hours=8))
    DEEPSEEK_OFF_PEAK_MULTIPLIER = 0.5
    MODEL_PRICES_CNY_PER_MILLION = {
        # DeepSeek publishes peak prices for 09:00-12:00 and 14:00-18:00
        # Beijing time. All other hours are charged at half these rates.
        "deepseek-v4-flash-0731": {
            "cached_input": 0.10, "input": 3.00, "output": 9.00,
        },
        "deepseek-v4-flash": {
            "cached_input": 0.10, "input": 3.00, "output": 9.00,
        },
        "deepseek-v4-pro-0813": {
            "cached_input": 0.30, "input": 9.00, "output": 27.00,
        },
        "deepseek-v4-pro": {
            "cached_input": 0.30, "input": 9.00, "output": 27.00,
        },
        "kimi-k2.6": {
            "cached_input": 1.10, "input": 6.50, "output": 27.00,
        },
        "kimi-k2.7-code": {
            "cached_input": 1.30, "input": 6.50, "output": 27.00,
        },
        # Kept only for pricing historical records created before the model-ID
        # migration. New requests use kimi-k2.7-code.
        "kimi-k2.7": {
            "cached_input": 1.30, "input": 6.50, "output": 27.00,
        },
        "kimi-k3": {
            "cached_input": 2.00, "input": 20.00, "output": 100.00,
        },
        # GPT-5.6 Sol reference ($0.50/$5/$30) converted at ¥7.20/USD.
        "gpt-5.6-sol": {
            "cached_input": 3.60, "input": 36.00, "output": 216.00,
        },
        "codex-sdk": {
            "cached_input": 3.60, "input": 36.00, "output": 216.00,
        },
    }
    DEFAULT_MODEL_BY_PROVIDER = {
        "codex": "gpt-5.6-sol",
        "openai": "gpt-5.6-sol",
        "kimi": "kimi-k3",
        "deepseek": "deepseek-v4-pro",
    }
    DEFAULT_MODEL_BY_AGENT = {
        "main_agent": "gpt-5.6-sol",
        "main_agent_summary": "gpt-5.6-sol",
        "governor": "gpt-5.6-sol",
        "reviewer": "gpt-5.6-sol",
        "emergency_coder": "gpt-5.6-sol",
        "planner": "kimi-k3",
        "worker": "deepseek-v4-pro",
    }

    def __init__(self, default_budget: JobBudget | None = None):
        self._budgets: dict[str, JobBudget] = {}
        self._usage: dict[str, list[UsageRecord]] = {}
        self._persisted_usage: dict[str, dict[str, float | int]] = {}
        self._default_budget = replace(default_budget or JobBudget())
        self._repair_reservations: set[str] = set()
        self._review_reservations: set[str] = set()
        self._workflow_reservations: set[str] = set()
        self._protected_capacity: dict[str, dict[str, dict[str, int]]] = {}
        self._request_reservations: dict[str, dict[str, dict[str, Any]]] = {}
        self._budget_locks: dict[str, asyncio.Lock] = {}

    def set_default_budget(self, budget: JobBudget):
        """Apply the user-visible default to future jobs."""
        self._default_budget = replace(budget)

    @classmethod
    def budget_from_config(cls, config: dict | None) -> JobBudget:
        """Build one coherent budget without hidden input/output ceilings."""
        values = config or {}
        total = max(100_000, int(values.get("max_total_tokens", 5_000_000)))
        auto_total = max(
            total,
            int(values.get("max_auto_total_tokens", 50_000_000)),
        )
        input_limit = max(
            100_000, int(values.get("max_input_tokens", total))
        )
        output_limit = max(
            100_000, int(values.get("max_output_tokens", total))
        )
        return JobBudget(
            max_total_tokens=total,
            # The UI exposes one token limit. Derive component limits from it
            # so a hidden 500k input ceiling cannot contradict a 1m setting.
            max_input_tokens=input_limit,
            max_output_tokens=output_limit,
            max_api_calls=max(10, int(values.get("max_api_calls", 500))),
            max_auto_total_tokens=auto_total,
            max_auto_input_tokens=max(
                input_limit,
                int(values.get("max_auto_input_tokens", auto_total)),
            ),
            max_auto_output_tokens=max(
                output_limit,
                int(values.get("max_auto_output_tokens", auto_total)),
            ),
            max_auto_api_calls=max(
                max(10, int(values.get("max_api_calls", 500))),
                int(values.get("max_auto_api_calls", 5_000)),
            ),
            cached_input_weight=min(
                1.0,
                max(0.0, float(values.get("cached_input_weight", 0.15))),
            ),
            max_cost_cny=max(0.10, cls._configured_cost_limit(values)),
        )

    @classmethod
    def _configured_cost_limit(cls, values: dict) -> float:
        if "max_cost_cny" in values:
            return float(values.get("max_cost_cny") or cls.DEFAULT_MAX_COST_CNY)
        if "max_cost_usd" in values:
            return round(
                float(values.get("max_cost_usd") or 0.50)
                * cls.LEGACY_USD_TO_CNY,
                2,
            )
        return cls.DEFAULT_MAX_COST_CNY

    def set_budget(self, job_id: str, budget: JobBudget):
        self._budgets[job_id] = budget
        self._usage[job_id] = []
        self._persisted_usage.pop(job_id, None)
        self._repair_reservations = {
            key for key in self._repair_reservations
            if not key.startswith(f"{job_id}:repair:")
        }
        self._review_reservations = {
            key for key in self._review_reservations
            if not key.startswith(f"{job_id}:review:")
        }
        self._workflow_reservations = {
            key for key in self._workflow_reservations
            if not key.startswith(f"{job_id}:workflow:")
        }
        self._request_reservations.pop(job_id, None)
        self._protected_capacity.pop(job_id, None)

    def reserve_repair_budget(self, job_id: str, repair_round: int) -> JobBudget:
        """Reserve bounded capacity for one review-repair round."""
        budget = self._ensure_job_budget(job_id)
        reservation_key = f"{job_id}:repair:{repair_round}"
        if reservation_key not in self._repair_reservations:
            budget.max_total_tokens = min(
                budget.max_auto_total_tokens,
                budget.max_total_tokens + 400_000,
            )
            budget.max_input_tokens = min(
                budget.max_auto_input_tokens,
                budget.max_input_tokens + 300_000,
            )
            budget.max_output_tokens = min(
                budget.max_auto_output_tokens,
                budget.max_output_tokens + 100_000,
            )
            budget.max_api_calls = min(
                budget.max_auto_api_calls, budget.max_api_calls + 40
            )
            self._repair_reservations.add(reservation_key)
        return budget

    def reserve_review_budget(self, job_id: str, review_round: int) -> JobBudget:
        """Reserve capacity before review so execution cannot starve it."""
        budget = self._ensure_job_budget(job_id)
        reservation_key = f"{job_id}:review:{review_round}"
        if reservation_key not in self._review_reservations:
            budget.max_total_tokens = min(
                budget.max_auto_total_tokens,
                budget.max_total_tokens + 200_000,
            )
            budget.max_input_tokens = min(
                budget.max_auto_input_tokens,
                budget.max_input_tokens + 150_000,
            )
            budget.max_output_tokens = min(
                budget.max_auto_output_tokens,
                budget.max_output_tokens + 50_000,
            )
            budget.max_api_calls = min(
                budget.max_auto_api_calls, budget.max_api_calls + 15
            )
            self._review_reservations.add(reservation_key)
        return budget

    def reserve_workflow_budget(
        self, job_id: str, task_input_tokens: int,
        required_api_calls: int = 0, required_output_tokens: int = 0,
        reservation_name: str = "execution",
    ) -> JobBudget:
        """Reserve execution, review, and one repair round before work starts."""
        budget = self._ensure_job_budget(job_id)
        key = f"{job_id}:workflow:{reservation_name}"
        if key in self._workflow_reservations:
            return budget
        task_input_tokens = max(0, int(task_input_tokens or 0))
        required_output_tokens = max(0, int(required_output_tokens or 0))
        # Main Agent/Planner plus Reviewer and one repair cycle cannot be starved
        # by Workers. These are soft safety allocations and never raise RMB.
        phase_input_reserve = 900_000
        phase_output_reserve = 250_000
        phase_call_reserve = 80
        required_input = task_input_tokens + phase_input_reserve
        required_output = required_output_tokens + phase_output_reserve
        required_calls = max(0, int(required_api_calls or 0)) + phase_call_reserve
        required_total = required_input + required_output + 200_000
        self._grow_soft_limits(
            budget,
            required_input=required_input,
            required_output=required_output,
            required_total=required_total,
            required_calls=required_calls,
        )
        self._protected_capacity.setdefault(job_id, {})[key] = {
            "tokens": phase_input_reserve + phase_output_reserve + 200_000,
            "calls": phase_call_reserve,
        }
        self._workflow_reservations.add(key)
        return budget

    def release_workflow_reservations(self, job_id: str) -> None:
        """Release protected review/repair capacity when review begins."""
        self._protected_capacity.pop(job_id, None)

    def reserve_document_budget(self, job_id: str,
                                task_input_tokens: int,
                                required_api_calls: int = 0,
                                required_output_tokens: int = 0) -> JobBudget:
        """Ensure a document task has token headroom without raising cost limits.

        Long-document reading legitimately repeats a sizeable context across
        page ranges. The visible paid-API cost ceiling remains authoritative;
        only token and call safety ceilings are enlarged for this workload.
        """
        budget = self._ensure_job_budget(job_id)
        task_input_tokens = max(0, int(task_input_tokens or 0))
        # ``task_input_tokens`` is an absolute per-task allowance. Add job
        # headroom for Main Agent/Planner/Reviewer rather than relying on the
        # generic one-million-token ceiling.
        required_input = task_input_tokens + 350_000
        output_headroom = max(
            200_000,
            int(required_output_tokens or 0),
            task_input_tokens // 6,
        )
        # Leave enough calls for the document batches plus the surrounding
        # workflow. This is a safety ceiling, not a billable-cost allowance.
        self._grow_soft_limits(
            budget,
            required_input=required_input,
            required_output=output_headroom,
            required_total=required_input + output_headroom + 100_000,
            required_calls=max(int(required_api_calls or 0) + 30, 180),
        )
        return budget

    @staticmethod
    def _grow_value(current: int, required: int, maximum: int) -> int:
        if required <= current:
            return current
        target = max(required, math.ceil(current * 1.5))
        return min(maximum, target)

    def _grow_soft_limits(
        self, budget: JobBudget, *, required_input: int = 0,
        required_output: int = 0, required_total: int = 0,
        required_calls: int = 0,
    ) -> bool:
        before = (
            budget.max_input_tokens, budget.max_output_tokens,
            budget.max_total_tokens, budget.max_api_calls,
        )
        budget.max_input_tokens = self._grow_value(
            budget.max_input_tokens, required_input,
            budget.max_auto_input_tokens,
        )
        budget.max_output_tokens = self._grow_value(
            budget.max_output_tokens, required_output,
            budget.max_auto_output_tokens,
        )
        budget.max_total_tokens = self._grow_value(
            budget.max_total_tokens, required_total,
            budget.max_auto_total_tokens,
        )
        budget.max_api_calls = self._grow_value(
            budget.max_api_calls, required_calls,
            budget.max_auto_api_calls,
        )
        return before != (
            budget.max_input_tokens, budget.max_output_tokens,
            budget.max_total_tokens, budget.max_api_calls,
        )

    def _ensure_job_budget(self, job_id: str) -> JobBudget:
        budget = self._budgets.get(job_id)
        if budget is None:
            budget = replace(self._default_budget)
            self._budgets[job_id] = budget
        return budget

    def get_budget(self, job_id: str) -> JobBudget:
        return self._budgets.get(job_id, self._default_budget)

    def refresh_job_limits(self, job_id: str) -> JobBudget:
        """Apply current user limits to a resumed Job without erasing usage."""
        budget = self._ensure_job_budget(job_id)
        defaults = self._default_budget
        for name in (
            "max_total_tokens", "max_input_tokens", "max_output_tokens",
            "max_api_calls", "max_auto_total_tokens", "max_auto_input_tokens",
            "max_auto_output_tokens", "max_auto_api_calls",
        ):
            setattr(budget, name, max(getattr(budget, name), getattr(defaults, name)))
        budget.cached_input_weight = defaults.cached_input_weight
        budget.max_cost_cny = defaults.max_cost_cny
        return budget

    def restore_persisted_usage(
        self, job_id: str, *, input_tokens: int = 0,
        cached_input_tokens: int = 0, output_tokens: int = 0,
        calls: int = 0, billable_cost: float = 0.0,
    ) -> None:
        """Restore aggregate usage so a resumed Job keeps its hard limits."""
        if job_id in self._persisted_usage:
            return
        input_tokens = max(0, int(input_tokens or 0))
        cached_input_tokens = min(
            input_tokens, max(0, int(cached_input_tokens or 0))
        )
        live = self._usage.get(job_id, [])
        live_input = sum(record.input_tokens for record in live)
        live_cached = sum(record.cached_input_tokens for record in live)
        live_output = sum(record.output_tokens for record in live)
        live_billable = sum(
            self.estimate_billable_cost(
                record.agent_type, record.input_tokens, record.output_tokens,
                record.provider, record.billing_mode,
                record.cached_input_tokens, record.model_name,
                at_time=record.timestamp,
            )
            for record in live
        )
        self._persisted_usage[job_id] = {
            # The current process may still hold the calls that were just
            # persisted. Store only the missing historical prefix.
            "input": max(0, input_tokens - live_input),
            "cached_input": max(0, cached_input_tokens - live_cached),
            "output": max(0, int(output_tokens or 0) - live_output),
            "calls": max(0, int(calls or 0) - len(live)),
            "billable_cost": max(
                0.0, float(billable_cost or 0.0) - live_billable
            ),
        }

    async def record_usage(self, job_id: str, agent_type: str,
                           input_tokens: int = 0,
                           cached_input_tokens: int = 0,
                           output_tokens: int = 0,
                           provider: str = "", model_name: str = "",
                           task_id: str = "",
                           billing_mode: str = "api",
                           reservation_id: str = "",
                           timestamp: str = ""):
        """Record token usage for a job."""
        lock = self._budget_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            if reservation_id:
                self._request_reservations.get(job_id, {}).pop(
                    reservation_id, None
                )
            if job_id not in self._usage:
                self._usage[job_id] = []
            self._usage[job_id].append(UsageRecord(
                agent_type=agent_type,
                input_tokens=input_tokens,
                cached_input_tokens=min(
                    max(0, int(cached_input_tokens or 0)),
                    max(0, int(input_tokens or 0)),
                ),
                output_tokens=output_tokens,
                provider=provider or "",
                model_name=model_name or "",
                billing_mode=billing_mode or "api",
                task_id=task_id or "",
                timestamp=(
                    str(timestamp).strip()
                    or datetime.now(timezone.utc).isoformat()
                ),
            ))
            # Actual usage may be larger than the estimate. Grow soft limits
            # after settlement so the next request is never rejected merely
            # because the provider reported a larger context than expected.
            snapshot = self._usage_totals(job_id)
            self._grow_soft_limits(
                self._ensure_job_budget(job_id),
                required_input=snapshot["effective_input"],
                required_output=snapshot["output"],
                required_total=snapshot["effective_total"],
                required_calls=snapshot["calls"],
            )
        logger.debug(f"Usage: {job_id} {agent_type} +{input_tokens}i/{output_tokens}o")

    async def admit_request(
        self, job_id: str, *, task_id: str = "",
        agent_type: str = "",
        estimated_input_tokens: int = 0, max_output_tokens: int = 0,
        estimated_billable_cost: float = 0.0,
    ) -> dict[str, Any]:
        """Atomically reserve one model request before it reaches a provider."""
        lock = self._budget_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            budget = self._ensure_job_budget(job_id)
            totals = self._usage_totals(job_id)
            pending = list(self._request_reservations.get(job_id, {}).values())
            reserved_input = sum(int(item["input_tokens"]) for item in pending)
            reserved_output = sum(int(item["output_tokens"]) for item in pending)
            reserved_cost = sum(float(item["billable_cost"]) for item in pending)
            reserved_calls = len(pending)
            protected = list(
                self._protected_capacity.get(job_id, {}).values()
            )
            protected_tokens = (
                sum(int(item["tokens"]) for item in protected)
                if agent_type in {"worker", "emergency_coder"}
                else 0
            )
            protected_calls = (
                sum(int(item["calls"]) for item in protected)
                if agent_type in {"worker", "emergency_coder"}
                else 0
            )
            request_input = max(1, int(estimated_input_tokens or 0))
            request_output = max(1, int(max_output_tokens or 0))
            request_cost = max(0.0, float(estimated_billable_cost or 0.0))
            if totals["billable_cost"] + reserved_cost + request_cost > budget.max_cost_cny:
                raise BudgetExceededError(
                    "Billable API hard cost limit would be exceeded: "
                    f"¥{totals['billable_cost'] + reserved_cost + request_cost:.4f}/"
                    f"¥{budget.max_cost_cny:.4f}"
                )
            projected_input = totals["effective_input"] + reserved_input + request_input
            projected_output = totals["output"] + reserved_output + request_output
            projected_total = (
                projected_input + projected_output + protected_tokens
            )
            projected_calls = (
                totals["calls"] + reserved_calls + 1 + protected_calls
            )
            expanded = self._grow_soft_limits(
                budget,
                required_input=math.ceil(projected_input * 1.10),
                required_output=math.ceil(projected_output * 1.10),
                required_total=math.ceil(projected_total * 1.10),
                required_calls=projected_calls + 10,
            )
            if (
                projected_input > budget.max_input_tokens
                or projected_output > budget.max_output_tokens
                or projected_total > budget.max_total_tokens
                or projected_calls > budget.max_api_calls
            ):
                raise BudgetExceededError(
                    "Soft Token auto-expansion ceiling reached; progress was "
                    "preserved for continuation"
                )
            reservation_id = uuid.uuid4().hex
            self._request_reservations.setdefault(job_id, {})[reservation_id] = {
                "task_id": task_id or "",
                "input_tokens": request_input,
                "output_tokens": request_output,
                "billable_cost": request_cost,
            }
            return {
                "reservation_id": reservation_id,
                "expanded": expanded,
                "budget": self.get_budget_snapshot(job_id),
            }

    async def release_request(self, job_id: str, reservation_id: str) -> None:
        if not reservation_id:
            return
        lock = self._budget_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            self._request_reservations.get(job_id, {}).pop(
                reservation_id, None
            )

    def _effective_input(self, budget: JobBudget, records: list[UsageRecord]) -> int:
        ordinary = sum(
            max(0, record.input_tokens - record.cached_input_tokens)
            for record in records
        )
        cached = sum(record.cached_input_tokens for record in records)
        return ordinary + math.ceil(cached * budget.cached_input_weight)

    def _usage_totals(self, job_id: str) -> dict[str, Any]:
        budget = self.get_budget(job_id)
        usage = self._usage.get(job_id, [])
        persisted = self._persisted_usage.get(job_id, {})
        persisted_input = int(persisted.get("input", 0) or 0)
        persisted_cached = min(
            persisted_input,
            int(persisted.get("cached_input", 0) or 0),
        )
        effective_input = (
            persisted_input - persisted_cached
            + math.ceil(persisted_cached * budget.cached_input_weight)
            + self._effective_input(budget, usage)
        )
        output = int(persisted.get("output", 0) or 0) + sum(
            record.output_tokens for record in usage
        )
        billable_cost = float(persisted.get("billable_cost", 0.0) or 0.0) + sum(
            self.estimate_billable_cost(
                record.agent_type, record.input_tokens, record.output_tokens,
                record.provider, record.billing_mode,
                record.cached_input_tokens, record.model_name,
                at_time=record.timestamp,
            )
            for record in usage
        )
        return {
            "effective_input": effective_input,
            "output": output,
            "effective_total": effective_input + output,
            "calls": int(persisted.get("calls", 0) or 0) + len(usage),
            "billable_cost": billable_cost,
        }

    async def check_budget(self, job_id: str) -> tuple[bool, str]:
        """Check if job is within budget. Returns (ok, reason)."""
        budget = self.get_budget(job_id)
        usage = self._usage.get(job_id, [])
        totals = self._usage_totals(job_id)
        total_input = totals["effective_input"]
        total_output = totals["output"]
        total_api = totals["calls"]

        # RMB limits apply only to calls that can be billed through a
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
                record.cached_input_tokens,
                record.model_name,
                at_time=record.timestamp,
            )
            for record in usage
        )

        total_tokens = total_input + total_output
        self._grow_soft_limits(
            budget,
            required_input=total_input,
            required_output=total_output,
            required_total=total_tokens,
            required_calls=total_api,
        )
        if billable_cost > budget.max_cost_cny:
            return False, (
                "Billable API cost exceeded: "
                f"¥{billable_cost:.4f}/¥{budget.max_cost_cny:.4f}"
            )

        return True, (
            f"OK: {total_input}i/{total_output}o, "
            f"¥{billable_cost:.4f} billable API estimate"
        )

    async def check_task_budget(self, job_id: str, task_id: str,
                                max_input_tokens: int = 0) -> tuple[bool, str]:
        """Bound one task so it cannot consume the entire job budget."""
        if not task_id or max_input_tokens <= 0:
            return True, "No task budget"
        used = self.get_task_usage(job_id, task_id)["effective_input_tokens"]
        # Per-task Token limits are progress thresholds, not terminal gates.
        return True, f"Task soft budget: {used}/{max_input_tokens}"

    def get_task_usage(self, job_id: str, task_id: str) -> dict[str, int]:
        """Return task-local usage for continuation and finalization forecasts."""
        records = [
            record for record in self._usage.get(job_id, [])
            if record.task_id == task_id
        ]
        budget = self.get_budget(job_id)
        input_tokens = sum(record.input_tokens for record in records)
        cached_input_tokens = sum(record.cached_input_tokens for record in records)
        effective_input_tokens = self._effective_input(budget, records)
        output_tokens = sum(record.output_tokens for record in records)
        calls = len(records)
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "effective_input_tokens": effective_input_tokens,
            "output_tokens": output_tokens,
            "calls": calls,
            "average_input_tokens": (
                math.ceil(effective_input_tokens / calls) if calls else 0
            ),
        }

    def get_budget_snapshot(self, job_id: str) -> dict[str, Any]:
        """Return live used/reserved/remaining values for UI and checkpoints."""
        budget = self.get_budget(job_id)
        totals = self._usage_totals(job_id)
        pending = list(self._request_reservations.get(job_id, {}).values())
        inflight_tokens = sum(
            int(item["input_tokens"]) + int(item["output_tokens"])
            for item in pending
        )
        protected = list(self._protected_capacity.get(job_id, {}).values())
        protected_tokens = sum(int(item["tokens"]) for item in protected)
        protected_calls = sum(int(item["calls"]) for item in protected)
        reserved_tokens = inflight_tokens + protected_tokens
        reserved_calls = len(pending) + protected_calls
        used = totals["effective_total"]
        return {
            "used_tokens": used,
            "used_effective_input_tokens": totals["effective_input"],
            "used_output_tokens": totals["output"],
            "reserved_tokens": reserved_tokens,
            "inflight_tokens": inflight_tokens,
            "protected_phase_tokens": protected_tokens,
            "remaining_tokens": max(0, budget.max_total_tokens - used - reserved_tokens),
            "soft_token_limit": budget.max_total_tokens,
            "max_auto_tokens": budget.max_auto_total_tokens,
            "used_calls": totals["calls"],
            "reserved_calls": reserved_calls,
            "protected_phase_calls": protected_calls,
            "remaining_calls": max(
                0, budget.max_api_calls - totals["calls"] - reserved_calls
            ),
            "soft_call_limit": budget.max_api_calls,
            "max_auto_calls": budget.max_auto_api_calls,
            "billable_cost": round(totals["billable_cost"], 6),
            "hard_cost_limit_cny": budget.max_cost_cny,
            "cached_input_weight": budget.cached_input_weight,
        }

    def get_usage_summary(self, job_id: str) -> dict:
        """Get usage summary for a job."""
        usage = self._usage.get(job_id, [])
        by_agent: dict[str, dict] = {}
        for record in usage:
            if record.agent_type not in by_agent:
                by_agent[record.agent_type] = {
                    "input": 0, "cached_input": 0, "output": 0, "calls": 0,
                }
            by_agent[record.agent_type]["input"] += record.input_tokens
            by_agent[record.agent_type]["cached_input"] += (
                record.cached_input_tokens
            )
            by_agent[record.agent_type]["output"] += record.output_tokens
            by_agent[record.agent_type]["calls"] += 1

        equivalent_cost = sum(
            self.estimate_cost(
                record.agent_type,
                record.input_tokens,
                record.output_tokens,
                record.provider,
                record.cached_input_tokens,
                record.model_name,
                at_time=record.timestamp,
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
                record.cached_input_tokens,
                record.model_name,
                at_time=record.timestamp,
            )
            for record in usage
        )

        return {
            "by_agent": by_agent,
            "total_input": sum(r.input_tokens for r in usage),
            "total_cached_input": sum(r.cached_input_tokens for r in usage),
            "effective_input": self._effective_input(
                self.get_budget(job_id), usage
            ),
            "total_output": sum(r.output_tokens for r in usage),
            "total_calls": len(usage),
            # Keep total_cost as a backwards-compatible alias for the
            # equivalent estimate used by existing callers.
            "total_cost": round(equivalent_cost, 4),
            "equivalent_cost": round(equivalent_cost, 4),
            "billable_cost": round(billable_cost, 4),
            "currency": self.CURRENCY,
            "budget": self.get_budget_snapshot(job_id),
        }

    @classmethod
    def _cost_rates(
        cls, agent_type: str, provider: str = "", model_name: str = "",
        at_time: datetime | str | None = None,
    ) -> dict:
        normalized = str(model_name or "").strip().lower()
        if normalized not in cls.MODEL_PRICES_CNY_PER_MILLION:
            for candidate in sorted(
                cls.MODEL_PRICES_CNY_PER_MILLION, key=len, reverse=True
            ):
                if normalized.startswith(candidate + "-"):
                    normalized = candidate
                    break
        if normalized not in cls.MODEL_PRICES_CNY_PER_MILLION:
            normalized = cls.DEFAULT_MODEL_BY_PROVIDER.get(
                (provider or "").lower(),
                cls.DEFAULT_MODEL_BY_AGENT.get(agent_type, "deepseek-v4-pro"),
            )
        rates = dict(cls.MODEL_PRICES_CNY_PER_MILLION[normalized])
        if normalized.startswith("deepseek-v4-") and not cls.is_deepseek_peak_period(
            at_time
        ):
            return {
                name: value * cls.DEEPSEEK_OFF_PEAK_MULTIPLIER
                for name, value in rates.items()
            }
        return rates

    @classmethod
    def is_deepseek_peak_period(
        cls, at_time: datetime | str | None = None,
    ) -> bool:
        """Return whether DeepSeek's Beijing-time peak pricing applies."""
        moment: datetime
        if isinstance(at_time, str):
            try:
                moment = datetime.fromisoformat(at_time.replace("Z", "+00:00"))
            except ValueError:
                moment = datetime.now(timezone.utc)
        elif isinstance(at_time, datetime):
            moment = at_time
        else:
            moment = datetime.now(timezone.utc)

        if moment.tzinfo is None:
            beijing = moment.replace(tzinfo=cls.BEIJING_TIMEZONE)
        else:
            beijing = moment.astimezone(cls.BEIJING_TIMEZONE)
        minute = beijing.hour * 60 + beijing.minute
        return (9 * 60 <= minute < 12 * 60) or (
            14 * 60 <= minute < 18 * 60
        )

    @classmethod
    def estimate_cost(cls, agent_type: str, input_tokens: int = 0,
                      output_tokens: int = 0, provider: str = "",
                      cached_input_tokens: int = 0,
                      model_name: str = "",
                      at_time: datetime | str | None = None) -> float:
        """Estimate an API-price-equivalent RMB value for model usage."""
        rates = cls._cost_rates(agent_type, provider, model_name, at_time)
        input_count = max(0, int(input_tokens or 0))
        cached_count = min(
            input_count, max(0, int(cached_input_tokens or 0))
        )
        ordinary_count = input_count - cached_count
        return round(
            (cached_count / 1_000_000) * rates["cached_input"]
            + (ordinary_count / 1_000_000) * rates["input"]
            + (max(0, output_tokens) / 1_000_000) * rates["output"],
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
                               billing_mode: str = "api",
                               cached_input_tokens: int = 0,
                               model_name: str = "",
                               at_time: datetime | str | None = None) -> float:
        """Estimate cost that should count against the paid-API budget."""
        if not cls.is_billable_api_mode(billing_mode):
            return 0.0
        return cls.estimate_cost(
            agent_type, input_tokens, output_tokens, provider,
            cached_input_tokens, model_name, at_time,
        )

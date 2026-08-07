"""Model Router — smart routing with risk, cost, and scoring for V6."""

import logging
import time
from typing import Any

from .risk_engine import RiskEngine
from .cost_engine import CostEngine
from .model_scoring import ModelScoring

logger = logging.getLogger(__name__)

# Agent type → model selection strategy
ROUTING_STRATEGY = {
    "governor": {"provider": "codex", "priority": "accuracy"},
    "planner": {"provider": "kimi", "priority": "reasoning"},
    "worker": {"provider": "deepseek", "priority": "speed"},
    "reviewer": {"provider": "codex", "priority": "accuracy"},
    "emergency_coder": {"provider": "codex", "priority": "write_access"},
}


class ModelRouter:
    """Routes requests to the optimal model provider based on task profile.

    V6: Integrates with RiskEngine, CostEngine, and ModelScoring
    to make intelligent routing decisions.

    Agent-provider mapping can be overridden via config:
      {"governor": "codex", "planner": "kimi", "worker": "deepseek", ...}
    """

    def __init__(self, risk_engine: RiskEngine | None = None,
                 cost_engine: CostEngine | None = None,
                 model_scoring: ModelScoring | None = None,
                 provider_map: dict[str, str] | None = None,
                 event_bus=None):
        self._providers: dict[str, Any] = {}
        self.risk_engine = risk_engine or RiskEngine()
        self.cost_engine = cost_engine or CostEngine()
        self.model_scoring = model_scoring or ModelScoring()
        self._provider_map = provider_map or {}
        self.event_bus = event_bus
        self._current_job_id: str = ""

    def set_job_id(self, job_id: str):
        self._current_job_id = job_id

    def register_provider(self, agent_type: str, provider: Any):
        self._providers[agent_type] = provider

    def get_provider(self, agent_type: str) -> Any:
        provider = self._providers.get(agent_type)
        if not provider:
            raise ValueError(f"No provider registered for agent type: {agent_type}")
        return provider

    def has_provider(self, agent_type: str) -> bool:
        return agent_type in self._providers

    def get_route(self, agent_type: str, task=None) -> str:
        """Determine the optimal route for a given agent type and task."""
        # 1. User-configured mapping takes priority
        if agent_type in self._provider_map:
            configured = self._provider_map[agent_type]
            if self.has_provider(configured):
                return configured
            logger.warning(f"Configured provider '{configured}' for {agent_type} not registered, falling back")

        # 2. Use model scoring to find best model for task type
        if task and self.model_scoring:
            task_type = getattr(task, "task_type", "coding")
            best = self.model_scoring.get_best_model(task_type)
            if best and self.has_provider(best):
                logger.debug(f"Routing {agent_type} → {best} (scored best for {task_type})")
                return best

        # 3. Fall back to default strategy
        strategy = ROUTING_STRATEGY.get(agent_type, {})
        return strategy.get("provider", agent_type)

    async def chat(self, agent_type: str, system_prompt: str,
                   messages: list[dict], **kwargs) -> dict:
        """Chat with budget checking and scoring."""
        job_id = kwargs.get("job_id") or self._current_job_id or "unknown"
        task = kwargs.get("task", None)

        # Check budget
        ok, msg = await self.cost_engine.check_budget(job_id)
        if not ok:
            logger.warning(f"Budget exceeded for {job_id}: {msg}")
            return {"content": f"Error: Budget exceeded ({msg})", "finish_reason": "error"}

        # Route to best provider
        route = self.get_route(agent_type, task)
        provider = self.get_provider(route)

        # Snapshot messages for chat log before the call
        chat_prompt = system_prompt
        chat_messages = list(messages) if messages else []

        start = time.monotonic()
        try:
            response = await provider.chat(system_prompt, messages, **kwargs)
            duration_ms = int((time.monotonic() - start) * 1000)

            # Record usage
            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            await self.cost_engine.record_usage(
                job_id, agent_type, input_tokens, output_tokens
            )

            # Record scoring
            success = response.get("finish_reason") != "error"
            task_type = getattr(task, "task_type", "unknown") if task else "unknown"
            await self.model_scoring.record_run(
                agent_type, task_type, success,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
            )

            # Publish chat event for UI
            if self.event_bus:
                await self.event_bus.publish(
                    "model_chat",
                    agent_type=agent_type,
                    provider=route,
                    job_id=job_id,
                    task_type=task_type,
                    system_prompt=chat_prompt,
                    messages=chat_messages,
                    response=response.get("content", ""),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms,
                    error=None,
                )

            return response
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            task_type = getattr(task, "task_type", "unknown") if task else "unknown"
            await self.model_scoring.record_run(
                agent_type, task_type, False,
                duration_ms=duration_ms,
            )

            if self.event_bus:
                await self.event_bus.publish(
                    "model_chat",
                    agent_type=agent_type,
                    provider=route,
                    job_id=job_id,
                    task_type=task_type,
                    system_prompt=chat_prompt,
                    messages=chat_messages,
                    response="",
                    input_tokens=0,
                    output_tokens=0,
                    duration_ms=duration_ms,
                    error=str(e),
                )
            raise

    async def chat_with_tools(self, agent_type: str, system_prompt: str,
                              messages: list[dict], tools: list[dict],
                              **kwargs) -> dict:
        """Chat with tools, with budget checking and scoring."""
        job_id = kwargs.get("job_id") or self._current_job_id or "unknown"
        task = kwargs.get("task", None)

        ok, msg = await self.cost_engine.check_budget(job_id)
        if not ok:
            logger.warning(f"Budget exceeded for {job_id}: {msg}")
            return {"content": f"Error: Budget exceeded ({msg})", "finish_reason": "error"}

        route = kwargs.pop("provider_override", None) or self.get_route(agent_type, task)
        provider = self.get_provider(route)

        chat_prompt = system_prompt
        chat_messages = list(messages) if messages else []

        start = time.monotonic()
        try:
            response = await provider.chat_with_tools(
                system_prompt, messages, tools, **kwargs
            )
            duration_ms = int((time.monotonic() - start) * 1000)

            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            await self.cost_engine.record_usage(
                job_id, agent_type, input_tokens, output_tokens
            )

            success = response.get("finish_reason") != "error"
            task_type = getattr(task, "task_type", "unknown") if task else "unknown"
            await self.model_scoring.record_run(
                agent_type, task_type, success,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
            )

            if self.event_bus:
                await self.event_bus.publish(
                    "model_chat",
                    agent_type=agent_type,
                    provider=route,
                    job_id=job_id,
                    task_type=task_type,
                    system_prompt=chat_prompt,
                    messages=chat_messages,
                    response=response.get("content", ""),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=duration_ms,
                    error=None,
                )

            return response
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            task_type = getattr(task, "task_type", "unknown") if task else "unknown"
            await self.model_scoring.record_run(
                agent_type, task_type, False,
                duration_ms=duration_ms,
            )

            if self.event_bus:
                await self.event_bus.publish(
                    "model_chat",
                    agent_type=agent_type,
                    provider=route,
                    job_id=job_id,
                    task_type=task_type,
                    system_prompt=chat_prompt,
                    messages=chat_messages,
                    response="",
                    input_tokens=0,
                    output_tokens=0,
                    duration_ms=duration_ms,
                    error=str(e),
                )
            raise

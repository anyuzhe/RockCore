"""Model Router — smart routing with risk, cost, and scoring for V6."""

import asyncio
import inspect
import json
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
DEFAULT_REQUEST_TIMEOUT = 180


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
        self.request_timeout = DEFAULT_REQUEST_TIMEOUT

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

    async def _resolve_request(self, request, timeout: float | None):
        """Accept both async and sync provider implementations with one timeout."""
        if not inspect.isawaitable(request):
            return request
        if timeout:
            return await asyncio.wait_for(request, timeout=timeout)
        return await request

    @staticmethod
    def _normalize_response(response, route: str) -> dict:
        """Validate provider output before agents access response fields."""
        if not isinstance(response, dict):
            raise RuntimeError(
                f"Provider {route} returned an invalid response object; "
                "expected a JSON object"
            )
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        response["usage"] = {
            "input_tokens": ModelRouter._normalize_token_count(usage.get("input_tokens", 0)),
            "output_tokens": ModelRouter._normalize_token_count(usage.get("output_tokens", 0)),
        }
        content = response.get("content")
        if content is None:
            response["content"] = ""
        elif not isinstance(content, str):
            response["content"] = json.dumps(content, ensure_ascii=False, default=str)
        if response.get("tool_calls") is None:
            response["tool_calls"] = []
        return response

    @staticmethod
    def _normalize_token_count(value: Any) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError, OverflowError):
            return 0
        return max(0, value)

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
        route = kwargs.pop("provider_override", None) or self.get_route(agent_type, task)
        provider = self.get_provider(route)
        task_id = getattr(task, "task_id", "") if task else ""
        model_name = getattr(provider, "model", "") or route

        # Snapshot messages for chat log before the call
        chat_prompt = system_prompt
        chat_messages = list(messages) if messages else []

        start = time.monotonic()
        request_timeout = kwargs.pop("request_timeout", self.request_timeout)
        try:
            request = provider.chat(system_prompt, messages, **kwargs)
            response = await self._resolve_request(request, request_timeout)
            response = self._normalize_response(response, route)
            duration_ms = int((time.monotonic() - start) * 1000)

            # Record usage
            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            await self.cost_engine.record_usage(
                job_id, agent_type, input_tokens, output_tokens, provider=route
            )
            estimated_cost = self.cost_engine.estimate_cost(
                agent_type, input_tokens, output_tokens, provider=route
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
                    task_id=task_id,
                    model_name=model_name,
                    task_type=task_type,
                    system_prompt=chat_prompt,
                    messages=chat_messages,
                    response=response.get("content", ""),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=estimated_cost,
                    duration_ms=duration_ms,
                    error=None,
                )

            return response
        except Exception as e:
            normalized_error = e
            if isinstance(e, asyncio.TimeoutError):
                normalized_error = TimeoutError(
                    f"Provider request timed out after {request_timeout}s"
                )
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
                    task_id=task_id,
                    model_name=model_name,
                    task_type=task_type,
                    system_prompt=chat_prompt,
                    messages=chat_messages,
                    response="",
                    input_tokens=0,
                    output_tokens=0,
                    estimated_cost=0.0,
                    duration_ms=duration_ms,
                    error=str(normalized_error),
                )
            if normalized_error is not e:
                raise normalized_error from e
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
        task_id = getattr(task, "task_id", "") if task else ""
        model_name = getattr(provider, "model", "") or route

        chat_prompt = system_prompt
        chat_messages = list(messages) if messages else []

        start = time.monotonic()
        request_timeout = kwargs.pop("request_timeout", self.request_timeout)
        try:
            request = provider.chat_with_tools(
                system_prompt, messages, tools, **kwargs
            )
            response = await self._resolve_request(request, request_timeout)
            response = self._normalize_response(response, route)
            duration_ms = int((time.monotonic() - start) * 1000)

            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            await self.cost_engine.record_usage(
                job_id, agent_type, input_tokens, output_tokens, provider=route
            )
            estimated_cost = self.cost_engine.estimate_cost(
                agent_type, input_tokens, output_tokens, provider=route
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
                    task_id=task_id,
                    model_name=model_name,
                    task_type=task_type,
                    system_prompt=chat_prompt,
                    messages=chat_messages,
                    response=response.get("content", ""),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=estimated_cost,
                    duration_ms=duration_ms,
                    error=None,
                )

            return response
        except Exception as e:
            normalized_error = e
            if isinstance(e, asyncio.TimeoutError):
                normalized_error = TimeoutError(
                    f"Provider request timed out after {request_timeout}s"
                )
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
                    task_id=task_id,
                    model_name=model_name,
                    task_type=task_type,
                    system_prompt=chat_prompt,
                    messages=chat_messages,
                    response="",
                    input_tokens=0,
                    output_tokens=0,
                    estimated_cost=0.0,
                    duration_ms=duration_ms,
                    error=str(normalized_error),
                )
            if normalized_error is not e:
                raise normalized_error from e
            raise

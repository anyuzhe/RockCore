"""Model Router — explicit routing, budgets, and provider failover."""

import asyncio
import inspect
import json
import logging
import time
from typing import Any

from .risk_engine import RiskEngine
from .cost_engine import BudgetExceededError, CostEngine

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

    Integrates risk and cost controls with explicit provider configuration.

    Agent-provider mapping can be overridden via config:
      {"governor": "codex", "planner": "kimi", "worker": "deepseek", ...}
    """

    def __init__(self, risk_engine: RiskEngine | None = None,
                 cost_engine: CostEngine | None = None,
                 provider_map: dict[str, str] | None = None,
                 event_bus=None):
        self._providers: dict[str, Any] = {}
        self.risk_engine = risk_engine or RiskEngine()
        self.cost_engine = cost_engine or CostEngine()
        self._provider_map = provider_map or {}
        self._job_provider_maps: dict[str, dict[str, str]] = {}
        self._job_model_maps: dict[str, dict[str, str]] = {}
        self._job_reasoning_maps: dict[str, dict[str, str]] = {}
        self._provider_health: dict[str, dict[str, Any]] = {}
        self.event_bus = event_bus
        self._current_job_id: str = ""
        self.request_timeout = DEFAULT_REQUEST_TIMEOUT

    def set_job_id(self, job_id: str):
        self._current_job_id = job_id

    def set_provider_map(self, provider_map: dict[str, str] | None):
        """Replace the global role-to-provider mapping at runtime."""
        self._provider_map = dict(provider_map or {})

    def set_job_routing(self, job_id: str,
                        provider_map: dict[str, str] | None = None,
                        model_map: dict[str, str] | None = None,
                        reasoning_map: dict[str, str] | None = None):
        """Apply one project's role/provider/model choices to a job."""
        self._job_provider_maps[job_id] = dict(provider_map or {})
        self._job_model_maps[job_id] = dict(model_map or {})
        self._job_reasoning_maps[job_id] = dict(reasoning_map or {})

    def register_provider(self, agent_type: str, provider: Any):
        self._providers[agent_type] = provider

    def get_provider(self, agent_type: str) -> Any:
        provider = self._providers.get(agent_type)
        if not provider:
            raise ValueError(f"No provider registered for agent type: {agent_type}")
        return provider

    def has_provider(self, agent_type: str) -> bool:
        return agent_type in self._providers

    def _configured_provider(self, agent_type: str) -> str:
        job_map = self._job_provider_maps.get(self._current_job_id, {})
        return job_map.get(agent_type) or self._provider_map.get(agent_type, "")

    def _configured_model(self, job_id: str, agent_type: str) -> str:
        return self._job_model_maps.get(job_id, {}).get(agent_type, "")

    def _configured_reasoning(self, job_id: str, agent_type: str) -> str:
        return self._job_reasoning_maps.get(job_id, {}).get(agent_type, "")

    def _circuit_is_open(self, provider: str) -> bool:
        health = self._provider_health.get(provider) or {}
        return float(health.get("open_until", 0)) > time.monotonic()

    @staticmethod
    def _failure_kind(error: Exception | str) -> str:
        message = str(error or "").lower()
        capability = (
            "does not support this tool_choice", "unsupported tool_choice",
            "thinking mode", "unsupported parameter", "invalid parameter",
        )
        permanent = (
            "insufficient balance", "insufficient_balance", "invalid api key",
            "authentication", "missing credentials", "credentials were not found",
            "quota exceeded", "billing", "error code: 401", "status code: 401",
            "error code: 402", "status code: 402", "error code: 403",
            "status code: 403",
        )
        transient = (
            "timed out", "timeout", "rate limit", "too many requests",
            "connection error", "connection reset", "network error",
            "temporarily unavailable", "service unavailable", "server error",
            "status code: 500", "status code: 502", "status code: 503",
            "status code: 504", "error code: 500", "error code: 502",
            "error code: 503", "error code: 504", "invalid response",
            "malformed response", "expected a json object",
        )
        if any(marker in message for marker in capability):
            return "capability"
        if any(marker in message for marker in permanent):
            return "permanent"
        if any(marker in message for marker in transient):
            return "transient"
        return "other"

    def _record_provider_success(self, provider: str):
        self._provider_health.pop(provider, None)

    def _record_provider_failure(self, provider: str,
                                 error: Exception | str) -> str:
        kind = self._failure_kind(error)
        health = self._provider_health.setdefault(
            provider, {"failures": 0, "open_until": 0, "reason": ""}
        )
        health["failures"] = int(health.get("failures", 0)) + 1
        health["reason"] = str(error)[:300]
        if kind in {"capability", "permanent"}:
            health["open_until"] = time.monotonic() + 30 * 60
        elif kind == "transient" and health["failures"] >= 2:
            health["open_until"] = time.monotonic() + 60
        return kind

    def _fallback_provider(self, current: str, *, needs_tools: bool) -> str:
        candidates = ("kimi", "deepseek") if needs_tools else (
            "codex", "kimi", "deepseek"
        )
        for candidate in candidates:
            if (
                candidate != current
                and self.has_provider(candidate)
                and not self._circuit_is_open(candidate)
            ):
                return candidate
        return ""

    async def _publish_fallback(self, *, agent_type: str, job_id: str,
                                task_id: str, current: str, fallback: str,
                                reason: str):
        if not self.event_bus:
            return
        await self.event_bus.publish(
            "provider_circuit_open", agent_type=agent_type,
            provider=current, job_id=job_id, reason=reason[:300],
        )
        await self.event_bus.publish(
            "task_provider_fallback", agent_type=agent_type,
            job_id=job_id, task_id=task_id, from_provider=current,
            to_provider=fallback, reason=reason[:300],
        )

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
            "cached_input_tokens": ModelRouter._normalize_token_count(
                usage.get("cached_input_tokens", 0)
            ),
            "output_tokens": ModelRouter._normalize_token_count(usage.get("output_tokens", 0)),
        }
        response["usage"]["cached_input_tokens"] = min(
            response["usage"]["cached_input_tokens"],
            response["usage"]["input_tokens"],
        )
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

    @staticmethod
    def _billing_mode(provider: Any) -> str:
        """Classify the transport for paid-API budget accounting."""
        return str(getattr(provider, "authentication_mode", "api") or "api")

    def get_route(self, agent_type: str, task=None) -> str:
        """Determine the optimal route for a given agent type and task."""
        # 1. User-configured mapping takes priority
        configured = self._configured_provider(agent_type)
        if configured:
            if self.has_provider(configured):
                return configured
            logger.warning(f"Configured provider '{configured}' for {agent_type} not registered, falling back")

        # 2. Fall back to the stable role default.
        strategy = ROUTING_STRATEGY.get(agent_type, {})
        return strategy.get("provider", agent_type)

    async def chat(self, agent_type: str, system_prompt: str,
                   messages: list[dict], **kwargs) -> dict:
        """Chat with budget checking and provider failover."""
        job_id = kwargs.get("job_id") or self._current_job_id or "unknown"
        task = kwargs.get("task", None)
        task_id = getattr(task, "task_id", "") if task else ""
        fallback_attempted = bool(kwargs.pop("_fallback_attempted", False))
        allow_provider_fallback = bool(
            kwargs.pop("allow_provider_fallback", True)
        )

        # Check budget
        ok, msg = await self.cost_engine.check_budget(job_id)
        if not ok:
            logger.warning(f"Budget exceeded for {job_id}: {msg}")
            raise BudgetExceededError(msg)
        ok, msg = await self.cost_engine.check_task_budget(
            job_id,
            task_id,
            int(getattr(task, "_rockcore_input_budget", 0) or 0),
        )
        if not ok:
            raise BudgetExceededError(msg)

        # Route to best provider
        route = kwargs.pop("provider_override", None) or self.get_route(agent_type, task)
        if allow_provider_fallback and self._circuit_is_open(route):
            fallback = self._fallback_provider(route, needs_tools=False)
            if fallback:
                reason = (self._provider_health.get(route) or {}).get(
                    "reason", "provider circuit is open"
                )
                await self._publish_fallback(
                    agent_type=agent_type, job_id=job_id, task_id=task_id,
                    current=route, fallback=fallback, reason=reason,
                )
                route = fallback
        provider = self.get_provider(route)
        billing_mode = self._billing_mode(provider)
        configured_model = self._configured_model(job_id, agent_type)
        configured_reasoning = self._configured_reasoning(job_id, agent_type)
        configured_provider = self._configured_provider(agent_type)
        if configured_model and (
            not configured_provider or route == configured_provider
        ):
            if not kwargs.get("model"):
                kwargs["model"] = configured_model
        if (
            configured_reasoning
            and configured_reasoning != "default"
            and (not configured_provider or route == configured_provider)
        ):
            kwargs.setdefault("reasoning_effort", configured_reasoning)
        model_name = str(
            kwargs.get("model")
            or (
                configured_model
                if configured_model and route == configured_provider
                else getattr(provider, "model", "") or route
            )
        )

        # Snapshot messages for chat log before the call
        chat_prompt = system_prompt
        chat_messages = list(messages) if messages else []

        start = time.monotonic()
        request_timeout = kwargs.pop("request_timeout", self.request_timeout)
        try:
            request = provider.chat(
                system_prompt, messages, agent_type=agent_type, **kwargs
            )
            response = await self._resolve_request(request, request_timeout)
            response = self._normalize_response(response, route)
            self._record_provider_success(route)
            duration_ms = int((time.monotonic() - start) * 1000)

            # Record usage
            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            cached_input_tokens = usage.get("cached_input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            await self.cost_engine.record_usage(
                job_id, agent_type,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                provider=route, model_name=model_name,
                task_id=task_id, billing_mode=billing_mode,
            )
            estimated_cost = self.cost_engine.estimate_cost(
                agent_type, input_tokens, output_tokens, provider=route,
                cached_input_tokens=cached_input_tokens,
                model_name=model_name,
            )
            billable_cost = self.cost_engine.estimate_billable_cost(
                agent_type, input_tokens, output_tokens, provider=route,
                billing_mode=billing_mode,
                cached_input_tokens=cached_input_tokens,
                model_name=model_name,
            )

            task_type = getattr(task, "task_type", "unknown") if task else "unknown"

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
                    cached_input_tokens=cached_input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=estimated_cost,
                    billable_cost=billable_cost,
                    billing_mode=billing_mode,
                    cost_currency=self.cost_engine.CURRENCY,
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
                    cached_input_tokens=0,
                    output_tokens=0,
                    estimated_cost=0.0,
                    billable_cost=0.0,
                    billing_mode=billing_mode,
                    cost_currency=self.cost_engine.CURRENCY,
                    duration_ms=duration_ms,
                    error=str(normalized_error),
                )
            failure_kind = self._record_provider_failure(route, normalized_error)
            if (
                not fallback_attempted
                and allow_provider_fallback
                and failure_kind in {"capability", "permanent", "transient"}
            ):
                fallback = self._fallback_provider(route, needs_tools=False)
                if fallback:
                    await self._publish_fallback(
                        agent_type=agent_type, job_id=job_id, task_id=task_id,
                        current=route, fallback=fallback,
                        reason=str(normalized_error),
                    )
                    retry_kwargs = dict(kwargs)
                    retry_kwargs.pop("model", None)
                    retry_kwargs.pop("reasoning_effort", None)
                    return await self.chat(
                        agent_type, system_prompt, messages,
                        provider_override=fallback,
                        _fallback_attempted=True,
                        allow_provider_fallback=allow_provider_fallback,
                        request_timeout=request_timeout,
                        **retry_kwargs,
                    )
            if normalized_error is not e:
                raise normalized_error from e
            raise

    async def chat_with_tools(self, agent_type: str, system_prompt: str,
                              messages: list[dict], tools: list[dict],
                              **kwargs) -> dict:
        """Chat with tools, budget checking, and provider failover."""
        job_id = kwargs.get("job_id") or self._current_job_id or "unknown"
        task = kwargs.get("task", None)
        task_id = getattr(task, "task_id", "") if task else ""
        fallback_attempted = bool(kwargs.pop("_fallback_attempted", False))
        allow_provider_fallback = bool(
            kwargs.pop("allow_provider_fallback", True)
        )

        ok, msg = await self.cost_engine.check_budget(job_id)
        if not ok:
            logger.warning(f"Budget exceeded for {job_id}: {msg}")
            raise BudgetExceededError(msg)
        ok, msg = await self.cost_engine.check_task_budget(
            job_id,
            task_id,
            int(getattr(task, "_rockcore_input_budget", 0) or 0),
        )
        if not ok:
            raise BudgetExceededError(msg)

        route = kwargs.pop("provider_override", None) or self.get_route(agent_type, task)
        if allow_provider_fallback and self._circuit_is_open(route):
            fallback = self._fallback_provider(route, needs_tools=True)
            if fallback:
                reason = (self._provider_health.get(route) or {}).get(
                    "reason", "provider circuit is open"
                )
                await self._publish_fallback(
                    agent_type=agent_type, job_id=job_id, task_id=task_id,
                    current=route, fallback=fallback, reason=reason,
                )
                route = fallback
        provider = self.get_provider(route)
        billing_mode = self._billing_mode(provider)
        configured_model = self._configured_model(job_id, agent_type)
        configured_reasoning = self._configured_reasoning(job_id, agent_type)
        configured_provider = self._configured_provider(agent_type)
        if configured_model and (
            not configured_provider or route == configured_provider
        ):
            if not kwargs.get("model"):
                kwargs["model"] = configured_model
        if (
            configured_reasoning
            and configured_reasoning != "default"
            and (not configured_provider or route == configured_provider)
        ):
            kwargs.setdefault("reasoning_effort", configured_reasoning)
        model_name = str(
            kwargs.get("model")
            or (
                configured_model
                if configured_model and route == configured_provider
                else getattr(provider, "model", "") or route
            )
        )

        chat_prompt = system_prompt
        chat_messages = list(messages) if messages else []

        start = time.monotonic()
        request_timeout = kwargs.pop("request_timeout", self.request_timeout)
        try:
            request = provider.chat_with_tools(
                system_prompt, messages, tools, agent_type=agent_type, **kwargs
            )
            response = await self._resolve_request(request, request_timeout)
            response = self._normalize_response(response, route)
            self._record_provider_success(route)
            duration_ms = int((time.monotonic() - start) * 1000)

            usage = response.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            cached_input_tokens = usage.get("cached_input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            await self.cost_engine.record_usage(
                job_id, agent_type,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                provider=route, model_name=model_name,
                task_id=task_id, billing_mode=billing_mode,
            )
            estimated_cost = self.cost_engine.estimate_cost(
                agent_type, input_tokens, output_tokens, provider=route,
                cached_input_tokens=cached_input_tokens,
                model_name=model_name,
            )
            billable_cost = self.cost_engine.estimate_billable_cost(
                agent_type, input_tokens, output_tokens, provider=route,
                billing_mode=billing_mode,
                cached_input_tokens=cached_input_tokens,
                model_name=model_name,
            )

            task_type = getattr(task, "task_type", "unknown") if task else "unknown"

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
                    cached_input_tokens=cached_input_tokens,
                    output_tokens=output_tokens,
                    estimated_cost=estimated_cost,
                    billable_cost=billable_cost,
                    billing_mode=billing_mode,
                    cost_currency=self.cost_engine.CURRENCY,
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
                    cached_input_tokens=0,
                    output_tokens=0,
                    estimated_cost=0.0,
                    billable_cost=0.0,
                    billing_mode=billing_mode,
                    cost_currency=self.cost_engine.CURRENCY,
                    duration_ms=duration_ms,
                    error=str(normalized_error),
                )
            failure_kind = self._record_provider_failure(route, normalized_error)
            if (
                not fallback_attempted
                and allow_provider_fallback
                and failure_kind in {"capability", "permanent", "transient"}
            ):
                fallback = self._fallback_provider(route, needs_tools=True)
                if fallback:
                    await self._publish_fallback(
                        agent_type=agent_type, job_id=job_id, task_id=task_id,
                        current=route, fallback=fallback,
                        reason=str(normalized_error),
                    )
                    retry_kwargs = dict(kwargs)
                    retry_kwargs.pop("model", None)
                    retry_kwargs.pop("reasoning_effort", None)
                    return await self.chat_with_tools(
                        agent_type, system_prompt, messages, tools,
                        provider_override=fallback,
                        _fallback_attempted=True,
                        allow_provider_fallback=allow_provider_fallback,
                        request_timeout=request_timeout,
                        **retry_kwargs,
                    )
            if normalized_error is not e:
                raise normalized_error from e
            raise

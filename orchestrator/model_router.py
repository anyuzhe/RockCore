"""Model Router — explicit routing, budgets, and provider failover."""

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
import time
from contextvars import ContextVar
from typing import Any

from .risk_engine import RiskEngine
from .cost_engine import BudgetExceededError, CostEngine
from .failures import classify_provider_failure

logger = logging.getLogger(__name__)

# Agent type → model selection strategy
ROUTING_STRATEGY = {
    "main_agent": {"provider": "codex", "priority": "accuracy"},
    "main_agent_summary": {"provider": "codex", "priority": "accuracy"},
    "governor": {"provider": "codex", "priority": "accuracy"},
    "planner": {"provider": "kimi", "priority": "reasoning"},
    "worker": {"provider": "deepseek", "priority": "speed"},
    "reviewer": {"provider": "codex", "priority": "accuracy"},
    "emergency_coder": {"provider": "codex", "priority": "write_access"},
}
# Long reasoning/model-queue requests can legitimately take several minutes.
# Keep the RMB cost ceiling as the hard protection and avoid terminating useful
# provider work merely because the former three-minute client timer elapsed.
DEFAULT_REQUEST_TIMEOUT = 540
SAME_PROVIDER_RETRIES = 2


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
        # Availability is task/job-local. One project's provider incident must
        # never silently reroute another concurrently running project.
        self._provider_health: dict[tuple[str, str], dict[str, Any]] = {}
        self._unavailable_models: dict[tuple[str, str, str], str] = {}
        self.event_bus = event_bus
        self._job_context: ContextVar[str] = ContextVar(
            "rockcore_model_job_id", default=""
        )
        self.request_timeout = DEFAULT_REQUEST_TIMEOUT
        self._durability_barrier = None

    def set_durability_barrier(self, callback) -> None:
        """Install the job runtime's semantic pre-request persistence hook."""
        self._durability_barrier = callback

    async def _persist_request_intent(
        self, *, job_id: str, task_id: str, agent_type: str,
        provider: str, model_name: str, with_tools: bool,
        messages: list[dict],
    ) -> None:
        callback = self._durability_barrier
        if callback is None:
            return
        outcome = callback(
            "provider_request_prepared",
            job_id=job_id,
            task_id=task_id,
            agent_type=agent_type,
            provider=provider,
            model_name=model_name,
            with_tools=with_tools,
            message_count=len(messages or []),
            prompt_fingerprint=hashlib.sha256(
                json.dumps(messages or [], ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()[:16],
        )
        if inspect.isawaitable(outcome):
            await outcome

    def set_job_id(self, job_id: str):
        self._job_context.set(str(job_id or ""))

    @property
    def _current_job_id(self) -> str:
        """Backward-compatible view of the coroutine-local Job identifier."""
        return self._job_context.get()

    @_current_job_id.setter
    def _current_job_id(self, job_id: str):
        self._job_context.set(str(job_id or ""))

    @staticmethod
    def _estimate_text_tokens(value: Any) -> int:
        """Conservatively estimate mixed Chinese/ASCII prompt tokens."""
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, default=str)
        cjk = len(re.findall(r"[\u3400-\u9fff]", value))
        return cjk + math.ceil(max(0, len(value) - cjk) / 3.5)

    @classmethod
    def _estimate_request_tokens(
        cls, system_prompt: str, messages: list[dict],
        tools: list[dict] | None = None,
    ) -> int:
        payload = [system_prompt, messages]
        if tools:
            payload.append(tools)
        return max(256, cls._estimate_text_tokens(payload) + 64)

    async def _reserve_model_request(
        self, *, job_id: str, task, task_id: str, agent_type: str,
        provider: str, model_name: str, billing_mode: str,
        system_prompt: str, messages: list[dict],
        tools: list[dict] | None, kwargs: dict,
    ) -> dict:
        """Forecast, auto-expand, and atomically reserve a provider call."""
        estimated_input = self._estimate_request_tokens(
            system_prompt, messages, tools
        )
        task_usage = self.cost_engine.get_task_usage(job_id, task_id)
        if task_id:
            estimated_input = max(
                estimated_input,
                int(task_usage.get("average_input_tokens", 0) or 0),
            )
        max_output = max(
            256,
            int(
                kwargs.get("estimated_output_tokens")
                or 4_096
            ),
        )
        if task is not None:
            current = max(
                0, int(getattr(task, "_rockcore_input_budget", 0) or 0)
            )
            projected = int(
                task_usage.get("effective_input_tokens", 0) or 0
            ) + estimated_input
            if current and projected >= math.floor(current * 0.85):
                maximum = max(
                    current,
                    int(getattr(
                        task, "_rockcore_max_auto_input_budget", 20_000_000
                    ) or 20_000_000),
                )
                enlarged = min(
                    maximum,
                    max(math.ceil(current * 1.5), math.ceil(projected * 1.25)),
                )
                if enlarged > current:
                    task._rockcore_input_budget = enlarged
                    if self.event_bus:
                        await self.event_bus.publish(
                            "task_budget_extended",
                            job_id=job_id,
                            task_id=task_id,
                            previous_task_input_budget=current,
                            task_input_budget=enlarged,
                            reason="下一次模型调用预测将超过任务软额度的 85%",
                        )
        estimated_cost = self.cost_engine.estimate_billable_cost(
            agent_type,
            estimated_input,
            max_output,
            provider=provider,
            billing_mode=billing_mode,
            cached_input_tokens=0,
            model_name=model_name,
        )
        try:
            admission = await self.cost_engine.admit_request(
                job_id,
                task_id=task_id,
                agent_type=agent_type,
                estimated_input_tokens=estimated_input,
                max_output_tokens=max_output,
                estimated_billable_cost=estimated_cost,
            )
        except BudgetExceededError as error:
            if self.event_bus:
                await self.event_bus.publish(
                    "budget_continuation_required",
                    job_id=job_id,
                    task_id=task_id,
                    error=str(error),
                    budget=self.cost_engine.get_budget_snapshot(job_id),
                )
            raise
        if admission.get("expanded") and self.event_bus:
            await self.event_bus.publish(
                "budget_auto_expanded",
                job_id=job_id,
                task_id=task_id,
                reason="下一次调用的预测用量超过当前软额度",
                budget=admission.get("budget", {}),
            )
        return admission

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
        # Reconfiguring credentials/binaries must immediately close any
        # authentication circuit opened by the previous provider instance.
        self._provider_health = {
            key: value for key, value in self._provider_health.items()
            if not (
                (isinstance(key, tuple) and len(key) > 1 and key[1] == agent_type)
                or key == agent_type
            )
        }
        self._unavailable_models = {
            key: reason for key, reason in self._unavailable_models.items()
            if not (
                (isinstance(key, tuple) and len(key) > 1 and key[1] == agent_type)
                or (isinstance(key, tuple) and key and key[0] == agent_type)
            )
        }

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

    def _availability_scope(
        self, job_id: str = "", task_id: str = "",
    ) -> str:
        job = str(job_id or self._current_job_id or "unknown")
        return f"{job}:{str(task_id or '*')}"

    def _health_key(
        self, provider: str, job_id: str = "", task_id: str = "",
    ) -> tuple[str, str]:
        return (self._availability_scope(job_id, task_id), provider)

    def _model_key(
        self, provider: str, model_name: str, job_id: str = "",
        task_id: str = "",
    ) -> tuple[str, str, str]:
        return (
            self._availability_scope(job_id, task_id), provider, model_name
        )

    def _circuit_is_open(
        self, provider: str, job_id: str = "", task_id: str = "",
    ) -> bool:
        if task_id:
            health_records = [self._provider_health.get(
                self._health_key(provider, job_id, task_id)
            ) or {}]
        else:
            job_prefix = self._availability_scope(job_id).split(":", 1)[0] + ":"
            health_records = [
                health for key, health in self._provider_health.items()
                if isinstance(key, tuple)
                and len(key) == 2
                and key[1] == provider
                and str(key[0]).startswith(job_prefix)
            ]
            legacy = self._provider_health.get(provider)
            if isinstance(legacy, dict):
                health_records.append(legacy)
        return any(
            float(health.get("open_until", 0)) > time.monotonic()
            for health in health_records
        )

    @staticmethod
    def _is_model_unavailable_error(error: Exception | str) -> bool:
        message = str(error or "").lower()
        markers = (
            "not found the model",
            "model not found",
            "resource_not_found_error",
            "unknown model",
            "model does not exist",
            "model is not available",
        )
        return any(marker in message for marker in markers) or (
            "permission denied" in message
            and any(marker in message for marker in (
                "model", "404", "resource_not_found_error"
            ))
        )

    def _mark_model_unavailable(
        self, provider: str, model_name: str, error: Exception | str,
        job_id: str = "", task_id: str = "",
    ) -> None:
        self._unavailable_models[
            self._model_key(provider, model_name, job_id, task_id)
        ] = str(error)[:300]

    def _model_fallback(
        self, provider_name: str, model_name: str, job_id: str = "",
        task_id: str = "",
    ) -> str:
        """Return a provider-supported alternative not known to be unavailable."""
        provider = self.get_provider(provider_name)
        resolver = getattr(provider, "fallback_models", None)
        candidates = resolver(model_name) if callable(resolver) else []
        for candidate in candidates or []:
            candidate = str(candidate or "")
            if (
                candidate
                and candidate != model_name
                and self._model_key(
                    provider_name, candidate, job_id, task_id
                ) not in self._unavailable_models
            ):
                return candidate
        return ""

    @staticmethod
    def _remove_output_limit(kwargs: dict) -> None:
        """Keep RockCore from imposing a generation cap on provider APIs."""
        kwargs.pop("max_tokens", None)
        kwargs.pop("max_output_tokens", None)

    @staticmethod
    def _failure_kind(error: Exception | str) -> str:
        return classify_provider_failure(error).legacy_kind

    def _record_provider_success(
        self, provider: str, job_id: str = "", task_id: str = "",
    ):
        self._provider_health.pop(
            self._health_key(provider, job_id, task_id), None
        )

    def _record_provider_failure(self, provider: str,
                                 error: Exception | str,
                                 job_id: str = "",
                                 task_id: str = "") -> str:
        kind = self._failure_kind(error)
        key = self._health_key(provider, job_id, task_id)
        health = self._provider_health.setdefault(
            key, {"failures": 0, "open_until": 0, "reason": ""}
        )
        health["failures"] = int(health.get("failures", 0)) + 1
        health["reason"] = str(error)[:300]
        if kind in {"capability", "authentication", "unavailable"}:
            health["open_until"] = time.monotonic() + 30 * 60
        return kind

    def _fallback_provider(
        self, current: str, *, needs_tools: bool, job_id: str = "",
        task_id: str = "",
    ) -> str:
        candidates = ("kimi", "deepseek") if needs_tools else (
            "codex", "kimi", "deepseek"
        )
        for candidate in candidates:
            if (
                candidate != current
                and self.has_provider(candidate)
                and not self._circuit_is_open(candidate, job_id, task_id)
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

    async def _publish_model_fallback(
        self, *, agent_type: str, job_id: str, task_id: str,
        provider: str, current_model: str, fallback_model: str,
        reason: str,
    ) -> None:
        if not self.event_bus:
            return
        await self.event_bus.publish(
            "task_model_fallback",
            agent_type=agent_type,
            provider=provider,
            job_id=job_id,
            task_id=task_id,
            from_model=current_model,
            to_model=fallback_model,
            reason=reason[:300],
        )

    async def _publish_fallback_success(
        self, *, agent_type: str, job_id: str, task_id: str,
        provider: str, from_model: str = "", to_model: str = "",
        from_provider: str = "",
    ) -> None:
        """Report a fallback only after the replacement request succeeds."""
        if not self.event_bus:
            return
        event_type = (
            "task_model_fallback_succeeded"
            if from_model else "task_provider_fallback_succeeded"
        )
        await self.event_bus.publish(
            event_type,
            agent_type=agent_type,
            job_id=job_id,
            task_id=task_id,
            provider=provider,
            from_model=from_model,
            to_model=to_model,
            from_provider=from_provider,
            to_provider=provider,
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
        # The old boolean guard allowed only one same-provider fallback. Keep
        # accepting it for compatibility, but rely on the unavailable-model
        # cache so every declared candidate can be tried exactly once.
        kwargs.pop("_model_fallback_attempted", None)
        model_fallback_from = str(kwargs.pop("_model_fallback_from", "") or "")
        provider_fallback_from = str(
            kwargs.pop("_provider_fallback_from", "") or ""
        )
        allow_provider_fallback = bool(
            kwargs.pop("allow_provider_fallback", True)
        )
        allow_model_fallback = bool(kwargs.pop("allow_model_fallback", True))
        same_provider_retry_count = max(
            0, int(kwargs.pop("_same_provider_retry_count", 0) or 0)
        )

        # Route to best provider
        route = kwargs.pop("provider_override", None) or self.get_route(agent_type, task)
        if allow_provider_fallback and self._circuit_is_open(
            route, job_id, task_id
        ):
            fallback = self._fallback_provider(
                route, needs_tools=False, job_id=job_id, task_id=task_id
            )
            if fallback:
                provider_fallback_from = route
                reason = (self._provider_health.get(
                    self._health_key(route, job_id, task_id)
                ) or {}).get(
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
        unavailable_reason = self._unavailable_models.get(
            self._model_key(route, model_name, job_id, task_id), ""
        )
        if unavailable_reason and allow_model_fallback:
            fallback_model = self._model_fallback(
                route, model_name, job_id, task_id
            )
            if fallback_model:
                fallback_origin = model_name
                await self._publish_model_fallback(
                    agent_type=agent_type, job_id=job_id, task_id=task_id,
                    provider=route, current_model=model_name,
                    fallback_model=fallback_model,
                    reason=unavailable_reason,
                )
                kwargs["model"] = fallback_model
                model_name = fallback_model
                model_fallback_from = fallback_origin

        admission = await self._reserve_model_request(
            job_id=job_id,
            task=task,
            task_id=task_id,
            agent_type=agent_type,
            provider=route,
            model_name=model_name,
            billing_mode=billing_mode,
            system_prompt=system_prompt,
            messages=messages,
            tools=None,
            kwargs=kwargs,
        )
        self._remove_output_limit(kwargs)
        kwargs.pop("estimated_output_tokens", None)
        reservation_id = str(admission.get("reservation_id") or "")

        # Snapshot messages for chat log before the call
        chat_prompt = system_prompt
        chat_messages = list(messages) if messages else []

        await self._persist_request_intent(
            job_id=job_id, task_id=task_id, agent_type=agent_type,
            provider=route, model_name=model_name, with_tools=False,
            messages=messages,
        )
        start = time.monotonic()
        request_timeout = kwargs.pop("request_timeout", self.request_timeout)
        try:
            request = provider.chat(
                system_prompt, messages, agent_type=agent_type, **kwargs
            )
            response = await self._resolve_request(request, request_timeout)
            response = self._normalize_response(response, route)
            self._record_provider_success(route, job_id, task_id)
            if model_fallback_from:
                await self._publish_fallback_success(
                    agent_type=agent_type, job_id=job_id, task_id=task_id,
                    provider=route, from_model=model_fallback_from,
                    to_model=model_name,
                )
            elif provider_fallback_from:
                await self._publish_fallback_success(
                    agent_type=agent_type, job_id=job_id, task_id=task_id,
                    provider=route, from_provider=provider_fallback_from,
                )
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
                reservation_id=reservation_id,
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
                    budget=self.cost_engine.get_budget_snapshot(job_id),
                    cost_currency=self.cost_engine.CURRENCY,
                    duration_ms=duration_ms,
                    error=None,
                )

            return response
        except Exception as e:
            await self.cost_engine.release_request(job_id, reservation_id)
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
                    budget=self.cost_engine.get_budget_snapshot(job_id),
                    cost_currency=self.cost_engine.CURRENCY,
                    duration_ms=duration_ms,
                    error=str(normalized_error),
                    failure=classify_provider_failure(
                        normalized_error, provider=route, model=model_name,
                    ).as_dict(),
                )
            model_unavailable = self._is_model_unavailable_error(normalized_error)
            if model_unavailable:
                self._mark_model_unavailable(
                    route, model_name, normalized_error, job_id, task_id
                )
                failure_kind = "capability"
                fallback_model = (
                    self._model_fallback(
                        route, model_name, job_id, task_id
                    )
                    if allow_model_fallback else ""
                )
                if fallback_model:
                    await self._publish_model_fallback(
                        agent_type=agent_type, job_id=job_id, task_id=task_id,
                        provider=route, current_model=model_name,
                        fallback_model=fallback_model,
                        reason=str(normalized_error),
                    )
                    retry_kwargs = dict(kwargs)
                    retry_kwargs["model"] = fallback_model
                    retry_kwargs["_model_fallback_from"] = model_name
                    return await self.chat(
                        agent_type, system_prompt, messages,
                        provider_override=route,
                        _fallback_attempted=fallback_attempted,
                        allow_provider_fallback=allow_provider_fallback,
                        allow_model_fallback=allow_model_fallback,
                        request_timeout=request_timeout,
                        **retry_kwargs,
                    )
            else:
                failure_kind = self._record_provider_failure(
                    route, normalized_error, job_id, task_id
                )
            if (
                failure_kind == "retryable"
                and same_provider_retry_count < SAME_PROVIDER_RETRIES
            ):
                retry_kwargs = dict(kwargs)
                retry_kwargs["_same_provider_retry_count"] = (
                    same_provider_retry_count + 1
                )
                return await self.chat(
                    agent_type, system_prompt, messages,
                    provider_override=route,
                    _fallback_attempted=fallback_attempted,
                    allow_provider_fallback=allow_provider_fallback,
                    allow_model_fallback=allow_model_fallback,
                    request_timeout=request_timeout,
                    **retry_kwargs,
                )
            if (
                not fallback_attempted
                and allow_provider_fallback
                and failure_kind in {
                    "capability", "authentication", "unavailable"
                }
            ):
                fallback = self._fallback_provider(
                    route, needs_tools=False, job_id=job_id, task_id=task_id
                )
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
                        _provider_fallback_from=route,
                        allow_provider_fallback=allow_provider_fallback,
                        allow_model_fallback=allow_model_fallback,
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
        kwargs.pop("_model_fallback_attempted", None)
        model_fallback_from = str(kwargs.pop("_model_fallback_from", "") or "")
        provider_fallback_from = str(
            kwargs.pop("_provider_fallback_from", "") or ""
        )
        allow_provider_fallback = bool(
            kwargs.pop("allow_provider_fallback", True)
        )
        allow_model_fallback = bool(kwargs.pop("allow_model_fallback", True))
        same_provider_retry_count = max(
            0, int(kwargs.pop("_same_provider_retry_count", 0) or 0)
        )

        route = kwargs.pop("provider_override", None) or self.get_route(agent_type, task)
        if allow_provider_fallback and self._circuit_is_open(
            route, job_id, task_id
        ):
            fallback = self._fallback_provider(
                route, needs_tools=True, job_id=job_id, task_id=task_id
            )
            if fallback:
                provider_fallback_from = route
                reason = (self._provider_health.get(
                    self._health_key(route, job_id, task_id)
                ) or {}).get(
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
        unavailable_reason = self._unavailable_models.get(
            self._model_key(route, model_name, job_id, task_id), ""
        )
        if unavailable_reason and allow_model_fallback:
            fallback_model = self._model_fallback(
                route, model_name, job_id, task_id
            )
            if fallback_model:
                fallback_origin = model_name
                await self._publish_model_fallback(
                    agent_type=agent_type, job_id=job_id, task_id=task_id,
                    provider=route, current_model=model_name,
                    fallback_model=fallback_model,
                    reason=unavailable_reason,
                )
                kwargs["model"] = fallback_model
                model_name = fallback_model
                model_fallback_from = fallback_origin

        admission = await self._reserve_model_request(
            job_id=job_id,
            task=task,
            task_id=task_id,
            agent_type=agent_type,
            provider=route,
            model_name=model_name,
            billing_mode=billing_mode,
            system_prompt=system_prompt,
            messages=messages,
            tools=tools,
            kwargs=kwargs,
        )
        self._remove_output_limit(kwargs)
        kwargs.pop("estimated_output_tokens", None)
        reservation_id = str(admission.get("reservation_id") or "")

        chat_prompt = system_prompt
        chat_messages = list(messages) if messages else []

        await self._persist_request_intent(
            job_id=job_id, task_id=task_id, agent_type=agent_type,
            provider=route, model_name=model_name, with_tools=True,
            messages=messages,
        )
        start = time.monotonic()
        request_timeout = kwargs.pop("request_timeout", self.request_timeout)
        try:
            request = provider.chat_with_tools(
                system_prompt, messages, tools, agent_type=agent_type, **kwargs
            )
            response = await self._resolve_request(request, request_timeout)
            response = self._normalize_response(response, route)
            self._record_provider_success(route, job_id, task_id)
            if model_fallback_from:
                await self._publish_fallback_success(
                    agent_type=agent_type, job_id=job_id, task_id=task_id,
                    provider=route, from_model=model_fallback_from,
                    to_model=model_name,
                )
            elif provider_fallback_from:
                await self._publish_fallback_success(
                    agent_type=agent_type, job_id=job_id, task_id=task_id,
                    provider=route, from_provider=provider_fallback_from,
                )
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
                reservation_id=reservation_id,
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
                    budget=self.cost_engine.get_budget_snapshot(job_id),
                    cost_currency=self.cost_engine.CURRENCY,
                    duration_ms=duration_ms,
                    error=None,
                )

            return response
        except Exception as e:
            await self.cost_engine.release_request(job_id, reservation_id)
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
                    budget=self.cost_engine.get_budget_snapshot(job_id),
                    cost_currency=self.cost_engine.CURRENCY,
                    duration_ms=duration_ms,
                    error=str(normalized_error),
                    failure=classify_provider_failure(
                        normalized_error, provider=route, model=model_name,
                    ).as_dict(),
                )
            model_unavailable = self._is_model_unavailable_error(normalized_error)
            if model_unavailable:
                self._mark_model_unavailable(
                    route, model_name, normalized_error, job_id, task_id
                )
                failure_kind = "capability"
                fallback_model = (
                    self._model_fallback(
                        route, model_name, job_id, task_id
                    )
                    if allow_model_fallback else ""
                )
                if fallback_model:
                    await self._publish_model_fallback(
                        agent_type=agent_type, job_id=job_id, task_id=task_id,
                        provider=route, current_model=model_name,
                        fallback_model=fallback_model,
                        reason=str(normalized_error),
                    )
                    retry_kwargs = dict(kwargs)
                    retry_kwargs["model"] = fallback_model
                    retry_kwargs["_model_fallback_from"] = model_name
                    return await self.chat_with_tools(
                        agent_type, system_prompt, messages, tools,
                        provider_override=route,
                        _fallback_attempted=fallback_attempted,
                        allow_provider_fallback=allow_provider_fallback,
                        allow_model_fallback=allow_model_fallback,
                        request_timeout=request_timeout,
                        **retry_kwargs,
                    )
            else:
                failure_kind = self._record_provider_failure(
                    route, normalized_error, job_id, task_id
                )
            if (
                failure_kind == "retryable"
                and same_provider_retry_count < SAME_PROVIDER_RETRIES
            ):
                retry_kwargs = dict(kwargs)
                retry_kwargs["_same_provider_retry_count"] = (
                    same_provider_retry_count + 1
                )
                return await self.chat_with_tools(
                    agent_type, system_prompt, messages, tools,
                    provider_override=route,
                    _fallback_attempted=fallback_attempted,
                    allow_provider_fallback=allow_provider_fallback,
                    allow_model_fallback=allow_model_fallback,
                    request_timeout=request_timeout,
                    **retry_kwargs,
                )
            if (
                not fallback_attempted
                and allow_provider_fallback
                and failure_kind in {
                    "capability", "authentication", "unavailable"
                }
            ):
                fallback = self._fallback_provider(
                    route, needs_tools=True, job_id=job_id, task_id=task_id
                )
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
                        _provider_fallback_from=route,
                        allow_provider_fallback=allow_provider_fallback,
                        allow_model_fallback=allow_model_fallback,
                        request_timeout=request_timeout,
                        **retry_kwargs,
                    )
            if normalized_error is not e:
                raise normalized_error from e
            raise

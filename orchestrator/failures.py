"""Structured failure taxonomy shared by routing and recovery code."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


class FailureCode(str, Enum):
    CAPABILITY_INCOMPATIBLE = "capability_incompatible"
    USER_ACTION_REQUIRED = "user_action_required"
    AUTHENTICATION_FAILED = "authentication_failed"
    MODEL_UNAVAILABLE = "model_unavailable"
    RATE_LIMITED = "rate_limited"
    REQUEST_TIMEOUT = "request_timeout"
    TRANSIENT_NETWORK = "transient_network"
    PROVIDER_SERVER_ERROR = "provider_server_error"
    INVALID_RESPONSE = "invalid_response"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderFailure:
    """Machine-readable provider failure used by retry and UI projections."""

    code: FailureCode
    message: str
    retryable: bool = False
    requires_user: bool = False
    switch_model: bool = False
    switch_provider: bool = False
    http_status: int | None = None
    provider: str = ""
    model: str = ""
    request_id: str = ""

    @property
    def legacy_kind(self) -> str:
        if self.code == FailureCode.CAPABILITY_INCOMPATIBLE:
            return "capability"
        if self.code == FailureCode.USER_ACTION_REQUIRED:
            return "user_action"
        if self.code == FailureCode.AUTHENTICATION_FAILED:
            return "authentication"
        if self.code in {
            FailureCode.MODEL_UNAVAILABLE,
            FailureCode.RATE_LIMITED,
            FailureCode.REQUEST_TIMEOUT,
        }:
            return "unavailable"
        if self.retryable:
            return "retryable"
        return "other"

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["code"] = self.code.value
        payload["kind"] = self.legacy_kind
        return payload


def _http_status(message: str) -> int | None:
    lowered = message.lower()
    for status in (401, 402, 403, 404, 408, 409, 429, 500, 502, 503, 504):
        if any(marker in lowered for marker in (
            f"error code: {status}", f"status code: {status}",
            f"http {status}", f"http error {status}",
        )):
            return status
    return None


def classify_provider_failure(
    error: Exception | str,
    *,
    provider: str = "",
    model: str = "",
) -> ProviderFailure:
    """Normalize provider-specific text once at the routing boundary."""

    message = str(error or "Unknown provider error")
    lowered = message.lower()
    status = _http_status(message)

    if any(marker in lowered for marker in (
        "does not support this tool_choice", "unsupported tool_choice",
        "thinking mode", "unsupported parameter", "invalid parameter",
    )):
        return ProviderFailure(
            FailureCode.CAPABILITY_INCOMPATIBLE, message,
            switch_model=True, switch_provider=True, http_status=status,
            provider=provider, model=model,
        )
    if any(marker in lowered for marker in (
        "insufficient balance", "insufficient_balance", "quota exceeded",
        "insufficient_quota", "credit_balance_exhausted", "billing",
    )) or status == 402:
        return ProviderFailure(
            FailureCode.USER_ACTION_REQUIRED, message, requires_user=True,
            http_status=status, provider=provider, model=model,
        )
    if any(marker in lowered for marker in (
        "invalid api key", "authentication", "authorization",
        "missing credentials", "credentials were not found",
        "credentials unavailable",
    )) or status in {401, 403} or (
        "permission denied" in lowered and "model" not in lowered
    ):
        return ProviderFailure(
            FailureCode.AUTHENTICATION_FAILED, message, requires_user=True,
            switch_provider=False, http_status=status,
            provider=provider, model=model,
        )
    if any(marker in lowered for marker in (
        "not found the model", "model not found", "unknown model",
        "model does not exist", "model is not available",
        "resource_not_found_error",
    )) or status == 404 or (
        "permission denied" in lowered and "model" in lowered
    ):
        return ProviderFailure(
            FailureCode.MODEL_UNAVAILABLE, message, switch_model=True,
            switch_provider=True, http_status=status,
            provider=provider, model=model,
        )
    if any(marker in lowered for marker in (
        "rate limit", "too many requests", "overloaded",
    )) or status == 429:
        return ProviderFailure(
            FailureCode.RATE_LIMITED, message, retryable=True,
            switch_provider=True, http_status=status,
            provider=provider, model=model,
        )
    if any(marker in lowered for marker in ("timed out", "timeout")) or status == 408:
        return ProviderFailure(
            FailureCode.REQUEST_TIMEOUT, message, retryable=True,
            switch_provider=True, http_status=status,
            provider=provider, model=model,
        )
    if status in {500, 502, 503, 504} or any(marker in lowered for marker in (
        "service unavailable", "server error", "temporarily unavailable",
    )):
        return ProviderFailure(
            FailureCode.PROVIDER_SERVER_ERROR, message, retryable=True,
            http_status=status, provider=provider, model=model,
        )
    if any(marker in lowered for marker in (
        "connection error", "connection reset", "network error",
    )):
        return ProviderFailure(
            FailureCode.TRANSIENT_NETWORK, message, retryable=True,
            provider=provider, model=model,
        )
    if any(marker in lowered for marker in (
        "invalid response", "malformed response", "expected a json object",
    )):
        return ProviderFailure(
            FailureCode.INVALID_RESPONSE, message, retryable=True,
            provider=provider, model=model,
        )
    return ProviderFailure(
        FailureCode.UNKNOWN, message, http_status=status,
        provider=provider, model=model,
    )

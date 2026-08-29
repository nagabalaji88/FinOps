"""Typed application errors mapped to RFC-7807 style responses."""

from __future__ import annotations

from fastapi import HTTPException, status


class AppError(Exception):
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code

    def to_http(self) -> HTTPException:
        return HTTPException(
            status_code=self.status_code,
            detail={"code": self.code, "message": self.message, "details": self.details},
        )


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class AuthError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class ProviderNotConfiguredError(AppError):
    """Raised instead of returning a fabricated response when a provider is missing."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "provider_not_configured"


# Statuses where the same request, sent again, could plausibly succeed. Everything else a
# provider returns in the 4xx range is a defect in the request or the credential, and is
# identical on every attempt.
RETRYABLE_PROVIDER_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class ProviderError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        details: dict | None = None,
        code: str | None = None,
        provider_status: int | None = None,
        retryable: bool = True,
    ):
        super().__init__(message, details=details, code=code)
        self.provider_status = provider_status
        # A transport error carries no status and is retryable by default; once a provider
        # has answered, its status decides and the caller's default no longer applies.
        self.retryable = (
            retryable if provider_status is None else provider_status in RETRYABLE_PROVIDER_STATUSES
        )
        if provider_status is not None:
            self.details.setdefault("provider_status", provider_status)


class CircuitOpenError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "circuit_open"


class BudgetExceededError(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "budget_exceeded"


class GuardrailViolation(AppError):
    status_code = 422
    code = "guardrail_violation"


class ApprovalRequired(AppError):
    status_code = status.HTTP_202_ACCEPTED
    code = "approval_required"


#: Failures that no amount of waiting fixes. Both the retry ladder and the circuit breaker
#: consult this, because the two questions have the same answer: a rejected credential and a
#: malformed request fail identically on every attempt, so retrying one burns the ladder and
#: still reports the wrong cause -- and counting it as an outage then hides that cause behind
#: a CircuitOpenError for every caller until the breaker closes again.
_PERMANENT = (
    ProviderNotConfiguredError,
    CircuitOpenError,
    AuthError,
    ForbiddenError,
    ValidationError,
    NotFoundError,
    BudgetExceededError,
    GuardrailViolation,
    ApprovalRequired,
)


def is_transient(exc: BaseException) -> bool:
    """Whether retrying -- or waiting for a circuit to close -- could plausibly help."""
    if isinstance(exc, ProviderError):
        return exc.retryable
    if isinstance(exc, _PERMANENT):
        return False
    # An unrecognised exception is treated as transient: a genuine outage that this list
    # does not name yet must still trip the breaker.
    return True

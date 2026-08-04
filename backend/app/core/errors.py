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


class ProviderError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "provider_error"


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

"""Domain errors that the API maps to structured HTTP responses."""
from __future__ import annotations

from typing import Any


class DomainError(Exception):
    http_status = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}


class NotFound(DomainError):
    http_status = 404
    code = "NOT_FOUND"


class InvalidTransition(DomainError):
    http_status = 409
    code = "INVALID_TRANSITION"


class Conflict(DomainError):
    http_status = 409
    code = "CONFLICT"


class Forbidden(DomainError):
    http_status = 403
    code = "FORBIDDEN"


class ValidationFailed(DomainError):
    http_status = 422
    code = "VALIDATION_FAILED"

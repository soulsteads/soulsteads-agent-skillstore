"""Error types. Each carries a stable ``code`` used by the CLI, HTTP API and MCP."""

from __future__ import annotations


class SkillstoreError(Exception):
    code = "error"
    exit_code = 1

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message, **self.details}


class ValidationFailed(SkillstoreError):
    """The package is not a valid Agent Skills package."""

    code = "validation_failed"
    exit_code = 2


class Conflict(SkillstoreError):
    """The operation would overwrite local state; an explicit choice is required."""

    code = "conflict"
    exit_code = 3


class IntegrityError(SkillstoreError):
    """Content does not match its recorded digest (tampering or corruption)."""

    code = "integrity_error"
    exit_code = 4


class NotReviewed(SkillstoreError):
    """The requested version is not an authoritative (reviewed) release."""

    code = "not_reviewed"
    exit_code = 5


class PermissionDenied(SkillstoreError):
    code = "permission_denied"
    exit_code = 6


class NotFound(SkillstoreError):
    code = "not_found"
    exit_code = 7


class UsageError(SkillstoreError):
    code = "usage_error"
    exit_code = 64


ERRORS_BY_CODE = {
    cls.code: cls
    for cls in (
        SkillstoreError,
        ValidationFailed,
        Conflict,
        IntegrityError,
        NotReviewed,
        PermissionDenied,
        NotFound,
        UsageError,
    )
}

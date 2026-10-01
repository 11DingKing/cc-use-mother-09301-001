"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """所有业务规则违反的基类，携带稳定的错误码。"""

    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class NotFoundError(DomainError):
    code = "not_found"


class ConflictError(DomainError):
    code = "conflict"


class StateError(DomainError):
    code = "invalid_state"


class RecusalError(DomainError):
    code = "recusal_conflict"


class DuplicateSubmissionError(DomainError):
    code = "duplicate_submission"


class RuleVersionError(DomainError):
    code = "rule_version_error"

from __future__ import annotations


class WorkflowError(Exception):
    code = "WORKFLOW_ERROR"
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class IdempotencyConflict(WorkflowError):
    code = "IDEMPOTENCY_KEY_CONFLICT"
    status_code = 409


class OptimisticLockConflict(WorkflowError):
    code = "OPTIMISTIC_LOCK_CONFLICT"
    status_code = 409


class DiskSpaceInsufficient(WorkflowError):
    code = "DISK_SPACE_INSUFFICIENT"
    status_code = 507


class NotFound(WorkflowError):
    code = "NOT_FOUND"
    status_code = 404


class Forbidden(WorkflowError):
    code = "FORBIDDEN"
    status_code = 403


class InvalidStateTransition(WorkflowError):
    code = "INVALID_STATE_TRANSITION"
    status_code = 409


class ValidationFailed(WorkflowError):
    code = "VALIDATION_FAILED"
    status_code = 422

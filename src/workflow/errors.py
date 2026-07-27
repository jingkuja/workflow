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

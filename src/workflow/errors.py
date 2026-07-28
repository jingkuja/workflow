from __future__ import annotations


class WorkflowError(Exception):
    """业务错误基类。

    code 与 docs/新媒体内容制作工作流实施方案-v1.0.md §4.1 的稳定错误码清单对齐，
    属于 MCP 契约的一部分，修改必须保持向后兼容。
    """

    code = "WORKFLOW_ERROR"
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class Unauthenticated(WorkflowError):
    code = "UNAUTHENTICATED"
    status_code = 401


class Forbidden(WorkflowError):
    code = "FORBIDDEN"
    status_code = 403


class ResourceNotFound(WorkflowError):
    code = "RESOURCE_NOT_FOUND"
    status_code = 404


class InvalidArgument(WorkflowError):
    code = "INVALID_ARGUMENT"
    status_code = 400


class InvalidStateTransition(WorkflowError):
    code = "INVALID_STATE_TRANSITION"
    status_code = 409


class IdempotencyConflict(WorkflowError):
    code = "IDEMPOTENCY_CONFLICT"
    status_code = 409


class ConcurrentModification(WorkflowError):
    code = "CONCURRENT_MODIFICATION"
    status_code = 409


class NoEligibleAssignee(WorkflowError):
    code = "NO_ELIGIBLE_ASSIGNEE"
    status_code = 409


class DuplicateFile(WorkflowError):
    code = "DUPLICATE_FILE"
    status_code = 409


class FileTooLarge(WorkflowError):
    code = "FILE_TOO_LARGE"
    status_code = 413


class UnsupportedFileType(WorkflowError):
    code = "UNSUPPORTED_FILE_TYPE"
    status_code = 400


class FileProcessingFailed(WorkflowError):
    code = "FILE_PROCESSING_FAILED"
    status_code = 422


class InsufficientStorage(WorkflowError):
    code = "INSUFFICIENT_STORAGE"
    status_code = 507


class ExternalDependencyFailed(WorkflowError):
    code = "EXTERNAL_DEPENDENCY_FAILED"
    status_code = 502

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    """稳定错误码枚举，供客户端区分处理。

    错误码命名：{领域}_{原因}，字母大写蛇形。
    """

    # ---------- 基础：配置与系统 0xxx ----------
    CONFIG_MISSING = "CONFIG_MISSING"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"

    # ---------- 身份与权限 1xxx ----------
    AUTH_MISSING = "AUTH_MISSING"
    AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    AUTH_TENANT_MISMATCH = "AUTH_TENANT_MISMATCH"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    ROLE_FORBIDDEN = "ROLE_FORBIDDEN"

    # ---------- 参数与校验 2xxx ----------
    VALIDATION_ERROR = "VALIDATION_ERROR"
    MISSING_SLOT = "MISSING_SLOT"
    CONFLICT_PAYLOAD = "CONFLICT_PAYLOAD"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    # ---------- 资源 & 状态 3xxx ----------
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    REFUND_NOT_FOUND = "REFUND_NOT_FOUND"
    HANDOFF_NOT_FOUND = "HANDOFF_NOT_FOUND"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"

    # ---------- 业务 4xxx ----------
    CONVERSATION_BUSY = "CONVERSATION_BUSY"
    PENDING_ACTION_INVALID = "PENDING_ACTION_INVALID"
    PENDING_ACTION_EXPIRED = "PENDING_ACTION_EXPIRED"
    PENDING_ACTION_CANCELLED = "PENDING_ACTION_CANCELLED"
    PENDING_ACTION_ALREADY_EXECUTED = "PENDING_ACTION_ALREADY_EXECUTED"
    REFUND_NOT_ELIGIBLE = "REFUND_NOT_ELIGIBLE"
    REFUND_AMOUNT_MISMATCH = "REFUND_AMOUNT_MISMATCH"
    REFUND_QUOTA_EXCEEDED = "REFUND_QUOTA_EXCEEDED"
    HANDOFF_ALREADY_QUEUED = "HANDOFF_ALREADY_QUEUED"
    WAITING_HUMAN_BLOCKED = "WAITING_HUMAN_BLOCKED"
    MAX_STEPS_EXCEEDED = "MAX_STEPS_EXCEEDED"

    # ---------- 工具 & 依赖 5xxx ----------
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_NOT_ALLOWLISTED = "TOOL_NOT_ALLOWLISTED"
    TOOL_ARG_VALIDATION_FAILED = "TOOL_ARG_VALIDATION_FAILED"
    TOOL_IDENTITY_OVERRIDE_ATTEMPTED = "TOOL_IDENTITY_OVERRIDE_ATTEMPTED"
    TOOL_IDEMPOTENCY_KEY_REQUIRED = "TOOL_IDEMPOTENCY_KEY_REQUIRED"
    TOOL_IDEMPOTENCY_CONFLICT = "TOOL_IDEMPOTENCY_CONFLICT"
    TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_EXECUTION_TIMEOUT = "TOOL_EXECUTION_TIMEOUT"
    TOOL_EXECUTION_UNKNOWN = "TOOL_EXECUTION_UNKNOWN"
    TOOL_AUDIT_FAILED = "TOOL_AUDIT_FAILED"
    RAG_RETRIEVAL_FAILED = "RAG_RETRIEVAL_FAILED"
    LLM_PROVIDER_ERROR = "LLM_PROVIDER_ERROR"


class ErrorDisplay(BaseModel):
    """统一错误响应体。

    只暴露安全的可展示信息；stack_trace 仅在 LOCAL/TEST 环境包含。
    """

    code: ErrorCode
    message: str
    request_id: str | None = None
    trace_id: str | None = None
    details: dict[str, Any] | None = Field(
        default=None,
        description="结构化的附加详情，如缺失字段、冲突键等。",
    )
    stack_trace: str | None = Field(
        default=None,
        description="本地/测试环境下可选附带堆栈；生产级永远为 None。",
    )

    model_config = ConfigDict(use_enum_values=True)


class AppBaseError(Exception):
    """应用自定义异常基类。

    所有业务异常均应继承此类，由全局异常处理器转为 ErrorDisplay。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message: str = message
        self.details: dict[str, Any] | None = details
        self.http_status: int = http_status


class ConfigError(AppBaseError):
    """启动或运行期配置错误。"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            ErrorCode.CONFIG_MISSING,
            message,
            details=details,
            http_status=500,
        )


class AuthError(AppBaseError):
    """身份校验失败。"""

    def __init__(
        self,
        code: ErrorCode = ErrorCode.AUTH_TOKEN_INVALID,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code,
            message or code.value,
            details=details,
            http_status=401,
        )


class PermissionDeniedError(AppBaseError):
    """权限不足。"""

    def __init__(
        self,
        message: str = "permission denied",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            ErrorCode.PERMISSION_DENIED,
            message,
            details=details,
            http_status=403,
        )


class ValidationFailedError(AppBaseError):
    """参数校验失败。"""

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        http_status: int = 422,
    ) -> None:
        super().__init__(
            ErrorCode.VALIDATION_ERROR,
            message,
            details=details,
            http_status=http_status,
        )


class MissingSlotError(AppBaseError):
    """缺少必填槽位，需要上层澄清追问。"""

    def __init__(self, slot: str, *, message: str | None = None) -> None:
        super().__init__(
            ErrorCode.MISSING_SLOT,
            message or f"missing slot: {slot}",
            details={"slot": slot},
            http_status=422,
        )
        self.slot: str = slot


class ConflictError(AppBaseError):
    """请求冲突，如 request_id 内容变更、幂等参数不一致等。"""

    def __init__(
        self,
        code: ErrorCode = ErrorCode.CONFLICT_PAYLOAD,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code,
            message or code.value,
            details=details,
            http_status=409,
        )


class ResourceNotFoundError(AppBaseError):
    """资源不存在。对外不暴露归属判断，统一表现为 404。"""

    def __init__(
        self,
        code: ErrorCode = ErrorCode.RESOURCE_NOT_FOUND,
        *,
        message: str = "resource not found",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            code,
            message,
            details=details,
            http_status=404,
        )


class BusinessRuleError(AppBaseError):
    """业务规则不满足，如退款资格不足、状态机非法转换。"""

    def __init__(
        self,
        code: ErrorCode,
        *,
        message: str,
        details: dict[str, Any] | None = None,
        http_status: int = 400,
    ) -> None:
        super().__init__(code, message, details=details, http_status=http_status)


@dataclass
class ToolExecutionErrorMeta:
    """工具执行失败时传递给异常处理的元信息。"""

    tool_name: str
    error_classification: Literal["explicit_failure", "unknown"]
    idempotency_key: str | None = None
    trace_id: str | None = None


class AppError(AppBaseError):
    """为兼容历史命名保留的 AppError 别名（指向 AppBaseError）。"""


class ToolExecutionError(AppBaseError):
    """工具执行错误（由 ToolRunner / BaseTool 抛出）。"""

    def __init__(
        self,
        code: ErrorCode,
        *,
        message: str,
        details: dict[str, Any] | None = None,
        http_status: int = 422,
        retryable: bool = False,
    ) -> None:
        super().__init__(code, message, details=details, http_status=http_status)
        self.retryable = retryable


class IdempotencyConflictError(AppBaseError):
    """同 idempotency_key 但 arguments hash 不一致（409 类）。"""

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
        http_status: int = 409,
    ) -> None:
        super().__init__(ErrorCode.TOOL_IDEMPOTENCY_CONFLICT, message, details=details, http_status=http_status)

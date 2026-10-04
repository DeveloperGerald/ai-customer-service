"""工具框架 Schema（T4：所有工具调用的契约层）。

关键约束（AGENTS.md 直接落地）：
- 所有写工具调用必须带 idempotency_key（防止 LLM 同一工具调用重试造成重复退款）
- 可信身份注入：即使模型传了 user_id/tenant_id 参数，也必须丢弃，用 HTTP Actor 覆盖
- 所有工具调用必须落 tool_audit_logs（审计可追溯）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# ============================================================================
# 一、工具调用请求 / 响应契约（LangGraph Node / HTTP / 前端调用通用）
# ============================================================================


class ToolCallRequest(BaseModel):
    """Agent/HTTP 发起工具调用的统一请求体。

    - idempotency_key：写操作必填；读操作建议填
    - trust_actor_over_params=True：**固定 True**（由后端在执行前再次强制校验，
      即使恶意客户端传入 False 也会被后端忽略 → 身份永远用 Actor 覆盖）
    """

    tool_name: str = Field(..., max_length=64, description="注册在 ToolRegistry 里的工具名，如 order_query")
    arguments: dict[str, Any] = Field(default_factory=dict, description="工具参数（键值对）")
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="幂等 key；写工具必填。建议格式 {tenant_id}:{session_id}:{msg_id}:{tool_name}:{counter}",
    )
    session_id: str | None = Field(default=None, max_length=64, description="T6 LangGraph thread_id 关联（可选）")
    dry_run: bool = Field(default=False, description="仅校验权限 + 参数，不真正执行（用于预校验）")


class ToolCallError(BaseModel):
    """工具执行失败元数据（和 ToolResult 搭配，不再裸抛 Exception 到 Agent）。"""

    code: str = Field(..., max_length=64, description="ErrorCode 枚举值，例如 RESOURCE_NOT_FOUND")
    message: str = Field(..., max_length=1000)
    details: dict[str, Any] | None = None
    retryable: bool = Field(default=False, description="LLM 是否可以重新尝试（一般参数错=false，LLM 换参数即可重试）")


class ToolResult(BaseModel):
    """工具执行统一返回（LangGraph Node 直接把 data 给 LLM，前端也直接能渲染）。"""

    tool_name: str
    call_id: UUID = Field(..., description="本次调用唯一 ID，对应 tool_audit_logs PK")
    success: bool
    data: dict[str, Any] | None = Field(default=None, description="成功输出；不同工具 data schema 不同")
    error: ToolCallError | None = None
    from_idempotency_cache: bool = Field(
        default=False,
        description="true = 命中幂等缓存，这是旧结果（副作用未再执行）",
    )
    started_at: datetime
    finished_at: datetime

    @property
    def duration_ms(self) -> int:
        return max(0, int((self.finished_at - self.started_at).total_seconds() * 1000))


# ============================================================================
# 二、工具定义元数据（前端「有哪些工具」展示 + LLM function calling description 用）
# ============================================================================


class ToolParamSchema(BaseModel):
    name: str
    type: Literal["string", "integer", "number", "boolean", "array", "object"] = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None


class ToolDefinition(BaseModel):
    name: str
    description: str
    category: Literal["read", "write", "escalation"] = Field(
        default="read",
        description="read=只读，不触发幂等；write=写，强制 idempotency_key；escalation=转人工（D3 立即执行）",
    )
    params: list[ToolParamSchema] = Field(default_factory=list)

    @field_validator("category")
    @classmethod
    def _ck(cls, v: str) -> str:
        if v not in {"read", "write", "escalation"}:
            raise ValueError(f"invalid category: {v}")
        return v

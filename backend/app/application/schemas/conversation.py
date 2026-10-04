"""会话域 Schema（T6 会话 + LangGraph Checkpointer 适配占位）。

LangGraph thread_id 规则（面试硬编码）：
    thread_id = "{tenant_id}:{thread_suffix}"
    任何时候把 thread_id.split(":", 1)[0] 作为校验用 tenant_id；
    与 Actor.tenant_id 不一致则 ResourceNotFound 防越权枚举。

消息 rollup 策略（MVP）：
    不做 rollup，最近 N 条全量取；真实项目补 message rollup 表或截断。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

MessageRole = Literal["human", "agent", "tool"]
ThreadStatus = Literal["open", "escalated", "closed"]

_VALID_ROLES: frozenset[str] = frozenset({"human", "agent", "tool"})
_VALID_STATUS: frozenset[str] = frozenset({"open", "escalated", "closed"})


# ============================================================================
# 一、会话（Thread）
# ============================================================================


class ConversationThreadBase(BaseModel):
    """会话线程共享字段（create/read 复用）。"""

    title: str | None = Field(default=None, max_length=200, description="会话标题（可空，LLM 自动摘要时写入）。")
    initial_user_message: str | None = Field(default=None, max_length=4000, description="首条用户消息，便于列表展示。")
    status: ThreadStatus = Field(default="open", description="线程状态（open/escalated/closed）。")
    escalated_ticket_no: str | None = Field(default=None, max_length=64, description="转人工时的工单编号；转人工节点 D3 立即执行时回填。")

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: str) -> str:
        if v not in _VALID_STATUS:
            raise ValueError(f"status 必须在 {sorted(_VALID_STATUS)}")
        return v


class ConversationThreadCreate(ConversationThreadBase):
    """POST /api/conversations 请求体。

    通常用户首条消息到来时创建；也允许先空会话再 POST messages。
    """

    pass


class ConversationThreadRead(ConversationThreadBase):
    """会话线程对外响应。"""

    model_config = ConfigDict(from_attributes=True)

    thread_id: str = Field(
        description='LangGraph thread_id，格式为 "{tenant_id}:{uuid_hex}"。'
        "该 ID 同时是 LangGraph Postgres Checkpointer 的 checkpoint 主键。",
    )
    tenant_id: str
    owner_user_id: UUID = Field(description="会话创建者 user_id；consumer 仅能看自己的，staff/admin 看同租户全部。")
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime | None = Field(default=None, description="最后一条消息到达时间；用于列表排序。")


# ============================================================================
# 二、消息（Message）
# ============================================================================


class ConversationMessageBase(BaseModel):
    """消息共享字段（create/read 复用）。"""

    role: MessageRole = Field(description="消息角色：human=用户；agent=LLM 回复；tool=T4 工具调用输出。")
    content: str = Field(min_length=1, max_length=100_000, description="消息正文。LLM/tool 输出可能较大，上限放宽。")
    metadata: dict | None = Field(default=None, description="任意元数据（T7 LangGraph 会写工具调用信息）。")
    tool_name: str | None = Field(default=None, max_length=128, description="当 role=tool 时关联的工具名。")
    tool_call_id: str | None = Field(default=None, max_length=128, description="当 role=tool 时关联的 audit_logs.call_id。")

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        if v not in _VALID_ROLES:
            raise ValueError(f"role 必须在 {sorted(_VALID_ROLES)}")
        return v


class ConversationMessageCreate(ConversationMessageBase):
    """POST /api/conversations/{thread_id}/messages 请求体。

    真实 T7 走 Agent 后 agent/tool 消息由内部插入；HTTP 端仅允许 consumer 插入 human 消息
    （这里 MVP 不做 role 限制，便于演示脚本塞完整对话）。
    """

    pass


class ConversationMessageRead(ConversationMessageBase):
    model_config = ConfigDict(from_attributes=True)

    message_id: UUID
    thread_id: str
    tenant_id: str
    actor_id: UUID = Field(description="写入该消息的 actor.user_id；human=consumer；agent/tool=系统 actor。")
    created_at: datetime

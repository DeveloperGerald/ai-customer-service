"""会话 ORM（Thread + Message 两张表；LangGraph Checkpointer 占位兼容）。

架构说明（面试可以聊）：
  * 本模块的「threads/messages」是业务层视图表（前端对话列表/详情读这里）；
  * 未来 LangGraph Checkpointer 可以独立建 checkpoints/blobs 两张表（见 T7 任务），
    业务层读 messages，Agent 节点读 checkpoints。两者通过 thread_id = "{tenant_id}:{hex}" 关联。
  * 这里 MVP 先不落地 Checkpointer（避免 pg-async-sessionstore 依赖），仅在 Repository
    暴露两个预留方法：save_checkpoint / load_checkpoint（空实现 + 打 warning）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.infrastructure import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConversationThreadORM(Base):
    """会话线程（1 tenant 下 N 个消费者，每个消费者可能多线程）。"""

    __tablename__ = "conversation_threads"
    __table_args__ = (
        # tenant_id FK：删租户会被 RESTRICT 阻止，保留历史会话审计
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_threads_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "owner_user_id"],
            ["users.tenant_id", "users.user_id"],
            name="fk_threads_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('open','escalated','closed')",
            name="ck_threads_status_valid",
        ),
        CheckConstraint(
            "substring(thread_id from 1 for (char_length(tenant_id) + 1)) = (tenant_id || ':')",
            name="ck_threads_thread_id_prefix_matches_tenant",
        ),
        # 列表页查询通常是：同租户下某 owner_user_id 的最新线程
        Index("ix_threads_tenant_owner_updated", "tenant_id", "owner_user_id", "updated_at"),
        Index("ix_threads_tenant_status", "tenant_id", "status"),
    )

    # PK = thread_id（字符串，LangGraph 兼容格式），不再额外做自增
    thread_id = Column(String(128), primary_key=True, comment='格式 "{tenant_id}:{uuid_hex}"。')
    tenant_id = Column(String(50), nullable=False, index=True)

    title = Column(String(200), nullable=True)
    initial_user_message = Column(Text, nullable=True)
    owner_user_id = Column(UUID(as_uuid=True), nullable=False, comment="会话创建者（通常是消费者 user_id）。")
    status = Column(String(20), nullable=False, server_default="open", index=True)
    escalated_ticket_no = Column(String(64), nullable=True, comment="转人工时的工单编号；D3 立即执行时由 BaseTicketService 回填。")
    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=_utcnow,
    )


class ConversationMessageORM(Base):
    """会话消息（human/agent/tool 三类角色；追加写为主，极少 UPDATE）。"""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_msgs_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["users.tenant_id", "users.user_id"],
            name="fk_msgs_actor",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["thread_id"],
            ["conversation_threads.thread_id"],
            name="fk_msgs_thread",
            ondelete="CASCADE",  # 删线程 → 级联删消息
        ),
        CheckConstraint(
            "role IN ('human','agent','tool')",
            name="ck_msgs_role_valid",
        ),
        UniqueConstraint("message_id", "tenant_id", name="uq_msgs_id_tenant"),  # 仅用于逻辑分表时的审计
        Index("ix_msgs_thread_created", "thread_id", "created_at"),
        Index("ix_msgs_tenant_tool_call", "tenant_id", "tool_call_id"),
    )

    message_id = Column(UUID(as_uuid=True), primary_key=True, default=lambda: uuid4())
    tenant_id = Column(String(50), nullable=False, index=True)

    thread_id = Column(String(128), nullable=False, index=True, comment="关联 conversation_threads.thread_id。")
    actor_id = Column(UUID(as_uuid=True), nullable=False, comment="写入者 user_id；agent/tool 消息由系统级 user(例如 tenant_xxx:bot) 写入。")
    role = Column(String(16), nullable=False, index=True)
    content = Column(Text, nullable=False)

    # 工具调用关联（role=tool 时非空）
    tool_name = Column(String(128), nullable=True)
    tool_call_id = Column(CHAR(64), nullable=True, comment="对应 tool_audit_logs.call_id 的 UUID hex。")

    # 任意元数据（LLM token 消耗、流式 SSE event id 等）
    metadata_json = Column(
        JSONB(astext_type=None),
        nullable=True,
        comment="KV 元数据。避免 ALTER 频繁加列。",
    )

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)

    # 注意：不能在 DeclarativeBase 子类里暴露 metadata 属性，
    # 否则会和 Base.metadata (MetaData 注册表) 冲突，导致 AttributeError schema。
    # ConversationMessageRead.metadata ←→ ORM.metadata_json 的映射统一由 Repository 层完成。

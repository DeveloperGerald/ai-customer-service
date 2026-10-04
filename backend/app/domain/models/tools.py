"""工具框架 ORM：
- tool_audit_logs（审计：所有工具调用记录，无论成功失败）
- idempotency_records（幂等：(tenant_id, idempotency_key) UQ 防重入）
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
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.infrastructure import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ToolAuditLogORM(Base):
    """工具调用审计日志（AGENTS.md 硬约束：所有工具调用都记一条）。

    写入策略：调用前先 INSERT 一条 status=running；完成/失败后 UPDATE status。
    即使进程崩溃，也能看到"悬垂 running 记录"便于排查。
    """

    __tablename__ = "tool_audit_logs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_tool_audit_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["users.tenant_id", "users.user_id"],
            name="fk_tool_audit_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('running','succeeded','failed')",
            name="ck_tool_audit_status_valid",
        ),
        Index("ix_tool_audit_tenant_tool_created", "tenant_id", "tool_name", "started_at"),
        Index("ix_tool_audit_idem_key", "tenant_id", "idempotency_key"),
    )

    call_id = Column(UUID(as_uuid=True), primary_key=True, default=lambda: uuid4())
    tenant_id = Column(String(50), nullable=False, index=True)
    actor_id = Column(UUID(as_uuid=True), nullable=False, comment="调用者 user_id（从 Actor 注入，永不从 payload 取）")
    session_id = Column(String(64), nullable=True, index=True)
    tool_name = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=True, comment="写工具必填；读工具可空")

    arguments_hash = Column(
        CHAR(64),
        nullable=False,
        comment="SHA256(tool_name + sorted_json(arguments))，用于 diff 检测幂等重试",
    )
    arguments_json = Column(JSONB(astext_type=None), nullable=False, default=dict)

    status = Column(String(16), nullable=False, server_default="running")
    result_json = Column(JSONB(astext_type=None), nullable=True, comment="成功 data 或失败 error 对象")
    error_code = Column(String(64), nullable=True, index=True)

    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True, comment="冗余存储，便于按耗时查询慢工具")


class IdempotencyRecordORM(Base):
    """写工具幂等记录（UQ(tenant_id, idempotency_key) 防重复执行）。

    Redis 可做第一层 L1 缓存（T4.4 @idempotent 装饰器里可选择先查 Redis 再查 DB）；
    DB 层 UQ 作为最后兜底，**即使 Redis 穿透也不会重复执行**。
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_idem_tenant_key"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_idem_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["call_id"],
            ["tool_audit_logs.call_id"],
            name="fk_idem_audit_log",
            ondelete="RESTRICT",
        ),
        Index("ix_idem_expires_at", "expires_at"),
    )

    record_id = Column(UUID(as_uuid=True), primary_key=True, default=lambda: uuid4())
    tenant_id = Column(String(50), nullable=False)
    idempotency_key = Column(String(128), nullable=False)
    tool_name = Column(String(64), nullable=False)
    call_id = Column(UUID(as_uuid=True), nullable=False, comment="首次执行的审计日志 call_id，可直接 JOIN 拿结果")

    arguments_hash = Column(CHAR(64), nullable=False, comment="和 audit_logs.arguments_hash 对齐（若不同说明参数变了，返回 409 冲突）")
    cached_result_json = Column(JSONB(astext_type=None), nullable=True, comment="冗余存一份，命中缓存直接返回不再 JOIN audit_logs")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True, comment="建议 24h 过期，可定期清表")

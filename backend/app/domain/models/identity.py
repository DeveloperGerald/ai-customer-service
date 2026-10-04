from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import UUID

from app.core.infrastructure import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantORM(Base):
    """租户表。演示用三行数据（tenant_a/b/c）。"""

    __tablename__ = "tenants"

    tenant_id = Column(String(50), primary_key=True, comment="业务可读租户 ID，如 tenant_a。")
    name = Column(String(100), nullable=False, unique=True)
    display_name = Column(String(200))
    description = Column(Text)
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UserORM(Base):
    """平台用户。一个用户严格属于一个租户（多租户隔离的根约束）。

    - 同租户 username 唯一。
    - 外键 users.tenant_id -> tenants.tenant_id。
    - role 枚举由 CheckConstraint 限制，防止非法值。
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "username", name="uq_users_tenant_username"),
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_users_tenant_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "role IN ('consumer', 'staff', 'admin', 'agent_engineer')",
            name="ck_users_role_valid",
        ),
        Index("ix_users_tenant_role", "tenant_id", "role"),
    )

    user_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=lambda: uuid4(),
        comment="用户稳定标识，不使用自增 ID。",
    )
    tenant_id = Column(String(50), nullable=False, index=True)
    username = Column(String(80), nullable=False)
    display_name = Column(String(120))
    email = Column(String(254))
    phone = Column(String(32))
    role = Column(String(32), nullable=False, server_default="consumer")
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=_utcnow,
    )

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
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.core.infrastructure import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TenantPolicyConfigORM(Base):
    """每租户结构化售后政策配置表（staff/admin 可通过管理接口修改）。

    - tenant_id PK（每个租户一条记录，插入时与租户 1:1）
    - 数值字段 DB 层 CK 校验，避免绕过应用直接写入非法值
    - updated_by FK 指向 users，记录最后修改人
    """

    __tablename__ = "tenant_policy_configs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_policy_configs_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["updated_by"],
            ["users.user_id"],
            name="fk_policy_configs_updated_by",
            ondelete="SET NULL",
        ),
        CheckConstraint(
            "return_policy_type IN ('no_reason', 'quality_only', 'hybrid')",
            name="ck_policy_configs_type_valid",
        ),
        CheckConstraint(
            "return_days IS NULL OR (return_days >= 0 AND return_days <= 365)",
            name="ck_policy_configs_return_days",
        ),
        CheckConstraint(
            "restocking_fee_pct_non_quality BETWEEN 0 AND 100",
            name="ck_policy_configs_fee_0_100",
        ),
        CheckConstraint(
            "warranty_days_quality BETWEEN 0 AND 1095",
            name="ck_policy_configs_warranty",
        ),
    )

    tenant_id = Column(String(50), primary_key=True, comment="所属租户，1:1 对应 tenants.tenant_id")
    return_days = Column(SmallInteger, nullable=True, comment="非质量退货窗口(天)；NULL=不支持非质量退货")
    return_policy_type = Column(String(20), nullable=False, server_default="hybrid")
    restocking_fee_pct_non_quality = Column(SmallInteger, nullable=False, server_default="0")
    warranty_days_quality = Column(Integer, nullable=False, server_default="30")
    custom_product_allowed_return = Column(Boolean, nullable=False, server_default="false")
    updated_by = Column(UUID(as_uuid=True), nullable=True, comment="最后修改人 user_id，NULL 表示系统默认种子")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=_utcnow,
    )


class KnowledgeChunkORM(Base):
    """RAG 知识库段落表（一张表承载政策原文 + FAQ + 操作文档，全部带 tenant_id）。

    T5 RAG 直接复用此表：BaseRetriever.retrieve(query, top_k, tenant_id) 对 embedding 做
    相似度搜索时必须加 tenant_id 过滤（架构硬约束）。
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_knowledge_chunks_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["users.user_id"],
            name="fk_knowledge_chunks_created_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "source IN ('policy_manual', 'faq', 'operation_doc')",
            name="ck_knowledge_chunks_source_valid",
        ),
        UniqueConstraint("tenant_id", "source", "content_hash", name="uq_knowledge_chunks_dup"),
        Index("ix_knowledge_chunks_tenant_source", "tenant_id", "source"),
    )

    chunk_id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=lambda: uuid4(),
        comment="段落稳定 ID。",
    )
    tenant_id = Column(String(50), nullable=False, index=True)
    title = Column(String(255), nullable=True)
    content = Column(Text, nullable=False)
    source = Column(String(32), nullable=False, server_default="faq")
    content_hash = Column(String(64), nullable=False, comment="SHA256(content)，用于幂等去重。")
    embedding = Column(
        Text,
        nullable=True,
        comment="向量以 base64 / JSON 字符串形式暂存，接入真实 pgvector 后改为 vector(1536) 类型。",
    )
    created_by = Column(UUID(as_uuid=True), nullable=False, comment="上传人 user_id。")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

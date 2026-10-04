"""tenant_policy_configs + knowledge_chunks（承载 T2.5 管理配置 + T5 RAG 一张表）。

Revision ID: 0002
Revises: 0001
Create Date: 2025-09-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def _install_vector_extension() -> None:
    """尝试创建 pgvector 扩展。

    失败时不抛异常：演示环境如果没装 pgvector，embedding 仍可通过 TEXT 存 JSON；
    真接入 T5 向量检索时，运维需要先 `apt-get install postgresql-16-pgvector`。
    """
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception:  # noqa: BLE001
        pass


def upgrade() -> None:
    _install_vector_extension()

    # 1. 结构化配置 1:1 per tenant
    op.create_table(
        "tenant_policy_configs",
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("return_days", sa.SmallInteger(), nullable=True),
        sa.Column("return_policy_type", sa.String(length=20), nullable=False, server_default="hybrid"),
        sa.Column("restocking_fee_pct_non_quality", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("warranty_days_quality", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("custom_product_allowed_return", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_tenant_policy_configs"),
    )
    op.create_check_constraint(
        "ck_policy_configs_type_valid",
        "tenant_policy_configs",
        "return_policy_type IN ('no_reason', 'quality_only', 'hybrid')",
    )
    op.create_check_constraint(
        "ck_policy_configs_return_days",
        "tenant_policy_configs",
        "return_days IS NULL OR (return_days >= 0 AND return_days <= 365)",
    )
    op.create_check_constraint(
        "ck_policy_configs_fee_0_100",
        "tenant_policy_configs",
        "restocking_fee_pct_non_quality BETWEEN 0 AND 100",
    )
    op.create_check_constraint(
        "ck_policy_configs_warranty",
        "tenant_policy_configs",
        "warranty_days_quality BETWEEN 0 AND 1095",
    )
    op.create_foreign_key(
        "fk_policy_configs_tenant",
        "tenant_policy_configs",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_policy_configs_updated_by",
        "tenant_policy_configs",
        "users",
        ["updated_by"],
        ["user_id"],
        ondelete="SET NULL",
    )

    # 2. 知识库 chunks：政策 + FAQ + 操作文档 一张表
    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="faq"),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("chunk_id", name="pk_knowledge_chunks"),
    )
    op.create_check_constraint(
        "ck_knowledge_chunks_source_valid",
        "knowledge_chunks",
        "source IN ('policy_manual', 'faq', 'operation_doc')",
    )
    op.create_unique_constraint(
        "uq_knowledge_chunks_dup",
        "knowledge_chunks",
        ["tenant_id", "source", "content_hash"],
    )
    op.create_index(
        "ix_knowledge_chunks_tenant_source",
        "knowledge_chunks",
        ["tenant_id", "source"],
    )
    op.create_foreign_key(
        "fk_knowledge_chunks_tenant",
        "knowledge_chunks",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_knowledge_chunks_created_by",
        "knowledge_chunks",
        "users",
        ["created_by"],
        ["user_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # 先删表（顺序：子表 → 父表）
    op.drop_constraint("fk_knowledge_chunks_created_by", "knowledge_chunks", type_="foreignkey")
    op.drop_constraint("fk_knowledge_chunks_tenant", "knowledge_chunks", type_="foreignkey")
    op.drop_index("ix_knowledge_chunks_tenant_source", table_name="knowledge_chunks")
    op.drop_constraint("uq_knowledge_chunks_dup", "knowledge_chunks", type_="unique")
    op.drop_constraint("ck_knowledge_chunks_source_valid", "knowledge_chunks", type_="check")
    op.drop_table("knowledge_chunks")

    op.drop_constraint("fk_policy_configs_updated_by", "tenant_policy_configs", type_="foreignkey")
    op.drop_constraint("fk_policy_configs_tenant", "tenant_policy_configs", type_="foreignkey")
    op.drop_constraint("ck_policy_configs_warranty", "tenant_policy_configs", type_="check")
    op.drop_constraint("ck_policy_configs_fee_0_100", "tenant_policy_configs", type_="check")
    op.drop_constraint("ck_policy_configs_return_days", "tenant_policy_configs", type_="check")
    op.drop_constraint("ck_policy_configs_type_valid", "tenant_policy_configs", type_="check")
    op.drop_table("tenant_policy_configs")

    # 注意：downgrade 不 DROP EXTENSION vector（扩展是全局共享的，怕被其他数据库用到）

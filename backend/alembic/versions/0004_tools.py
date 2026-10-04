"""工具框架迁移：tool_audit_logs（审计） + idempotency_records（幂等 DB 兜底）。

Revision ID: 0004
Revises: 0003
Create Date: 2025-09-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ----- 1. tool_audit_logs -----
    op.create_table(
        "tool_audit_logs",
        sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("arguments_hash", sa.CHAR(64), nullable=False),
        sa.Column("arguments_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("call_id", name="pk_tool_audit_logs"),
    )
    op.create_check_constraint(
        "ck_tool_audit_status_valid",
        "tool_audit_logs",
        "status IN ('running','succeeded','failed')",
    )
    op.create_foreign_key(
        "fk_tool_audit_tenant",
        "tool_audit_logs",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_tool_audit_actor",
        "tool_audit_logs",
        "users",
        ["tenant_id", "actor_id"],
        ["tenant_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_tool_audit_tenant_tool_created", "tool_audit_logs", ["tenant_id", "tool_name", "started_at"])
    op.create_index("ix_tool_audit_idem_key", "tool_audit_logs", ["tenant_id", "idempotency_key"])
    op.create_index("ix_tool_audit_status", "tool_audit_logs", ["status"])

    # ----- 2. idempotency_records（UQ(tenant, idempotency_key) 最后兜底）-----
    op.create_table(
        "idempotency_records",
        sa.Column("record_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("arguments_hash", sa.CHAR(64), nullable=False),
        sa.Column("cached_result_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("record_id", name="pk_idempotency_records"),
    )
    op.create_unique_constraint("uq_idem_tenant_key", "idempotency_records", ["tenant_id", "idempotency_key"])
    op.create_foreign_key(
        "fk_idem_tenant",
        "idempotency_records",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_idem_audit_log",
        "idempotency_records",
        "tool_audit_logs",
        ["call_id"],
        ["call_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_idem_expires_at", "idempotency_records", ["expires_at"])
    op.create_index("ix_idem_call_id", "idempotency_records", ["call_id"])


def downgrade() -> None:
    op.drop_index("ix_idem_call_id", table_name="idempotency_records")
    op.drop_index("ix_idem_expires_at", table_name="idempotency_records")
    op.drop_constraint("fk_idem_audit_log", "idempotency_records", type_="foreignkey")
    op.drop_constraint("fk_idem_tenant", "idempotency_records", type_="foreignkey")
    op.drop_constraint("uq_idem_tenant_key", "idempotency_records", type_="unique")
    op.drop_table("idempotency_records")

    op.drop_index("ix_tool_audit_status", table_name="tool_audit_logs")
    op.drop_index("ix_tool_audit_idem_key", table_name="tool_audit_logs")
    op.drop_index("ix_tool_audit_tenant_tool_created", table_name="tool_audit_logs")
    op.drop_constraint("fk_tool_audit_actor", "tool_audit_logs", type_="foreignkey")
    op.drop_constraint("fk_tool_audit_tenant", "tool_audit_logs", type_="foreignkey")
    op.drop_constraint("ck_tool_audit_status_valid", "tool_audit_logs", type_="check")
    op.drop_table("tool_audit_logs")

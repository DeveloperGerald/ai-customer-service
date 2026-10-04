"""会话表迁移：conversation_threads（线程）+ conversation_messages（消息，T7 LangGraph Checkpointer 占位）。

Revision ID: 0005
Revises: 0004
Create Date: 2025-09-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ----- 1. conversation_threads -----
    op.create_table(
        "conversation_threads",
        sa.Column("thread_id", sa.String(length=128), nullable=False, comment='格式 "{tenant_id}:{uuid_hex}"'),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("initial_user_message", sa.Text(), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("escalated_ticket_no", sa.String(length=64), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("thread_id", name="pk_conversation_threads"),
    )
    op.create_check_constraint(
        "ck_threads_status_valid",
        "conversation_threads",
        "status IN ('open','escalated','closed')",
    )
    # thread_id 前缀必须等于 tenant_id:（面试防越权展示点）
    op.create_check_constraint(
        "ck_threads_thread_id_prefix_matches_tenant",
        "conversation_threads",
        "substring(thread_id from 1 for (char_length(tenant_id) + 1)) = (tenant_id || ':')",
    )
    op.create_foreign_key(
        "fk_threads_tenant",
        "conversation_threads",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_threads_owner",
        "conversation_threads",
        "users",
        ["tenant_id", "owner_user_id"],
        ["tenant_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_threads_tenant_owner_updated",
        "conversation_threads",
        ["tenant_id", "owner_user_id", "updated_at"],
    )
    op.create_index(
        "ix_threads_tenant_status",
        "conversation_threads",
        ["tenant_id", "status"],
    )
    op.create_index(
        "ix_threads_tenant_id",
        "conversation_threads",
        ["tenant_id"],
    )
    op.create_index(
        "ix_threads_last_message_at",
        "conversation_threads",
        ["last_message_at"],
    )

    # ----- 2. conversation_messages -----
    op.create_table(
        "conversation_messages",
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=True),
        sa.Column("tool_call_id", sa.CHAR(64), nullable=True),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("message_id", name="pk_conversation_messages"),
    )
    op.create_check_constraint(
        "ck_msgs_role_valid",
        "conversation_messages",
        "role IN ('human','agent','tool')",
    )
    op.create_unique_constraint(
        "uq_msgs_id_tenant",
        "conversation_messages",
        ["message_id", "tenant_id"],
    )
    op.create_foreign_key(
        "fk_msgs_tenant",
        "conversation_messages",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_msgs_actor",
        "conversation_messages",
        "users",
        ["tenant_id", "actor_id"],
        ["tenant_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_msgs_thread",
        "conversation_messages",
        "conversation_threads",
        ["thread_id"],
        ["thread_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_msgs_thread_created",
        "conversation_messages",
        ["thread_id", "created_at"],
    )
    op.create_index(
        "ix_msgs_tenant_tool_call",
        "conversation_messages",
        ["tenant_id", "tool_call_id"],
    )
    op.create_index(
        "ix_msgs_tenant_id",
        "conversation_messages",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_msgs_tenant_id", table_name="conversation_messages")
    op.drop_index("ix_msgs_tenant_tool_call", table_name="conversation_messages")
    op.drop_index("ix_msgs_thread_created", table_name="conversation_messages")
    op.drop_constraint("fk_msgs_thread", "conversation_messages", type_="foreignkey")
    op.drop_constraint("fk_msgs_actor", "conversation_messages", type_="foreignkey")
    op.drop_constraint("fk_msgs_tenant", "conversation_messages", type_="foreignkey")
    op.drop_constraint("uq_msgs_id_tenant", "conversation_messages", type_="unique")
    op.drop_constraint("ck_msgs_role_valid", "conversation_messages", type_="check")
    op.drop_table("conversation_messages")

    op.drop_index("ix_threads_last_message_at", table_name="conversation_threads")
    op.drop_index("ix_threads_tenant_id", table_name="conversation_threads")
    op.drop_index("ix_threads_tenant_status", table_name="conversation_threads")
    op.drop_index("ix_threads_tenant_owner_updated", table_name="conversation_threads")
    op.drop_constraint("fk_threads_owner", "conversation_threads", type_="foreignkey")
    op.drop_constraint("fk_threads_tenant", "conversation_threads", type_="foreignkey")
    op.drop_constraint(
        "ck_threads_thread_id_prefix_matches_tenant",
        "conversation_threads",
        type_="check",
    )
    op.drop_constraint("ck_threads_status_valid", "conversation_threads", type_="check")
    op.drop_table("conversation_threads")

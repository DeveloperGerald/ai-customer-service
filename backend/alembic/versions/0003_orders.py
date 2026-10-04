"""orders 表迁移（T3 订单物流只读）。

Revision ID: 0003
Revises: 0002
Create Date: 2025-09-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("order_no", sa.String(length=64), nullable=False),
        sa.Column("buyer_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending_payment"),
        sa.Column("total_amount_yuan", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("logistics_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("line_items_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("recipient_name_masked", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("recipient_phone_masked", sa.CHAR(11), nullable=False, server_default=""),
        sa.Column("recipient_address_masked", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("order_id", name="pk_orders"),
    )
    op.create_check_constraint(
        "ck_orders_status_valid",
        "orders",
        "status IN ('pending_payment','paid','shipped','delivered','completed','refunded','cancelled')",
    )
    op.create_check_constraint("ck_orders_total_non_negative", "orders", "total_amount_yuan >= 0")
    op.create_unique_constraint("uq_orders_tenant_order_no", "orders", ["tenant_id", "order_no"])
    op.create_foreign_key(
        "fk_orders_tenant",
        "orders",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_orders_buyer",
        "orders",
        "users",
        ["tenant_id", "buyer_user_id"],
        ["tenant_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_orders_tenant_buyer_created", "orders", ["tenant_id", "buyer_user_id", "created_at"])
    op.create_index("ix_orders_tenant_status", "orders", ["tenant_id", "status"])
    op.create_index("ix_orders_created_at", "orders", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_orders_created_at", table_name="orders")
    op.drop_index("ix_orders_tenant_status", table_name="orders")
    op.drop_index("ix_orders_tenant_buyer_created", table_name="orders")
    op.drop_constraint("fk_orders_buyer", "orders", type_="foreignkey")
    op.drop_constraint("fk_orders_tenant", "orders", type_="foreignkey")
    op.drop_constraint("uq_orders_tenant_order_no", "orders", type_="unique")
    op.drop_constraint("ck_orders_total_non_negative", "orders", type_="check")
    op.drop_constraint("ck_orders_status_valid", "orders", type_="check")
    op.drop_table("orders")

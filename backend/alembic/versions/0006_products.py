"""products 商品表迁移（商品目录只读查询 + 商品工具）。

Revision ID: 0006
Revises: 0005
Create Date: 2025-09-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=50), nullable=False),
        sa.Column("product_name", sa.String(length=200), nullable=False),
        sa.Column("sku_code", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="on_sale"),
        sa.Column("specs", sa.String(length=255), nullable=True),
        sa.Column("price_yuan", sa.Integer(), nullable=False),
        sa.Column("stock", sa.Integer(), nullable=False, server_default="0"),
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
        sa.PrimaryKeyConstraint("product_id", name="pk_products"),
    )
    op.create_check_constraint(
        "ck_products_status_valid",
        "products",
        "status IN ('on_sale','off_sale')",
    )
    op.create_check_constraint("ck_products_price_non_negative", "products", "price_yuan >= 0")
    op.create_check_constraint("ck_products_stock_non_negative", "products", "stock >= 0")
    op.create_unique_constraint("uq_products_tenant_sku", "products", ["tenant_id", "sku_code"])
    op.create_foreign_key(
        "fk_products_tenant",
        "products",
        "tenants",
        ["tenant_id"],
        ["tenant_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_products_tenant_status", "products", ["tenant_id", "status"])
    op.create_index("ix_products_tenant_category", "products", ["tenant_id", "category"])
    op.create_index("ix_products_tenant_created", "products", ["tenant_id", "created_at"])
    op.create_index("ix_products_tenant_id", "products", ["tenant_id"])

    # updated_at 由数据库触发器在行更新时刷新（PostgreSQL 无 ON UPDATE CURRENT_TIMESTAMP）
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS trigger AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_products_set_updated_at
        BEFORE UPDATE ON products
        FOR EACH ROW
        EXECUTE FUNCTION set_updated_at()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_products_set_updated_at ON products")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
    op.drop_index("ix_products_tenant_id", table_name="products")
    op.drop_index("ix_products_tenant_created", table_name="products")
    op.drop_index("ix_products_tenant_category", table_name="products")
    op.drop_index("ix_products_tenant_status", table_name="products")
    op.drop_constraint("fk_products_tenant", "products", type_="foreignkey")
    op.drop_constraint("uq_products_tenant_sku", "products", type_="unique")
    op.drop_constraint("ck_products_stock_non_negative", "products", type_="check")
    op.drop_constraint("ck_products_price_non_negative", "products", type_="check")
    op.drop_constraint("ck_products_status_valid", "products", type_="check")
    op.drop_table("products")

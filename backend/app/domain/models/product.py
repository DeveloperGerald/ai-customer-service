"""商品 ORM（products 表，按 tenant_id 强隔离的商品目录）。

与 orders 表一致，金额使用「元 × 100」整数列存储，避免浮点误差；
updated_at 由数据库触发器（0006 迁移中的 trg_products_set_updated_at）维护。
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import (
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
from sqlalchemy.dialects.postgresql import UUID

from app.core.infrastructure import Base


class ProductORM(Base):
    """商品表（同租户 SKU 唯一；status 控制上架/下架）。"""

    __tablename__ = "products"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_products_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "sku_code", name="uq_products_tenant_sku"),
        CheckConstraint(
            "status IN ('on_sale','off_sale')",
            name="ck_products_status_valid",
        ),
        CheckConstraint("price_yuan >= 0", name="ck_products_price_non_negative"),
        CheckConstraint("stock >= 0", name="ck_products_stock_non_negative"),
        Index("ix_products_tenant_status", "tenant_id", "status"),
        Index("ix_products_tenant_category", "tenant_id", "category"),
        Index("ix_products_tenant_created", "tenant_id", "created_at"),
    )

    product_id = Column(UUID(as_uuid=True), primary_key=True, default=lambda: uuid4())
    tenant_id = Column(String(50), nullable=False, index=True)

    product_name = Column(String(200), nullable=False, comment="商品名，例如 禅饰坊·星月菩提 108 颗手串")
    sku_code = Column(String(64), nullable=False, comment="SKU 编码，同租户内唯一")
    category = Column(String(64), nullable=False, comment="商品分类，例如 菩提/南红/紫檀/和田玉")
    status = Column(
        String(16),
        nullable=False,
        server_default="on_sale",
        comment="上架状态：on_sale=在售 / off_sale=下架",
    )
    specs = Column(String(255), nullable=True, comment="规格描述，例如 12mm·108颗")
    price_yuan = Column(Integer, nullable=False, comment="售价 × 100，避免浮点")
    stock = Column(Integer, nullable=False, server_default="0", comment="可售库存件数")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="行更新时由触发器 trg_products_set_updated_at 刷新",
    )

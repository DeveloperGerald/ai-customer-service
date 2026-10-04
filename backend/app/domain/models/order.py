"""订单 ORM（订单主表 + 内嵌物流 JSONB + 内嵌商品行 JSONB）。

MVP 为避免 JOIN 复杂物流表，直接把物流轨迹和商品行存 JSONB 列；
真实项目演进时可拆 logistics / order_items 两张子表，Repository 接口不变。
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


class OrderORM(Base):
    """订单主表（物流 + 商品行内嵌 JSONB，按 tenant_id 强隔离）。"""

    __tablename__ = "orders"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            name="fk_orders_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "buyer_user_id"],
            ["users.tenant_id", "users.user_id"],
            name="fk_orders_buyer",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("tenant_id", "order_no", name="uq_orders_tenant_order_no"),
        CheckConstraint(
            "status IN ('pending_payment','paid','shipped','delivered','completed','refunded','cancelled')",
            name="ck_orders_status_valid",
        ),
        CheckConstraint("total_amount_yuan >= 0", name="ck_orders_total_non_negative"),
        Index("ix_orders_tenant_buyer_created", "tenant_id", "buyer_user_id", "created_at"),
        Index("ix_orders_tenant_status", "tenant_id", "status"),
    )

    order_id = Column(UUID(as_uuid=True), primary_key=True, default=lambda: uuid4())
    tenant_id = Column(String(50), nullable=False, index=True)

    order_no = Column(String(64), nullable=False, comment="用户/客服展示用业务订单号")
    buyer_user_id = Column(UUID(as_uuid=True), nullable=False)

    status = Column(String(32), nullable=False, server_default="pending_payment", index=True)
    total_amount_yuan = Column(Integer, nullable=False, default=0, comment="总金额 × 100，避免浮点")

    # ---- 内嵌 JSONB：物流 ----
    logistics_json = Column(
        JSONB(astext_type=None),
        nullable=True,
        comment="OrderLogistics JSON：{carrier_name,tracking_no,current_status,delivered_at,tracks:[...]}",
    )

    # ---- 内嵌 JSONB：商品行（通常 1 行，预留数组扩展） ----
    line_items_json = Column(
        JSONB(astext_type=None),
        nullable=False,
        default=list,
        comment="list[LineItem]：[{sku_id,product_name,product_type,unit_price_yuan,quantity}, ...]",
    )

    # ---- 收件人脱敏字段（入库前脱敏，或直接存脱敏值，此处直接存脱敏后值）----
    recipient_name_masked = Column(String(64), nullable=False, server_default="")
    recipient_phone_masked = Column(CHAR(11), nullable=False, server_default="")
    recipient_address_masked = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)

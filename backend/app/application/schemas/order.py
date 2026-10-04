"""订单与物流 Pydantic Schema（只读查询用，T8 退款流程消费）。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

OrderStatus = Literal["pending_payment", "paid", "shipped", "delivered", "completed", "refunded", "cancelled"]
LogisticsStatus = Literal["pending", "picked_up", "in_transit", "out_for_delivery", "delivered", "exception"]
ProductType = Literal["standard", "custom", "limited"]

_VALID_ORDER_STATUS = {"pending_payment", "paid", "shipped", "delivered", "completed", "refunded", "cancelled"}
_VALID_LOGISTICS_STATUS = {"pending", "picked_up", "in_transit", "out_for_delivery", "delivered", "exception"}
_VALID_PRODUCT_TYPE = {"standard", "custom", "limited"}


class OrderStatusE(Enum):
    PENDING_PAYMENT = "pending_payment"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    COMPLETED = "completed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class LogisticsTrackPoint(BaseModel):
    """单条物流轨迹点。"""

    happened_at: datetime
    status: LogisticsStatus
    description: str = Field(..., max_length=255)
    location: str | None = Field(default=None, max_length=128)

    @field_validator("status")
    @classmethod
    def _check_logi_status(cls, v: str) -> str:
        if v not in _VALID_LOGISTICS_STATUS:
            raise ValueError(f"invalid logistics_status: {v}")
        return v


class OrderLogistics(BaseModel):
    """订单内嵌物流信息（为简化 MVP，1 order → 1 条物流主单）。"""

    carrier_name: str = Field(..., max_length=64, description="快递公司，例如 顺丰/圆通/EMS")
    tracking_no: str = Field(..., max_length=64, description="快递单号")
    current_status: LogisticsStatus = Field(..., description="当前物流状态")
    delivered_at: datetime | None = Field(default=None, description="签收时间（签收时赋值，用于判断 7 天窗口）")
    latest_track: LogisticsTrackPoint | None = Field(default=None)
    tracks: list[LogisticsTrackPoint] = Field(default_factory=list, description="轨迹列表，按时间升序")

    @field_validator("current_status")
    @classmethod
    def _check(cls, v: str) -> str:
        if v not in _VALID_LOGISTICS_STATUS:
            raise ValueError(f"invalid logistics_status: {v}")
        return v


class LineItem(BaseModel):
    """单行商品（MVP 每订单仅 1 行，保留数组方便扩展）。"""

    sku_id: str = Field(..., max_length=64)
    product_name: str = Field(..., max_length=255)
    product_type: ProductType = Field(default="standard", description="standard=普通款 / custom=定制款 / limited=限量款")
    unit_price_yuan: int = Field(..., ge=0, description="单价，单位 元 × 100（整数存储避免浮点误差）")
    quantity: int = Field(..., ge=1, le=99)

    @property
    def subtotal_yuan(self) -> int:
        return self.unit_price_yuan * self.quantity

    @field_validator("product_type")
    @classmethod
    def _pt(cls, v: str) -> str:
        if v not in _VALID_PRODUCT_TYPE:
            raise ValueError(f"invalid product_type: {v}")
        return v


class OrderRead(BaseModel):
    """订单查询响应（对外输出）。"""

    model_config = ConfigDict(from_attributes=True)

    order_id: UUID
    tenant_id: str
    order_no: str = Field(..., max_length=64, description="用户可见订单号，展示用")
    buyer_user_id: UUID = Field(description="买家 user_id，跨表与 users 绑定")
    status: OrderStatus
    total_amount_yuan: int = Field(..., ge=0, description="总金额 × 100")
    line_items: list[LineItem] = Field(default_factory=list)
    logistics: OrderLogistics | None = Field(default=None, description="发货后非空")
    recipient_name_masked: str = Field(..., max_length=64, description="收件人姓名脱敏：张*三")
    recipient_phone_masked: str = Field(..., max_length=32, description="收件人手机号脱敏：138****1234")
    recipient_address_masked: str | None = Field(default=None, max_length=255, description="收件地址脱敏")
    created_at: datetime
    paid_at: datetime | None = None


class OrderListItem(BaseModel):
    """列表页用精简字段（不展开物流轨迹）。"""

    model_config = ConfigDict(from_attributes=True)

    order_id: UUID
    tenant_id: str
    order_no: str
    buyer_user_id: UUID
    status: OrderStatus
    total_amount_yuan: int
    product_type_summary: str | None = Field(default=None, max_length=64, description="商品类型简述：standard/custom")
    created_at: datetime


class OrderQueryFilter(BaseModel):
    """列表查询参数（FastAPI Query 解析后包装）。"""

    status_in: set[OrderStatus] | None = Field(default=None)
    product_type: ProductType | None = None
    days_since_delivered_le: int | None = Field(default=None, ge=0, description="签收至今天数 ≤X，用于退款判断")
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

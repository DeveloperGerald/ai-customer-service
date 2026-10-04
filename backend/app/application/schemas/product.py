"""商品 Pydantic Schema（只读目录查询用，商品工具与 /api/products 消费）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ProductStatus = Literal["on_sale", "off_sale"]


class ProductRead(BaseModel):
    """商品详情/列表统一输出（商品字段均为轻量标量，list 与 get 共用）。"""

    model_config = ConfigDict(from_attributes=True)

    product_id: UUID
    tenant_id: str
    product_name: str = Field(..., max_length=200)
    sku_code: str = Field(..., max_length=64, description="SKU 编码，同租户唯一")
    category: str = Field(..., max_length=64, description="商品分类")
    status: ProductStatus = Field(..., description="on_sale=在售 / off_sale=下架")
    specs: str | None = Field(default=None, max_length=255, description="规格描述，例如 12mm·108颗")
    price_yuan: int = Field(..., ge=0, description="售价，单位 元 × 100（整数存储避免浮点误差）")
    stock: int = Field(..., ge=0, description="可售库存件数")
    created_at: datetime
    updated_at: datetime


class ProductQueryFilter(BaseModel):
    """商品列表查询参数（FastAPI Query 解析后包装）。"""

    name_like: str | None = Field(default=None, max_length=200, description="商品名模糊匹配关键词")
    category: str | None = Field(default=None, max_length=64)
    status: ProductStatus | None = Field(default=None, description="不传则返回全部上架状态")
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

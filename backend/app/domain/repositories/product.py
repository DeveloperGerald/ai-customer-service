"""商品只读 Repository（/api/products 与 product_list/product_query 工具消费）。

关键约束：
1. 所有方法**强制显式 tenant_id 参数**，不读 ContextVar，保证单测独立于 HTTP 层。
2. 商品目录对同租户所有角色可见；是否按上架状态过滤由调用方通过 ProductQueryFilter.status 决定。
3. 对「不存在 / 跨租户」统一抛 ResourceNotFound（所有查询本身都带 tenant_id 条件，不存在跨租户行）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.schemas.product import ProductQueryFilter, ProductRead
from app.core.errors import ErrorCode, ResourceNotFoundError
from app.domain.models.product import ProductORM


def _escape_ilike(keyword: str) -> str:
    """转义 ILIKE 通配符（反斜杠必须最先替换），避免用户输入 %/_ 被当通配符。"""
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _orm_to_read(row: ProductORM) -> ProductRead:
    return ProductRead.model_validate(row)


class ProductRepository:
    """商品只读仓储（当前版本无写接口，录入走后续管理端模块）。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------- 读（ORM 行，内部使用）----------

    async def get_by_id(self, tenant_id: str, product_id: UUID) -> ProductORM | None:
        stmt = select(ProductORM).where(
            ProductORM.tenant_id == tenant_id,
            ProductORM.product_id == product_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_sku(self, tenant_id: str, sku_code: str) -> ProductORM | None:
        stmt = select(ProductORM).where(
            ProductORM.tenant_id == tenant_id,
            ProductORM.sku_code == sku_code,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---------- 读（对外 Schema）----------

    async def get_read_by_id(self, tenant_id: str, product_id: UUID) -> ProductRead:
        """按主键读商品；不存在（含跨租户）统一 ResourceNotFound。"""
        row = await self.get_by_id(tenant_id, product_id)
        if row is None:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "product", "resource_id": str(product_id)},
            )
        return _orm_to_read(row)

    async def get_read_by_sku(self, tenant_id: str, sku_code: str) -> ProductRead:
        """按 SKU 编码读商品；不存在（含跨租户）统一 ResourceNotFound。"""
        row = await self.get_by_sku(tenant_id, sku_code)
        if row is None:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "product", "sku_code": sku_code},
            )
        return _orm_to_read(row)

    async def list_products(
        self,
        tenant_id: str,
        *,
        flt: ProductQueryFilter | None = None,
    ) -> list[ProductRead]:
        """列商品：tenant_id 强隔离 + 商品名 ILIKE 模糊 + 分类/上架状态可选过滤 + 分页。"""
        flt = flt or ProductQueryFilter()
        clauses: list[Any] = [ProductORM.tenant_id == tenant_id]
        if flt.name_like:
            pattern = f"%{_escape_ilike(flt.name_like)}%"
            clauses.append(ProductORM.product_name.ilike(pattern, escape="\\"))
        if flt.category:
            clauses.append(ProductORM.category == flt.category)
        if flt.status is not None:
            clauses.append(ProductORM.status == flt.status)
        stmt = (
            select(ProductORM)
            .where(and_(*clauses))
            .order_by(ProductORM.created_at.desc(), ProductORM.product_id.desc())
            .limit(flt.limit)
            .offset(flt.offset)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_orm_to_read(row) for row in rows]

"""商品目录只读查询接口（供 Agent 工具调用 + 前端商品咨询页）。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import require_actor
from app.application.schemas.product import (
    ProductQueryFilter,
    ProductRead,
    ProductStatus,
)
from app.domain.repositories.identity import Actor
from app.domain.repositories.product import ProductRepository
from app.infrastructure.db.engine import scoped_db_session

router = APIRouter(prefix="/api/products", tags=["products"])


async def _session(request: Request) -> AsyncSession:
    async with scoped_db_session(request.app.state.bundle.infra) as sess:
        yield sess


@router.get("", response_model=list[ProductRead])
async def list_products(
    request: Request,
    name: str | None = Query(default=None, max_length=200, description="商品名模糊查询关键词"),
    category: str | None = Query(default=None, max_length=64),
    status: ProductStatus | None = Query(default=None, description="上架状态筛选，不传返回全部"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> list[ProductRead]:
    """按 actor 所属租户查询商品列表（名称模糊 + 分类 + 上架状态 + 分页）。"""
    repo = ProductRepository(session)
    flt = ProductQueryFilter(
        name_like=name,
        category=category,
        status=status,
        limit=limit,
        offset=offset,
    )
    rows = await repo.list_products(actor.tenant_id, flt=flt)
    await session.commit()
    return rows


@router.get("/{product_id}", response_model=ProductRead)
async def get_product_detail(
    product_id: UUID,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> ProductRead:
    """查指定商品详情；跨租户/不存在统一 ResourceNotFound。"""
    repo = ProductRepository(session)
    result = await repo.get_read_by_id(actor.tenant_id, product_id)
    await session.commit()
    return result

"""订单只读查询接口（供 Agent 工具调用 + 前端用户「我的订单」页）。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import require_actor
from app.application.schemas.order import (
    OrderListItem,
    OrderQueryFilter,
    OrderRead,
    OrderStatus,
    ProductType,
)
from app.domain.repositories.identity import Actor
from app.domain.repositories.order import OrderRepository
from app.infrastructure.db.engine import scoped_db_session

router = APIRouter(prefix="/api/orders", tags=["orders"])


async def _session(request: Request) -> AsyncSession:
    async with scoped_db_session(request.app.state.bundle.infra) as sess:
        yield sess


@router.get("", response_model=list[OrderListItem])
async def list_orders(
    request: Request,
    status: list[OrderStatus] | None = Query(default=None),
    product_type: ProductType | None = None,
    days_since_delivered_le: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> list[OrderListItem]:
    """按 actor 所属租户查询订单列表。

    - consumer：只返回自己的订单
    - staff/admin：返回同租户全部订单
    """
    repo = OrderRepository(session)
    flt = OrderQueryFilter(
        status_in=set(status) if status else None,
        product_type=product_type,
        days_since_delivered_le=days_since_delivered_le,
        limit=limit,
        offset=offset,
    )
    rows = await repo.list_for_actor(actor, actor.tenant_id, flt=flt)
    await session.commit()
    return rows


@router.get("/{order_id}", response_model=OrderRead)
async def get_order_detail(
    order_id: UUID,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> OrderRead:
    """查指定订单详情；consumer 仅能查自己的，越权统一 ResourceNotFound。"""
    repo = OrderRepository(session)
    result = await repo.get_read_for_actor(actor, actor.tenant_id, order_id)
    await session.commit()
    return result

"""订单只读 Repository（T8 退款流程、T4 OrderQueryTool 消费）。

关键约束：
1. 所有方法**强制显式 tenant_id 参数**，不读 ContextVar，保证单测独立于 HTTP 层。
2. 对「不存在 / 跨租户 / 同租户他人」三种场景统一抛 ResourceNotFound（存在性不泄露）。
3. 商品行 & 物流从 JSONB 列解析成 Pydantic 对象，将来拆表时 Repository 接口不变。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.schemas.order import (
    LineItem,
    OrderListItem,
    OrderLogistics,
    OrderQueryFilter,
    OrderRead,
)
from app.core.errors import ErrorCode, ResourceNotFoundError, ToolExecutionError
from app.domain.models.order import OrderORM
from app.domain.repositories.identity import Actor

# 订单状态机：合法流转映射（key=当前 status → set[可流转到的 status]）
# 终态（completed/refunded/cancelled）不允许再流转；同一 status 视为 no-op 不在映射内。
_ORDER_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending_payment": frozenset({"cancelled"}),
    "paid": frozenset({"cancelled", "refunded"}),
    "shipped": frozenset({"refunded"}),
    "delivered": frozenset({"refunded"}),
    # completed/refunded/cancelled 为终态，无任何合法流转
    "completed": frozenset(),
    "refunded": frozenset(),
    "cancelled": frozenset(),
}


def _mask_name(name: str) -> str:
    if not name:
        return ""
    if len(name) == 1:
        return name + "*"
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]


def _mask_phone(phone: str) -> str:
    if not phone or len(phone) < 7:
        return ""
    return phone[:3] + "****" + phone[-4:]


def _mask_address(addr: str | None) -> str | None:
    if not addr or len(addr) < 4:
        return addr
    return addr[:4] + "***" + addr[-2:]


def _build_logistics_from_json(data: dict[str, Any] | None) -> OrderLogistics | None:
    if not data:
        return None
    return OrderLogistics(**data)


def _build_line_items(data: list[dict[str, Any]] | None) -> list[LineItem]:
    if not data:
        return []
    return [LineItem(**row) for row in data]


def _orm_to_read(row: OrderORM) -> OrderRead:
    return OrderRead(
        order_id=row.order_id,
        tenant_id=row.tenant_id,
        order_no=row.order_no,
        buyer_user_id=row.buyer_user_id,
        status=row.status,
        total_amount_yuan=row.total_amount_yuan,
        line_items=_build_line_items(row.line_items_json),
        logistics=_build_logistics_from_json(row.logistics_json),
        recipient_name_masked=row.recipient_name_masked,
        recipient_phone_masked=row.recipient_phone_masked,
        recipient_address_masked=row.recipient_address_masked,
        created_at=row.created_at,
        paid_at=row.paid_at,
    )


def _orm_to_list_item(row: OrderORM) -> OrderListItem:
    items = _build_line_items(row.line_items_json)
    types = {it.product_type for it in items}
    summary = ",".join(sorted(types)) if types else None
    return OrderListItem(
        order_id=row.order_id,
        tenant_id=row.tenant_id,
        order_no=row.order_no,
        buyer_user_id=row.buyer_user_id,
        status=row.status,
        total_amount_yuan=row.total_amount_yuan,
        product_type_summary=summary,
        created_at=row.created_at,
    )


class OrderRepository:
    """订单只读仓储（upsert_by_order_no 仅种子使用）。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------- 工具：查询 ----------

    def _base_where(self, tenant_id: str) -> Any:
        return OrderORM.tenant_id == tenant_id

    # ---------- 读（公共 API）----------

    async def get_by_id(self, tenant_id: str, order_id: UUID) -> OrderORM | None:
        stmt = select(OrderORM).where(
            OrderORM.tenant_id == tenant_id,
            OrderORM.order_id == order_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_order_no(self, tenant_id: str, order_no: str) -> OrderORM | None:
        stmt = select(OrderORM).where(
            OrderORM.tenant_id == tenant_id,
            OrderORM.order_no == order_no,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_read_for_actor(self, actor: Actor, tenant_id: str, order_id: UUID) -> OrderRead:
        """对外读取：consumer 只能看自己；staff/admin 看同租户。统一 ResourceNotFound。"""
        if actor.tenant_id.lower() != tenant_id.lower():
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "order", "resource_id": str(order_id)},
            )
        row = await self.get_by_id(tenant_id, order_id)
        if row is None:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "order", "resource_id": str(order_id)},
            )
        # 权限：consumer 仅自己可见（统一 ResourceNotFound，不泄露存在性）
        if actor.role.value == "consumer" and str(row.buyer_user_id) != actor.actor_id:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "order", "resource_id": str(order_id)},
            )
        return _orm_to_read(row)

    async def list_for_actor(
        self,
        actor: Actor,
        tenant_id: str,
        *,
        flt: OrderQueryFilter | None = None,
    ) -> list[OrderListItem]:
        """列表查询：consumer→仅自己；staff/admin→同租户全量（可加状态筛选）。"""
        flt = flt or OrderQueryFilter()
        if actor.tenant_id.lower() != tenant_id.lower():
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "order_list", "resource_id": tenant_id},
            )
        clauses = [self._base_where(tenant_id)]
        if actor.role.value == "consumer":
            clauses.append(OrderORM.buyer_user_id == UUID(actor.actor_id))
        if flt.status_in:
            clauses.append(OrderORM.status.in_(flt.status_in))
        if flt.days_since_delivered_le is not None:
            # 通过 logistics_json→>delivered_at 判断（PG jsonb operator：->>）
            days = flt.days_since_delivered_le
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
            # MVP：此处只在 SQL 层加 created_at 宽松过滤；真实生产建议拆 delivered_at 独立列 + GIN 索引
            clauses.append(OrderORM.created_at >= cutoff - _dt.timedelta(days=30))
        stmt = (
            select(OrderORM)
            .where(and_(*clauses))
            .order_by(OrderORM.created_at.desc())
            .limit(flt.limit)
            .offset(flt.offset)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        if flt.product_type is not None:
            # JSONB product_type 在 SQL 中过滤需要 jsonb 操作符，MVP 在内存层筛（面试演示数据量小）
            pt = flt.product_type
            rows = [
                r
                for r in rows
                if any(str(it.get("product_type", "")) == pt for it in (r.line_items_json or []))
            ]
        if flt.days_since_delivered_le is not None:
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=flt.days_since_delivered_le)
            kept = []
            for r in rows:
                logi = _build_logistics_from_json(r.logistics_json)
                deli = logi.delivered_at if logi else None
                if deli is None:
                    continue
                if deli >= cutoff:
                    kept.append(r)
            rows = kept
        return [_orm_to_list_item(r) for r in rows]

    # ---------- 写：状态流转（refund/cancel 用，强制 tenant_id + 状态机校验）----------

    async def update_status(
        self,
        tenant_id: str,
        order_id: UUID,
        new_status: str,
        *,
        actor: Actor | None = None,
    ) -> OrderORM:
        """状态机校验 + 租户隔离的状态流转（refund/cancel 写工具消费）。

        - tenant_id 强隔离：跨租户/不存在统一 ResourceNotFoundError（不泄露存在性）。
        - actor 非空时做权限校验：consumer 仅自己订单可流转（统一 ResourceNotFound）。
        - 状态机：`_ORDER_STATUS_TRANSITIONS` 定义合法流转；终态/非法流转抛
          ToolExecutionError(VALIDATION_ERROR, retryable=False)。
        - 同 status 不在映射内，按非法流转处理（防重复写入）。
        - 不 commit（由调用方控制事务边界）；flush 保证本 session 后续读可见。
        """
        row = await self.get_by_id(tenant_id, order_id)
        if row is None:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "order", "resource_id": str(order_id)},
            )
        if actor is not None and actor.role.value == "consumer" and str(row.buyer_user_id) != actor.actor_id:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "order", "resource_id": str(order_id)},
            )
        allowed = _ORDER_STATUS_TRANSITIONS.get(row.status, frozenset())
        if new_status not in allowed:
            raise ToolExecutionError(
                ErrorCode.VALIDATION_ERROR,
                message=f"订单状态 {row.status} 不允许流转到 {new_status}",
                retryable=False,
                details={
                    "order_id": str(order_id),
                    "current_status": row.status,
                    "attempted_status": new_status,
                },
            )
        row.status = new_status
        await self.session.flush()
        return row

    # ---------- 写：仅 seed 使用（Upsert 按 (tenant_id, order_no)）----------

    async def upsert_by_order_no(
        self,
        tenant_id: str,
        order_no: str,
        *,
        fixed_order_id: UUID | None,
        buyer_user_id: UUID,
        status: str,
        total_amount_yuan: int,
        line_items: list[LineItem],
        logistics: OrderLogistics | None,
        recipient_name: str,
        recipient_phone: str,
        recipient_address: str | None,
        created_at: _dt.datetime,
        paid_at: _dt.datetime | None,
    ) -> OrderORM:
        """seed_orders.py 用：INSERT ON CONFLICT DO UPDATE 幂等。

        不做权限校验（seed 没有 HTTP actor）。
        """
        line_items_raw = [it.model_dump(mode="json") for it in line_items]
        logistics_raw = logistics.model_dump(mode="json") if logistics else None
        values: dict[str, Any] = dict(
            tenant_id=tenant_id,
            order_no=order_no,
            buyer_user_id=buyer_user_id,
            status=status,
            total_amount_yuan=total_amount_yuan,
            line_items_json=line_items_raw,
            logistics_json=logistics_raw,
            recipient_name_masked=_mask_name(recipient_name),
            recipient_phone_masked=_mask_phone(recipient_phone),
            recipient_address_masked=_mask_address(recipient_address),
            created_at=created_at,
            paid_at=paid_at,
        )
        if fixed_order_id is not None:
            values["order_id"] = fixed_order_id
        upsert = (
            insert(OrderORM)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["tenant_id", "order_no"],
                set_={k: v for k, v in values.items() if k not in {"tenant_id", "order_no"}},
            )
        )
        await self.session.execute(upsert)
        await self.session.flush()
        return await self.get_by_order_no(tenant_id, order_no)  # type: ignore[return-value]

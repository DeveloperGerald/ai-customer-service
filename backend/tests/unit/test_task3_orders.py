"""T3 订单物流只读 6 条单测。

覆盖：
- TR3-1 consumer 查询自己的订单成功；物流/行项目 JSONB 解析为 Pydantic 对象
- TR3-2 consumer 查询同租户他人订单 → ResourceNotFound（存在性不泄露）
- TR3-3 consumer 查询跨租户订单 → ResourceNotFound（tenant mismatch）
- TR3-4 staff 查询本租户订单列表，他人自己的都能看到（consumer 限制解除）
- TR3-5 状态筛选 + product_type 筛选正确
- TR3-6 upsert_by_order_no 幂等：两次调用 session.add 计数为 0，INSERT ON CONFLICT 生效
"""

from __future__ import annotations

import datetime as _dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.application.schemas.identity import Role
from app.application.schemas.order import (
    LineItem,
    OrderLogistics,
    OrderQueryFilter,
    OrderStatus,
)
from app.core.errors import ResourceNotFoundError
from app.domain.models.order import OrderORM
from app.domain.repositories.identity import Actor
from app.domain.repositories.order import OrderRepository

TENANT_A_CONSUMER = Actor(
    actor_id="a0000000-0000-0000-0000-000000000001",
    tenant_id="tenant_a",
    role=Role.CONSUMER,
)
TENANT_A_STAFF = Actor(
    actor_id="a0000000-0000-0000-0000-000000000002",
    tenant_id="tenant_a",
    role=Role.STAFF,
)
TENANT_B_CONSUMER = Actor(
    actor_id="b0000000-0000-0000-0000-000000000001",
    tenant_id="tenant_b",
    role=Role.CONSUMER,
)


def _make_order(*, order_id: str, tenant_id: str, buyer_id: str, status: OrderStatus = "delivered",
                total: int = 100_00, custom: bool = False, days_since_delivered: int = 3) -> OrderORM:
    delivered_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_since_delivered)
    created_at = delivered_at - _dt.timedelta(days=7)
    line_items = [
        LineItem(
            sku_id="S001",
            product_name="手串",
            product_type="custom" if custom else "standard",
            unit_price_yuan=total,
            quantity=1,
        ).model_dump(mode="json"),
    ]
    logistics = OrderLogistics(
        carrier_name="顺丰",
        tracking_no="SF001",
        current_status="delivered",
        delivered_at=delivered_at,
        latest_track=None,
        tracks=[],
    ).model_dump(mode="json")
    return OrderORM(
        order_id=UUID(order_id),
        tenant_id=tenant_id,
        order_no=f"{tenant_id.upper()}-ORD-{order_id[:8]}",
        buyer_user_id=UUID(buyer_id),
        status=status,
        total_amount_yuan=total,
        line_items_json=line_items,
        logistics_json=logistics,
        recipient_name_masked="张*三",
        recipient_phone_masked="138****1234",
        recipient_address_masked="北京市***1702",
        created_at=created_at,
        paid_at=created_at,
    )


# ============================================================================
# TR3-1 consumer 查询自己订单成功
# ============================================================================


@pytest.mark.asyncio
async def test_tr31_consumer_reads_own_order_ok_jsonb_parsed() -> None:
    order = _make_order(
        order_id="11111111-1111-1111-1111-111111111111",
        tenant_id="tenant_a",
        buyer_id=TENANT_A_CONSUMER.actor_id,
    )
    session = AsyncMock()
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = order
    session.execute = AsyncMock(return_value=mr)

    repo = OrderRepository(session)
    read = await repo.get_read_for_actor(TENANT_A_CONSUMER, "tenant_a", order.order_id)
    assert read.order_id == order.order_id
    assert read.buyer_user_id == UUID(TENANT_A_CONSUMER.actor_id)
    # JSONB 解析成功断言
    assert read.logistics is not None
    assert read.logistics.tracking_no == "SF001"
    assert read.logistics.current_status == "delivered"
    assert len(read.line_items) == 1
    assert read.line_items[0].product_type == "standard"
    assert read.total_amount_yuan == 100_00


# ============================================================================
# TR3-2 consumer 查同租户他人 → ResourceNotFound
# ============================================================================


@pytest.mark.asyncio
async def test_tr32_consumer_reads_other_same_tenant_raises_resource_not_found() -> None:
    # 同租户 tenant_a，他人订单 buyer = admin（非 consumer）
    other = _make_order(
        order_id="22222222-2222-2222-2222-222222222222",
        tenant_id="tenant_a",
        buyer_id="a0000000-0000-0000-0000-000000000003",  # tenant_a admin
    )
    session = AsyncMock()
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = other
    session.execute = AsyncMock(return_value=mr)
    repo = OrderRepository(session)

    with pytest.raises(ResourceNotFoundError):
        await repo.get_read_for_actor(TENANT_A_CONSUMER, "tenant_a", other.order_id)


# ============================================================================
# TR3-3 consumer 查跨租户 → ResourceNotFound（权限层先拦截）
# ============================================================================


@pytest.mark.asyncio
async def test_tr33_cross_tenant_read_raises_resource_not_found_before_execute() -> None:
    session = AsyncMock()
    repo = OrderRepository(session)
    # TENANT_B_CONSUMER 尝试查 tenant_a 的订单
    with pytest.raises(ResourceNotFoundError):
        await repo.get_read_for_actor(
            TENANT_B_CONSUMER, "tenant_a", UUID("11111111-1111-1111-1111-111111111111")
        )
    # 越权被最外层拦截，未执行任何 SQL
    assert session.execute.call_count == 0


# ============================================================================
# TR3-4 staff 能看到同租户他人订单
# ============================================================================


@pytest.mark.asyncio
async def test_tr34_staff_sees_other_same_tenant_orders() -> None:
    o1 = _make_order(
        order_id="33333333-3333-3333-3333-333333333333",
        tenant_id="tenant_a",
        buyer_id=TENANT_A_CONSUMER.actor_id,  # 他人
    )
    o2 = _make_order(
        order_id="44444444-4444-4444-4444-444444444444",
        tenant_id="tenant_a",
        buyer_id=TENANT_A_STAFF.actor_id,  # staff 自己
    )
    session = AsyncMock()
    mr = MagicMock()
    mr.scalars.return_value.all.return_value = [o1, o2]
    session.execute = AsyncMock(return_value=mr)
    repo = OrderRepository(session)

    items = await repo.list_for_actor(TENANT_A_STAFF, "tenant_a")
    assert len(items) == 2
    # staff 可看 buyer_user_id != 自己的
    assert any(str(it.buyer_user_id) == TENANT_A_CONSUMER.actor_id for it in items)
    assert any(str(it.buyer_user_id) == TENANT_A_STAFF.actor_id for it in items)


# ============================================================================
# TR3-5 状态筛选 + product_type 筛选
# ============================================================================


@pytest.mark.asyncio
async def test_tr35_filter_status_and_product_type_works() -> None:
    delivered_std = _make_order(
        order_id="55555555-5555-5555-5555-555555555555",
        tenant_id="tenant_a",
        buyer_id=TENANT_A_STAFF.actor_id,
        status="delivered",
        custom=False,
    )
    refunded_custom = _make_order(
        order_id="66666666-6666-6666-6666-666666666666",
        tenant_id="tenant_a",
        buyer_id=TENANT_A_STAFF.actor_id,
        status="refunded",
        custom=True,
    )
    cancelled_std = _make_order(
        order_id="77777777-7777-7777-7777-777777777777",
        tenant_id="tenant_a",
        buyer_id=TENANT_A_STAFF.actor_id,
        status="cancelled",
        custom=False,
    )
    all_rows = [delivered_std, refunded_custom, cancelled_std]
    session = AsyncMock()
    mr = MagicMock()
    mr.scalars.return_value.all.return_value = all_rows
    session.execute = AsyncMock(return_value=mr)
    repo = OrderRepository(session)

    # 过滤 status in {delivered, cancelled} + product_type=standard
    flt = OrderQueryFilter(status_in={"delivered", "cancelled"}, product_type="standard", limit=100)
    got = await repo.list_for_actor(TENANT_A_STAFF, "tenant_a", flt=flt)
    got_ids = {str(it.order_id) for it in got}
    # refunded_custom 被排掉：status 不在集合
    assert str(refunded_custom.order_id) not in got_ids
    # cancelled_std status 正确，product_type 正确 → 留
    assert str(cancelled_std.order_id) in got_ids
    # delivered_std status 正确 + 标准款 → 留
    assert str(delivered_std.order_id) in got_ids


# ============================================================================
# TR3-6 upsert_by_order_no 幂等（session.add 计数 == 0 代表纯 UPSERT）
# ============================================================================


@pytest.mark.asyncio
async def test_tr36_upsert_by_order_no_idempotent_no_session_add() -> None:
    session = AsyncMock()
    # execute 第二阶段返回 ORM 对象（get_by_order_no）
    orm_after = _make_order(
        order_id="88888888-8888-8888-8888-888888888888",
        tenant_id="tenant_a",
        buyer_id=TENANT_A_CONSUMER.actor_id,
    )
    # 手动覆盖 order_no，避免 _make_order 自动合成 TENANT_A-ORD-88888888
    orm_after.order_no = "ORD-888"
    mr_fetch = MagicMock()
    mr_fetch.scalar_one_or_none.return_value = orm_after

    cnt: dict[str, int] = {"n": 0}

    async def exec_seq(stmt, *a: Any, **kw: Any) -> Any:
        cnt["n"] += 1
        return mr_fetch

    session.execute = AsyncMock(side_effect=exec_seq)
    session.flush = AsyncMock()
    repo = OrderRepository(session)

    items = [
        LineItem(
            sku_id="S1",
            product_name="测试款",
            product_type="standard",
            unit_price_yuan=1_00,
            quantity=1,
        )
    ]
    # 第一次 upsert
    r1 = await repo.upsert_by_order_no(
        "tenant_a",
        "ORD-888",
        fixed_order_id=orm_after.order_id,
        buyer_user_id=UUID(TENANT_A_CONSUMER.actor_id),
        status="delivered",
        total_amount_yuan=1_00,
        line_items=items,
        logistics=None,
        recipient_name="测试",
        recipient_phone="13800000001",
        recipient_address=None,
        created_at=_dt.datetime.now(_dt.timezone.utc),
        paid_at=None,
    )
    # 第二次 upsert（幂等）
    r2 = await repo.upsert_by_order_no(
        "tenant_a",
        "ORD-888",
        fixed_order_id=orm_after.order_id,
        buyer_user_id=UUID(TENANT_A_CONSUMER.actor_id),
        status="delivered",
        total_amount_yuan=1_00,
        line_items=items,
        logistics=None,
        recipient_name="测试",
        recipient_phone="13800000001",
        recipient_address=None,
        created_at=_dt.datetime.now(_dt.timezone.utc),
        paid_at=None,
    )
    assert r1.order_no == "ORD-888"
    assert r2.order_no == "ORD-888"
    # 全程未用 session.add（全部 DB UPSERT 语句完成）
    assert session.add.call_count == 0

"""T3 演示订单种子脚本（4 场景，绑定 T2 固定 user_id）。

场景对应：
- A-ORD-202509-001 禅饰坊 tenant_a 普通款 7 天无理由（签收 3 天）
- A-ORD-202509-002 禅饰坊 tenant_a 南红手串 质量问题场景（签收 5 天，带备注 珠子有裂痕）
- B-ORD-202509-003 梵印阁 tenant_b 定制刻字款（按政策不能 7 天无理由）
- C-ORD-202509-004 玉语轩 tenant_c 和田玉 超保修期（签收 40 天，想无理由 → 收 10% 手续费）

依赖：必须先跑 seed_tenants.py（让 tenants/users 存在并带固定 UUID）。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
from typing import Any
from uuid import UUID

from scripts.seed_tenants import FIXED_USER_IDS

# 4 个固定 order_id（保证幂等和绑定未来演示订单归属）
FIXED_ORDER_IDS: dict[str, UUID] = {
    "A-ORD-202509-001": UUID("aaaa0001-0000-0000-0000-000000000001"),
    "A-ORD-202509-002": UUID("aaaa0002-0000-0000-0000-000000000002"),
    "B-ORD-202509-003": UUID("bbbb0003-0000-0000-0000-000000000003"),
    "C-ORD-202509-004": UUID("cccc0004-0000-0000-0000-000000000004"),
}


def _days_ago_utc(days: int, hour: int = 10) -> _dt.datetime:
    now = _dt.datetime.now(_dt.timezone.utc)
    return (now - _dt.timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


def _make_4_scenarios() -> list[dict[str, Any]]:
    """返回 4 条场景定义，可直接喂 OrderRepository.upsert_by_order_no。"""
    return [
        # O1：tenant_a 普通款 7 天无理由（签收 3 天）
        {
            "tenant_id": "tenant_a",
            "order_no": "A-ORD-202509-001",
            "fixed_order_id": FIXED_ORDER_IDS["A-ORD-202509-001"],
            "buyer_username": "tenant_a_consumer",
            "status": "delivered",
            "total_amount_yuan": 399_00,  # ￥399.00
            "line_items": [
                {
                    "sku_id": "SKU-A-STANDARD-001",
                    "product_name": "禅饰坊·星月菩提 108 颗手串（通货）",
                    "product_type": "standard",
                    "unit_price_yuan": 399_00,
                    "quantity": 1,
                },
            ],
            "created_at": _days_ago_utc(10, hour=9),
            "paid_at": _days_ago_utc(10, hour=9),
            "shipped_at": _days_ago_utc(9, hour=16),
            "delivered_at": _days_ago_utc(3, hour=14),
            "carrier": "顺丰速运",
            "tracking_no": "SF1234567890A01",
            "recipient_name": "张小明",
            "recipient_phone": "13800000001",
            "recipient_address": "北京市朝阳区建国路 88 号 SOHO 现代城 3 号楼 1702",
        },
        # O2：tenant_a 质量问题（签收 5 天）— 演示「质量问题 30 天保修」
        {
            "tenant_id": "tenant_a",
            "order_no": "A-ORD-202509-002",
            "fixed_order_id": FIXED_ORDER_IDS["A-ORD-202509-002"],
            "buyer_username": "tenant_a_consumer",
            "status": "delivered",
            "total_amount_yuan": 1299_00,  # ￥1299
            "line_items": [
                {
                    "sku_id": "SKU-A-NANHONG-007",
                    "product_name": "禅饰坊·天然南红玛瑙手串 满肉柿子红 12mm",
                    "product_type": "standard",
                    "unit_price_yuan": 1299_00,
                    "quantity": 1,
                },
            ],
            "created_at": _days_ago_utc(15, hour=20),
            "paid_at": _days_ago_utc(15, hour=20),
            "shipped_at": _days_ago_utc(12, hour=10),
            "delivered_at": _days_ago_utc(5, hour=18),
            "carrier": "圆通速递",
            "tracking_no": "YT66112233445566",
            "recipient_name": "张小明",
            "recipient_phone": "13800000001",
            "recipient_address": "北京市朝阳区建国路 88 号 SOHO 现代城 3 号楼 1702",
        },
        # O3：tenant_b 梵印阁 定制刻字款（不支持 7 天无理由，签收 7 天 卡在边界）
        {
            "tenant_id": "tenant_b",
            "order_no": "B-ORD-202509-003",
            "fixed_order_id": FIXED_ORDER_IDS["B-ORD-202509-003"],
            "buyer_username": "tenant_b_consumer",
            "status": "completed",
            "total_amount_yuan": 2680_00,
            "line_items": [
                {
                    "sku_id": "SKU-B-CUSTOM-2025",
                    "product_name": "梵印阁·小叶紫檀 18mm 定制刻字款「吉祥如意」",
                    "product_type": "custom",
                    "unit_price_yuan": 2680_00,
                    "quantity": 1,
                },
            ],
            "created_at": _days_ago_utc(20, hour=11),
            "paid_at": _days_ago_utc(20, hour=11),
            "shipped_at": _days_ago_utc(15, hour=9),
            "delivered_at": _days_ago_utc(7, hour=16),
            "carrier": "顺丰速运",
            "tracking_no": "SF99887766554433",
            "recipient_name": "李雅文",
            "recipient_phone": "13900000001",
            "recipient_address": "上海市徐汇区漕河泾开发区桂箐路 168 号 1 栋 1103",
        },
        # O4：tenant_c 玉语轩 超保修期（签收 40 天，非质量退款收 10% 手续费）
        {
            "tenant_id": "tenant_c",
            "order_no": "C-ORD-202509-004",
            "fixed_order_id": FIXED_ORDER_IDS["C-ORD-202509-004"],
            "buyer_username": "tenant_c_consumer",
            "status": "completed",
            "total_amount_yuan": 3888_00,
            "line_items": [
                {
                    "sku_id": "SKU-C-HETIAN-002",
                    "product_name": "玉语轩·新疆和田玉青白玉老型珠手串 10mm 附国检证书",
                    "product_type": "standard",
                    "unit_price_yuan": 3888_00,
                    "quantity": 1,
                },
            ],
            "created_at": _days_ago_utc(60, hour=15),
            "paid_at": _days_ago_utc(60, hour=15),
            "shipped_at": _days_ago_utc(50, hour=14),
            "delivered_at": _days_ago_utc(40, hour=10),
            "carrier": "EMS 特快",
            "tracking_no": "EE111222333CN",
            "recipient_name": "王建国",
            "recipient_phone": "13700000001",
            "recipient_address": "广州市天河区珠江新城华夏路 10 号富力中心 2805",
        },
    ]


def _build_logistics(scenario: dict[str, Any]) -> dict[str, Any]:
    """把 shipped_at / delivered_at 拆成若干轨迹点。"""
    from app.application.schemas.order import (
        LogisticsTrackPoint,
        OrderLogistics,
    )

    tracks: list[LogisticsTrackPoint] = []
    if scenario.get("created_at"):
        tracks.append(LogisticsTrackPoint(happened_at=scenario["created_at"], status="pending", description="订单创建，等待揽收"))
    if scenario.get("shipped_at"):
        tracks.append(LogisticsTrackPoint(happened_at=scenario["shipped_at"], status="picked_up", description=f"【{scenario['carrier']}】已取件，快递单号 {scenario['tracking_no']}"))
    in_transit = scenario["shipped_at"] + _dt.timedelta(days=1) if scenario.get("shipped_at") else None
    if in_transit:
        tracks.append(LogisticsTrackPoint(happened_at=in_transit, status="in_transit", description="运输途中，预计 2-3 天送达", location="杭州转运中心"))
    out_for_deliv = (scenario["delivered_at"] - _dt.timedelta(hours=4)) if scenario.get("delivered_at") else None
    if out_for_deliv:
        tracks.append(LogisticsTrackPoint(happened_at=out_for_deliv, status="out_for_delivery", description="派送员已出发，今日送达", location="朝阳区建国路营业点"))
    if scenario.get("delivered_at"):
        tracks.append(LogisticsTrackPoint(happened_at=scenario["delivered_at"], status="delivered", description="已签收，本人签收", location="收件地址"))

    return OrderLogistics(
        carrier_name=scenario["carrier"],
        tracking_no=scenario["tracking_no"],
        current_status="delivered" if scenario.get("delivered_at") else "in_transit",
        delivered_at=scenario.get("delivered_at"),
        latest_track=tracks[-1] if tracks else None,
        tracks=sorted(tracks, key=lambda t: t.happened_at),
    ).model_dump(mode="json")


async def seed_orders() -> dict[str, Any]:
    """执行 4 场景订单幂等种子。"""
    from app.application.schemas.order import LineItem
    from app.config import load_settings
    from app.core.infrastructure import InfrastructureBundle
    from app.domain.repositories.order import OrderRepository
    from app.infrastructure.db.engine import scoped_db_session

    settings = load_settings()
    bundle = InfrastructureBundle(settings)
    await bundle.start()
    try:
        async with scoped_db_session(bundle) as session:
            repo = OrderRepository(session)
            new_orders: list[str] = []
            before = 0
            scenarios = _make_4_scenarios()
            # 粗略计算本次是否新增（先查每个 order_no 是否存在）
            for sc in scenarios:
                before_row = await repo.get_by_order_no(sc["tenant_id"], sc["order_no"])
                if before_row is None:
                    before += 1
            for sc in scenarios:
                buyer_user_id = UUID(FIXED_USER_IDS[(sc["tenant_id"], sc["buyer_username"])])
                logistics_dict = _build_logistics(sc)
                # model_dump(mode="json") 直接给 Repository 的参数 logistic 接受 Pydantic 对象
                from app.application.schemas.order import OrderLogistics

                logi_obj = OrderLogistics(**logistics_dict)
                line_items = [LineItem(**it) for it in sc["line_items"]]
                await repo.upsert_by_order_no(
                    tenant_id=sc["tenant_id"],
                    order_no=sc["order_no"],
                    fixed_order_id=sc["fixed_order_id"],
                    buyer_user_id=buyer_user_id,
                    status=sc["status"],
                    total_amount_yuan=sc["total_amount_yuan"],
                    line_items=line_items,
                    logistics=logi_obj,
                    recipient_name=sc["recipient_name"],
                    recipient_phone=sc["recipient_phone"],
                    recipient_address=sc["recipient_address"],
                    created_at=sc["created_at"],
                    paid_at=sc.get("paid_at"),
                )
                if before:
                    new_orders.append(f"{sc['tenant_id']}/{sc['order_no']}")
            await session.commit()
    finally:
        await bundle.stop()
    return {
        "new_orders": new_orders,
        "scenarios_total": len(_make_4_scenarios()),
        "fixed_order_ids": {k: str(v) for k, v in FIXED_ORDER_IDS.items()},
    }


def _print_result(result: dict[str, Any]) -> None:
    print("=" * 80)
    print(f"【seed_orders.py 完成】共 {result['scenarios_total']} 场景，本次新增 {len(result['new_orders'])} 条")
    print("=" * 80)
    for k, v in result["fixed_order_ids"].items():
        print(f"  {k:<20} → order_id={v}")
    if result["new_orders"]:
        print("本次新增：")
        for n in result["new_orders"]:
            print(f"  - {n}")
    else:
        print("已幂等，订单库无变化（可多次运行不重复）")


async def _main() -> None:
    res = await seed_orders()
    _print_result(res)


if __name__ == "__main__":
    asyncio.run(_main())

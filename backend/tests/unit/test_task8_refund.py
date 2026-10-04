"""T8 RefundQualificationService 单元测试（纯函数资格判定，不接 DB/LLM）。

覆盖所有 7 个 reason_code 分支 + 边界：
  TR8-1 禅饰坊 tenant_a：标准款 3 天签收 → 7 天无理由（免手续费）
  TR8-2 禅饰坊 定制款 2 天前签收（非质量）→ custom_product_excluded
  TR8-3 禅饰坊 标准款 10 天前签收（超 7 天）→ window_expired
  TR8-4 梵印阁 tenant_b 任何非质量 → policy_not_allowed
  TR8-5 玉语轩 tenant_c：10% 手续费 分档金额计算（实付 10000 分 → 到手 9000 分）
  TR8-6 质量/维修：30 天内保修窗 → eligible_quality；超 30 天 → 成本价维修
  TR8-7 policy_override（真表数据注入）+ 缺订单 → human_required_missing_info
"""

from __future__ import annotations

import datetime as _dt
from uuid import uuid4

from app.application.services.refund_qualification import (
    PolicyInput,
    RefundQualificationService,
)
from app.domain.constants.policies import TENANT_POLICIES, TenantPolicy

TENANT_A = "tenant_a"
TENANT_B = "tenant_b"
TENANT_C = "tenant_c"

_POLICY_A = TENANT_POLICIES[TENANT_A]
_POLICY_B = TENANT_POLICIES[TENANT_B]
_POLICY_C = TENANT_POLICIES[TENANT_C]


def _delivered_days_ago(days: int, *, hours: int = 0) -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    t = now - _dt.timedelta(days=days, hours=hours)
    return t.isoformat()


def _order(
    *,
    tenant_id: str,
    delivered_days_ago: int,
    product_type: str = "standard",
    total_cents: int = 10000,  # 100.00 元
    is_customized: bool = False,
) -> dict:
    return {
        "order_id": str(uuid4()),
        "order_no": f"{tenant_id[:1].upper()}-ORD-202509-001",
        "tenant_id": tenant_id,
        "owner_id": f"{tenant_id}-owner-1",
        "status": "delivered",
        "product_type": product_type,
        "is_customized": is_customized,
        "delivered_at": _delivered_days_ago(delivered_days_ago),
        "total_amount_cents": total_cents,
    }


SVC = RefundQualificationService()


# ========================================================================
# TR8-1 tenant_a 标准款 3 天前签收 → 7 天无理由（免手续费）
# ========================================================================


def test_tr81_tenant_a_standard_3days_no_reason_eligible() -> None:
    inp = PolicyInput(
        tenant_id=TENANT_A,
        order_detail=_order(tenant_id=TENANT_A, delivered_days_ago=3),
        intent="refund",
        policy_override=_POLICY_A,
    )
    d = SVC.decide(inp)
    assert d.can_refund is True
    assert d.can_exchange is True
    assert d.can_repair is False
    assert d.requires_quality_evidence is False
    assert d.restocking_fee_pct == 0
    assert d.reason_code == "eligible_no_reason"
    assert d.refund_amount_cents == 10000
    assert "免退货手续费" in d.reason_human_readable
    assert d.debug is not None
    assert d.debug["days_since_delivery"] == 3


# ========================================================================
# TR8-2 tenant_a 定制款 2 天前签收（非质量）→ custom_product_excluded
# ========================================================================


def test_tr82_tenant_a_custom_not_allowed_for_no_reason() -> None:
    inp = PolicyInput(
        tenant_id=TENANT_A,
        order_detail=_order(
            tenant_id=TENANT_A,
            delivered_days_ago=2,
            product_type="custom_engraved",
            is_customized=True,
        ),
        intent="refund",
        policy_override=_POLICY_A,
    )
    d = SVC.decide(inp)
    assert d.can_refund is False
    assert d.can_exchange is False
    assert d.can_repair is False
    assert d.reason_code == "custom_product_excluded"
    assert "定制" in d.reason_human_readable or "刻字" in d.reason_human_readable
    # 定制款 + 质量 intent (quality) ：仍可退（非定制条款拦截；质量分支优先）
    inp2 = PolicyInput(
        tenant_id=TENANT_A,
        order_detail=_order(
            tenant_id=TENANT_A, delivered_days_ago=2, product_type="custom_engraved"
        ),
        intent="quality",
        policy_override=_POLICY_A,
    )
    d2 = SVC.decide(inp2)
    assert d2.can_refund is True
    assert d2.requires_quality_evidence is True
    assert d2.reason_code == "eligible_quality"


# ========================================================================
# TR8-3 超 7 天窗口 → window_expired；但质量 intent 仍可以申请
# ========================================================================


def test_tr83_window_expired_10days_but_quality_still_eligible() -> None:
    # 非质量：超窗
    inp = PolicyInput(
        tenant_id=TENANT_A,
        order_detail=_order(tenant_id=TENANT_A, delivered_days_ago=10),
        intent="refund",
        policy_override=_POLICY_A,
    )
    d = SVC.decide(inp)
    assert d.reason_code == "window_expired"
    assert d.can_refund is False
    assert "超过7天" in d.reason_human_readable or "超过 7 天" in d.reason_human_readable

    # 质量 intent：超 7 天仍在 30 天保修 → eligible_quality（can_refund=true，需举证）
    inp2_quality = PolicyInput(
        tenant_id=TENANT_A,
        order_detail=_order(tenant_id=TENANT_A, delivered_days_ago=10),
        intent="quality",
        policy_override=_POLICY_A,
    )
    d2 = SVC.decide(inp2_quality)
    assert d2.can_refund is True
    assert d2.reason_code == "eligible_quality"
    assert d2.requires_quality_evidence is True

    # 维修 intent：10 天（保修内，repair → 被 is_quality 吸收为 eligible_quality，
    # 与 T7 原实现完全一致；can_refund=is_quality=True 但 repair 是维修 intent，前端只展示 can_repair）
    inp2_repair = PolicyInput(
        tenant_id=TENANT_A,
        order_detail=_order(tenant_id=TENANT_A, delivered_days_ago=10),
        intent="repair",
        policy_override=_POLICY_A,
    )
    d3 = SVC.decide(inp2_repair)
    assert d3.can_repair is True
    assert d3.reason_code == "eligible_quality"  # 与 T7 对齐：repair 自身命中 is_quality=True
    assert d3.requires_quality_evidence is True
    assert d3.can_refund is True  # is_quality=True 分支默认 can_refund=is_quality=True（同 T7）
    assert d3.can_exchange is True


# ========================================================================
# TR8-4 梵印阁 quality_only：非质量一律 policy_not_allowed
# ========================================================================


def test_tr84_tenant_b_quality_only_blocks_no_reason() -> None:
    # 标准款 1 天前签收（非质量）→ 仍不允许
    inp = PolicyInput(
        tenant_id=TENANT_B,
        order_detail=_order(
            tenant_id=TENANT_B,
            delivered_days_ago=1,
            product_type="standard",
            total_cents=388800,
        ),
        intent="不喜欢想退",
        policy_override=_POLICY_B,
    )
    d = SVC.decide(inp)
    assert d.reason_code == "policy_not_allowed"
    assert d.can_refund is False
    assert d.can_exchange is False
    # requires_quality_evidence 为 True → 指引用户拍工艺照
    assert d.requires_quality_evidence is True
    assert "梵印阁" in d.reason_human_readable

    # 梵印阁 质量 intent（30天内）→ 仍走质量分支，不被 policy_not_allowed 截住
    inp2 = PolicyInput(
        tenant_id=TENANT_B,
        order_detail=_order(tenant_id=TENANT_B, delivered_days_ago=3, total_cents=388800),
        intent="quality",
        policy_override=_POLICY_B,
    )
    d2 = SVC.decide(inp2)
    assert d2.reason_code == "eligible_quality"
    assert d2.can_refund is True
    assert d2.refund_amount_cents == 388800


# ========================================================================
# TR8-5 玉语轩 tenant_c：10% 手续费分档计算（金额 = total_cents * 0.9）
# ========================================================================


def test_tr85_tenant_c_10pct_fee_and_amount_calculation() -> None:
    inp = PolicyInput(
        tenant_id=TENANT_C,
        order_detail=_order(
            tenant_id=TENANT_C,
            delivered_days_ago=3,
            product_type="standard",
            total_cents=123456,  # 1234.56 元 → 10% 手续费：应退 1111.104 = 111110 分
        ),
        intent="exchange",  # exchange 也应该命中 10% 手续费
        policy_override=_POLICY_C,
    )
    d = SVC.decide(inp)
    assert d.reason_code == "eligible_no_reason"
    assert d.restocking_fee_pct == 10
    # 向下取整 (int(123456 * 10 / 100) = 12345) → 123456 - 12345 = 111111
    expected = 123456 - int(123456 * 10 / 100)
    assert d.refund_amount_cents == expected
    assert "收取 10% 手续费" in d.reason_human_readable
    # 0 元订单 → refund_amount_cents=None（不除零错误）
    inp_free = PolicyInput(
        tenant_id=TENANT_C,
        order_detail=_order(
            tenant_id=TENANT_C, delivered_days_ago=2, total_cents=0
        ),
        intent="refund",
        policy_override=_POLICY_C,
    )
    d_free = SVC.decide(inp_free)
    assert d_free.refund_amount_cents is None
    assert d_free.can_refund is True


# ========================================================================
# TR8-6 维修分支时间边界（30 天保修 vs 超 30 天）
# ========================================================================


def test_tr86_repair_warranty_boundary() -> None:
    # 正好 30 天：repair 命中 is_quality=True（T7 原判定）→ eligible_quality，
    # can_refund=False（is_quality=True 时 can_refund=is_quality=True？实际上 T7 repair intent：
    # is_quality = intent == "repair" or any("quality" in intent) → True → can_refund=is_quality=True。
    # 这里保持与 T7 行为：is_quality=True 即 repair intent 自身也视为保修范围内。
    inp_30 = PolicyInput(
        tenant_id=TENANT_A,
        order_detail=_order(tenant_id=TENANT_A, delivered_days_ago=30),
        intent="repair",
        policy_override=_POLICY_A,
    )
    d_30 = SVC.decide(inp_30)
    assert d_30.can_repair is True
    assert d_30.reason_code == "eligible_quality"
    # 31 天：超保修 → 成本价维修（仍是 eligible_warranty_repair 但 can_refund/can_exchange=False）
    inp_31 = PolicyInput(
        tenant_id=TENANT_A,
        order_detail=_order(tenant_id=TENANT_A, delivered_days_ago=31),
        intent="repair",
        policy_override=_POLICY_A,
    )
    d_31 = SVC.decide(inp_31)
    assert d_31.can_repair is True
    assert d_31.can_refund is False
    assert "已超30天" in d_31.reason_human_readable or "已超 30 天" in d_31.reason_human_readable
    assert "成本价维修" in d_31.reason_human_readable


# ========================================================================
# TR8-7 policy_override 数据注入 + 缺订单 / 缺政策 兜底
# ========================================================================


def test_tr87_policy_override_and_missing_data() -> None:
    # 构造一个虚构租户 + 政策（policy_override 从真表注入的场景）
    fake_policy = TenantPolicy(
        tenant_id="tenant_fake",
        brand_name="假品牌",
        slogan="演示用 slogan",
        return_days=3,
        return_policy_type="hybrid",
        custom_product_allowed=True,
        restocking_fee_pct_non_quality=5,
        warranty_days_quality=60,
        highlights=["假高亮"],
        full_text="假政策全文",
    )
    inp = PolicyInput(
        tenant_id="tenant_fake",  # TENANT_POLICIES 里不存在
        order_detail={
            **_order(tenant_id="tenant_fake", delivered_days_ago=1, total_cents=20000),
            "tenant_id": "tenant_fake",
        },
        intent="refund",
        policy_override=fake_policy,  # ← 关键：真表注入
    )
    d = SVC.decide(inp)
    assert d.reason_code == "eligible_no_reason"
    assert d.restocking_fee_pct == 5
    # 20000 * 5% = 1000 → 退款 19000
    assert d.refund_amount_cents == 19000
    # 超 3 天（fake 只有 return_days=3）
    inp_expired = PolicyInput(
        tenant_id="tenant_fake",
        order_detail=_order(tenant_id="tenant_fake", delivered_days_ago=10),
        intent="refund",
        policy_override=fake_policy,
    )
    assert SVC.decide(inp_expired).reason_code == "window_expired"

    # 缺订单 → human_required_missing_info
    inp_none_order = PolicyInput(
        tenant_id=TENANT_A, order_detail=None, intent="refund"
    )
    assert SVC.decide(inp_none_order).reason_code == "human_required_missing_info"
    # 缺政策（没有 policy_override 且 tenant_id 不在常量里）
    inp_nopol = PolicyInput(
        tenant_id="tenant_unknown",
        order_detail=_order(tenant_id="tenant_unknown", delivered_days_ago=1),
        intent="refund",
    )
    d_nopol = SVC.decide(inp_nopol)
    assert d_nopol.reason_code == "human_required_missing_info"
    assert d_nopol.debug is not None and d_nopol.debug["missing"] == "tenant_policy"
    # 但一注入 override 就生效
    inp_fixed = PolicyInput(
        tenant_id="tenant_unknown",
        order_detail=_order(tenant_id="tenant_unknown", delivered_days_ago=1),
        intent="refund",
        policy_override=fake_policy,
    )
    assert SVC.decide(inp_fixed).reason_code == "eligible_no_reason"


# ========================================================================
# TR8-8 非法日期/空 delivered_at → 不崩溃且不超窗（视为 "未知"）
# ========================================================================


def test_tr88_invalid_delivered_at_does_not_crash() -> None:
    detail_invalid = _order(tenant_id=TENANT_A, delivered_days_ago=1)
    detail_invalid["delivered_at"] = "不是一个合法的日期"
    inp1 = PolicyInput(
        tenant_id=TENANT_A, order_detail=detail_invalid, intent="refund", policy_override=_POLICY_A
    )
    d1 = SVC.decide(inp1)
    # 非法 delivered_at 不会超窗（days_since_delivery < 0 被 normalize 视为未知）
    assert d1.reason_code in {"eligible_no_reason", "human_required_missing_info"}

    detail_empty = _order(tenant_id=TENANT_A, delivered_days_ago=1)
    detail_empty["delivered_at"] = None
    inp2 = PolicyInput(
        tenant_id=TENANT_A, order_detail=detail_empty, intent="refund", policy_override=_POLICY_A
    )
    d2 = SVC.decide(inp2)
    assert d2.reason_code == "eligible_no_reason"
    assert d2.can_refund is True
    # debug 里 days_since_delivery 应为 None（缺失）
    assert d2.debug is not None
    assert d2.debug.get("days_since_delivery") is None

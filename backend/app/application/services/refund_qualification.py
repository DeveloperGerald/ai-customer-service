"""T8 退款/换货/维修 资格判定服务（从政策数值 + 订单属性结构化计算 PolicyDecision）。

定位：
    - 从 T7 nodes._decide_policy 独立为 domain service，脱离 LangGraph 也可单测复用。
    - 未来接入真实 tenant_policy_configs 表时，只需替换 _policy_for_tenant 的数据源。
    - 所有函数**纯计算 + 无 IO**（不访问 DB、不访问 LLM、不访问 Redis），
      便于离线环境、单元测试、面试讲解。

计算输入契约（PolicyInput）：
    - tenant_id / policy_config（来源可替换：常量 TENANT_POLICIES ↔ 真表）
    - order_detail（delivered_at/签收天数、total_cents、product_type/is_customized）
    - intent：仅用于区分「质量/维修/非质量」和「can_refund/can_exchange」映射

reason_code 枚举值（面试讲解：7+1 种）：
    eligible_quality                 质量范围内（可退换，需举证）
    eligible_warranty_repair         保修范围内（可免费/成本价维修）
    policy_not_allowed               梵印阁 quality_only 非质量不允许
    custom_product_excluded          定制款非质量不允许
    window_expired                   超 return_days 无理由退换窗口
    eligible_no_reason               命中无理由退换（免或扣手续费）
    human_required_missing_info      缺政策/缺订单（T7 router 转人工）
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any

from app.application.schemas.agent import PolicyDecision
from app.domain.constants.policies import TenantPolicy

# =========================================================================
# 1. 输入 Schema（dataclass slots 友好；纯数据）
# =========================================================================


@dataclass(frozen=True)
class PolicyInput:
    """资格判定输入：从调用方收集的所有判断条件。

    所有字段可选，缺失走合理兜底：
      - delivered_at=None → 「不知道是否超窗」→ 不触发 window_expired
      - total_cents=0 → refund_amount_cents=None（金额未知）
      - product_type 为空 → 按 standard 处理
    """

    tenant_id: str
    order_detail: dict[str, Any] | None
    intent: str

    # 可选：调用方注入租户政策（必须从 DB PolicyConfigRepository 查询）；
    # None 时不使用任何硬编码常量，返回「缺政策转人工」（满足全链路真实数据要求）。
    policy_override: TenantPolicy | None = None
    # 可选：注入当前时间（便于单测固定 delivered_days_ago 阈值）
    now: _dt.datetime | None = None


# =========================================================================
# 2. 辅助工具（订单属性归一化 / 日期 / 金额）
# =========================================================================


def _policy_for_tenant(inp: PolicyInput) -> TenantPolicy | None:
    """只从 inp.policy_override 取真实 DB 政策；不再使用任何硬编码 TENANT_POLICIES 常量。

    若调用方未注入 policy_override（应视为配置错误），返回 None，
    下游将走「缺政策 → 转人工」分支（human_required_missing_info），
    绝不再从内存常量伪造政策数据。
    """
    return inp.policy_override


def _normalize_order(order_detail: dict[str, Any]) -> tuple[int, int, str, bool, dict[str, Any]]:
    """归一化订单字段 → (total_cents, days_since_delivery_or_-1, product_type, is_custom, debug_extra)。

    当 delivered_at 缺失或非法 → days_since_delivery=-1，下游视作「未知，不作超窗判定」。
    """
    total_cents: int = int(order_detail.get("total_amount_cents") or 0)
    product_type: str = str(order_detail.get("product_type") or "standard")
    is_custom = "custom" in product_type.lower() or bool(order_detail.get("is_customized"))
    delivered_at_str = order_detail.get("delivered_at")
    now = _dt.datetime.now(_dt.timezone.utc)
    days_since_delivery: int = -1
    if delivered_at_str:
        try:
            delivered_at = _dt.datetime.fromisoformat(str(delivered_at_str))
            days_since_delivery = max(0, (now - delivered_at).days)
        except Exception:  # pragma: no cover - defensive
            days_since_delivery = -1
    debug_extra: dict[str, Any] = {
        "is_custom": is_custom,
        "days_since_delivery": days_since_delivery if days_since_delivery >= 0 else None,
    }
    return total_cents, days_since_delivery, product_type, is_custom, debug_extra


# =========================================================================
# 3. 主服务类（RefundQualificationService，可注入 policy_resolver / now）
# =========================================================================


class RefundQualificationService:
    """售后资格判定服务（纯函数：输入 → PolicyDecision，无副作用）。"""

    # 可在子类覆盖（单测 spy）；生产可把 _policy_for_tenant 改为真实 SQL 查询
    @staticmethod
    def policy_for(inp: PolicyInput) -> TenantPolicy | None:
        return _policy_for_tenant(inp)

    @staticmethod
    def now_for(inp: PolicyInput) -> _dt.datetime:
        # 允许测试固定"当前时间"（避免 delivered_days_ago 随时间漂移失败）
        return inp.now or _dt.datetime.now(_dt.timezone.utc)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def decide(self, inp: PolicyInput) -> PolicyDecision:
        """资格判定入口：return PolicyDecision。"""
        policy: TenantPolicy | None = self.policy_for(inp)
        if policy is None:
            return PolicyDecision(
                can_refund=False,
                can_exchange=False,
                can_repair=False,
                requires_quality_evidence=False,
                restocking_fee_pct=0,
                refund_amount_cents=None,
                reason_code="human_required_missing_info",
                reason_human_readable="未找到该租户政策，已转人工协助。",
                debug={"missing": "tenant_policy"},
            )

        if inp.order_detail is None:
            policy_type = policy.return_policy_type
            return_days = policy.return_days
            warranty_days_quality = policy.warranty_days_quality
            fee_pct_non_quality = policy.restocking_fee_pct_non_quality
            custom_allowed = policy.custom_product_allowed

            if policy_type == "no_reason":
                days_str = f"{return_days} 天" if return_days is not None else "合理期限"
                fee_pct_str = (
                    f"，非质量问题退货将收取 {fee_pct_non_quality}% 手续费。"
                    if fee_pct_non_quality > 0
                    else "，免退货手续费。"
                )
                reason = f"{policy.brand_name}支持 {days_str} 无理由退换货{fee_pct_str}"
                if not custom_allowed:
                    reason += "（定制款 / 刻字款非质量问题除外，如有质量问题请举证。）"
                else:
                    reason += "定制款若未刻字使用，也支持同口径无理由退换。"
                reason_code = "policy_info_no_reason"
            elif policy_type == "quality_only":
                reason = (
                    f"{policy.brand_name}仅支持质量问题售后：签收 {warranty_days_quality} 天内"
                    "，非质量问题不适用 7 天无理由。请提供清晰照片以便审核。"
                )
                reason_code = "policy_info_quality_only"
            else:
                reason = (
                    f"{policy.brand_name}售后规则：非质量问题 {return_days} 天无理由"
                    + (
                        f"，手续费 {fee_pct_non_quality}%。"
                        if fee_pct_non_quality > 0
                        else "，免手续费。"
                    )
                    + f"质量问题保修 {warranty_days_quality} 天。"
                )
                reason_code = "policy_info_mixed"

            return PolicyDecision(
                can_refund=False,
                can_exchange=False,
                can_repair=False,
                requires_quality_evidence=False,
                restocking_fee_pct=fee_pct_non_quality,
                refund_amount_cents=None,
                reason_code=reason_code,
                reason_human_readable=reason,
                debug={
                    "need_order_no": False,
                    "policy_type": policy_type,
                    "return_days": return_days,
                    "warranty_days_quality": warranty_days_quality,
                    "fee_pct_non_quality": fee_pct_non_quality,
                    "custom_product_allowed": custom_allowed,
                    "policy_consultation": True,
                },
            )

        total_cents, days_since_delivery, _product_type, is_custom, order_debug = _normalize_order(inp.order_detail)

        debug: dict[str, Any] = {
            "policy_type": policy.return_policy_type,
            "return_days": policy.return_days,
            "warranty_days_quality": policy.warranty_days_quality,
            "fee_pct_non_quality": policy.restocking_fee_pct_non_quality,
            "custom_product_allowed": policy.custom_product_allowed,
        }
        debug.update(order_debug)

        # 维修 / 质量：命中 is_quality
        # （与 T7 _decide_policy 对齐：只看 intent==repair 或 intent 含 "quality" 字样，
        #  不在这里做自然语言关键词，关键词归类由 IntentClassifierProtocol 负责）
        is_quality = inp.intent == "repair" or ("quality" in inp.intent)

        # --- 分支 1：质量 / 维修 ---
        if is_quality or inp.intent == "repair":
            in_window = (
                days_since_delivery < 0
                or policy.warranty_days_quality is None
                or days_since_delivery <= policy.warranty_days_quality
            )
            if in_window:
                return PolicyDecision(
                    can_refund=is_quality,
                    can_exchange=is_quality,
                    can_repair=True,
                    requires_quality_evidence=True,
                    restocking_fee_pct=0,
                    refund_amount_cents=total_cents if is_quality and total_cents else None,
                    reason_code="eligible_quality" if is_quality else "eligible_warranty_repair",
                    reason_human_readable=(
                        f"质量/保修范围内：{policy.warranty_days_quality}天内"
                        + (
                            "，可免费退换（需举证照片）。"
                            if is_quality
                            else "，提供免费维修服务。"
                        )
                    ),
                    debug=debug,
                )
            # 超保修期：仍可提供成本价维修
            return PolicyDecision(
                can_refund=False,
                can_exchange=False,
                can_repair=True,
                requires_quality_evidence=True,
                restocking_fee_pct=0,
                refund_amount_cents=None,
                reason_code="eligible_warranty_repair",
                reason_human_readable=(
                    f"已超{policy.warranty_days_quality}天免费退换窗口，"
                    "仍可提供成本价维修服务（用户承担往返运费）。"
                ),
                debug=debug,
            )

        # --- 分支 2：梵印阁 quality_only → 非质量不允许 ---
        if policy.return_policy_type == "quality_only":
            return PolicyDecision(
                can_refund=False,
                can_exchange=False,
                can_repair=False,
                requires_quality_evidence=True,
                restocking_fee_pct=0,
                refund_amount_cents=None,
                reason_code="policy_not_allowed",
                reason_human_readable=(
                    f"{policy.brand_name}主打定制款，非质量问题不适用 7 天无理由。"
                    "如您确认商品本身存在工艺缺陷，请提供清晰照片以便审核。"
                ),
                debug=debug,
            )

        # --- 分支 3：定制款且租户不允许定制款无理由退换 ---
        if is_custom and not policy.custom_product_allowed:
            return PolicyDecision(
                can_refund=False,
                can_exchange=False,
                can_repair=False,
                requires_quality_evidence=False,
                restocking_fee_pct=0,
                refund_amount_cents=None,
                reason_code="custom_product_excluded",
                reason_human_readable="定制款/刻字款非质量问题不支持无理由退换，如有质量问题请举证。",
                debug=debug,
            )

        # --- 分支 4：超 return_days 无理由窗口 ---
        if (
            policy.return_days is not None
            and days_since_delivery >= 0
            and days_since_delivery > policy.return_days
        ):
            return PolicyDecision(
                can_refund=False,
                can_exchange=False,
                can_repair=False,
                requires_quality_evidence=False,
                restocking_fee_pct=0,
                refund_amount_cents=None,
                reason_code="window_expired",
                reason_human_readable=(
                    f"已超过{policy.return_days}天无理由退换窗口。"
                    "若为质量问题仍可申请审核，请提供照片。"
                ),
                debug=debug,
            )

        # --- 分支 5：命中无理由退换（分档手续费计算）---
        fee_pct = policy.restocking_fee_pct_non_quality
        refund_cents = (
            max(0, total_cents - int(total_cents * fee_pct / 100))
            if total_cents > 0
            else None
        )
        action_intents = {"refund", "exchange", "order_status"}
        return PolicyDecision(
            can_refund=inp.intent in action_intents,
            can_exchange=inp.intent in action_intents,
            can_repair=False,
            requires_quality_evidence=False,
            restocking_fee_pct=fee_pct,
            refund_amount_cents=refund_cents,
            reason_code="eligible_no_reason",
            reason_human_readable=(
                f"符合{policy.return_days}天无理由退换条件"
                + (
                    f"，非质量退货将收取 {fee_pct}% 手续费。"
                    if fee_pct > 0
                    else "，免退货手续费。"
                )
            ),
            debug=debug,
        )

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TenantPolicy:
    """演示三租户的差异化售后政策。

    该常量同时服务于：
    1. seed_tenants.py 在 tenant.description 中写入差异化说明。
    2. T5 RAG 知识库导入时直接作为 txt 分段写入 vector store。
    3. T7 LangGraph 节点判断"是否符合退款条件"时的分支逻辑。
    """

    tenant_id: str
    brand_name: str
    slogan: str
    return_days: int | None
    return_policy_type: Literal["no_reason", "quality_only", "hybrid"]
    custom_product_allowed: bool
    restocking_fee_pct_non_quality: int
    warranty_days_quality: int
    highlights: list[str]
    full_text: str


_TENANT_A_FULL = """
【禅饰坊（tenant_a）售后政策 v1.0】

一、通用规则
1. 自用户签收之日起，支持 7 天无理由退换货。
2. 商品需保持原包装完好、未佩戴、不影响二次销售。
3. 定制款商品（含刻字、特殊尺寸、特殊配色）不适用 7 天无理由，仅在质量问题时可退换。

二、质量问题
1. 质量问题包含：串珠裂纹、配件脱焊、金属氧化脱色（非佩戴磨损）、编绳自然脱线。
2. 自签收之日起 30 天内出现上述质量问题，支持免费退换，来回运费由本店承担。
3. 超过 30 天的质量问题，提供免费维修服务，用户承担往返运费。

三、退款到账
1. 原路退回：支付宝/微信 1-3 个工作日，银行卡 3-7 个工作日。
2. 定制款非质量问题不支持退款。

四、例外情形
- 人为损坏（摔、砸、化学品腐蚀）不支持售后。
- 有明显佩戴痕迹（珠面划痕、编绳起毛）影响二次销售的，无理由退货不予受理。
"""


_TENANT_B_FULL = """
【梵印阁（tenant_b）售后政策 v1.0】

一、关于 7 天无理由的特别说明
本店主打高端定制款，所有商品默认不适用"7 天无理由退换货"，请您谨慎下单。
以下情形不在售后受理范围：
1. 纯定制款（含个性化雕刻、私人订制图案、客户自带石料镶嵌）。
2. 限量款 / 联名款 / 预售款（商品详情页会特别标注）。

二、仅质量问题可售后
1. 自签收之日起 30 天内，因工艺或材质导致的质量问题，支持退款或免费更换同款一次。
2. 质量问题必须提供清晰的问题照片 + 订单截图。客服在 24 小时内审核。
3. 经鉴定为"自然包浆 / 正常佩戴痕迹 / 木质天然纹路"的，不认定为质量问题。

三、维修服务
1. 所有商品终身提供成本价维修（仅收材料费，不含工费）。
2. 往返运费由用户承担。

四、退款时效
- 审核通过后 3-5 个工作日原路退回。
"""


_TENANT_C_FULL = """
【玉语轩（tenant_c）售后政策 v1.0】

一、质量问题
1. 自签收之日起 15 天内，出现工艺或材质缺陷，包退包换，运费由本店承担。
2. 可选择更换同款或折算为本店余额券（余额券额外赠送 5% 代金）。

二、非质量问题（主观不喜欢/尺寸不合适/与想象不符）
1. 自签收之日起 7 天内，可申请退货或换货。
2. 此类情形将收取商品实付金额 10% 作为退货手续费。
3. 商品需无佩戴、无划痕、包装配件齐全。
4. 换货同一订单仅限一次，超出需重新下单。

三、定制款说明
- 定制款仅质量问题可售后，非质量问题不支持 7 天无理由。

四、到账时效
1. 质量退款 1-2 工作日。
2. 非质量退款 3-5 工作日（需扣除 10% 手续费后原路返回）。
"""


TENANT_POLICIES: dict[str, TenantPolicy] = {
    "tenant_a": TenantPolicy(
        tenant_id="tenant_a",
        brand_name="禅饰坊",
        slogan="日常百搭手串，7 天无忧购",
        return_days=7,
        return_policy_type="no_reason",
        custom_product_allowed=False,
        restocking_fee_pct_non_quality=0,
        warranty_days_quality=30,
        highlights=[
            "普通款 7 天无理由退货",
            "定制款仅质量问题可退",
            "质量问题 30 天包退换",
            "非质量退货不收手续费",
        ],
        full_text=_TENANT_A_FULL.strip(),
    ),
    "tenant_b": TenantPolicy(
        tenant_id="tenant_b",
        brand_name="梵印阁",
        slogan="高端定制，品质承诺",
        return_days=None,
        return_policy_type="quality_only",
        custom_product_allowed=False,
        restocking_fee_pct_non_quality=0,
        warranty_days_quality=30,
        highlights=[
            "所有商品不适用 7 天无理由",
            "仅质量问题 30 天内可退可换",
            "终身成本价维修服务",
            "定制/限量/联名款均不支持无理由",
        ],
        full_text=_TENANT_B_FULL.strip(),
    ),
    "tenant_c": TenantPolicy(
        tenant_id="tenant_c",
        brand_name="玉语轩",
        slogan="品质文玩，灵活售后",
        return_days=7,
        return_policy_type="hybrid",
        custom_product_allowed=False,
        restocking_fee_pct_non_quality=10,
        warranty_days_quality=15,
        highlights=[
            "质量问题 15 天包退换，免运费",
            "非质量 7 天可退，收 10% 手续费",
            "余额券退款额外赠 5% 代金",
            "定制款仅质量问题可退",
        ],
        full_text=_TENANT_C_FULL.strip(),
    ),
}

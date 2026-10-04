"""内置工具集合（LangChain 1.x 模块级 @tool，无状态）：

读：
  order_query       按 order_id / order_no 查单详情
  product_list      商品列表（名称模糊 + 分类/上架状态过滤）
  product_query     按 product_id / sku_code 查商品详情
  current_time      服务器当前时间（UTC+8）
写（内置 interrupt 人在回路确认）：
  refund_request    退款：确认后订单状态 paid/shipped/delivered → refunded
  exchange_request  换货：受理工单 EX-，不变更订单状态
  repair_request    维修：受理工单 RP-，不变更订单状态
  cancel_order      取消：pending_payment/paid → cancelled
虚拟：
  policy_check      纯函数政策判定，不写库

身份/session/repo 全部取自 `runtime.context`（AgentRunContext），
工具签名中无 tenant_id/actor 参数。审计与幂等由 ToolGovernanceMiddleware 承接。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from app.application.agent.context import AgentRunContext
from app.application.schemas.product import ProductQueryFilter
from app.core.errors import ErrorCode, ResourceNotFoundError, ToolExecutionError
from app.domain.models.order import OrderORM
from app.domain.repositories.order import OrderRepository
from app.domain.repositories.product import ProductRepository

# HITL 暂停态过期时间（秒），与 Redis checkpointer TTL 对齐
_HITL_TTL_SECONDS = 600

# ============================================================================
# 一、Pydantic Args 类（@tool args_schema，逐字保留）
# ============================================================================


class OrderQueryArgs(BaseModel):
    """order_query 参数（二选一；同时传 order_id 优先）。"""

    order_id: str | None = Field(
        default=None,
        description="订单 UUID（与 order_no 二选一，优先）",
    )
    order_no: str | None = Field(
        default=None,
        description="用户展示用订单号，例如 A-ORD-202509-001",
    )


class ExchangeRequestArgs(BaseModel):
    """exchange_request 参数（写操作）。"""

    order_id: str = Field(description="要换货的订单 UUID")
    reason: Literal["quality", "size", "wrong_good", "other"] = Field(
        description="换货原因",
    )
    remark: str | None = Field(default=None, description="用户说明，≤500 字")


class RepairRequestArgs(BaseModel):
    """repair_request 参数（写操作）。"""

    order_id: str = Field(description="订单 UUID")
    issue_desc: str = Field(description="故障描述")


class RefundRequestArgs(BaseModel):
    """refund_request 参数（写操作，内置 interrupt 人在回路确认）。"""

    order_id: str = Field(description="订单 UUID（必须为已通过 OrderQueryTool 校验归属的订单）")
    reason: Literal["7_day_return", "quality", "wrong_good", "other"] = Field(
        description="退款原因",
    )
    remark: str | None = Field(default=None, description="用户备注，≤500 字")


class CancelOrderArgs(BaseModel):
    """cancel_order 参数（写操作，取消订单）。"""

    order_id: str = Field(description="要取消的订单 UUID")
    reason: Literal["no_longer_needed", "wrong_order", "price_change", "other"] = Field(
        description="取消原因",
    )
    remark: str | None = Field(default=None, description="用户备注，≤500 字")


class ProductListArgs(BaseModel):
    """product_list 参数（全部可选，至少支持名称模糊查询）。"""

    name: str | None = Field(
        default=None,
        description="商品名模糊查询关键词，例如 南红 / 星月菩提",
    )
    category: str | None = Field(
        default=None,
        description="商品分类精确匹配，例如 菩提/南红/紫檀/和田玉",
    )
    status: Literal["on_sale", "off_sale"] | None = Field(
        default=None,
        description="上架状态筛选：on_sale=在售 / off_sale=下架；不传返回全部",
    )
    limit: int = Field(default=10, ge=1, le=50, description="返回条数上限，默认 10")


class ProductQueryArgs(BaseModel):
    """product_query 参数（product_id 与 sku_code 二选一；同时传 product_id 优先）。"""

    product_id: str | None = Field(
        default=None,
        description="商品 UUID（与 sku_code 二选一优先）",
    )
    sku_code: str | None = Field(
        default=None,
        description="SKU 编码，例如 SKU-A-NANHONG-007",
    )


class CurrentTimeArgs(BaseModel):
    """current_time 无参数。"""


class PolicyCheckArgs(BaseModel):
    """policy_check 参数（不走 DB，纯函数）。"""

    order_detail: dict[str, Any] = Field(
        description="OrderQueryTool 返回的完整订单详情 dict（必须来自真实调用，禁止编造）"
    )
    intent_hint: str | None = Field(
        default="refund",
        description="可选：refund/exchange/repair 之一，告知政策判定重点方向",
        json_schema_extra={"enum": ["refund", "exchange", "repair"]},
    )


# ============================================================================
# 二、辅助函数（逐字移植）
# ============================================================================


def _build_order_detail_for_policy(row: OrderORM) -> dict[str, Any]:
    """从 OrderORM 提取 RefundQualificationService.decide 需要的 order_detail 字段。

    服务消费的 key：total_amount_cents / product_type / is_customized / delivered_at / status。
    """
    line_items = row.line_items_json or []
    product_type = "standard"
    if line_items and isinstance(line_items[0], dict):
        product_type = str(line_items[0].get("product_type") or "standard")
    logistics = row.logistics_json or {}
    delivered_at = logistics.get("delivered_at") if isinstance(logistics, dict) else None
    return {
        "order_id": str(row.order_id),
        "order_no": row.order_no,
        "tenant_id": row.tenant_id,
        "status": row.status,
        "product_type": product_type,
        "is_customized": "custom" in product_type.lower(),
        "delivered_at": delivered_at,
        "total_amount_cents": row.total_amount_yuan,  # 列名虽含 _yuan 但实际是 ×100 整数
    }


# 写工具 → 卡片展示用中文操作名
_ACTION_LABELS: dict[str, str] = {
    "refund_request": "退款申请",
    "exchange_request": "换货申请",
    "repair_request": "维修申请",
    "cancel_order": "取消订单",
}

# 写工具 reason 枚举 → 中文展示
_REASON_LABELS: dict[str, dict[str, str]] = {
    "refund_request": {
        "7_day_return": "7天无理由退货",
        "quality": "质量问题",
        "wrong_good": "发错商品",
        "other": "其他",
    },
    "exchange_request": {
        "quality": "质量问题",
        "size": "尺码不合适",
        "wrong_good": "发错商品",
        "other": "其他",
    },
    "cancel_order": {
        "no_longer_needed": "不想要了",
        "wrong_order": "下错订单",
        "price_change": "价格变动",
        "other": "其他",
    },
}


def _reason_label(tool_name: str, reason: str) -> str:
    return _REASON_LABELS.get(tool_name, {}).get(reason, reason)


def _build_pending(
    *,
    tool_name: str,
    args_dict: dict[str, Any],
    summary: str,
    order_no: str | None = None,
    fields: list[tuple[str, str]] | None = None,
    ttl_seconds: int = _HITL_TTL_SECONDS,
) -> dict[str, Any]:
    """构造 interrupt(pending) 的 pending payload（前端展示确认卡片 + 过期倒计时用）。

    展示字段：
      - action_label：中文操作名（退款申请/换货申请/维修申请/取消订单）
      - order_no：业务订单号（顶层固定位，卡片突出展示）
      - fields：[(label, value), ...] 结构化详情行（原因/金额/工单号等）
      - expires_at：确认截止时间（ISO，TTL 10min）
    """
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    pending: dict[str, Any] = {
        "tool": tool_name,
        "action_label": _ACTION_LABELS.get(tool_name, tool_name),
        "args": args_dict,
        "summary": summary,
        "expires_at": expires_at,
    }
    if order_no:
        pending["order_no"] = order_no
    if fields:
        pending["fields"] = [{"label": label, "value": value} for label, value in fields]
    return pending


def _slim_order_read(read: Any) -> dict[str, Any]:
    """order_query 输出瘦身：去掉 logistics.tracks 全量轨迹数组（保留 latest_track 当前节点）。

    全量轨迹会让 tool result 膨胀到 1.3K+ 字符，GLM-4-Flash 在下一轮调用
    （需把 order_detail 抄入 policy_check 参数）时生成超时并返回空响应。
    """
    data = read.model_dump(mode="json")
    logistics = data.get("logistics")
    if isinstance(logistics, dict):
        logistics.pop("tracks", None)
    return data


def _stable_ticket_no(prefix: str, *, tenant_id: str, thread_id: str, seed: str) -> str:
    """确定性工单号：interrupt 后 resume 时工具函数会整体重跑，uuid4 会让
    确认卡片上的工单号与最终执行结果不一致。用 uuid5（线程+参数为种子）
    保证两次执行生成同一工单号；同线程同参数重复申请天然幂等。
    """
    digest = uuid5(NAMESPACE_URL, f"{tenant_id}:{thread_id}:{seed}").hex[:8].upper()
    return f"{prefix}-{tenant_id.upper()}-{digest}"


def _decide_policy_for_write(
    *,
    tenant_id: str,
    row: OrderORM,
    intent: str,
    policy_override: Any | None,
) -> Any:
    """调用 RefundQualificationService.decide 做政策判定（纯函数，无 IO）。"""
    from app.application.services.refund_qualification import (
        PolicyInput,
        RefundQualificationService,
    )

    service = RefundQualificationService()
    return service.decide(
        PolicyInput(
            tenant_id=tenant_id,
            order_detail=_build_order_detail_for_policy(row),
            intent=intent,
            policy_override=policy_override,
        )
    )


async def _aload_order_for_write(
    *,
    repo: OrderRepository,
    tenant_id: str,
    order_id: UUID,
    actor: Any,
) -> OrderORM:
    """加载订单 + 权限校验（idempotent SELECT，resume 重跑无副作用）。

    跨租户/不存在/consumer 非自己 → 统一 ResourceNotFoundError（不泄露存在性）。
    """
    row = await repo.get_by_id(tenant_id, order_id)
    if row is None:
        raise ResourceNotFoundError(
            ErrorCode.RESOURCE_NOT_FOUND,
            message="resource not found",
            details={"resource_type": "order", "resource_id": str(order_id)},
        )
    if actor.role.value == "consumer" and str(row.buyer_user_id) != actor.actor_id:
        raise ResourceNotFoundError(
            ErrorCode.RESOURCE_NOT_FOUND,
            message="resource not found",
            details={"resource_type": "order", "resource_id": str(order_id)},
        )
    return row


def _parse_uuid(order_id_str: str) -> UUID:
    try:
        return UUID(order_id_str)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ToolExecutionError(
            ErrorCode.VALIDATION_ERROR,
            message=f"order_id 不是合法 UUID：{order_id_str}",
            retryable=True,
        ) from exc


# ============================================================================
# 三、读工具
# ============================================================================


@tool(args_schema=OrderQueryArgs)
async def order_query(
    order_id: str | None,
    order_no: str | None,
    runtime: ToolRuntime[AgentRunContext],
) -> dict[str, Any]:
    """查询订单详情（含当前物流状态、行项目、签收时间、总金额）。

    用于判断：是否在 7 天无理由窗口内 / 是否为定制款 / 是否超保修期 / 退款手续费比例。
    物流仅返回 latest_track 当前节点（不返回全量 tracks 轨迹数组，避免超长上下文）。
    """
    ctx = runtime.context
    tenant_id = ctx.actor.tenant_id
    repo = OrderRepository(ctx.session)
    if order_id:
        oid_uuid = _parse_uuid(order_id)
        read = await repo.get_read_for_actor(ctx.actor, tenant_id, oid_uuid)
        return _slim_order_read(read)
    if order_no:
        row = await repo.get_by_order_no(tenant_id, order_no)
        if row is None:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message=f"订单不存在：{order_no}",
                details={"order_no": order_no, "tenant_id": tenant_id},
            )
        read = await repo.get_read_for_actor(ctx.actor, tenant_id, row.order_id)
        return _slim_order_read(read)
    raise ToolExecutionError(
        ErrorCode.VALIDATION_ERROR,
        message="order_query 需要至少一个参数：order_id 或 order_no",
        retryable=True,
    )


@tool(args_schema=ProductListArgs)
async def product_list(
    name: str | None,
    category: str | None,
    status: str | None,
    limit: int,
    runtime: ToolRuntime[AgentRunContext],
) -> dict[str, Any]:
    """查询本租户商品目录列表（商品名模糊匹配，可按分类/上架状态过滤）。

    用于回答：商品是否在售、售价、库存、规格、分类等咨询。
    返回 items 数组，每项含商品名/SKU/分类/状态/规格/售价（分）/库存。
    """
    ctx = runtime.context
    if status is not None and status not in {"on_sale", "off_sale"}:
        raise ToolExecutionError(
            ErrorCode.VALIDATION_ERROR,
            message="status 必须是 on_sale 或 off_sale",
            retryable=True,
        )
    repo = ProductRepository(ctx.session)
    flt = ProductQueryFilter(
        name_like=name,
        category=category,
        status=status,  # type: ignore[arg-type]
        limit=limit,
    )
    items = await repo.list_products(ctx.actor.tenant_id, flt=flt)
    return {
        "items": [item.model_dump(mode="json") for item in items],
        "count": len(items),
    }


@tool(args_schema=ProductQueryArgs)
async def product_query(
    product_id: str | None,
    sku_code: str | None,
    runtime: ToolRuntime[AgentRunContext],
) -> dict[str, Any]:
    """按 product_id（商品 UUID）或 sku_code（SKU 编码）查询单个商品详情。

    返回商品名/分类/上架状态/规格/售价（分）/库存/创建更新时间。
    需要某个具体商品的准确售价或库存时优先使用本工具。
    """
    ctx = runtime.context
    tenant_id = ctx.actor.tenant_id
    repo = ProductRepository(ctx.session)
    if product_id:
        pid_uuid = _parse_uuid(product_id)
        read = await repo.get_read_by_id(tenant_id, pid_uuid)
        return read.model_dump(mode="json")
    if sku_code:
        read = await repo.get_read_by_sku(tenant_id, sku_code)
        return read.model_dump(mode="json")
    raise ToolExecutionError(
        ErrorCode.VALIDATION_ERROR,
        message="product_query 需要至少一个参数：product_id 或 sku_code",
        retryable=True,
    )


@tool(args_schema=CurrentTimeArgs)
async def current_time(runtime: ToolRuntime[AgentRunContext]) -> dict[str, Any]:
    """获取当前服务器时间（UTC+8），返回 now_iso / date_str / time_str / weekday。

    判断「是否在7天无理由窗口内/是否超期/签收至今几天」等时间问题时必须先调用本工具，
    禁止凭训练记忆猜测当前日期。
    """
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return {
        "now_iso": now.isoformat(timespec="seconds"),
        "date_str": now.strftime("%Y-%m-%d"),
        "time_str": now.strftime("%H:%M:%S"),
        "weekday": weekdays[now.weekday()],
        "timezone": "UTC+8",
    }


# ============================================================================
# 四、写工具（interrupt 人在回路确认）
# ============================================================================


@tool(args_schema=ExchangeRequestArgs)
async def exchange_request(
    order_id: str,
    reason: str,
    remark: str | None,
    runtime: ToolRuntime[AgentRunContext],
) -> dict[str, Any]:
    """提交换货申请（写操作）。调用本工具会自动弹出前端确认卡片（interrupt），用户点击确认后才真正受理工单，

    无需先用文字向用户索要确认——直接调用即可。
    必须在 policy_check 允许 can_exchange=true 后调用。
    执行成功后生成换货受理单号 EX-；换货不取消订单，仅记录工单。
    """
    from langgraph.types import interrupt

    if not order_id or reason not in {"quality", "size", "wrong_good", "other"}:
        raise ToolExecutionError(
            ErrorCode.VALIDATION_ERROR,
            message="参数非法：order_id 不能为空，reason 必须是枚举之一",
            retryable=True,
        )
    oid = _parse_uuid(order_id)
    ctx = runtime.context
    actor = ctx.actor
    repo = OrderRepository(ctx.session)
    row = await _aload_order_for_write(
        repo=repo, tenant_id=actor.tenant_id, order_id=oid, actor=actor
    )
    decision = _decide_policy_for_write(
        tenant_id=actor.tenant_id,
        row=row,
        intent="exchange",
        policy_override=ctx.effective_policy,
    )
    if not decision.can_exchange:
        raise ToolExecutionError(
            ErrorCode.REFUND_NOT_ELIGIBLE,
            message=f"政策不允许换货：{decision.reason_human_readable}",
            retryable=False,
            details={"reason_code": decision.reason_code},
        )

    ticket_no = _stable_ticket_no(
        "EX",
        tenant_id=actor.tenant_id,
        thread_id=ctx.thread_id,
        seed=f"exchange:{order_id}:{reason}:{remark or ''}",
    )
    fields: list[tuple[str, str]] = [
        ("原因", _reason_label("exchange_request", reason)),
    ]
    if remark:
        fields.append(("备注", remark))
    fields.append(("工单号", ticket_no))
    pending = _build_pending(
        tool_name="exchange_request",
        args_dict={"order_id": order_id, "reason": reason, "remark": remark},
        summary=f"换货申请：订单 {row.order_no}，原因 {reason}（工单号 {ticket_no}）",
        order_no=row.order_no,
        fields=fields,
    )
    resume_value = interrupt(pending)
    if not (isinstance(resume_value, dict) and resume_value.get("confirmed")):
        reason_text = resume_value.get("reason", "user_cancelled") if isinstance(resume_value, dict) else "user_cancelled"
        return {
            "tool": "exchange_request",
            "accepted": False,
            "reason": reason_text,
            "order_id": order_id,
            "order_no": row.order_no,
            "tenant_id": actor.tenant_id,
        }
    return {
        "tool": "exchange_request",
        "ticket_no": ticket_no,
        "accepted": True,
        "order_id": order_id,
        "order_no": row.order_no,
        "reason": reason,
        "reason_code": decision.reason_code,
        "reason_human_readable": decision.reason_human_readable,
        "tenant_id": actor.tenant_id,
        "message": "换货申请已受理，请保持商品完好等待客服联系。",
    }


@tool(args_schema=RepairRequestArgs)
async def repair_request(
    order_id: str,
    issue_desc: str,
    runtime: ToolRuntime[AgentRunContext],
) -> dict[str, Any]:
    """提交维修申请（写操作）。调用本工具会自动弹出前端确认卡片（interrupt），用户点击确认后才真正受理工单，

    无需先用文字向用户索要确认——直接调用即可。
    保修期内免费；必须在 policy_check 允许 can_repair=true 后调用。执行成功后生成维修工单 RP-。
    """
    from langgraph.types import interrupt

    if not order_id or not issue_desc or len(str(issue_desc)) < 2:
        raise ToolExecutionError(
            ErrorCode.VALIDATION_ERROR,
            message="参数非法：order_id 和 issue_desc 都必须提供，issue_desc 至少 2 字",
            retryable=True,
        )
    oid = _parse_uuid(order_id)
    ctx = runtime.context
    actor = ctx.actor
    repo = OrderRepository(ctx.session)
    row = await _aload_order_for_write(
        repo=repo, tenant_id=actor.tenant_id, order_id=oid, actor=actor
    )
    decision = _decide_policy_for_write(
        tenant_id=actor.tenant_id,
        row=row,
        intent="repair",
        policy_override=ctx.effective_policy,
    )
    if not decision.can_repair:
        raise ToolExecutionError(
            ErrorCode.REFUND_NOT_ELIGIBLE,
            message=f"政策不允许维修：{decision.reason_human_readable}",
            retryable=False,
            details={"reason_code": decision.reason_code},
        )

    ticket_no = _stable_ticket_no(
        "RP",
        tenant_id=actor.tenant_id,
        thread_id=ctx.thread_id,
        seed=f"repair:{order_id}:{issue_desc}",
    )
    pending = _build_pending(
        tool_name="repair_request",
        args_dict={"order_id": order_id, "issue_desc": issue_desc},
        summary=f"维修申请：订单 {row.order_no}，故障 {issue_desc}（工单号 {ticket_no}）",
        order_no=row.order_no,
        fields=[("故障描述", issue_desc), ("工单号", ticket_no)],
    )
    resume_value = interrupt(pending)
    if not (isinstance(resume_value, dict) and resume_value.get("confirmed")):
        reason_text = resume_value.get("reason", "user_cancelled") if isinstance(resume_value, dict) else "user_cancelled"
        return {
            "tool": "repair_request",
            "accepted": False,
            "reason": reason_text,
            "order_id": order_id,
            "order_no": row.order_no,
            "tenant_id": actor.tenant_id,
        }
    return {
        "tool": "repair_request",
        "ticket_no": ticket_no,
        "accepted": True,
        "order_id": order_id,
        "order_no": row.order_no,
        "issue_desc": issue_desc,
        "reason_code": decision.reason_code,
        "reason_human_readable": decision.reason_human_readable,
        "tenant_id": actor.tenant_id,
        "message": "维修申请已受理，请在保修期内寄回。",
    }


@tool(args_schema=RefundRequestArgs)
async def refund_request(
    order_id: str,
    reason: str,
    remark: str | None,
    runtime: ToolRuntime[AgentRunContext],
) -> dict[str, Any]:
    """提交退款申请（写操作）。调用本工具会自动弹出前端确认卡片（interrupt），用户点击确认后才真正执行退款，

    无需先用文字向用户索要确认——直接调用即可。
    必须在 policy_check 允许 can_refund=true 后调用。
    执行成功后订单状态流转为 refunded，退款金额/手续费来自 policy_decision。
    """
    from langgraph.types import interrupt

    if not order_id or reason not in {"7_day_return", "quality", "wrong_good", "other"}:
        raise ToolExecutionError(
            ErrorCode.VALIDATION_ERROR,
            message="参数非法：order_id 必填，reason 必须是 7_day_return/quality/wrong_good/other",
            retryable=True,
        )
    oid = _parse_uuid(order_id)
    ctx = runtime.context
    actor = ctx.actor
    repo = OrderRepository(ctx.session)
    row = await _aload_order_for_write(
        repo=repo, tenant_id=actor.tenant_id, order_id=oid, actor=actor
    )
    decision = _decide_policy_for_write(
        tenant_id=actor.tenant_id,
        row=row,
        intent="refund",
        policy_override=ctx.effective_policy,
    )
    if not decision.can_refund:
        raise ToolExecutionError(
            ErrorCode.REFUND_NOT_ELIGIBLE,
            message=f"政策不允许退款：{decision.reason_human_readable}",
            retryable=False,
            details={"reason_code": decision.reason_code},
        )

    ticket_no = _stable_ticket_no(
        "RF",
        tenant_id=actor.tenant_id,
        thread_id=ctx.thread_id,
        seed=f"refund:{order_id}:{reason}:{remark or ''}",
    )
    refund_amount = decision.refund_amount_cents
    fee_pct = decision.restocking_fee_pct
    pending = _build_pending(
        tool_name="refund_request",
        args_dict={"order_id": order_id, "reason": reason, "remark": remark},
        summary=(
            f"退款申请：订单 {row.order_no}，原因 {reason}，"
            f"退款金额 {refund_amount or 0} 分，手续费 {fee_pct}%（工单号 {ticket_no}）"
        ),
        order_no=row.order_no,
        fields=[
            ("原因", _reason_label("refund_request", reason)),
            ("退款金额", f"¥{(refund_amount or 0) / 100:.2f}"),
            ("手续费", f"{fee_pct}%"),
            ("工单号", ticket_no),
        ],
    )
    resume_value = interrupt(pending)
    if not (isinstance(resume_value, dict) and resume_value.get("confirmed")):
        reason_text = resume_value.get("reason", "user_cancelled") if isinstance(resume_value, dict) else "user_cancelled"
        return {
            "tool": "refund_request",
            "accepted": False,
            "reason": reason_text,
            "order_id": order_id,
            "order_no": row.order_no,
            "tenant_id": actor.tenant_id,
        }
    # 确认后执行真实状态流转（状态机保证防重复写入）
    updated = await repo.update_status(actor.tenant_id, oid, "refunded", actor=actor)
    return {
        "tool": "refund_request",
        "ticket_no": ticket_no,
        "accepted": True,
        "order_id": order_id,
        "order_no": updated.order_no,
        "new_status": "refunded",
        "refund_amount_cents": refund_amount,
        "fee_pct": fee_pct,
        "reason": reason,
        "reason_code": decision.reason_code,
        "reason_human_readable": decision.reason_human_readable,
        "tenant_id": actor.tenant_id,
    }


@tool(args_schema=CancelOrderArgs)
async def cancel_order(
    order_id: str,
    reason: str,
    remark: str | None,
    runtime: ToolRuntime[AgentRunContext],
) -> dict[str, Any]:
    """取消订单（写操作）。调用本工具会自动弹出前端确认卡片（interrupt），用户点击确认后才真正执行取消，

    无需先用文字向用户索要确认——直接调用即可。
    仅当订单处于 pending_payment 或 paid 状态时可取消；
    已发货/已签收的订单不可取消，需走退款或换货流程。执行成功后订单状态流转为 cancelled。
    """
    from langgraph.types import interrupt

    if not order_id or reason not in {
        "no_longer_needed",
        "wrong_order",
        "price_change",
        "other",
    }:
        raise ToolExecutionError(
            ErrorCode.VALIDATION_ERROR,
            message="参数非法：order_id 必填，reason 必须是 no_longer_needed/wrong_order/price_change/other",
            retryable=True,
        )
    oid = _parse_uuid(order_id)
    ctx = runtime.context
    actor = ctx.actor
    repo = OrderRepository(ctx.session)
    row = await _aload_order_for_write(
        repo=repo, tenant_id=actor.tenant_id, order_id=oid, actor=actor
    )
    # 取消不做 policy_check（仅看订单状态机，由 update_status 强制校验）
    if row.status not in {"pending_payment", "paid"}:
        raise ToolExecutionError(
            ErrorCode.VALIDATION_ERROR,
            message=(
                f"订单 {row.order_no} 当前状态为 {row.status}，不可取消；"
                "已发货/已签收的订单请走退款或换货流程。"
            ),
            retryable=False,
            details={"order_no": row.order_no, "current_status": row.status},
        )

    ticket_no = _stable_ticket_no(
        "CX",
        tenant_id=actor.tenant_id,
        thread_id=ctx.thread_id,
        seed=f"cancel:{order_id}:{reason}:{remark or ''}",
    )
    cancel_fields: list[tuple[str, str]] = [
        ("原因", _reason_label("cancel_order", reason)),
    ]
    if remark:
        cancel_fields.append(("备注", remark))
    cancel_fields.append(("工单号", ticket_no))
    pending = _build_pending(
        tool_name="cancel_order",
        args_dict={"order_id": order_id, "reason": reason, "remark": remark},
        summary=f"取消订单：{row.order_no}，原因 {reason}（工单号 {ticket_no}）",
        order_no=row.order_no,
        fields=cancel_fields,
    )
    resume_value = interrupt(pending)
    if not (isinstance(resume_value, dict) and resume_value.get("confirmed")):
        reason_text = resume_value.get("reason", "user_cancelled") if isinstance(resume_value, dict) else "user_cancelled"
        return {
            "tool": "cancel_order",
            "accepted": False,
            "reason": reason_text,
            "order_id": order_id,
            "order_no": row.order_no,
            "tenant_id": actor.tenant_id,
        }
    updated = await repo.update_status(actor.tenant_id, oid, "cancelled", actor=actor)
    return {
        "tool": "cancel_order",
        "ticket_no": ticket_no,
        "accepted": True,
        "order_id": order_id,
        "order_no": updated.order_no,
        "new_status": "cancelled",
        "reason": reason,
        "tenant_id": actor.tenant_id,
    }


# ============================================================================
# 五、虚拟工具：policy_check（纯函数，不写库）
# ============================================================================


@tool(args_schema=PolicyCheckArgs)
async def policy_check(
    order_detail: dict[str, Any],
    intent_hint: str | None,
    runtime: ToolRuntime[AgentRunContext],
) -> dict[str, Any]:
    """根据订单详情 + 租户政策（纯函数确定性计算，不写 DB）

    判断该订单能否退款/换货/维修。返回值包含 can_refund/can_exchange/can_repair/
    reason_code/reason_human_readable。必须在 order_query 成功后调用一次。
    """
    from app.application.services.refund_qualification import (
        PolicyInput,
        RefundQualificationService,
    )

    intent = str(intent_hint or "refund")
    if intent not in {"refund", "exchange", "repair"}:
        intent = "refund"
    ctx = runtime.context
    service = RefundQualificationService()
    decision = service.decide(
        PolicyInput(
            tenant_id=ctx.tenant_id,
            order_detail=order_detail,
            intent=intent,
            policy_override=ctx.effective_policy,
        )
    )
    return decision.to_state_json()


# ============================================================================
# 六、工具清单（顺序与旧 build_langchain_tools 白名单一致）
# ============================================================================

ALL_TOOLS: list = [
    product_list,
    product_query,
    order_query,
    current_time,
    exchange_request,
    repair_request,
    refund_request,
    cancel_order,
    policy_check,
]

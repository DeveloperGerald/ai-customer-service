"""task 分支原生 Agent（LangChain 1.x create_agent）。

- create_agent(chat_model, ALL_TOOLS) 一次构建、多轮复用：工具为模块级 @tool
  （无状态），身份/租户/session 经 `ainvoke(..., context=AgentRunContext)` 每次注入；
- ToolGovernanceMiddleware 承接审计（tool_audit_logs）+ 幂等（idempotency_records），
  并把 tool_executions / policy_decision / order_detail_json / action_kind /
  action_result_json / 拒绝文案（draft_reply/final_reply）写进本子图 state；
- @dynamic_prompt 每次模型调用时按 state（intent_hint/rag_hits/order_ref_candidate）
  + context（effective_policy/tenant_id）动态拼 system prompt（内容与旧实现逐字一致）；
- 本子图不挂 checkpointer：写工具 interrupt(pending) 的 GraphInterrupt 从这里
  冒泡到外层图（task 节点 re-raise），由外层 checkpointer 落 Redis（HITL）。
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from typing import Any, NotRequired

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import dynamic_prompt
from langchain.agents.middleware.types import AgentMiddleware, ModelRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.application.agent.context import AgentRunContext
from app.application.agent.governance import ToolGovernanceMiddleware
from app.application.tools.builtin import ALL_TOOLS
from app.domain.constants.policies import TenantPolicy


class TaskAgentState(AgentState, total=False):
    """create_agent 的 state schema：AgentState（messages/jump_to/structured_response）
    + 外层图传入字段（dynamic_prompt 消费）+ 治理中间件写入字段（task 节点读取后
    回传外层图）。"""

    # ---- 外层图传入（dynamic_prompt 消费）----
    intent_hint: NotRequired[str | None]
    rag_hits: NotRequired[list[dict[str, Any]]]
    order_ref_candidate: NotRequired[dict[str, Any] | None]
    # ---- ToolGovernanceMiddleware 写入 ----
    tool_executions: NotRequired[list[dict[str, Any]]]
    policy_decision: NotRequired[dict[str, Any]]
    order_detail_json: NotRequired[dict[str, Any]]
    action_kind: NotRequired[str]
    action_result_json: NotRequired[dict[str, Any]]
    draft_reply: NotRequired[str]
    final_reply: NotRequired[str]


def _build_agent_system_prompt(
    *,
    tenant_id: str,
    intent_hint: str | None,
    rag_hits: list[dict[str, Any]] | None,
    policy_override: TenantPolicy | None,
    order_ref_candidate: dict[str, Any] | None = None,
) -> str:
    """拼 Agent 子图的 system prompt（RAG 结果 + 政策摘要 + 角色约束）。

    关键约束：
      1. policy_check 必须在写工具之前调用；
      2. 写工具成功后立即 Final Answer；
      3. 写工具会先向用户确认（interrupt），确认后才执行。
    """
    brand = ""
    slogan = ""
    if policy_override is not None:
        brand = policy_override.brand_name or ""
        slogan = policy_override.slogan or ""
    now = _dt.datetime.now(_dt.timezone.utc).astimezone(_dt.timezone(_dt.timedelta(hours=8)))
    role_header = [
        "你是一位专业的手串品牌售后智能客服 Agent（ReAct 模式）。",
        "你只能调用白名单工具来回答用户问题，禁止编造订单信息、政策或工单号。",
        f"【当前时间】{now.strftime('%Y-%m-%d %H:%M:%S')}（UTC+8）。所有涉及「几天内/是否超期/7天无理由」的时间判断，"
        "必须以此时间为准，禁止凭感觉或训练记忆猜测当前日期。",
    ]
    if order_ref_candidate and order_ref_candidate.get("order_no"):
        role_header.append(
            f"【检测到订单号】用户消息中检测到订单号：{order_ref_candidate['order_no']}，"
            "请优先使用此订单号调用 order_query。"
        )
    else:
        role_header.append(
            "【重要】用户本次消息中未检测到订单号。如果对话历史中也没有订单号，"
            "你必须直接请用户提供订单号（作为 Final Answer 返回），"
            "绝对禁止自行编造任何订单号。"
        )
    if brand:
        role_header.append(f"当前服务品牌：{brand}" + (f"（{slogan}）" if slogan else ""))
    role_header.append("租户 ID 仅供内部日志使用，禁止在回复中向用户暴露 tenant_id。")

    hard_constraints = [
        "",
        "【意图判断前提（必须严格遵守）】",
        "  判断用户真实意图时，必须结合对话历史，不能只看当前这一条消息。",
        "  例如：上文用户说「我想取消订单」，本轮只回复一个订单号——本轮意图仍是 cancel，不是 order_status。",
        "  intent_hint 仅来自当前消息的表层信号，当它与对话历史冲突时，以对话历史为准。",
        "",
        "【调用顺序硬约束（必须严格遵守）】",
        "  Step 1. 若用户提供了订单号/你需要订单信息：先调用 order_query(order_no=...) 获取订单详情。",
        "         必须用真实 order_id/order_no，不准编造。缺订单号则让用户提供（作为 Final Answer 返回）。",
        "  Step 2. 退款/换货/维修流程：拿到 order_detail 后，立刻调用 policy_check(order_detail=<Step1结果>, intent_hint=<refund/exchange/repair 之一>)。",
        "         policy_check 返回的 can_refund/can_exchange/can_repair 是唯一真理，不准自己判断。",
        "         【严禁】看到 delivered_at 后自行心算「签收至今几天」来判断是否在7天内——",
        "         必须调用 policy_check，由服务端用真实当前时间计算 days_since_delivery。",
        "  Step 3. 若 policy_check 返回某 can_*=true：再调用对应写工具（refund_request/exchange_request/repair_request）。",
        "         写工具会先向用户确认（interrupt），用户确认后才执行。写工具执行成功后 → 立即输出 Final Answer，不再调用任何工具。",
        "  Step 4. 若 policy_check 返回对应 can_*=false：直接给出 Final Answer，告知原因 + 引导回复「人工」转人工。",
        "  Step 5. 【取消订单专属流程，必须严格遵守，不可跳过】",
        "         a. 取消订单不需要 policy_check，仅看订单状态。",
        "         b. 当用户意图是取消订单（结合对话历史判断）时：先调用 order_query 拿到 order_id 和 status。",
        "         c. 若 status ∈ {pending_payment, paid}：你必须调用 cancel_order 工具",
        "            （order_id 取自 order_query 返回的 order_id；reason 选 no_longer_needed/wrong_order/price_change/other 中最贴近的一项，缺信息时选 other）。",
        "         d. cancel_order 工具内部会触发用户确认弹窗（interrupt），用户在前端点确认后才真正执行取消。",
        "         e. 禁止仅用文字回复「可以取消 / 我们将为您处理」而不调用 cancel_order 工具——",
        "            不调用工具就不会发出确认弹窗，用户的取消请求不会被处理。你的职责是调用工具，不是替用户做取消动作。",
        "         f. 若 order_query 返回 status ∉ {pending_payment, paid}（已发货/已签收等）：",
        "            直接 Final Answer 告知不可取消及原因，引导走退款/换货流程。",
        "  Step 6. 最多 5 轮；超过直接汇总已获取信息作为 Final Answer。",
        "",
        "【写操作最终禁令（违反即视为错误回答）】",
        "  - 用户意图为退款/换货/维修/取消订单时，你的唯一职责是按上述 Step 调用工具；",
        "    禁止在未调用对应写工具（refund_request/exchange_request/repair_request/cancel_order）的情况下，",
        "    用 Final Answer 宣称「申请已收到/已受理/工单已提交/我们将为您处理」。",
        "  - 禁止编造政策（如「商品已发货需先寄回」）——能否维修/退/换以 policy_check 返回为唯一依据。",
        "  - 禁止编造工单号——工单号只能来自写工具返回的 ticket_no。",
        "  - 若缺少写工具必填参数（如维修缺故障描述），Final Answer 只做一件事：向用户索要该信息。",
    ]
    if intent_hint:
        hard_constraints.append(
            f"  · 当前消息的 intent_hint 信号：{intent_hint}（仅供 policy_check 的 intent_hint 参数参考；"
            "若与对话历史冲突，以对话历史为准。）"
        )

    rag_block: list[str] = ["", "【RAG 参考片段（来自前置检索，仅供语言组织，不替代 policy_check 结果）】"]
    if rag_hits:
        for idx, h in enumerate(rag_hits[:3]):
            content = (h.get("content") or "").strip().replace("\n", " ")
            if len(content) > 400:
                content = content[:400] + "…"
            title = h.get("metadata", {}).get("title") if isinstance(h.get("metadata"), dict) else None
            rag_block.append(f"  [{idx + 1}]" + (f" {title}：" if title else " ") + content)
    else:
        rag_block.append("  （无前置 RAG 命中；请以工具返回和政策判定为准。）")

    output_fmt = [
        "",
        "【Final Answer 输出格式约束】",
        "  - 如果写工具成功且返回 ticket_no：第一句写政策原因，第二句写「✅/🔁/🔧 XXXX 工单号：RF-xxxx」；不要 Markdown 标题。",
        "  - 如果 policy deny：第一句告知不满足原因，第二句引导「如需进一步协助，请回复「人工」」。",
        "  - 如果缺订单号：直接请用户提供订单号，简短友好。",
        "  - 字数控制在 60~160 字；禁止输出「Thought:」「Action:」等中间步骤标签。",
    ]
    return "\n".join(role_header + hard_constraints + rag_block + output_fmt)


@dynamic_prompt
def _task_system_prompt(request: ModelRequest) -> str:
    """每次模型调用时动态拼 system prompt（内容与旧 _build_agent_system_prompt 一致）。"""
    state = request.state
    ctx: AgentRunContext = request.runtime.context
    return _build_agent_system_prompt(
        tenant_id=ctx.tenant_id,
        intent_hint=state.get("intent_hint"),
        rag_hits=state.get("rag_hits") or [],
        policy_override=ctx.effective_policy,
        order_ref_candidate=state.get("order_ref_candidate"),
    )


class _RetryOnEmptyMiddleware(AgentMiddleware):
    """GLM-4-Flash 偶发返回空 AIMessage（content 为空且无 tool_calls），
    ReAct 循环会静默终止 → 用户看到空泛回复、写流程中断（不发确认卡片）。
    检测到空响应时追加催促消息重试一次（仅一次，不会死循环）。
    """

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> Any:  # type: ignore[no-untyped-def]
        response = await handler(request)
        msg = _first_ai_message(response)
        if not _is_empty_ai_message(msg):
            return response
        from app.core.logging import get_logger

        get_logger("agent.task.retry_empty").warning("model.empty_response_retry")
        nudge = HumanMessage(
            content="（系统提示）请继续按流程处理：已拿到订单详情就立即调用 policy_check/对应写工具；"
            "信息不足就直接向用户提问。禁止输出空回复。"
        )
        retry_request = dataclasses.replace(
            request, messages=[*request.messages, nudge]
        )
        return await handler(retry_request)


def _first_ai_message(response: Any) -> AIMessage | None:
    result = getattr(response, "result", None)
    if isinstance(result, list) and result:
        first = result[0]
        return first if isinstance(first, AIMessage) else None
    return response if isinstance(response, AIMessage) else None


def _is_empty_ai_message(msg: AIMessage | None) -> bool:
    if msg is None:
        return False
    if msg.tool_calls:
        return False
    content = msg.content
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        return not any(
            (isinstance(p, dict) and str(p.get("text", "")).strip()) or (isinstance(p, str) and p.strip())
            for p in content
        )
    return True


def build_task_agent(chat_model: BaseChatModel) -> Any:
    """构建 task 分支的 create_agent 图（不挂 checkpointer：interrupt 冒泡给外层图）。

    Returns:
        CompiledStateGraph：ainvoke({"messages": [...], ...}, context=AgentRunContext)。
    """
    return create_agent(
        chat_model,
        ALL_TOOLS,
        middleware=[ToolGovernanceMiddleware(), _task_system_prompt, _RetryOnEmptyMiddleware()],
        state_schema=TaskAgentState,
        context_schema=AgentRunContext,
    )

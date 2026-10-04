"""Agent 节点实现（四分类意图重构版）：
- 所有节点均为无状态：(state: AgentState, ctx: AgentNodeContext) → Partial<AgentState>
- 四分类意图：simple_qa / handoff / knowledge_qa / task
- 知识问答：policy_lookup 优先（结构化政策字段），未命中才 RAG
- 写操作：task 子图内写工具内置 interrupt 人在回路确认
- 所有分支汇总到 compliance_check（规则合规 + LLM 包装）→ END
- 节点异常统一兜底（facade _trace wrapper 捕获 → draft_reply 安全文案）
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent.context import AgentRunContext
from app.application.schemas.agent import AgentState
from app.application.schemas.conversation import ConversationMessageCreate
from app.domain.constants.policies import TenantPolicy
from app.domain.repositories.conversation import ConversationRepository
from app.domain.repositories.identity import Actor
from app.infrastructure.llm.classifiers import (
    IntentClassifierProtocol,
)
from app.infrastructure.llm.providers import BaseRetriever

_CUSTOMER_SERVICE_SYSTEM_PROMPT = """你是一位专业、耐心、有温度的手串品牌售后智能客服。
请严格按【参考上下文】中的政策判定结果、RAG 知识片段、工具执行结果来回应用户，
禁止编造政策、禁止承诺上下文里没有的权益。

角色与语气约束：
  - 身份：同品牌官方售后客服，使用品牌名作为开头（上下文里已给出品牌名）
  - 语气：真诚、礼貌、简洁，避免官话套话，像朋友帮用户解决问题
  - 字数：单条回复控制在 80~200 字，除非工具结果必须详细
  - 禁止：输出 Markdown 标题/粗体/代码块；输出 emoji 仅限于 ✅🔁🔧⚠️🚫✅💬 等少量必要的

严格遵循以下优先级（从上到下）输出回复：
  1. 若【已转人工】：第一句说明已转人工 + 原因，第二句给工单号，第三句告知接入时效；禁止继续解答问题
  2. 若 action_kind = refund / exchange / repair / cancel：先讲政策判定原因，再给工单号，最后补必要的提醒（如上传凭证/等联系）
  3. 若 action_kind = simple_qa：欢迎语 + 引导用户说明需求
  4. 知识问答/政策不满足等其他：先直接回答用户问题，结合 RAG 片段；信息不足时引导转人工（回复「人工」）

当上下文中真实存在工单号、退款金额、手续费比例时，必须将这些关键数字明确写在回复里，方便用户截图留证。
禁止编造上下文中不存在的工单号、金额、比例等数字；知识问答类回复没有工单号，不要提及工单。
"""


@dataclass
class AgentNodeContext:
    """节点运行上下文（避免每个节点都拿一堆参数，便于 T7 单测替换）。"""

    session: AsyncSession
    actor: Actor
    retriever: BaseRetriever | None  # None 时用规则关键词命中模拟 RAG（MVP 单测友好）
    conversation_repo: ConversationRepository
    service_actor: Actor  # 同租户 STAFF 角色：写 tool/agent 消息、mark_escalated 等内部操作统一用它
    classifier: IntentClassifierProtocol | None = None  # 仅 LLMIntentClassifier；None 时节点兜底 simple_qa
    chat_model: BaseChatModel | None = None  # compliance/policy_lookup LLM 调用；None 时节点兜底
    task_agent: Any | None = None  # create_agent 子图（task_agent.build_task_agent 产物）
    idempotency_salt: str = ""
    rag_top_k: int = 4
    rag_similarity_threshold: float = 0.5
    effective_policy: TenantPolicy | None = None


# =========================================================================
# 0. 小型规则工具（政策判定）
#   意图识别逻辑已统一收敛到 app.infrastructure.llm.classifiers（LLMIntentClassifier
#   直接输出图路由的 4 大类 + intent_hint），节点层不再维护第二套关键词规则。
# =========================================================================


# =========================================================================
# 1. LangGraph 节点
# =========================================================================


async def intent_classify_node(state: AgentState, ctx: AgentNodeContext) -> dict[str, Any]:
    """入口节点：调用 IntentClassifierProtocol 输出 4 大类路由意图。

    4 分类：simple_qa / handoff / knowledge_qa / task
    + intent_hint（refund/exchange/repair/cancel/order_status/product）+ 订单号候选。
    分类器为 None 或非法值 → 兜底 simple_qa（交 LLM 引导澄清）。
    """
    text = state.get("user_message", "") or ""
    if ctx.classifier is None:
        return {
            "intent_candidate": "simple_qa",
            "order_ref_candidate": None,
            "intent_hint": None,
        }
    # 加载最近对话历史，让分类器能结合上下文判断意图（解决「用户先说取消订单、
    # 本轮只发订单号」被误判为 order_status、进而导致 task 节点不调写工具、
    # 不推确认卡片的问题）。facade 已在本节点前把本轮用户消息写入会话；
    # 取最后一条之前的消息作为历史（排除本轮，避免与当前 user_message 重复）。
    history: list[dict[str, str]] | None = None
    try:
        msgs = await ctx.conversation_repo.list_messages(
            ctx.service_actor,
            state.get("tenant_id", ""),
            state.get("thread_id", ""),
            limit=9,
        )
        recent = [
            m for m in msgs
            if m.role in ("human", "user", "agent", "ai", "assistant")
        ][-9:-1]  # 末条为本轮 user_message，排除以免重复
        if recent:
            history = [
                {
                    "role": "user" if m.role in ("human", "user") else "assistant",
                    "content": (m.content or "")[:150],
                }
                for m in recent
            ]
    except Exception:  # pragma: no cover - 历史加载失败不阻塞分类
        history = None
    intent, order_ref, hint = await ctx.classifier.aclassify(
        text,
        tenant_id=state.get("tenant_id"),
        thread_id=state.get("thread_id"),
        history=history,
    )
    whitelist_4 = {"simple_qa", "handoff", "knowledge_qa", "task"}
    if intent not in whitelist_4:
        intent = "simple_qa"
    return {
        "intent_candidate": intent,
        "order_ref_candidate": order_ref,
        "intent_hint": hint,
    }


def intent_router(state: AgentState) -> str:
    """条件边路由：intent_candidate → 节点名。

    simple_qa    → compliance_check（直接交 LLM 引导）
    handoff      → handoff（立即转人工 → compliance_check）
    knowledge_qa → policy_lookup（先查结构化政策字段）
    task         → task（ReAct 子图执行读写工具）
    """
    intent = state.get("intent_candidate", "simple_qa")
    if intent == "handoff":
        return "handoff"
    if intent == "knowledge_qa":
        return "policy_lookup"
    if intent == "task":
        return "task"
    return "compliance_check"  # simple_qa 或未知


# =========================================================================
# 1.1 知识问答：政策优先 → RAG 兜底
# =========================================================================


async def policy_lookup_node(state: AgentState, ctx: AgentNodeContext) -> dict[str, Any]:
    """知识问答政策优先节点：LLM 判断结构化政策字段能否直接回答用户问题。

    命中 → policy_lookup_hit=True + policy_answer（字段化文案）→ 走 compliance 直接答
    未命中 → policy_lookup_hit=False → 走 rag_retrieve 检索向量库
    LLM 失败/未配置 → policy_lookup_hit=False 兜底走 RAG
    """
    policy = ctx.effective_policy
    user_msg = state.get("user_message", "") or ""

    if policy is None:
        return {"policy_lookup_hit": False, "policy_answer": None}

    fields_summary = {
        "return_days": policy.return_days,
        "return_policy_type": policy.return_policy_type,
        "custom_product_allowed": policy.custom_product_allowed,
        "restocking_fee_pct_non_quality": policy.restocking_fee_pct_non_quality,
        "warranty_days_quality": policy.warranty_days_quality,
        "brand_name": policy.brand_name,
        "slogan": policy.slogan,
    }

    if ctx.chat_model is None:
        return {"policy_lookup_hit": False, "policy_answer": None}

    judge_prompt = (
        "你是一个政策判断助手。根据以下租户售后政策结构化字段，判断能否直接回答用户的问题。\n\n"
        f"租户政策字段：\n{json.dumps(fields_summary, ensure_ascii=False, indent=2)}\n\n"
        '字段说明：return_days(无理由退换天数窗口,None表示不支持无理由退换),return_policy_type(无理由退货/仅质量问题退货/支持质量问题和非质量问题退货),custom_product_allowed(定制商品是否允许非质量退货),restocking_fee_pct_non_quality(非质量问题退货手续费百分比,0=不收取任何手续费,>0=收取实付金额该百分比的手续费),warranty_days_quality(质量问题保修期限或免费退换天数),brand_name(品牌名),slogan(品牌宣传语)\n'
        f"用户问题：{user_msg}\n\n"
        "【硬约束】answer 必须严格基于上述字段的值，逐字核对数字（尤其 0 与非 0 的区别），"
        "禁止编造字段中不存在的数字、工单号、金额、比例或'具体费用根据情况而定'等模糊表述。"
        "answer 必须是完整可发给用户的最终中文回复，不得出现'基于政策字段的回答'这类占位文字。\n"
        '如果能用上述字段直接回答，输出 JSON：{"can_answer": true, "answer": "完整回答"}\n'
        '如果不能（需要更多订单信息/售后操作/不在政策范围内），输出 JSON：{"can_answer": false}\n'
        "只输出 JSON，不要 markdown 代码块，不要其他内容。"
    )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        result = await ctx.chat_model.ainvoke(
            [
                SystemMessage(content="你是政策判断助手，只输出 JSON。"),
                HumanMessage(content=judge_prompt),
            ]
        )
        # result 可能是 AIMessage 或 str
        raw = ""
        if hasattr(result, "content"):
            content = result.content
            raw = content if isinstance(content, str) else str(content)
        else:
            raw = str(result)

        # 解析 JSON（容错：提取第一个 {...} 块）
        answer_dict = _safe_json_extract(raw)
        if answer_dict and answer_dict.get("can_answer") is True:
            answer_text = str(answer_dict.get("answer") or "").strip()
            if answer_text:
                return {
                    "policy_lookup_hit": True,
                    "policy_answer": {
                        "answer": answer_text,
                        "source": "policy_fields",
                        "fields": fields_summary,
                    },
                    "draft_reply": answer_text,
                    "draft_context": {"source": "policy"},
                }
    except Exception:  # pragma: no cover - LLM 失败兜底
        pass

    return {"policy_lookup_hit": False, "policy_answer": None}


def knowledge_router(state: AgentState) -> str:
    """条件边路由：policy_lookup_hit → 节点名。

    True  → compliance_check（政策字段直接作答）
    False → rag_retrieve（RAG 检索向量库）
    """
    if state.get("policy_lookup_hit") is True:
        return "compliance_check"
    return "rag_retrieve"


async def rag_retrieve_node(state: AgentState, ctx: AgentNodeContext) -> dict[str, Any]:
    """RAG 检索：仅在 knowledge_qa 未命中政策时调用。

    硬约束：必须带 tenant_id 过滤（T5 retriever 自己保证）。
    """
    tenant_id = state["tenant_id"]
    query = state.get("user_message", "") or ""
    if ctx.retriever is None:
        return {"rag_hits": []}
    hits = await ctx.retriever.retrieve(
        tenant_id=tenant_id,
        query=query,
        top_k=ctx.rag_top_k,
        similarity_threshold=ctx.rag_similarity_threshold,
    )
    rag_hits = [
        {
            "chunk_id": h.get("chunk_id"),
            "tenant_id": tenant_id,
            "content": h.get("content", ""),
            "similarity": h.get("similarity"),
            "metadata": h.get("metadata") or {},
        }
        for h in hits
    ]
    return {
        "rag_hits": rag_hits,
        "draft_context": {"source": "rag"},
    }


# =========================================================================
# 1.2 转人工节点
# =========================================================================


async def handoff_node(state: AgentState, ctx: AgentNodeContext) -> dict[str, Any]:
    """D3：明确的转人工操作立即执行 → mark_escalated 填 ticket_no。"""
    ticket_no = f"HO-{ctx.actor.tenant_id.upper()}-{uuid4().hex[:8].upper()}"
    await ctx.conversation_repo.mark_escalated(
        ctx.service_actor, state["tenant_id"], state["thread_id"], ticket_no
    )
    reason = state.get("escalation_reason") or (
        state.get("policy_decision") or {}
    ).get("reason_human_readable") or "用户申请人工客服。"
    return {
        "action_kind": "handoff",
        "action_result_json": {"ticket_no": ticket_no, "stub": True, "tenant_id": state["tenant_id"]},
        "escalated": True,
        "escalated_ticket_no": ticket_no,
        "escalation_reason": reason,
        "draft_reply": f"已为您转接人工客服。工单号：{ticket_no}。请稍候，客服将尽快接入。",
        "draft_context": {"source": "handoff"},
    }


# =========================================================================
# 1.3 task 节点：原生 create_agent 子图（ToolGovernanceMiddleware + @dynamic_prompt）
# =========================================================================


async def task_node(state: AgentState, ctx: AgentNodeContext) -> dict[str, Any]:
    """task 分支节点：调用原生 create_agent 子图（task_agent.build_task_agent 产物）。

    - 会话历史（limit=20，跳过 tool 消息）+ 当前 HumanMessage 注入子图 messages；
      system prompt 不在此处注入（@dynamic_prompt 中间件每次模型调用时动态拼）；
    - intent_hint / rag_hits / order_ref_candidate 作为子图 state 输入（@dynamic_prompt 消费）；
    - 身份/租户/session 经 context=AgentRunContext 注入（@tool 与治理中间件取用）；
    - 写工具 interrupt(pending) → 子图 ainvoke 返回 __interrupt__ → re-raise
      GraphInterrupt 冒泡到外层图落 checkpointer（HITL）；
    - 治理中间件写入子图 state 的 tool_executions / policy_decision /
      order_detail_json / action_kind / action_result_json / 拒绝文案回传外层图。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    tenant_id = state["tenant_id"]
    thread_id = state.get("thread_id") or ""
    user_msg = state.get("user_message", "") or ""

    if ctx.task_agent is None:
        return {
            "draft_reply": "抱歉，当前无法执行售后操作，请稍后重试或回复「人工」转人工。",
            "draft_context": {"source": "task", "error": "no_llm"},
        }

    messages: list[Any] = []
    # 加载对话历史：让 agent 能看到前序轮次的上下文
    # （如用户先给订单号查询，再追问"都需要"时 agent 能引用上一轮的 order_query 结果）
    try:
        msg_list = await ctx.conversation_repo.list_messages(
            ctx.service_actor,
            tenant_id,
            thread_id,
            limit=20,
        )
        for m in msg_list:
            if m.role == "tool":
                continue
            if m.role in ("human", "user"):
                messages.append(HumanMessage(content=m.content))
            elif m.role in ("agent", "ai", "assistant"):
                messages.append(AIMessage(content=m.content))
    except Exception as exc:
        from app.core.logging import get_logger as _get_logger
        _get_logger("agent.nodes.task").warning(
            "task.load_history_non_fatal", error_type=type(exc).__name__
        )
    messages.append(HumanMessage(content=user_msg))

    run_ctx = AgentRunContext(
        actor=ctx.actor,
        tenant_id=tenant_id,
        thread_id=thread_id or f"{tenant_id}:local-{uuid4().hex[:8]}",
        service_actor=ctx.service_actor,
        effective_policy=ctx.effective_policy,
        idempotency_salt=ctx.idempotency_salt,
        session=ctx.session,
        conversation_repo=ctx.conversation_repo,
    )

    result = await ctx.task_agent.ainvoke(
        {
            "messages": messages,
            "intent_hint": state.get("intent_hint"),
            "rag_hits": state.get("rag_hits") or [],
            "order_ref_candidate": state.get("order_ref_candidate"),
        },
        config={"recursion_limit": 15},
        context=run_ctx,
    )

    # create_agent 子图（无 checkpointer）的 ainvoke() 不会 raise GraphInterrupt，
    # 而是把 interrupt 作为 result['__interrupt__'] 返回。需要手动 re-raise
    # GraphInterrupt，让外层图的 Pregel loop 捕获并落 checkpointer，这样
    # facade._detect_interrupt_pending 才能通过 get_state() 检测到暂停态，
    # emit confirmation_required SSE frame。
    _interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
    if _interrupts:
        from langgraph.errors import GraphInterrupt as _GraphInterrupt

        raise _GraphInterrupt(tuple(_interrupts))

    result_msgs = result.get("messages") or []
    final_answer = ""
    if result_msgs:
        last = result_msgs[-1]
        content = getattr(last, "content", "")
        if isinstance(content, str):
            final_answer = content
        elif isinstance(content, list):
            parts = [str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content]
            final_answer = "".join(parts)

    patch: dict[str, Any] = {}
    # 治理中间件写入子图 state 的字段回传外层图
    for _key in (
        "tool_executions",
        "policy_decision",
        "order_detail_json",
        "action_kind",
        "action_result_json",
    ):
        _v = result.get(_key)
        if _v is not None:
            patch[_key] = _v

    # 写工具拒绝确认（accepted=False）时治理中间件已直写标准拒绝文案
    # （draft_reply/final_reply），优先于 LLM 的最终答复，直通 compliance
    # 「上游已写 final_reply → 直通」路径，保证拒绝语义不被误读。
    draft = (final_answer or "").strip()
    gov_draft = result.get("draft_reply")
    if isinstance(gov_draft, str) and gov_draft.strip():
        draft = gov_draft
    if draft:
        patch["draft_reply"] = draft
        if ("人工" in draft or "转人工" in draft) and result.get("action_result_json") is None:
            patch["escalation_reason"] = (result.get("policy_decision") or {}).get(
                "reason_human_readable"
            ) or ("当前条件暂不符线上自动办理，已引导用户转人工。")
    gov_final = result.get("final_reply")
    if isinstance(gov_final, str) and gov_final.strip():
        patch["final_reply"] = gov_final

    patch["draft_context"] = {"source": "task"}
    # 把子图 messages 写回外层状态（compliance 可从中提取）
    if result_msgs:
        patch["messages"] = result_msgs
    return patch


# =========================================================================
# 1.4 合规检查节点（规则合规 + LLM 包装，替代旧 llm_wrap_node）
# =========================================================================


async def compliance_check_node(state: AgentState, ctx: AgentNodeContext) -> dict[str, Any]:
    """统一合规检查 + LLM 包装节点：所有分支汇总到此。

    草稿来源：
      - draft_reply（simple_qa/knowledge_qa/handoff/policy_lookup 产出）
      - messages[-1].content（task 子图最终答复）
      - 若上游已写 final_reply（handoff 预设文案）→ 直通跳过 LLM

    规则合规校验（生成后）：
      1. 屏蔽 tenant_id 泄露
      2. 工单号格式校验
      3. 不出现编造订单号
      4. 写操作回复必须对应 policy_decision

    违规且无法自动修整 → 降级安全文案 + node_errors["compliance"]。
    """
    from app.core.logging import get_logger

    _log = get_logger("agent.nodes.compliance")

    # 1. 优先级最高：上游已写 final_reply（handoff 预设文案/异常兜底）→ 直通
    prefixed_final = state.get("final_reply")
    if isinstance(prefixed_final, str) and prefixed_final.strip():
        appended_msg = None
        try:
            appended_msg = await ctx.conversation_repo.append_message(
                ctx.service_actor,
                state["tenant_id"],
                state.get("thread_id", ""),
                ConversationMessageCreate(
                    role="agent",
                    content=prefixed_final,
                    metadata={"action_kind": state.get("action_kind"), "prefixed": True},
                ),
            )
        except Exception as exc:
            _log.warning("compliance.prefixed_append_failed", error_type=type(exc).__name__)
        final_patch: dict[str, Any] = {
            "final_reply": prefixed_final,
            "_stream_chunks": [],
        }
        if appended_msg is not None:
            final_patch["appended_messages"] = [appended_msg]
        return final_patch

    # 2. 获取草稿：draft_reply 优先，否则从 messages 末尾取
    draft_reply = state.get("draft_reply") or ""
    if not draft_reply:
        messages = state.get("messages") or []
        if messages:
            last_msg = messages[-1]
            content = getattr(last_msg, "content", "")
            if isinstance(content, str):
                draft_reply = content
            elif isinstance(content, list):
                parts = [str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content]
                draft_reply = "".join(parts)

    draft_context = state.get("draft_context") or {}
    tenant_id = state["tenant_id"]
    action_kind = state.get("action_kind")
    action_result = state.get("action_result_json") or {}
    policy_decision = state.get("policy_decision") or {}
    rag_hits = state.get("rag_hits") or []
    escalated = bool(state.get("escalated"))
    ticket_no = state.get("escalated_ticket_no") or action_result.get("ticket_no")

    # 3. 规则合规校验 + 修整
    # 空草稿（simple_qa 直答 / 上游节点未产出）跳过转换：不能把空草稿变成错误文案
    # 再交给 LLM 润色（会导致 LLM 把错误文案当草稿润色 + 幻觉工单号）。
    draft_was_empty = not (draft_reply or "").strip()
    if not draft_was_empty:
        draft_reply = _apply_compliance_rules(
            draft_reply,
            tenant_id=tenant_id,
            action_kind=action_kind,
            action_result=action_result,
            policy_decision=policy_decision,
            draft_context=draft_context,
        )

    # 4. LLM 包装（若 chat_model 可用）；空草稿 → LLM 直答用户消息（简单问答 -> llm）
    # 错误兜底草稿（draft_context.error 存在）跳过 LLM 润色：安全文案直接透传，
    # 避免 LLM 润色错误文案时幻觉出假工单号。
    is_error_draft = bool(draft_context.get("error"))
    if ctx.chat_model is not None and (draft_reply or draft_was_empty) and not is_error_draft:
        system_prompt = _build_compliance_prompt(
            draft_context=draft_context,
            action_kind=action_kind,
            rag_hits=rag_hits,
            policy_decision=policy_decision,
            escalated=escalated,
            tenant_policy=ctx.effective_policy,
        )
        try:
            from langchain_core.messages import (
                AIMessage,
                AIMessageChunk,
                HumanMessage,
                SystemMessage,
            )

            from app.infrastructure.llm.providers import _format_extra_context

            reply_buf: list[str] = []

            history: list[dict[str, str]] = []
            try:
                msg_list = await ctx.conversation_repo.list_messages(
                    ctx.service_actor,
                    tenant_id,
                    state.get("thread_id", ""),
                    limit=40,
                )
                for m in msg_list:
                    if m.role == "tool":
                        continue
                    if m.role in ("human", "user"):
                        history.append({"role": "user", "content": m.content})
                    elif m.role in ("agent", "ai", "assistant"):
                        history.append({"role": "assistant", "content": m.content})
                if history and history[-1].get("role") == "user":
                    history.pop()
            except Exception as exc:
                _log.warning("compliance.load_history_non_fatal", error_type=type(exc).__name__)
                history = []

            extra_context: dict[str, Any] = {
                "action_kind": action_kind,
                "ticket_no": ticket_no,
                "policy_decision": policy_decision,
                "action_result": action_result,
                "order_detail": state.get("order_detail_json"),
                "rag_hits": rag_hits,
                "escalated": escalated,
            }
            # 空草稿（直答）不放 draft_reply：避免 LLM 把兜底错误文案当草稿润色
            if not draft_was_empty:
                extra_context["draft_reply"] = draft_reply

            # 拼 BaseMessage 列表（语义与旧 provider.astream 的输入构造逐字一致）
            lc_messages: list[Any] = [SystemMessage(content=system_prompt)]
            for h in history:
                if h["role"] == "user":
                    lc_messages.append(HumanMessage(content=h["content"]))
                else:
                    lc_messages.append(AIMessage(content=h["content"]))
            user_parts = [state.get("user_message", "") or ""]
            extra_str = _format_extra_context(extra_context)
            if extra_str:
                user_parts.append(f"\n\n---\n【参考上下文】\n{extra_str}")
            lc_messages.append(HumanMessage(content="\n".join(user_parts)))

            chat_model = ctx.chat_model
            async for item in chat_model.astream(lc_messages):
                chunk: str = ""
                if isinstance(item, AIMessageChunk):
                    if isinstance(item.content, str):
                        chunk = item.content
                    elif isinstance(item.content, list):
                        for part in item.content:
                            if isinstance(part, dict) and "text" in part:
                                chunk += str(part["text"])
                elif isinstance(item, str):
                    chunk = item
                if not chunk:
                    continue
                reply_buf.append(chunk)

            if reply_buf:
                draft_reply = "".join(reply_buf)
                # LLM 直答/润色结果同样要过合规规则（tenant_id 泄露屏蔽、工单号一致性）
                draft_reply = _apply_compliance_rules(
                    draft_reply,
                    tenant_id=tenant_id,
                    action_kind=action_kind,
                    action_result=action_result,
                    policy_decision=policy_decision,
                    draft_context=draft_context,
                )
        except Exception as exc:
            _log.exception("compliance.llm_wrap_failed", error_type=type(exc).__name__)

    # LLM 不可用/失败/返回空 且草稿仍为空 → 安全文案兜底
    if not (draft_reply or "").strip():
        draft_reply = "抱歉，处理出现问题，请稍后重试或回复「人工」转人工。"

    # 5. 写 DB
    appended_msg = None
    try:
        appended_msg = await ctx.conversation_repo.append_message(
            ctx.service_actor,
            tenant_id,
            state["thread_id"],
            ConversationMessageCreate(
                role="agent",
                content=draft_reply,
                metadata={
                    "action_kind": action_kind,
                    "ticket_no": ticket_no,
                    "policy_reason_code": policy_decision.get("reason_code"),
                },
            ),
        )
    except Exception as exc:
        _log.warning("compliance.append_failed_non_fatal", error_type=type(exc).__name__)

    final_patch = {
        "final_reply": draft_reply,
        "_stream_chunks": [],
    }
    if appended_msg is not None:
        final_patch["appended_messages"] = [appended_msg]
    return final_patch


def _apply_compliance_rules(
    draft: str,
    *,
    tenant_id: str,
    action_kind: str | None,
    action_result: dict[str, Any],
    policy_decision: dict[str, Any],
    draft_context: dict[str, Any],
) -> str:
    """规则合规校验 + 自动修整（纯函数，无 IO）。

    1. 屏蔽 tenant_id 泄露（替换为 ***）
    2. 工单号格式校验（若 action_result 有 ticket_no，draft 必须包含它）
    3. 写操作无 policy_decision 不得宣称成功
    违规且无法修整 → 降级安全文案。
    """
    if not draft:
        return "抱歉，处理出现问题，请稍后重试或回复「人工」转人工。"

    # 1. 屏蔽 tenant_id 泄露
    if tenant_id and tenant_id in draft:
        draft = draft.replace(tenant_id, "***")

    # 2. 工单号一致性校验
    ticket_no = action_result.get("ticket_no") if isinstance(action_result, dict) else None
    if ticket_no and ticket_no not in draft and action_kind in {"refund", "exchange", "repair", "cancel", "handoff"}:
        draft = f"{draft}\n工单号：{ticket_no}"

    # 2b. 反向校验：知识问答（action_kind=None）上下文中不存在真实工单，
    #     小模型包装时可能幻觉编造工单号 → 规则级剥离含「工单」的句子
    if action_kind is None and not ticket_no and "工单" in draft:
        import re as _re

        sentences = [s for s in _re.split(r"(?<=[。！？!?\n])", draft) if "工单" not in s]
        stripped = "".join(sentences).strip()
        if stripped:
            draft = stripped

    # 3. 写操作必须对应 policy_decision
    if action_kind in {"refund", "exchange", "repair"}:
        can_key = f"can_{action_kind}"
        if not policy_decision.get(can_key) and "成功" in draft:
            draft = "抱歉，当前条件不满足售后政策，暂无法办理。如需进一步协助，请回复「人工」。"

    return draft


def _build_compliance_prompt(
    *,
    draft_context: dict[str, Any],
    action_kind: str | None,
    rag_hits: list[dict[str, Any]],
    policy_decision: dict[str, Any],
    escalated: bool,
    tenant_policy: TenantPolicy | None,
) -> str:
    """按 draft_context.source 拼 compliance 节点的 system prompt。"""
    source = draft_context.get("source", "unknown")
    brand_name = tenant_policy.brand_name if tenant_policy else "手串售后"
    slogan = tenant_policy.slogan if tenant_policy else ""

    prompt = _CUSTOMER_SERVICE_SYSTEM_PROMPT
    if brand_name:
        prompt = f"{prompt}\n当前服务品牌：{brand_name}"
        if slogan:
            prompt = f"{prompt}（{slogan}）"

    constraints: list[str] = []
    if source == "rag":
        constraints.append("【硬约束】回答必须遵循检索到的 RAG 内容，不得编造未检索到的信息。")
    elif source == "policy":
        constraints.append("【硬约束】回答必须基于政策结构化字段，不得编造政策外的权益；政策问答没有工单，禁止提及工单号或要求用户提供退货原因用于计费。")
    elif source == "task":
        constraints.append("【硬约束】回复必须包含工单号（如有）、政策判定原因，不得编造订单信息。")
    elif source == "handoff":
        constraints.append("【硬约束】已转人工，第一句说明原因 + 工单号，不再解答原问题。")

    if constraints:
        prompt = f"{prompt}\n" + "\n".join(constraints)

    return prompt


# =========================================================================
# 2. 辅助函数
# =========================================================================


def _safe_json_extract(text: str) -> dict[str, Any] | None:
    """从文本中提取第一个 JSON 对象（容错：可能前后有非 JSON 文本）。"""
    import re

    # 尝试直接 parse
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    # 提取第一个 {...} 块
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None

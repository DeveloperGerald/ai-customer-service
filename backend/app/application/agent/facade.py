"""Agent 服务门面（四分类意图重构版）：
- 对外唯一入口：CustomerServiceAgentFacade.invoke(actor, thread_id, user_message)
- 负责：节点函数 ctx 绑定 → 图构建 → 执行 → 聚合结果成 AgentRunResult。
- HITL：写工具 interrupt 后 facade.astream_events 检测暂停态 → yield confirmation_required；
        resume_stream(decision) 用 Command(resume=...) 恢复。
- 面试演示点：依赖注入（tool_registry/retriever/classifier/chat_model 可替换），
  保证离线单测、真实部署、演示环境三种模式下不改业务代码。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from functools import partial
from typing import Any

from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent.graph import (
    _build_langgraph_config,
    astream_unified,
    build_customer_service_graph,
)
from app.application.agent.nodes import (
    AgentNodeContext,
    compliance_check_node,
    handoff_node,
    intent_classify_node,
    intent_router,
    knowledge_router,
    policy_lookup_node,
    rag_retrieve_node,
    task_node,
)
from app.application.agent.task_agent import build_task_agent
from app.application.schemas.agent import AgentRunResult, AgentState
from app.application.schemas.identity import Role
from app.domain.constants.policies import TenantPolicy
from app.domain.repositories.conversation import ConversationRepository
from app.domain.repositories.identity import Actor, UserRepository
from app.domain.repositories.policy import PolicyConfigRepository
from app.infrastructure.llm.classifiers import (
    IntentClassifierProtocol,
    LLMIntentClassifier,
)
from app.infrastructure.llm.providers import BaseRetriever


def _fresh_turn_state(
    *,
    actor: Actor,
    tenant_id: str,
    thread_id: str,
    user_message: str,
) -> AgentState:
    """构建每轮对话的初始 state，显式清空上一轮 checkpointer 残留的输出字段。

    LangGraph checkpointer 按 thread_id 存取状态；同一 thread 连续多轮调用 ainvoke 时，
    新 input 与旧 checkpoint 做 merge——未显式覆写的字段会保留上一轮的值。
    若不清空 final_reply / draft_reply 等，compliance_check 会因 final_reply 非空而短路，
    返回上一轮的旧回复。

    注意：只清「输出型」字段（每轮重新产出），保留「上下文型」字段（跨轮积累）：
      保留：order_detail_json / tool_executions / rag_hits / policy_decision 等
      ——这些是上一轮工具调用/检索的结果，后续轮次 LLM 需要引用（如用户追问"都需要"）。
    """
    return {
        "actor": actor,
        "tenant_id": tenant_id,
        "thread_id": thread_id,
        "user_message": user_message,
        "appended_messages": [],
        # ---- 输出型字段：每轮必须清空，防止短路/串轮 ----
        "final_reply": "",
        "draft_reply": None,
        "draft_context": None,
        "_agent_final_answer_draft": None,
        "action_kind": None,
        "action_result_json": None,
        "escalated": False,
        "escalated_ticket_no": None,
        "escalation_reason": None,
        "node_errors": {},
        "_stream_chunks": [],
        # ---- 会被节点覆写的字段：清空防泄漏 ----
        "intent_candidate": None,
        "intent_hint": None,
        "order_ref_candidate": None,
        "policy_lookup_hit": None,
        "policy_answer": None,
        # ---- 上下文型字段：不清空，跨轮积累 ----
        # order_detail_json / tool_executions / rag_hits / policy_decision 保留
    }


class CustomerServiceAgentFacade:
    """售后智能客服对外门面（HTTP API / SSE 都用它）。"""

    def __init__(
        self,
        *,
        retriever: BaseRetriever | None,
        classifier: IntentClassifierProtocol | None = None,
        chat_model: BaseChatModel | None = None,
        rag_top_k: int = 4,
        rag_similarity_threshold: float = 0.5,
    ) -> None:
        self.retriever = retriever
        self.chat_model: BaseChatModel | None = chat_model
        self.rag_top_k = int(rag_top_k)
        self.rag_similarity_threshold = float(rag_similarity_threshold)
        # 仅使用 LLMIntentClassifier：未显式注入时，若有 chat_model 则自动构建；
        # 两者皆无时 classifier=None（节点层兜底为 unknown）。
        if classifier is not None:
            self.classifier: IntentClassifierProtocol | None = classifier
        elif chat_model is not None:
            self.classifier = LLMIntentClassifier(chat_model=chat_model)
        else:
            self.classifier = None
        # task 分支原生 create_agent 子图（create_agent + 治理中间件 + @dynamic_prompt）。
        # 工具无状态，身份经 context 注入 → 门面级单实例即可，无跨租户泄漏。
        self.task_agent = build_task_agent(chat_model) if chat_model is not None else None

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def invoke(
        self,
        *,
        actor: Actor,
        tenant_id: str,
        thread_id: str,
        user_message: str,
        session: AsyncSession,
        idempotency_salt: str = "",
        service_actor_override: Actor | None = None,
        effective_policy_override: TenantPolicy | None = None,
    ) -> AgentRunResult:
        """同步风格异步入口：执行一轮完整对话 → 返回 AgentRunResult。

        Args:
            actor: 当前请求发起方（JWT 解析出的 consumer/staff/admin）。
            tenant_id: 强制显式传入，不依赖 actor.tenant_id（多租户硬隔离兜底）。
            thread_id: T6 约定格式 `{tenant_id}:{uuid_hex}`；LangGraph checkpoint 直接用它。
            user_message: 用户本轮输入（纯文本；图片/附件不解析，直接转人工）。
            session: AsyncSession（事务由 HTTP 层控制提交）。
            idempotency_salt: 工具层写操作幂等 key 种子（SSE 重试可复用同一 salt）。
            service_actor_override: 测试/离线注入的内部 STAFF Actor；默认 None 则从 DB 查询/创建。
            effective_policy_override: 测试/离线注入的 TenantPolicy；默认 None 则从 DB 查询。
        """
        conversation_repo = ConversationRepository(session)
        if service_actor_override is not None:
            service_actor = service_actor_override
        else:
            user_repo = UserRepository(session)
            service_staff = await user_repo.get_or_create_system_staff(tenant_id)
            service_actor = Actor(
                actor_id=str(service_staff.user_id),
                tenant_id=tenant_id,
                role=Role.STAFF,
            )
        if effective_policy_override is not None:
            effective_policy = effective_policy_override
        else:
            policy_repo = PolicyConfigRepository(session)
            effective_policy = await policy_repo.get_effective_policy(tenant_id)
        ctx = AgentNodeContext(
            session=session,
            actor=actor,
            retriever=self.retriever,
            conversation_repo=conversation_repo,
            service_actor=service_actor,
            classifier=self.classifier,
            chat_model=self.chat_model,
            task_agent=self.task_agent,
            idempotency_salt=idempotency_salt,
            rag_top_k=self.rag_top_k,
            rag_similarity_threshold=self.rag_similarity_threshold,
            effective_policy=effective_policy,
        )

        # 先把用户 human 消息写入会话（追加写模型；即使后续节点失败也保留历史）
        await conversation_repo.append_message(
            actor,
            tenant_id,
            thread_id,
            _human_create(user_message),
        )

        # 绑定节点函数 ctx
        node_fns = _bind_node_contexts(ctx)

        # 构建 + 执行图（4 分类新拓扑 + checkpointer）
        graph = build_customer_service_graph(
            node_fns,
            intent_router=intent_router,
            knowledge_router=knowledge_router,
        )
        initial = _fresh_turn_state(
            actor=actor,
            tenant_id=tenant_id,
            thread_id=thread_id,
            user_message=user_message,
        )
        run_metadata = {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "actor_id": actor.actor_id,
            "actor_role": actor.role.value if hasattr(actor.role, "value") else str(actor.role),
            "user_message_preview": user_message[:120],
            "idempotency_salt": idempotency_salt or "",
            "graph_version": "v3-4category",
        }
        # 设置原生多流 Config（thread_id/metadata/LangSmith tracer）
        cfg = _build_langgraph_config(
            None,
            run_metadata=run_metadata,
            run_name="customer-service-agent",
            thread_id=thread_id,
        )
        result_state: dict[str, Any] = await graph.ainvoke(initial, cfg)

        decision_debug = _build_debug(result_state)
        return AgentRunResult(
            final_reply=str(result_state.get("final_reply") or "抱歉，暂无法处理。"),
            escalated=bool(result_state.get("escalated")),
            escalated_ticket_no=result_state.get("escalated_ticket_no"),
            escalation_reason=result_state.get("escalation_reason"),
            decision_debug=decision_debug,
        )

    async def astream_events(
        self,
        *,
        actor: Actor,
        tenant_id: str,
        thread_id: str,
        user_message: str,
        session: AsyncSession,
        idempotency_salt: str = "",
        service_actor_override: Actor | None = None,
        effective_policy_override: TenantPolicy | None = None,
    ):
        """真正节点级 + token 级流式事件。

        事件协议（API 层再翻译成 SSE 帧）：
          - {"type": "start", "thread_id": str, "user_input": str}
          - {"type": "node_start", "node": str}
          - {"type": "tool", "kind": str, "payload": dict}        (可选：policy_decision 结束时产出)
          - {"type": "reply_chunk", "text": str}                  (llm_wrap 逐 token)
          - {"type": "node_end",   "node": str, "patch": dict|None}
          - {"type": "reply", "text": str}                        (最终完整回复，兼容旧版前端)
          - {"type": "debug", "payload": dict}
          - {"type": "escalated", "ticket_no": str, "reason": str|None}
          - {"type": "done"}
        """
        conversation_repo = ConversationRepository(session)
        if service_actor_override is not None:
            service_actor = service_actor_override
        else:
            user_repo = UserRepository(session)
            service_staff = await user_repo.get_or_create_system_staff(tenant_id)
            service_actor = Actor(
                actor_id=str(service_staff.user_id),
                tenant_id=tenant_id,
                role=Role.STAFF,
            )
        if effective_policy_override is not None:
            effective_policy = effective_policy_override
        else:
            policy_repo = PolicyConfigRepository(session)
            effective_policy = await policy_repo.get_effective_policy(tenant_id)
        ctx = AgentNodeContext(
            session=session,
            actor=actor,
            retriever=self.retriever,
            conversation_repo=conversation_repo,
            service_actor=service_actor,
            classifier=self.classifier,
            chat_model=self.chat_model,
            task_agent=self.task_agent,
            idempotency_salt=idempotency_salt,
            rag_top_k=self.rag_top_k,
            rag_similarity_threshold=self.rag_similarity_threshold,
            effective_policy=effective_policy,
        )

        # 先把用户消息写入会话（即使后续节点失败也保留历史）。
        # 失败不中断（DB 没起/thread 尚未创建等），后续流式链路照常产出 reply/reply_chunk
        try:
            await conversation_repo.append_message(
                actor,
                tenant_id,
                thread_id,
                _human_create(user_message),
            )
        except Exception as exc:
            from app.core.logging import get_logger as _get_logger

            _get_logger("agent.facade.stream").warning(
                "append_user_msg_failed_non_fatal",
                error_type=type(exc).__name__,
                error=str(exc),
                tenant_id=tenant_id,
                thread_id=thread_id,
            )

        node_fns = _bind_node_contexts(ctx)
        graph = build_customer_service_graph(
            node_fns,
            intent_router=intent_router,
            knowledge_router=knowledge_router,
        )
        initial = _fresh_turn_state(
            actor=actor,
            tenant_id=tenant_id,
            thread_id=thread_id,
            user_message=user_message,
        )
        run_metadata = {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "actor_id": actor.actor_id,
            "actor_role": actor.role.value if hasattr(actor.role, "value") else str(actor.role),
            "user_message_preview": user_message[:120],
            "idempotency_salt": idempotency_salt or "",
            "graph_version": "v3-4category",
        }

        yield {"type": "start", "thread_id": thread_id, "user_input": user_message}

        final_state: dict[str, Any] = dict(initial)
        # reply_chunk 本地累计双保险：即便是 final_state.patch 因 LangGraph 版本差异丢了
        # final_reply，也能用 token 级实时拼起来的完整文本兜底（前端看到打字机就一定能看到
        # 最终完整回复，不会被 9 字 "抱歉" 覆盖）。
        _acc_chunks: list[str] = []
        import time as _t

        from app.core.logging import get_logger as _fac_logger

        _fac_log = _fac_logger("agent.facade.astream")
        _t0_graph = _t.perf_counter()

        _evt_count = 0
        _first_evt_meta: tuple[str, int] | None = None
        _last_evt_type = None
        try:
            _fac_log.info(
                "graph.astream_events.about_to_call",
                tenant_id=tenant_id,
                thread_id=thread_id,
                initial_keys=sorted(initial.keys()),
            )
            cfg = _build_langgraph_config(
                None,
                run_metadata=run_metadata,
                run_name="customer-service-agent",
                thread_id=thread_id,
            )
            graph_it: AsyncIterator[dict[str, Any]] = astream_unified(graph, initial, cfg)
            _fac_log.info(
                "graph.astream_events.iterator_created",
                tenant_id=tenant_id,
                thread_id=thread_id,
                iterator_type=str(type(graph_it).__name__),
                ms_after_create=int((_t.perf_counter() - _t0_graph) * 1000),
            )
            async for evt in graph_it:
                _evt_count += 1
                if _evt_count == 1:
                    _first_evt_meta = (
                        str(evt.get("type") or "?"),
                        int((_t.perf_counter() - _t0_graph) * 1000),
                    )
                    _fac_log.info(
                        "graph.astream_events.first_event",
                        tenant_id=tenant_id,
                        thread_id=thread_id,
                        first_event_type=evt.get("type"),
                        first_event_keys=sorted(evt.keys()),
                        first_event_preview=str(evt)[:300],
                        ms_after_start=_first_evt_meta[1],
                    )
                if _evt_count % 250 == 0:
                    _fac_log.info(
                        "graph.astream_events.heartbeat",
                        tenant_id=tenant_id,
                        thread_id=thread_id,
                        event_count=_evt_count,
                        last_seen_type=_last_evt_type,
                    )
                etype = evt.get("type")
                _last_evt_type = etype
                if etype == "node_start":
                    yield {"type": "node_start", "node": evt.get("node", "")}
                elif etype == "node_end":
                    patch = evt.get("patch")
                    if patch:
                        final_state.update(patch)
                    node = evt.get("node", "")
                    if node == "policy_decision" and patch and patch.get("policy_decision"):
                        yield {
                            "type": "tool",
                            "kind": "policy_decision",
                            "payload": patch.get("policy_decision"),
                        }
                    yield {"type": "node_end", "node": node, "patch": patch}
                elif etype == "token_chunk":
                    txt = evt.get("text", "") or ""
                    if txt:
                        _acc_chunks.append(txt)
                    yield {"type": "reply_chunk", "text": txt}
                else:
                    yield evt
            _fac_log.info(
                "graph.astream_events.done",
                tenant_id=tenant_id,
                thread_id=thread_id,
                total_event_count=_evt_count,
                ms_total=int((_t.perf_counter() - _t0_graph) * 1000),
                final_state_has_final_reply=isinstance(final_state.get("final_reply"), str)
                and len(final_state.get("final_reply") or "") > 0,
                final_reply_preview=str(final_state.get("final_reply") or "")[:200],
                acc_chunk_count=len(_acc_chunks),
                acc_chunk_total_chars=sum(len(x) for x in _acc_chunks),
            )
        except Exception as exc:
            _fac_log.exception(
                "graph.astream_events.failed",
                tenant_id=tenant_id,
                thread_id=thread_id,
                event_count_so_far=_evt_count,
                last_event_type=_last_evt_type,
                first_event=_first_evt_meta,
                error_type=type(exc).__name__,
                error=str(exc),
                ms_total=int((_t.perf_counter() - _t0_graph) * 1000),
            )
            raise

        # HITL 检测：调用 graph.aget_state(config) 看是否暂停在写工具 interrupt
        pending = await _detect_interrupt_pending(graph, thread_id, run_metadata)
        if pending is not None:
            # 暂停在写工具 interrupt：yield confirmation_required，不结束本轮（不 yield reply/done）
            yield {
                "type": "confirmation_required",
                "thread_id": thread_id,
                "pending_action": pending,
            }
            return

        # 转人工卡片事件
        if final_state.get("escalated"):
            yield {
                "type": "escalated",
                "ticket_no": final_state.get("escalated_ticket_no"),
                "reason": final_state.get("escalation_reason"),
            }

        # 兼容旧前端：一条完整 reply（final_state.patch + 本地累计双保险选优）
        raw_final: str = str(final_state.get("final_reply") or "")
        accumulated_text = "".join(_acc_chunks)
        # 选择策略：
        #   长度合理 (> 15) 且不是兜底模板 → 优先选 final_state.final_reply
        #   否则若本地累计更优（非空/更长）→ 用 accumulated_text
        #   两者都空 → 才走抱歉 9 字兜底
        _fallback_text = "抱歉，暂无法处理。"
        is_raw_fallback = (not raw_final) or raw_final.strip() == _fallback_text
        if is_raw_fallback and accumulated_text.strip():
            final_reply = accumulated_text
            final_state["final_reply"] = final_reply
        elif raw_final.strip():
            final_reply = raw_final
        elif accumulated_text.strip():
            final_reply = accumulated_text
            final_state["final_reply"] = final_reply
        else:
            final_reply = _fallback_text
            final_state["final_reply"] = final_reply
        yield {"type": "reply", "text": final_reply}

        # debug 面板
        debug_payload = _build_debug(final_state)
        if debug_payload:
            yield {"type": "debug", "payload": debug_payload}

        yield {"type": "done"}

    async def resume_stream(
        self,
        *,
        actor: Actor,
        tenant_id: str,
        thread_id: str,
        session: AsyncSession,
        decision: dict[str, Any],
        service_actor_override: Actor | None = None,
        effective_policy_override: TenantPolicy | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """HITL resume：用 Command(resume=decision) 恢复暂停的图。

        与 astream_events 同协议产出续跑事件：reply_chunk / node_start / node_end / reply / done。
        若 get_state 显示无 pending（已超时 / checkpoint 过期）→ yield 错误事件 + done。
        """
        from langgraph.types import Command

        conversation_repo = ConversationRepository(session)
        if service_actor_override is not None:
            service_actor = service_actor_override
        else:
            user_repo = UserRepository(session)
            service_staff = await user_repo.get_or_create_system_staff(tenant_id)
            service_actor = Actor(
                actor_id=str(service_staff.user_id),
                tenant_id=tenant_id,
                role=Role.STAFF,
            )
        if effective_policy_override is not None:
            effective_policy = effective_policy_override
        else:
            policy_repo = PolicyConfigRepository(session)
            effective_policy = await policy_repo.get_effective_policy(tenant_id)
        ctx = AgentNodeContext(
            session=session,
            actor=actor,
            retriever=self.retriever,
            conversation_repo=conversation_repo,
            service_actor=service_actor,
            classifier=self.classifier,
            chat_model=self.chat_model,
            task_agent=self.task_agent,
            rag_top_k=self.rag_top_k,
            rag_similarity_threshold=self.rag_similarity_threshold,
            effective_policy=effective_policy,
        )

        node_fns = _bind_node_contexts(ctx)
        graph = build_customer_service_graph(
            node_fns,
            intent_router=intent_router,
            knowledge_router=knowledge_router,
        )
        run_metadata = {
            "tenant_id": tenant_id,
            "thread_id": thread_id,
            "actor_id": actor.actor_id,
            "graph_version": "v3-4category",
            "resume": True,
        }

        # 先校验是否仍处于暂停态（超时/已 resume → 不允许再次 resume）
        pending = await _detect_interrupt_pending(graph, thread_id, run_metadata)
        if pending is None:
            yield {
                "type": "error",
                "message": "操作已超时或已被处理，请重新发起。",
                "thread_id": thread_id,
            }
            yield {"type": "done"}
            return

        yield {"type": "resume_started", "thread_id": thread_id, "decision": decision}

        final_state: dict[str, Any] = {}
        _acc_chunks: list[str] = []
        cfg = _build_langgraph_config(
            None,
            run_metadata=run_metadata,
            run_name="customer-service-agent-resume",
            thread_id=thread_id,
        )
        command = Command(resume=decision)
        graph_it: AsyncIterator[dict[str, Any]] = astream_unified(graph, command, cfg)
        async for evt in graph_it:
            etype = evt.get("type")
            if etype == "node_start":
                yield {"type": "node_start", "node": evt.get("node", "")}
            elif etype == "node_end":
                patch = evt.get("patch")
                if patch:
                    final_state.update(patch)
                yield {"type": "node_end", "node": evt.get("node", ""), "patch": patch}
            elif etype == "token_chunk":
                txt = evt.get("text", "") or ""
                if txt:
                    _acc_chunks.append(txt)
                yield {"type": "reply_chunk", "text": txt}
            else:
                yield evt

        # 转人工卡片事件（resume 后也可能产生）
        if final_state.get("escalated"):
            yield {
                "type": "escalated",
                "ticket_no": final_state.get("escalated_ticket_no"),
                "reason": final_state.get("escalation_reason"),
            }

        raw_final: str = str(final_state.get("final_reply") or "")
        accumulated_text = "".join(_acc_chunks)
        _fallback_text = "抱歉，暂无法处理。"
        if raw_final.strip():
            final_reply = raw_final
        elif accumulated_text.strip():
            final_reply = accumulated_text
            final_state["final_reply"] = final_reply
        else:
            final_reply = _fallback_text
            final_state["final_reply"] = final_reply
        yield {"type": "reply", "text": final_reply}

        debug_payload = _build_debug(final_state)
        if debug_payload:
            yield {"type": "debug", "payload": debug_payload}

        yield {"type": "done"}


# =========================================================================
# 内部帮助
# =========================================================================


def _bind_node_contexts(
    ctx: AgentNodeContext,
) -> dict[str, Any]:
    """把 AgentNodeContext 绑定到每个节点函数（partial 包装），
    返回 {node_name: async_fn(state)->patch} 字典供 StateGraph 使用。

    4 分类新拓扑节点集：intent_classify / policy_lookup / rag_retrieve / task / handoff / compliance_check。
    node_start / node_end 事件由 graph.astream_unified 的原生 debug 流产出，无需旁路。

    异常兜底（FR-5 重构要求 5）：
      - GraphBubbleUp/GraphInterrupt（写工具 HITL 暂停）→ 必须放行向上冒泡，让 LangGraph checkpointer 落 Redis；
      - 其他 Exception → 捕获，写 node_errors[node] + 返回安全草稿 draft_reply，
        保证异常分支仍汇入 compliance_check 而非 500。
    """
    from functools import wraps

    from langgraph.errors import GraphBubbleUp

    def _trace(node_name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        async def _w(state: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            try:
                return await fn(state, **_kw)
            except GraphBubbleUp:
                # HITL 暂停信号，必须放行让外层图 checkpointer 落盘
                raise
            except Exception as exc:
                # 其他异常：写 node_errors + 返回安全草稿，让 compliance_check 兜底
                from app.core.logging import get_logger as _get_logger

                _get_logger("agent.facade.node").warning(
                    "node.failed_non_fatal",
                    node=node_name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                return {
                    "node_errors": {
                        node_name: {"type": type(exc).__name__, "message": str(exc)}
                    },
                    "draft_reply": "处理出现问题，请稍后重试或回复「人工」转人工。",
                    # error 标记：compliance_check 对错误兜底草稿跳过 LLM 润色直接透传，
                    # 避免 LLM 把「处理出现问题」包装成「申请已收到/工单号为XXX」之类的幻觉文案
                    "draft_context": {"source": "error", "node": node_name, "error": True},
                }

        return _w

    raw = {
        "intent_classify": partial(intent_classify_node, ctx=ctx),
        "policy_lookup": partial(policy_lookup_node, ctx=ctx),
        "rag_retrieve": partial(rag_retrieve_node, ctx=ctx),
        "task": partial(task_node, ctx=ctx),
        "handoff": partial(handoff_node, ctx=ctx),
        "compliance_check": partial(compliance_check_node, ctx=ctx),
    }
    return {name: _trace(name, fn) for name, fn in raw.items()}


async def _detect_interrupt_pending(
    graph: Any,
    thread_id: str,
    run_metadata: dict[str, Any],
) -> dict[str, Any] | None:
    """检测图是否暂停在写工具 interrupt，返回 pending payload（或 None）。

    真 LangGraph：调 graph.aget_state(config) → StateSnapshot，若 .next 非空，
    从 .tasks[*].interrupts[*].value 取 pending payload。
    Minimal 路径：返回 None（不支持 HITL）。
    """
    try:
        config = _build_langgraph_config(
            None, run_metadata=run_metadata, thread_id=thread_id
        )
        snap = await graph.aget_state(config)
        if snap is None:
            return None
        # StateSnapshot.next 非空 = 暂停态
        next_nodes = getattr(snap, "next", None)
        if not next_nodes:
            return None
        # 从 tasks[*].interrupts[*].value 取 pending payload
        tasks = getattr(snap, "tasks", None) or []
        for task in tasks:
            interrupts = getattr(task, "interrupts", None) or []
            for intr in interrupts:
                value = getattr(intr, "value", None)
                if isinstance(value, dict):
                    return value
        # next 非空但没找到 interrupts value：返回最小标记
        return {
            "tool": "unknown",
            "action_label": "未知操作",
            "summary": "图已暂停，请确认是否继续执行。",
        }
    except Exception:  # pragma: no cover - get_state 失败兜底
        return None


def _build_debug(state: dict[str, Any]) -> dict[str, Any] | None:
    """组装面试展示用 debug 面板信息（前端点击「查看决策依据」弹出）。

    注意：
      (a) 前端 debug 面板读取 intent/action 两个顶层键；
      (b) 为兼容旧测试（断言 intent 是 7 标签 refund/exchange/repair/faq...），
          当 intent_candidate 为 4 大类时，优先用 intent_hint（具体售后子类型）
          回填到 intent，保持旧语义不变。
    """
    keys_of_interest = (
        "intent_candidate",
        "intent_hint",
        "order_ref_candidate",
        "policy_lookup_hit",
        "policy_answer",
        "rag_hits",
        "order_detail_json",
        "policy_decision",
        "action_kind",
        "action_result_json",
        "tool_executions",
        "node_errors",
    )
    debug: dict[str, Any] = {k: state.get(k) for k in keys_of_interest if state.get(k) is not None}
    # 兼容层：4 大类 intent_candidate → 旧 7 标签语义（用于测试断言和前端面板友好展示）
    candidate = debug.get("intent_candidate")
    hint = debug.get("intent_hint")
    legacy_intent: str | None = None
    if candidate == "task" and isinstance(hint, str):
        # task + hint=refund/exchange/repair/cancel/order_status/product → 旧 7 标签
        legacy_intent = hint if hint in {"refund", "exchange", "repair", "cancel"} else None
    elif candidate == "knowledge_qa":
        legacy_intent = "faq"
    elif candidate == "simple_qa":
        legacy_intent = "smalltalk"
    elif candidate == "handoff":
        legacy_intent = "handoff"
    if legacy_intent is not None:
        debug["intent_legacy"] = legacy_intent
        # 把「intent_candidate」也回填成旧标签（兼容直接断言 intent_candidate 的旧用例）
        debug["intent_candidate"] = legacy_intent
    # 面板友好别名：显式设置 intent / action
    if "intent" not in debug:
        debug["intent"] = debug.get("intent_candidate")
    if "action_kind" in debug and "action" not in debug:
        debug["action"] = debug["action_kind"]
    # 规范化 policy_decision 显示：把 debug 里可读字段提到顶层，方便前端直接渲染
    policy = debug.get("policy_decision")
    if isinstance(policy, dict):
        inner_debug = policy.get("debug") if isinstance(policy.get("debug"), dict) else {}
        if inner_debug and "policy_debug" not in debug:
            debug["policy_debug"] = inner_debug
        for flat_key in ("reason_code", "reason_human_readable", "can_refund", "can_exchange", "can_repair"):
            if flat_key in policy and f"policy_{flat_key}" not in debug:
                debug[f"policy_{flat_key}"] = policy[flat_key]
    return debug or None


def _human_create(content: str):
    """延迟导入 ConversationMessageCreate，避免循环 import（facade → schemas → ）。"""
    from app.application.schemas.conversation import ConversationMessageCreate

    return ConversationMessageCreate(role="human", content=content)

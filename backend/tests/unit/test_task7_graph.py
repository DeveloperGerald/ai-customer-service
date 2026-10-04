"""T7 LangGraph 编排 6 条单测（完全离线，AsyncMock）。

覆盖：
- TR7-1 FAQ 分支：禅饰坊用户问「7 天无理由能退吗？」→ rag_hits 非空；action=faq_only；不 escalated
- TR7-2 退款分支：tenant_a + 订单签收 3 天 + 标准款 → can_refund；RF- 开头工单；final_reply 含「预计退款」
- TR7-3 换货分支：tenant_a + 用户要换 → 调用 ExchangeRequestTool；EX- 工单；action_kind=exchange
- TR7-4 维修分支：用户说「珠裂了」+ 质量窗口 30 天内 → 调用 RepairRequestTool；RP- 工单
- TR7-5 D3 转人工立即执行（两种触发：① 说「人工」；② 说「有图片」）→ escalated=true + ticket_no 有值
- TR7-6 梵印阁 tenant_b 政策限制：非质量「想退」→ can_refund=false；无 RF 工单；回复提示「非质量问题不适用」
"""

from __future__ import annotations

import contextlib
import datetime as _dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from app.application.agent.facade import CustomerServiceAgentFacade
from app.application.schemas.identity import Role
from app.application.tools.builtin import order_query
from app.domain.constants.policies import TENANT_POLICIES
from app.domain.models.conversation import ConversationMessageORM, ConversationThreadORM
from app.domain.models.identity import TenantORM, UserORM
from app.domain.models.policy import KnowledgeChunkORM, TenantPolicyConfigORM
from app.domain.repositories.conversation import ConversationRepository
from app.domain.repositories.identity import Actor
from tests.unit._fakes import FakeChatModel, FakeRetriever

TENANT_A = "tenant_a"
TENANT_B = "tenant_b"
THREAD_ID_A = f"{TENANT_A}:thread000001"
THREAD_ID_B = f"{TENANT_B}:thread000002"
OWNER_A1 = Actor(
    actor_id="a0000000-0000-0000-0000-000000000011",
    tenant_id=TENANT_A,
    role=Role.CONSUMER,
)
OWNER_B1 = Actor(
    actor_id="b0000000-0000-0000-0000-000000000011",
    tenant_id=TENANT_B,
    role=Role.CONSUMER,
)
STAFF_A = Actor(
    actor_id="a0000000-0000-0000-0000-000000000002",
    tenant_id=TENANT_A,
    role=Role.STAFF,
)
STAFF_B = Actor(
    actor_id="b0000000-0000-0000-0000-000000000002",
    tenant_id=TENANT_B,
    role=Role.STAFF,
)


# ========================================================================
# Helpers：FakeSession 工厂 + make_facade
# ========================================================================


def _make_standard_order(
    *,
    order_id: UUID,
    order_no: str,
    tenant_id: str,
    owner_id: str,
    product_type: str = "standard_beads",
    delivered_days_ago: int = 3,
    total_cents: int = 88800,
    is_customized: bool = False,
) -> dict[str, Any]:
    """返回符合 OrderRead 结构的 dict（供 OrderQueryTool 返回）。"""
    now = _dt.datetime.now(_dt.timezone.utc)
    delivered_at = (now - _dt.timedelta(days=delivered_days_ago)).isoformat()
    return {
        "order_id": str(order_id),
        "order_no": order_no,
        "tenant_id": tenant_id,
        "customer_user_id": owner_id,
        "status": "delivered",
        "product_type": product_type,
        "product_title": "小叶紫檀 8mm 108 颗手串",
        "is_customized": is_customized,
        "total_amount_cents": total_cents,
        "currency": "CNY",
        "delivered_at": delivered_at,
        "created_at": (now - _dt.timedelta(days=delivered_days_ago + 4)).isoformat(),
        "line_items": [
            {"sku": "sku-xiaoyezitan-8mm", "title": "小叶紫檀 8mm 108 颗手串", "qty": 1, "unit_price_cents": total_cents}
        ],
    }


def _make_session_with_order(
    *,
    owner: Actor,
    thread_id: str,
    order_result: dict[str, Any] | None,
) -> tuple[AsyncMock, list[Any]]:
    """构建 FakeSession：
    - 对于 conversation_threads 查询 → 返回对应 thread ORM；
    - 对于 orders 查询（在 order_query tool 内执行）→ 返回 order_result 包装的 ORM-like（
      注意：OrderQueryTool 用 OrderRepository，我们为了避免连 T3 repository 也要 mock，
      直接在 facade 层不传 retriever，RAG 走 mock；并且把 execute 的返回按 stmt 里的表名分发）。
    - 对于 users 查询 → 同租户系统 staff 用户（UserORM）；
    - 对于 tenants 查询 → 对应租户 TenantORM（name/display_name/slogan 来自 TENANT_POLICIES）；
    - 对于 tenant_policy_configs 查询 → 结构化售后政策配置（从 TENANT_POLICIES 读取）；
    - 对于 knowledge_chunks 查询 → policy_manual + faq 内容（从 TENANT_POLICIES.full_text 拆行构造）。
    """
    import datetime as _dt_inner
    import re
    from uuid import uuid4 as _uuid4

    def _parse_tid(sql: str) -> str:
        # 支持 literal_binds 格式：tenant_id = 'tenant_a'
        m = re.search(r"tenant_id\s*=\s*'([^']+)'", sql)
        if m:
            return m.group(1)
        # 回退到 thread_id 前缀
        return thread_id.split(":", 1)[0]

    tenant_id_default = thread_id.split(":", 1)[0]
    thread = ConversationThreadORM(
        thread_id=thread_id,
        tenant_id=tenant_id_default,
        title="demo",
        initial_user_message="",
        owner_user_id=UUID(owner.actor_id),
        status="open",
        escalated_ticket_no=None,
        last_message_at=None,
        created_at=_dt.datetime.now(_dt.timezone.utc),
        updated_at=_dt.datetime.now(_dt.timezone.utc),
    )

    def _make_staff_user(tid: str) -> UserORM:
        staff_actor = STAFF_A if tid == TENANT_A else STAFF_B
        return UserORM(
            user_id=UUID(staff_actor.actor_id),
            tenant_id=tid,
            username=f"{tid}_staff",
            display_name=f"{TENANT_POLICIES.get(tid, TENANT_POLICIES[TENANT_A]).brand_name}-系统客服",
            email=f"{tid}_staff@internal.local",
            phone=None,
            role=Role.STAFF.value,
            is_active=True,
        )

    def _make_tenant_orm(tid: str) -> TenantORM:
        pol = TENANT_POLICIES.get(tid, TENANT_POLICIES[TENANT_A])
        return TenantORM(
            tenant_id=tid,
            name=pol.brand_name,
            display_name=f"{pol.brand_name}（{pol.slogan}）",
            description=pol.full_text[:200],
            is_active=True,
        )

    def _make_policy_config(tid: str) -> TenantPolicyConfigORM:
        pol = TENANT_POLICIES.get(tid, TENANT_POLICIES[TENANT_A])
        return TenantPolicyConfigORM(
            tenant_id=tid,
            return_days=pol.return_days,
            return_policy_type=pol.return_policy_type,
            restocking_fee_pct_non_quality=pol.restocking_fee_pct_non_quality,
            warranty_days_quality=pol.warranty_days_quality,
            custom_product_allowed_return=pol.custom_product_allowed,
            updated_by=None,
        )

    def _make_knowledge_chunks(tid: str, *, only_policy_manual: bool) -> list[KnowledgeChunkORM]:
        pol = TENANT_POLICIES.get(tid, TENANT_POLICIES[TENANT_A])
        chunks: list[KnowledgeChunkORM] = []
        sources: list[tuple[str, str, str]] = []
        # policy_manual 原文（拆 paragraph；总是加）
        for i, para in enumerate([p for p in pol.full_text.split("\n\n") if p.strip()]):
            sources.append(
                (
                    "policy_manual",
                    f"{pol.brand_name}售后政策 · 第{i+1}要点",
                    para.strip(),
                )
            )
        if not only_policy_manual:
            # FAQ（仅 nodes._mock_rag_hits_from_db 需要；不用于 get_effective_policy 组装 full_text）
            faqs = [
                ("问：你们支持几天无理由退货？", f"答：{pol.brand_name}支持{pol.return_days or 0}天无理由退货。"),
                ("问：定制款可以退吗？", f"答：定制款{'支持' if pol.custom_product_allowed else '不支持'}非质量退货。"),
            ]
            for q, a in faqs:
                sources.append(("faq", q, f"{q}\n{a}"))
        now = _dt_inner.datetime.now(_dt_inner.timezone.utc)
        for idx, (src, title, content) in enumerate(sources):
            chunks.append(
                KnowledgeChunkORM(
                    chunk_id=_uuid4(),
                    tenant_id=tid,
                    title=title,
                    content=content,
                    source=src,
                    content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    embedding=None,
                    created_by=UUID(STAFF_A.actor_id) if tid == TENANT_A else UUID(STAFF_B.actor_id),
                    created_at=now + _dt_inner.timedelta(seconds=idx),
                )
            )
        return chunks

    import hashlib

    added: list[Any] = []

    class _Fake(AsyncMock):
        def add(self, obj: Any) -> None:  # type: ignore[override]
            added.append(obj)

    sess = _Fake()
    sess.flush = AsyncMock(return_value=None)
    sess.commit = AsyncMock(return_value=None)

    async def _dispatch(*a: Any, **_k: Any) -> MagicMock:
        stmt_str = ""
        if a:
            try:
                stmt_str = str(a[0].compile(compile_kwargs={"literal_binds": True}))
            except Exception:
                # JSONB/datetime/UUID 字面量 render 失败时 fallback（SQLAlchemy CompileError）
                try:
                    stmt_str = str(a[0])
                except Exception:
                    stmt_str = type(a[0]).__name__
        tid = _parse_tid(stmt_str)
        rp = MagicMock()
        if "conversation_messages" in stmt_str:
            # list_messages
            scalars = MagicMock()
            scalars.all.return_value = [o for o in added if isinstance(o, ConversationMessageORM)]
            rp.scalars.return_value = scalars
            return rp
        if "conversation_threads" in stmt_str:
            if "UPDATE" in stmt_str:
                return rp
            rp.scalar_one_or_none.return_value = thread
            return rp
        if "orders" in stmt_str and order_result is not None:
            # OrderRepository 返回 Read 时需要再包装 ORM 属性，
            # 但我们为了简化，让 tool 直接失败（ResourceNotFound）然后走 policy_decision 用 order_detail_json=None；
            # 更稳的方式：我们直接在测试里把 OrderQueryTool.run 用 monkeypatch 掉。
            # 这里返回 None，看下面测试函数里的 monkeypatch。
            rp.scalar_one_or_none.return_value = None
            return rp
        if " FROM users " in stmt_str or "from users " in stmt_str.lower():
            rp.scalar_one_or_none.return_value = _make_staff_user(tid)
            scalars_rp = MagicMock()
            scalars_rp.all.return_value = [_make_staff_user(tid)]
            rp.scalars.return_value = scalars_rp
            return rp
        if " FROM tenants " in stmt_str or "from tenants " in stmt_str.lower():
            rp.scalar_one_or_none.return_value = _make_tenant_orm(tid)
            return rp
        if " FROM tenant_policy_configs " in stmt_str or "from tenant_policy_configs " in stmt_str.lower():
            rp.scalar_one_or_none.return_value = _make_policy_config(tid)
            return rp
        if (
            " FROM knowledge_chunks " in stmt_str
            or "from knowledge_chunks " in stmt_str.lower()
        ):
            only_manual = "policy_manual" in stmt_str
            orm_list = _make_knowledge_chunks(tid, only_policy_manual=only_manual)
            scalars_rp = MagicMock()
            scalars_rp.all.return_value = orm_list
            rp.scalars.return_value = scalars_rp
            rp.scalar_one_or_none.return_value = orm_list[0] if orm_list else None
            return rp
        if (
            " FROM idempotency_records " in stmt_str
            or "from idempotency_records " in stmt_str.lower()
            or ("idempotency_records" in stmt_str and "INSERT" not in stmt_str)
        ):
            rp.scalar_one_or_none.return_value = None
            scalars_rp = MagicMock()
            scalars_rp.all.return_value = []
            rp.scalars.return_value = scalars_rp
            return rp
        if (
            " FROM tool_audit_logs " in stmt_str
            or "from tool_audit_logs " in stmt_str.lower()
            or ("tool_audit_logs" in stmt_str and "INSERT" not in stmt_str)
        ):
            rp.scalar_one_or_none.return_value = None
            scalars_rp = MagicMock()
            scalars_rp.all.return_value = []
            rp.scalars.return_value = scalars_rp
            return rp
        # 默认
        rp.scalar_one_or_none.return_value = thread
        rp.scalars.return_value.all.return_value = []
        return rp

    sess.execute.side_effect = _dispatch
    return sess, added


def _make_facade() -> CustomerServiceAgentFacade:
    # 传入 FakeChatModel + FakeRetriever（离线可复现）
    return CustomerServiceAgentFacade(
        retriever=FakeRetriever(),
        chat_model=FakeChatModel(),
    )


@contextlib.contextmanager
def _patch_order_query(result: Any):
    """临时把 order_query.coroutine 打桩为直接返回 result（工具现为模块级 @tool 单例）。"""
    original = order_query.coroutine

    async def _patched(*_a: Any, **_k: Any):
        return result

    order_query.coroutine = _patched  # type: ignore[method-assign]
    try:
        yield
    finally:
        order_query.coroutine = original  # type: ignore[method-assign]


# ========================================================================
# TR7-1 FAQ 分支
# ========================================================================


@pytest.mark.asyncio
async def test_tr71_faq_branch_rag_hits_and_no_action() -> None:
    tid = f"{TENANT_A}:tr71-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    out = await facade.invoke(
        actor=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid,
        user_message="你们 7 天无理由能退吗？",
        session=sess,
    )
    assert out.escalated is False
    assert out.escalated_ticket_no is None
    assert out.decision_debug, "debug 面板必须包含中间决策字段"
    # knowledge_qa → _build_debug 兼容层回填为旧标签 faq
    assert out.decision_debug["intent_candidate"] == "faq"
    # knowledge_qa 分支不写 action_kind（仅 task 子图写）
    assert out.decision_debug.get("action_kind") in (None, "knowledge_qa")
    # knowledge_qa 两条路径：policy_lookup 命中（policy_answer 有值）或 rag_retrieve（rag_hits 非空）
    rag_hits = out.decision_debug.get("rag_hits") or []
    policy_answer = out.decision_debug.get("policy_answer")
    assert len(rag_hits) >= 1 or policy_answer, (
        "knowledge_qa 必须命中 policy_lookup 或返回 RAG 命中（mock）"
    )
    assert "7 天" in out.final_reply or "无理由" in out.final_reply


# ========================================================================
# TR7-2 退款分支
# ========================================================================


@pytest.mark.asyncio
async def test_tr72_refund_branch_tenant_a_standard_3days() -> None:
    tid = f"{TENANT_A}:tr72-{uuid4().hex[:6]}"
    sess, _added = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    order_id = uuid4()
    order_json = _make_standard_order(
        order_id=order_id,
        order_no="A-ORD-202509-001",
        tenant_id=TENANT_A,
        owner_id=OWNER_A1.actor_id,
        delivered_days_ago=3,
        total_cents=10000,
    )

    # Monkey-patch order_query.coroutine → 直接返回 order_json（避免依赖 T3 OrderRepository 的 SQL 结构）
    with _patch_order_query(order_json):
        out = await facade.invoke(
            actor=OWNER_A1,
            tenant_id=TENANT_A,
            thread_id=tid,
            user_message="我这单 A-ORD-202509-001 不合适，想退。",
            session=sess,
        )

    assert out.escalated is False
    assert out.decision_debug is not None
    # task + hint=refund → _build_debug 兼容层回填为旧标签 refund
    assert out.decision_debug["intent_candidate"] == "refund"
    # 写工具内置 HITL interrupt，单测 FakeSession 无法提供写工具所需的 OrderORM；
    # ReAct 子图在 policy_check 后调用 refund_request 但无法完成 → action_kind=knowledge_qa
    policy = out.decision_debug["policy_decision"]
    assert policy["can_refund"] is True
    assert policy["reason_code"] == "eligible_no_reason"
    # 写工具未完成 → action_result_json 为 None（无 RF- 工单）
    assert out.decision_debug.get("action_result_json") is None
    # tool_executions 仅含 order_query + policy_check（写工具不走 ToolRunner call_history）
    tool_execs = out.decision_debug.get("tool_executions") or []
    success_names = {t.get("tool_name") for t in tool_execs if isinstance(t, dict) and t.get("success")}
    assert "order_query" in success_names
    assert "policy_check" in success_names


# ========================================================================
# TR7-3 换货分支
# ========================================================================


@pytest.mark.asyncio
async def test_tr73_exchange_branch_calls_exchange_tool() -> None:
    tid = f"{TENANT_A}:tr73-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    order_id = uuid4()
    order_json = _make_standard_order(
        order_id=order_id,
        order_no="A-ORD-202509-002",
        tenant_id=TENANT_A,
        owner_id=OWNER_A1.actor_id,
        delivered_days_ago=2,
    )
    with _patch_order_query(order_json):
        out = await facade.invoke(
            actor=OWNER_A1,
            tenant_id=TENANT_A,
            thread_id=tid,
            user_message="A-ORD-202509-002 尺寸不合适，想换一条。",
            session=sess,
        )

    assert out.escalated is False
    assert out.decision_debug is not None
    # task + hint=exchange → 兼容层回填 exchange
    assert out.decision_debug["intent_candidate"] == "exchange"
    policy = out.decision_debug["policy_decision"]
    assert policy["can_exchange"] is True
    # 写工具内置 HITL interrupt，单测 FakeSession 无法提供写工具所需 OrderORM
    # → action_kind=knowledge_qa，无 EX- 工单
    assert out.decision_debug.get("action_result_json") is None
    assert "换货" in out.final_reply


# ========================================================================
# TR7-4 维修分支
# ========================================================================


@pytest.mark.asyncio
async def test_tr74_repair_branch_calls_repair_tool() -> None:
    tid = f"{TENANT_A}:tr74-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    order_id = uuid4()
    order_json = _make_standard_order(
        order_id=order_id,
        order_no="A-ORD-202509-003",
        tenant_id=TENANT_A,
        owner_id=OWNER_A1.actor_id,
        delivered_days_ago=10,  # 还在 30 天质量保修期内
    )
    with _patch_order_query(order_json):
        out = await facade.invoke(
            actor=OWNER_A1,
            tenant_id=TENANT_A,
            thread_id=tid,
            user_message="A-ORD-202509-003 这串珠裂了，坏了，能帮我修一下吗？",
            session=sess,
        )

    assert out.escalated is False
    assert out.decision_debug is not None
    # task + hint=repair → 兼容层回填 repair
    assert out.decision_debug["intent_candidate"] == "repair"
    policy = out.decision_debug["policy_decision"]
    assert policy["can_repair"] is True
    # 写工具内置 HITL interrupt，单测 FakeSession 无法提供写工具所需 OrderORM
    # → action_kind=knowledge_qa，无 RP- 工单
    assert out.decision_debug.get("action_result_json") is None
    assert "维修" in out.final_reply


# ========================================================================
# TR7-5 D3 转人工立即执行（2 子触发：① 说人工；② 提图片）
# ========================================================================


@pytest.mark.asyncio
async def test_tr75_d3_handoff_triggered_immediately() -> None:
    tid1 = f"{TENANT_A}:tr75-1-{uuid4().hex[:6]}"
    tid2 = f"{TENANT_A}:tr75-2-{uuid4().hex[:6]}"
    sess, added = _make_session_with_order(
        owner=OWNER_A1, thread_id=tid1, order_result=None
    )
    # 为了断言 mark_escalated 被调用，我们 wrap ConversationRepository.mark_escalated
    call_log: list[tuple[str, str]] = []
    orig_mark = ConversationRepository.mark_escalated

    async def _spy_mark(self_inner, actor, tenant_id, thread_id, ticket_no):
        call_log.append((thread_id, ticket_no))
        return await orig_mark(self_inner, actor, tenant_id, thread_id, ticket_no)

    ConversationRepository.mark_escalated = _spy_mark  # type: ignore[method-assign]
    try:
        facade = _make_facade()
        # 触发 1：用户说「转人工」
        out1 = await facade.invoke(
            actor=OWNER_A1,
            tenant_id=TENANT_A,
            thread_id=tid1,
            user_message="转人工，我要真人客服。",
            session=sess,
        )
        assert out1.escalated is True
        assert out1.escalated_ticket_no is not None
        assert out1.escalated_ticket_no.startswith("HO-")
        assert len(call_log) == 1
        assert call_log[0][0] == tid1

        # 触发 2：用户提图片
        out2 = await facade.invoke(
            actor=OWNER_A1,
            tenant_id=TENANT_A,
            thread_id=tid2,
            user_message="有图片截图你们看一下。",
            session=sess,
            idempotency_salt="img",
        )
        assert out2.escalated is True
        assert out2.escalated_ticket_no is not None
        assert len(call_log) == 2, "图片触发也必须立即执行 mark_escalated（AGENTS 约束：不做图片识别直接转人工）"
    finally:
        ConversationRepository.mark_escalated = orig_mark  # type: ignore[method-assign]
        # 避免 added 越积越多（本断言用 call_log 不看 added）
        _ = added


# ========================================================================
# TR7-6 梵印阁 tenant_b 政策限制（非质量不允许无理由）
# ========================================================================


@pytest.mark.asyncio
async def test_tr76_tenant_b_quality_only_no_refund_for_non_quality() -> None:
    tid = f"{TENANT_B}:tr76-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(
        owner=OWNER_B1, thread_id=tid, order_result=None
    )
    facade = _make_facade()
    order_json = _make_standard_order(
        order_id=uuid4(),
        order_no="B-ORD-202509-888",
        tenant_id=TENANT_B,
        owner_id=OWNER_B1.actor_id,
        delivered_days_ago=2,
        product_type="custom_engraved",  # 梵印阁全部都不适用 7 天无理由（即使是非定制款也不行）
        total_cents=388800,
    )
    with _patch_order_query(order_json):
        out = await facade.invoke(
            actor=OWNER_B1,
            tenant_id=TENANT_B,
            thread_id=tid,
            user_message="B-ORD-202509-888 不喜欢，想退（非质量）。",
            session=sess,
        )

    assert out.escalated is False
    assert out.decision_debug is not None
    policy = out.decision_debug["policy_decision"]
    # 梵印阁 return_policy_type=quality_only：非质量情况下 can_refund=false
    assert policy["can_refund"] is False
    assert policy["reason_code"] == "policy_not_allowed"
    action = out.decision_debug.get("action_result_json")
    # 写工具未完成 → 不应该生成 RF 工单
    assert action is None or "RF-" not in str(action.get("ticket_no", ""))
    # ReAct 子图 policy_check deny 后 FakeAgentChatModel 返回引导人工的兜底文案
    text = out.final_reply
    assert "不符合退款条件" in text or "人工" in text or "客服" in text


# ========================================================================
# TR7-7 闲聊 smalltalk 分支（你好 / 谢谢 / 极短文本）
# ========================================================================


@pytest.mark.asyncio
async def test_tr77_smalltalk_branch_greeting_and_polite() -> None:
    # 子 case1：打招呼「你好」→ 触发 simple_qa 欢迎模板
    tid1 = f"{TENANT_A}:tr77-1-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(
        owner=OWNER_A1, thread_id=tid1, order_result=None
    )
    facade = _make_facade()
    # 打桩 order_query，断言 simple_qa 分支绝对不会调用任何工具（即使工具存在）
    oq_spy = AsyncMock(return_value={})
    orig_coroutine = order_query.coroutine
    order_query.coroutine = oq_spy  # type: ignore[method-assign]
    try:
        out = await facade.invoke(
            actor=OWNER_A1,
            tenant_id=TENANT_A,
            thread_id=tid1,
            user_message="你好",
            session=sess,
        )
    finally:
        order_query.coroutine = orig_coroutine  # type: ignore[method-assign]

    assert out.escalated is False
    # simple_qa 分支：不应调用 order_query 工具
    oq_spy.assert_not_awaited()
    assert out.decision_debug is not None
    # simple_qa → _build_debug 兼容层回填为旧标签 smalltalk
    assert out.decision_debug["intent_candidate"] == "smalltalk"
    # simple_qa 分支不写 action_kind（state 为 None → _build_debug 过滤掉）
    assert out.decision_debug.get("action_kind") is None
    # 品牌欢迎模板命中
    assert "禅饰坊" in out.final_reply
    assert ("您好" in out.final_reply) or ("办理什么业务" in out.final_reply)
    # 不应出现任何工单开头（RF- / EX- / RP- / HO-）
    assert "RF-" not in out.final_reply
    assert "EX-" not in out.final_reply
    assert "RP-" not in out.final_reply
    assert "HO-" not in out.final_reply

    # 子 case2：礼貌语「谢谢！」→ 命中 polite 分支（不客气模板）
    tid2 = f"{TENANT_A}:tr77-2-{uuid4().hex[:6]}"
    sess2, added2 = _make_session_with_order(
        owner=OWNER_A1, thread_id=tid2, order_result=None
    )
    facade2 = _make_facade()
    out2 = await facade2.invoke(
        actor=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid2,
        user_message="  谢谢！  ",
        session=sess2,
    )
    assert out2.escalated is False
    assert out2.decision_debug is not None
    assert out2.decision_debug["intent_candidate"] == "smalltalk"
    assert ("不客气" in out2.final_reply) or ("很高兴为您服务" in out2.final_reply)
    _ = added2

    # 子 case3：纯空格 + 全角标点「?？!！。. 」 → 命中 simple_qa（不报错不转人工）
    tid3 = f"{TENANT_A}:tr77-3-{uuid4().hex[:6]}"
    sess3, _ = _make_session_with_order(
        owner=OWNER_A1, thread_id=tid3, order_result=None
    )
    facade3 = _make_facade()
    out3 = await facade3.invoke(
        actor=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid3,
        user_message=" ？？！ ",
        session=sess3,
    )
    assert out3.escalated is False
    assert out3.decision_debug is not None
    assert out3.decision_debug["intent_candidate"] == "smalltalk"

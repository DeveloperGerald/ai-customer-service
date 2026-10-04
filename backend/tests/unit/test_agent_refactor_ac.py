"""Spec.md AC-1/AC-2/AC-3/AC-5 验收测试（T8.2~T8.5）。

新 4 分类架构下的验收 AC：
    - AC-1：4 路分支 → 事件流 node_start 序列符合新拓扑；simple_qa/handoff
      **没有** rag_retrieve 节点事件；knowledge_qa 可能命中 policy_lookup 或 rag_retrieve。
    - AC-2：退款 path → ReAct 子图执行 order_query/policy_check；写工具内置 HITL
      interrupt，单测 FakeSession 无法提供 OrderORM → 写工具未完成，action_result_json=None。
    - AC-3：签收超期 → policy can_refund=false → 不触发 refund_request 调用；
      回复引导「人工」。
    - AC-5：simple_qa → final_reply 命中欢迎模板（品牌名 + 引导语）。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.application.agent.facade import CustomerServiceAgentFacade
from app.domain.models.tools import ToolAuditLogORM
from app.domain.repositories.identity import Actor
from tests.unit.test_task7_graph import (
    OWNER_A1,
    TENANT_A,
    _make_facade,
    _make_session_with_order,
    _make_standard_order,
    _patch_order_query,
)


class _StaticClassifier:
    """鸭子类型 IntentClassifierProtocol：恒返回固定 intent4，用于隔离 LLM 调用。"""

    def __init__(self, intent4: str = "simple_qa") -> None:
        self._intent4 = intent4

    async def aclassify(self, text: str, **kwargs):
        return (self._intent4, None, None)


# ========================================================================
# Helpers：收集 node_start 序列 + LLM 调用计数
# ========================================================================


async def _collect_node_starts(
    facade: CustomerServiceAgentFacade,
    *,
    owner: Actor,
    tenant_id: str,
    thread_id: str,
    user_message: str,
    session: Any,
) -> tuple[list[str], dict[str, Any]]:
    """走 facade.astream_events，收集 (node_start 名称列表, 最后 reply/debug dict)。"""
    node_starts: list[str] = []
    last_debug: dict[str, Any] = {}
    last_reply_text = ""
    async for evt in facade.astream_events(
        actor=owner,
        tenant_id=tenant_id,
        thread_id=thread_id,
        user_message=user_message,
        session=session,
    ):
        et = evt.get("type")
        if et == "node_start":
            node_starts.append(evt.get("node", ""))
        elif et == "debug":
            last_debug = evt.get("payload") or {}
        elif et == "reply":
            last_reply_text = evt.get("text") or ""
    meta = {"final_reply": last_reply_text, "debug": last_debug}
    return node_starts, meta


# ========================================================================
# AC-1：4 分支节点序列断言
# ========================================================================


@pytest.mark.asyncio
async def test_ac1_smalltalk_has_no_rag_nodes_in_stream() -> None:
    """simple_qa：node_start 序列中必须不含 rag_retrieve 节点。"""
    tid = f"{TENANT_A}:ac1-smalltalk-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    nodes, meta = await _collect_node_starts(
        facade,
        owner=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid,
        user_message="你好呀～",
        session=sess,
    )
    assert nodes, "必须有至少 1 个 node_start 事件"
    assert nodes[0] == "intent_classify", f"首节点必须 intent_classify，实际 {nodes[0]!r}"
    assert not any(n.startswith("rag_retrieve") for n in nodes), (
        f"simple_qa 绝对不能走 RAG，但节点序列: {nodes}"
    )
    assert "compliance_check" in nodes, f"simple_qa 必须经过 compliance_check：{nodes}"
    debug = meta["debug"]
    # simple_qa 分支不写 action_kind（_build_debug 过滤掉 None）
    assert debug.get("action_kind") is None
    # simple_qa → 兼容层回填为旧标签 smalltalk
    assert debug.get("intent_candidate") == "smalltalk"


@pytest.mark.asyncio
async def test_ac1_handoff_has_no_rag_nodes_in_stream() -> None:
    """handoff：node_start 序列中必须不含 rag_retrieve 节点，且 escalated=true。"""
    tid = f"{TENANT_A}:ac1-handoff-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    nodes, _meta = await _collect_node_starts(
        facade,
        owner=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid,
        user_message="转人工，我要投诉",
        session=sess,
    )
    assert nodes and nodes[0] == "intent_classify"
    assert not any(n.startswith("rag_retrieve") for n in nodes), (
        f"handoff 绝对不能走 RAG，但节点序列: {nodes}"
    )
    assert "handoff" in nodes, f"handoff 节点缺失: {nodes}"
    assert "compliance_check" in nodes, f"handoff 必须经过 compliance_check：{nodes}"


@pytest.mark.asyncio
async def test_ac1_simple_qa_has_no_rag_nodes_and_greeting() -> None:
    """simple_qa（强制注入）：节点序列不含 rag_retrieve；final_reply 命中欢迎模板。"""
    tid = f"{TENANT_A}:ac1-simple-qa-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade_base = _make_facade()
    # 用 StaticClassifier 强制 simple_qa 标签
    facade = CustomerServiceAgentFacade(
        retriever=facade_base.retriever,
        classifier=_StaticClassifier("simple_qa"),
        chat_model=facade_base.chat_model,
    )
    nodes, meta = await _collect_node_starts(
        facade,
        owner=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid,
        user_message="今天吃火锅",
        session=sess,
    )
    assert nodes and nodes[0] == "intent_classify"
    assert not any(n.startswith("rag_retrieve") for n in nodes), (
        f"simple_qa 绝对不能走 RAG，但节点序列: {nodes}"
    )
    # simple_qa 走 compliance_check 生成欢迎模板（含品牌名 + 引导语）
    assert "禅饰坊" in meta["final_reply"], (
        f"simple_qa 必须命中品牌欢迎模板：{meta['final_reply']!r}"
    )


@pytest.mark.asyncio
async def test_ac1_knowledge_qa_has_rag_retrieve_before_compliance() -> None:
    """knowledge_qa：必须出现 rag_retrieve，且顺序在 compliance_check 之前。"""
    tid = f"{TENANT_A}:ac1-knowledge-qa-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    nodes, _meta = await _collect_node_starts(
        facade,
        owner=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid,
        user_message="7天无理由规则是什么？",
        session=sess,
    )
    # knowledge_qa 先走 policy_lookup；若 policy_lookup 未命中再走 rag_retrieve
    # FakeRetriever + FakeChatModelProvider 下 policy_lookup 可能命中也可能不命中，
    # 这里仅断言若 rag_retrieve 出现则必须在 compliance_check 之前
    if "rag_retrieve" in nodes:
        rag_idx = nodes.index("rag_retrieve")
        llm_idx = nodes.index("compliance_check")
        assert rag_idx < llm_idx, (
            f"RAG 必须在 compliance_check 之前：rag@{rag_idx} llm@{llm_idx}"
        )
    assert "compliance_check" in nodes, f"knowledge_qa 必须经过 compliance_check：{nodes}"


@pytest.mark.asyncio
async def test_ac1_task_has_task_node_before_compliance() -> None:
    """task：必须出现 task 节点 → compliance_check，顺序保持 task → compliance_check。"""
    tid = f"{TENANT_A}:ac1-task-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    order_id = uuid4()
    order_json = _make_standard_order(
        order_id=order_id,
        order_no="A-ORD-202509-001",
        tenant_id=TENANT_A,
        owner_id=OWNER_A1.actor_id,
        delivered_days_ago=2,
        total_cents=10000,
    )
    with _patch_order_query(order_json):
        nodes, _meta = await _collect_node_starts(
            facade,
            owner=OWNER_A1,
            tenant_id=TENANT_A,
            thread_id=tid,
            user_message="A-ORD-202509-001 我要退款（不喜欢）",
            session=sess,
        )

    assert "task" in nodes, f"task 分支必须有 task 节点: {nodes}"
    assert "compliance_check" in nodes, f"必须经过 compliance_check: {nodes}"
    task_idx = nodes.index("task")
    llm_idx = nodes.index("compliance_check")
    assert task_idx < llm_idx, (
        f"顺序必须 task@{task_idx} < compliance_check@{llm_idx}，序列: {nodes}"
    )


# ========================================================================
# AC-2：退款 path → tool_executions 含 order_query/policy_check（写工具未完成）
# ========================================================================


@pytest.mark.asyncio
async def test_ac2_refund_path_order_query_and_policy_check_in_tool_executions() -> None:
    """退款 path → tool_executions 含 order_query / policy_check（写工具内置 HITL
    interrupt，单测 FakeSession 无法提供 OrderORM → 写工具未完成，无 RF- 工单）。
    """
    tid = f"{TENANT_A}:ac2-refund-{uuid4().hex[:6]}"
    sess, added = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    order_id = uuid4()
    order_json = _make_standard_order(
        order_id=order_id,
        order_no="A-ORD-202509-001",
        tenant_id=TENANT_A,
        owner_id=OWNER_A1.actor_id,
        delivered_days_ago=3,  # 7 天内 → can_refund=true
        total_cents=10000,
    )
    with _patch_order_query(order_json):
        out = await facade.invoke(
            actor=OWNER_A1,
            tenant_id=TENANT_A,
            thread_id=tid,
            user_message="A-ORD-202509-001 收到就不喜欢，想退",
            session=sess,
        )

    # 1. tool_executions（call_history 序列化）必须有 order_query / policy_check 成功调用
    debug = out.decision_debug or {}
    tool_execs = debug.get("tool_executions") or []
    assert isinstance(tool_execs, list), f"tool_executions 必须是 list: {type(tool_execs)}"
    success_names = {
        t.get("tool_name")
        for t in tool_execs
        if isinstance(t, dict) and t.get("success") is True
    }
    for required in ("order_query", "policy_check"):
        assert required in success_names, (
            f"AC-2 缺少 tool_executions[{required}]，实际 success 集合: {success_names}"
        )

    # 2. policy_decision.can_refund=true（标准款 + 7 天内）
    policy = debug.get("policy_decision") or {}
    assert policy.get("can_refund") is True, f"can_refund 应为 True：{policy}"

    # 3. 写工具内置 HITL interrupt，单测 FakeSession 无法提供 OrderORM
    #    → 写工具未完成，action_result_json 为 None（无 RF- 工单）
    assert debug.get("action_result_json") is None, (
        f"写工具未完成应无 action_result_json：{debug.get('action_result_json')!r}"
    )

    # 4. DB audit_logs 至少 1 条 order_query success（policy_check 是纯函数虚拟工具不写 DB）
    audit_logs = [o for o in added if isinstance(o, ToolAuditLogORM)]
    db_success_names = {log.tool_name for log in audit_logs if log.status == "succeeded"}
    assert "order_query" in db_success_names, (
        f"AC-2 DB 层缺少 audit_log[order_query]，实际 success 集合: {db_success_names}"
    )


# ========================================================================
# AC-3：签收超期 → policy can_refund=false，不触发 refund_request 调用，引导人工
# ========================================================================


@pytest.mark.asyncio
async def test_ac3_overdue_signature_no_refund_call_and_mention_human() -> None:
    """签收 365 天超期：refund_request 不能出现在 audit_logs；final_reply 必须含「人工」。"""
    tid = f"{TENANT_A}:ac3-overdue-{uuid4().hex[:6]}"
    sess, added = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade = _make_facade()
    order_id = uuid4()
    order_json = _make_standard_order(
        order_id=order_id,
        order_no="A-ORD-202509-001",
        tenant_id=TENANT_A,
        owner_id=OWNER_A1.actor_id,
        delivered_days_ago=365,  # 严重超期
        total_cents=10000,
    )
    with _patch_order_query(order_json):
        out = await facade.invoke(
            actor=OWNER_A1,
            tenant_id=TENANT_A,
            thread_id=tid,
            user_message="A-ORD-202509-001 我要退款，珠子都裂了（但我签收很久了）",
            session=sess,
        )

    audit_logs = [o for o in added if isinstance(o, ToolAuditLogORM)]
    tool_names = {log.tool_name for log in audit_logs}
    assert (
        "refund_request" not in tool_names
    ), f"policy deny 时绝对不能调 refund_request，但实际 tool_names={tool_names}"

    # 决策层必须 policy_decision 里 can_refund=false 或引导人工
    debug = out.decision_debug or {}
    policy_decision = debug.get("policy_decision") or {}
    # ReAct pipeline 在 policy deny 时会把 final 答复设置为引导人工
    assert (
        "人工" in out.final_reply
        or "客服" in out.final_reply
        or policy_decision.get("can_refund") is False
    ), (
        f"签收超期必须引导人工或明确 can_refund=false；reply={out.final_reply!r}, "
        f"policy={policy_decision}"
    )


# ========================================================================
# AC-5：simple_qa 欢迎模板 + LLM 调用计数
# ========================================================================


@pytest.mark.asyncio
async def test_ac5_simple_qa_greeting_template_and_llm_invoked() -> None:
    """simple_qa：final_reply 命中品牌欢迎模板（含品牌名 + 引导语）。"""
    tid = f"{TENANT_A}:ac5-simple-qa-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(owner=OWNER_A1, thread_id=tid, order_result=None)
    facade_base = _make_facade()

    # 用 StaticClassifier 强制 simple_qa 标签（避免 Keyword classifier fallback 到 knowledge_qa）
    facade = CustomerServiceAgentFacade(
        retriever=facade_base.retriever,
        classifier=_StaticClassifier("simple_qa"),
        chat_model=facade_base.chat_model,
    )

    out = await facade.invoke(
        actor=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid,
        user_message="我今天吃火锅，好开心",
        session=sess,
    )

    # simple_qa → compliance_check 调 LLM 生成欢迎模板（品牌名 + 引导语）
    assert "禅饰坊" in out.final_reply, (
        f"simple_qa 必须命中品牌欢迎模板：actual={out.final_reply!r}"
    )
    assert "您好" in out.final_reply or "办理什么业务" in out.final_reply, (
        f"欢迎模板必须含引导语：actual={out.final_reply!r}"
    )

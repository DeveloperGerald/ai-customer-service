"""T4 工具层测试（LangChain 1.x 原生化重写）。

覆盖：
- 结构保证：9 个模块级 @tool 名称/顺序固定；输入 schema 无任何身份字段
- Args schema 枚举校验（refund/exchange/cancel/product_list）
- ToolGovernanceMiddleware：
  · 读工具成功：audit running→succeeded，不写幂等
  · 写工具成功：幂等 key 计算、action_kind 映射、幂等记录写入
  · 写工具 resume：复用既有 running 审计行
  · 幂等缓存命中：from_cache，handler 不执行
  · 幂等冲突：失败 observation + audit failed
  · 业务异常：失败 observation + audit failed（错误码透传）
  · GraphBubbleUp：放行，audit 保持 running
  · accepted=False：action_kind=aborted + 标准拒绝 final_reply，不写幂等
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command
from pydantic import ValidationError

from app.application.agent.context import AgentRunContext
from app.application.agent.governance import ToolGovernanceMiddleware, _hash_arguments
from app.application.schemas.identity import Role
from app.application.tools.builtin import (
    ALL_TOOLS,
    CancelOrderArgs,
    ExchangeRequestArgs,
    ProductListArgs,
    RefundRequestArgs,
)
from app.core.errors import ErrorCode, ResourceNotFoundError
from app.domain.models.tools import ToolAuditLogORM
from app.domain.repositories.identity import Actor

TENANT_A = "tenant_a"
THREAD_ID = "tenant_a:thread-test"
CONSUMER_A = Actor(
    actor_id="a0000000-0000-0000-0000-000000000001",
    tenant_id=TENANT_A,
    role=Role.CONSUMER,
)
STAFF_A = Actor(
    actor_id="a0000000-0000-0000-0000-000000000002",
    tenant_id=TENANT_A,
    role=Role.STAFF,
)

_FORBIDDEN_IDENTITY_FIELDS = {
    "tenant_id",
    "actor_id",
    "user_id",
    "buyer_user_id",
    "buyer_id",
    "owner_id",
    "operator_id",
}

_EXPECTED_TOOL_NAMES = [
    "product_list",
    "product_query",
    "order_query",
    "current_time",
    "exchange_request",
    "repair_request",
    "refund_request",
    "cancel_order",
    "policy_check",
]


# ============================================================================
# 一、结构保证
# ============================================================================


def test_tool_inventory_names_and_order() -> None:
    assert [t.name for t in ALL_TOOLS] == _EXPECTED_TOOL_NAMES


def test_tool_input_schemas_have_no_identity_fields() -> None:
    for tool in ALL_TOOLS:
        fields = set(tool.get_input_schema().model_fields)
        leaked = fields & _FORBIDDEN_IDENTITY_FIELDS
        assert not leaked, f"{tool.name} leaked identity fields: {leaked}"
        # runtime 注入参数不应出现在工具输入 schema
        assert "runtime" not in fields


# ============================================================================
# 二、Args schema 枚举校验
# ============================================================================


def test_refund_args_reject_bad_reason_enum() -> None:
    with pytest.raises(ValidationError):
        RefundRequestArgs(order_id=str(UUID(int=1)), reason="refund")


def test_exchange_args_reject_bad_reason_enum() -> None:
    with pytest.raises(ValidationError):
        ExchangeRequestArgs(order_id=str(UUID(int=1)), reason="refund")


def test_cancel_args_reject_bad_reason_enum() -> None:
    with pytest.raises(ValidationError):
        CancelOrderArgs(order_id=str(UUID(int=1)), reason="refund")


def test_product_list_args_reject_bad_status() -> None:
    with pytest.raises(ValidationError):
        ProductListArgs(status="all")


# ============================================================================
# 三、ToolGovernanceMiddleware
# ============================================================================


class FakeSession:
    """最小 FakeSession：add 记录 + execute scalar 序列 + flush。"""

    def __init__(self, scalar_sequence: list[Any] | None = None) -> None:
        self.added: list[Any] = []
        self._scalars = list(scalar_sequence or [])
        self.flush = AsyncMock(return_value=None)
        self.execute_calls = 0

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def execute(self, stmt: Any) -> MagicMock:
        self.execute_calls += 1
        value = self._scalars.pop(0) if self._scalars else None
        rp = MagicMock()
        rp.scalar_one_or_none.return_value = value
        return rp


def _run_ctx(session: Any, *, idempotency_salt: str = "salt1") -> AgentRunContext:
    return AgentRunContext(
        actor=CONSUMER_A,
        tenant_id=TENANT_A,
        thread_id=THREAD_ID,
        service_actor=STAFF_A,
        effective_policy=None,
        idempotency_salt=idempotency_salt,
        session=session,
        conversation_repo=MagicMock(),
    )


def _request(
    *,
    tool_name: str,
    args: dict[str, Any],
    state: dict[str, Any] | None = None,
    ctx: AgentRunContext,
) -> ToolCallRequest:
    runtime = MagicMock()
    runtime.context = ctx
    return ToolCallRequest(
        tool_call={"name": tool_name, "args": args, "id": f"call-{tool_name}"},
        tool=None,
        state=state or {},
        runtime=runtime,
    )


def _ok_handler(data: dict[str, Any]) -> Any:
    async def handler(request: Any) -> ToolMessage:
        return ToolMessage(
            content=json.dumps(data, ensure_ascii=False),
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
        )

    return handler


def _audit_logs(session: FakeSession) -> list[ToolAuditLogORM]:
    return [o for o in session.added if isinstance(o, ToolAuditLogORM)]


@pytest.mark.asyncio
async def test_governance_read_tool_success_audit_no_idempotency() -> None:
    session = FakeSession()
    ctx = _run_ctx(session)
    data = {"order_id": "x", "status": "delivered"}
    req = _request(
        tool_name="order_query",
        args={"order_no": "A-1"},
        state={"messages": [HumanMessage(content="帮我查一下订单 A-1 的状态")]},
        ctx=ctx,
    )

    out = await ToolGovernanceMiddleware().awrap_tool_call(
        req, _ok_handler(data)
    )

    assert isinstance(out, Command)
    logs = _audit_logs(session)
    assert len(logs) == 1
    assert logs[0].status == "succeeded"
    assert logs[0].idempotency_key is None
    assert logs[0].tenant_id == TENANT_A
    assert logs[0].actor_id == UUID(CONSUMER_A.actor_id)
    # 读工具：仅一次 SELECT running log 检查不会发生（读工具不查 running）
    executions = out.update["tool_executions"]
    assert len(executions) == 1
    assert executions[0]["success"] is True
    assert out.update["order_detail_json"] == data


@pytest.mark.asyncio
async def test_governance_order_query_blocks_fabricated_order_no() -> None:
    """防幻觉护栏：order_query 的 order_no 未出现在任何用户消息中 → 拦截，handler 不执行。"""
    session = FakeSession()
    ctx = _run_ctx(session)
    req = _request(
        tool_name="order_query",
        args={"order_no": "A-ORD-202509-001"},
        state={"messages": [HumanMessage(content="我想维修")]},
        ctx=ctx,
    )

    async def handler(request: Any) -> ToolMessage:  # pragma: no cover
        raise AssertionError("handler 不应被执行")

    out = await ToolGovernanceMiddleware().awrap_tool_call(req, handler)

    logs = _audit_logs(session)
    assert len(logs) == 1
    assert logs[0].status == "failed"
    assert logs[0].error_code == str(ErrorCode.VALIDATION_ERROR.value)
    content = json.loads(out.update["messages"][0].content)
    assert content["success"] is False
    assert content["error_code"] == str(ErrorCode.VALIDATION_ERROR.value)
    assert "编造" in content["error"]


@pytest.mark.asyncio
async def test_governance_write_success_maps_action_and_writes_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    append_mock = AsyncMock()
    monkeypatch.setattr(
        "app.application.agent.governance.ConversationRepository.append_message",
        append_mock,
    )

    session = FakeSession(scalar_sequence=[None, None])
    ctx = _run_ctx(session)
    data = {
        "tool": "refund_request",
        "accepted": True,
        "ticket_no": "RF-TENANT_A-12345678",
        "order_id": "x",
        "order_no": "A-ORD-PROBE",
        "new_status": "refunded",
        "refund_amount_cents": 88800,
    }
    req = _request(
        tool_name="refund_request",
        args={"order_id": "x", "reason": "quality"},
        ctx=ctx,
    )

    out = await ToolGovernanceMiddleware().awrap_tool_call(
        req, _ok_handler(data)
    )

    logs = _audit_logs(session)
    assert len(logs) == 1
    assert logs[0].status == "succeeded"
    # 幂等 key = agt-{thread_id}-1-{salt}
    assert logs[0].idempotency_key == f"agt-{THREAD_ID}-1-salt1"
    assert out.update["action_kind"] == "refund"
    assert out.update["action_result_json"] == data
    # 幂等 INSERT：execute 调用 = running log SELECT + idem SELECT + INSERT
    assert session.execute_calls == 3
    assert append_mock.await_count == 1


@pytest.mark.asyncio
async def test_governance_write_seq_uses_prior_write_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.application.agent.governance.ConversationRepository.append_message",
        AsyncMock(),
    )
    session = FakeSession(scalar_sequence=[None, None])
    ctx = _run_ctx(session, idempotency_salt="s")
    prior_execution = {
        "tool_name": "exchange_request",
        "success": True,
        "data": {},
    }
    req = _request(
        tool_name="refund_request",
        args={"order_id": "x", "reason": "quality"},
        state={"tool_executions": [prior_execution]},
        ctx=ctx,
    )

    await ToolGovernanceMiddleware().awrap_tool_call(
        req, _ok_handler({"accepted": True})
    )

    logs = _audit_logs(session)
    assert logs[0].idempotency_key == f"agt-{THREAD_ID}-2-s"


@pytest.mark.asyncio
async def test_governance_resume_reuses_running_audit_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.application.agent.governance.ConversationRepository.append_message",
        AsyncMock(),
    )
    real_hash = _hash_arguments(
        "refund_request", {"order_id": "x", "reason": "quality"}
    )
    existing = ToolAuditLogORM(
        call_id=uuid4(),
        tenant_id=TENANT_A,
        actor_id=UUID(CONSUMER_A.actor_id),
        tool_name="refund_request",
        idempotency_key=f"agt-{THREAD_ID}-1-salt1",
        arguments_hash=real_hash,
        arguments_json={"order_id": "x"},
        status="running",
        started_at=_dt.datetime.now(_dt.timezone.utc),
    )
    # execute 序列：running log SELECT 命中 → idem record SELECT None → INSERT
    session = FakeSession(scalar_sequence=[existing, None])
    ctx = _run_ctx(session)
    req = _request(
        tool_name="refund_request",
        args={"order_id": "x", "reason": "quality"},
        ctx=ctx,
    )

    out = await ToolGovernanceMiddleware().awrap_tool_call(
        req, _ok_handler({"accepted": True})
    )

    logs = _audit_logs(session)
    # resume 复用查出来的 running 行（不新增悬垂行），existing 被收尾为 succeeded
    assert len(logs) == 0
    assert existing.status == "succeeded"
    assert out.update["action_kind"] == "refund"


@pytest.mark.asyncio
async def test_governance_idempotency_cache_hit_skips_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.domain.models.tools import IdempotencyRecordORM

    monkeypatch.setattr(
        "app.application.agent.governance.ConversationRepository.append_message",
        AsyncMock(),
    )

    cached_data = {"tool": "refund_request", "accepted": True, "cached": True}
    cached = IdempotencyRecordORM(
        tenant_id=TENANT_A,
        idempotency_key=f"agt-{THREAD_ID}-1-salt1",
        tool_name="refund_request",
        call_id=uuid4(),
        arguments_hash=_hash_arguments(
            "refund_request", {"order_id": "x", "reason": "quality"}
        ),
        cached_result_json=cached_data,
    )
    # running log None → idem record 命中
    session = FakeSession(scalar_sequence=[None, cached])
    ctx = _run_ctx(session)
    req = _request(
        tool_name="refund_request",
        args={"order_id": "x", "reason": "quality"},
        ctx=ctx,
    )

    called = False

    async def handler(request: Any) -> ToolMessage:
        nonlocal called
        called = True
        raise AssertionError("handler must not run on cache hit")

    out = await ToolGovernanceMiddleware().awrap_tool_call(req, handler)

    assert called is False
    execution = out.update["tool_executions"][0]
    assert execution["from_idempotency_cache"] is True
    assert json.loads(out.update["messages"][0].content) == cached_data


@pytest.mark.asyncio
async def test_governance_idempotency_conflict_returns_failed_observation() -> None:
    from app.domain.models.tools import IdempotencyRecordORM

    cached = IdempotencyRecordORM(
        tenant_id=TENANT_A,
        idempotency_key=f"agt-{THREAD_ID}-1-salt1",
        tool_name="refund_request",
        call_id=uuid4(),
        arguments_hash="different-old-hash",
        cached_result_json={"old": True},
    )
    session = FakeSession(scalar_sequence=[None, cached])
    ctx = _run_ctx(session)
    req = _request(
        tool_name="refund_request",
        args={"order_id": "x", "reason": "quality"},
        ctx=ctx,
    )

    out = await ToolGovernanceMiddleware().awrap_tool_call(
        req, _ok_handler({"accepted": True})
    )

    logs = _audit_logs(session)
    assert logs[0].status == "failed"
    assert logs[0].error_code == str(ErrorCode.TOOL_IDEMPOTENCY_CONFLICT.value)
    content = json.loads(out.update["messages"][0].content)
    assert content["success"] is False
    assert content["error_code"] == str(ErrorCode.TOOL_IDEMPOTENCY_CONFLICT.value)
    assert out.update["tool_executions"][0]["success"] is False


@pytest.mark.asyncio
async def test_governance_business_error_returns_failed_observation() -> None:
    session = FakeSession()
    ctx = _run_ctx(session)
    req = _request(
        tool_name="order_query",
        args={"order_no": "A-1"},
        state={"messages": [HumanMessage(content="查订单 A-1")]},
        ctx=ctx,
    )

    async def handler(request: Any) -> ToolMessage:
        raise ResourceNotFoundError(
            ErrorCode.RESOURCE_NOT_FOUND,
            message="订单不存在：A-1",
        )

    out = await ToolGovernanceMiddleware().awrap_tool_call(req, handler)

    logs = _audit_logs(session)
    assert logs[0].status == "failed"
    assert logs[0].error_code == str(ErrorCode.RESOURCE_NOT_FOUND.value)
    content = json.loads(out.update["messages"][0].content)
    assert content["success"] is False
    assert "订单不存在" in content["error"]


@pytest.mark.asyncio
async def test_governance_graph_bubble_up_propagates_and_keeps_running() -> None:
    session = FakeSession()
    ctx = _run_ctx(session)
    req = _request(
        tool_name="refund_request",
        args={"order_id": "x", "reason": "quality"},
        ctx=ctx,
    )

    async def handler(request: Any) -> ToolMessage:
        raise GraphBubbleUp()

    with pytest.raises(GraphBubbleUp):
        await ToolGovernanceMiddleware().awrap_tool_call(req, handler)

    logs = _audit_logs(session)
    assert len(logs) == 1
    assert logs[0].status == "running"


@pytest.mark.asyncio
async def test_governance_write_rejected_maps_aborted_without_idempotency() -> None:
    session = FakeSession(scalar_sequence=[None, None])
    ctx = _run_ctx(session)
    data = {
        "tool": "refund_request",
        "accepted": False,
        "reason": "user_cancelled",
        "order_id": "x",
        "order_no": "A-ORD-PROBE",
    }
    req = _request(
        tool_name="refund_request",
        args={"order_id": "x", "reason": "quality"},
        ctx=ctx,
    )

    out = await ToolGovernanceMiddleware().awrap_tool_call(
        req, _ok_handler(data)
    )

    assert out.update["action_kind"] == "aborted"
    assert out.update["action_result_json"] == data
    assert "已取消" in out.update["final_reply"]
    # running log SELECT + idem SELECT；无 INSERT
    assert session.execute_calls == 2
    logs = _audit_logs(session)
    assert logs[0].status == "succeeded"

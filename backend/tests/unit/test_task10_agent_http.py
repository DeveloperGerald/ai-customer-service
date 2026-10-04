"""T10 Agent HTTP 接口 6 条单测（同步 JSON + SSE 流式）。

覆盖：
- TR10-1 同步 run：FAQ 分支 → final_reply 非空 + decision_debug intent=faq
- TR10-2 同步 run：退款分支（无理由）→ policy_decision refund_amount + reason_code
- TR10-3 同步 run：命中 escalated → escalated=true + ticket_no 前缀 HO-
- TR10-4 SSE stream：start → escalated → tool → reply → debug → done + [DONE]
- TR10-5 跨 tenant + cross owner 访问 → 404（ResourceNotFound 不暴露存在性）
- TR10-6 idempotency_key 字段透传到 facade.invoke
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.application.auth import REQUEST_ACTOR_CONTEXT
from app.application.schemas.agent import AgentRunResult
from app.application.schemas.identity import issue_demo_token
from app.config import Settings
from app.core.infrastructure import InfrastructureBundle
from app.infrastructure.db import engine as _db_engine_mod
from app.main import AppState, create_app
from tests.unit.test_task6_conversations import (  # noqa: F401
    CONSUMER_A1,
    CONSUMER_B1,
    TENANT_A,
    TENANT_B,
)


class _FakeFacade:
    """Spy 版 CustomerServiceAgentFacade：记录 invoke 参数 + 返回预设结果。"""

    def __init__(self, result: AgentRunResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def invoke(self, **kwargs: Any) -> AgentRunResult:
        self.calls.append(dict(kwargs))
        return self.result

    async def astream_events(self, **kwargs: Any):
        """Mock 版流式：按新协议产出 start → escalated → reply → debug → done。

        与真实 facade.astream_events 对齐的事件协议：
          {"type":"start",...} → {"type":"escalated",...} → {"type":"reply",...} → {"type":"debug",...} → {"type":"done"}
        """
        import asyncio

        self.stream_calls.append(dict(kwargs))
        r: AgentRunResult = self.result
        yield {"type": "start", "thread_id": kwargs.get("thread_id", ""), "user_input": kwargs.get("user_message", "")}
        # 模拟节点级事件（前端不强制处理，但事件协议要完整）
        for node in ("intent_classify", "rag_retrieve", "order_query", "policy_decision"):
            yield {"type": "node_start", "node": node}
            await asyncio.sleep(0)
            yield {"type": "node_end", "node": node, "patch": None}
        if r.escalated:
            yield {
                "type": "escalated",
                "ticket_no": r.escalated_ticket_no,
                "reason": r.escalation_reason,
            }
        if r.decision_debug and r.decision_debug.get("policy_decision"):
            yield {
                "type": "tool",
                "kind": "policy_decision",
                "payload": r.decision_debug["policy_decision"],
            }
        # token 级增量（为了单测验证 reply_chunk 通路存在：先发几块，最后发完整 reply）
        text = r.final_reply or ""
        step = max(1, len(text) // 4)
        for i in range(0, len(text), step):
            chunk = text[i : i + step]
            if chunk:
                yield {"type": "reply_chunk", "text": chunk}
        yield {"type": "reply", "text": text}
        if r.decision_debug:
            yield {"type": "debug", "payload": r.decision_debug}
        yield {"type": "done"}


def _headers(settings: Any, actor: Any) -> dict[str, str]:
    """构造测试 HTTP 头：X-Tenant-Id + Bearer 演示令牌。

    注意：issue_demo_token 返回 DemoTokenBundle（含 access_token/claims/expires_in），
    必须取 .access_token 字符串拼到 Bearer，不能把 Pydantic Model 直接 f-string 进去，
    否则 Authorization 头会变成 `Bearer DemoTokenBundle(...)`，PyJWT 解码前就会抛
    `Invalid header string: utf-8 codec can't decode byte 0xc7 in position 1`。
    """
    token = issue_demo_token(settings.security, tenant_id=actor.tenant_id, actor_id=actor.actor_id, role=actor.role)
    access = getattr(token, "access_token", None)
    if not isinstance(access, str):
        raise RuntimeError(
            f"issue_demo_token 返回非 DemoTokenBundle：{token}(type={type(token).__name__})"
        )
    return {
        "X-Tenant-Id": actor.tenant_id,
        "Authorization": f"Bearer {access}",
    }


def _make_fake_session_class(added_container: list[Any] | None = None):
    """构造 FakeSession 类（继承 AsyncMock 但重写 add）。"""

    class _FakeSession(AsyncMock):
        _added: list[Any]

        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            object.__setattr__(self, "_added", added_container if added_container is not None else [])

        def add(self, obj: Any) -> None:  # type: ignore[override]
            self._added.append(obj)

    return _FakeSession


def _make_thread_orm(owner_actor=CONSUMER_A1):
    from tests.unit.test_task6_conversations import _make_thread_orm as _mk

    tid_raw = uuid4()
    return _mk(
        thread_id=f"{owner_actor.tenant_id}:{tid_raw.hex}",
        tenant_id=owner_actor.tenant_id,
        owner_id=owner_actor.actor_id,
    )


def _wire_app_with_session(
    monkeypatch: pytest.MonkeyPatch,
    settings: Any,
    infra_bundle: InfrastructureBundle,
    facade: _FakeFacade,
    *,
    thread_orm: Any,
    get_thread_side_effect: Any = None,
) -> Any:
    """使用 TR5-6 同款模式：双保险 monkey patch scoped_db_session。"""
    import re as _re
    from uuid import UUID

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.application.schemas.identity import Role
    from app.domain.constants.policies import TENANT_POLICIES
    from app.domain.models.identity import TenantORM, UserORM

    fake_session_cls = _make_fake_session_class()
    fake_sess: Any = fake_session_cls()

    def _parse_tid_from_sql(sql: str) -> str:
        m = _re.search(r"tenant_id\s*=\s*'([^']+)'", sql)
        if m:
            return m.group(1)
        # 回退：从 thread_orm 拿
        return getattr(thread_orm, "tenant_id", "tenant_a")

    def _stable_staff_uuid(tid: str) -> str:
        # 把 tenant_id 哈希成 8 位 hex 前缀，避免 "tenant_a" 中含非 hex 字符
        import hashlib as _hl
        return _hl.md5(tid.encode("utf-8")).hexdigest()[:8] + "-0000-0000-0000-000000000002"

    def _make_staff_user(tid: str) -> UserORM:
        return UserORM(
            user_id=UUID(_stable_staff_uuid(tid)),
            tenant_id=tid,
            username=f"{tid}_staff",
            display_name=f"{TENANT_POLICIES.get(tid, TENANT_POLICIES['tenant_a']).brand_name}-系统客服",
            email=f"{tid}_staff@internal.local",
            phone=None,
            role=Role.STAFF.value,
            is_active=True,
        )

    def _make_tenant_orm(tid: str) -> TenantORM:
        pol = TENANT_POLICIES.get(tid, TENANT_POLICIES["tenant_a"])
        return TenantORM(
            tenant_id=tid,
            name=pol.brand_name,
            display_name=f"{pol.brand_name}（{pol.slogan}）",
            description=pol.full_text[:200],
            is_active=True,
        )

    async def _dispatch_execute(*a: Any, **_k: Any) -> MagicMock:
        stmt_str = str(a[0].compile(compile_kwargs={"literal_binds": True})) if a else ""
        tid = _parse_tid_from_sql(stmt_str)
        if get_thread_side_effect is not None:
            rp = MagicMock()
            rp.scalar_one_or_none = MagicMock(side_effect=get_thread_side_effect)
            rp.scalars = MagicMock(side_effect=get_thread_side_effect)
            return rp
        rp = MagicMock()
        if " FROM users " in stmt_str or "from users " in stmt_str.lower():
            rp.scalar_one_or_none.return_value = _make_staff_user(tid)
            return rp
        if " FROM tenants " in stmt_str or "from tenants " in stmt_str.lower():
            rp.scalar_one_or_none.return_value = _make_tenant_orm(tid)
            return rp
        if "conversation_threads" in stmt_str and "UPDATE" not in stmt_str:
            rp.scalar_one_or_none.return_value = thread_orm
            scalars = MagicMock()
            scalars.first.return_value = thread_orm
            scalars.all.return_value = []
            rp.scalars.return_value = scalars
            return rp
        # 兜底：conversation_threads / conversation_messages 等返回 thread_orm 或空
        rp.scalar_one_or_none.return_value = thread_orm
        scalars = MagicMock()
        scalars.first.return_value = thread_orm
        scalars.all.return_value = []
        rp.scalars.return_value = scalars
        return rp

    fake_sess.execute = AsyncMock(side_effect=_dispatch_execute)
    fake_sess.flush = AsyncMock()
    fake_sess.commit = AsyncMock()

    async def _fake_factory_caller(**_kw: Any):
        return fake_sess

    factory_mock = MagicMock(spec=async_sessionmaker)
    factory_mock.side_effect = _fake_factory_caller
    infra_bundle.db_session_factory = factory_mock  # type: ignore[assignment]

    @asynccontextmanager
    async def _fake_scope(_bundle: Any):
        yield fake_sess

    monkeypatch.setattr(_db_engine_mod, "scoped_db_session", _fake_scope)
    import app.api.agent as _agent_mod

    monkeypatch.setattr(_agent_mod, "scoped_db_session", _fake_scope)

    # 与 conftest._build_test_app 对齐：create_app() 内部 load_settings/verify_demo_token 必须使用
    # 测试夹具 settings，否则 backend/.env 里的 SECURITY__DEMO_TOKEN_SECRET 会污染 ActorMiddleware
    # 验签密钥 → 401 AUTH_TOKEN_INVALID。
    import app.application.schemas.identity as _identity_mod
    import app.config as _config_mod
    import app.main as _main_mod

    _orig_verify = _identity_mod.verify_demo_token
    _orig_load_cfg = _config_mod.load_settings
    _orig_load_main = _main_mod.load_settings

    def _patched_load(*_a: Any, **_k: Any) -> Settings:
        return settings

    def _verify_fixture_sec(_ignored_settings: Any, token: str):
        return _orig_verify(settings.security, token)

    monkeypatch.setattr(_config_mod, "load_settings", _patched_load)
    monkeypatch.setattr(_main_mod, "load_settings", _patched_load)
    monkeypatch.setattr(_identity_mod, "verify_demo_token", _verify_fixture_sec)

    app = create_app()
    app.state.bundle = AppState(settings, infra_bundle)
    app.state.agent_facade_overrides = facade
    return app


def _parse_sse_stream(body: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event_line = None
        data_line = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_line = line[len("event: "):]
            elif line.startswith("data: "):
                data_line = line[len("data: "):]
        if event_line is None or data_line is None:
            continue
        if data_line == "[DONE]":
            frames.append({"event": event_line, "data": "[DONE]"})
            continue
        frames.append({"event": event_line, "data": json.loads(data_line)})
    return frames


# ========================================================================
# TR10-1 同步 FAQ 分支
# ========================================================================


@pytest.mark.asyncio
async def test_tr101_sync_run_faq_branch(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Any,
    infra_bundle: InfrastructureBundle,
) -> None:
    thread = _make_thread_orm(CONSUMER_A1)
    facade = _FakeFacade(
        AgentRunResult(
            final_reply="您好，我是客服小串，可以查询订单政策和售后处理。",
            escalated=False,
            decision_debug={"intent": "faq", "action": "faq_reply", "policy_decision": None},
        )
    )

    app = _wire_app_with_session(monkeypatch, test_settings, infra_bundle, facade, thread_orm=thread)
    from httpx import ASGITransport, AsyncClient

    REQUEST_ACTOR_CONTEXT.set(CONSUMER_A1)
    headers = _headers(test_settings, CONSUMER_A1)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(
            f"/api/agent/conversations/{thread.thread_id}/run",
            json={"text": "你们支持几天无理由退货？"},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["final_reply"], str) and len(body["final_reply"]) > 0
    assert body["escalated"] is False
    assert body.get("decision_debug", {}).get("intent") == "faq"

    assert len(facade.calls) == 1
    assert facade.calls[0]["tenant_id"] == CONSUMER_A1.tenant_id
    assert facade.calls[0]["thread_id"] == thread.thread_id
    assert facade.calls[0]["user_message"] == "你们支持几天无理由退货？"
    assert facade.calls[0]["idempotency_salt"] == ""


# ========================================================================
# TR10-2 同步 run：退款分支 → policy_decision 非空
# ========================================================================


@pytest.mark.asyncio
async def test_tr102_sync_run_refund_eligible_no_reason(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Any,
    infra_bundle: InfrastructureBundle,
) -> None:
    thread = _make_thread_orm(CONSUMER_A1)
    facade = _FakeFacade(
        AgentRunResult(
            final_reply="可以为您办理无理由退换，按政策将扣除 10% 手续费，预计退款 1111.11 元。",
            decision_debug={
                "intent": "refund",
                "policy_decision": {
                    "can_refund": True,
                    "can_exchange": True,
                    "can_repair": False,
                    "requires_quality_evidence": False,
                    "restocking_fee_pct": 10,
                    "refund_amount_cents": 111_111,
                    "reason_code": "eligible_no_reason",
                    "reason_human_readable": "可办理无理由退货，扣除 10% 手续费",
                    "debug": {"order_total_cents": 123_456},
                },
            },
        )
    )

    app = _wire_app_with_session(monkeypatch, test_settings, infra_bundle, facade, thread_orm=thread)
    from httpx import ASGITransport, AsyncClient

    REQUEST_ACTOR_CONTEXT.set(CONSUMER_A1)
    headers = _headers(test_settings, CONSUMER_A1)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(
            f"/api/agent/conversations/{thread.thread_id}/run",
            json={"text": "我要退手串订单 SO-1001，刚收到不喜欢"},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    pd = body.get("decision_debug", {}).get("policy_decision") or {}
    assert pd["reason_code"] == "eligible_no_reason"
    assert pd["refund_amount_cents"] == 111_111
    assert pd["restocking_fee_pct"] == 10
    assert body["escalated"] is False


# ========================================================================
# TR10-3 同步 escalated（转人工分支，文本/卡片展示）
# ========================================================================


@pytest.mark.asyncio
async def test_tr103_sync_run_escalated_ticket(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Any,
    infra_bundle: InfrastructureBundle,
) -> None:
    thread = _make_thread_orm(CONSUMER_A1)
    facade = _FakeFacade(
        AgentRunResult(
            final_reply="已为您转接人工客服：工单号 HO-20250913-00012，客服将在 5-10 分钟内介入。",
            escalated=True,
            escalated_ticket_no="HO-20250913-00012",
            escalation_reason="订单缺少必要信息，无法自动判定，已按 D3 转人工。",
        )
    )

    app = _wire_app_with_session(monkeypatch, test_settings, infra_bundle, facade, thread_orm=thread)
    from httpx import ASGITransport, AsyncClient

    REQUEST_ACTOR_CONTEXT.set(CONSUMER_A1)
    headers = _headers(test_settings, CONSUMER_A1)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(
            f"/api/agent/conversations/{thread.thread_id}/run",
            json={"text": "我要找人工客服处理"},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["escalated"] is True
    assert str(body["escalated_ticket_no"]).startswith("HO-")
    assert isinstance(body["escalation_reason"], str) and len(body["escalation_reason"]) > 0
    assert "人工客服" in body["final_reply"]


# ========================================================================
# TR10-4 SSE stream：事件帧顺序
# ========================================================================


@pytest.mark.asyncio
async def test_tr104_sse_stream_event_sequence(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Any,
    infra_bundle: InfrastructureBundle,
) -> None:
    thread = _make_thread_orm(CONSUMER_A1)
    facade = _FakeFacade(
        AgentRunResult(
            final_reply="已为您转接人工客服：HO-20250913-00099",
            escalated=True,
            escalated_ticket_no="HO-20250913-00099",
            escalation_reason="缺少订单号，已转人工。",
            decision_debug={"intent": "handoff", "policy_decision": None},
        )
    )

    app = _wire_app_with_session(monkeypatch, test_settings, infra_bundle, facade, thread_orm=thread)
    from httpx import ASGITransport, AsyncClient

    REQUEST_ACTOR_CONTEXT.set(CONSUMER_A1)
    headers = _headers(test_settings, CONSUMER_A1)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(
            f"/api/agent/conversations/{thread.thread_id}/stream",
            json={"text": "我没有订单号要投诉"},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    ctype = resp.headers.get("content-type", "")
    assert "text/event-stream" in ctype, ctype
    frames = _parse_sse_stream(resp.text)
    event_names = [f["event"] for f in frames]

    assert event_names[0] == "start"
    assert event_names[-2] == "done"
    assert event_names[-1] == "done" and frames[-1]["data"] == "[DONE]"
    assert "reply" in event_names
    assert "debug" in event_names
    assert "escalated" in event_names

    escalated = next(f["data"] for f in frames if f["event"] == "escalated")
    assert escalated["ticket_no"] == "HO-20250913-00099"

    reply = next(f["data"] for f in frames if f["event"] == "reply")
    assert isinstance(reply["text"], str) and len(reply["text"]) > 0

    start = next(f["data"] for f in frames if f["event"] == "start")
    assert start["user_input"] == "我没有订单号要投诉"


# ========================================================================
# TR10-5 跨 tenant 访问 → 404（不暴露存在）
# ========================================================================


@pytest.mark.asyncio
async def test_tr105_cross_tenant_access_returns_404(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Any,
    infra_bundle: InfrastructureBundle,
) -> None:
    thread = _make_thread_orm(CONSUMER_A1)
    from app.core.errors import ResourceNotFoundError

    facade = _FakeFacade(AgentRunResult(final_reply="不该被调用"))

    def _not_found(*a: Any, **kw: Any) -> None:  # pragma: no cover - side effect 直接抛
        raise ResourceNotFoundError("thread", thread.thread_id)

    app = _wire_app_with_session(
        monkeypatch,
        test_settings,
        infra_bundle,
        facade,
        thread_orm=thread,
        get_thread_side_effect=_not_found,
    )
    from httpx import ASGITransport, AsyncClient

    REQUEST_ACTOR_CONTEXT.set(CONSUMER_B1)
    headers = _headers(test_settings, CONSUMER_B1)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp_sync = await ac.post(
            f"/api/agent/conversations/{thread.thread_id}/run",
            json={"text": "跨租户访问"},
            headers=headers,
        )
        resp_stream = await ac.post(
            f"/api/agent/conversations/{thread.thread_id}/stream",
            json={"text": "跨租户访问"},
            headers=headers,
        )

    assert resp_sync.status_code == 404, resp_sync.text
    assert resp_stream.status_code == 404, resp_stream.text
    assert len(facade.calls) == 0


# ========================================================================
# TR10-6 idempotency_key 透传
# ========================================================================


@pytest.mark.asyncio
async def test_tr106_idempotency_key_propagates_to_facade(
    monkeypatch: pytest.MonkeyPatch,
    test_settings: Any,
    infra_bundle: InfrastructureBundle,
) -> None:
    thread = _make_thread_orm(CONSUMER_A1)
    facade = _FakeFacade(AgentRunResult(final_reply="OK"))
    app = _wire_app_with_session(monkeypatch, test_settings, infra_bundle, facade, thread_orm=thread)
    from httpx import ASGITransport, AsyncClient

    REQUEST_ACTOR_CONTEXT.set(CONSUMER_A1)
    headers = _headers(test_settings, CONSUMER_A1)
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    idem_key = f"idem-sync-{uuid4().hex}"
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        resp = await ac.post(
            f"/api/agent/conversations/{thread.thread_id}/run",
            json={"text": "帮我换货 SO-2020", "idempotency_key": idem_key},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    assert len(facade.calls) == 1
    assert facade.calls[0]["idempotency_salt"] == idem_key
    assert facade.calls[0]["tenant_id"] == CONSUMER_A1.tenant_id
    assert facade.calls[0]["user_message"] == "帮我换货 SO-2020"
    assert facade.calls[0]["thread_id"] == thread.thread_id

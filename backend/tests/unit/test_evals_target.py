"""target 单测：事件收集与 confirmation 暂停识别。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.evals import target as target_mod


class _FakeFacade:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.calls: list[dict[str, Any]] = []

    async def astream_events(self, **kwargs: Any):
        self.calls.append(kwargs)
        for event in self.events:
            yield event


@pytest.fixture
def _patched_target(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def _fake_scope(_bundle: Any):
        yield object()

    monkeypatch.setattr(target_mod, "scoped_db_session", _fake_scope)

    class _FakeRepo:
        def __init__(self, _session: Any) -> None:
            pass

        async def create_thread(self, *_args: Any, **_kwargs: Any):
            return SimpleNamespace(thread_id="tenant_a:threadhex")

    monkeypatch.setattr(target_mod, "ConversationRepository", _FakeRepo)


@pytest.mark.asyncio
async def test_target_collects_reply_and_patches(
    _patched_target: None,
    test_settings: Any,
) -> None:
    events = [
        {"type": "start"},
        {
            "type": "node_end",
            "patch": {
                "intent_candidate": "task",
                "intent_hint": "order_status",
                "tool_executions": [{"tool_name": "order_query"}],
            },
        },
        {"type": "reply", "text": "订单已签收"},
        {"type": "done"},
    ]
    facade = _FakeFacade(events)
    target = target_mod.make_async_target(
        settings=test_settings,
        facade=facade,  # type: ignore[arg-type]
        bundle=object(),
    )

    result = await target(
        {"tenant_id": "tenant_a", "message": "查一下订单"}
    )

    assert result["final_reply"] == "订单已签收"
    assert result["intent_candidate"] == "task"
    assert result["intent_hint"] == "order_status"
    assert result["tool_executions"] == [{"tool_name": "order_query"}]
    assert result["confirmation_required"] is False
    assert result["pending_action"] is None
    # actor 必须是固定 demo consumer
    assert facade.calls[0]["actor"].actor_id == target_mod.EVAL_USER_IDS["tenant_a"]


@pytest.mark.asyncio
async def test_target_records_confirmation_pending(
    _patched_target: None,
    test_settings: Any,
) -> None:
    pending = {"tool": "refund_request", "args": {"order_id": "x"}}
    events = [
        {
            "type": "node_end",
            "patch": {"intent_candidate": "task", "intent_hint": "refund"},
        },
        {"type": "confirmation_required", "pending_action": pending},
    ]
    facade = _FakeFacade(events)
    target = target_mod.make_async_target(
        settings=test_settings,
        facade=facade,  # type: ignore[arg-type]
        bundle=object(),
    )

    result = await target(
        {"tenant_id": "tenant_a", "message": "我要退款"}
    )

    assert result["confirmation_required"] is True
    assert result["pending_action"] == pending

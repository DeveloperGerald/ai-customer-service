"""aevaluate 的异步 target。

每条用例：建临时线程 → 仅消费 facade.astream_events() 收集结果。
写操作遇到 confirmation_required 即终点（首期不 resume、不真正写库）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.application.agent.facade import CustomerServiceAgentFacade
from app.application.schemas.conversation import ConversationThreadCreate
from app.config import Settings
from app.domain.repositories.conversation import ConversationRepository
from app.domain.repositories.identity import Actor
from app.evals.schema import EvalCase  # noqa: F401  (类型文档用途)
from app.infrastructure.db.engine import InfrastructureBundle, scoped_db_session

# 三租户固定 demo consumer（与前端 demo token / DB users 一致）
EVAL_USER_IDS: dict[str, str] = {
    "tenant_a": "3f88a233-4d11-50e6-926b-e0ddd2838c0c",
    "tenant_b": "d5f58850-d733-5150-9b07-f96fa2d53b41",
    "tenant_c": "ce37ef2f-e00a-54b3-ace9-9eb2cb673409",
}

THREAD_TITLE_PREFIX = "[离线评估]"

AsyncTarget = Callable[[dict[str, Any]], Any]


def make_async_target(
    *,
    settings: Settings,
    facade: CustomerServiceAgentFacade,
    bundle: InfrastructureBundle,
) -> AsyncTarget:
    """构造 aevaluate 用 target（每用例独立临时线程 + 独立 session）。"""

    async def target(inputs: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(inputs["tenant_id"])
        message = str(inputs["message"])
        actor = Actor(
            actor_id=EVAL_USER_IDS[tenant_id],
            tenant_id=tenant_id,
            role=_consumer_role(),
        )

        async with scoped_db_session(bundle) as session:
            conv_repo = ConversationRepository(session)
            thread = await conv_repo.create_thread(
                actor,
                tenant_id,
                ConversationThreadCreate(
                    title=f"{THREAD_TITLE_PREFIX} {message[:20]}",
                    initial_user_message=message,
                ),
            )
            thread_id = thread.thread_id

            state: dict[str, Any] = {}
            final_reply = ""
            pending_action: dict[str, Any] | None = None

            event_iter = facade.astream_events(
                actor=actor,
                tenant_id=tenant_id,
                thread_id=thread_id,
                user_message=message,
                session=session,
            )
            async for evt in event_iter:
                etype = evt.get("type")
                if etype == "node_end" and isinstance(evt.get("patch"), dict):
                    state.update(evt["patch"])
                elif etype == "reply":
                    final_reply = str(evt.get("text") or "")
                elif etype == "confirmation_required":
                    pending_action = evt.get("pending_action")

        return {
            "final_reply": final_reply,
            "intent_candidate": state.get("intent_candidate"),
            "intent_hint": state.get("intent_hint"),
            "tool_executions": state.get("tool_executions") or [],
            "rag_hits": state.get("rag_hits") or [],
            "action_kind": state.get("action_kind"),
            "escalated": bool(state.get("escalated")),
            "confirmation_required": pending_action is not None,
            "pending_action": pending_action,
        }

    return target


def _consumer_role() -> Any:
    from app.application.schemas.identity import Role

    return Role.CONSUMER

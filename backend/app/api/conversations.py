"""会话 HTTP 接口（T6 会话域 CRUD）。

提供：
    POST   /api/conversations                           创建线程
    GET    /api/conversations                           列线程（consumer=只看自己；staff/admin=同租户所有）
    GET    /api/conversations/{thread_id}               线程详情
    POST   /api/conversations/{thread_id}/messages      追加消息
    GET    /api/conversations/{thread_id}/messages      列消息（升序 append-only list）

所有接口强制：X-Tenant-Id + JWT Actor Middleware；越权一律 ResourceNotFound（404）。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import require_actor
from app.application.schemas.conversation import (
    ConversationMessageCreate,
    ConversationMessageRead,
    ConversationThreadCreate,
    ConversationThreadRead,
)
from app.domain.repositories.conversation import ConversationRepository
from app.domain.repositories.identity import Actor
from app.infrastructure.db.engine import scoped_db_session

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


async def _session(request: Request) -> AsyncSession:
    async with scoped_db_session(request.app.state.bundle.infra) as sess:
        yield sess


@router.post("", response_model=ConversationThreadRead, status_code=201)
async def create_conversation(
    payload: ConversationThreadCreate,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> ConversationThreadRead:
    """创建一条新会话线程；title/initial_message 都可空。"""
    repo = ConversationRepository(session)
    try:
        row = await repo.create_thread(actor, actor.tenant_id, payload)
    except Exception as exc:
        if _is_not_found(exc):
            raise HTTPException(status_code=404, detail="not found") from exc
        raise
    await session.commit()
    return row


@router.get("", response_model=list[ConversationThreadRead])
async def list_conversations(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> list[ConversationThreadRead]:
    repo = ConversationRepository(session)
    rows = await repo.list_threads(actor, actor.tenant_id, limit=limit, offset=offset)
    await session.commit()
    return rows


@router.get("/{thread_id}", response_model=ConversationThreadRead)
async def get_conversation(
    thread_id: str,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> ConversationThreadRead:
    repo = ConversationRepository(session)
    try:
        row = await repo.get_thread(actor, actor.tenant_id, thread_id)
    except Exception as exc:
        if _is_not_found(exc):
            raise HTTPException(status_code=404, detail="not found") from exc
        raise
    await session.commit()
    return row


@router.post(
    "/{thread_id}/messages",
    response_model=ConversationMessageRead,
    status_code=201,
)
async def append_message(
    thread_id: str,
    payload: ConversationMessageCreate,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> ConversationMessageRead:
    """追加消息（human/agent/tool）。consumer 仅能 role=human；staff/admin 不限。"""
    repo = ConversationRepository(session)
    try:
        msg = await repo.append_message(actor, actor.tenant_id, thread_id, payload)
    except Exception as exc:
        if _is_not_found(exc):
            raise HTTPException(status_code=404, detail="not found") from exc
        raise
    await session.commit()
    return msg


@router.get("/{thread_id}/messages", response_model=list[ConversationMessageRead])
async def list_messages(
    thread_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    since_message_id: UUID | None = Query(default=None),
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> list[ConversationMessageRead]:
    repo = ConversationRepository(session)
    try:
        rows = await repo.list_messages(
            actor,
            actor.tenant_id,
            thread_id,
            limit=limit,
            since_message_id=since_message_id,
        )
    except Exception as exc:
        if _is_not_found(exc):
            raise HTTPException(status_code=404, detail="not found") from exc
        raise
    await session.commit()
    return rows


# ---- helpers ----


def _is_not_found(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    return str(code) in {"RESOURCE_NOT_FOUND", "PERMISSION_DENIED"}

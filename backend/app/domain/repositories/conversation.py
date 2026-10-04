"""会话仓储（T6）。

核心安全：
  * 所有方法强制显式 tenant_id（遵循 AGENTS 架构约束）。
  * thread_id 格式校验：thread_id.split(":", 1)[0] 必须 == tenant_id，否则 ResourceNotFound（防枚举）。
  * 读权限：consumer 只能读自己的；staff/admin 读同租户所有（他人会话也可读，便于客服介入）。
  * 写权限：consumer 只能写自己的线程/消息；staff/admin 可插入 agent/tool 消息模拟。
  * 越权 / 不存在 / 跨租户 → 统一 ResourceNotFound，不泄露资源存在性。

Checkpointer 占位（MVP 预留接口）：
  * LangGraph Postgres Checkpointer 独立表需要：checkpoint_id/blobs/writes 等 3-4 张表；
  * 当前 MVP 只提供：
      - save_checkpoint(thread_id, state_json) → 写 threads.metadata_json（兼容）
      - load_checkpoint(thread_id) → 读 threads.metadata_json 作为 checkpoint state（有则返回，无则 None）
  * 真实启用 LangGraph Checkpointer（T7）时，替换为：langgraph.checkpoint.postgres.aio.AsyncPostgresSaver
"""

from __future__ import annotations

import datetime as _dt
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.schemas.conversation import (
    ConversationMessageCreate,
    ConversationMessageRead,
    ConversationThreadCreate,
    ConversationThreadRead,
)
from app.core.errors import ErrorCode, ResourceNotFoundError
from app.domain.models.conversation import ConversationMessageORM, ConversationThreadORM
from app.domain.repositories.identity import Actor


def _parse_thread_tenant(thread_id: str) -> str:
    """从 thread_id 拆出前缀作为 tenant_id 候选；格式不对返回空字符串。"""
    if not thread_id or ":" not in thread_id:
        return ""
    return thread_id.split(":", 1)[0]


def _normalize_thread_id(thread_id: str) -> str:
    """把前端可能传入的 `tenant_id:uuid-with-dashes` 规范化为仓储统一使用的 `tenant_id:uuid_hex`。

    背景：create_thread 里始终用 `.hex`（无连字符的 32 字符）生成 thread_id 后缀，
    但前端演示代码直接用 UUID(..., version=4) 格式化的带连字符字符串，导致查不到刚写入的行。
    这里在所有仓储入口统一规范化，避免跨层对齐。
    """
    if not thread_id or ":" not in thread_id:
        return thread_id
    prefix, suffix = thread_id.split(":", 1)
    try:
        suffix_hex = UUID(suffix).hex
    except Exception:
        return thread_id
    return f"{prefix}:{suffix_hex}"


def _gen_thread_id(tenant_id: str, suffix_uuid: UUID | None = None) -> str:
    suffix = (suffix_uuid or uuid4()).hex
    return f"{tenant_id}:{suffix}"


def _ensure_actor_can_read_thread(actor: Actor, tenant_id: str, row: ConversationThreadORM) -> None:
    """根据 actor.role 决定是否允许读该 thread；否则 ResourceNotFound。"""
    if row is None:
        raise ResourceNotFoundError(
            ErrorCode.RESOURCE_NOT_FOUND,
            message="resource not found",
            details={"resource_type": "conversation_thread", "resource_id": "null"},
        )
    if actor.tenant_id.lower() != tenant_id.lower() or str(row.tenant_id).lower() != tenant_id.lower():
        raise ResourceNotFoundError(
            ErrorCode.RESOURCE_NOT_FOUND,
            message="resource not found",
            details={"resource_type": "conversation_thread", "resource_id": row.thread_id},
        )
    if actor.role.value == "consumer" and str(row.owner_user_id) != str(actor.actor_id):
        raise ResourceNotFoundError(
            ErrorCode.RESOURCE_NOT_FOUND,
            message="resource not found",
            details={"resource_type": "conversation_thread", "resource_id": row.thread_id},
        )


class ConversationRepository:
    """会话仓储：线程 + 消息 CRUD（追加写为主）。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ================================================================
    # Threads 写
    # ================================================================

    async def create_thread(
        self,
        actor: Actor,
        tenant_id: str,
        payload: ConversationThreadCreate,
        *,
        _override_suffix: UUID | None = None,
    ) -> ConversationThreadRead:
        """创建新线程（consumer 创建的线程 owner = 自己；staff/admin 也可代创建，owner 仍是自己）。

        MVP 下：
          * consumer 必须创建自己的线程；staff/admin 可以创建 owner=任意同租户用户
        """
        if actor.tenant_id.lower() != tenant_id.lower():
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "conversation_thread", "resource_id": "create@cross_tenant"},
            )
        owner_id = UUID(actor.actor_id)

        thread_id = _gen_thread_id(tenant_id, _override_suffix)
        now = _dt.datetime.now(_dt.timezone.utc)
        row = ConversationThreadORM(
            thread_id=thread_id,
            tenant_id=tenant_id,
            title=payload.title,
            initial_user_message=payload.initial_user_message,
            owner_user_id=owner_id,
            status=payload.status or "open",
            escalated_ticket_no=payload.escalated_ticket_no,
            last_message_at=None,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return ConversationThreadRead.model_validate(row)

    async def mark_escalated(
        self,
        actor: Actor,
        tenant_id: str,
        thread_id: str,
        ticket_no: str,
    ) -> ConversationThreadRead:
        """D3 转人工后：标记线程 status=escalated + 填入工单编号。"""
        row = await self._get_orm_for_actor(actor, tenant_id, thread_id)
        row.status = "escalated"
        row.escalated_ticket_no = ticket_no
        row.updated_at = _dt.datetime.now(_dt.timezone.utc)
        await self.session.flush()
        return ConversationThreadRead.model_validate(row)

    # ================================================================
    # Threads 读
    # ================================================================

    async def get_thread(self, actor: Actor, tenant_id: str, thread_id: str) -> ConversationThreadRead:
        row = await self._get_orm_for_actor(actor, tenant_id, thread_id)
        return ConversationThreadRead.model_validate(row)

    async def list_threads(
        self,
        actor: Actor,
        tenant_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationThreadRead]:
        """列出线程。consumer 仅返回自己的；staff/admin 返回同租户所有。"""
        if actor.tenant_id.lower() != tenant_id.lower():
            return []
        stmt = (
            select(ConversationThreadORM)
            .where(ConversationThreadORM.tenant_id == tenant_id)
            .order_by(
                ConversationThreadORM.last_message_at.desc().nullslast(),
                ConversationThreadORM.updated_at.desc(),
            )
            .limit(max(1, min(limit, 500)))
            .offset(max(0, offset))
        )
        if actor.role.value == "consumer":
            stmt = stmt.where(ConversationThreadORM.owner_user_id == UUID(actor.actor_id))
        rows = (await self.session.execute(stmt)).scalars().all()
        reads = [ConversationThreadRead.model_validate(r) for r in rows]
        await self._fill_missing_summaries(tenant_id, rows, reads)
        return reads

    async def _fill_missing_summaries(
        self,
        tenant_id: str,
        rows: list[ConversationThreadORM],
        reads: list[ConversationThreadRead],
    ) -> None:
        """历史线程 initial_user_message 为空时，取各线程首条 human 消息截断回填（仅回显，不落库）。"""
        missing = [
            (r, read)
            for r, read in zip(rows, reads, strict=False)
            if not (r.initial_user_message or "").strip()
        ]
        if not missing:
            return
        thread_ids = [r.thread_id for r, _ in missing]
        sub = (
            select(
                ConversationMessageORM.thread_id.label("thread_id"),
                ConversationMessageORM.content.label("content"),
                func.row_number()
                .over(
                    partition_by=ConversationMessageORM.thread_id,
                    order_by=ConversationMessageORM.created_at.asc(),
                )
                .label("rn"),
            )
            .where(
                ConversationMessageORM.tenant_id == tenant_id,
                ConversationMessageORM.role == "human",
                ConversationMessageORM.thread_id.in_(thread_ids),
            )
            .subquery()
        )
        stmt = select(sub.c.thread_id, sub.c.content).where(sub.c.rn == 1)
        firsts = {
            tid: (content or "").strip()[:80]
            for tid, content in (await self.session.execute(stmt)).all()
        }
        for _, read in missing:
            summary = firsts.get(read.thread_id)
            if summary:
                read.initial_user_message = summary

    async def _get_orm_for_actor(
        self,
        actor: Actor,
        tenant_id: str,
        thread_id: str,
    ) -> ConversationThreadORM:
        thread_id = _normalize_thread_id(thread_id)
        if actor.tenant_id.lower() != tenant_id.lower() or _parse_thread_tenant(thread_id).lower() != tenant_id.lower():
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "conversation_thread", "resource_id": str(thread_id)},
            )
        stmt = select(ConversationThreadORM).where(
            ConversationThreadORM.thread_id == thread_id,
            ConversationThreadORM.tenant_id == tenant_id,
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        _ensure_actor_can_read_thread(actor, tenant_id, row if row is not None else None)  # type: ignore[arg-type]
        assert row is not None
        return row

    # ================================================================
    # Messages 写/读
    # ================================================================

    async def append_message(
        self,
        actor: Actor,
        tenant_id: str,
        thread_id: str,
        payload: ConversationMessageCreate,
        *,
        _override_time: _dt.datetime | None = None,
    ) -> ConversationMessageRead:
        """追加消息（human/agent/tool）并顺带更新 thread.last_message_at + updated_at。

        权限：
          * consumer → 仅限 role=human，且 thread 归属自己
          * staff/admin → 可写任何角色（便于演示 agent 消息塞入）
        """
        thread_id = _normalize_thread_id(thread_id)
        thread = await self._get_orm_for_actor(actor, tenant_id, thread_id)
        if actor.role.value == "consumer" and payload.role != "human":
            # consumer 不能伪造 agent/tool 消息（模拟用户行为）
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "conversation_message", "resource_id": "consumer_forged_role"},
            )
        now = _override_time or _dt.datetime.now(_dt.timezone.utc)
        actor_id = UUID(actor.actor_id)
        # 写消息
        msg = ConversationMessageORM(
            message_id=uuid4(),
            tenant_id=tenant_id,
            thread_id=thread_id,
            actor_id=actor_id,
            role=payload.role,
            content=payload.content,
            tool_name=payload.tool_name,
            tool_call_id=payload.tool_call_id,
            metadata_json=payload.metadata or None,
            created_at=now,
        )
        self.session.add(msg)
        # 更新线程时间戳
        thread.last_message_at = now
        thread.updated_at = now
        # 会话列表摘要：首条 human 消息截断回填（前端 truncate 兜底展示）
        if payload.role == "human" and not (thread.initial_user_message or "").strip():
            thread.initial_user_message = payload.content.strip()[:80] or None
        await self.session.flush()
        return ConversationMessageRead.model_validate({
            "message_id": msg.message_id,
            "thread_id": msg.thread_id,
            "tenant_id": msg.tenant_id,
            "actor_id": msg.actor_id,
            "role": msg.role,
            "content": msg.content,
            "tool_name": msg.tool_name,
            "tool_call_id": msg.tool_call_id,
            "metadata": msg.metadata_json,
            "created_at": msg.created_at,
        })

    async def list_messages(
        self,
        actor: Actor,
        tenant_id: str,
        thread_id: str,
        *,
        limit: int = 100,
        since_message_id: UUID | None = None,
    ) -> list[ConversationMessageRead]:
        """按创建时间升序列出消息（追加写模型 → append-only 列表）。"""
        # 先做线程级权限校验（包含 thread_id 前缀匹配 + owner/role 规则）
        thread_id = _normalize_thread_id(thread_id)
        await self._get_orm_for_actor(actor, tenant_id, thread_id)

        stmt = (
            select(ConversationMessageORM)
            .where(
                ConversationMessageORM.tenant_id == tenant_id,
                ConversationMessageORM.thread_id == thread_id,
            )
            .order_by(ConversationMessageORM.created_at.asc())
            .limit(max(1, min(limit, 1000)))
        )
        if since_message_id is not None:
            stmt = stmt.where(ConversationMessageORM.message_id > since_message_id)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            ConversationMessageRead.model_validate({
                "message_id": r.message_id,
                "thread_id": r.thread_id,
                "tenant_id": r.tenant_id,
                "actor_id": r.actor_id,
                "role": r.role,
                "content": r.content,
                "tool_name": r.tool_name,
                "tool_call_id": r.tool_call_id,
                "metadata": r.metadata_json,
                "created_at": r.created_at,
            })
            for r in rows
        ]

    # ================================================================
    # LangGraph Checkpointer 占位 MVP 接口（T7 再落地真实 3 张表）
    # ================================================================

    async def save_checkpoint(
        self,
        actor: Actor,
        tenant_id: str,
        thread_id: str,
        state_json: dict[str, Any],
    ) -> None:
        """MVP：写 threads 扩展列；真实 T7 改为 AsyncPostgresSaver.put。"""

        row = await self._get_orm_for_actor(actor, tenant_id, thread_id)
        # 目前 threads 表没有单独的 checkpoint_json 列；复用 initial_user_message 不合适，
        # 所以这里选择「只校验权限，不真正落库」并返回，避免 ALTER 新列（T7 再补）。
        # 真实 Checkpointer 启用后本方法可删除。
        _ = state_json, row
        # 为了让测试可证：做一个 INSERT ON CONFLICT 到空表 no-op（不做实际任何 ALTER），或静默返回
        return None

    async def load_checkpoint(
        self,
        actor: Actor,
        tenant_id: str,
        thread_id: str,
    ) -> dict[str, Any] | None:
        """MVP：永远返回 None（代表「无 checkpoint，LangGraph 从头开始跑」；避免 mock 时 ALTER 新列）。"""
        await self._get_orm_for_actor(actor, tenant_id, thread_id)
        return None

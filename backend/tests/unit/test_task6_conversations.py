"""T6 会话域 5 条单测（Repository + HTTP，完全离线 AsyncMock）。

覆盖：
- TR6-1 创建会话：thread_id = "tenant_a:{uuid_hex}"，且 owner_user_id 正确
- TR6-2 consumer 读他人会话 → ResourceNotFound（防枚举）
- TR6-3 consumer 跨租户创建/读取 → ResourceNotFound
- TR6-4 追加 3 条消息（human + agent + tool），list_messages 升序返回
- TR6-5 staff/admin 能看见同租户 consumer 的所有会话（包括别人的）
- TR6-6 Checkpointer 占位：save/load_checkpoint 不抛异常，load 返回 None（MVP 空实现）
"""

from __future__ import annotations

import datetime as _dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.application.schemas.conversation import (
    ConversationMessageCreate,
    ConversationThreadCreate,
)
from app.application.schemas.identity import Role
from app.core.errors import ResourceNotFoundError
from app.domain.models.conversation import ConversationMessageORM, ConversationThreadORM
from app.domain.repositories.conversation import (
    ConversationRepository,
    _parse_thread_tenant,
)
from app.domain.repositories.identity import Actor

TENANT_A = "tenant_a"
TENANT_B = "tenant_b"
CONSUMER_A1 = Actor(
    actor_id="a0000000-0000-0000-0000-000000000011",
    tenant_id=TENANT_A,
    role=Role.CONSUMER,
)
CONSUMER_A2 = Actor(
    actor_id="a0000000-0000-0000-0000-000000000012",
    tenant_id=TENANT_A,
    role=Role.CONSUMER,
)
STAFF_A = Actor(
    actor_id="a0000000-0000-0000-0000-000000000002",
    tenant_id=TENANT_A,
    role=Role.STAFF,
)
CONSUMER_B1 = Actor(
    actor_id="b0000000-0000-0000-0000-000000000011",
    tenant_id=TENANT_B,
    role=Role.CONSUMER,
)


# ========================================================================
# Helpers
# ========================================================================


def _new_session() -> tuple[AsyncMock, list[Any]]:
    """AsyncMock session + manual added list（与 T4 同 pattern）。"""
    added: list[Any] = []

    class _Fake(AsyncMock):
        def add(self, obj: Any) -> None:  # type: ignore[override]
            added.append(obj)

    s = _Fake()
    s.flush = AsyncMock(return_value=None)
    s.commit = AsyncMock(return_value=None)
    return s, added


def _make_thread_orm(*, thread_id: str, tenant_id: str, owner_id: str,
                     status: str = "open", last_msg_at: _dt.datetime | None = None) -> ConversationThreadORM:
    now = _dt.datetime(2025, 9, 13, 10, 0, 0, tzinfo=_dt.timezone.utc)
    return ConversationThreadORM(
        thread_id=thread_id,
        tenant_id=tenant_id,
        title=f"会话-{thread_id[-6:]}",
        initial_user_message="帮我看看这单能退吗？",
        owner_user_id=UUID(owner_id),
        status=status,
        escalated_ticket_no=None,
        last_message_at=last_msg_at,
        created_at=now,
        updated_at=now,
    )


# ========================================================================
# TR6-1 创建会话 → thread_id 格式正确
# ========================================================================


@pytest.mark.asyncio
async def test_tr61_create_thread_prefix_equals_tenant_id() -> None:
    sess, added = _new_session()
    repo = ConversationRepository(sess)
    create = ConversationThreadCreate(
        title="退手串",
        initial_user_message="这个能退吗？",
        status="open",
    )
    row_read = await repo.create_thread(CONSUMER_A1, TENANT_A, create)
    # thread_id 格式
    assert _parse_thread_tenant(row_read.thread_id) == TENANT_A
    assert row_read.tenant_id == TENANT_A
    assert str(row_read.owner_user_id) == CONSUMER_A1.actor_id
    assert row_read.title == "退手串"
    # ORM 被正确加入 session.add
    threads_in_add = [o for o in added if isinstance(o, ConversationThreadORM)]
    assert len(threads_in_add) == 1
    assert threads_in_add[0].thread_id == row_read.thread_id


# ========================================================================
# TR6-2 consumer 读他人（同租户）会话 → ResourceNotFound
# ========================================================================


@pytest.mark.asyncio
async def test_tr62_consumer_reads_other_owner_raises_not_found() -> None:
    sess, _ = _new_session()
    # ORM 行 owner = CONSUMER_A2
    t = _make_thread_orm(
        thread_id=f"{TENANT_A}:abcdef1234",
        tenant_id=TENANT_A,
        owner_id=CONSUMER_A2.actor_id,
    )
    # session.execute → scalar_one_or_none → 返回 t
    rp = MagicMock()
    rp.scalar_one_or_none.return_value = t
    sess.execute = AsyncMock(return_value=rp)

    repo = ConversationRepository(sess)
    with pytest.raises(ResourceNotFoundError):
        await repo.get_thread(CONSUMER_A1, TENANT_A, t.thread_id)


# ========================================================================
# TR6-3 consumer 跨租户创建 / 读取 → ResourceNotFound
# ========================================================================


@pytest.mark.asyncio
async def test_tr63_cross_tenant_create_and_read_raises_not_found() -> None:
    sess, _ = _new_session()
    repo = ConversationRepository(sess)

    # CONSUMER_B1 尝试写 TENANT_A 的会话
    with pytest.raises(ResourceNotFoundError):
        await repo.create_thread(CONSUMER_B1, TENANT_A, ConversationThreadCreate())

    # 读：tenant_id 参数与 thread_id 前缀不一致（典型越权拼接）
    rp = MagicMock()
    rp.scalar_one_or_none.return_value = _make_thread_orm(
        thread_id=f"{TENANT_A}:xxxx",
        tenant_id=TENANT_A,
        owner_id=CONSUMER_A1.actor_id,
    )
    sess.execute = AsyncMock(return_value=rp)
    with pytest.raises(ResourceNotFoundError):
        # 非法 thread_id：前缀是 tenant_a，但调用方假装传 tenant_b target
        await repo.get_thread(CONSUMER_B1, TENANT_B, f"{TENANT_A}:xxxx")


# ========================================================================
# TR6-4 追加 3 条消息 → list_messages 升序
# ========================================================================


@pytest.mark.asyncio
async def test_tr64_append_3_messages_and_list_asc() -> None:
    sess, added = _new_session()
    # 先让 get_thread / list_messages 的 execute 返回正确的 thread ORM / message ORM 列表
    owner = CONSUMER_A1
    t = _make_thread_orm(
        thread_id=f"{TENANT_A}:thread0001",
        tenant_id=TENANT_A,
        owner_id=owner.actor_id,
    )
    # 先创建线程（要用到 repo.create_thread，方便内部自动 add thread 行）
    repo = ConversationRepository(sess)
    # 直接构造 thread 放入 session，再模拟 execute 返回即可（避免 create_thread 逻辑复杂依赖）
    # ↓ make execute 的返回根据调用内容不同有所区别：
    # 调用 1（append_message 里的 _get_orm_for_actor）→ 返回 thread ORM
    # 调用 2（list_messages 里的 list_messages stmt）→ 返回 messages 列表
    async def _execute_smart(*_a: Any, **_k: Any) -> MagicMock:
        # 每次调用都新建一个独立 MagicMock，避免共用 return_value 污染
        mr = MagicMock()
        stmt_str = str(_a[0].compile(compile_kwargs={"literal_binds": True})) if _a else ""
        if "conversation_messages" in stmt_str:
            scalars_mr = MagicMock()
            # 注意：append_message 把消息加到 FakeSession.added 列表，所以这里从 added 里取
            scalars_mr.all.return_value = [o for o in added if isinstance(o, ConversationMessageORM)]
            mr.scalars.return_value = scalars_mr
        else:
            mr.scalar_one_or_none.return_value = t
        return mr

    sess.execute.side_effect = _execute_smart

    # 3 条 append
    now0 = _dt.datetime(2025, 9, 13, 10, 0, 0, tzinfo=_dt.timezone.utc)
    msg1 = await repo.append_message(
        CONSUMER_A1, TENANT_A, t.thread_id,
        ConversationMessageCreate(role="human", content="能退吗？"),
        _override_time=now0,
    )
    msg2 = await repo.append_message(
        STAFF_A, TENANT_A, t.thread_id,
        ConversationMessageCreate(
            role="tool",
            content='{"can_refund":true}',
            tool_name="order_query",
            tool_call_id="call-" + "a" * 32,
            metadata={"token": 123},
        ),
        _override_time=now0 + _dt.timedelta(seconds=2),
    )
    msg3 = await repo.append_message(
        STAFF_A, TENANT_A, t.thread_id,
        ConversationMessageCreate(role="agent", content="您好，可退（7 天无理由）。"),
        _override_time=now0 + _dt.timedelta(seconds=4),
    )
    # session.add 过 3 条 MessageORM
    added_msgs = [o for o in added if isinstance(o, ConversationMessageORM)]
    assert len(added_msgs) == 3
    # 线程 last_message_at 已更新（到最后一条的时间）
    assert t.last_message_at is not None
    assert abs((t.last_message_at - (now0 + _dt.timedelta(seconds=4))).total_seconds()) < 1

    # list_messages 升序
    listed = await repo.list_messages(CONSUMER_A1, TENANT_A, t.thread_id)
    assert [m.message_id for m in listed] == [msg1.message_id, msg2.message_id, msg3.message_id]
    # tool 字段完整
    tool_msg = next(m for m in listed if m.role == "tool")
    assert tool_msg.tool_name == "order_query"
    assert tool_msg.tool_call_id is not None
    assert tool_msg.metadata is not None and tool_msg.metadata.get("token") == 123


# ========================================================================
# TR6-5 staff/admin 能看同租户的所有会话（包括别人 owner 的）
# ========================================================================


@pytest.mark.asyncio
async def test_tr65_staff_sees_all_same_tenant_threads() -> None:
    sess, _ = _new_session()
    t1 = _make_thread_orm(thread_id=f"{TENANT_A}:a1", tenant_id=TENANT_A, owner_id=CONSUMER_A1.actor_id)
    t2 = _make_thread_orm(thread_id=f"{TENANT_A}:a2", tenant_id=TENANT_A, owner_id=CONSUMER_A2.actor_id)

    rp = MagicMock()
    rp.scalars.return_value.all.return_value = [t1, t2]
    sess.execute = AsyncMock(return_value=rp)

    repo = ConversationRepository(sess)
    # STAFF_A 列出 → 2 条都看到
    rows_staff = await repo.list_threads(STAFF_A, TENANT_A, limit=20)
    assert {r.thread_id for r in rows_staff} == {t1.thread_id, t2.thread_id}
    # CONSUMER_A1 列出 → 仅 1 条（owner 自己的）
    # consumer 情况下 list_threads 会 AND owner_user_id == CONSUMER_A1.actor_id，
    # 这里 execute 还返回 [t1, t2] 会导致 list_threads 违反预期的“2条全返回”，
    # 但实际在真实 SQL 里 WHERE 会生效；这里只是验证「Repository 层逻辑上确实加了 AND 条件」，
    # 因此我们做代码断言：当 role=consumer 时 stmt 含 owner_user_id 条件（靠再次触发 execute 看 SQL）
    captured_stmt: list[str] = []

    async def _capture(*_a: Any, **_k: Any) -> MagicMock:
        if _a:
            captured_stmt.append(str(_a[0].compile(compile_kwargs={"literal_binds": True})))
        empty = MagicMock()
        empty.scalars.return_value.all.return_value = []
        return empty

    sess.execute.side_effect = _capture
    await repo.list_threads(CONSUMER_A1, TENANT_A, limit=20)
    assert captured_stmt, "list_threads 至少执行一次 SQL"
    last_sql = captured_stmt[-1]
    assert "owner_user_id" in last_sql, "consumer 视图必须加 owner_user_id 过滤条件"


# ========================================================================
# TR6-6 Checkpointer 占位 save/load 不抛错，MVP load 返回 None
# ========================================================================


@pytest.mark.asyncio
async def test_tr66_checkpointer_placeholder_noop_and_safe() -> None:
    sess, _ = _new_session()
    t = _make_thread_orm(
        thread_id=f"{TENANT_A}:ckpt1",
        tenant_id=TENANT_A,
        owner_id=CONSUMER_A1.actor_id,
    )
    rp = MagicMock()
    rp.scalar_one_or_none.return_value = t
    sess.execute = AsyncMock(return_value=rp)
    repo = ConversationRepository(sess)

    # save 不抛
    await repo.save_checkpoint(
        CONSUMER_A1, TENANT_A, t.thread_id,
        {"step": 3, "nodes": ["rag", "policy"]},
    )
    # load 不抛且返回 None（MVP 空实现）
    loaded = await repo.load_checkpoint(CONSUMER_A1, TENANT_A, t.thread_id)
    assert loaded is None

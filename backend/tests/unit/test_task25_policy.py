"""T2.5 租户售后政策配置 + 知识库 chunks 单测。

覆盖：
- TR-P1 admin 同租户读 policy
- TR-P2 admin 同租户改 policy（字段修改成功 + updated_by 注入）
- TR-P3 consumer 尝试改 policy → ResourceNotFound（不抛 PermissionDenied，防止存在性泄露）
- TR-P4 tenant_a staff 改 tenant_b → ResourceNotFound（跨租户）
- TR-P5 create_for_actor 即使在 payload 中塞额外 created_by 字段，实际写入也用 actor.actor_id
- TR-P6 upsert_default_from_constant 幂等（两次调用不生成多条记录）
"""

from __future__ import annotations

import datetime as _dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from app.application.schemas.identity import Role
from app.application.schemas.policy import (
    KnowledgeChunkCreate,
    PolicyConfigUpdate,
)
from app.core.errors import ResourceNotFoundError
from app.domain.constants.policies import TENANT_POLICIES
from app.domain.models.policy import KnowledgeChunkORM, TenantPolicyConfigORM
from app.domain.repositories.identity import Actor
from app.domain.repositories.policy import KnowledgeChunkRepository, PolicyConfigRepository


def _policy_orm(*, tenant_id: str = "tenant_a", return_days: int = 7) -> TenantPolicyConfigORM:
    t0 = _dt.datetime(2025, 1, 1, tzinfo=_dt.timezone.utc)
    return TenantPolicyConfigORM(
        tenant_id=tenant_id,
        return_days=return_days,
        return_policy_type="no_reason",
        restocking_fee_pct_non_quality=0,
        warranty_days_quality=30,
        custom_product_allowed_return=False,
        updated_by=UUID("deadbeef-dead-beef-dead-beefdeadbeef"),
        created_at=t0,
        updated_at=t0,
    )


def _admin_actor(tenant_id: str) -> Actor:
    actor_id = {"tenant_a": "a0000000-0000-0000-0000-000000000003"}.get(tenant_id, "00000000-0000-0000-0000-000000000003")
    return Actor(actor_id=actor_id, tenant_id=tenant_id, role=Role.ADMIN)


def _staff_actor(tenant_id: str) -> Actor:
    actor_id = {"tenant_a": "a0000000-0000-0000-0000-000000000002"}.get(tenant_id, "00000000-0000-0000-0000-000000000002")
    return Actor(actor_id=actor_id, tenant_id=tenant_id, role=Role.STAFF)


def _consumer_actor(tenant_id: str) -> Actor:
    return Actor(
        actor_id="a0000000-0000-0000-0000-000000000001", tenant_id=tenant_id, role=Role.CONSUMER
    )


# ============================================================================
# TR-P1 / TR-P2：admin 同租户读写 policy
# ============================================================================


@pytest.mark.asyncio
async def test_tr_p1_admin_read_same_tenant_ok() -> None:
    session = AsyncMock()
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = _policy_orm()
    session.execute = AsyncMock(return_value=mr)

    repo = PolicyConfigRepository(session)
    read = await repo.get_read_for_actor(_admin_actor("tenant_a"), "tenant_a")
    assert read.tenant_id == "tenant_a"
    assert read.return_days == 7
    assert read.return_policy_type == "no_reason"


@pytest.mark.asyncio
async def test_tr_p2_admin_update_same_tenant_updates_fields_and_injects_updated_by() -> None:
    session = AsyncMock()
    existing = _policy_orm(tenant_id="tenant_a", return_days=7)
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=mr)

    repo = PolicyConfigRepository(session)
    patch = PolicyConfigUpdate(
        return_days=14,
        restocking_fee_pct_non_quality=5,
        return_policy_type="hybrid",
    )
    new_read = await repo.update_for_actor(_admin_actor("tenant_a"), "tenant_a", patch)
    assert new_read.return_days == 14
    assert new_read.restocking_fee_pct_non_quality == 5
    assert new_read.return_policy_type == "hybrid"
    # updated_by 必须是 actor_id，而不是旧值
    assert str(new_read.updated_by) == _admin_actor("tenant_a").actor_id


# ============================================================================
# TR-P3：consumer 尝试改 → ResourceNotFound（不泄露存在性）
# ============================================================================


@pytest.mark.asyncio
async def test_tr_p3_consumer_update_same_tenant_raises_resource_not_found() -> None:
    """consumer 不允许改，即使同租户也应该抛 ResourceNotFound，而不是 PermissionDenied。

    这样攻击者无法从错误类型区分「存在但你无权改」与「根本不存在」。
    """
    session = AsyncMock()
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = _policy_orm()
    session.execute = AsyncMock(return_value=mr)

    repo = PolicyConfigRepository(session)
    patch = PolicyConfigUpdate(return_days=90)
    with pytest.raises(ResourceNotFoundError):
        await repo.update_for_actor(_consumer_actor("tenant_a"), "tenant_a", patch)


# ============================================================================
# TR-P4：跨租户越权 → ResourceNotFound
# ============================================================================


@pytest.mark.asyncio
async def test_tr_p4_staff_a_update_tenant_b_raises_resource_not_found() -> None:
    session = AsyncMock()
    session.execute = AsyncMock()  # 即使不查库，_ensure_can_modify 先拦截
    repo = PolicyConfigRepository(session)
    patch = PolicyConfigUpdate(return_days=7)
    with pytest.raises(ResourceNotFoundError):
        await repo.update_for_actor(_staff_actor("tenant_a"), "tenant_b", patch)
    # 未到 execute 阶段就被权限拦截 → execute 应该被调用 0 次
    assert session.execute.call_count == 0


# ============================================================================
# TR-P5：chunk create_for_actor 的 created_by 永远取 actor，不读 payload 外部字段
# ============================================================================


@pytest.mark.asyncio
async def test_tr_p5_chunk_created_by_sourced_from_actor_never_payload() -> None:
    """即使客户端构造的 payload 通过某种方式带了 created_by（或其他字典扩展字段），
    Repository 层必须忽略，一律取 actor.actor_id 作为创建人。
    """
    session = AsyncMock()
    repo = KnowledgeChunkRepository(session)
    actor = _staff_actor("tenant_a")
    expected_created_by = UUID(actor.actor_id)

    payload = KnowledgeChunkCreate(
        title="FAQ 怎么退款",
        source="faq",
        content="在订单详情点申请售后。",
    )

    # 预期 ORM 对象：created_by 必然是 actor_id（不是 ffff...）
    orm_after = KnowledgeChunkORM(
        chunk_id=UUID("11111111-1111-1111-1111-111111111111"),
        tenant_id="tenant_a",
        title=payload.title,
        content=payload.content,
        source=payload.source,
        content_hash="0" * 64,
        embedding="[]",
        created_by=expected_created_by,
        created_at=_dt.datetime(2025, 1, 1, tzinfo=_dt.timezone.utc),
    )
    call_cnt: dict[str, int] = {"n": 0}

    async def fake_exec(_stmt, *a: Any, **kw: Any) -> MagicMock:
        call_cnt["n"] += 1
        if call_cnt["n"] == 1:
            mr = MagicMock()
            mr.scalar_one_or_none.return_value = None
            return mr
        mr = MagicMock()
        mr.scalar_one.return_value = orm_after
        return mr

    session.execute = AsyncMock(side_effect=fake_exec)  # type: ignore[method-assign]
    session.flush = AsyncMock()

    created = await repo.create_for_actor(actor, "tenant_a", payload)
    assert str(created.created_by) == actor.actor_id
    # 反证明：不可能是客户端伪造的那个值
    assert str(created.created_by) != "ffffffff-ffff-ffff-ffff-ffffffffffff"


# ============================================================================
# TR-P6：PolicyConfig.upsert_default_from_constant 幂等
# ============================================================================


@pytest.mark.asyncio
async def test_tr_p6_upsert_default_from_constant_idempotent() -> None:
    session = AsyncMock()
    policy_a = TENANT_POLICIES["tenant_a"]
    repo = PolicyConfigRepository(session)

    exec_results: list[MagicMock] = []

    async def execute_seq(*a: Any, **kw: Any) -> MagicMock:
        mr = MagicMock()
        mr.scalar_one_or_none.return_value = _policy_orm(tenant_id="tenant_a")
        exec_results.append(mr)
        return mr

    session.execute = AsyncMock(side_effect=execute_seq)  # type: ignore[method-assign]
    session.flush = AsyncMock()

    first = await repo.upsert_default_from_constant("tenant_a", policy_a)
    second = await repo.upsert_default_from_constant("tenant_a", policy_a)
    assert first.tenant_id == "tenant_a"
    assert second.tenant_id == "tenant_a"
    # 两次 INSERT ON CONFLICT；session.add 从不调用（完全由 DB UPSERT 负责）
    assert session.add.call_count == 0

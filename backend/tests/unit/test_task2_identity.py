"""Task 2 身份与租户模块单元测试。

覆盖 TR-2.1 ~ TR-2.5 五条验收要求。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import (
    ActorMiddleware,
    RequireActor,
)
from app.application.schemas.identity import (
    DemoTokenBundle,
    Role,
    TenantCreate,
    UserCreate,
    issue_demo_token,
    parse_bearer_token,
    verify_demo_token,
)
from app.config import Settings
from app.core.errors import (
    AuthError,
    ErrorCode,
    ResourceNotFoundError,
)
from app.domain.models.identity import TenantORM, UserORM
from app.domain.repositories.identity import Actor, TenantRepository, UserRepository

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def valid_actor_id() -> str:
    return "a0000000-0000-0000-0000-000000000001"


@pytest.fixture
def valid_tenant_id() -> str:
    return "tenant_a"


@pytest.fixture
def demo_token(test_settings: Settings, valid_actor_id: str, valid_tenant_id: str) -> DemoTokenBundle:
    return issue_demo_token(
        test_settings.security,
        tenant_id=valid_tenant_id,
        actor_id=valid_actor_id,
        role=Role.CONSUMER,
    )


@pytest.fixture
def protected_app(test_settings: Settings) -> FastAPI:
    """构造一个只挂载 ActorMiddleware 的最小 FastAPI 用于测试 HTTP 层。

    不使用 conftest 的 test_app，因为那里的 settings_probe 可能因缺 .env 为 None，
    从而未挂 ActorMiddleware，无法覆盖 TR-2.1/2.2/2.3。
    """
    app = FastAPI(title="identity-test", docs_url=None, redoc_url=None)
    app.add_middleware(ActorMiddleware, settings=test_settings)

    @app.get("/api/debug/me")
    async def debug_me(actor: Actor = RequireActor):
        return {
            "actor_id": actor.actor_id,
            "tenant_id": actor.tenant_id,
            "role": actor.role.value,
        }

    return app


@pytest_asyncio.fixture
async def protected_client(protected_app: FastAPI) -> AsyncClient:
    transport = ASGITransport(app=protected_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ============================================================================
# TR-2.1：有效令牌 + 一致 tenant header → 上下文 actor 正确
# ============================================================================


@pytest.mark.asyncio
async def test_tr21_valid_token_with_matching_tenant_header_sets_actor_context(
    protected_client: AsyncClient,
    test_settings: Settings,
    demo_token: DemoTokenBundle,
    valid_actor_id: str,
    valid_tenant_id: str,
) -> None:
    """合法请求应成功返回且 actor 信息匹配签发时的值。"""
    headers = {
        test_settings.security.tenant_id_header: valid_tenant_id,
        test_settings.security.auth_header: f"Bearer {demo_token.access_token}",
    }
    resp = await protected_client.get("/api/debug/me", headers=headers)
    assert resp.status_code == 200, f"body={resp.text}"
    body = resp.json()
    assert body["actor_id"] == valid_actor_id
    assert body["tenant_id"] == valid_tenant_id
    assert body["role"] == Role.CONSUMER.value


# ============================================================================
# TR-2.2：令牌 tenant_a + header tenant_b → 401 AUTH_TENANT_MISMATCH
# ============================================================================


@pytest.mark.asyncio
async def test_tr22_token_tenant_mismatch_header_returns_401(
    protected_client: AsyncClient,
    test_settings: Settings,
    demo_token: DemoTokenBundle,
) -> None:
    """X-Tenant-Id 与令牌 tenant_id 必须大小写一致地相等，否则 401。"""
    headers = {
        test_settings.security.tenant_id_header: "tenant_b",
        test_settings.security.auth_header: f"Bearer {demo_token.access_token}",
    }
    resp = await protected_client.get("/api/debug/me", headers=headers)
    assert resp.status_code == 401, f"body={resp.text}"
    body = resp.json()
    assert body["code"] == ErrorCode.AUTH_TENANT_MISMATCH.value


# ============================================================================
# TR-2.3：伪造签名 / 过期令牌 → 401 AUTH_TOKEN_INVALID / EXPIRED
# ============================================================================


@pytest.mark.asyncio
async def test_tr23_forged_token_returns_auth_token_invalid(
    protected_client: AsyncClient,
    test_settings: Settings,
    valid_tenant_id: str,
) -> None:
    """完全随机的 token 应该立刻判定为无效。"""
    headers = {
        test_settings.security.tenant_id_header: valid_tenant_id,
        test_settings.security.auth_header: "Bearer invalid-jwt.string.here",
    }
    resp = await protected_client.get("/api/debug/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == ErrorCode.AUTH_TOKEN_INVALID.value


@pytest.mark.asyncio
async def test_tr23_expired_token_returns_auth_token_expired(
    test_settings: Settings,
    protected_client: AsyncClient,
    valid_tenant_id: str,
    valid_actor_id: str,
) -> None:
    """签发 TTL=-2h 的令牌，应立刻判定过期。"""
    expiring = issue_demo_token(
        test_settings.security,
        tenant_id=valid_tenant_id,
        actor_id=valid_actor_id,
        role=Role.CONSUMER,
        extra_ttl=timedelta(hours=-2),
    )
    headers = {
        test_settings.security.tenant_id_header: valid_tenant_id,
        test_settings.security.auth_header: f"Bearer {expiring.access_token}",
    }
    resp = await protected_client.get("/api/debug/me", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == ErrorCode.AUTH_TOKEN_EXPIRED.value


def test_tr23_verify_throws_correct_error_codes(test_settings: Settings, valid_tenant_id: str, valid_actor_id: str) -> None:
    """直接测 verify_demo_token 的分支覆盖。"""
    good = issue_demo_token(test_settings.security, tenant_id=valid_tenant_id, actor_id=valid_actor_id, role=Role.STAFF)
    claims = verify_demo_token(test_settings.security, good.access_token)
    assert claims.tenant_id == valid_tenant_id
    assert claims.role == Role.STAFF

    with pytest.raises(AuthError) as ei:
        verify_demo_token(test_settings.security, "garbage-token")
    assert ei.value.code == ErrorCode.AUTH_TOKEN_INVALID

    with pytest.raises(AuthError) as ei2:
        parse_bearer_token("NotBearer xxx")
    assert ei2.value.code == ErrorCode.AUTH_MISSING


# ============================================================================
# TR-2.4：consumer 取同租户他人 user_id → ResourceNotFound（不泄露存在性）
# ============================================================================


def _make_user_orm(*, tenant_id: str, user_id: str, role: str = "consumer", username: str = "u") -> UserORM:
    import datetime
    from uuid import UUID

    return UserORM(
        user_id=UUID(user_id),
        tenant_id=tenant_id,
        username=username,
        display_name="display",
        email="a@b.com",
        phone="13800000000",
        role=role,
        is_active=True,
        created_at=datetime.datetime(2025, 1, 1),
        updated_at=datetime.datetime(2025, 1, 1),
    )


@pytest.mark.asyncio
async def test_tr24_consumer_reading_other_same_tenant_raises_resource_not_found() -> None:
    """get_read_for_actor 对越权和不存在两种情况都抛 ResourceNotFound。

    关键：**不能**抛 PermissionDenied，因为会向调用方泄露"该资源存在但你无权访问"，
    这正是本项目中「存在性不泄露」的防护点。
    """
    session = AsyncMock(spec=AsyncSession)
    repo = UserRepository(session)

    consumer_actor = Actor(
        actor_id="11111111-1111-1111-1111-111111111111",
        tenant_id="tenant_a",
        role=Role.CONSUMER,
    )
    other_user_id = "22222222-2222-2222-2222-222222222222"

    other = _make_user_orm(tenant_id="tenant_a", user_id=other_user_id, username="other")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = other
    session.execute = AsyncMock(return_value=mock_result)  # type: ignore[method-assign]

    with pytest.raises(ResourceNotFoundError):
        await repo.get_read_for_actor(consumer_actor, other_user_id)

    non_exists_id = "deadbeef-dead-beef-dead-beefdeadbeef"
    mock_result2 = MagicMock()
    mock_result2.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mock_result2)  # type: ignore[method-assign]

    # 两种场景抛出相同的异常类型，保证攻击者无法通过异常类型区分"存在但越权 vs 不存在"
    with pytest.raises(ResourceNotFoundError):
        await repo.get_read_for_actor(consumer_actor, non_exists_id)


@pytest.mark.asyncio
async def test_tr24_staff_can_read_other_same_tenant_user() -> None:
    """staff/admin 在同租户下应该能读其他人（反向断言保证分支覆盖）。"""
    session = AsyncMock(spec=AsyncSession)
    repo = UserRepository(session)
    staff_actor = Actor(
        actor_id="99999999-9999-9999-9999-999999999999",
        tenant_id="tenant_a",
        role=Role.STAFF,
    )
    other_user_id = "22222222-2222-2222-2222-222222222222"
    other = _make_user_orm(tenant_id="tenant_a", user_id=other_user_id, username="other")
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = other
    session.execute = AsyncMock(return_value=mock_result)  # type: ignore[method-assign]
    read = await repo.get_read_for_actor(staff_actor, other_user_id)
    assert read.user_id == other_user_id
    assert read.tenant_id == "tenant_a"


# ============================================================================
# TR-2.5：种子脚本幂等性（Repository 层面 upsert 两次行为一致）
# ============================================================================


@pytest.mark.asyncio
async def test_tr25_tenant_upsert_twice_is_idempotent() -> None:
    """TenantRepository.upsert_by_id 连续两次相同 payload，应不触发多次 INSERT。"""
    captured_calls: list[str] = []
    session = AsyncMock(spec=AsyncSession)

    # 模拟 first=not found → insert；second=found → update
    async def fake_execute_first(stmt, *args: Any, **kwargs: Any):
        mr = MagicMock()
        mr.scalar_one_or_none.return_value = None
        captured_calls.append("get:not_found")
        return mr

    async def fake_execute_second(stmt, *args: Any, **kwargs: Any):
        mr = MagicMock()
        existing = TenantORM(tenant_id="tenant_x", name="x", is_active=True)
        mr.scalar_one_or_none.return_value = existing
        captured_calls.append("get:found")
        return mr

    repo = TenantRepository(session)
    payload = TenantCreate(tenant_id="tenant_x", name="X Brand", display_name="X", description="", is_active=True)

    session.execute = AsyncMock(side_effect=fake_execute_first)  # type: ignore[method-assign]
    first = await repo.upsert_by_id(payload)
    assert first.tenant_id == "tenant_x"
    assert session.add.call_count == 1  # 第一次 INSERT

    session.execute = AsyncMock(side_effect=fake_execute_second)  # type: ignore[method-assign]
    session.add.reset_mock()
    second = await repo.upsert_by_id(payload)
    assert second.tenant_id == "tenant_x"
    assert session.add.call_count == 0  # 第二次是 UPDATE，不再 add
    assert captured_calls == ["get:not_found", "get:found"]


@pytest.mark.asyncio
async def test_tr25_user_upsert_twice_is_idempotent_and_user_id_stable() -> None:
    """UserRepository.upsert_by_username 幂等：第二次不改变 user_id。"""
    session = AsyncMock(spec=AsyncSession)
    repo = UserRepository(session)
    payload = UserCreate(
        user_id="55555555-5555-5555-5555-555555555555",
        username="alice",
        display_name="Alice First",
        role=Role.CONSUMER,
        phone="13811112222",
    )

    # First: not exists → create
    mr1 = MagicMock()
    mr1.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mr1)  # type: ignore[method-assign]
    await repo.upsert_by_username("tenant_a", payload)
    assert session.add.call_count == 1
    added_user: UserORM = session.add.call_args_list[0][0][0]
    assert str(added_user.user_id) == payload.user_id

    # Second: exists → update display name, no add
    existing = _make_user_orm(
        tenant_id="tenant_a",
        user_id=payload.user_id,
        username="alice",
    )
    mr2 = MagicMock()
    mr2.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=mr2)  # type: ignore[method-assign]
    session.add.reset_mock()
    updated_payload = UserCreate(
        user_id=payload.user_id,
        username="alice",
        display_name="Alice Updated",
        role=Role.CONSUMER,
    )
    updated = await repo.upsert_by_username("tenant_a", updated_payload)
    assert session.add.call_count == 0
    assert updated.display_name == "Alice Updated"

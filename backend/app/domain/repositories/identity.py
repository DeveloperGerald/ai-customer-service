from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.schemas.identity import Role, TenantCreate, TenantRead, UserCreate, UserRead
from app.core.errors import ConfigError, ResourceNotFoundError
from app.domain.models.identity import TenantORM, UserORM

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import CursorResult, Result


@dataclass(frozen=True)
class Actor:
    """经过身份验证的调用方标识。供 Repository / Service 作为显式参数传递。"""

    actor_id: str
    tenant_id: str
    role: Role

    def is_consumer(self) -> bool:
        return self.role == Role.CONSUMER

    def can_access(self, *, tenant_id: str, owner_user_id: str | UUID | None = None) -> bool:
        """权限判断：staff/admin 同租户所有资源；consumer 仅自己。"""
        if self.tenant_id != tenant_id:
            return False
        if self.role in (Role.STAFF, Role.ADMIN, Role.AGENT_ENGINEER):
            return True
        # consumer
        if owner_user_id is None:
            return True
        return str(self.actor_id) == str(owner_user_id)


class TenantRepository:
    """租户 CRUD。演示场景主要是读取。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: TenantCreate) -> TenantORM:
        tenant = TenantORM(
            tenant_id=payload.tenant_id,
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
            is_active=payload.is_active,
        )
        self._session.add(tenant)
        await self._session.flush()
        return tenant

    async def get(self, tenant_id: str) -> TenantORM | None:
        stmt = select(TenantORM).where(TenantORM.tenant_id == tenant_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert_by_id(self, payload: TenantCreate) -> TenantORM:
        existing = await self.get(payload.tenant_id)
        if existing is None:
            return await self.create(payload)
        for field in ("name", "display_name", "description", "is_active"):
            setattr(existing, field, getattr(payload, field))
        await self._session.flush()
        return existing

    @staticmethod
    def to_read(orm: TenantORM) -> TenantRead:
        return TenantRead.model_validate(orm)


class UserRepository:
    """用户 Repository。所有查询必须显式带 tenant_id，防止越权。"""

    SYSTEM_STAFF_USERNAME_TEMPLATE = "{tenant_id}_staff"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------- 写入 ----------

    async def create(self, tenant_id: str, payload: UserCreate) -> UserORM:
        if not tenant_id:
            raise ConfigError("UserRepository.create 必须显式提供 tenant_id。")
        user = UserORM(
            user_id=payload.user_id,
            tenant_id=tenant_id,
            username=payload.username,
            display_name=payload.display_name,
            email=payload.email,
            phone=payload.phone,
            role=payload.role.value,
            is_active=payload.is_active,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def upsert_by_username(self, tenant_id: str, payload: UserCreate) -> UserORM:
        """按 (tenant_id, username) 幂等创建；已存在则更新显示信息和角色。

        幂等种子脚本的核心函数。
        """
        stmt = select(UserORM).where(
            and_(
                UserORM.tenant_id == tenant_id,
                UserORM.username == payload.username,
            )
        )
        result = await self._session.execute(stmt)
        existing: UserORM | None = result.scalar_one_or_none()
        if existing is None:
            return await self.create(tenant_id, payload)
        for field in ("display_name", "email", "phone", "role", "is_active"):
            value = getattr(payload, field)
            if field == "role":
                value = value.value
            setattr(existing, field, value)
        await self._session.flush()
        return existing

    # ---------- 系统内部账号 ----------

    async def get_or_create_system_staff(self, tenant_id: str) -> UserORM:
        """获取同租户「系统客服 STAFF」账号；不存在则创建一个用于内部 tool/agent 消息写入。

        说明：
          - 约定 username = f"{tenant_id}_staff"（与 seed_tenants.py 一致）。
          - 若 seed 脚本已导入则直接返回；否则创建一个内部账号（user_id 用 uuid5 稳定生成），
            保证 conversation_messages.actor_id FK 不指向不存在的用户。
          - 不做 HTTP 层角色校验：本方法用于服务端内部调用（facade / api 层构造 service_actor）。
        """
        from uuid import NAMESPACE_DNS, uuid5

        from app.application.schemas.identity import Role

        username = self.SYSTEM_STAFF_USERNAME_TEMPLATE.format(tenant_id=tenant_id)
        existing = await self.get_by_username(tenant_id, username)
        if existing is not None:
            return existing
        tenant_repo = TenantRepository(self._session)
        tenant = await tenant_repo.get(tenant_id)
        brand_name = tenant.name if tenant else tenant_id
        stable_id = str(uuid5(NAMESPACE_DNS, f"staff@{tenant_id}.internal"))
        payload = UserCreate(
            user_id=stable_id,
            username=username,
            display_name=f"{brand_name}-系统客服",
            email=f"{username}@internal.local",
            phone=None,
            role=Role.STAFF,
            is_active=True,
        )
        return await self.create(tenant_id, payload)

    # ---------- 读取（强制 tenant_id）----------

    async def get_by_user_id(self, tenant_id: str, user_id: str | UUID) -> UserORM | None:
        stmt = select(UserORM).where(
            and_(
                UserORM.tenant_id == tenant_id,
                UserORM.user_id == user_id,
                UserORM.is_active.is_(True),
            )
        )
        result: Result[Any] = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_username(self, tenant_id: str, username: str) -> UserORM | None:
        stmt = select(UserORM).where(
            and_(
                UserORM.tenant_id == tenant_id,
                UserORM.username == username,
                UserORM.is_active.is_(True),
            )
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def exists_for_tenant(self, tenant_id: str, user_id: str | UUID) -> bool:
        """仅用于"归属是否正确"断言。不返回任何资源详情，避免泄露存在性。"""
        stmt = exists(
            select(UserORM.user_id).where(
                and_(
                    UserORM.tenant_id == tenant_id,
                    UserORM.user_id == user_id,
                )
            )
        ).select()
        result: CursorResult[Any] = await self._session.execute(stmt)
        row = result.fetchone()
        return bool(row and row[0])

    # ---------- Actor 感知读取（consumer 仅自己）----------

    async def get_read_for_actor(self, actor: Actor, user_id: str | UUID) -> UserRead:
        """返回 UserRead（脱敏），不区分"不存在"和"越权"，统一抛 ResourceNotFound。

        对 ResourceNotFound 调用者不要输出"该用户属于其他租户"等信息。
        """
        user = await self.get_by_user_id(actor.tenant_id, user_id)
        if user is None or not actor.can_access(
            tenant_id=actor.tenant_id,
            owner_user_id=str(user.user_id) if user else None,
        ):
            raise ResourceNotFoundError(message="resource not found")
        return UserRead.model_validate(user)

    @staticmethod
    def to_read(orm: UserORM) -> UserRead:
        return UserRead.model_validate(orm)

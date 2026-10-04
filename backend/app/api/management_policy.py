"""租户政策结构化配置管理接口（staff/admin 可读写）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import require_actor
from app.application.schemas.policy import PolicyConfigRead, PolicyConfigUpdate
from app.core.errors import ResourceNotFoundError
from app.domain.repositories.identity import Actor
from app.domain.repositories.policy import PolicyConfigRepository
from app.infrastructure.db.engine import scoped_db_session

router = APIRouter(prefix="/api/management/tenants/{target_tenant_id}/policy", tags=["management-policy"])


async def _session(request: Request) -> AsyncSession:
    async with scoped_db_session(request.app.state.bundle.infra) as sess:
        yield sess


@router.get("", response_model=PolicyConfigRead)
async def get_policy_config(
    target_tenant_id: str,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> PolicyConfigRead:
    """读取指定租户的售后配置。

    同租户 consumer 可读（用于展示用户退款流程）；跨租户任何角色一律 ResourceNotFound。
    """
    repo = PolicyConfigRepository(session)
    try:
        config = await repo.get_read_for_actor(actor, target_tenant_id)
    except ResourceNotFoundError as exc:
        raise exc
    await session.commit()
    return config


@router.put("", response_model=PolicyConfigRead)
async def update_policy_config(
    target_tenant_id: str,
    update: PolicyConfigUpdate,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> PolicyConfigRead:
    """修改指定租户的售后配置。

    写权限：仅同租户 staff/admin；consumer/跨租户 统一抛 ResourceNotFound（存在性不泄露）。
    """
    repo = PolicyConfigRepository(session)
    new_cfg = await repo.update_for_actor(actor, target_tenant_id, update)
    await session.commit()
    return new_cfg

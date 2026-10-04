from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.schemas.policy import (
    KnowledgeChunkCreate,
    KnowledgeChunkRead,
    PolicyConfigRead,
    PolicyConfigUpdate,
)
from app.core.errors import ErrorCode, ResourceNotFoundError
from app.domain.constants.policies import TenantPolicy
from app.domain.models.policy import KnowledgeChunkORM, TenantPolicyConfigORM
from app.domain.repositories.identity import Actor

_EMBEDDING_DIM = 1536


@dataclass
class _MockEmbedding:
    """T2.5 阶段：本地无 embedding provider，生成固定维度的 mock 向量。

    T5 接入真实 provider 后可删除。返回值是 JSON 序列化后的 1536 维 float 列表，
    存 TEXT 列，等 pgvector 列类型切换后直接 ALTER COLUMN。
    """

    seed: str
    dim: int = _EMBEDDING_DIM

    def to_json_str(self) -> str:
        rng = random.Random(self.seed.encode("utf-8"))
        vec = [round(rng.uniform(-0.1, 0.1), 6) for _ in range(self.dim)]
        return json.dumps(vec, ensure_ascii=False)


def _sha256(parts: Sequence[str]) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
    return h.hexdigest()


def ensure_can_modify_tenant(actor: Actor, target_tenant_id: str) -> None:
    """只允许 staff/admin 修改**同租户**配置；越权统一抛 ResourceNotFound。

    仓储层与 API 层共用的租户写权限校验。

    Args:
        actor: 已鉴权的操作者。
        target_tenant_id: 目标租户。

    Raises:
        ResourceNotFoundError: 跨租户或角色不是 staff/admin（伪装成 404，不暴露存在性）。
    """
    if actor.tenant_id.lower() != target_tenant_id.lower():
        # 跨租户 → 不泄露"目标租户是否存在配置"
        raise ResourceNotFoundError(
            ErrorCode.RESOURCE_NOT_FOUND,
            message="resource not found",
            details={"resource_type": "tenant_policy", "resource_id": target_tenant_id},
        )
    if actor.role.value not in {"staff", "admin"}:
        # consumer → 同样 ResourceNotFound，避免暴露存在性
        raise ResourceNotFoundError(
            ErrorCode.RESOURCE_NOT_FOUND,
            message="resource not found",
            details={"resource_type": "tenant_policy", "resource_id": target_tenant_id},
        )


class PolicyConfigRepository:
    """每租户结构化售后配置仓储（1 tenant → 1 row）。

    权限：
    - 读：同租户任意角色可读（consumer 查询退款时需要判断规则，所以必须可读）。
    - 写：仅同租户 staff/admin。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------- 读 ----------

    async def get_or_none(self, tenant_id: str) -> TenantPolicyConfigORM | None:
        stmt = select(TenantPolicyConfigORM).where(TenantPolicyConfigORM.tenant_id == tenant_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_read_for_actor(self, actor: Actor, tenant_id: str) -> PolicyConfigRead:
        if actor.tenant_id.lower() != tenant_id.lower():
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "tenant_policy", "resource_id": tenant_id},
            )
        row = await self.get_or_none(tenant_id)
        if row is None:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "tenant_policy", "resource_id": tenant_id},
            )
        return PolicyConfigRead.model_validate(row)

    async def get_effective_policy(self, tenant_id: str) -> TenantPolicy | None:
        """从 DB 真实数据（tenant_policy_configs + tenants + knowledge_chunks）
        组装成 TenantPolicy dataclass。

        数据来源：
          - 结构化字段：tenant_policy_configs 表（return_days / policy_type / fee / warranty 等）
          - 品牌 / slogan：tenants 表（name + display_name 推断 slogan）
          - 政策全文：knowledge_chunks 表（source='policy_manual'，按 created_at 升序拼接）

        若未 seed（3 张表都空）返回 None，调用方回退到合理默认值（不抛异常，不中断链路）。
        """
        from app.domain.models.identity import TenantORM

        config = await self.get_or_none(tenant_id)
        tenant_stmt = select(TenantORM).where(TenantORM.tenant_id == tenant_id)
        tenant = (await self.session.execute(tenant_stmt)).scalar_one_or_none()
        brand_name = tenant.name if tenant else tenant_id
        display_name = tenant.display_name if tenant else brand_name
        slogan = display_name
        if tenant and tenant.name and "（" in display_name and display_name.endswith("）"):
            inner = display_name[display_name.index("（") + 1 : -1]
            if inner:
                slogan = inner
        chunk_stmt = (
            select(KnowledgeChunkORM)
            .where(
                KnowledgeChunkORM.tenant_id == tenant_id,
                KnowledgeChunkORM.source == "policy_manual",
            )
            .order_by(KnowledgeChunkORM.created_at.asc())
        )
        chunks = (await self.session.execute(chunk_stmt)).scalars().all()
        full_text_parts: list[str] = []
        highlights: list[str] = []
        for c in chunks:
            if c.content:
                full_text_parts.append(c.content.strip())
            if c.title and "要点" in c.title:
                for line in c.content.strip().splitlines():
                    line = line.strip().strip("；;。.")
                    if line and len(line) <= 40:
                        highlights.append(line)
        full_text = "\n\n".join(full_text_parts)
        if config is None and not full_text:
            return None
        highlights = highlights or [f"{brand_name}官方售后政策"]
        if config is not None:
            return TenantPolicy(
                tenant_id=tenant_id,
                brand_name=brand_name,
                slogan=slogan,
                return_days=config.return_days,
                return_policy_type=config.return_policy_type,  # type: ignore[arg-type]
                custom_product_allowed=bool(config.custom_product_allowed_return),
                restocking_fee_pct_non_quality=int(config.restocking_fee_pct_non_quality),
                warranty_days_quality=int(config.warranty_days_quality),
                highlights=highlights,
                full_text=full_text
                or f"【{brand_name}售后政策】\n\n（政策全文待录入；结构化配置已生效。）",
            )
        return TenantPolicy(
            tenant_id=tenant_id,
            brand_name=brand_name,
            slogan=slogan,
            return_days=None,
            return_policy_type="hybrid",
            custom_product_allowed=False,
            restocking_fee_pct_non_quality=0,
            warranty_days_quality=30,
            highlights=highlights,
            full_text=full_text
            or f"【{brand_name}售后政策】\n\n（政策全文待录入；结构化配置已生效。）",
        )

    # ---------- 写（种子用，绕过权限校验）----------

    async def upsert_default_from_constant(
        self,
        tenant_id: str,
        policy: TenantPolicy,
        *,
        updated_by: UUID | None = None,
    ) -> TenantPolicyConfigORM:
        """seed_tenants.py 用：按 policies.py 的常量 upsert 一条默认配置。

        不做角色校验（seed 没有 HTTP actor）；更新时 updated_by=None 表示系统种子。
        """
        values: dict[str, Any] = dict(
            tenant_id=tenant_id,
            return_days=policy.return_days,
            return_policy_type=policy.return_policy_type,
            restocking_fee_pct_non_quality=policy.restocking_fee_pct_non_quality,
            warranty_days_quality=policy.warranty_days_quality,
            custom_product_allowed_return=policy.custom_product_allowed,
        )
        if updated_by is not None:
            values["updated_by"] = updated_by
        insert_stmt = insert(TenantPolicyConfigORM).values(**values)
        upsert = insert_stmt.on_conflict_do_update(
            index_elements=[TenantPolicyConfigORM.tenant_id],
            set_={k: v for k, v in values.items() if k != "tenant_id"},
        )
        await self.session.execute(upsert)
        await self.session.flush()
        return await self.get_or_none(tenant_id)  # type: ignore[return-value]

    # ---------- 写（管理接口用，严格权限）----------

    async def update_for_actor(
        self,
        actor: Actor,
        tenant_id: str,
        update: PolicyConfigUpdate,
    ) -> PolicyConfigRead:
        """HTTP 管理接口用：只改 payload 中传了的字段；返回修改后的 Read。"""
        ensure_can_modify_tenant(actor, tenant_id)
        row = await self.get_or_none(tenant_id)
        if row is None:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "tenant_policy", "resource_id": tenant_id},
            )
        patch = update.model_dump(exclude_unset=True, exclude_none=False)
        for field, value in patch.items():
            setattr(row, field, value)
        row.updated_by = UUID(actor.actor_id)
        await self.session.flush()
        return PolicyConfigRead.model_validate(row)


class KnowledgeChunkRepository:
    """知识库段落仓储（政策 + FAQ + 操作文档一张表）。

    权限：
    - 读：同租户任意角色可读（T5 RAG 时 agent 也会读）。
    - 写：仅同租户 staff/admin。
    - 幂等去重：UQ(tenant_id, source, content_hash)。
    - 安全：created_by **永远**从 actor.actor_id 注入，忽略 payload 中任何同名字段（即使未来有人乱加 schema）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------- 读 ----------

    async def list_for_tenant(
        self,
        actor: Actor,
        tenant_id: str,
        *,
        source: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KnowledgeChunkRead]:
        if actor.tenant_id.lower() != tenant_id.lower():
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "knowledge_chunk", "resource_id": tenant_id},
            )
        stmt = (
            select(KnowledgeChunkORM)
            .where(KnowledgeChunkORM.tenant_id == tenant_id)
            .order_by(KnowledgeChunkORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if source is not None:
            stmt = stmt.where(KnowledgeChunkORM.source == source)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [KnowledgeChunkRead.model_validate(r) for r in rows]

    # ---------- 写 ----------

    async def create_for_actor(
        self,
        actor: Actor,
        tenant_id: str,
        payload: KnowledgeChunkCreate,
        *,
        _override_time: datetime | None = None,
    ) -> KnowledgeChunkRead:
        """创建 chunk，自动：
        - SHA256 算 content_hash
        - created_by 用 actor.actor_id，**完全忽略 payload 中任何形式的 created_by**
        - mock embedding（T5 后替换）
        - 命中 UQ 时直接读已存在的那条（幂等），不抛重复键错
        """
        ensure_can_modify_tenant(actor, tenant_id)
        content_hash = _sha256([tenant_id, payload.source, payload.content])
        values: dict[str, Any] = dict(
            tenant_id=tenant_id,
            title=payload.title,
            content=payload.content,
            source=payload.source,
            content_hash=content_hash,
            created_by=UUID(actor.actor_id),
            embedding=_MockEmbedding(seed=content_hash).to_json_str(),
        )
        stmt = (
            insert(KnowledgeChunkORM)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["tenant_id", "source", "content_hash"])
        )
        await self.session.execute(stmt)
        await self.session.flush()

        fetch_q = select(KnowledgeChunkORM).where(
            KnowledgeChunkORM.tenant_id == tenant_id,
            KnowledgeChunkORM.source == payload.source,
            KnowledgeChunkORM.content_hash == content_hash,
        )
        row = (await self.session.execute(fetch_q)).scalar_one()
        return KnowledgeChunkRead.model_validate(row)

    async def delete_for_actor(self, actor: Actor, tenant_id: str, chunk_id: UUID) -> None:
        ensure_can_modify_tenant(actor, tenant_id)
        stmt = select(KnowledgeChunkORM).where(
            KnowledgeChunkORM.chunk_id == chunk_id,
            KnowledgeChunkORM.tenant_id == tenant_id,
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError(
                ErrorCode.RESOURCE_NOT_FOUND,
                message="resource not found",
                details={"resource_type": "knowledge_chunk", "resource_id": str(chunk_id)},
            )
        await self.session.delete(row)
        await self.session.flush()

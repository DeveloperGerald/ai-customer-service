"""演示用身份种子脚本。

运行方式：
    PATH="$HOME/Library/Python/3.9/bin:$PATH" python3 scripts/seed_tenants.py

幂等特性：
    - 租户按 tenant_id upsert。
    - 用户按 (tenant_id, username) upsert。
    - 重复执行不新增记录，不改变 user_id。
    - user_id 采用固定 UUID，便于后续 T3 订单归属绑定。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


FIXED_USER_IDS: Final[dict[tuple[str, str], str]] = {
    ("tenant_a", "tenant_a_consumer"): "3f88a233-4d11-50e6-926b-e0ddd2838c0c",
    ("tenant_a", "tenant_a_staff"): "7dab33cd-d516-5f11-8692-28554fa26646",
    ("tenant_a", "tenant_a_admin"): "175a13f9-0375-5b6f-83db-e0ffb4dc6674",
    ("tenant_b", "tenant_b_consumer"): "d5f58850-d733-5150-9b07-f96fa2d53b41",
    ("tenant_b", "tenant_b_staff"): "d852ecfc-e419-528c-ad42-afa0ed273993",
    ("tenant_b", "tenant_b_admin"): "84d8de71-cc60-50c7-a9e3-4fdc7c4e63cf",
    ("tenant_c", "tenant_c_consumer"): "ce37ef2f-e00a-54b3-ace9-9eb2cb673409",
    ("tenant_c", "tenant_c_staff"): "f1e1d992-35e2-51e1-97ea-87de0297428a",
    ("tenant_c", "tenant_c_admin"): "f3eb0d91-a41e-500a-a867-a8314850c723",
}


async def seed_database() -> dict[str, object]:
    """执行幂等种子：三租户 + 每租户 3 角色 + 每租户默认售后配置 + 政策全文 chunks。

    Returns:
        包含 tenants / users / demo_tokens / policy_configs / knowledge_chunks 的字典。
    """
    from app.application.schemas.identity import (
        Role,
        TenantCreate,
        UserCreate,
        issue_demo_token,
    )
    from app.application.schemas.policy import KnowledgeChunkCreate
    from app.config import load_settings
    from app.core.infrastructure import InfrastructureBundle
    from app.domain.constants.policies import TENANT_POLICIES
    from app.domain.repositories.identity import Actor, TenantRepository, UserRepository
    from app.domain.repositories.policy import KnowledgeChunkRepository, PolicyConfigRepository
    from app.infrastructure.db.engine import scoped_db_session

    settings = load_settings()
    bundle = InfrastructureBundle(settings)
    await bundle.start()
    try:
        async with scoped_db_session(bundle) as session:
            tenant_repo = TenantRepository(session)
            user_repo = UserRepository(session)
            policy_repo = PolicyConfigRepository(session)
            chunk_repo = KnowledgeChunkRepository(session)
            created_tenants: list[str] = []
            created_user_keys: list[tuple[str, str]] = []

            # ---------- 租户 ----------
            for tenant_id, policy in TENANT_POLICIES.items():
                payload = TenantCreate(
                    tenant_id=tenant_id,
                    name=policy.brand_name,
                    display_name=policy.brand_name + "（" + policy.slogan + "）",
                    description="售后政策要点：" + "；".join(policy.highlights)
                    + "\n\n【完整政策】\n"
                    + policy.full_text,
                    is_active=True,
                )
                existing = await tenant_repo.get(tenant_id)
                tenant = await tenant_repo.upsert_by_id(payload)
                await session.flush()
                if existing is None:
                    created_tenants.append(tenant_id)
                assert tenant.tenant_id == tenant_id

            # ---------- 用户 ----------
            user_map: dict[tuple[str, str], str] = {}
            demo_tokens_output: list[dict[str, str]] = []
            for (tenant_id, username), fixed_uuid in FIXED_USER_IDS.items():
                role_name = username.rsplit("_", 1)[-1]
                role_map = {
                    "consumer": Role.CONSUMER,
                    "staff": Role.STAFF,
                    "admin": Role.ADMIN,
                }
                role = role_map[role_name]
                brand = TENANT_POLICIES[tenant_id].brand_name
                display_map = {
                    Role.CONSUMER: f"{brand}-演示买家",
                    Role.STAFF: f"{brand}-客服人员",
                    Role.ADMIN: f"{brand}-管理员",
                }
                payload = UserCreate(
                    user_id=fixed_uuid,
                    username=username,
                    display_name=display_map[role],
                    email=f"{username}@example.com",
                    phone={
                        "tenant_a": "13800000001",
                        "tenant_b": "13900000001",
                        "tenant_c": "13700000001",
                    }[tenant_id],
                    role=role,
                    is_active=True,
                )
                existing = await user_repo.get_by_username(tenant_id, username)
                user = await user_repo.upsert_by_username(tenant_id, payload)
                await session.flush()
                if existing is None:
                    created_user_keys.append((tenant_id, username))
                assert str(user.user_id) == fixed_uuid
                assert user.tenant_id == tenant_id
                assert user.role == role.value
                user_map[(tenant_id, role.value)] = fixed_uuid

                bundle_info = issue_demo_token(
                    settings.security,
                    tenant_id=tenant_id,
                    actor_id=str(user.user_id),
                    role=role,
                )
                demo_tokens_output.append(
                    {
                        "tenant_id": tenant_id,
                        "tenant_name": TENANT_POLICIES[tenant_id].brand_name,
                        "role": role.value,
                        "username": username,
                        "user_id": str(user.user_id),
                        "access_token": bundle_info.access_token,
                        "expires_in_seconds": bundle_info.expires_in_seconds,
                    }
                )

            # ---------- 结构化售后配置 upsert ----------
            policy_seed_status: dict[str, str] = {}
            for tenant_id, policy in TENANT_POLICIES.items():
                await policy_repo.upsert_default_from_constant(tenant_id, policy)
                policy_seed_status[tenant_id] = "upserted_or_kept"
            await session.flush()

            # ---------- 知识库 chunks：按租户把 full_text 分成规则 chunk ----------
            chunk_stats: dict[str, int] = {}
            for tenant_id, policy in TENANT_POLICIES.items():
                admin_uuid = user_map[(tenant_id, "admin")]
                actor = Actor(
                    actor_id=admin_uuid,
                    tenant_id=tenant_id,
                    role=Role.ADMIN,
                )
                policy_highlights = KnowledgeChunkCreate(
                    title=f"{policy.brand_name}售后政策要点",
                    source="policy_manual",
                    content=f"【{policy.brand_name}售后亮点】{'；'.join(policy.highlights)}\n\n完整政策请参考【{policy.brand_name}售后政策 v1.0】全文段落。",
                )
                await chunk_repo.create_for_actor(actor, tenant_id, policy_highlights)

                full = policy.full_text
                chunk_size = 900
                chunks_created_before = len(
                    await chunk_repo.list_for_tenant(actor, tenant_id)
                )
                for i in range(0, len(full), chunk_size):
                    piece = full[i : i + chunk_size]
                    nth = i // chunk_size + 1
                    title = f"{policy.brand_name}售后政策 v1.0 — 第 {nth} 段"
                    p = KnowledgeChunkCreate(title=title, source="policy_manual", content=piece)
                    await chunk_repo.create_for_actor(actor, tenant_id, p)
                await session.flush()
                final_chunks = await chunk_repo.list_for_tenant(actor, tenant_id)
                chunk_stats[tenant_id] = len(final_chunks) - chunks_created_before

            await session.commit()
    finally:
        await bundle.stop()
    return {
        "new_tenants": created_tenants,
        "new_users": created_user_keys,
        "demo_tokens": demo_tokens_output,
        "policy_configs": policy_seed_status,
        "knowledge_chunk_delta": chunk_stats,
    }


def _print_human_readable(result: dict[str, object]) -> None:
    tokens = result["demo_tokens"]
    print("=" * 80)
    print(
        "【种子脚本完成】新增租户:",
        len(result["new_tenants"]),
        "新增用户:",
        len(result["new_users"]),
        "每租户知识库 chunk 增量:",
        result.get("knowledge_chunk_delta"),
    )
    print("=" * 80)
    print()
    print(f"{'tenant_id':<12} {'品牌':<8} {'role':<10} {'username':<22} {'user_id':<40}")
    print("-" * 100)
    for t in tokens:
        print(f"{t['tenant_id']:<12} {t['tenant_name']:<8} {t['role']:<10} {t['username']:<22} {t['user_id']:<40}")
    print()
    print("售后配置 seed：", result.get("policy_configs"))
    print()
    print("=" * 80)
    print("【演示令牌】（复制下面 JSON 到前端，可直接硬编码演示）")
    print("=" * 80)
    print(json.dumps(tokens, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    out = asyncio.run(seed_database())
    _print_human_readable(out)

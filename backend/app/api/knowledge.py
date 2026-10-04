"""知识库 RAG 检索接口（所有角色，同租户即可调用）。

管理 CRUD 接口在 management_knowledge.py（staff/admin only）；
此处只暴露「语义搜索」（consumer/staff/admin 同租户都能调用，便于前端 & Agent 节点使用）。

检索实现：app.state.bundle.vector_store（langchain_postgres.PGVectorStore 封装）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import require_actor
from app.application.schemas.knowledge import (
    KnowledgeSearchHit,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.domain.repositories.identity import Actor
from app.infrastructure.db.engine import scoped_db_session
from app.infrastructure.vectorstore import PgVectorStoreRetriever

router = APIRouter(prefix="/api/knowledge", tags=["knowledge-search"])


async def _session(request: Request) -> AsyncSession:
    async with scoped_db_session(request.app.state.bundle.infra) as sess:
        yield sess


def _retriever_from_state(request: Request) -> PgVectorStoreRetriever:
    """从 app.state 取出 lifespan 预构建的向量检索器（未初始化 → 503）。"""
    from fastapi import HTTPException

    store = getattr(request.app.state.bundle, "vector_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="知识向量库未初始化（数据库不可用或未配置 pgvector）",
        )
    return PgVectorStoreRetriever(store)


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    payload: KnowledgeSearchRequest,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> KnowledgeSearchResponse:
    """按用户 query 做向量检索（强制按 actor.tenant_id 硬隔离，越权一律 0 结果不泄露）。

    传入 `target_tenant_id` 作为 path 参数是不允许的；这里强制使用鉴权 Actor 的租户，
    符合架构约束：「RAG 检索必须带 tenant_id 过滤，且该 tenant_id 必须是 HTTP 端确认的。」
    """
    _ = session  # 检索走独立向量库连接池，session 仅保持鉴权/事务依赖一致性
    retriever = _retriever_from_state(request)
    raw = await retriever.retrieve(
        tenant_id=actor.tenant_id,
        query=payload.query,
        top_k=payload.top_k,
        similarity_threshold=payload.similarity_threshold,
    )
    if payload.source:
        raw = [r for r in raw if r.get("source") == payload.source]
    hits = [
        KnowledgeSearchHit(
            chunk_id=r["chunk_id"],
            tenant_id=r["tenant_id"],
            title=r.get("title"),
            content=r["content"],
            source=r["source"],
            similarity=float(r.get("similarity", 0.0)),
            doc_name=(r.get("metadata") or {}).get("doc_name"),
        )
        for r in raw
    ]
    return KnowledgeSearchResponse(
        query=payload.query,
        hits=hits,
        total=len(hits),
        top_k=payload.top_k,
        similarity_threshold=payload.similarity_threshold,
    )


@router.get("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge_get(
    q: str = Query(..., min_length=1, max_length=1000, description="检索词", alias="q"),
    top_k: int = Query(default=4, ge=1, le=50),
    similarity_threshold: float = Query(default=0.3, ge=0.0, le=1.0),
    source: str | None = Query(default=None),
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
    request: Request = None,  # type: ignore[assignment]
) -> KnowledgeSearchResponse:
    """GET 版本（便于 curl / Swagger 直接调用）。"""
    body = KnowledgeSearchRequest(
        query=q,
        top_k=top_k,
        similarity_threshold=similarity_threshold,
        source=source,
    )
    return await search_knowledge(body, request, actor, session)

"""知识库 chunks 管理接口（T5 RAG 复用此表）+ 文档上传到向量库接口。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import require_actor
from app.application.schemas.knowledge import (
    KnowledgeDocumentContent,
    KnowledgeDocumentRead,
    KnowledgeDocumentUploadResponse,
)
from app.application.schemas.policy import ChunkSource, KnowledgeChunkCreate, KnowledgeChunkRead
from app.core.logging import get_logger
from app.domain.repositories.identity import Actor
from app.domain.repositories.policy import KnowledgeChunkRepository, ensure_can_modify_tenant
from app.infrastructure.db.engine import scoped_db_session

router = APIRouter(
    prefix="/api/management/tenants/{target_tenant_id}/knowledge", tags=["management-knowledge"]
)

_ALLOWED_DOC_SUFFIXES = {".md", ".markdown", ".txt"}
_DOC_MAX_BYTES = 2 * 1024 * 1024
"""文档大小上限（2MB），超出拒绝，防误传巨型文件。"""

_log = get_logger("api.management_knowledge")


async def _session(request: Request) -> AsyncSession:
    async with scoped_db_session(request.app.state.bundle.infra) as sess:
        yield sess


def _vector_store_from_state(request: Request):
    """从 app.state 取出 lifespan 初始化的知识向量库（未初始化 → 503）。"""
    store = getattr(request.app.state.bundle, "vector_store", None)
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="知识向量库未初始化（数据库不可用或未配置 pgvector）",
        )
    return store


@router.get("/chunks", response_model=list[KnowledgeChunkRead])
async def list_chunks(
    target_tenant_id: str,
    request: Request,
    source: ChunkSource | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> list[KnowledgeChunkRead]:
    """列出指定租户的 FAQ/政策段落。"""
    repo = KnowledgeChunkRepository(session)
    rows = await repo.list_for_tenant(
        actor, target_tenant_id, source=source, limit=limit, offset=offset
    )
    await session.commit()
    return rows


@router.post("/chunks", response_model=KnowledgeChunkRead, status_code=201)
async def create_chunk(
    target_tenant_id: str,
    payload: KnowledgeChunkCreate,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> KnowledgeChunkRead:
    """新增 FAQ/政策段。

    created_by 从 actor 自动注入，即使客户端伪造也不会使用；命中幂等 UQ 时返回已存在的 chunk。
    """
    repo = KnowledgeChunkRepository(session)
    row = await repo.create_for_actor(actor, target_tenant_id, payload)
    await session.commit()
    return row


@router.delete("/chunks/{chunk_id}", status_code=204)
async def delete_chunk(
    target_tenant_id: str,
    chunk_id: UUID,
    request: Request,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> None:
    """删除 chunk。仅 staff/admin 同租户可删；chunk_id 不存在/跨租户 均 404。"""
    repo = KnowledgeChunkRepository(session)
    try:
        await repo.delete_for_actor(actor, target_tenant_id, chunk_id)
    except Exception as exc:
        # 统一 ResourceNotFound → 404；PermissionDenied 由 ensure_can_modify_tenant 伪装成 ResourceNotFound
        if hasattr(exc, "code") and str(exc.code) in {"RESOURCE_NOT_FOUND", "PERMISSION_DENIED"}:
            raise HTTPException(status_code=404, detail="not found") from exc
        raise
    await session.commit()
    return None


@router.get("/documents", response_model=list[KnowledgeDocumentRead])
async def list_documents(
    target_tenant_id: str,
    request: Request,
    actor: Actor = Depends(require_actor),
) -> list[KnowledgeDocumentRead]:
    """列出指定租户向量库中的知识文档（按 doc_name 聚合的文档级视图）。

    权限：仅同租户 staff/admin；越权一律 404（不暴露租户存在性）。
    """
    from app.core.errors import ResourceNotFoundError

    try:
        ensure_can_modify_tenant(actor, target_tenant_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc

    vector_store = _vector_store_from_state(request)
    rows = await vector_store.list_documents(tenant_id=target_tenant_id)
    return [KnowledgeDocumentRead(tenant_id=target_tenant_id, **row) for row in rows]


@router.get("/documents/{doc_name:path}", response_model=KnowledgeDocumentContent)
async def get_document(
    target_tenant_id: str,
    doc_name: str,
    request: Request,
    actor: Actor = Depends(require_actor),
) -> KnowledgeDocumentContent:
    """读取指定文档的拼接内容（切片按写入顺序组装）。

    权限：仅同租户 staff/admin；越权或文档不存在一律 404。
    """
    from app.core.errors import ResourceNotFoundError

    try:
        ensure_can_modify_tenant(actor, target_tenant_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc

    vector_store = _vector_store_from_state(request)
    row = await vector_store.get_document(tenant_id=target_tenant_id, doc_name=doc_name)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    return KnowledgeDocumentContent(
        tenant_id=target_tenant_id,
        doc_name=row["doc_name"],
        source=row["source"],
        title=row["title"],
        chunks=row["chunks"],
        content=row["content"],
    )


@router.post("/documents", response_model=KnowledgeDocumentUploadResponse, status_code=201)
async def upload_document(
    target_tenant_id: str,
    request: Request,
    file: UploadFile = File(..., description="Markdown/纯文本知识文档"),
    source: ChunkSource = Form(default="faq", description="知识来源"),
    title: str | None = Form(default=None, description="文档标题（默认取文件名去后缀）"),
    doc_name: str | None = Form(
        default=None, description="文档名（默认取文件名；同租户内重复上传覆盖旧版本）"
    ),
    actor: Actor = Depends(require_actor),
) -> KnowledgeDocumentUploadResponse:
    """上传知识文档到多租户向量库（langchain_postgres.PGVectorStore）。

    - 权限：仅同租户 staff/admin；越权一律 404（不暴露租户存在性）。
    - 切片：source=faq 按问答对切分（一问一块，识别不到问答结构时回退通用切分）；
      其他来源按 Markdown 等长切分（chunk=500，overlap=80）。
    - 幂等：同 (tenant_id, doc_name) 覆盖式重建——先删旧切片再写入，重复上传结果一致。
    - tenant_id 强制取 path 参数并要求与 actor 同租户，绝不信客户端注入的元数据。
    """
    from app.core.errors import ResourceNotFoundError

    try:
        ensure_can_modify_tenant(actor, target_tenant_id)
    except ResourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc

    vector_store = _vector_store_from_state(request)

    raw_name = (doc_name or file.filename or "").strip()
    if not raw_name:
        raise HTTPException(status_code=422, detail="doc_name 不能为空（或提供带文件名的上传）")
    suffix = ("." + raw_name.rsplit(".", 1)[-1].lower()) if "." in raw_name else ""
    if suffix and suffix not in _ALLOWED_DOC_SUFFIXES:
        raise HTTPException(
            status_code=422,
            detail=f"仅支持 {_ALLOWED_DOC_SUFFIXES} 文档，收到：{suffix}",
        )
    final_doc_name = raw_name
    final_title = (title or raw_name.rsplit(".", 1)[0]).strip() or None

    data = await file.read()
    if len(data) > _DOC_MAX_BYTES:
        raise HTTPException(status_code=413, detail="文档过大（上限 2MB）")
    try:
        content = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="文档必须是 UTF-8 编码文本") from exc

    chunk_ids = await vector_store.ingest_document(
        tenant_id=target_tenant_id,
        doc_name=final_doc_name,
        content=content,
        source=source,
        title=final_title,
    )
    _log.info(
        "knowledge.document.uploaded",
        tenant_id=target_tenant_id,
        doc_name=final_doc_name,
        source=source,
        chunks=len(chunk_ids),
        actor_id=str(actor.actor_id),
    )
    return KnowledgeDocumentUploadResponse(
        tenant_id=target_tenant_id,
        doc_name=final_doc_name,
        source=source,
        title=final_title,
        chunks=len(chunk_ids),
        chunk_ids=chunk_ids,
    )

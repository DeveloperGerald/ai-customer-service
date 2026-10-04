"""RAG 检索相关的请求/响应 Schema（T5）。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.application.schemas.policy import KnowledgeChunkRead


class KnowledgeSearchRequest(BaseModel):
    """POST /api/knowledge/search 请求体。

    所有字段中，仅 query 必填；top_k / threshold 可由 Agent 参数默认覆盖。
    """

    query: str = Field(..., min_length=1, max_length=1000, description="用户问题/检索词")
    top_k: int = Field(default=4, ge=1, le=50, description="返回最多多少条证据")
    similarity_threshold: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description="相似度阈值（余弦相似度，低于此阈值的 chunk 会被过滤）。",
    )
    source: str | None = Field(
        default=None,
        description="可选按 source 过滤：policy_manual / faq / operation_doc",
    )


class KnowledgeSearchHit(BaseModel):
    """单条检索命中项，便于前端直接展示。"""

    model_config = ConfigDict(from_attributes=True)

    chunk_id: UUID
    tenant_id: str
    title: str | None = None
    content: str
    source: str
    similarity: float = Field(ge=-1.0, le=1.0, description="余弦相似度，越高越相关")
    doc_name: str | None = Field(default=None, description="来源文档名（向量库文档级标识）")
    chunk: KnowledgeChunkRead | None = Field(
        default=None,
        description="PGVectorStore 路径下没有对应 knowledge_chunks ORM 行，为 None。",
    )


class KnowledgeSearchResponse(BaseModel):
    """知识检索响应。"""

    query: str
    hits: list[KnowledgeSearchHit]
    total: int
    top_k: int
    similarity_threshold: float


class KnowledgeDocumentRead(BaseModel):
    """GET /api/management/tenants/{tenant}/knowledge/documents 列表项（文档级聚合）。"""

    tenant_id: str
    doc_name: str = Field(description="文档名（同租户内唯一标识）")
    source: str = Field(description="知识来源：policy_manual / faq / operation_doc")
    title: str | None = Field(default=None, description="文档标题")
    chunks: int = Field(ge=0, description="该文档在向量库中的切片数量")


class KnowledgeDocumentContent(KnowledgeDocumentRead):
    """GET /api/management/tenants/{tenant}/knowledge/documents/{doc_name} 响应（含拼接内容）。"""

    content: str = Field(description="全部切片按写入顺序拼接的文档内容")


class KnowledgeDocumentUploadResponse(BaseModel):
    """POST /api/management/tenants/{tenant}/knowledge/documents 响应。"""

    tenant_id: str
    doc_name: str = Field(description="文档名（同租户内唯一标识，重复上传覆盖旧版本）")
    source: str = Field(description="知识来源：policy_manual / faq / operation_doc")
    title: str | None = Field(default=None, description="文档标题")
    chunks: int = Field(ge=0, description="本次写入向量库的切片数量")
    chunk_ids: list[UUID] = Field(description="切片在向量库中的稳定 ID（内容级幂等）")

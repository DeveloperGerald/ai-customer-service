"""基于 langchain_postgres.PGVectorStore 的多租户知识向量库（T5-RAG 重构）。

组件：
    - KnowledgeVectorStore：独立向量表（默认 knowledge_vectors）的封装。
        * 建表：PGEngine.ainit_vectorstore_table(overwrite_existing=False)，幂等；
          tenant_id / source / doc_name 声明为真实元数据列（可索引、filter 可直译 SQL）。
        * ingest_document：文档切片 + 确定性 UUID（uuid5）+ 同名文档覆盖式重建，保证幂等。
          source=faq 走问答对切分（一问一块），其他来源走 Markdown 等长切分。
        * search：强制 tenant_id 过滤（架构硬约束）；asimilarity_search_with_score 返回
          余弦「距离」，换算成 similarity = 1 - distance 后再按阈值过滤。
        * delete_document：按 (tenant_id, doc_name) 元数据列批量删除。
    - PgVectorStoreRetriever：BaseRetriever 适配（Agent facade / 知识检索 API 共用接口形状）。

向量表与 knowledge_chunks 关系：
    knowledge_chunks（结构化段落 CRUD + 审计）保持不变；RAG 检索与文档上传全部走本模块，
    两套数据相互独立。
"""

from __future__ import annotations

import re
import uuid as _uuid
from typing import TYPE_CHECKING, Any

from langchain_core.embeddings import Embeddings

from app.core.errors import ConfigError
from app.infrastructure.llm.providers import BaseRetriever

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.ext.asyncio import AsyncEngine

# =========================================================================
# 1. 知识向量库（PGVectorStore 封装）
# =========================================================================

KNOWLEDGE_VECTOR_TABLE = "knowledge_vectors"
"""向量表名（独立于 knowledge_chunks 结构化表）。"""

_CHUNK_SIZE = 500
"""非 FAQ 文档切片目标长度（字符）。"""

_CHUNK_OVERLAP = 80
"""相邻切片重叠长度（字符）。"""

_FAQ_QUESTION_PATTERN = re.compile(
    r"^\s*(?:[-*+]\s+|#{1,6}\s+)?(?:\*\*)?Q\s*[0-9]*\s*[：:]"
)
"""FAQ 问题行起始：兼容 ``Q1：`` / ``Q：`` / ``Q1:`` 及 Markdown 列表/加粗前缀。"""

_FAQ_BLANK_LINE_PATTERN = re.compile(r"\n[ \t]*(?:\r?\n[ \t]*)+")
"""FAQ 段落间的一个或多个空行（允许空行上只有空白字符）。"""


def _chunk_document_id(tenant_id: str, doc_name: str, content: str) -> _uuid.UUID:
    """生成切片的确定性 UUID（同租户同文档同内容 → 同 ID，幂等 upsert）。"""
    return _uuid.uuid5(_uuid.NAMESPACE_URL, f"{tenant_id}\x1f{doc_name}\x1f{content}")


class KnowledgeVectorStore:
    """多租户知识向量库：建表 / 文档切片入库 / 强隔离检索 / 文档删除。

    Attributes:
        _store: 底层 PGVectorStore 实例，``initialize()`` 成功后可用；
            单测可注入 fake（只需实现 aadd_texts / asimilarity_search_with_score / adelete）。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        embeddings: Embeddings,
        *,
        vector_size: int,
        table_name: str = KNOWLEDGE_VECTOR_TABLE,
    ) -> None:
        """绑定连接与向量配置（不触库，需 await initialize() 完成建表）。

        Args:
            engine: SQLAlchemy AsyncEngine（复用 InfrastructureBundle.db_engine）。
            embeddings: LangChain Embeddings（langchain_openai.OpenAIEmbeddings 等）。
            vector_size: 向量维度（settings.llm.embedding_dim，建表时固定）。
            table_name: 向量表名。
        """
        self._engine = engine
        self._embeddings = embeddings
        self._vector_size = int(vector_size)
        self._table_name = table_name
        self._store: Any | None = None

    @property
    def ready(self) -> bool:
        """向量库是否已完成初始化。"""
        return self._store is not None

    async def initialize(self) -> None:
        """初始化：CREATE EXTENSION IF NOT EXISTS vector + 建表（幂等）+ 构建 PGVectorStore。

        langchain_postgres 的 ainit_vectorstore_table 生成裸 CREATE TABLE（无 IF NOT EXISTS），
        这里先查 information_schema 判存，保证应用重启时幂等。

        Raises:
            ConfigError: 数据库不可达 / pgvector 扩展不可用 / 建表失败。
        """
        try:
            from langchain_postgres import Column, PGEngine, PGVectorStore

            pg_engine = PGEngine.from_engine(self._engine)
            if not await self._table_exists():
                await pg_engine.ainit_vectorstore_table(
                    self._table_name,
                    self._vector_size,
                    metadata_columns=[
                        Column(name="tenant_id", data_type="TEXT", nullable=False),
                        Column(name="source", data_type="TEXT", nullable=False),
                        Column(name="doc_name", data_type="TEXT", nullable=False),
                        Column(name="title", data_type="TEXT", nullable=True),
                    ],
                )
            self._store = await PGVectorStore.create(
                pg_engine,
                self._embeddings,
                self._table_name,
                metadata_columns=["tenant_id", "source", "doc_name", "title"],
            )
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(
                f"知识向量库初始化失败：{exc}",
                details={"table": self._table_name},
            ) from exc

    async def _table_exists(self) -> bool:
        """检查向量表是否已存在（默认 public schema）。"""
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            row = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = :name"
                ),
                {"name": self._table_name},
            )
            return row.first() is not None

    async def ingest_document(
        self,
        *,
        tenant_id: str,
        doc_name: str,
        content: str,
        source: str = "faq",
        title: str | None = None,
    ) -> list[_uuid.UUID]:
        """文档覆盖式入库：删除同 (tenant_id, doc_name) 旧切片 → 切片 → 幂等 upsert。

        Args:
            tenant_id: 所属租户（硬隔离键）。
            doc_name: 文档名（同租户内唯一标识，重复上传即覆盖旧版本）。
            content: 文档原文（Markdown/纯文本）。
            source: 知识来源：policy_manual / faq / operation_doc。
                faq 按问答对切分（一问一块）；其他来源按 Markdown 等长切分。
            title: 可选文档标题。

        Returns:
            本次写入的切片 ID 列表（写入顺序与切片顺序一致）。
        """
        if self._store is None:
            raise ConfigError("KnowledgeVectorStore 尚未初始化，请先调用 initialize()。")
        if source == "faq":
            chunks = self.split_faq(content)
            if not chunks:
                # FAQ 文档却识别不到任何问答结构：回退通用切分，避免整篇文档静默丢失
                chunks = self.split_document(content)
        else:
            chunks = self.split_document(content)
        # 覆盖式重建：先清掉同名旧版本（编辑后的残留切片不会遗留）
        await self.delete_document(tenant_id=tenant_id, doc_name=doc_name)
        if not chunks:
            return []
        metadatas = [
            {
                "tenant_id": tenant_id,
                "source": source,
                "doc_name": doc_name,
                "title": title,
                # 写入顺序号（langchain_metadata JSON），供文档级内容读取时按序拼接
                "chunk_index": i,
            }
            for i, _ in enumerate(chunks)
        ]
        ids = [_chunk_document_id(tenant_id, doc_name, c) for c in chunks]
        added: list[str | _uuid.UUID] = await self._store.aadd_texts(
            chunks, metadatas=metadatas, ids=ids
        )
        return [(_uuid.UUID(i) if isinstance(i, str) else i) for i in added]

    async def search(
        self,
        *,
        tenant_id: str,
        query: str,
        top_k: int = 4,
        similarity_threshold: float = 0.3,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """向量检索（强制 tenant_id 过滤，越权返回 0 条）。

        Args:
            tenant_id: 检索租户（必须来自 HTTP 鉴权 Actor，禁止客户端指定）。
            query: 用户检索词。
            top_k: 返回最多条数。
            similarity_threshold: 余弦相似度下限（similarity = 1 - distance）。
            source: 可选按知识来源过滤。

        Returns:
            与旧 PgVectorRetriever 兼容的 dict 列表：
            {"chunk_id","tenant_id","title","content","source","similarity","metadata"}，
            按相似度降序。
        """
        if self._store is None:
            raise ConfigError("KnowledgeVectorStore 尚未初始化，请先调用 initialize()。")
        if not tenant_id or not query.strip():
            return []
        top_k = max(1, int(top_k))
        filter_: dict[str, Any] = {"tenant_id": tenant_id}
        if source:
            filter_["source"] = source
        pairs = await self._store.asimilarity_search_with_score(query, k=top_k, filter=filter_)
        out: list[dict[str, Any]] = []
        for doc, distance in pairs:
            similarity = round(1.0 - float(distance), 6)
            if similarity < similarity_threshold:
                continue
            meta = doc.metadata or {}
            out.append(
                {
                    "chunk_id": str(doc.id) if doc.id is not None else "",
                    "tenant_id": meta.get("tenant_id", tenant_id),
                    "title": meta.get("title"),
                    "content": doc.page_content,
                    "source": meta.get("source", "faq"),
                    "similarity": similarity,
                    "metadata": {"doc_name": meta.get("doc_name"), "title": meta.get("title")},
                }
            )
        return out

    async def list_documents(self, *, tenant_id: str) -> list[dict[str, Any]]:
        """按 doc_name 聚合并列出该租户的全部文档（文档级视图，非切片级）。

        tenant_id / source / doc_name / title 在建表时声明为真实列，
        聚合直接走 SQL，不经 PGVectorStore filter。

        Args:
            tenant_id: 所属租户（硬隔离键）。

        Returns:
            [{"doc_name", "source", "title", "chunks"}]，按 doc_name 升序。
        """
        if self._store is None:
            raise ConfigError("KnowledgeVectorStore 尚未初始化，请先调用 initialize()。")
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        f'SELECT doc_name, source, MAX(title) AS title, COUNT(*)::int AS chunks '
                        f'FROM "{self._table_name}" WHERE tenant_id = :tenant_id '
                        f'GROUP BY doc_name, source ORDER BY doc_name'
                    ),
                    {"tenant_id": tenant_id},
                )
            ).mappings().all()
        return [dict(r) for r in rows]

    async def get_document(self, *, tenant_id: str, doc_name: str) -> dict[str, Any] | None:
        """按 (tenant_id, doc_name) 读取文档全部切片并拼接为文档级内容视图。

        切片按写入时的 chunk_index（langchain_metadata JSON 键）升序拼接；
        存量无 chunk_index 的数据按 langchain_id 兜底排序（重传后恢复真实顺序）。

        Args:
            tenant_id: 所属租户（硬隔离键）。
            doc_name: 文档名。

        Returns:
            {"doc_name", "source", "title", "chunks", "content"}；文档不存在时返回 None。
        """
        if self._store is None:
            raise ConfigError("KnowledgeVectorStore 尚未初始化，请先调用 initialize()。")
        from sqlalchemy import text

        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        f'SELECT content, source, title, '
                        f"COALESCE((langchain_metadata->>'chunk_index')::int, 0) AS idx "
                        f'FROM "{self._table_name}" '
                        f'WHERE tenant_id = :tenant_id AND doc_name = :doc_name '
                        f'ORDER BY idx, langchain_id'
                    ),
                    {"tenant_id": tenant_id, "doc_name": doc_name},
                )
            ).mappings().all()
        if not rows:
            return None
        return {
            "doc_name": doc_name,
            "source": rows[0]["source"],
            "title": rows[0]["title"],
            "chunks": len(rows),
            "content": "\n\n".join(r["content"] for r in rows),
        }

    async def delete_document(self, *, tenant_id: str, doc_name: str) -> None:
        """按 (tenant_id, doc_name) 删除文档全部切片（不存在时不报错）。

        Args:
            tenant_id: 所属租户。
            doc_name: 文档名。
        """
        if self._store is None:
            raise ConfigError("KnowledgeVectorStore 尚未初始化，请先调用 initialize()。")
        await self._store.adelete(filter={"tenant_id": tenant_id, "doc_name": doc_name})

    @staticmethod
    def split_document(content: str) -> list[str]:
        """通用 Markdown 等长切片（policy_manual / operation_doc 及 FAQ 回退路径使用）。

        RecursiveCharacterTextSplitter，分隔符与长度固定可复现。
        """
        if not content or not content.strip():
            return []
        from langchain_text_splitters import Language, RecursiveCharacterTextSplitter

        splitter = RecursiveCharacterTextSplitter.from_language(
            Language.MARKDOWN, chunk_size=_CHUNK_SIZE, chunk_overlap=_CHUNK_OVERLAP
        )
        return [c for c in splitter.split_text(content) if c.strip()]

    @staticmethod
    def split_faq(content: str) -> list[str]:
        """FAQ 问答对切分：以问题行为边界，一问一块（问题 + 答案及多行续行）。

        切分规则（确定性、无长度截断）：
            1. 先按空行把文档分成段落块（FAQ 惯例：问答对/小标题之间空行分隔）；
            2. 段落块内再按问题行（``Q1：`` / ``Q：`` / ``Q1:``，可带 Markdown
               列表或加粗前缀）定位边界，兼容问答之间没有空行的写法；
            3. 问题前的裸文本小标题（如「售后与退换」）以 ``【小标题】`` 前缀拼入
               紧随其后的问答块，补充检索上下文；``#`` 开头的 Markdown 主标题不拼
               （文档标题已存 metadata）；
            4. 答案的多行续行与问题同属一块；不含任何问题行的段落块丢弃；
            5. 整个文档识别不到任何问题行时返回 ``[]``，由调用方决定是否回退。

        Args:
            content: FAQ 文档原文（Markdown/纯文本）。

        Returns:
            问答对文本块列表（保持文档顺序）；空文档或无问答结构时返回空列表。
        """
        if not content or not content.strip():
            return []
        chunks: list[str] = []
        blocks = _FAQ_BLANK_LINE_PATTERN.split(content.strip())
        for block in blocks:
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            if not lines:
                continue
            q_indices = [i for i, ln in enumerate(lines) if _FAQ_QUESTION_PATTERN.match(ln)]
            if not q_indices:
                # 纯标题/纯说明段落（无问答）不入库，文档标题已在 metadata
                continue
            for pos, start in enumerate(q_indices):
                end = q_indices[pos + 1] if pos + 1 < len(q_indices) else len(lines)
                body = "\n".join(lines[start:end])
                if pos == 0 and start > 0:
                    header = lines[start - 1]
                    if not header.startswith("#"):
                        header = header.lstrip("-*+ ").strip()
                        if header:
                            body = f"【{header}】\n{body}"
                chunks.append(body)
        return chunks


# =========================================================================
# 3. BaseRetriever 适配（Agent facade / 知识检索 API 共用）
# =========================================================================


class PgVectorStoreRetriever(BaseRetriever):
    """把 KnowledgeVectorStore.search 适配成 BaseRetriever（facade / 知识检索 API 用）。"""

    def __init__(self, store: KnowledgeVectorStore) -> None:
        """绑定已初始化的向量库。

        Args:
            store: 已调用 initialize() 的 KnowledgeVectorStore。
        """
        self._store = store

    async def retrieve(
        self,
        *,
        tenant_id: str,
        query: str,
        top_k: int,
        similarity_threshold: float,
    ) -> list[dict[str, Any]]:
        """执行向量检索并保持旧 retriever 的返回形状。

        Args:
            tenant_id: 检索租户。
            query: 用户检索词。
            top_k: 返回最多条数。
            similarity_threshold: 余弦相似度下限。

        Returns:
            证据 chunk 列表（按相似度降序），每项含
            {"chunk_id","tenant_id","title","content","source","similarity","metadata"}。
        """
        return await self._store.search(
            tenant_id=tenant_id,
            query=query,
            top_k=top_k,
            similarity_threshold=similarity_threshold,
        )

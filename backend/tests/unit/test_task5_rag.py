"""T5 RAG 模块单测（PGVectorStore 重构后：Embedding 适配 + 切片/入库 + 硬隔离 + HTTP）。

覆盖：
- TR5-1 FakeEmbeddings 向量确定性：相同文本两次 embed 相同；不同文本不同；长度 = embedding_dim 且归一化
- TR5-2 已删除（LangChainEmbeddingsAdapter 随 provider 抽象层一起移除，Embeddings 协议直连）
- TR5-3 split_document：Markdown 感知切片；空内容 → []
- TR5-4 切片 ID 确定性：同 (tenant, doc, content) → 同 UUID；内容不同 → UUID 不同
- TR5-5 KnowledgeVectorStore.search：tenant_id 过滤透传、distance→similarity 换算、阈值过滤、
        空租户/空 query 短路、source 过滤
- TR5-6 KnowledgeVectorStore.ingest_document：同名文档覆盖式重建（先 adelete 后 aadd_texts）、
        metadata 注入、确定性 ID、空内容只删不加
- TR5-7 PgVectorStoreRetriever.retrieve：返回 dict 形状与 top_k/降序语义
- TR5-8 HTTP POST /api/knowledge/search：走 app.state.vector_store，强制 actor 租户
- TR5-9 HTTP POST /api/management/tenants/{t}/knowledge/documents：
        staff 同租户 201 / 跨租户 404 / consumer 404 / 非法后缀 422 / 向量库未初始化 503
- TR5-10 HTTP GET /api/management/tenants/{t}/knowledge/documents：文档级列表，
        staff 同租户 200 / 越权 404 且不触库 / 向量库未初始化 503
"""

from __future__ import annotations

import math
import uuid as _uuid
from typing import Any

import pytest
from langchain_core.documents import Document

from app.application.schemas.identity import Role, issue_demo_token
from app.domain.repositories.identity import Actor
from app.infrastructure.vectorstore import (
    KnowledgeVectorStore,
    PgVectorStoreRetriever,
    _chunk_document_id,
)
from tests.unit._fakes import FakeEmbeddings

TENANT_A = "tenant_a"
TENANT_B = "tenant_b"
ACTOR_A_STAFF = Actor(
    actor_id="a0000000-0000-0000-0000-000000000001",
    tenant_id=TENANT_A,
    role=Role.STAFF,
)
ACTOR_A_CONSUMER = Actor(
    actor_id="a0000000-0000-0000-0000-000000000003",
    tenant_id=TENANT_A,
    role=Role.CONSUMER,
)


# =========================================================================
# Helpers
# =========================================================================


class _FakePGVectorStore:
    """PGVectorStore 的最小 fake（只实现 KnowledgeVectorStore 用到的异步方法）。"""

    def __init__(self, pairs: list[tuple[Document, float]] | None = None) -> None:
        self.pairs = pairs or []
        self.search_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self.add_calls: list[dict[str, Any]] = []

    async def asimilarity_search_with_score(
        self,
        query: str,
        k: int = 4,
        filter: dict[str, Any] | None = None,  # noqa: A002
        **_: Any,
    ) -> list[tuple[Document, float]]:
        self.search_calls.append({"query": query, "k": k, "filter": filter})
        # 模拟真库语义：filter 生效（SQL WHERE），按 distance 升序（近→远）后截断 top-k
        rows = self.pairs
        if filter:
            rows = [p for p in rows if all(p[0].metadata.get(k_) == v for k_, v in filter.items())]
        return sorted(rows, key=lambda p: p[1])[:k]

    async def adelete(
        self,
        ids: list[Any] | None = None,
        filter: dict[str, Any] | None = None,  # noqa: A002
        **_: Any,
    ) -> bool | None:
        self.delete_calls.append({"ids": ids, "filter": filter})
        return None

    async def aadd_texts(
        self,
        texts: list[str],
        metadatas: list[dict[str, Any]] | None = None,
        ids: list[Any] | None = None,
        **_: Any,
    ) -> list[str]:
        self.add_calls.append({"texts": texts, "metadatas": metadatas, "ids": ids})
        return [str(i) for i in (ids or [])]


def _make_vector_store(
    fake: _FakePGVectorStore | None = None,
) -> tuple[KnowledgeVectorStore, _FakePGVectorStore]:
    """构造注入 fake 的 KnowledgeVectorStore（不触真库，initialize 未调用）。"""
    store = KnowledgeVectorStore(
        None,  # type: ignore[arg-type]  # 单测不触库，engine 仅 initialize() 使用
        FakeEmbeddings(256),
        vector_size=256,
    )
    f = fake or _FakePGVectorStore()
    store._store = f  # 单测注入点
    return store, f


def _doc(
    doc_id: str, content: str, distance: float, tenant_id: str = TENANT_A
) -> tuple[Document, float]:
    return (
        Document(
            page_content=content,
            metadata={
                "tenant_id": tenant_id,
                "source": "faq",
                "doc_name": "FAQ.md",
                "title": "FAQ",
            },
            id=doc_id,
        ),
        distance,
    )


def _auth_headers(settings: Any, actor: Actor) -> dict[str, str]:
    """签演示令牌并构造 HTTP 头（与 test_task10_agent_http 对齐）。"""
    token = issue_demo_token(
        settings.security, tenant_id=actor.tenant_id, actor_id=actor.actor_id, role=actor.role
    )
    access = getattr(token, "access_token", None)
    if not isinstance(access, str):
        raise RuntimeError("issue_demo_token 返回非 DemoTokenBundle")
    return {"X-Tenant-Id": actor.tenant_id, "Authorization": f"Bearer {access}"}


# =========================================================================
# TR5-1 Mock 向量确定性 & 维度
# =========================================================================


@pytest.mark.asyncio
async def test_tr51_mock_embedding_deterministic_and_dim() -> None:
    emb = FakeEmbeddings(256)
    v1a = emb.embed_query("手串 7 天无理由能退吗？")
    v1b = emb.embed_query("手串 7 天无理由能退吗？")
    v2 = emb.embed_query("定制款能退吗？")
    assert len(v1a) == 256
    assert len(v2) == 256
    assert v1a == v1b
    assert v1a != v2
    batch = emb.embed_documents(["a", "b", "a"])
    assert len(batch) == 3
    assert batch[0] == batch[2]
    assert batch[0] != batch[1]
    norm = math.sqrt(sum(x * x for x in v1a))
    assert abs(norm - 1.0) < 0.001


# =========================================================================
# TR5-3 split_document
# =========================================================================


def test_tr53_split_document_markdown_and_empty() -> None:
    md = "# 标题\n\n" + "\n\n".join(f"## 段落 {i}\n内容" * 3 for i in range(20))
    chunks = KnowledgeVectorStore.split_document(md)
    assert len(chunks) > 1
    assert all(c.strip() for c in chunks)
    assert KnowledgeVectorStore.split_document("") == []
    assert KnowledgeVectorStore.split_document("   \n  ") == []


def test_tr53b_split_faq_one_chunk_per_question_pair() -> None:
    """FAQ 按问答对切分：一问一块，不做等长截断；分类小标题作为上下文前缀。"""
    content = (
        "品牌与定位\n"
        "Q1：梵印阁是什么品牌？\n"
        "A： 梵印阁是一家高端手串品牌。\n"
        "\n"
        "Q2：品牌理念是什么？\n"
        "A： 质量优先，宁缺毋滥。\n"
        "\n"
        "售后与退换\n"
        "Q3：支持退货吗？\n"
        "A： 支持 30 天无理由退换。\n"
    )
    chunks = KnowledgeVectorStore.split_faq(content)
    assert len(chunks) == 3
    assert chunks[0] == "【品牌与定位】\nQ1：梵印阁是什么品牌？\nA： 梵印阁是一家高端手串品牌。"
    assert chunks[1] == "Q2：品牌理念是什么？\nA： 质量优先，宁缺毋滥。"
    assert chunks[2] == "【售后与退换】\nQ3：支持退货吗？\nA： 支持 30 天无理由退换。"
    # 每个块恰好包含一个问题行（杜绝多个问答混在同一块）
    assert all(c.count("Q") >= 1 for c in chunks)
    assert sum(c.startswith("Q") or "\nQ" in c for c in chunks) == 3


def test_tr53c_split_faq_supports_multiline_answer_and_variants() -> None:
    """多行答案随问题归入同一块；兼容无编号 Q：、ASCII 冒号与 Markdown 标题。"""
    content = (
        "# 禅饰坊 FAQ\n"
        "\n"
        "Q: 如何保养？\n"
        "A: 第一步清洁。\n"
        "第二步密封保存。\n"
        "\n"
        "Q：7 天无理由能退吗？\n"
        "A：支持，详见售后政策。\n"
    )
    chunks = KnowledgeVectorStore.split_faq(content)
    assert len(chunks) == 2
    # Markdown 主标题不进入块（title 已在 metadata）
    assert "# 禅饰坊 FAQ" not in chunks[0]
    assert "【" not in chunks[0]
    assert chunks[0] == "Q: 如何保养？\nA: 第一步清洁。\n第二步密封保存。"
    assert chunks[1] == "Q：7 天无理由能退吗？\nA：支持，详见售后政策。"


def test_tr53d_split_faq_empty_and_non_qa_fallback() -> None:
    """空内容 → []；完全没有问答结构的 FAQ 文档 → []（由入库层回退通用切分）。"""
    assert KnowledgeVectorStore.split_faq("") == []
    assert KnowledgeVectorStore.split_faq("   \n  ") == []
    assert KnowledgeVectorStore.split_faq("# 标题\n\n普通段落，没有任何问答结构。\n") == []


# =========================================================================
# TR5-4 切片确定性 UUID
# =========================================================================


def test_tr54_chunk_document_id_deterministic() -> None:
    a1 = _chunk_document_id(TENANT_A, "FAQ.md", "Q：能退吗")
    a2 = _chunk_document_id(TENANT_A, "FAQ.md", "Q：能退吗")
    b = _chunk_document_id(TENANT_A, "FAQ.md", "Q：能换吗")
    c = _chunk_document_id(TENANT_B, "FAQ.md", "Q：能退吗")
    assert a1 == a2
    assert a1 != b
    assert a1 != c
    assert isinstance(a1, _uuid.UUID)


# =========================================================================
# TR5-5 search：硬隔离 / 阈值 / 短路
# =========================================================================


@pytest.mark.asyncio
async def test_tr55_search_tenant_filter_threshold_and_shortcircuit() -> None:
    fake = _FakePGVectorStore(
        [
            _doc("60000000-0000-0000-0000-000000000001", "7 天无理由可退", 0.10),
            _doc("60000000-0000-0000-0000-000000000002", "质量问题保修 30 天", 0.50),
            _doc("60000000-0000-0000-0000-000000000003", "几乎不相关的内容", 0.95),
            _doc("60000000-0000-0000-0000-000000000004", "B 租户内容", 0.01, tenant_id=TENANT_B),
        ]
    )
    store, fake = _make_vector_store(fake)

    hits = await store.search(
        tenant_id=TENANT_A, query="退货政策", top_k=10, similarity_threshold=0.3
    )
    # filter 强制按 tenant_id（硬隔离），B 租户即使 distance 最小也不会命中
    assert fake.search_calls[0]["filter"] == {"tenant_id": TENANT_A}
    # distance → similarity = 1 - distance；阈值 0.3 过滤掉 sim=0.05 的 003
    assert [h["chunk_id"] for h in hits] == [
        "60000000-0000-0000-0000-000000000001",
        "60000000-0000-0000-0000-000000000002",
    ]
    assert hits[0]["similarity"] == pytest.approx(0.9, abs=1e-6)
    assert hits[0]["tenant_id"] == TENANT_A
    assert hits[0]["metadata"]["doc_name"] == "FAQ.md"

    # source 过滤 → filter 追加 source
    await store.search(
        tenant_id=TENANT_A, query="q", top_k=4, similarity_threshold=0.0, source="policy_manual"
    )
    assert fake.search_calls[1]["filter"] == {"tenant_id": TENANT_A, "source": "policy_manual"}

    # 空租户 / 空 query 短路：不触底库
    assert await store.search(tenant_id="", query="q", top_k=4, similarity_threshold=0.0) == []
    assert (
        await store.search(tenant_id=TENANT_A, query="   ", top_k=4, similarity_threshold=0.0) == []
    )
    assert len(fake.search_calls) == 2

    # 未初始化 → ConfigError
    raw = KnowledgeVectorStore(None, FakeEmbeddings(256), vector_size=256)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="尚未初始化"):
        await raw.search(tenant_id=TENANT_A, query="q", top_k=1, similarity_threshold=0.0)


# =========================================================================
# TR5-6 ingest_document：覆盖式重建 + 幂等 ID
# =========================================================================


@pytest.mark.asyncio
async def test_tr56_ingest_document_replaces_and_upserts() -> None:
    store, fake = _make_vector_store()
    md = "# 标题\n\n" + "\n\n".join(f"## 段落 {i}\n内容" * 3 for i in range(20))

    ids = await store.ingest_document(
        tenant_id=TENANT_A, doc_name="FAQ.md", content=md, source="faq", title="FAQ"
    )

    # 1) 覆盖式：先按 (tenant_id, doc_name) 删旧
    assert fake.delete_calls[0] == {
        "ids": None,
        "filter": {"tenant_id": TENANT_A, "doc_name": "FAQ.md"},
    }
    # 2) 切片入库：metadata 注入 tenant_id/source/doc_name/title + chunk_index 写入顺序；ID 确定性且与内容绑定
    assert len(fake.add_calls) == 1
    call = fake.add_calls[0]
    assert len(call["texts"]) == len(ids) > 1
    for i, meta in enumerate(call["metadatas"]):
        assert meta == {
            "tenant_id": TENANT_A,
            "source": "faq",
            "doc_name": "FAQ.md",
            "title": "FAQ",
            "chunk_index": i,
        }
    assert call["ids"] == [_chunk_document_id(TENANT_A, "FAQ.md", t) for t in call["texts"]]
    assert set(ids) == set(call["ids"])

    # 重复上传同内容 → 相同 ID 集合（幂等）
    ids2 = await store.ingest_document(
        tenant_id=TENANT_A, doc_name="FAQ.md", content=md, source="faq", title="FAQ"
    )
    assert ids2 == ids

    # 空内容 → 只删不加
    out = await store.ingest_document(tenant_id=TENANT_A, doc_name="empty.md", content="  ")
    assert out == []
    assert len(fake.add_calls) == 2  # 没有新增调用
    assert fake.delete_calls[-1]["filter"] == {"tenant_id": TENANT_A, "doc_name": "empty.md"}


@pytest.mark.asyncio
async def test_tr56b_ingest_routes_faq_to_qa_split_and_others_to_length_split() -> None:
    """source=faq 按问答对入库（一问一块）；其他 source 保持 Markdown 等长切分。"""
    store, fake = _make_vector_store()
    faq_md = (
        "售后与退换\n"
        "Q1：支持退货吗？\nA： 支持 30 天无理由退换。\n\n"
        "Q2：运费谁承担？\nA： 质量问题商家承担。\n"
    )

    ids = await store.ingest_document(
        tenant_id=TENANT_A, doc_name="brand FAQ.md", content=faq_md, source="faq"
    )
    assert len(ids) == 2
    added = fake.add_calls[0]["texts"]
    assert added[0].startswith("【售后与退换】\nQ1：")
    assert added[1] == "Q2：运费谁承担？\nA： 质量问题商家承担。"

    # 非 FAQ 来源：同一份问答文本不再按问题边界切（交给通用 Markdown 切分器）
    await store.ingest_document(
        tenant_id=TENANT_A, doc_name="policy.md", content=faq_md, source="policy_manual"
    )
    policy_texts = fake.add_calls[1]["texts"]
    assert policy_texts == KnowledgeVectorStore.split_document(faq_md)

    # FAQ 文档但内容不含任何问答结构 → 回退通用切分，避免整篇丢失
    plain_md = "# 标题\n\n" + "\n\n".join(f"## 段落 {i}\n内容" * 3 for i in range(20))
    fallback_ids = await store.ingest_document(
        tenant_id=TENANT_A, doc_name="weird-faq.md", content=plain_md, source="faq"
    )
    assert len(fallback_ids) == len(KnowledgeVectorStore.split_document(plain_md)) > 1


# =========================================================================
# TR5-7 PgVectorStoreRetriever.retrieve 形状 & top_k / 降序
# =========================================================================


@pytest.mark.asyncio
async def test_tr57_retriever_shape_topk_and_order() -> None:
    fake = _FakePGVectorStore(
        [
            _doc("70000000-0000-0000-0000-000000000001", "最相关", 0.05),
            _doc("70000000-0000-0000-0000-000000000002", "次相关", 0.20),
            _doc("70000000-0000-0000-0000-000000000003", "第三", 0.60),
        ]
    )
    store, _ = _make_vector_store(fake)
    retriever = PgVectorStoreRetriever(store)

    hits = await retriever.retrieve(
        tenant_id=TENANT_A, query="退货", top_k=2, similarity_threshold=0.0
    )
    assert len(hits) == 2
    assert hits[0]["similarity"] >= hits[1]["similarity"]
    for h in hits:
        assert set(h) >= {
            "chunk_id",
            "tenant_id",
            "title",
            "content",
            "source",
            "similarity",
            "metadata",
        }
        assert h["tenant_id"] == TENANT_A


# =========================================================================
# TR5-8 HTTP POST /api/knowledge/search
# =========================================================================


@pytest.mark.asyncio
async def test_tr58_http_search_uses_actor_tenant(test_app, test_settings, client) -> None:
    fake = _FakePGVectorStore(
        [
            _doc("80000000-0000-0000-0000-000000000001", "7 天无理由可退", 0.10),
            _doc(
                "80000000-0000-0000-0000-000000000002", "B 租户不应泄露", 0.01, tenant_id=TENANT_B
            ),
        ]
    )
    test_app.state.bundle.vector_store = _make_vector_store(fake)[0]

    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, ACTOR_A_CONSUMER)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(ACTOR_A_CONSUMER)
    try:
        resp = await client.post(
            "/api/knowledge/search",
            json={"query": "7 天无理由能退吗", "top_k": 5, "similarity_threshold": 0.0},
            headers=headers,
        )
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)

    assert resp.status_code == 200, f"HTTP 失败: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["total"] == 1
    hit = body["hits"][0]
    assert hit["tenant_id"] == TENANT_A
    assert hit["chunk_id"] == "80000000-0000-0000-0000-000000000001"
    assert hit["similarity"] == pytest.approx(0.9, abs=1e-6)
    assert hit["doc_name"] == "FAQ.md"
    assert hit["chunk"] is None
    # 检索强制用 actor 租户，而不是客户端可注入的任何值
    assert fake.search_calls[0]["filter"] == {"tenant_id": TENANT_A}


@pytest.mark.asyncio
async def test_tr58b_http_search_503_when_store_missing(test_app, test_settings, client) -> None:
    test_app.state.bundle.vector_store = None
    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, ACTOR_A_CONSUMER)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(ACTOR_A_CONSUMER)
    try:
        resp = await client.post("/api/knowledge/search", json={"query": "q"}, headers=headers)
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)
    assert resp.status_code == 503


# =========================================================================
# TR5-9 HTTP 上传文档接口
# =========================================================================

_UPLOADED_MD = "# 禅饰坊 FAQ\n\nQ：7 天无理由能退吗？\nA：支持，详见售后政策。\n"
_UPLOAD_URL = "/api/management/tenants/tenant_a/knowledge/documents"


@pytest.mark.asyncio
async def test_tr59_upload_document_success(test_app, test_settings, client) -> None:
    store, fake = _make_vector_store()
    test_app.state.bundle.vector_store = store

    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, ACTOR_A_STAFF)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(ACTOR_A_STAFF)
    try:
        resp = await client.post(
            _UPLOAD_URL,
            files={"file": ("禅饰坊 FAQ.md", _UPLOADED_MD.encode("utf-8"), "text/markdown")},
            data={"source": "faq", "title": "禅饰坊 FAQ"},
            headers=headers,
        )
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)

    assert resp.status_code == 201, f"HTTP 失败: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["tenant_id"] == TENANT_A
    assert body["doc_name"] == "禅饰坊 FAQ.md"
    assert body["source"] == "faq"
    assert body["chunks"] >= 1
    assert len(body["chunk_ids"]) == body["chunks"]
    assert fake.add_calls[0]["metadatas"][0]["tenant_id"] == TENANT_A
    assert fake.add_calls[0]["metadatas"][0]["title"] == "禅饰坊 FAQ"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "target"),
    [
        (ACTOR_A_CONSUMER, TENANT_A),  # consumer 无写权限 → 404
        (ACTOR_A_STAFF, TENANT_B),  # 跨租户 → 404
    ],
)
async def test_tr59b_upload_document_forbidden_404(
    test_app, test_settings, client, actor: Actor, target: str
) -> None:
    test_app.state.bundle.vector_store = _make_vector_store()[0]
    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, actor)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(actor)
    try:
        resp = await client.post(
            f"/api/management/tenants/{target}/knowledge/documents",
            files={"file": ("a.md", b"x")},
            headers=headers,
        )
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tr59c_upload_document_rejects_bad_suffix(test_app, test_settings, client) -> None:
    test_app.state.bundle.vector_store = _make_vector_store()[0]
    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, ACTOR_A_STAFF)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(ACTOR_A_STAFF)
    try:
        resp = await client.post(
            _UPLOAD_URL,
            files={"file": ("evil.exe", b"MZ...")},
            headers=headers,
        )
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_tr59d_upload_document_503_when_store_missing(
    test_app, test_settings, client
) -> None:
    test_app.state.bundle.vector_store = None
    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, ACTOR_A_STAFF)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(ACTOR_A_STAFF)
    try:
        resp = await client.post(
            _UPLOAD_URL,
            files={"file": ("a.md", b"x")},
            headers=headers,
        )
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)
    assert resp.status_code == 503


# =========================================================================
# TR5-10 HTTP GET /api/management/tenants/{t}/knowledge/documents（文档级列表）
# =========================================================================


def _stub_list_documents(store: KnowledgeVectorStore, rows: list[dict[str, Any]]) -> list[str]:
    """单测不触真库（engine=None）：用 async stub 替换 list_documents，并记录 tenant_id。"""
    seen: list[str] = []

    async def _fake_list(*, tenant_id: str) -> list[dict[str, Any]]:
        seen.append(tenant_id)
        return rows

    store.list_documents = _fake_list  # type: ignore[method-assign]  # 单测注入点
    return seen


@pytest.mark.asyncio
async def test_tr510_list_documents_success(test_app, test_settings, client) -> None:
    store, _ = _make_vector_store()
    test_app.state.bundle.vector_store = store
    seen = _stub_list_documents(
        store,
        [
            {"doc_name": "FAQ.md", "source": "faq", "title": "禅饰坊 FAQ", "chunks": 12},
            {"doc_name": "policy.md", "source": "policy_manual", "title": None, "chunks": 3},
        ],
    )

    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, ACTOR_A_STAFF)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(ACTOR_A_STAFF)
    try:
        resp = await client.get(_UPLOAD_URL, headers=headers)
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)

    assert resp.status_code == 200, f"HTTP 失败: {resp.status_code} {resp.text}"
    body = resp.json()
    assert [d["doc_name"] for d in body] == ["FAQ.md", "policy.md"]
    assert body[0] == {
        "tenant_id": TENANT_A,
        "doc_name": "FAQ.md",
        "source": "faq",
        "title": "禅饰坊 FAQ",
        "chunks": 12,
    }
    # 强制 actor 租户，不信任任何客户端输入
    assert seen == [TENANT_A]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "target"),
    [
        (ACTOR_A_CONSUMER, TENANT_A),  # consumer 无管理权限 → 404
        (ACTOR_A_STAFF, TENANT_B),  # 跨租户 → 404
    ],
)
async def test_tr510b_list_documents_forbidden_404(
    test_app, test_settings, client, actor: Actor, target: str
) -> None:
    store, _ = _make_vector_store()
    test_app.state.bundle.vector_store = store
    seen = _stub_list_documents(store, [])
    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, actor)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(actor)
    try:
        resp = await client.get(
            f"/api/management/tenants/{target}/knowledge/documents",
            headers=headers,
        )
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)
    assert resp.status_code == 404
    assert seen == []  # 越权请求不得触库


@pytest.mark.asyncio
async def test_tr510c_list_documents_503_when_store_missing(
    test_app, test_settings, client
) -> None:
    test_app.state.bundle.vector_store = None
    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, ACTOR_A_STAFF)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(ACTOR_A_STAFF)
    try:
        resp = await client.get(_UPLOAD_URL, headers=headers)
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)
    assert resp.status_code == 503


# =========================================================================
# TR5-11 HTTP GET /api/management/tenants/{t}/knowledge/documents/{doc_name}
# =========================================================================


def _stub_get_document(
    store: KnowledgeVectorStore, row: dict[str, Any] | None
) -> list[tuple[str, str]]:
    """单测不触真库（engine=None）：用 async stub 替换 get_document，并记录调用参数。"""
    seen: list[tuple[str, str]] = []

    async def _fake_get(*, tenant_id: str, doc_name: str) -> dict[str, Any] | None:
        seen.append((tenant_id, doc_name))
        return row

    store.get_document = _fake_get  # type: ignore[method-assign]  # 单测注入点
    return seen


@pytest.mark.asyncio
async def test_tr511_get_document_success(test_app, test_settings, client) -> None:
    store, _ = _make_vector_store()
    test_app.state.bundle.vector_store = store
    seen = _stub_get_document(
        store,
        {
            "doc_name": "FAQ.md",
            "source": "faq",
            "title": "禅饰坊 FAQ",
            "chunks": 2,
            "content": "Q1：…\n\nQ2：…",
        },
    )

    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, ACTOR_A_STAFF)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(ACTOR_A_STAFF)
    try:
        resp = await client.get(f"{_UPLOAD_URL}/FAQ.md", headers=headers)
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)

    assert resp.status_code == 200, f"HTTP 失败: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["tenant_id"] == TENANT_A
    assert body["doc_name"] == "FAQ.md"
    assert body["chunks"] == 2
    assert body["content"] == "Q1：…\n\nQ2：…"
    assert seen == [(TENANT_A, "FAQ.md")]


@pytest.mark.asyncio
async def test_tr511b_get_document_not_found_404(test_app, test_settings, client) -> None:
    store, _ = _make_vector_store()
    test_app.state.bundle.vector_store = store
    _stub_get_document(store, None)

    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, ACTOR_A_STAFF)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(ACTOR_A_STAFF)
    try:
        resp = await client.get(f"{_UPLOAD_URL}/missing.md", headers=headers)
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "target"),
    [
        (ACTOR_A_CONSUMER, TENANT_A),  # consumer 无管理权限 → 404
        (ACTOR_A_STAFF, TENANT_B),  # 跨租户 → 404
    ],
)
async def test_tr511c_get_document_forbidden_404(
    test_app, test_settings, client, actor: Actor, target: str
) -> None:
    store, _ = _make_vector_store()
    test_app.state.bundle.vector_store = store
    seen = _stub_get_document(store, None)
    from app.application.auth import REQUEST_ACTOR_CONTEXT

    headers = _auth_headers(test_settings, actor)
    ctx_tok = REQUEST_ACTOR_CONTEXT.set(actor)
    try:
        resp = await client.get(
            f"/api/management/tenants/{target}/knowledge/documents/FAQ.md",
            headers=headers,
        )
    finally:
        REQUEST_ACTOR_CONTEXT.reset(ctx_tok)
    assert resp.status_code == 404
    assert seen == []  # 越权请求不得触库

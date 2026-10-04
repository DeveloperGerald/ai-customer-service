# RAG 重构：LangChain 原生 PGVector + 文档上传 Ingestion 规范

## Overview
- **Summary**: 将当前自研的 `KnowledgeChunkORM` + 手写 `PgVectorRetriever` + `MockEmbeddingProvider` 整套 RAG 栈，整体替换为 LangChain 原生生态：`langchain-postgres.PGVector`（向量存储）+ `langchain_core.embeddings.DeterministicFakeEmbedding` / `langchain_openai.OpenAIEmbeddings`（嵌入模型）+ `langchain_text_splitters`（文档切分）。同时新增一个按文件上传的文档入库 API，支持将 FAQ/政策文档按问答对或固定长度切分后灌入向量库，并通过 collection + metadata 双层机制实现多租户硬隔离。
- **Purpose**: 消除当前为"兼容离线环境"手写的 SQL 向量查询、JSON 反序列化 embedding、自造 mock 嵌入等复杂代码，直接复用 LangChain 官方生产级封装，减少自研代码面，同时满足面试演示"RAG + 多租户隔离 + 文档上传 Ingestion"的完整闭环展示。
- **Target Users**: 面试官（演示文档上传 → 切分 → 向量入库 → 语义检索 → Agent 引用完整链路）、技术评审（验证架构简洁性、租户隔离、幂等安全）。

## Goals
1. **PGVector 原生接入**：向量存储、索引、相似度查询、metadata 过滤全部走 `langchain-postgres.PGVector`，不再手写 SQL `<=>` 和 Python 侧余弦。
2. **Embedding 原生接入**：Mock 模式用 `langchain_core.embeddings.DeterministicFakeEmbedding`，OpenAI 模式用 `langchain_openai.OpenAIEmbeddings`，删除自研 `MockEmbeddingProvider` / `OpenAIEmbeddingProvider` / `BaseEmbeddingProvider` 抽象层（或仅留薄 wrapper 供 `build_embedding_provider` 工厂兼容）。
3. **多租户硬隔离**：每个租户一个独立 `collection_name`（如 `tenant_梵印阁`）+ 每条 embedding 的 metadata 里强制带 `tenant_id`，且 `similarity_search` 时同时加 `filter={"tenant_id": tenant_id}`（collection + metadata 双重防线，面试展示防御深度）。
4. **FAQ 问答对切分**：对 `docs/knowledge/` 下三个 FAQ 的规整结构（`Q1：...\nA：...`），按问答对语义切分，不做粗暴固定长度截断，保障检索精度。
5. **文档上传 Ingestion API**：暴露 `POST /api/management/tenants/{target_tenant_id}/knowledge/ingest`（multipart/form-data）接口，Staff/Admin 同租户权限，上传 `.md` / `.txt` 文件后自动切分 + 嵌入 + 入库 + 去重，返回新增/跳过 chunk 统计。
6. **现有接口无缝兼容**：`/api/knowledge/search`（消费者语义搜索）和 `/api/management/.../knowledge/chunks`（chunks 增删查）两套 REST 的请求/响应 Schema 保持不变，仅内部实现换为 PGVector。
7. **Agent 链路无缝兼容**：`rag_retrieve_node` + `AgentNodeContext.retriever` 读取 rag_hits 的逻辑不变，仅 retriever 实现替换为 `PGVector.as_retriever(search_kwargs={"filter": {...}, "k": top_k})` 的包装层。

## Non-Goals
1. **不做 Hybrid Search**：本次不接 tsvector + RRF 的全文×向量混合检索，仅用纯向量相似度 + metadata filter（面试 demo 精度足够）。
2. **不做 RecordManager Indexing API**：不用 LangChain `SQLRecordManager` 做全量增量追踪，仅用 `content_hash + tenant_id + source` 的 metadata UNIQUE 约束实现简单幂等（PGVector 原生表无 UNIQUE 约束，改为 ingestion 前先查 hash 是否存在再决定是否 add）。
3. **不做种子数据自动灌入**：`docs/knowledge/` 三个 FAQ 文件仅提供给用户手动调 Ingestion API 上传，不在 `seed_tenants.py` 里自动 ingest（用户决策）。
4. **不保留旧 `knowledge_chunks` 表**：方案 A（完全替换），旧表、旧 Repository、旧 PgVectorRetriever、旧 ORM 在新代码全部替换后删除，不做双写/兼容读取。
5. **不改 Agent 图拓扑**：`intent_classify → rag_retrieve → agent/faq_node` 的图结构保持不变，只替换 `rag_retrieve_node` 内部检索实现。
6. **不做图片/非文本文档**：Ingestion API 仅支持 `.md` 和 `.txt`，上传 PDF/Word 直接 400 拒。

## Background & Context
### 当前实现存在的问题
1. **自研代码过重**：`PgVectorRetriever` 同时维护 `_retrieve_py_cosine`（TEXT 列 JSON 反序列化）和 `_retrieve_native`（原生 `<=>`）两套路径，加上自造的 `MockEmbeddingProvider`、`_cosine()` 等，累计约 300+ 行，实际 LangChain 官方已全部原生支持。
2. **EmbeddingProvider 抽象冗余**：`BaseEmbeddingProvider`（`embed_texts`/`embed_query`）本质和 LangChain 标准 `Embeddings`（`embed_documents`/`embed_query`，含异步版）接口完全同构，可直接换原生，无需自研一层。
3. **FAQ 切分无结构感知**：`seed_tenants.py` 里按 900 字符硬切，`Q`/`A` 可能被切断，语义破坏。当前三个 FAQ 文档严格按 `Q数字：xxx\nA：xxx` 结构，可按问答对精准切分。
4. **Ingestion 能力缺失**：旧系统只有"单 chunk 手工 POST"的管理接口，无法一键上传一整份 Markdown 文档，演示体验差。

### 已确认的 5 项决策（来自用户 2026-09-19 输入）
1. **向量库封装**：选 `langchain.vectorstores.pgvector.PGVector`（实际包名 `langchain-postgres`，底层 `psycopg3`）。
2. **表结构策略**：方案 A，完全替换为 LangChain 原生 collection/embedding 两张表。
3. **切分策略**：FAQ 按问答对切分（若上传非 FAQ 文档则 fallback 为 `RecursiveCharacterTextSplitter` 固定长度）。
4. **Ingestion API 方式**：方案 1，上传文件（multipart/form-data），暂不提供纯 JSON 文本提交。
5. **Embedding 选型**：优先用 LangChain 原生 `DeterministicFakeEmbedding` / `OpenAIEmbeddings`，能不用自研就不用。
6. **种子数据**：不自动导入，接口好用户自行调用。

### 项目依赖现状（backend/pyproject.toml + venv pip list）
- 已满足：`langchain 0.3.30`、`langchain-core 0.3.86`（含 `DeterministicFakeEmbedding`）、`langchain-openai 0.3.35`（含 `OpenAIEmbeddings`）、`langchain-text-splitters 0.3.11`、`pgvector 0.3.x`、`psycopg[binary,pool] 3.x`。
- **需新增**：`langchain-postgres`（独立包，不包含在 `langchain-community` 里，从 langchain 0.3 起独立发布）。

## Functional Requirements

### 1. 依赖与基础设施
- **FR-1 pyproject 新增依赖**：`pyproject.toml` 的 `[project.optional-dependencies].llm` 列表追加 `langchain-postgres>=0.1,<1`（或单独建 `vector` extra 合并进 `llm`），确保 `pip install -e ".[llm]"` 即可获得完整 RAG 能力。
- **FR-2 连接复用现有 DB**：PGVector 的 `connection` 参数直接复用现有 `DATABASE_URL`（注意：`langchain-postgres` 要求驱动名是 `postgresql+psycopg://`，若现有配置是 `postgresql+psycopg://` 则直接用；若是 `postgresql+asyncpg://` 则需要在配置层做兼容替换）。
- **FR-3 扩展自动创建**：应用启动（FastAPI lifespan）或 PGVector 首次 `__init__` 时，执行 `CREATE EXTENSION IF NOT EXISTS vector`（和旧迁移逻辑一致，失败不抛，兼容无 pgvector 的离线环境）。

### 2. Embedding 工厂层（LangChain 原生适配）
- **FR-4 工厂函数签名不变**：`backend/app/infrastructure/llm/providers.py` 中的 `build_embedding_provider(settings)` 签名保持不变，但返回值改为**同时实现两个接口**的对象：
  - 对外（给 `PGVector`、`as_retriever` 用）：继承 `langchain_core.embeddings.Embeddings`，实现 `embed_documents / aembed_documents / embed_query / aembed_query`。
  - 对内（兼容旧代码少量直接调用 `embed_texts` 的位置）：如果有其他模块直接调 `provider.embed_texts()`，则在 wrapper 上挂一个同名 alias 方法（`embed_texts = embed_documents`）。
- **FR-5 Mock 实现切原生**：当 `settings.llm.embedding_provider == "mock"` 时，factory 返回 `DeterministicFakeEmbedding(size=settings.llm.embedding_dim)` 包一层 alias（不再走 `MockEmbeddingProvider` SHA256→random 逻辑，因为 `DeterministicFakeEmbedding` 内部就是 SHA256 种子 + numpy rng.normal，语义等价且官方维护）。
- **FR-6 OpenAI 实现切原生**：当 `settings.llm.embedding_provider == "openai"` 时，factory 返回 `langchain_openai.OpenAIEmbeddings(model=settings.llm.embedding_model, dimensions=settings.llm.embedding_dim, api_key=..., base_url=..., timeout=..., max_retries=...)`，完全替代自研 `OpenAIEmbeddingProvider` 的分批/超时/维度裁剪逻辑（`OpenAIEmbeddings` 原生已支持分批 + timeout + max_retries + dimensions 参数）。

### 3. 向量存储与多租户隔离
- **FR-7 PGVector 单例工厂**：新增 `build_pgvector_store(embeddings, connection, collection_name) -> PGVector` 工厂函数，统一封装：
  - `embeddings=Embeddings`（FR-4 返回的对象）
  - `connection=str`（FR-2 的 DATABASE_URL，驱动名 psycopg）
  - `collection_name=str`（按租户动态切换，如 `tenant_{tenant_id}`，**不**用全局固定 collection）
  - `embedding_dimension=int`（显式传 settings.llm.embedding_dim，防止表建错维度）
  - `use_jsonb=True`（metadata 用 JSONB 列，支持 `@>` 过滤运算符，比 `JSON` 快）
- **FR-8 租户隔离双层防线**：
  - **防线 1（物理 collection 级）**：每个租户的 FAQ/政策 chunk 只写入专属 `collection_name = f"tenant_{tenant_id}"`（注意 collection_name 规则：允许字母数字下划线，tenant_id 若含中文/连字符则先做 slugify 或直接用 tenant_id 的值；实际 `langchain-postgres` 支持任意字符串作 collection_name，直接用原始 tenant_id 字符串即可，因为 UUID 是纯 ASCII）。
  - **防线 2（metadata filter 级）**：每次 `add_documents` 时每个 Document 的 `metadata` dict 强制写入 `tenant_id`、`source`、`content_hash`、`created_by`、`title`；每次 `similarity_search` / `as_retriever` 搜索时强制带 `filter={"tenant_id": tenant_id}`（即使 collection 已隔离，filter 仍保留，作为"越权防御深度"面试讲解点；且 DB 侧执行 `WHERE metadata @> '{"tenant_id": ...}'` 是 JSONB 索引友好操作）。
- **FR-9 collection 懒创建**：PGVector 构造时若 collection 不存在自动 `create_collection_if_not_exists`（langchain-postgres 原生支持，无需手工 DDL）；旧的 `alembic/versions/0002_policy.py` 中 `knowledge_chunks` 相关建表语句保留（仅保留，不再使用），并追加新迁移版本 0003 用于：(a) `CREATE EXTENSION IF NOT EXISTS vector`（从 0002 移动到独立 migration 确保执行顺序）、(b) 记录"langchain_pg_collection/langchain_pg_embedding 表由 PGVector 动态创建，不通过 Alembic 管理"的说明性注释（不对这两张表做 Alembic 版本化，避免 PGVector 内部 schema 升级时冲突）。

### 4. FAQ 问答对切分器
- **FR-10 QA Pair 切分器**：新增 `QAPairSplitter`（可放在 `backend/app/infrastructure/llm/chunking.py`），输入 `text: str`（完整 FAQ Markdown），输出 `list[Document]`，每条 Document 对应一组 `Q+A`：
  - **正则识别**：用 `r'Q\s*(\d+)[：:]\s*(.*?)\n\s*A[：:]\s*(.*?)(?=\n\s*Q\s*\d+[：:]|\Z)'`（re.DOTALL）按问答对拆分。
  - **chunk 字段**：
    - `page_content = f"Q：{q_text}\nA：{a_text}"`（规范化空格/冒号，保证检索时用户问句能匹配 Q 或 A）
    - `metadata = {"qa_index": int(qa_num), "title": f"Q{qa_num}：{q_text[:50]}...（超 50 截断）", "chunk_type": "qa_pair"}`
  - **兼容兜底**：若正则提取到的问答对数量 < 3（暗示上传非 FAQ 文档 / 结构不规整），则 fallback 到 `langchain_text_splitters.MarkdownHeaderTextSplitter` + `RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=80)` 的标准管线，并给每条 chunk metadata 打 `chunk_type="fixed_length"`。
- **FR-11 content_hash 计算**：每条 Document 在入库前计算 `content_hash = SHA256(tenant_id.encode() + source.encode() + page_content.encode()).hexdigest()`，写入 metadata（和旧 `KnowledgeChunkORM.content_hash` 算法一致，保证跨方案哈希值可对比）。

### 5. Ingestion 上传 API
- **FR-12 API 路由与鉴权**：新增 `POST /api/management/tenants/{target_tenant_id}/knowledge/ingest`（放在 `backend/app/api/management_knowledge.py` 或新文件 `management_ingest.py`；因和 knowledge 管理强相关，建议放在 `management_knowledge.py` 追加路由），要求：
  - **鉴权**：复用现有 `require_actor` + `_ensure_can_modify_tenant`（Staff/Admin 且 `actor.tenant_id == target_tenant_id`；跨租户 Admin 按项目既有惯例返回 404）。
  - **Content-Type**：`multipart/form-data`。
  - **Form 字段**：
    - `file: UploadFile`（必填；仅允许 `.md`/`.markdown`/`.txt` 后缀，mime `text/*`，单文件 ≤ 10MB，超限 413）
    - `source: ChunkSource`（选填；枚举 `faq` / `policy_manual` / `operation_doc`；默认 `faq`）
    - `chunk_strategy: Literal["auto", "qa_pair", "fixed_length"]`（选填；默认 `auto` = 先 QA Pair 切分，不达标则 fallback fixed）
    - `chunk_size: int`（选填；仅 fixed 模式生效；默认 800，范围 100-4000）
    - `chunk_overlap: int`（选填；仅 fixed 模式生效；默认 80，范围 0-500，≤ chunk_size//2）
- **FR-13 Ingestion 幂等去重**：上传文件 → 切分为 `docs: list[Document]` 后，先对每条 doc 算 `content_hash`，然后对本 `(tenant_id, source)` collection 执行一次批量 metadata 查询：`filter={"tenant_id": tid, "source": src, "content_hash": {"$in": [hash1, hash2, ...]}}`（langchain-postgres filter 语法 `$in` 原生支持），收集已存在 hash 集合，只把 `hash not in existing_hashes` 的 Document 调 `PGVector.aadd_documents` 写入，剩余标记为 `skipped_duplicates`。
- **FR-14 Ingestion 响应**：`201 Created`，响应体 JSON：
  ```json
  {
    "tenant_id": "...",
    "source": "faq",
    "file_name": "梵印阁 FAQ.md",
    "total_chunks_extracted": 85,
    "chunks_added": 82,
    "chunks_skipped_duplicate": 3,
    "chunk_strategy_used": "qa_pair",
    "sample_hashes": { "added": ["sha...(前3条)"], "skipped": ["sha...(前3条)"] }
  }
  ```
  （sample_hashes 用于用户快速验证幂等：同一文件第二次上传应返回 chunks_added=0，skipped=N）。
- **FR-15 异常处理**：
  - 文件后缀非白名单 → `400 {"code": "INVALID_FILE_TYPE", "detail": "仅支持 .md / .txt"}`
  - 超过 10MB → `413 {"code": "FILE_TOO_LARGE", "detail": "单文件最大 10MB"}`
  - 切分后 total_chunks_extracted == 0（空文档 / 无可识别内容）→ `400 {"code": "NO_CHUNK_EXTRACTED", "detail": "未提取到任何可入库内容"}`
  - 其它错误 → 统一走项目既有 `AppError` → HTTP 中间件转换机制。

### 6. 现有检索与管理接口的内部实现替换
- **FR-16 检索接口替换**：`POST /api/knowledge/search` + `GET /api/knowledge/search`（`backend/app/api/knowledge.py`）内部实现，从"PgVectorRetriever._retrieve_xxx 调 SQL"替换为：
  1. `store = build_pgvector_store(embeddings, conn, collection_name=f"tenant_{actor.tenant_id}")`
  2. `docs_and_scores = await store.asimilarity_search_with_score(query=req.query, k=req.top_k, filter={"tenant_id": actor.tenant_id, **(req.source and {"source": req.source})})`
  3. 对结果做 `similarity = 1 - score`（因为 `asimilarity_search_with_score` 返回的是 cosine **距离** ∈ [0, 2]，需换算为用户旧接口约定的 cosine **相似度** ∈ [-1, 1]，再 `max(-1, min(1, 1 - score))` 裁剪）。
  4. `similarity < req.similarity_threshold` 的条目过滤掉。
  5. 每条结果映射回 `KnowledgeSearchResponse.hits[]` 的 schema（chunk_id/tenant_id/title/content/source/similarity）：chunk_id 用 PGVector 返回的 Document `id` 字段（`langchain-postgres` 0.1.x 里每条 Document 入库后会把 id 回写到 metadata 或单独返回；若不回写则由 ingestion 时预生成 UUID 并写入 metadata `chunk_id` 字段），title/content/source 从 metadata 取，tenant_id 从 metadata/actor 任一取。
- **FR-17 Chunks 管理接口替换**：`GET / POST / DELETE /api/management/tenants/{tid}/knowledge/chunks`（`management_knowledge.py`）内部实现：
  - **GET list**：`store.similarity_search_with_score(query="", k=limit+offset, filter={"tenant_id": tid, **(source and {"source": source})})` → 去重 + 按 offset 截断（注：PGVector 没有"全量列 metadata"的原生 API，若面试 demo 需要真分页列表，则在 metadata 里单独存 `created_at`，并加一个轻量 SQL：`SELECT uuid, cmetadata FROM langchain_pg_embedding e JOIN langchain_pg_collection c ON e.collection_id = c.id WHERE c.name = :collection AND (cmetadata->>'tenant_id') = :tid [AND (cmetadata->>'source') = :src] ORDER BY (cmetadata->>'created_at') DESC LIMIT :limit OFFSET :offset`；这条 SQL 只查 metadata 不查 embedding，允许直接用 `psycopg.AsyncConnection` 执行，不走 PGVector 封装，因为 list 场景本身不是向量搜索）。
  - **POST create**：接收旧 `KnowledgeChunkCreate`（title, content, source）→ 构造成单条 Document → 算 `content_hash` → 执行 FR-13 的去重检查 → `store.aadd_documents([doc], ids=[uuid4().hex])` → 返回和旧接口一致的 `KnowledgeChunkRead`。
  - **DELETE /{chunk_id}**：`store.adelete([chunk_id])`（langchain-postgres `PGVector.adelete` 原生支持），成功 204；chunk_id 不存在/跨租户 → 404（通过先 filter 再 delete 或先查 metadata 校验 tenant_id 再删，确保越权防御）。

### 7. Agent 图内 RAG 节点替换
- **FR-18 rag_retrieve_node 内部改造**：`backend/app/application/agent/nodes.py: rag_retrieve_node(state, ctx)`：
  - 原 `ctx.retriever.retrieve(tenant_id, query, top_k, threshold)` 调用保留，但 `ctx.retriever` 实现改为新的 `LangChainRetrieverAdapter(BaseRetriever)`（包装 `PGVector.as_retriever`，对外保持旧 `BaseRetriever.retrieve()` 方法签名，不影响上层节点/测试）。
  - 新 Adapter 在 `retrieve()` 内部：根据 tenant_id 动态切换 `collection_name`，调用 `as_retriever(search_kwargs={"k": top_k, "filter": {"tenant_id": tenant_id}}).ainvoke(query)` 拿 docs，再对每条用 metadata 里提前记录的 embedding 向量 / 或 query 向量与 doc 向量重算相似度（若 `PGVector.as_retriever` 返回的不带 score，则退一步：因为 FAISS/PGVector 的 `invoke` 确实不返回 score 带分数，这时要调 `asimilarity_search_with_score` 而不是 invoke，所以 Adapter 里应该直接用 `store.asimilarity_search_with_score` 而不是 `as_retriever().ainvoke`，以稳定拿到 score → 转 similarity，再按 threshold 过滤）。
  - 最终返回 `rag_hits` 格式和旧接口 100% 一致（含 chunk_id/tenant_id/title/content/source/similarity/read 字段），`_build_agent_system_prompt` 拼"【RAG 参考片段】"的逻辑零修改。
- **FR-19 mock_rag_hits_from_db 的去向**：离线 mock fallback（`_mock_rag_hits_from_db`，SQL 关键词打分）保留，仅在 `build_pgvector_store` 初始化失败（比如 `CREATE EXTENSION vector` 失败）或 `DATABASE_URL` 未配置时作为最后兜底，不进正常链路。

### 8. 旧代码清理
- **FR-20 清理清单**（在新链路跑通 + 所有测试通过后，一次性执行）：
  - 删除 `PgVectorRetriever` 类 + `_cosine` 函数 + `BaseEmbeddingProvider` + `MockEmbeddingProvider` + `OpenAIEmbeddingProvider`（providers.py 里仅保留 factory 和新 wrapper）。
  - 删除 `KnowledgeChunkRepository`、`KnowledgeChunkORM`、`KnowledgeChunkBase/Create/Read` schema 中仅被旧链路使用的字段（或保留 ORM 类但标记 `# deprecated: no longer used, PGVector manages its own tables`，防止 Alembic 误删表；实际方案 A 约定完全替换，建议直接删，Alembic 新增 0003 迁移 `DROP TABLE IF EXISTS knowledge_chunks` 用于"新环境一键干净"，但在生产环境建议保留数据，迁移里只加注释不 DROP——由用户手动确认后再删）。
  - `test_task5_rag.py` 中依赖旧 `PgVectorRetriever` 的用例改写为新链路等价断言（见 TR 章节）。

## Non-Functional Requirements
- **NFR-1 测试回归**：原 backend/tests 下所有 pytest 用例，除了明确依赖旧 `KnowledgeChunkORM` 表结构的（预计 2-3 个单测）需重写为新 PGVector 等价语义外，其余（含 Agent 端到端、工具调用、HTTP 接口）全部通过。通过率 ≥ 95%。
- **NFR-2 Ingestion 幂等性**：同一 FAQ 文件在同一租户下连续调用 Ingestion API 两次，第二次返回 `chunks_added == 0 && chunks_skipped_duplicate == 第一次 chunks_added`，且 `langchain_pg_embedding` 表行数不增长。
- **NFR-3 多租户零泄露**：在 tenant_a 上传 85 条、tenant_b 上传 80 条 chunk 的情况下，对 tenant_a 执行 `/api/knowledge/search`（任何 query，包括"品牌理念"），返回 hits 中每一条 metadata.tenant_id 都必须 == tenant_a，且 SQL 层实际扫描的 collection 也必须是 tenant_a 的 collection（可通过 explain analyze 或直接 join langchain_pg_collection 表校验）。
- **NFR-4 向后兼容 Schema 不变**：`/api/knowledge/search` 和 `/api/management/.../chunks` 的 OpenAPI schema（请求/响应字段、枚举、分页）与重构前 diff ≤ 新增字段，不允许 break 已有前端/调用方（Ingestion API 是新增路由，不存在兼容问题）。
- **NFR-5 ruff 0 告警**：所有新增/修改文件（backend/app、backend/tests）ruff check 无告警（沿用 pyproject.toml 中既有 ignore 规则）。
- **NFR-6 无新增硬编码**：FAQ 文件名、租户名、切分正则、阈值（threshold 默认值等）全部走配置或函数参数，不在函数内部写死字符串常量（除了正则 pattern 本身这种必须内聚的常量）。

## Constraints
- **Technical**: 技术栈不可变（AGENTS.md）：Python 3.14 + FastAPI + LangChain 0.3+ + PostgreSQL + pgvector + pytest + ruff。向量库封装必须用 `langchain-postgres.PGVector`（用户决策 1），禁止回退到手写 `<=>` SQL 方案（除了 FR-17 GET list 的 metadata 分页 SQL 特例）。EmbeddingProvider 优先 `langchain-core` / `langchain-openai` 原生实现（用户决策 5）。
- **Business**: 多租户隔离必须"collection + metadata filter"双层（project_memory 已记录「RAG 必须带 tenant_id 过滤」硬约束）。unknown 固定文案约束不变，不受 RAG 重构影响。
- **Security**: Ingestion API 属于写操作（但写的是知识向量，非订单/工单类核心业务数据），仍需：(a) Actor 鉴权 + Staff/Admin 角色；(b) 跨租户伪装 target_tenant_id 一律 404；(c) 文件名、文件内容的 content_hash 写入 metadata.created_by == actor.user_id，支持审计溯源"谁上传了这份 FAQ"。
- **Dependencies**: `langchain-postgres` 包与现有 `psycopg[binary,pool] 3.x` 版本必须兼容（langchain-postgres 官方要求 psycopg >= 3.1，我们已有 psycopg 3.x，应无冲突）。若出现依赖冲突，优先 pin `psycopg` 版本，不动其它 LangChain 版本。

## Assumptions
1. `langchain-postgres` 的 `PGVector(connection=..., use_jsonb=True, embedding_dimension=..., collection_name=...)` 构造函数在 0.1.x 版本中 API 稳定；若签名小变动则在 `build_pgvector_store` 工厂内适配，不扩散到调用方。
2. `langchain-postgres.PGVector.adelete(ids=[...])` 支持批量删除；若该 API 不存在，则退回 `psycopg.AsyncConnection` 直接执行 `DELETE FROM langchain_pg_embedding WHERE uuid::text = ANY(%s)`（关联 collection_id 过滤，保证不删其他租户）。
3. `DeterministicFakeEmbedding` 返回的向量维度恰好等于 `size` 参数，且相同文本两次调用向量一致（已读源码确认：是，`_get_seed(text)` + `np.random.default_rng(seed).normal(size=size)` 保证确定性）。
4. 前端当前对 `/api/knowledge/search` 的返回结构依赖仅 `hits[].title / hits[].content / hits[].similarity` 三个字段（可通过前端 App.tsx grep 确认），其余 chunk_id/source/read 是管理接口用，不会因为 chunk_id 从 ORM UUID 变 PGVector UUID 字符串而前端崩。

## Acceptance Criteria

### AC-1: LangChain 原生 PGVector 建库 + 基础 CRUD（Rule）
- **Type**: `rule`
- **Given**: 干净的 Postgres DB（已建 extension 或允许自动建）+ DATABASE_URL 正确 + `embedding_provider=mock` + 两个租户（tenant_a, tenant_b）的 Staff Actor。
- **When**:
  1. `build_pgvector_store(emb, conn, collection_name="tenant_tenant_a").aadd_documents([Document(page_content="测试A", metadata={"tenant_id":"tenant_a","source":"faq","content_hash":"h1","created_by":"u1","title":"T1"})])`
  2. 同样内容对 tenant_b 的 collection 再加一条。
  3. 对 tenant_a 执行 `store.asimilarity_search_with_score("测试", k=3, filter={"tenant_id":"tenant_a"})`。
- **Then**:
  1. DB 内自动出现两张表：`langchain_pg_collection`（含 2 行：tenant_a, tenant_b）和 `langchain_pg_embedding`（含 2 行 embedding）。
  2. 第 3 步检索结果 `len(results) == 1`，唯一一条结果的 `metadata.tenant_id == "tenant_a"`，**不**包含 tenant_b 的记录。
  3. `langchain_pg_embedding.embedding` 列类型为 `vector(1536)`（默认维度；若配置 384 则为 vector(384)）。
- **Pass Condition**: 3 条断言全部通过（通过 pytest 用例 + `await conn.fetchrow("SELECT column_name, data_type, udt_name FROM information_schema.columns WHERE table_name='langchain_pg_embedding' AND column_name='embedding'")` 校验列类型）。
- **Evidence**: 新增单测 `test_pgvector_store_setup_and_isolation` 运行 + DB schema 快照。

### AC-2: FAQ 问答对切分正确性（Rule）
- **Type**: `rule`
- **Given**: `docs/knowledge/梵印阁 FAQ.md` 原文（85 组 QA）、`docs/knowledge/玉语轩 FAQ.md`（80 组）、`docs/knowledge/禅饰坊 FAQ.md`（85 组）。
- **When**: 对三份原文分别跑 `QAPairSplitter.split_text(原文)`（chunk_strategy="qa_pair"，强制不走 fixed fallback）。
- **Then**:
  1. 梵印阁：`len(docs) == 85`；第 1 条 `page_content.startswith("Q：梵印阁是什么品牌？\nA： 梵印阁是一家以质量优先为核心理念的高端手串品牌")`；metadata.qa_index 范围 1..85 连续无重复。
  2. 玉语轩：`len(docs) == 80`；第 5 条 `page_content` 含 `Q5：玉语轩为什么收取手续费？` 及对应 Answer 全文。
  3. 禅饰坊：`len(docs) == 85`；最后一条 `qa_index=85` 含 "禅饰坊有实体店吗" 的问答。
- **Pass Condition**: 3 份文档的条数 + 首尾样本内容断言全部通过。
- **Evidence**: 单测 `test_qa_pair_splitter_three_brands` 断言输出。

### AC-3: Ingestion API 上传、幂等、去重、鉴权（Rule）
- **Type**: `rule`
- **Given**: tenant_a Staff Actor（token A）、tenant_b Staff Actor（token B）+ `梵印阁 FAQ.md` 本地文件。
- **When**:
  1. 用 token A 调 `POST /api/management/tenants/tenant_a/knowledge/ingest` 上传 FAQ.md（`source=faq, chunk_strategy=auto`）。
  2. 用 token A **同样文件再调一次**。
  3. 用 token B 调 `POST /api/management/tenants/tenant_a/knowledge/ingest`（**跨租户伪装**）。
  4. 用 token A 上传一个 `.jpg` 文件（非白名单后缀）。
- **Then**:
  1. 第 1 次响应：`chunks_added == 85, chunks_skipped_duplicate == 0, chunk_strategy_used == "qa_pair"`（假设 FAQ 原文从未上传过）。
  2. 第 2 次响应：`chunks_added == 0, chunks_skipped_duplicate == 85, total_chunks_extracted == 85`（验证 content_hash 去重生效，DB 行数不增）。
  3. 第 3 次跨租户：HTTP **404**（不区分"租户不存在/无权限"，防止存在性探测，和项目既有 cross-tenant 语义一致）。
  4. 第 4 次错格式：HTTP **400**，`body.code == "INVALID_FILE_TYPE"`。
- **Pass Condition**: 4 个场景的响应码、JSON 字段断言全通过，且第 2 次调用前后 `SELECT count(*) FROM langchain_pg_embedding` 不变。
- **Evidence**: HTTP 集成测试 `test_ingest_api_idempotent_and_auth` 运行输出。

### AC-4: 检索接口多租户零泄露 + threshold 过滤（Rule）
- **Type**: `rule`
- **Given**: tenant_a collection 有「Q1梵印阁品牌」「Q27手续费=0%」两条 FAQ chunk；tenant_b collection 有「Q5玉语轩收手续费分档」一条含"手续费 5%"的 chunk。
- **When**: 用 tenant_a Actor 的 token 调 `POST /api/knowledge/search`，`query="手续费怎么收", top_k=10, similarity_threshold=0.0`（阈值 0 保证只要 tenant_id 对就全返回）。
- **Then**:
  1. 返回的 `hits[]` 中**不含**任何含"5%"、"分档"、"玉语轩"字样的 chunk（tenant_b 泄露阻断）。
  2. 返回的 hits 中含一条 tenant_a 内的"Q27手续费=0%"的记录（tenant_a 内正确命中）。
  3. 当把 `similarity_threshold=0.9999`（极高阈值）时，同一请求应返回 `hits == []`，`total == 0`（阈值过滤生效）。
- **Pass Condition**: 3 条断言全部通过。
- **Evidence**: 单测 `test_search_tenant_isolation_and_threshold`。

### AC-5: Agent 图内 RAG 链路不退化（Rule）
- **Type**: `rule`
- **Given**: 对 tenant_a 先 Ingestion 上传 FAQ，确保「7天能退吗 → 命中梵印阁 30天无理由」的问答对在向量库。
- **When**: 通过 Facade 对 tenant_a 的用户发起 `user_message="请问梵印阁支持无理由退换吗？"`（intent=faq_only 分支，走 `rag_retrieve_node → faq_node → llm_wrap`）。
- **Then**:
  1. LangGraph State 的 `rag_hits[]` 非空，且 `rag_hits[0].content` 含"30天无理由"字样（证明向量检索跑通，命中了正确 QA chunk）。
  2. 最终 `final_reply` 含"30天"或"无理由"关键词（llm_wrap 把 RAG 片段正确拼给了 LLM/mock LLM，mock 模式下可断言 rag_hits 内容被拼进 system prompt 的「【RAG 参考片段】」section——通过 `ctx.chat_model.last_system_prompt` hook 抓出验证）。
  3. 整个流程不抛异常，不进 `_mock_rag_hits_from_db` fallback（可通过 mock 该 fallback 函数的 call_count == 0 验证）。
- **Pass Condition**: 3 条通过。
- **Evidence**: Agent 端到端测试 `test_rag_flow_in_agent_faq_branch` 运行输出。

### AC-6: 代码复杂度瘦身（Rubric）
- **Type**: `rubric`
- **Dimension**: RAG 相关自研代码总行数的削减幅度（只算 backend/app，不含测试），行数越少越好（证明我们达成"用 LangChain 原生替代手写复杂逻辑"的目标）。
- **Scale**: 1-5
  - `1`: 删除行数 < 50，新增 > 删除（反增代码）
  - `2`: 删除 50-150，新增 ≥ 删除（基本无瘦身）
  - `3`: 删除 150-250，新增 < 删除（中度瘦身）
  - `4`: 删除 250-350，新增 ≈ 删除的 60%（显著瘦身，仅保留 factory、adapter、splitter 薄封装）
  - `5`: 删除 ≥ 350 行，新增 ≤ 删除的 40%（极度简洁，核心业务代码只剩 API/Agent 调用 glue，向量/嵌入/切分全是 LangChain 原生）
- **Pass Threshold**: >= 4
- **Evidence**: `git diff --stat` 输出快照（对比重构前 vs 重构后，只数 backend/app 下的 `.py` 文件增删行）。

### AC-7: 全量测试 + ruff 通过（Rule）
- **Type**: `rule`
- **Given**: 完整 backend/tests 套件 + 已执行 `pip install -e ".[llm,vector,dev,demo]"`。
- **When**: 运行：
  1. `cd backend && pytest -q --strict-config --strict-markers`
  2. `cd backend && ruff check app tests`
- **Then**:
  1. pytest exit code = 0；如果有因表结构变化必须重写的用例，其数量 ≤ 3，并在 tasks.md 中逐一列出被重写的用例名与新旧语义对照。
  2. ruff exit code = 0，no warnings。
- **Pass Condition**: 2 条全部通过。
- **Evidence**: 命令行 stdout 全文拷贝 + pytest summary 最后一行（如 `123 passed, 3 deselected`）。

## Open Questions
- [x] Q1: 向量库封装用哪个包？→ `langchain-postgres.PGVector`（通过 langchain.vectorstores.pgvector 转发入口，用户决策 1）。
- [x] Q2: 表结构保留旧 knowledge_chunks 吗？→ 方案 A，完全替换，旧表/ORM/Repo 在新链路跑通后清理（用户决策 2）。
- [x] Q3: FAQ 切分方式？→ 按问答对切分，非 FAQ fallback 固定长度（用户决策 3）。
- [x] Q4: Ingestion API 传文件还是传 JSON？→ 上传文件 multipart/form-data（用户决策 4）。
- [x] Q5: Embedding 用自研还是 LangChain 原生？→ 优先原生 `DeterministicFakeEmbedding` / `OpenAIEmbeddings`（用户决策 5）。
- [x] Q6: 种子数据自动导入 docs/knowledge/*.md 吗？→ 不做，接口调通后用户自己导入（用户决策 6）。

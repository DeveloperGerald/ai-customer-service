# RAG 重构：LangChain 原生 PGVector + 文档上传 Ingestion 任务队列

> 按依赖顺序垂直切片：依赖/pyproject → Embedding 工厂 → PGVector 工厂 → 切分器 → Ingestion API → 检索/管理接口内部替换 → Agent 节点替换 → 旧代码清理 → 测试+ruff。
> 每条 AC 对应 [spec.md](./spec.md)；每条 TR 为 rule 或 rubric。

---

## Task 1: 新增 `langchain-postgres` 依赖 + 兼容 DB URL 驱动名
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: None
- **Description**:
  1. **修改 `backend/pyproject.toml`** `[project.optional-dependencies].llm`：追加 `langchain-postgres>=0.3,<1.0`（用较新版，确保 psycopg3 + `asimilarity_search_with_score` API 齐全）。
  2. **在 `backend/app/config.py`（或 DB engine 模块）中新增 `_normalize_langchain_postgres_conn(url: str) -> str` 辅助函数**：如果用户给的是 `postgresql+asyncpg://...` 或 `postgresql+psycopg2://...`，统一替换 driver 为 `postgresql+psycopg://`（langchain-postgres 强制要求 psycopg3 sync/async driver 字符串格式），但 **DB engine 本身的连接 URL 保持不变**（只给 PGVector 的 connection 参数传转换后的值，避免影响现有 SQLAlchemy AsyncSession）。
  3. 运行 `cd backend && pip install -e ".[llm,vector,dev,demo]"` 安装新依赖，验证无冲突。
- **Acceptance Criteria Addressed**: AC-1（FR-1, FR-2）
- **Test Requirements**:
  - `rule` TR-1.1: `import langchain_postgres; print(langchain_postgres.__version__)` 在 venv 内执行 exit code 0，版本 ≥ 0.3。
  - `rule` TR-1.2: 对 4 种 URL 单测 `_normalize_langchain_postgres_conn`：
    | Input | Output |
    |-------|--------|
    | `postgresql+psycopg://u:p@h:5432/db` | 原样返回 |
    | `postgresql+asyncpg://u:p@h/db` | `postgresql+psycopg://u:p@h/db` |
    | `postgresql+psycopg2://u:p@h/db` | `postgresql+psycopg://u:p@h/db` |
    | `postgresql://u:p@h/db`（默认 driver） | 追加为 `postgresql+psycopg://u:p@h/db` |
  - `rule` TR-1.3: 现有 `InfrastructureBundle.db_session_factory`（SQLAlchemy AsyncSession）的初始化代码**不修改**，即 engine 用的是原 URL；转换函数只被 PGVector 构造时调用，不改变现有 SQLAlchemy 行为。
  - `rule` TR-1.4: `ruff check backend/pyproject.toml backend/app/config.py backend/app/infrastructure/db/engine.py` 0 告警（pyproject 本身 ruff 不检查，但相关 Python 文件检查）。
- **Notes**: 如果 langchain-postgres 在 pip 安装时拉取到旧版本（< 0.3），则 pin 为 `langchain-postgres>=0.3.4,<1` 或任何保证 `asimilarity_search_with_score` 和 `adelete` 存在的最低版本。

---

## Task 2: 重写 Embedding 工厂为 LangChain 原生实现（删旧自研 Provider）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 1
- **Description**:
  1. **修改文件**: `backend/app/infrastructure/llm/providers.py`
  2. **删除/标记 deprecated**：
     - `class BaseEmbeddingProvider(ABC)`（删，因为 LangChain `Embeddings` 抽象已完全覆盖；若还有其他模块直接 import，先做全局 grep 确认调用点，保留一个 type alias `BaseEmbeddingProvider = Embeddings` 以兼容遗留 import）。
     - `class MockEmbeddingProvider`（删，替换为 `DeterministicFakeEmbedding`）。
     - `class OpenAIEmbeddingProvider`（删，替换为 `langchain_openai.OpenAIEmbeddings`）。
  3. **保留并改写 `build_embedding_provider(settings)` 工厂**：
     ```python
     # 示意代码（非精确实现，仅描述逻辑）
     def build_embedding_provider(settings: Settings) -> Embeddings:
         llm = settings.llm
         size = llm.embedding_dim
         if llm.embedding_provider == EmbeddingProvider.MOCK:
             return DeterministicFakeEmbedding(size=size)
         if llm.embedding_provider == EmbeddingProvider.OPENAI:
             return OpenAIEmbeddings(
                 model=llm.embedding_model,
                 dimensions=size,
                 api_key=llm.api_key,
                 base_url=llm.base_url,
                 timeout=llm.embedding_request_timeout,
                 max_retries=3,  # 或从 settings 扩展
             )
         raise ConfigError(...)
     ```
  4. **遗留兼容 alias**：如果有其它模块直接调 `emb.embed_texts([...])`，则在 factory 返回的对象上动态挂一个属性 `emb.embed_texts = emb.embed_documents`（或通过一个 5 行子类 wrapper：`class _CompatEmb(DeterministicFakeEmbedding): def embed_texts(self, texts): return self.embed_documents(texts)`，确保 `embed_query` 也保持同名）。
  5. 在 `backend/app/__init__.py` 或 `providers.py` 顶部加延迟导入：`from langchain_core.embeddings import Embeddings, DeterministicFakeEmbedding`（已装 langchain-core 0.3.86，有此 API）。
- **Acceptance Criteria Addressed**: AC-1（FR-4, FR-5, FR-6）、AC-5（Agent RAG 链路的嵌入层不退化）
- **Test Requirements**:
  - `rule` TR-2.1 (mock 确定性): `emb = build_embedding_provider(settings_mock(embedding_provider=MOCK, dim=8))`；两次 `emb.embed_query("相同文本")` 的 list 逐元素相等（断言 `np.allclose` 或直接比 `==`）。
  - `rule` TR-2.2 (openai 配置传递正确): 用 pytest-httpx mock `https://api.openai.com/v1/embeddings`，设置 settings `provider=OPENAI, model="text-embedding-3-small", dim=256, api_key="sk-test"`，调 `await emb.aembed_documents(["abc"])`，断言 httpx 捕获到的请求 JSON 里 `"model" == "text-embedding-3-small"` 且 `"dimensions" == 256`（证明 dimensions 参数被原生 OpenAIEmbeddings 正确转发）。
  - `rule` TR-2.3 (兼容 alias 存在): `hasattr(emb, "embed_texts") == True`；调 `emb.embed_texts(["a","b"])` 返回长度 2 的 list，值等于 `emb.embed_documents(["a","b"])`（直接对同一对象断言结果相等）。
  - `rule` TR-2.4 (类型继承): `isinstance(emb, Embeddings) == True`。
  - `rule` TR-2.5: `ruff check backend/app/infrastructure/llm/providers.py` 0 告警。
- **Notes**: OpenAI 用例可能因网络环境不通实际 endpoint，通过 httpx mock 拦截即可（项目已有 pytest-httpx 依赖）。

---

## Task 3: PGVector 单例工厂 + extension 创建 + 租户隔离 metadata 注入辅助
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 2
- **Description**:
  1. **新建文件** `backend/app/infrastructure/llm/vectorstore.py`（或放在 `backend/app/infrastructure/db/vectorstore.py`，选前者即可，因为和 LLM stack 更近）。
  2. 导出：
     - `build_pgvector_store(embeddings: Embeddings, connection: str, *, collection_name: str, embedding_dimension: int, use_jsonb: bool = True) -> PGVector`：
       - 内部在第一次调用时，通过 `psycopg.AsyncConnection.connect(...)`（或 sync 连接都行，因为 CREATE EXTENSION 是 DDL 只跑一次）执行 `CREATE EXTENSION IF NOT EXISTS vector;`，用 try/except 吞错（离线环境没装 extension 时不崩；但如果连 DB 都不通则直接抛，不静默）。
       - `PGVector(embeddings=embeddings, collection_name=collection_name, connection=connection, use_jsonb=use_jsonb, embedding_dimension=embedding_dimension)`。注意 langchain-postgres 0.3+ 的 API 签名：如果某些参数名在该版本不存在（如老版本只接受 `connection=None, engine=None, async_mode=True/False`），在 factory 内做 `inspect.signature(PGVector.__init__)` 动态裁剪 kwargs，不要硬写死参数列表，避免小版本升级崩。
       - 返回 store 实例。
     - `_enrich_metadata(metadata: dict, *, tenant_id: str, source: str, created_by: str, title: str | None = None, content_hash: str | None = None) -> dict`：对要写入的 Document metadata 统一注入必填租户字段（tenant_id/source/created_by/content_hash/title/chunk_id），其中 chunk_id 若没传则 `str(uuid4())`，created_at 用 ISO 字符串写入，保证后续 FR-17 GET list 可排序。
     - `get_collection_name(tenant_id: str) -> str`：统一返回 `f"tenant_{tenant_id}"`（避免多处拼接错；tenant_id 若为 UUID 字符串，结果也是 ASCII，不会触发 SQL 标识符问题）。
     - `_to_cosine_similarity(score: float) -> float`：因为 `asimilarity_search_with_score` 返回的 `score` 默认是 cosine **distance**（0=完全相同，2=相反），需换算到 cosine **similarity**：`sim = 1.0 - float(score)`，再裁剪到 [-1, 1]；如果 langchain-postgres 的 DistanceStrategy 默认不是 EUCLIDEAN/COSINE（可通过 `PGVector.distance_strategy` 检查），在 factory 里显式传 `distance_strategy=DistanceStrategy.COSINE` 保证 distance ∈ [0, 2]，避免度量不同导致换算错误。
  3. **在 FastAPI lifespan（或 InfrastructureBundle 构造时）预加载一次**：确保 `langchain_pg_collection` / `langchain_pg_embedding` 两张表存在，如果 langchain-postgres 不在 init 时建表则显式调一次 `store.create_collection_if_not_exists()`（如果该 API 存在；如果不存在，第一次 `aadd_documents` 时会自动建表）。
- **Acceptance Criteria Addressed**: AC-1（FR-7, FR-8, FR-9）、AC-4（检索租户隔离的基础）
- **Test Requirements**:
  - `rule` TR-3.1 (factory 构造): `build_pgvector_store(emb, conn, collection_name="t_foo", embedding_dimension=8)` 返回的对象是 `langchain_postgres.PGVector` 实例。
  - `rule` TR-3.2 (extension 不崩): 在一个临时 SQLite 内存 DB（无 vector extension）上调用 build_pgvector_store，不抛异常（extension 创建失败被 try/except 吞）。
  - `rule` TR-3.3 (tenant 隔离 metadata 注入): `_enrich_metadata({}, tenant_id="a", source="faq", created_by="u1", title="T", content_hash="h")` 返回 dict 含 keys `{tenant_id, source, created_by, content_hash, title, chunk_id, created_at}` 且全部非空；chunk_id 符合 UUID4 正则。
  - `rule` TR-3.4 (score 换算): 对以下边界值断言 `_to_cosine_similarity(x)`：
    - x = 0.0 → 1.0（完全相同）
    - x = 1.0 → 0.0（正交）
    - x = 2.0 → -1.0（完全相反）
    - x = -0.001 → 裁剪到 -1.0（容错，防止浮点误差）
    - x = 2.001 → 裁剪到 1.0（容错）
  - `rule` TR-3.5 (collection_name 稳定): `get_collection_name("tenant_a-123") == "tenant_tenant_a-123"`（即不做额外 slugify，直接拼；因为 langchain-postgres 的 collection_name 是存在 `langchain_pg_collection.name` 列里而非 SQL 标识符，任意字符串都 OK）。
  - `rule` TR-3.6: `ruff check backend/app/infrastructure/llm/vectorstore.py` 0 告警。
- **Notes**: langchain-postgres 在不同版本间参数名有差异，factory 内部用 `**kwargs` 裁剪策略保证兼容，不硬写具体参数；如果遇到 API 变更，优先改 factory，不上层扩散。

---

## Task 4: 实现 QAPairSplitter（问答对切分）+ fallback 固定长度切分
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: None
- **Description**:
  1. **新建文件** `backend/app/infrastructure/llm/chunking.py`。
  2. 实现 `QAPairSplitter`：
     - **输入**: `text: str, *, default_source: str = "faq", file_name: str | None = None`
     - **输出**: `list[Document]`（`from langchain_core.documents import Document`）
     - **核心正则**（用 re.DOTALL + re.UNICODE）：
       ```
       PATTERN = re.compile(
           r'Q\s*(\d+)\s*[：:]\s*(.*?)\n\s*A\s*[：:]\s*(.*?)(?=\n\s*Q\s*\d+\s*[：:]|\Z)',
           re.DOTALL,
       )
       ```
       - 对每个 match，group(1)=qa_num（int），group(2)=q_text（去首尾空格），group(3)=a_text（去首尾空格，保留内部换行）。
       - 规范化 Q 和 A 前面的空白：统一写成 `page_content = f"Q：{q_text.strip()}\nA：{a_text.strip()}"`（注意用全角冒号和用户文档里的保持一致，避免检索时用户问"Q1 梵印阁"但文档切分后存半角冒号匹配弱）。
       - metadata = `_build_qa_metadata(qa_num, q_text, file_name, default_source)`：qa_index=int, title=`f"Q{qa_num}：{truncate(q_text, 80)}"`, chunk_type="qa_pair", source=default_source, file_name=file_name or ""。
     - **Fallback 判定**: 如果 `len(qa_docs) < 3`（识别问答对过少，例如非 FAQ 文档），则放弃 QA 模式，改用 langchain 原生切分器：
       ```python
       from langchain_text_splitters import (
           MarkdownHeaderTextSplitter,
           RecursiveCharacterTextSplitter,
       )
       headers = [("#", "Header 1"), ("##", "Header 2"), ("###", "Header 3")]
       md_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers)
       after_md = md_splitter.split_text(text)
       rec_splitter = RecursiveCharacterTextSplitter(
           chunk_size=chunk_size, chunk_overlap=chunk_overlap,
       )
       fixed_docs = rec_splitter.split_documents(after_md) or rec_splitter.create_documents([text])
       ```
       然后给每条 fixed_docs[i] 的 metadata 追加 `chunk_type="fixed_length", chunk_index=i, source=default_source, file_name=...`。
     - 最后返回的 Document list **必须**：每条 metadata 都包含 `chunk_type`（qa_pair / fixed_length）和 `source`，供 Task 5 的 Ingestion 去重使用。
  3. 在 chunking.py 顶部导出 `split_document(text: str, *, strategy: Literal["auto","qa_pair","fixed_length"]="auto", chunk_size=800, chunk_overlap=80, default_source="faq", file_name=None) -> list[Document]` 作为上层统一入口。
- **Acceptance Criteria Addressed**: AC-2（FR-10, FR-11）
- **Test Requirements**:
  - `rule` TR-4.1 (梵印阁 85 QA 全识别)：Read 实际文件 `docs/knowledge/梵印阁 FAQ.md` 传 text，strategy=qa_pair → len(docs)==85；首条 `page_content.startswith("Q：梵印阁是什么品牌？")`；末条 `qa_index == 85`。
  - `rule` TR-4.2 (玉语轩 80 + 禅饰坊 85)：同上条数断言。
  - `rule` TR-4.3 (非 FAQ 文档 fallback fixed)：输入一段不含 "Q1：" 格式的普通 Markdown（500 字），strategy=auto → `len(docs) >= 1` 且所有 doc.metadata.chunk_type == "fixed_length"。
  - `rule` TR-4.4 (content_hash 稳定，与切分器无关)：这个 TR 放到 Task 5 里测（因为 hash 要结合 tenant_id/source/page_content 三者）；切分器本身只保证 page_content 文本规范化后相同输入→相同输出。
  - `rule` TR-4.5: `ruff check backend/app/infrastructure/llm/chunking.py` 0 告警。
- **Notes**: 实际测试用例读 docs/knowledge 下的三个 FAQ 文件时，注意工作目录问题，pytest 从 backend/ 目录跑时，文件路径要用 `Path(__file__).parents[3] / "docs" / "knowledge" / "xxx.md"` 绝对路径化，避免找不到文件。

---

## Task 5: Ingestion API（上传文件 → 切分 → 去重 → 入库）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 3, Task 4
- **Description**:
  1. **修改文件** `backend/app/api/management_knowledge.py`（追加路由；因为管理知识库相关，放同一文件合理）。
  2. **新增请求/响应 schema**（放在 `backend/app/application/schemas/knowledge.py` 追加）：
     - `KnowledgeIngestResponse(BaseModel)`：字段对齐 FR-14（tenant_id / source / file_name / total_chunks_extracted / chunks_added / chunks_skipped_duplicate / chunk_strategy_used / sample_hashes）。
  3. **Ingestion 步骤实现**（endpoint handler 内部）：
     1. **鉴权**：`actor = Depends(require_actor)`；然后用现有 `_ensure_can_modify_tenant(actor, target_tenant_id)`（如果 management_knowledge.py 里已有同租户校验逻辑就复用；若它是通过 `repo.list_for_tenant` 里隐式校验的，则抽一个显式 helper：`assert actor.role in ["staff","admin"] and actor.tenant_id == target_tenant_id`，不满足直接抛 ResourceNotFound→404，不区分"不存在/无权限"）。
     2. **文件校验**：
        - suffix 白名单 ∈ {`.md`, `.markdown`, `.txt`}。
        - `await file.read()` 后 `len(content) > 10*1024*1024` → 413。
        - mime 允许 `text/*`（即便浏览器传的是 application/octet-stream 但后缀合法也放行，避免 mime 误判）。
        - `content_str = content.decode("utf-8", errors="replace")`（中文 FAQ 基本 UTF-8，乱码字符用 U+FFFD 不影响切分）。
     3. **切分**：`docs = split_document(content_str, strategy=chunk_strategy, chunk_size=chunk_size, chunk_overlap=chunk_overlap, default_source=source, file_name=file.filename)`；若 `len(docs) == 0` → 400 NO_CHUNK_EXTRACTED。
     4. **算 content_hash + 填充必填 metadata**：
        ```python
        tenant_id = target_tenant_id
        for d in docs:
            text = d.page_content
            d.metadata = _enrich_metadata(
                d.metadata,
                tenant_id=tenant_id,
                source=source,
                created_by=str(actor.user_id),
                title=d.metadata.get("title") or f"{file.filename}-chunk",
                content_hash=_calc_hash(tenant_id, source, text),
            )
        ```
        其中 `_calc_hash(tid, src, txt) = hashlib.sha256(tid.encode()+src.encode()+txt.encode()).hexdigest()`。
     5. **去重查询**：
        - 收集所有 `hashes = {d.metadata["content_hash"] for d in docs}`（可能有同一文件内重复 QA 的情况，先转 set 缩小查询）。
        - 对本租户 collection 做一次向量无关的 metadata 查询：因为 PGVector 没有直接的 "按 metadata hash 批量查询是否存在" 的原生 API，这里允许**用 psycopg.AsyncConnection 执行一条轻量 raw SQL**（只查 cmetadata，不查 embedding），在 factory 里再补一个辅助 `_get_raw_async_conn(connection_str) -> psycopg.AsyncConnection`（用 `autocommit=True` 即可，不进事务池），SQL：
          ```sql
          SELECT DISTINCT (e.cmetadata->>'content_hash') AS h
          FROM langchain_pg_embedding e
          JOIN langchain_pg_collection c ON e.collection_id = c.id
          WHERE c.name = %(coll)s
            AND e.cmetadata @> %(meta)s::jsonb
            AND (e.cmetadata->>'content_hash') = ANY(%(hashes)s)
          ```
          `meta = {"tenant_id": tenant_id, "source": source}`，`hashes = list(hashes)`；拿到的结果集是已存在 hash 集合 `existing_hashes`。
     6. **分批插入**：`to_add = [d for d in docs if d.metadata["content_hash"] not in existing_hashes]`；若 `len(to_add) > 0`，`ids = [d.metadata["chunk_id"] for d in to_add]`，调 `await store.aadd_documents(to_add, ids=ids)`（langchain-postgres 支持传 ids 或从 metadata 自动取；若该版本 aadd_documents 不接受 ids 参数，则改为 chunk_id 写入 Document.id 属性再传）。
     7. **返回响应**：构造 `KnowledgeIngestResponse`，其中 `sample_hashes = {"added": [h1,h2,h3 最多3条], "skipped": [h1,h2,h3 最多3条]}`。
- **Acceptance Criteria Addressed**: AC-3（FR-12, FR-13, FR-14, FR-15）
- **Test Requirements**:
  - `rule` TR-5.1 (HTTP 集成: 首次 + 幂等): 见 AC-3 Step 1/2：首次 201 chunks_added=85；第二次 chunks_added=0 skipped=85；两次调用前后 `SELECT count(*) FROM langchain_pg_embedding WHERE collection_id=(SELECT id FROM langchain_pg_collection WHERE name='tenant_X')` 数值不变。
  - `rule` TR-5.2 (跨租户 404): tenant_b Actor 调 target_tenant_id=tenant_a → HTTP 404；且 DB 查询确认 tenant_a 的 collection 没新增行。
  - `rule` TR-5.3 (非 md/txt 后缀 400): 上传 a.jpg → 400 INVALID_FILE_TYPE。
  - `rule` TR-5.4 (空文档 400): 上传一个空文件（0 字节或全换行空白）→ 400 NO_CHUNK_EXTRACTED。
  - `rule` TR-5.5 (chunk_strategy_used 字段正确): 传梵印阁 FAQ 时 strategy=auto → 响应 chunk_strategy_used == "qa_pair"；传一份普通 Markdown 文本 → chunk_strategy_used == "fixed_length"。
  - `rule` TR-5.6: `ruff check backend/app/api/management_knowledge.py backend/app/application/schemas/knowledge.py` 0 告警。
- **Notes**: Raw SQL 只用于去重查询，不涉及向量运算或嵌入列操作，不破坏"用 LangChain 原生封装"的大原则；因为去重是 ingestion 特有业务逻辑不是 LangChain PGVector 封装范围，用原生 SQL 更高效且面试讲解点更清晰（"LangChain 管向量，业务去重我自己用 SQL 保证幂等"）。

---

## Task 6: 重写 `/api/knowledge/search` 和 `/api/management/.../knowledge/chunks` 内部实现
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 3
- **Description**:
  1. **重写 `backend/app/api/knowledge.py`（消费者语义搜索）内部**：
     - 删除对旧 `PgVectorRetriever.retrieve()` 的调用；改为：
       ```
       store = build_pgvector_store(
           embeddings=build_embedding_provider(settings),
           connection=_normalize_langchain_postgres_conn(db_url),
           collection_name=get_collection_name(actor.tenant_id),
           embedding_dimension=settings.llm.embedding_dim,
       )
       filter = {"tenant_id": actor.tenant_id}
       if req.source: filter["source"] = req.source
       docs_and_scores = await store.asimilarity_search_with_score(
           req.query, k=req.top_k, filter=filter,
       )
       hits = []
       for doc, score in docs_and_scores:
           sim = _to_cosine_similarity(score)
           if sim < req.similarity_threshold: continue
           hits.append({
               "chunk_id": doc.metadata.get("chunk_id") or str(doc.id or uuid4()),
               "tenant_id": doc.metadata["tenant_id"],
               "title": doc.metadata.get("title") or doc.page_content[:40],
               "content": doc.page_content,
               "source": doc.metadata.get("source", "faq"),
               "similarity": round(sim, 6),
               "read": KnowledgeChunkRead(...)  # 与旧 schema 兼容：如果旧 Read 依赖 ORM 对象字段，就构造一个 KnowledgeChunkORM(**映射字段)** 用 from_orm，或者直接用 model_construct 填好必需字段，不实际查 DB。
           })
       ```
     - 响应外层 `KnowledgeSearchResponse(query=req.query, hits=hits, total=len(hits), top_k=req.top_k, similarity_threshold=req.similarity_threshold, source=req.source, took_ms=...)`：took_ms 与旧逻辑保持一致（如果旧接口没 took_ms 字段就不填，保持 OpenAPI 兼容）。
  2. **重写 `backend/app/api/management_knowledge.py: list_chunks / create_chunk / delete_chunk`**：
     - **GET list_chunks**：直接用 Task 3 里提到的"raw SQL 查 metadata"模式（list 场景不走向量搜索），按 created_at 倒序，limit/offset 原样分页；返回每条构造为 KnowledgeChunkRead（同上 schema 兼容构造）。
     - **POST create_chunk**：把单条 `KnowledgeChunkCreate(title, content, source)` 包成单元素 Document 列表，复用 Task 5 的 4/5/6 步骤（算 hash→查重→add），返回 KnowledgeChunkRead。
     - **DELETE /{chunk_id}**：
       - 第一步：用 raw SQL 查 `SELECT cmetadata FROM langchain_pg_embedding e JOIN langchain_pg_collection c ON ... WHERE e.uuid::text = %s AND c.name=%s`（或者把 uuid 传成 UUID 类型；具体字段名按实际 langchain-postgres 建表列名：langchain-postgres 里 embedding 表主键一般是 `uuid UUID PRIMARY KEY`，collection 表 `id UUID PRIMARY KEY` + `name TEXT UNIQUE`，直接查）。
       - 第二步：如果没查到 OR `cmetadata['tenant_id'] != target_tenant_id` → **404**（统一越权/不存在响应，符合项目惯例）。
       - 第三步：`await store.adelete([str(chunk_id)])`，成功 204；如果 store.adelete 不存在，则直接 `DELETE FROM langchain_pg_embedding WHERE uuid::text = %s` raw SQL 删除，保证功能可用。
- **Acceptance Criteria Addressed**: AC-4（FR-16, FR-17）、AC-7（全量测试）
- **Test Requirements**:
  - `rule` TR-6.1 (search 租户隔离): 同 AC-4：tenant_a 搜"手续费"不返回 tenant_b 的 5% 记录；threshold 极高时 hits=[]。
  - `rule` TR-6.2 (list_chunks 分页): 插入 3 条记录，`GET ...?limit=2&offset=1` 返回 2 条，与 created_at 顺序一致。
  - `rule` TR-6.3 (delete 越权 404): tenant_b Actor 删 tenant_a 下的 chunk_id → 404；该 chunk 仍存在于 DB。
  - `rule` TR-6.4 (create 兼容旧 schema): 旧 KnowledgeChunkCreate(title="T", content="C", source="faq") POST → 返回 KnowledgeChunkRead.chunk_id 是 UUID 字符串，tenant_id 正确。
  - `rule` TR-6.5: `ruff check backend/app/api/knowledge.py backend/app/api/management_knowledge.py backend/app/application/schemas/knowledge.py backend/app/application/schemas/policy.py` 0 告警（最后一个文件因为旧 KnowledgeChunk 定义在 schemas/policy.py，需要兼容 wrapper）。
- **Notes**: 旧 Schema `KnowledgeChunkRead` 需要 ORM 对象 `from_orm` 的话，用 `model_construct` 手动填字段即可，不必强依赖 ORM；或在 schemas 层把 KnowledgeChunkRead 改成 `from_attributes = True`（Pydantic v2 `model_config`）+ 兼容 dict，灵活适配。

---

## Task 7: 重写 Agent 图内 `rag_retrieve_node` + `BaseRetriever` 实现
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 3, Task 6
- **Description**:
  1. **修改文件**: `backend/app/application/agent/nodes.py` + `backend/app/infrastructure/llm/providers.py`（删除旧 BaseRetriever 抽象和 PgVectorRetriever，新增 LangChainRetrieverAdapter）。
  2. **新增 `LangChainRetrieverAdapter`**（放在 vectorstore.py 或 providers.py，放前者更好）：
     ```python
     class LangChainRetrieverAdapter(BaseRetriever if BaseRetriever 还存在 else object):
         # 对外方法签名 100% 兼容旧 BaseRetriever（FR-18 要求）
         async def retrieve(self, *, tenant_id, query, top_k, similarity_threshold):
             store = build_pgvector_store(...)
             ds = await store.asimilarity_search_with_score(query, k=top_k, filter={"tenant_id": tenant_id})
             out = []
             for doc, score in ds:
                 sim = _to_cosine_similarity(score)
                 if sim < similarity_threshold: continue
                 out.append(_pack_result(doc, sim))  # _pack_result 映射回 {chunk_id,tenant_id,title,content,source,similarity,read} dict
             return out
     ```
  3. **修改 `AgentNodeContext.retriever` 注入位置**：旧的 `PgVectorRetriever(bundle=...)` 在 facade 构造 ctx 时改成 `LangChainRetrieverAdapter(bundle=...)`。
  4. **保留 `_mock_rag_hits_from_db` fallback**：当 `build_pgvector_store` 初始化异常（比如 DB 连不上）时，捕获异常 + 记 structlog warning + 返回空 rag_hits（不直接用 mock 关键词，避免演示环境无 pgvector 时返回假数据；用户若要 mock 关键词路径，可手动开关；默认关，符合 project_memory 硬约束 RAG 必走真向量库的精神——但如果没 vector extension 会导致 Agent 图在 faq_only 分支无命中，这个时候允许走 mock 关键词兜底，需要在代码里明确一个 fallback 开关，比如 `try: build_pgvector_store ... except: use_mock_fallback = True`）。
  5. **修改 `rag_retrieve_node`**：如果 ctx.retriever 存在，就调 adapter.retrieve(...)；结果写入 `state.rag_hits`，格式与旧版完全同构，`_build_agent_system_prompt` 不改。
- **Acceptance Criteria Addressed**: AC-5（FR-18, FR-19）
- **Test Requirements**:
  - `rule` TR-7.1 (Agent faq_only RAG 命中): 同 AC-5：先 ingestion 上传 FAQ → 调 facade.ainvoke("梵印阁支持无理由退换吗？") → state.rag_hits 非空，首条 content 含 "30 天"；_mock_rag_hits_from_db 没被调用（mock 函数 call_count == 0）。
  - `rule` TR-7.2 (rag_hits 格式兼容): rag_hits[0] dict 必含 keys `{chunk_id, tenant_id, title, content, source, similarity, read}`，类型全部正确（similarity 是 float in [-1,1]，chunk_id 是 UUID 格式 str）。
  - `rule` TR-7.3 (threshold 生效): 构造一个 query 向量和已有 chunk 完全不相关（或 mock 一个 embedding provider 返回和已知向量正交的）→ `similarity_threshold=0.99` → rag_hits == []。
  - `rule` TR-7.4: `ruff check backend/app/application/agent/nodes.py backend/app/infrastructure/llm/vectorstore.py` 0 告警。
- **Notes**: 如果旧的 `BaseRetriever` abstract 在删除后还有多处 `isinstance(x, BaseRetriever)` 断言，保留一个 stub type alias：`BaseRetriever = Any`（或用 typing.Protocol），不影响运行。

---

## Task 8: 清理旧代码 + Alembic 迁移 0003
- **Status**: `pending`
- **Priority**: `medium`（必须在所有测试通过后执行）
- **Depends On**: Task 1–7 全部 TR 通过
- **Description**:
  1. **删除代码**（先 grep 全项目确认无引用再删）：
     - `backend/app/infrastructure/llm/providers.py`：删除旧 `PgVectorRetriever` 类 + `_cosine` 函数（这两个在 Task 7 后应该已不再被调用）。
     - `backend/app/domain/repositories/policy.py: KnowledgeChunkRepository`：类本身删除，以及相关 `create_for_actor / list_for_tenant / delete_for_actor` 方法。
     - `backend/app/domain/models/policy.py: KnowledgeChunkORM`：删除 ORM 类定义（或保留 class 但清空字段只留 comment "deprecated, replaced by PGVector native tables"，避免 Alembic autogenerate 误删表——下面迁移文件明确说明这一点）。
  2. **Alembic 迁移 `backend/alembic/versions/0003_rag_langchain_native.py`**：
     - `upgrade()`：
       - 先 `op.execute("CREATE EXTENSION IF NOT EXISTS vector")`（从 0002 里移过来，保证独立执行也 OK）。
       - **不要**自己 `CREATE TABLE langchain_pg_*`——注释说明这两张表由 langchain-postgres 首次 `aadd_documents` 时动态创建。
       - 不要 `DROP TABLE knowledge_chunks`（防止用户已有真实数据被误删）；加注释"如需清理旧表，请手动执行 `DROP TABLE knowledge_chunks;`，自动迁移不做破坏性操作"。
       - 如果 0002 里已经跑了 `CREATE EXTENSION`，这里重复执行 `IF NOT EXISTS` 是安全的。
     - `downgrade()`：逆步骤不处理（降级无意义）。
  3. **清理遗留 import + type hint**：全局 grep `KnowledgeChunkORM` / `KnowledgeChunkRepository` / `PgVectorRetriever` / `_cosine` 替换为新实现或删除。
- **Acceptance Criteria Addressed**: AC-6（代码瘦身 rubric）、AC-7
- **Test Requirements**:
  - `rule` TR-8.1 (no unused imports): `ruff check app tests` 不出现 F401/F841 等未使用错误（ruff ignore F 是关掉的？实际项目 ruff.toml select 含 F，未使用 import 会报）。
  - `rule` TR-8.2 (Alembic upgrade 幂等): `cd backend && alembic upgrade head` 连续两次 exit code 0；`alembic current` 显示 revision 为 0003。
  - `rule` TR-8.3 (旧表不自动删): 升级后旧 `knowledge_chunks` 表如果之前存在就仍在（`SELECT to_regclass('knowledge_chunks')` 非空），不做破坏性删除。
- **Notes**: 删代码这一步容易引入"漏网之鱼"导致 import error，所以放在 Task 1-7 全部跑通后做，且需用 pytest 全量回归验证。

---

## Task 9: 验证套件 + 回归测试重写
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 1–7
- **Description**:
  1. **重写 `backend/tests/unit/test_task5_rag.py`** 中依赖旧 `PgVectorRetriever` / `KnowledgeChunkORM` 的 6 个用例（TR5-1..TR5-6）为新等价：
     - TR5-1（Mock embedding 确定性）→ 用 Task 2 等价单测（DeterministicFakeEmbedding 两次 embed_query 同文同值）。
     - TR5-2（租户隔离）→ 用 AC-1/TR-6.1 覆盖。
     - TR5-3/4/5（top_k / threshold / 空库）→ AC-4 检索链路 TR 覆盖。
     - TR5-6（HTTP search 接口）→ TR-6.1 覆盖。
  2. **新增 3 个专项测试文件**（或合并进 test_task5_rag.py + test_task9_ingest.py + test_task10_store.py 等命名合理即可）：
     - `test_pgvector_store.py`：AC-1/TR-3.x。
     - `test_chunking_qa.py`：AC-2/TR-4.x。
     - `test_ingest_api.py`：AC-3/TR-5.x。
  3. **修改 conftest fixtures**：
     - `backend/tests/conftest.py` 里如果有注入旧 `PgVectorRetriever` 的 fixture，替换为 `LangChainRetrieverAdapter`。
     - 新增 `pgvector_store_factory(emb, db_url, tmp_collection_suffix)` fixture：每个测试用独立 `collection_name = f"test_{uuid4().hex[:8]}"`，避免不同测试污染互相（因为 collection 是多租户共享的，测试间必须用不同 collection_name 隔离 + 测试结束后可选 DROP collection 数据）。
- **Acceptance Criteria Addressed**: AC-7（全量 pytest 通过）
- **Test Requirements**:
  - `rule` TR-9.1: `pytest tests -q` 全部通过（exit 0）。
  - `rule` TR-9.2: 旧 test_task5_rag.py 不再 import 任何 `KnowledgeChunkORM` 或 `PgVectorRetriever`（grep 断言）。
  - `rule` TR-9.3: 测试用例之间 collection 隔离，跑 10 次 `pytest -q`（连续多轮）结果一致（验证无测试间 state 污染）。
- **Notes**: 如果用 `pytest-asyncio` + `httpx` 跑 HTTP 集成测试时 AsyncSession 的 lifecycle 有坑，复用项目现有 `seed-all` 脚本后 db_session fixture 的模式即可，不造新轮子。

---

## Task 10: 最终验证 + 代码瘦身量化（AC-6 / AC-7 收尾）
- **Status**: `pending`
- **Priority**: `medium`
- **Depends On**: Task 8, Task 9
- **Description**:
  1. 执行 `pytest -q backend/tests` + `ruff check backend/app backend/tests`，截屏/保存 stdout。
  2. 执行 `git diff --stat` 只看 backend/app 下的 Python 文件，统计删除行数 / 新增行数，计算 AC-6 rubric 得分（目标 ≥ 4 → 删 250+ 行且新增 ≤ 删除的 60%）。如果未达标，再一轮微调：比如把 provider 里遗留的兼容 wrapper 再简化（如果确实没有 embed_texts 调用就删 alias）、把旧的 BaseRetriever type alias 也删掉、清理多余注释等。
  3. 本地用 HTTPie 或 curl 手动验证 Ingestion API 端到端（可选，为了面试演示提前走一遍）：
     ```bash
     # 1. 启动后端
     # 2. 先登录拿 token
     # 3. 上传
     http -f POST :8000/api/management/tenants/<tenant_id>/knowledge/ingest \
         "Authorization: Bearer <staff_token>" \
         file@docs/knowledge/梵印阁\ FAQ.md \
         source=faq chunk_strategy=auto
     ```
     期望返回 chunks_added=85；第二次同样命令 chunks_added=0。
- **Acceptance Criteria Addressed**: AC-6（rubric）、AC-7（rule）
- **Test Requirements**:
  - `rubric` TR-10.1（AC-6）：代码瘦身得分 ≥ 4（评分说明见 AC-6 anchors）。证据：`git diff --stat` 输出 + 得分 rationale。
  - `rule` TR-10.2 (pytest + ruff): 两项 exit code 0。证据：命令行 stdout 粘贴。
  - `rule` TR-10.3 (手动端到端): 如果环境允许启动后端服务器，Ingestion API 首次 + 二次调用的响应 JSON 符合预期；若无法启动（无 Postgres 等），则跳过此条，记录为"环境限制未执行，集成等价覆盖在 test_ingest_api.py 中已验证"。

---

## Task DAG 汇总

```
Task1 ──┐
        ├─→ Task2 ──→ Task3 ──┬─→ Task5 ──→ Task6 ──→ Task7 ──┐
Task4 ──┘                       └─→ (helpers used in 5,6,7)    │
                                                                ├─→ Task9 ──→ Task8 ──→ Task10
                                                                │
(Task2 also feeds Task7 via Provider)                          └─ Task7 的输入也来自 Task3
```

**并行友好组**：Task 1 + Task 4 可并行（互不依赖）；Task 3 + Task 5 与 Task 6/7 强顺序；Task 8 必须等 Task 9 通过后才清理。

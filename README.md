# 手串售后智能客服 Agent

面试演示型 MVP，多租户手串品牌售后智能客服系统。基于 LangGraph 状态图编排 Agent 流程，结构化政策判定优先、RAG 知识检索兜底，任务型诉求交由 LangChain 1.x 原生 `create_agent` 子图多轮调用工具，写操作支持人在回路（HITL）确认，全程 SSE 流式输出。

## 项目介绍

### 定位

面向手串品牌的售后客服场景，支持多租户隔离（每家品牌独立政策/知识库），覆盖政策知识问答、订单/商品查询、退换修与取消订单申请、人工转接等核心售后链路。

### 核心特性

- **多租户三层隔离**：HTTP 层校验 `X-Tenant-Id` + JWT 一致性，数据库层复合外键 + 前缀 CHECK 约束，RAG 检索强制 tenant_id 过滤
- **政策优先 + RAG 兜底**：知识类问题先命中结构化退货政策（确定性代码判定，100% 准确），未命中再走 PGVectorStore 语义检索 FAQ/手册
- **LangGraph 4 路分流状态图**：`intent_classify` 将诉求分为 simple_qa / handoff / knowledge_qa / task 四类；所有分支最终汇聚到统一的 `compliance_check`（规则合规 + LLM 包装）后回复
- **原生 Agent 任务子图**：task 分支使用 LangChain 1.x 原生 `create_agent`（ReAct 模式）多轮调用 9 个 LangChain `@tool`，支持缺槽位追问
- **写操作人在回路（HITL）**：退款/换货/维修/取消订单 4 个写工具内置 `interrupt()`，图暂停后状态经 Redis checkpointer 持久化（10 分钟 TTL），前端推送确认卡片，用户点击后 `/actions/confirm` 恢复执行
- **工具治理三件套**：可信身份覆盖（防 LLM 伪造租户/用户）、参数防幻觉校验（如订单号必须出现在用户消息中）、参数哈希幂等 + 审计日志；写工单号按 tenant+thread+tool+args 确定性生成
- **知识库管理**：同租户 staff/admin 可上传 `.md/.markdown/.txt`（UTF-8、≤2MB），FAQ 按问答对切片（一问一块），同名文档重传覆盖
- **离线质量评估**：管理端页面手动触发 LangSmith Experiment，35 条黄金用例 + 可配置 Judge 模型，接口仅 ADMIN 可用
- **SSE 流式输出**：`start → node_start/node_end → reply_chunk →（confirmation_required）→ reply → debug → done` 事件序列
- **9 宫格演示令牌**：3 租户 × 3 角色（consumer/staff/admin），前端一键切换身份，导航按角色收敛

### 演示链路

1. **政策知识问答**：三个租户对同一问题返回各自结构化政策；政策未覆盖时走 RAG 并附引用片段
2. **订单/商品查询**：缺少订单号时多轮追问，仅能查询当前消费者有权访问的订单
3. **退换修 / 取消订单（HITL）**：查订单 → 政策校验 → 推送确认卡片 → 用户确认 → 创建工单（RF/EX/RP/CX 前缀）
4. **人工交接**：明确要求转人工/投诉等 → 立即转人工，会话标记 `escalated` 并生成工单号
5. **多轮流式**：跨轮保留上下文与槽位，流式展示回答、节点流转、工具调用与确认状态

---

## 技术栈

以下版本为当前 Docker 运行环境实际安装版本（2026-10 核对）。

### 后端

| 组件 | 技术与版本 |
| --- | --- |
| 语言运行时 | Python 3.11.17（`pyproject.toml` 要求 >=3.10，镜像 `python:3.11-slim`） |
| Web 框架 | FastAPI 0.142.2 / Uvicorn 0.54.0 |
| 数据校验 | Pydantic 2.13.5 / pydantic-settings 2.15.0 |
| ORM / 驱动 | SQLAlchemy 2.1.1（async）/ psycopg 3.3.6 |
| 数据库迁移 | Alembic 1.20.0 |
| Redis 客户端 | redis-py 8.1.0 |
| 日志 | structlog 26.1.0（JSON 日志，request_id 串联） |
| HTTP 客户端 | httpx 0.28.1 |
| 认证 | PyJWT 2.15.1（HS256 演示令牌） |
| 其他 | python-multipart 0.0.32（文档上传）/ orjson 3.12.0 / tenacity 9.1.4 |

### Agent / 大模型 / RAG

| 组件 | 技术与版本 |
| --- | --- |
| Agent 编排 | LangGraph 1.2.12 |
| Checkpointer | langgraph-checkpoint 4.2.0 / langgraph-checkpoint-redis 0.5.2（AsyncRedisSaver） |
| LLM 框架 | LangChain 1.4.3（langchain-core 1.6.6） |
| 模型接入 | langchain-openai 1.6.7 / openai SDK 3.23.0（仅 OpenAI 兼容协议） |
| 向量库 | langchain-postgres 0.0.18（`PGVectorStore`，pgvector `<=>` 原生检索）/ pgvector 0.3.6 |
| 文档切分 | langchain-text-splitters 1.1.2 |
| 可观测 | LangSmith SDK 0.14.3（环境变量开关，默认关闭） |
| 演示模型 | 智谱 GLM-4-Flash（chat）+ embedding-3（1024 维）；亦兼容 OpenAI / DeepSeek 等兼容协议端点 |

> 大模型、LangChain、PostgreSQL、Redis 均为项目强依赖：未配置 `LLM__OPENAI_API_KEY` 等启动必备项时应用直接抛异常终止启动，不做静默降级。

### 前端

| 组件 | 技术与版本 |
| --- | --- |
| 框架 | React 18.3.1 / react-dom 18.3.1 |
| 语言 | TypeScript 5.9.3 |
| 构建 | Vite 5.4.21 / @vitejs/plugin-react 4.7.0 |
| 样式 | Tailwind CSS 3.4.19 / PostCSS 8.5 / autoprefixer 10.6 |
| 运行时 | Node.js 20（镜像 `node:20-alpine`） |
| SSE | 原生 fetch + ReadableStream 手写解析（需携带鉴权头，故不用 EventSource） |
| 生产托管（可选） | nginx 1.27-alpine（Dockerfile 多阶段构建 `frontend-prod` target） |

### 基础设施与开发工具

| 组件 | 技术与版本 |
| --- | --- |
| 数据库 | PostgreSQL 16（镜像 `pgvector/pgvector:pg16`）+ pgvector 扩展 0.8.6 |
| 缓存 / 状态 | Redis Stack 7.4.7（镜像 `redis/redis-stack-server:latest`，含 RedisJSON + RediSearch，经 `/entrypoint.sh` 启动加载模块） |
| 编排 | Docker Compose v2（postgres / redis / backend / frontend 四服务） |
| 测试 | pytest 8.x / pytest-asyncio / pytest-httpx / pytest-cov |
| 质量工具 | ruff（lint + format）/ factory-boy / faker |

---

## 项目结构

```
ai-customer-service/
├── backend/
│   ├── app/
│   │   ├── api/                     # HTTP 路由层
│   │   │   ├── agent.py             # Agent 对话：/run（同步）、/stream（SSE）、/actions/confirm（HITL）
│   │   │   ├── conversations.py     # 会话线程与消息
│   │   │   ├── orders.py            # 消费者订单查询
│   │   │   ├── products.py          # 商品列表/详情（只读）
│   │   │   ├── knowledge.py         # RAG 检索（/api/knowledge/search）
│   │   │   ├── management_policy.py # 租户售后政策 GET/PUT（staff/admin）
│   │   │   ├── management_knowledge.py # 知识 chunks/documents 管理与文档上传
│   │   │   ├── evaluations.py       # 离线评估 run/status/experiments（仅 ADMIN）
│   │   │   ├── tools.py             # 工具定义列表与手动执行接口
│   │   │   └── health.py            # 健康检查
│   │   ├── application/
│   │   │   ├── agent/
│   │   │   │   ├── graph.py         # StateGraph 拓扑 + 原生多流事件映射
│   │   │   │   ├── nodes.py         # 6 节点：intent_classify/policy_lookup/rag_retrieve/handoff/task/compliance_check
│   │   │   │   ├── task_agent.py    # create_agent 任务子图（含空回复重试中间件）
│   │   │   │   ├── governance.py    # 工具治理：可信身份/防幻觉/幂等/审计
│   │   │   │   ├── facade.py        # Agent 对外门面（构图、事件协议、interrupt 检测/resume）
│   │   │   │   └── context.py       # 节点运行时上下文
│   │   │   ├── tools/builtin.py     # 9 个原生 @tool 单例（ALL_TOOLS）
│   │   │   ├── services/            # 退款资格判定等业务服务
│   │   │   ├── schemas/             # Pydantic 请求/响应/AgentState Schema
│   │   │   └── auth.py              # ActorMiddleware + 租户/角色权限
│   │   ├── domain/                  # 领域层：models / repositories / 常量（含三租户政策）
│   │   ├── infrastructure/
│   │   │   ├── db/engine.py         # SQLAlchemy 异步引擎
│   │   │   ├── redis/               # Redis 客户端
│   │   │   ├── llm/
│   │   │   │   ├── providers.py     # ChatOpenAI / OpenAIEmbeddings 构建 + Retriever 协议
│   │   │   │   └── classifiers.py   # IntentClassifierProtocol + LLMIntentClassifier（4 分类）
│   │   │   ├── agent/checkpoint.py  # Redis checkpointer 单例（AsyncRedisSaver）
│   │   │   └── vectorstore.py       # KnowledgeVectorStore / PgVectorStoreRetriever（表 knowledge_vectors）
│   │   ├── evals/                   # 离线评估：runner/target/judge/evaluators/dataset
│   │   │   └── cases/               # 黄金用例：intent.json(12) / knowledge.json(13) / task.json(10)
│   │   ├── core/                    # 错误码 / 日志 / 基础设施 bundle
│   │   ├── config.py                # Pydantic Settings（env_nested_delimiter="__"）
│   │   └── main.py                  # FastAPI 入口：中间件/路由/异常处理/lifespan 装配
│   ├── alembic/versions/            # 6 个迁移：0001 身份 / 0002 政策 / 0003 订单 / 0004 工具 / 0005 会话 / 0006 商品
│   ├── scripts/
│   │   ├── seed_tenants.py          # 3 租户 + 9 用户 + 结构化政策
│   │   └── seed_orders.py           # 4 个演示订单（每租户均有覆盖）
│   ├── tests/                       # pytest 单元/HTTP 测试
│   ├── Dockerfile / pyproject.toml / alembic.ini / conftest.py
├── frontend/
│   └── src/
│       ├── App.tsx                  # 主界面，5 个导航按角色显隐
│       ├── components/
│       │   ├── MessageBubble.tsx    # 消息气泡（流式打字机 +「正在思考中」动效 + 工单/确认卡片）
│       │   ├── IdentitySwitcher.tsx # 9 宫格身份切换
│       │   ├── DebugDecisionPanel.tsx # 每轮节点/检索/工具决策面板
│       │   └── EvaluationPage.tsx   # 评估实验触发与结果页
│       ├── identity/IdentityContext.tsx
│       ├── lib/sse-client.ts        # SSE fetch 客户端
│       └── demo-tokens.ts           # 9 个预置演示 JWT
├── docs/
│   ├── knowledge/                   # 三个租户 FAQ 源文档（通过知识库页面上传）
│   ├── architecture.md              # 架构设计
│   ├── agent_code.md                # Agent 代码说明
│   ├── frontend-backend-interaction.md
│   ├── tasks.md / summary.md
├── docker-compose.yml               # postgres / redis / backend / frontend
├── .env.example                     # 环境变量模板
├── PRD.md                           # 产品需求文档
├── AGENTS.md                        # 开发规则与硬约束
└── agent-design.md                  # Agent 链路设计总结
```

---

## Agent 架构

### 状态图拓扑

```
START
  → intent_classify ──4 路条件路由──┐
      simple_qa    ──────────────────────────────→ compliance_check
      handoff      → handoff ───────────────────→ compliance_check
      knowledge_qa → policy_lookup ──命中────────→ compliance_check
                                   ──未命中→ rag_retrieve → compliance_check
      task         → task（langchain create_agent 子图，写工具内置 interrupt）
                     → compliance_check
  → compliance_check（规则合规校验 + LLM 包装成自然语言）→ END
```

### 工具清单（9 个 `@tool`）

| 类别 | 工具 | 说明 |
| --- | --- | --- |
| 只读 | `product_list` / `product_query` | 商品列表（名称/分类/状态过滤）/ 商品详情（product_id 优先，其次 sku_code） |
| 只读 | `order_query` | 订单详情（强制租户与本人归属；返回会裁剪冗长物流轨迹） |
| 只读 | `policy_check` | 调用纯函数做结构化退换货资格判定，结果确定性 |
| 只读 | `current_time` | 当前时间（保修/退货天数计算基准） |
| 写（HITL） | `refund_request` / `exchange_request` / `repair_request` / `cancel_order` | 退款 RF / 换货 EX / 维修 RP / 取消 CX，执行前弹确认卡片 |

### HITL 时序

写工具调用 `interrupt(pending)` → 外层图暂停、状态写入 Redis（AsyncRedisSaver，checkpoint TTL 10 分钟）→ 后端检测到暂停态后下发 `confirmation_required` SSE 帧 → 前端渲染确认卡片 → `POST /api/agent/conversations/{thread_id}/actions/confirm` 以 `Command(resume=...)` 恢复 → 工具完成写入 → compliance_check → 最终回复。

---

## 快速开始

### 前置条件

- Docker Engine 24+ / Docker Compose v2（推荐）
- 或本地：Python 3.10+、Node.js 18+（推荐 20）、PostgreSQL 16 + pgvector、Redis Stack（含 RedisJSON 模块）
- 一个 OpenAI 兼容协议的 API Key（演示默认使用智谱 GLM，亦可使用任意兼容端点）

### 方式一：Docker Compose 一键启动（推荐）

```bash
# 1. 准备环境变量
cp .env.example .env
# 编辑 .env，至少填写 LLM__OPENAI_API_KEY；默认端点/模型已按智谱 GLM 配好

# 2. 启动全部服务（首次构建约 2-3 分钟）
docker compose up -d --build

# 3. 初始化数据库与演示数据（首次或清空数据卷后执行）
docker exec ai_cs_backend alembic upgrade head
docker exec ai_cs_backend python -m scripts.seed_tenants
docker exec ai_cs_backend python -m scripts.seed_orders

# 4. 上传 FAQ 知识文档
#    使用 staff/admin 身份登录前端 →「FAQ / 知识库」页面，
#    将 docs/knowledge/ 下三个租户的 FAQ.md 分别上传到对应租户。
#    （知识库无 seed 脚本；商品数据同样需手工录入，products 表初始为空。）
```

启动后访问：

- 前端：http://localhost:5174
- 后端 API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

```bash
# 查看日志 / 停止
docker logs ai_cs_backend -f --tail 200
docker compose down            # 保留数据
docker compose down -v         # 同时删除数据卷（清空数据库）
```

### 方式二：本地开发（热重载）

**后端：**

```bash
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[llm,vector,demo,dev]"

# .env 放在仓库根目录或 backend/ 下均可（后者覆盖前者），DB/Redis 指向 localhost
alembic upgrade head
python -m scripts.seed_tenants
python -m scripts.seed_orders

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**前端：**

```bash
cd frontend
npm install
npm run dev        # http://localhost:5174，/api 自动代理到 http://127.0.0.1:8000
```

---

## 配置说明

所有配置通过环境变量注入，嵌套字段用双下划线 `__` 分隔（如 `DB__URL`）。仓库根目录 `.env` 与 `backend/.env` 都会加载，字段相同以后者为准。

### 基础 / 数据库 / Redis

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_ENV` | `local` | 运行环境：local/test/staging/prod |
| `LOG_LEVEL` / `JSON_LOG` | `INFO` / `true` | 日志级别 / 是否 JSON 结构化输出 |
| `CORS_ORIGINS` | `["http://localhost:5174","http://localhost:3000"]` | 允许的前端源（JSON 数组） |
| `DB__URL` | 必填 | `postgresql+psycopg://user:pass@host:5432/dbname`（psycopg3 风格） |
| `DB__POOL_SIZE` / `DB__MAX_OVERFLOW` | `10` / `20` | 连接池大小 / 溢出连接 |
| `DB__POOL_RECYCLE` / `DB__ECHO` | `1800` / `false` | 连接回收秒数 / 打印 SQL |
| `REDIS__URL` | 必填 | `redis://:pass@host:6379/0` |
| `REDIS__SOCKET_CONNECT_TIMEOUT` / `REDIS__SOCKET_TIMEOUT` | `2.0` / `3.0` | Redis 超时秒数 |

### 安全 / 演示令牌

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SECURITY__DEMO_TOKEN_SECRET` | 必填 | JWT HS256 密钥，至少 32 字符 |
| `SECURITY__DEMO_TOKEN_TTL_SECONDS` | `2592000`（30 天） | 演示令牌有效期 |

> ⚠️ `frontend/src/demo-tokens.ts` 中的 9 个预置 JWT 按默认 secret `change-me-to-a-random-32-chars-string-please` 签名；修改 secret 后需重新生成全部令牌。

### 大模型与 Embedding（必填项）

| 变量 | compose 默认值 | 说明 |
| --- | --- | --- |
| `LLM__PROVIDER` / `LLM__EMBEDDING_PROVIDER` | `openai` | 仅支持 OpenAI 兼容协议（智谱/DeepSeek/OpenAI 官方等） |
| `LLM__OPENAI_API_KEY` | 空（**必填**） | 未配置时应用启动直接失败，不做 mock 降级 |
| `LLM__OPENAI_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4/` | 兼容协议端点，留空走 OpenAI 官方 |
| `LLM__CHAT_MODEL` | `glm-4-flash` | 聊天模型 |
| `LLM__CHAT_TEMPERATURE` / `LLM__CHAT_MAX_TOKENS` | `0.2` / `1024` | 采样参数 |
| `LLM__EMBEDDING_MODEL` | `embedding-3` | Embedding 模型 |
| `LLM__EMBEDDING_DIM` | `1024` | 向量维度（智谱 embedding-3 为 1024；OpenAI text-embedding-3-small 为 1536，须与模型匹配） |
| `LLM__EMBEDDING_BATCH_SIZE` / `LLM__EMBEDDING_REQUEST_TIMEOUT` | `32` / `15.0` | 批大小 / 超时秒数 |

> 更换 embedding 模型或维度后，旧向量与新查询不在同一向量空间，必须按文档名重传全部知识文档。

### LangSmith 可观测（可选）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LANGSMITH__TRACING_ENABLED` | `false` | 改 `true` 且配置 API Key 后生效；应用会自动注入 `LANGCHAIN_*` 标准环境变量 |
| `LANGSMITH__API_KEY` | 空 | 留空则完全静默禁用，不发任何请求 |
| `LANGSMITH__ENDPOINT` | `https://api.smith.langchain.com` | LangSmith 端点 |
| `LANGSMITH__PROJECT` | `ai-customer-service-demo` | 项目名（需在 LangSmith 控制台预先创建同名 Project） |

开启后重启 backend，日志出现 `langsmith.enabled` 即成功。Trace 中可查看 4 路分流拓扑、task 子图的多轮 Thought→Action→Observation、每步 Prompt/Completion、延迟与 Token 成本；metadata 自带 tenant_id / thread_id / actor 等字段，可按租户过滤。

### 离线评估

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EVALUATION__DATASET_NAME` | `ai-cs-golden` | LangSmith Dataset 名称 |
| `EVALUATION__JUDGE_MODEL` / `EVALUATION__JUDGE_API_KEY` / `EVALUATION__JUDGE_BASE_URL` | 空 | Judge 模型；全部留空时复用主聊天模型 |
| `EVALUATION__CONCURRENCY` | `4` | 用例并发度 |

### Agent 参数

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT__RAG_TOP_K` / `AGENT__RAG_SIMILARITY_THRESHOLD` | `4` / `0.5` | RAG Top-K / 相似度截断 |
| `AGENT__INTENT_CONFIDENCE_THRESHOLD` | `0.7` | 意图置信度阈值 |
| `AGENT__MAX_GRAPH_STEPS` / `AGENT__CLARIFICATION_MAX_COUNT` | `20` / `3` | 图最大步数 / 澄清次数上限（超限转人工） |
| `AGENT__PENDING_ACTION_TTL_SECONDS` | `1800` | 待确认动作业务有效期 |
| `AGENT__TOOL_DEFAULT_TIMEOUT_SECONDS` | `15.0` | 工具执行超时 |

---

## 知识库管理

- 入口：前端「FAQ / 知识库」页（staff/admin），或 `/api/management/tenants/{tenant_id}/knowledge/` 系列接口
- 文档：仅同租户 staff/admin 可写，越权/跨租户一律 404；支持 `.md` / `.markdown` / `.txt`，UTF-8，单个 ≤ 2MB
- 切片：`source=faq` 按问答对切分（一问一块，无问答结构时回退长度切分）；`policy_manual` / `operation_doc` 使用 RecursiveCharacterTextSplitter（500/80）
- 向量表：`knowledge_vectors`，metadata 含 tenant_id / source / doc_name / title / chunk_index；chunk id 为 uuid5 确定性生成，同名文档重传即覆盖
- 接口能力：chunks 增删查、文档列表（含 chunk 数）、按文档名读取按 chunk_index 拼接的全文、文档上传

---

## 离线评估

- 入口：前端「评估测试」页（仅 admin 可见），或 `/api/evaluations/run|status|experiments|experiments/{name}`（仅 ADMIN）
- 用例：`backend/app/evals/cases/` 下 35 条黄金用例（intent 12 / knowledge 13 / task 10），仅支持页面手动触发，无定时任务
- 执行：为每个用例创建临时会话经 `astream_events` 跑真实图，采集最终回复、节点补丁、`confirmation_required` 状态；task 类用例遇到确认暂停即作为评估终点（不 resume、不真正写库）
- 评分：Judge LLM 打分（faithfulness 等），实验与指标结果存 LangSmith Experiment，页面支持查看实验列表与逐用例反馈

---

## 演示指南

### 9 宫格身份与租户政策

| 租户 | 品牌 | 政策特点 | 角色 |
| --- | --- | --- | --- |
| `tenant_a` | 禅饰坊（日常百搭） | 普通款 7 天无理由 / 质量问题 30 天包退换 / 非质量退货 0 手续费 | consumer / staff / admin |
| `tenant_b` | 梵印阁（高端定制） | 不支持 7 天无理由 / 仅质量问题 30 天可退换 / 终身成本价维修 | consumer / staff / admin |
| `tenant_c` | 玉语轩（品质文玩） | 7 天无理由但非质量收 10% 手续费 / 质量问题 15 天包退换 | consumer / staff / admin |

导航按角色收敛：consumer → 智能客服 + 我的订单；staff → 售后政策管理 + FAQ/知识库；admin → 上述 + 评估测试。

### 示例对话 Prompt

```text
# 政策对比（切换三个租户的 consumer 各问一次）
你们支持几天无理由退货？

# 退款 HITL（tenant_a consumer，签收 3 天，0 手续费）
订单 A-ORD-202509-001 我要退款，不喜欢了

# 质量问题换货（tenant_a，订单备注「珠子有裂痕」）
我在 A-ORD-202509-002 买的手串珠子裂了，想换货

# 政策拦截（tenant_b 梵印阁，无理由不适用）
B-ORD-202509-003 我要无理由退货

# 转人工
帮我转人工，我要投诉
```

### 调试面板

聊天区下方的 DebugDecisionPanel 按轮展示：意图分类与 hint、订单号候选、结构化政策判定、RAG 命中片段、task 子图工具调用参数与结果、HITL 暂停/恢复状态。

---

## 数据库与 Redis 查看

容器连接参数（compose 内网络主机名分别为 `postgres` / `redis`；宿主机直连用 `localhost`）：

- PostgreSQL：`postgres/postgres`，库名 `ai_cs_demo`，端口 `5432`
- Redis：无密码，db 0，端口 `6379`

```bash
# psql
docker exec -it ai_cs_postgres psql -U postgres -d ai_cs_demo
# 常用查询：\dt 查表；SELECT * FROM knowledge_vectors ...；SELECT ... FROM conversation_threads ...

# redis-cli（checkpointer 键为 LangGraph 序列化结构，用 JSON 命令查看）
docker exec -it ai_cs_redis redis-cli

# GUI 直连：DBeaver / TablePlus / Navicat 连 localhost:5432；RedisInsight 连 localhost:6379

# 清空全部数据重来
docker compose down -v && docker compose up -d --build
```

> 必须使用 `redis/redis-stack-server` 镜像并以 `/entrypoint.sh` 启动，普通 `redis:7-alpine` 缺少 RedisJSON 模块，AsyncRedisSaver 的 JSON 命令会报错。

---

## 常用命令

```bash
# ---- 后端 ----
cd backend
alembic upgrade head            # 迁移到最新
alembic downgrade -1           # 回滚一个版本
pytest                          # 全部测试
pytest tests/unit/test_task7_graph.py -v   # 单个模块
ruff check .                    # lint
ruff format .                   # 格式化

# ---- 前端 ----
cd frontend
npm install
npm run build                   # tsc 类型检查 + vite 构建
npm run preview                 # 预览构建产物（4173）
```

---

## 相关文档

- 产品需求：[PRD.md](PRD.md)
- 架构设计：[docs/architecture.md](docs/architecture.md)
- Agent 代码说明：[docs/agent_code.md](docs/agent_code.md)
- 前后端交互：[docs/frontend-backend-interaction.md](docs/frontend-backend-interaction.md)
- Agent 链路设计总结：[agent-design.md](agent-design.md)
- 开发规则与硬约束：[AGENTS.md](AGENTS.md)

---

## 常见问题

**Q：后端启动失败，提示 `OpenAI API Key 未配置`？**
A：大模型是强依赖，没有 mock 兜底。请在 `.env` 填写 `LLM__OPENAI_API_KEY`，并确认 `LLM__PROVIDER=openai`、base_url 与模型名匹配。

**Q：切换 embedding 模型后知识问答检索不到内容？**
A：向量维度/空间不一致（如 1024 ↔ 1536）。改完 `LLM__EMBEDDING_*` 后需在知识库页面按原文档名重传全部 FAQ，旧向量会被同名覆盖。

**Q：演示令牌失效（401）？**
A：检查 `.env` 的 `SECURITY__DEMO_TOKEN_SECRET` 是否保持默认值 `change-me-to-a-random-32-chars-string-please`；更换 secret 必须同步重新生成 `demo-tokens.ts` 中的 9 个 JWT。

**Q：写操作确认卡片点了没反应 / 确认过期？**
A：checkpoint 在 Redis 中保留 10 分钟，超时需重新发起诉求；业务结果以数据库工单/订单状态为准，幂等键保证不会重复写单。

**Q：如何确认租户隔离在数据库层生效？**
A：`docker exec ai_cs_postgres psql -U postgres -d ai_cs_demo -c "\d+ users" | grep -i check` 可看到 `users_tenant_id_check` 等前缀 CHECK 约束，属于 DB 层硬隔离而非仅靠应用层 WHERE。

# 手串售后智能客服 Agent - 面试演示 MVP 实施任务队列

> 按依赖顺序实施，每次一个模块：接口/schema → 测试 → 实现 → pytest+ruff → 验收记录。
> 不跨模块实现，不自动 git commit。AC 对应 [spec.md](./spec.md)。

---

## Task 1: 工程基础与依赖锁定（T1 工程底座）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: None
- **Description**:
  - 生成 `backend/pyproject.toml`，锁定 Python 3.14 依赖（FastAPI、LangChain、LangGraph、SQLAlchemy 2.x、psycopg[binary,pool]、pgvector、Pydantic v2、pydantic-settings、structlog、pytest、pytest-asyncio、ruff、alembic、httpx、redis、tenacity）
  - 调研并锁定 LangGraph PostgreSQL checkpointer 兼容方案（优先 langgraph-checkpoint-sqlalchemy 或 langgraph-checkpoint-postgres，验证 Python 3.14 兼容性，备选实现轻量自定义 checkpointer）
  - 定义 `backend/app/config.py`：Settings(Pydantic) 全量类型化配置（数据库、Redis、LLM/embedding、安全、LangSmith、mock 开关），缺失必填配置启动抛明确错误
  - 定义 `backend/app/core/errors.py`：自定义异常基类 + 稳定错误码枚举 + 统一错误响应 Pydantic schema
  - 定义 `backend/app/core/logging.py`：structlog JSON 配置，request 级绑定中间件（tenant_id/request_id/session_id/trace_id），敏感字段脱敏过滤器（token/phone/address）
  - 更新 `.env.example`：补全所有配置项，带注释区分必填/可选，无示例密钥
  - 生成 `backend/pytest.ini` + `backend/conftest.py`：pytest async 模式、测试 DB/Redis fixture、httpx.AsyncClient app fixture
  - 生成 `backend/ruff.toml`：规则对齐 AGENTS.md（Google docstring、类型注解、行宽等）
  - 更新 `docker-compose.yml`：backend / postgres:16-alpine(pgvector) / redis:7-alpine 三服务，持久化卷、健康检查、depends_on 顺序，环境变量映射
  - `backend/app/main.py`：FastAPI 应用骨架 + 生命周期钩子（DB/Redis/模型客户端初始化/关闭）+ `/health` 路由 + 全局异常处理器 + structlog 请求 ID 中间件
  - 验证：服务启动（无必填配置报错）、`/health` 200、ruff 0 告警、基础 smoke test 通过
- **Acceptance Criteria Addressed**: AC-6, AC-7
- **Test Requirements**:
  - `rule` TR-1.1: 缺少必填 DB_URL 时启动抛出明确的配置错误（非 KeyError/AttributeError）
  - `rule` TR-1.2: `/health` 返回 JSON `{"status": "ok"}`，HTTP 200
  - `rule` TR-1.3: 未知异常被全局处理器捕获，返回稳定错误码，不暴露 Python 堆栈
  - `rule` TR-1.4: structlog 日志输出 JSON 格式，绑定 request_id、tenant_id（有值时）
  - `rule` TR-1.5: `ruff check backend/app` 退出码 0，无告警
  - `rubric` TR-1.6: 目录分层清晰性；scale 1-5；anchors 1=堆文件无目录 3=有基本 api/core 5=严格分层( api/application/domain/infrastructure/core/tests )；threshold >= 4；evidence `find backend/app -type f | sort` 输出
- **Notes**: 先确认 checkpointer 兼容方案再写代码；mock LLM 作为默认模式，真实 LLM 仅通过 env 切换。

---

## Task 2: 身份与租户模块（T2）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 1
- **Description**:
  - **Schema 定义**：`Tenant`、`User`、`Role`、`DemoTokenClaims` Pydantic schema（请求/响应/数据库模型）
  - **Alembic 迁移**：tenants、users 表（复合外键含 tenant_id，账号与租户绑定唯一约束；手机号不是查询凭证）
  - **Domain Repository**：`TenantRepository`、`UserRepository`（所有查询强制 tenant_id，消费者额外 user_id 过滤；查无结果返回统一 404 不泄露存在性）
  - **演示令牌签发/验证**：JWT（HS256，服务器签名密钥），claims 含 actor_id/tenant_id/role/exp；签发接口校验账号属于指定租户；验证中间件解析并注入请求上下文（`RequestContext`）
  - **请求上下文注入**：ASGI 中间件校验请求头 `X-Tenant-Id` 与 token claims 的 tenant_id 一致；不一致返回 401；上下文存 `app.state` 或 ContextVar，供 Repository/Service 读取
  - **种子数据脚本**：`backend/scripts/seed_tenants.py` 幂等导入 3 租户（tenant_a 普通/tenant_b 定制/tenant_c 质量）× 每租户 3 角色（consumer/staff/admin）共 9 账号，输出可直接用的演示令牌
  - 三租户差异化政策文本（先作为常量/JSON，T5 再入库）：tenant_a 普通商品 7 天无理由（定制除外）；tenant_b 定制商品不支持 7 天，仅质量问题 30 天；tenant_c 质量问题 15 天包换，非质量收 10% 手续费
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-6
- **Test Requirements**:
  - `rule` TR-2.1: 有效令牌含 tenant_a/consumer，请求头 `X-Tenant-Id: tenant_a` → 中间件放行，上下文正确注入
  - `rule` TR-2.2: 令牌 tenant_a 配 `X-Tenant-Id: tenant_b` → 401 拒绝，不进入业务逻辑
  - `rule` TR-2.3: 伪造令牌（签名错误）/过期令牌 → 401 拒绝
  - `rule` TR-2.4: consumer 角色通过 Repository 查询同租户他人 user → 返回空/统一 not found，不抛出"属于其他用户"
  - `rule` TR-2.5: 种子脚本连续执行 2 次，不产生重复账号（idempotent）
- **Notes**: 令牌签名密钥从 Settings 读取，禁止硬编码；Repository 层必须显式接收 tenant_id 参数（不能隐式从 ContextVar 拿，保证可测试性）。

---

## Task 3: 订单与物流模块（T3 只读）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 2
- **Description**:
  - **Schema 定义**：`Order`（含 Decimal 金额/币种/签收时间/状态/定制属性/可退金额）、`Logistics`（订单关联轨迹）Pydantic 模型 + DB ORM
  - **Alembic 迁移**：orders 表（tenant_id + order_no 租户内唯一）、logistics 表（tenant_id 复合外键 orders）
  - **Repository**：`OrderRepository.get_by_order_no(tenant_id, user_id, order_no)`（强制三元组）；跨租户/他人均返回空
  - **Application Service**：`OrderService.query_order_and_logistics(ctx, order_no)` → 缺 order_no 抛 `MissingSlotError` 供上层追问；有权限返回 OrderDetailDTO（含物流轨迹）
  - **种子导入脚本**：`backend/scripts/seed_orders.py` 幂等导入 3 租户 × 每租户 2~3 订单 × 若干物流轨迹，覆盖场景：A1001（已签收 3 天，可全额退）、A1002（定制商品，已签收 1 天，tenant_a 不支持 7 天）、B2001（tenant_b 定制，质量场景）、C3001（tenant_c 已签收 20 天，超 15 天仅 10%）等
  - 预留扩展：`BaseTicketService` 抽象基类（工单查询占位，AC-6 扩展点）
- **Acceptance Criteria Addressed**: AC-2, AC-6
- **Test Requirements**:
  - `rule` TR-3.1: user_a1 (tenant_a) 查 A1001 → 返回完整订单 + 物流
  - `rule` TR-3.2: user_a1 查 A1003 (同 tenant_a 他人订单) → 返回统一空（不区分"无订单"或"无权"）
  - `rule` TR-3.3: user_a1 查 B2001 (tenant_b 订单) → 返回统一空
  - `rule` TR-3.4: 缺 order_no → 服务抛 `MissingSlotError(slot="order_no")`
  - `rule` TR-3.5: 种子脚本重跑不重复，金额字段为 Decimal(12,2)，非 float
- **Notes**: 金额统一用 `decimal.Decimal` + PostgreSQL NUMERIC，禁止 float 累计。

---

## Task 4: 工具框架（T7，为退款和交接打基础）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 2, Task 3
- **Description**:
  - **Alembic 迁移**：tool_audit_logs（调用 ID、身份、脱敏参数摘要、状态/错误码、trace_id、时间）、idempotency_records（tenant_id+op_type+key 复合唯一、参数摘要、执行状态、结果引用）
  - **BaseTool 协议**（ABC）：`name`、`description`、`args_schema: type[BaseModel]`、`async def execute(ctx, params) -> BaseModel`；所有工具继承此类
  - **ToolRegistry / Allowlist**：显式注册，未注册工具调用拒绝；执行前 Pydantic 校验参数
  - **可信身份注入 Hook**：execute 前从 `RequestContext` 取 tenant_id/actor_id/role 注入工具执行上下文；**丢弃模型参数中出现的 user_id/tenant_id 字段**，并在审计中记录此类尝试
  - **审计中间件**：工具调用前写 `started` 审计；成功/失败/拒绝分别写对应终态审计；审计写入失败 → 业务不提交（关闭开关）
  - **通用幂等装饰器**：`@idempotent(op_type, key_from_params_fn)` → 先查 idempotency_records，命中同参数摘要直接返回原结果；不同参数抛冲突；新执行写入记录
  - **超时与错误分类**：用 tenacity 控制超时，区分 `明确失败`（异常类型已知）与 `结果未知`（超时/连接中断）；未知写结果先查幂等记录再决定
  - **注册第一个工具**：`OrderQueryTool`（封装 Task3 OrderService，只读幂等语义）
  - 预留扩展：`ExchangeTool`、`RepairTool` 占位骨架（raise NotImplementedError 附实现思路注释，AC-6）
- **Acceptance Criteria Addressed**: AC-4, AC-6, AC-7
- **Test Requirements**:
  - `rule` TR-4.1: OrderQueryTool 合法调用 → tool_audit_logs 成功记录，参数 order_no、身份正确
  - `rule` TR-4.2: 模型在 OrderQueryTool params 中塞 `user_id=其他用户` → 被 Hook 丢弃用 ctx 身份，审计标 `identity_override_attempted`
  - `rule` TR-4.3: 调用 `NotRegisteredTool` → Allowlist 拒绝，audit 记录拒绝原因
  - `rule` TR-4.4: 同 idempotency_key 两次 refund_create 模拟（同参数）→ 第二次不重复执行业务，返回同结果；改金额同 key → 冲突错误
  - `rule` TR-4.5: 工具 execute 抛 ValueError → audit 记录失败状态+错误码，响应错误但不 panic
- **Notes**: 审计日志的"参数摘要"用 SHA256(normalized_params_json)，不存完整参数（防敏感泄露+节省空间）。

---

## Task 5: 知识导入与 RAG（T5+T6，政策问答链路）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 2, Task 4
- **Description**:
  - **Schema 定义**：`KnowledgeDoc`（doc_id/tenant_id/version/title/source/content_hash）、`KnowledgeChunk`（chunk_id/tenant_id/doc_id/version/position_start/end/text/embedding）
  - **Alembic 迁移**：knowledge_docs、knowledge_chunks 表；knowledge_chunks 建 `IVFFlat` pgvector 索引（维度先按 Settings.embedding_dim 动态 SQL，默认 1536）
  - **Embedding 封装**：`BaseEmbeddingProvider` 抽象 + `MockEmbeddingProvider`（固定维度 mock 向量，默认启用）+ `OpenAIEmbeddingProvider`（env 切换）；创建/查询时严格校验维度与 Settings 一致
  - **受控导入脚本**：`backend/scripts/seed_knowledge.py`（幂等：按 content_hash 去重；同 doc 新版本创建新版本号，不覆盖旧版本）。三租户差异化政策（Task2 中那三段）拆分为若干 chunk（按段落分，保留 position）
  - **Retrieval Service**：`RetrievalService.retrieve(ctx, query, top_k=4)` → SQL 层 `WHERE tenant_id = %s`，pgvector 余弦相似度；返回 `EvidenceChunk[]`（含 doc_id/chunk_id/version/title/text_snippet）
  - **引用校验机制**：Agent 生成节点只接受证据集合内 chunk 的引用；引用越界在 service 层截断并告警
  - **无依据处理**：top_k 全部相似度 < 阈值（配置项，默认 0.5）→ 返回空 evidence，生成节点输出"未找到相关政策"
  - 预留扩展：`BM25Retriever`、`Reranker` 抽象基类占位（AC-6）
- **Acceptance Criteria Addressed**: AC-1, AC-6
- **Test Requirements**:
  - `rule` TR-5.1: 用 tenant_a 身份问"七天无理由" → evidence 中 chunk.doc_id 全部为 tenant_a；SQL 日志确认 WHERE tenant_id 过滤
  - `rule` TR-5.2: 同问句三租户分别查，各自 chunk 的 tenant_id 正确，无交叉
  - `rule` TR-5.3: 生成节点尝试引用不在 evidence 集合的 chunk_id → 被引用校验器拒绝，不进入答案
  - `rule` TR-5.4: 完全无关查询（如"火星天气"，mock 向量可设置返回低相似度）→ evidence 空，回答"未找到相关政策"
  - `rule` TR-5.5: 知识导入脚本跑 2 次 → 不产生重复 doc/chunk（content_hash 唯一）
- **Notes**: 真实 embedding 可能花成本，默认 Mock 模式让 demo 无门槛运行；面试时可现场切真实模型。

---

## Task 6: 会话状态与 Checkpoint（T4 基础）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 2, Task 1（checkpointer 锁定）
- **Description**:
  - **Schema 定义**：`Session`（tenant_id/user_id/status[active/waiting_human/closed]）、`Message`（session_id/role/content/request_id/seq）、`ChatRequest`（tenant_id/session_id/request_id 唯一/payload_hash/run_id/status/result_ref）、`PendingAction`（action_id 唯一/tenant_id/session_id/tool/params_hash/policy_version/expires_at/status[pending/confirmed/executed/cancelled/expired/invalidated]）
  - **Alembic 迁移**：以上 4 表；checkpoint 表（按 LangGraph checkpointer 要求的 schema，含 thread_id 前缀 tenant_id:session_id）
  - **Checkpointer 集成**：LangGraph PostgreSQL checkpointer；服务端生成 thread_id = f"{tenant_id}:{session_id}"；恢复前校验会话权限（用户是否属于该会话）
  - **Request 去重**：同 (tenant_id, request_id) 查 ChatRequest → 命中同 payload_hash 返回原结果；不同 payload_hash 抛 409 冲突
  - **会话并发占用**：chat_requests 表版本号 + 租约字段；同 session 已有 in_progress run → 返回 `conversation_busy` 错误（明确、可展示）
  - **PendingAction 生命周期方法**：`create / confirm_valid / cancel / mark_expired / invalidate`（状态机校验非法转换抛错）
  - 预留扩展：`LongTermUserProfile` 抽象（AC-6）
- **Acceptance Criteria Addressed**: AC-3, AC-9, AC-6
- **Test Requirements**:
  - `rule` TR-6.1: 同 request_id 两次相同 payload → 第二次返回第一次的响应，不追加新 message
  - `rule` TR-6.2: 同 request_id + payload 变 → 409 冲突
  - `rule` TR-6.3: 同 session 并发请求 → 至少一个返回 conversation_busy
  - `rule` TR-6.4: PendingAction 状态机：pending → executed 不允许（必须经过 confirmed）；pending → cancelled 允许
  - `rule` TR-6.5: thread_id 被客户端伪造（直接传别人的 session_id）→ 权限校验失败，不恢复 checkpoint
- **Notes**: Checkpoint 不是业务事实来源，PendingAction/ChatRequest/Refund 等业务表才是；恢复时先查业务表再决定图节点是否重跑。

---

## Task 7: Agent 编排（LangGraph 图，T10 裁剪版）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 4, Task 5, Task 6
- **Description**:
  - **意图识别**：简化版 `IntentClassifier`（mock 规则+可选 LLM），输出 `{intent: policy|order_query|refund|handoff|clarify, confidence, slots, need_human, handoff_reason}`；置信阈值配置化
  - **AgentState 定义**（TypedDict 或 Pydantic）：`tenant_id_ref, user_id_ref, session_id, request_id, run_id, intent, slots, clarification_count(max=3), evidence: list[EvidenceChunk], tool_results_refs, pending_action_id, handoff_status, messages`（身份由服务端注入，State 中只存引用 ID）
  - **LangGraph 节点**实现：
    1. `identity_validate_node`（从 ctx 注入身份到 State，失败抛错）
    2. `handoff_gate_node`（输入类型为图片/明确转人工词 → 立刻走 handoff_tool）
    3. `input_router_node`（输入判别：confirm/cancel/message）
    4. `handle_action_node`（confirm：校验 action → 有效走 write_tool；cancel → pending 转 cancelled）
    5. `understand_node`（意图识别+槽位抽取）
    6. `router_node`（置信低/缺槽 → clarify_node 超限→handoff；policy→rag；订单→order_tool；退款→order→policy_check→prepare_pending_action）
    7. `rag_node`（调用 RetrievalService，写入 evidence）
    8. `order_tool_node`（调 OrderQueryTool，缺槽抛 MissingSlot）
    9. `policy_check_node`（退款资格：**确定性代码**，读订单+政策版本，校验状态/时间/金额，不准用 LLM 算金额）
    10. `prepare_pending_action_node`（创建 PendingAction，返回 confirmation，图暂停）
    11. `refund_write_node`（调 RefundCreateTool，事务写业务）
    12. `handoff_tool_node`（调 HandoffTool，写 waiting_human）
    13. `clarify_node`（生成追问消息）
    14. `generate_node`（统一生成：用 evidence/tool_result 生成自然语言回答，不准编造引用；无依据走规定话术）
  - **条件边**：路由优先级按 PRD 3.1 节；步数上限（MAX_STEPS=20）超限终止
  - **Agent 服务封装**：`AgentService.run(ctx, input_union[message|confirm|cancel])` → 调用图；返回 `AgentRunResult`（输出消息 + citation 列表 + confirmation 或 handoff 状态 + events）
- **Acceptance Criteria Addressed**: AC-5, AC-1, AC-2, AC-3, AC-10
- **Test Requirements**:
  - `rule` TR-7.1: 政策消息走图 → trace 节点顺序 identity→handoff_gate→input_router→understand→router→rag→generate
  - `rule` TR-7.2: 缺 order_no 的"查订单"→ understand→router→clarify→generate；clarification_count=3 后下一次→handoff
  - `rule` TR-7.3: 完整退款链路 → 节点顺序正确，prepare_pending_action 后图 END（返回 confirmation），第二次 confirm 事件进入 handle_action→refund_write→generate
  - `rule` TR-7.4: 步数超过 20 → 抛出 `MaxStepsExceeded`，图终止
  - `rule` TR-7.5: policy_check_node 订单金额校验 → 金额用 Decimal 计算，不准出现 float；边界值（刚好 7 天/超 7 天 1 天）正确
- **Notes**: 生成节点禁止直接输出工具原始数据，统一走展示 schema；"珠子裂了"等词在 handoff_gate_node 匹配立即交接（D3 立即执行）。

---

## Task 8: 退款写操作（T8 核心，连工具+事务）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 3, Task 4, Task 6, Task 7
- **Description**:
  - **Schema**：`Refund`（tenant_id/refund_id/order_id 唯一/action_id 唯一/amount[Decimal]/reason/status[submitted/approved/rejected] 模拟/mock_payment_refund_id）
  - **Alembic 迁移**：refunds 表（action_id UNIQUE；orders 剩余可申请额额外校验约束或应用层检查）
  - **注册工具**：`RefundCreateTool(args_schema=RefundCreateArgs)` → 接入工具框架（allowlist/审计/幂等/身份注入）
  - **资格规则引擎**（确定性代码，非 LLM）：`RefundQualificationService.check(order, policy_version)` → 读取订单签收时间、状态、定制属性、政策版本，返回可退金额/原因/条件；结果结构化（非自然语言）
  - **退款事务 5 步流程**（Task4 审计 + Task6 PendingAction + 幂等 + 新 refund 在同一 DB 事务）：
    1. 写 tool_audit `started`
    2. 开启事务，锁定 action + order（SELECT FOR UPDATE）
    3. 校验 action 归属/有效期/政策版本/订单状态/金额/剩余额度；不通过标记 action=invalidated，抛错
    4. 查/写 idempotency_records（tenant_id+"refund_create"+idem_key）
    5. INSERT refund，UPDATE action=executed，UPDATE idempotency=success，写 audit 成功，提交事务
  - 重复/并发/改参防御（AC-3 子场景 4/5）
  - **注册工具**：`RefundQueryTool`（只读）
- **Acceptance Criteria Addressed**: AC-3, AC-4
- **Test Requirements**:
  - `rule` TR-8.1: 完整走 prepare → confirm → 事务提交 → refund 表 1 条，action=executed，幂等记录 success
  - `rule` TR-8.2: 同一 action_id 并发 confirm（pytest `asyncio.gather` 2 次）→ 只 1 条 refund，另一请求返回原结果
  - `rule` TR-8.3: 改金额复用旧 action_id → 校验失败，action=invalidated，不写 refund
  - `rule` TR-8.4: 确认前订单状态/政策版本变化 → 执行前校验失败，重新要求确认
  - `rule` TR-8.5: 直接用 HTTP API（绕过 Agent）调 refund_create 接口 → 同样要求已确认 action_id，不能跳过确认流程
- **Notes**: 提交事务成功后再发 SSE，发送失败不回滚事务（客户端靠 request 查询）。

---

## Task 9: 人工交接占位（T9 占位实现）
- **Status**: `pending`
- **Priority**: `medium`
- **Depends On**: Task 4, Task 6, Task 7
- **Description**:
  - **Schema**：`Handoff`（handoff_id/tenant_id/session_id/reason/urgency/context_ref/idem_key/queue_status[queued]）
  - **Alembic 迁移**：handoffs 表（同 session 只有 1 条 active 的部分唯一索引，WHERE status='queued'）
  - **注册工具**：`HandoffTool(args_schema=HandoffArgs)` → D3 立即执行，但仍走工具框架（鉴权/审计/幂等）
  - **上下文摘要**：打包意图+槽位+工具历史+message 节选为 `context_ref`（存 handoffs.context_json）；摘要生成失败不阻断交接，回退到完整 message list
  - **会话状态阻断**：waiting_human 状态的会话，输入除"补充信息"外的退款/订单请求 → 返回"已在人工交接中，暂不自动处理"
  - 不实现：客服队列查询 UI、真人接管回复协议（AC-6 占位类 `BaseHumanAgentProtocol`）
- **Acceptance Criteria Addressed**: AC-10, AC-6
- **Test Requirements**:
  - `rule` TR-9.1: 输入"转人工"或"珠子裂了"→ handoff_tool 执行，handoffs 表写入记录，session.status=waiting_human，SSE handoff 事件内容符合 D2 说明
  - `rule` TR-9.2: 同会话 2 次转人工（同原因）→ 只 1 条 active handoff
  - `rule` TR-9.3: waiting_human 中请求退款 → 被阻断，不创建 PendingAction
  - `rule` TR-9.4: 摘要生成模拟失败（mock 抛异常）→ 交接仍完成，context_ref 存原始 messages JSON
- **Notes**: D3 立即执行 = handoff_gate_node 命中直接调工具，不额外弹 confirmation；但工具内部仍写审计+幂等。

---

## Task 10: HTTP 接口 + SSE（T11 裁剪）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 7, Task 8, Task 9
- **Description**:
  - **Schema**（Pydantic v2）：`ChatStreamRequest`（auth/tenant_id/request_id/session_id/input: Union[MessageInput|ConfirmInput|CancelInput]）；8 种 SSE 事件：MetaEvent/TokenEvent/CitationEvent/ToolCallEvent/ToolResultEvent/ConfirmationEvent/HandoffEvent/ErrorEvent/DoneEvent
  - **`POST /api/chat/stream`**：鉴权前置→schema 校验→调用 `AgentService.run_stream()` → 用 `StreamingResponse`（media_type=text/event-stream）推送 8 种事件；心跳用注释行
  - **流前后错误区分**：响应头写之前的错误→HTTP 错误码 JSON；写头之后→ErrorEvent 再 DoneEvent（关闭连接）
  - **GET 接口**：
    - `/api/sessions/{session_id}`：授权会话消息历史 + 当前 pending_action_id + status
    - `/api/requests/{request_id}`：原请求状态和结果引用（断线重查）
    - `/api/tools/order/{order_no}`：同 OrderQueryTool（工具公开 API）
    - `/api/tools/logistics/{order_no}`：（同上）
    - `POST /api/tools/refunds`：需要已确认 action_id，与 Agent 走同一 RefundService（AC-3 绕过保护）
    - `GET /api/tools/refunds/{refund_id}`：查询申请
    - `POST /api/tools/handoff`：D3 立即执行，公开 API
  - **tool_result 脱敏**：工具内部对象转 ToolResultDisplaySchema（不暴露内部 ID/金额精度以外的字段；电话/地址脱敏）
  - 预留扩展：`GET /api/handoffs` 路由骨架返回 501（未实现，占位）
- **Acceptance Criteria Addressed**: AC-5, AC-3, AC-8, AC-9, AC-10
- **Test Requirements**:
  - `rule` TR-10.1: `POST /api/chat/stream` 返回 `Content-Type: text/event-stream`，首条事件为 meta，结束有 done（状态正常/待确认/交接区分 done.status 字段）
  - `rule` TR-10.2: 鉴权失败在头前→HTTP 401 JSON，不进入 SSE
  - `rule` TR-10.3: 生成中途抛业务错误→发送 error 事件+done(failed)，连接正常关闭，不 hang
  - `rule` TR-10.4: `GET /api/requests/{request_id}` 在业务提交成功但 SSE 断开后→返回已成功终态
  - `rule` TR-10.5: 直接 `POST /api/tools/refunds`（不带 action_id）→ 422/业务错误，不绕过确认
  - `rubric` TR-10.6: SSE 事件顺序和正确性的集成测试可读性；scale 1-5；1=事件乱序无测试 3=基本顺序断言 5=完整链路事件序列+字段断言；threshold >= 4
- **Notes**: SSE 用 `text/event-stream` + `\n\n` 分隔事件；客户端 POST body JSON，不用 EventSource（只 GET）。

---

## Task 11: 前端演示 UI（T12 裁剪）
- **Status**: `pending`
- **Priority**: `medium`
- **Depends On**: Task 10
- **Description**:
  - 初始化前端：Vite + React + TypeScript + Tailwind；`docker-compose.yml` 加 frontend 服务（node 构建 + nginx 或 dev server）
  - **身份选择页面**：三租户 × 三角色卡片，点击直接用演示令牌（后端 seed 输出的 token 硬编码常量，仅面试便捷）存 localStorage
  - **聊天页**布局（Tailwind）：左侧会话列表（简化/占位），中间聊天窗口，右侧空（预留调试面板）
  - **SSE 客户端**：`fetch` + `ReadableStream` 逐块解析（处理多事件合并、UTF-8 边界、重连逻辑；EventSource 不用）
  - **消息渲染**：
    - 文本消息渲染 token 增量合并
    - citation 列表（点击展开原文片段 + doc_id/version）
    - tool_call / tool_result 组件（脱敏展示，绿色成功/红色失败/灰色进行中）
    - confirmation 卡片：订单号、原因、金额、政策依据引用、**两个显式按钮「确认」「取消」**（发 confirm/cancel 事件带 action_id）
    - handoff 状态条：橙色，文案"已模拟入队，首版不提供真人回复"（D2/D4 合规）
  - **无虚假 UI**：不显示"上传图片"按钮/入口（D4）；不显示"客服已上线"等文案
  - **断线恢复**：网络错误后保留 request_id，自动调 `GET /api/requests/{id}` 补终态
  - 预留扩展：`components/handoff/` 目录空占位，`components/admin/` 知识管理占位（AC-6）
- **Acceptance Criteria Addressed**: AC-8, AC-6, AC-10
- **Test Requirements**:
  - `rule` TR-11.1: 首页身份选择点击 consumer→ 进入聊天页，localStorage 存 token，请求头带认证
  - `rule` TR-11.2: SSE 事件 meta/token/citation/confirmation/done 顺序渲染正确；一个 token 事件分多次 read 不拆坏
  - `rule` TR-11.3: confirmation 卡片「确认」按钮触发 confirm 事件 payload 带 action_id；普通"好的"聊天消息不触发 confirm
  - `rule` TR-11.4: 整个页面 DOM 中找不到 `type="file"` 上传控件（D4 校验）
  - `rubric` TR-11.5: 代码组织与组件命名；scale 1-5；1=单文件 3=按页面拆分 5=按 domain+components+hooks 分层；threshold >= 3
- **Notes**: 前端 MVP 目标是"能演示+不乱来"，不追求精美；组件尽量简单，避免引入过多依赖。

---

## Task 12: 可观测与部署验证（T13+T15 裁剪，交付就绪）
- **Status**: `pending`
- **Priority**: `medium`
- **Depends On**: Task 10, Task 11
- **Description**:
  - **全链路 Trace 标识贯通**：request 生成 trace_id（uuid），structlog 绑定，LangSmith metadata 注入，SSE meta 事件携带
  - **LangSmith 接入（可降级）**：Settings.langsmith_api_key 有值则启用 LangChain Tracing；无值自动降级（不报错，不影响业务）
  - **Redis 不可用降级**：缓存层 try/except，失败回源 DB；限流失败放行（不硬失败）；写告警日志
  - **README 演示脚本**（中文，简洁）：
    1. 前置：Docker + 填写 .env（可留空 LLM key=启用 mock）
    2. 启动：`docker compose up --build`
    3. 5 分钟演示步骤清单（对应 AC-8 的 5 步）+ 15 分钟扩展演示（多租户对比/退款并发/状态恢复）
    4. 常见问题排查
  - **种子数据汇总入口**：`backend/scripts/seed_all.py` 顺序调用 tenant/order/knowledge 脚本
  - **启动脚本**：首次启动后自动执行迁移+种子（或明确手动步骤）
  - **最终验收跑**：本机 `pytest backend/tests` 全过 + `ruff check backend/app` 0 告警 + docker compose up 手动走 AC-8 步骤
- **Acceptance Criteria Addressed**: AC-8, AC-7
- **Test Requirements**:
  - `rule` TR-12.1: 未设置 LANGSMITH_API_KEY 启动应用 → 正常启动，无运行时错误；日志打印"LangSmith tracing disabled"
  - `rule` TR-12.2: Redis 停止时查询订单（缓存 miss）→ 回源 DB 返回正确结果，不崩溃
  - `rule` TR-12.3: `pytest backend/tests` 退出码 0（所有已写测试）
  - `rule` TR-12.4: `ruff check backend/app backend/tests` 退出码 0
  - `rule` TR-12.5: README 步骤在新环境（至少 mock 模式）能跑通 → 演示脚本 5 步不遇阻塞性错误
- **Notes**: 不生成独立评测集（T14），但核心 AC 对应 pytest 用例就是验收证据。

---

## 任务依赖速览（有向无环）

```
T1 工程基础
├─ T2 身份租户
│   ├─ T3 订单物流
│   │   └─ T8 退款写操作 ──────────────────────┐
│   ├─ T4 工具框架 ──────────────┬─ T7 Agent 编排 ─ T10 HTTP+SSE ─ T11 前端 ─ T12 交付
│   │   └─ T9 人工交接(占位)─────┘                │
│   └─ T6 会话+Checkpoint ────────────────────────┘
└─ T5 知识导入+RAG ────────────────┘
```

实施顺序建议：T1 → T2 → T3 → T4 → T6 → T5 → T7 → T8 → T9 → T10 → T11 → T12

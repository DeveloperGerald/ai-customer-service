# 手串售后智能客服 Agent —— 项目总结（面试演示版）

## 1. 项目介绍

### 1.1 项目是什么

一个**多租户售后智能客服 Agent**（面试演示型 MVP）：模拟三个手串品牌租户（tenant_a / tenant_b / tenant_c），消费者在前端与 AI 客服对话，完成政策问答、订单/物流查询、退款/换货/维修/取消订单等售后全流程。订单、物流、退款均为模拟业务，但 LLM 调用、向量检索、状态持久化、评估链路全部是真实工程实现。

### 1.2 核心功能

| 功能 | 说明 |
| --- | --- |
| 意图识别与路由 | LLM 分类器输出 4 大类意图（simple_qa / handoff / knowledge_qa / task）+ 子类型 hint，结合对话历史消歧 |
| 知识问答（政策优先 + RAG 兜底） | 先让 LLM 判断结构化政策字段能否直接作答，未命中再走 pgvector 向量检索 |
| 订单/商品查询 | order_query / product_query / product_list / current_time 只读工具 |
| 售后写操作（HITL） | 退款/换货/维修/取消订单，写工具内置 `interrupt`，前端弹确认卡片，用户点击后才真正执行 |
| 转人工 | 明确人工/投诉/高风险立即交接，生成工单号并保存上下文 |
| SSE 流式 | 节点级 + token 级双粒度流式，打字机效果 |
| 多租户隔离 | 中间件鉴权 + 仓储强制过滤 + 向量检索过滤，全链路 tenant_id 硬隔离 |
| 离线评估 | LangSmith Dataset + 确定性评估器 + LLM-as-judge，手动触发实验，Web 页查看报告 |
| 管理端 | 知识库文档上传/查看（按租户）、政策配置、商品/订单管理、评估实验页 |
| 可观测 | LangSmith tracing 全链路 Trace（按 tenant_id / thread_id 过滤） |

### 1.3 亮点

1. **LangGraph 1.x 原生状态图编排**：四分类意图路由 + `create_agent` ReAct 子图（task 分支）嵌套，所有分支汇入统一合规检查节点。
2. **人在回路（HITL）工程闭环**：`interrupt` → Redis checkpointer 暂停（TTL 10min）→ SSE 确认卡片 → `Command(resume=...)` 恢复，配套幂等键 + 审计日志 + 确定性工单号。
3. **工具治理中间件**：每次工具调用包裹审计（tool_audit_logs）、幂等（idempotency_records，24h TTL）、防幻觉护栏（拦截模型编造的订单号）。
4. **统一合规检查**：所有分支的草稿回复过规则校验（tenant_id 泄露屏蔽、工单号一致性、幻觉工单剥离、政策不满足禁说"成功"）再 LLM 润色。
5. **两级知识问答**：结构化政策字段优先（确定性高、可解释），RAG 兜底，回答忠实性可被评估器量化。
6. **真实的多租户硬隔离**：越权访问统一 404（不泄露资源存在性），向量检索强制 tenant filter。
7. **LangSmith 评估体系**：golden 数据集版本化同步、5 个评估器（3 确定性 + 2 LLM judge）、实验报告 Web 页展示。

### 1.4 技术栈

- **后端**：Python 3.12 / FastAPI / SQLAlchemy async / Alembic / Pydantic v2
- **Agent**：LangChain 1.4.3 / LangGraph 1.2.12 / langgraph-checkpoint-redis / LangSmith 0.14.2
- **LLM**：GLM-4-Flash（OpenAI 兼容协议），embedding-3（1536 维），judge 模型可配置
- **存储**：PostgreSQL 16 + pgvector（业务数据 + 向量库）、redis-stack-server（checkpointer，含 RedisJSON/RediSearch）
- **前端**：React 18 + TypeScript + Vite（fetch + ReadableStream 自研 SSE 客户端）
- **基础设施**：Docker Compose（postgres / redis / backend / frontend）

---

## 2. Agent 部分：一条消息的完整旅程

### 2.1 调用链路总览

```
POST /api/agent/conversations/{thread_id}/stream
  → ActorMiddleware（鉴权 + X-Tenant-Id 与 JWT 一致性校验）
  → _get_actor_and_verify_thread（线程归属校验，合法新 thread_id 懒创建）
  → facade.astream_events(actor, tenant_id, thread_id, user_message, session)
      ├─ 1. 写入本轮 human 消息到会话（即使后续失败也保留历史）
      ├─ 2. _bind_node_contexts：AgentNodeContext（session/actor/retriever/
      │      classifier/chat_model/task_agent/effective_policy）绑定到各节点
      ├─ 3. build_customer_service_graph：StateGraph 编译（挂 Redis checkpointer）
      ├─ 4. _fresh_turn_state：构造本轮初始 state（关键：清空输出型字段）
      ├─ 5. _build_langgraph_config：thread_id + LangSmith metadata + tracer
      └─ 6. astream_unified：graph.astream(stream_mode=["debug","messages"])
             → 统一事件协议（node_start/node_end/token_chunk）
             → facade 翻译成 start/node/tool/reply_chunk/reply/debug/done
             → API 层逐帧写成 SSE
```

### 2.2 图拓扑（四分类意图）

```
START → intent_classify ─┬─ simple_qa    ─────────────────────┐
                         ├─ handoff      → handoff ───────────┤
                         ├─ knowledge_qa → policy_lookup ─┬─ hit → │
                         │                                └─ miss → rag_retrieve → │
                         └─ task        → task（create_agent ReAct 子图）─┤
                                           ↓ 全部汇入
                                    compliance_check（规则合规 + LLM 包装）→ END
```

- **intent_classify**（[nodes.py](../backend/app/application/agent/nodes.py)）：LLM 分类器结合最近 8 条对话历史输出 4 大类 + intent_hint + 订单号候选。
- **policy_lookup**：LLM 判断租户结构化政策字段（退换天数/手续费/保修期/定制规则等）能否直接回答，命中即出草稿。
- **rag_retrieve**：pgvector 检索（强制 tenant 过滤），产出 rag_hits。
- **task**：内嵌 `create_agent` ReAct 子图，9 个工具（4 读 + 4 写 + policy_check），治理中间件包裹每次工具调用。
- **handoff**：mark_escalated + 生成工单号，预设文案直通。
- **compliance_check**：统一收口。规则合规修整 → LLM 流式润色（打字机 token 来源）→ 写库。

### 2.3 task 子图（ReAct Agent）

[task_agent.py](../backend/app/application/agent/task_agent.py) 用 LangChain 1.x `create_agent` 构建：

```python
create_agent(
    chat_model,
    ALL_TOOLS,          # 模块级 @tool 单例，身份经 context 注入
    middleware=[ToolGovernanceMiddleware(), _task_system_prompt, _RetryOnEmptyMiddleware()],
    state_schema=TaskAgentState,
    context_schema=AgentRunContext,   # tenant/actor/session/policy 每次调用注入
)
```

- **@dynamic_prompt**：每次模型调用时动态拼 system prompt（当前时间、订单号检测、调用顺序硬约束、RAG 片段、输出格式）。
- **ToolGovernanceMiddleware**：`awrap_tool_call` 包裹每次工具调用 → 审计 + 幂等 + 防幻觉 + 状态字段回传。
- **工具无状态**：模块级 `@tool` 单例，`ToolRuntime[AgentRunContext]` 注入身份/session，签名中无 tenant 参数（LLM 无法伪造租户）。

### 2.4 重点问题与解决方案（面试讲述点）

**Q1：多轮对话时 checkpointer 残留上一轮的 final_reply，compliance 短路返回旧回复？**
→ `_fresh_turn_state` 每轮显式清空「输出型字段」（final_reply/draft_reply/action_kind/escalated 等），保留「上下文型字段」（order_detail_json/rag_hits/tool_executions，供后续轮次引用）。

**Q2：create_agent 子图的 `ainvoke()` 不抛 GraphInterrupt，HITL 暂停检测不到？**
→ LangGraph 1.x 的 `Pregel.ainvoke()` 把 interrupt 捕获为 `result['__interrupt__']`；task_node 显式检查该 key 并 re-raise `GraphInterrupt`，冒泡到外层图由 checkpointer 落 Redis。

**Q3：AsyncRedisSaver 在主线程同步调用 `get_state()` 抛 InvalidStateError？**
→ 暂停态检测统一用 `await graph.aget_state(config)`（async 版本）。

**Q4：GLM-4-Flash 的两个臭毛病怎么治？**
- 长 tool result 后返回空响应（stall 25~96s 后 content 为空）：① `_slim_order_read` 砍掉 logistics.tracks 全量轨迹（1.3K→瘦身）；② `_RetryOnEmptyMiddleware` 检测空 AIMessage 后追加催促消息重试一次。
- 编造订单号（如 A-ORD-202509-001）：治理中间件护栏 `order_query` 的 order_no 必须真实出现在某条用户消息中，否则返回 VALIDATION_ERROR 失败观察，handler 不执行。

**Q5：interrupt 后 resume 工具整体重跑，工单号不一致（确认卡片 ≠ 最终结果）？**
→ `_stable_ticket_no` 用 uuid5(tenant+thread_id+工具+参数) 生成确定性工单号，两次执行必然相同，且天然幂等。

**Q6：节点异常导致整轮 500？**
→ facade `_trace` 包装器：`GraphBubbleUp`（HITL 信号）放行；其他异常捕获 → 写 node_errors + 安全草稿 `draft_context={"error": True}` → compliance 对错误草稿跳过 LLM 润色直接透传（防止 LLM 把"处理出现问题"包装成"工单已受理"的幻觉文案）。

**Q7：用户先说"我要取消订单"，下轮只发订单号，被误判成查询？**
→ intent_classify 加载最近对话历史给分类器；task 子图 system prompt 明确"判断意图必须结合对话历史，intent_hint 只是当前消息的表层信号"。

---

## 3. SSE 流式实现

### 3.1 后端实现

[api/agent.py](../backend/app/api/agent.py) 用 FastAPI `StreamingResponse` + async generator：

- **事件协议**：`start → node(start/end) → tool? → escalated? → confirmation_required? → reply_chunk* → reply → debug → done → [DONE]`。
- **token 来源**：`graph.astream(stream_mode=["debug", "messages"])` —— debug 流产出节点级事件，messages 流产出 LLM token；**只透传 `langgraph_node == "compliance_check"` 的 token 流**（task 子图内 agent 的中间流不透传，避免双重打字机）。
- **防缓冲三件套**：
  1. 每个业务帧后紧跟注释帧 `: sse-flush\n\n`，强制 Nginx/uvicorn 立即 flush（否则代理攒到 4~8KB 才发，打字机失效）；
  2. 响应头 `X-Accel-Buffering: no` + `Cache-Control: no-cache, no-transform` 等；
  3. 全链路 async generator，无一处同步阻塞。
- **异常兜底**：任何异常都 yield `error → done → [DONE]`，绝不把异常抛给 Starlette；commit 失败 non-fatal（内容已流式展示）。
- **序列化兜底**：`_json_default` 处理 dataclass/Pydantic/UUID/datetime，保证 SSE 帧绝不因序列化抛错。

### 3.2 前端实现

[frontend/src/lib/sse-client.ts](../frontend/src/lib/sse-client.ts)：

- **用 `fetch` + `ReadableStream`，不用 EventSource**：EventSource 只支持 GET、无法自定义 Authorization/X-Tenant-Id 请求头、无法带 body。
- **TCP 分块处理**：维护 buffer 字符串，按 `\n\n` 切帧，一帧跨多次 read 也不会拆坏。
- **UTF-8 多字节边界**：`TextDecoder.decode(value, {stream: true})`，结尾再 flush（否则中文可能乱码）。
- 忽略 `:` 开头的注释帧（后端的 flush-hint），`[DONE]` 作为终帧。

### 3.3 面试可能的问题

1. **SSE vs WebSocket 怎么选？** SSE 是单向（服务端→客户端）推送，对话场景用户发消息走普通 POST 即可，不需要全双工；SSE 基于 HTTP，天然过代理/防火墙，自带重连语义，实现简单。WebSocket 适合真正双向高频（如协同编辑）。
2. **为什么不用 EventSource？** 只支持 GET、无法自定义请求头（JWT/X-Tenant-Id）、无法带 body；fetch + ReadableStream 更灵活。
3. **SSE 的坑：代理缓冲。** Nginx 默认缓冲响应，小帧被攒住 → 打字机变成整段弹出。解法：`X-Accel-Buffering: no` + 注释帧强制 flush + 每帧写日志可复现定位。
4. **断线重连/幂等**：请求带 idempotency_key 作为写操作幂等 salt；SSE 重试复用同一 salt 不会重复写。
5. **背压（backpressure）**：async generator 天然拉取式，消费者慢时生产者自然挂起；token 级帧小，无内存压力。
6. **事件协议为什么这样设计？** 节点级事件驱动前端"决策过程"展示（正在分类意图/正在检索/正在调工具），token 级事件驱动打字机，reply 完整帧做兼容兜底（前端没看到 chunk 也能显示最终回复）。
7. **如何定位"前端没打字机效果"？** 后端每帧写结构化日志（event/size/elapsed_ms），可一眼判断是后端没产出还是中间层缓冲。

---

## 4. HITL（人在回路）实现

### 4.1 完整流程

```
用户："帮我取消订单 B-ORD-202509-019"
  → task 子图 ReAct：order_query → cancel_order
  → cancel_order 内部：校验订单归属/状态 → 政策/状态机校验
    → 生成确定性工单号 → interrupt(pending={tool,args,summary,order_no,expires_at})
  → GraphInterrupt 冒泡：子图 → task_node re-raise → 外层图暂停
  → checkpointer 把图状态落 Redis（TTL 600s）
  → facade.aget_state() 检测 snap.next 非空 → yield confirmation_required
  → SSE 推 confirmation_required 帧（本轮不发 reply/done）
  → 前端弹确认卡片（10 分钟倒计时）

用户点击「确认」→ POST /actions/confirm {decision: true}
  → facade.resume_stream：先 aget_state 校验仍在暂停态
  → Command(resume={"confirmed": true}) 恢复图
  → 写工具从头重跑：校验 → interrupt() 直接返回 resume 值 → confirmed → 执行真实写
  → compliance_check → SSE 流式输出最终结果
```

### 4.2 关键设计

- **状态持久化**：[checkpoint.py](../backend/app/infrastructure/agent/checkpoint.py) 用 `AsyncRedisSaver`（要求 redis-stack-server 的 RedisJSON/RediSearch 模块），TTL 600s 与 pending 的 expires_at 对齐——超时后 resume 返回"操作已超时或已被处理"。
- **工具重跑的副作用控制**（resume 时工具函数整体重跑）：
  - 订单加载/政策判定是幂等 SELECT + 纯函数，重跑无害；
  - 工单号用 `_stable_ticket_no`（uuid5）保证卡片与结果一致；
  - 真实写库（订单状态流转）放在 `interrupt()` 之后，且由订单状态机防重复；
- **幂等**：治理中间件为写工具生成幂等键 `agt-{thread_id}-{seq}-{salt}`，resume 复用暂停前那条 running 审计行（不产生悬垂行）；成功后写 idempotency_records（24h TTL），重放命中缓存直接返回；同 key 参数哈希不一致 → TOOL_IDEMPOTENCY_CONFLICT。
- **用户拒绝**：resume `{"confirmed": false}` → 工具返回 `accepted=False` → 治理中间件直写标准拒绝文案到 final_reply（绕过 LLM 改写，保证拒绝语义不被误读）。
- **审计**：tool_audit_logs 记录每次调用的参数哈希/状态/耗时/结果，按租户可查。

### 4.3 面试可能的问题

1. **为什么暂停状态存 Redis 而不是 DB/内存？** 暂停态是短暂的会话级状态（10min TTL），Redis 天然支持过期；DB 存临时态需要清理任务；内存态无法跨实例。langgraph-checkpoint-redis 原生支持。
2. **checkpointer 存的是什么？** 整个图的 StateSnapshot（各通道值 + next 节点 + task interrupts），resume 时从断点精确恢复，不是重新跑整轮。
3. **interrupt 发生在子图里，为什么能冒泡到外层？** 子图不挂 checkpointer，`GraphInterrupt`（`GraphBubbleUp` 子类）逐层冒泡到挂了 checkpointer 的外层图；中间每层 `_trace`/`awrap_tool_call` 都必须显式放行 `GraphBubbleUp`。
4. **10 分钟超时怎么实现？** Redis checkpointer TTL + pending payload 里的 expires_at，前端倒计时；超时后 `aget_state` 无 pending → 返回 error 事件。
5. **并发安全：用户双击确认/刷新页面重发怎么办？** resume 前先校验暂停态（已被处理则拒绝）；写操作幂等键 + DB 唯一约束兜底；订单状态机防重复流转。
6. **为什么不用 LangGraph 的 interrupt_before 编译期断点？** 断点粒度在节点，而本项目写确认发生在 ReAct 循环内部的工具调用里（模型动态决定调哪个写工具），运行期 `interrupt()` 才能精确挂在工具内部。
7. **多实例部署有问题吗？** 状态在 Redis，resume 请求落到任意实例都能恢复；facade 无状态（节点 ctx 每次请求重建绑定）。

---

## 5. RAG 实现

### 5.1 架构

[vectorstore.py](../backend/app/infrastructure/vectorstore.py) 基于 `langchain_postgres.PGVectorStore`：

- 独立向量表 `knowledge_vectors`；`tenant_id / source / doc_name / title` 声明为**真实元数据列**（可索引、filter 直译 SQL），chunk_index 存 langchain_metadata JSON。
- Embedding：OpenAI 兼容协议 embedding-3，1536 维（建表时固定）。
- 检索：`asimilarity_search_with_score` 返回余弦距离 → `similarity = 1 - distance` → 阈值过滤（默认 0.5~0.6）→ top_k（默认 4~5）。

### 5.2 两级知识问答（政策优先）

```
knowledge_qa → policy_lookup：LLM 判断结构化政策字段（return_days/
  restocking_fee_pct/warranty_days/custom_product_allowed...）能否直接回答
    ├─ 命中 → 政策答案草稿（确定性、可解释）→ compliance_check
    └─ 未命中 → rag_retrieve：向量检索 rag_hits → compliance_check
                  （system prompt 硬约束：必须基于检索片段，不得编造）
```

### 5.3 切片策略（按 source 路由）

- **FAQ（source=faq）**：问答对切分——以 `Q1：`/`Q：` 问题行为边界一问一块，答案多行续行同属一块，裸文本小标题以 `【标题】` 前缀拼入补充检索上下文；识别不到问答结构回退通用切分。
- **政策手册/操作文档**：`RecursiveCharacterTextSplitter`（Markdown 感知，chunk_size=500 / overlap=80）。
- **确定性 ID**：uuid5(tenant+doc_name+content)，同内容重传天然幂等；同 doc_name 重传先删旧切片再重建（覆盖式，编辑残留不遗留）。

### 5.4 面试可能的问题

1. **为什么 FAQ 不用等长切分？** 等长切分会把一个问答对拦腰切断、或把多个 Q&A 混进一块，检索命中后上下文不完整。按语义边界（问答对）切分，一块就是一个完整答案，命中即可用。
2. **为什么政策问答要走"结构化字段优先"而不是纯 RAG？** 政策是强约束数字（7 天/15% 手续费），RAG 检索+生成容易编造或误读；结构化字段 + LLM 判断能否回答，命中即确定性输出；RAG 兜底长尾问题。这也是评估里 faithfulness 指标能拿 1.0 的原因之一。
3. **换 embedding 模型/维度会发生什么？** 新旧向量不在同一空间（实测同文本自相似度仅 0.70、检索 top sim ~0.27），rag_hits 全空。必须按 doc_name 覆盖重传全部文档。维度在建表时固定，改维度需重建表。
4. **相似度阈值/top_k 怎么调？** 用评估集回归：threshold 过高召回不足（rag_hits 空 → 转人工话术），过低引入噪声干扰生成；当前 0.5~0.6 + top_k 4~5 是评估实验调出来的。
5. **多租户在检索层怎么隔离？** `search()` 强制 `filter={"tenant_id": ...}`，tenant_id 只能来自鉴权后的 Actor，客户端无法指定；越权检索返回 0 条。
6. **文档更新怎么保证一致性？** 同名覆盖式重建（先删后写）+ uuid5 确定性 ID；chunk_index 记录写入顺序，文档内容读取按序拼接。
7. **为什么选 PGVectorStore 而不是独立向量库（Milvus/Qdrant）？** 演示项目数据量小（每租户几十~几百 chunk），pgvector 与业务库同实例，少一个组件、事务/备份一致；tenant_id 等元数据是真实列，隔离过滤直接走 SQL，工程上最简。
8. **chunk overlap 为什么有用？** 等长切分时答案横跨边界，overlap 80 字符保证上下文窗口 continuity，降低切断关键句的概率。

---

## 6. 多租户隔离实现

### 6.1 四层隔离

```
① 网关层：ActorMiddleware（auth.py）
   - 解析 JWT → claims；X-Tenant-Id 请求头必须与 claims.tenant_id 完全一致，否则 401
   - Actor(actor_id, tenant_id, role) 写入 ContextVar，业务层 Depends 注入

② API/仓储层：所有查询强制 tenant_id 过滤
   - consumer 只能访问自己的订单/会话；staff/admin 限同租户
   - 跨租户/不存在/无权限 → 统一 ResourceNotFound（404），不泄露资源存在性
   - thread_id 格式 {tenant_id}:{uuid}，前缀校验 + 归属校验

③ Agent/工具层：身份经 context 注入，LLM 无法伪造
   - 工具签名无 tenant_id 参数，全部从 ToolRuntime.context（AgentRunContext）取
   - 治理中间件写审计/幂等记录时 tenant 也取自 context

④ 向量检索层：search() 强制 filter={"tenant_id": ...}
   - 知识库上传仅同租户 staff/admin；检索越权返回 0 条
```

### 6.2 配套设计

- **service_actor**：Agent 内部写消息/标转人工用同租户 STAFF 系统账号（`get_or_create_system_staff`），绕开 consumer 不能写 agent 消息的仓储限制，同时保持租户边界。
- **数据模型**：所有业务表（orders/products/conversations/knowledge_vectors/tool_audit_logs/idempotency_records）都有 tenant_id 列；订单 UNIQUE(tenant_id, order_no)、商品 UNIQUE(tenant_id, sku_code)。
- **演示账号**：三租户各有 consumer/staff/admin demo token，前端 IdentitySwitcher 一键切换身份演示隔离效果。

### 6.3 面试可能的问题

1. **为什么越权返回 404 而不是 403？** 403 会泄露"资源存在但不属于你"这一信息；404 对攻击者不区分"不存在"和"不属于你"，是安全最佳实践（IDOR 防护）。
2. **如果 LLM 在工具参数里传了别的 tenant_id 会怎样？** 不可能生效——工具签名里根本没有 tenant 参数，租户只从服务端注入的 context 取。这是"不信任模型输出"的边界设计。
3. **共享库 vs 独立库/schema 怎么权衡？** 演示项目选共享库 + tenant_id 列（成本最低、迁移简单）；生产上若租户体量差异大或有合规要求，可演进到 schema 级/库级隔离，当前仓储层过滤的写法不变。
4. **租户隔离的测试怎么做的？** E2E 冒烟里有跨租户 404 用例；仓储层单测覆盖 consumer/staff 可见性矩阵。
5. **ContextVar 传身份有什么好处？** 中间件一次解析，全链路（日志、仓储、工具）隐式可得；异步安全（每个请求独立 context）；单测可 monkeypatch。

---

## 7. LangSmith 评估测试

### 7.1 体系结构

```
backend/app/evals/
  cases/            本地 golden 用例（intent.json / knowledge.json / task.json，
                    50~80 条，按 intent/knowledge/policy/task/handoff/safety tag 分组）
  schema.py         EvalCase：tenant_id + message + expected（intent/hint/tool/
                    require_confirmation/must_contain/reference_answer...）+ tags
  dataset.py        用例 → LangSmith Dataset 同步（按 metadata.case_id diff：
                    新增/变更重建/过期删除）
  target.py         aevaluate 的 target：每用例建临时线程，消费
                    facade.astream_events 收集 final_reply/tool_executions/
                    rag_hits/confirmation_required
  evaluators.py     5 个评估器（见下）
  judge.py          LLM-as-judge 客户端（模型/key/base_url 可配置，默认继承主模型）
  runner.py         编排 + 进度 + 实验查询（TTL 缓存）
```

### 7.2 评估器设计（确定性 + LLM-as-judge 分工）

| 评估器 | 类型 | 判定内容 |
| --- | --- | --- |
| intent_match | 确定性 | 意图大类 + hint 与期望一致 |
| task_tool_match | 确定性 | 选对工具且成功；写操作必须产生 confirmation_required 暂停且 pending 工具/order_id 正确 |
| answer_contains | 确定性 | 回复必须包含/禁止包含的关键词 |
| knowledge_correctness | LLM judge | 对照参考答案评 0~1（要点完整、无矛盾） |
| faithfulness | LLM judge | 回复结论必须能被 rag_hits 支撑，无片段外编造 |

确定性规则能判的绝不用 judge（便宜、稳定、可复现）；开放性质量（回答好不好、有没有编造）才交给 judge，judge temperature=0 + 强 JSON 约束 + 1 次重试。

### 7.3 运行与查询

- **手动触发**：`POST /api/evaluations/run`（仅 ADMIN），模块级 asyncio 锁保证单实验并发；`aevaluate(blocking=False)` 异步执行，逐行计数暴露进度（`/status` 轮询）。
- **写操作安全**：评估 target 遇到 confirmation_required 即终止该用例——不 resume、不真正写库，既能验证"正确发起了确认"，又保证评估可重复执行不产生脏数据。
- **实验查询优化**（真实踩坑）：列表页原来对每个实验调一次 list_runs 取 URL（N+1，11 个实验 29.6s）且 LangSmith 同步 SDK 阻塞事件循环 → 改为 `project.url` 零请求取 URL + 模块级 TTL 缓存（列表 30s/详情 15s）+ `asyncio.to_thread` 包裹 → 首屏 1.7s。
- **反馈聚合**：不直接用 LangSmith 快照（实验刚结束时 judge 反馈未物化、avg=null 导致前端误显示"-"），改为从 list_feedback 逐用例实时聚合。
- **Tracing**：图执行挂载 LangChainTracer（按 env 开关），metadata 带 tenant_id/thread_id/actor_id，LangSmith UI 可按租户/会话过滤每条 Trace；评估页可跳转每个用例的完整 Trace。

### 7.4 面试可能的问题

1. **为什么评估要手动触发而不是定时跑？** 评估消耗 LLM token（有成本）且针对 golden set，价值在"变更后回归"而非持续监控；手动触发 + tag 过滤（只跑 rag 相关用例）更灵活。
2. **golden 数据集怎么版本管理？** 用例文件在 Git 里版本化，启动实验时按 case_id diff 同步到 LangSmith Dataset（变更的 example 删除重建），保证实验可复现、可对比。
3. **LLM-as-judge 可信吗？怎么防 judge 幻觉？** judge 只用于有明确参照的任务（对照参考答案 / 对照检索片段），prompt 强约束 JSON + temperature=0 + 解析失败重试；judge 模型可配置（默认同主模型，可换更强模型）；确定性指标仍是主干。
4. **评估写操作会不会污染数据？** 不会——target 消费到 confirmation_required 就停，不 resume；临时线程独立创建；评估用固定 demo 消费者账号。
5. **怎么定位一个失败用例？** 实验详情页 → 用例行 → 跳转 LangSmith Trace，逐节点看 intent 分类、工具调用参数、rag_hits、policy_decision、最终回复；node_errors 也在 debug 面板。
6. **并发与锁**：模块级 asyncio.Lock 防止多实验并发（共享 demo 数据 + 避免 token 爆量）；target 内每用例独立 DB session/线程，用例间隔离。
7. **指标体系怎么选的？** 对应系统核心风险：意图错（intent_match）、工具乱调（task_tool_match）、写操作不确认（require_confirmation）、答非所问（knowledge_correctness）、RAG 编造（faithfulness）、敏感词/缺关键信息（answer_contains）。

---

## 附：目录速查

```
backend/app/
  api/agent.py                    SSE/run/confirm 三个端点 + SSE 帧组装
  application/agent/graph.py      StateGraph 拓扑 + astream_unified 事件协议
  application/agent/nodes.py      6 个节点实现 + 合规规则
  application/agent/facade.py     对外门面：invoke/astream_events/resume_stream
  application/agent/task_agent.py create_agent ReAct 子图 + 动态 prompt + 空响应重试
  application/agent/governance.py 工具治理中间件（审计/幂等/防幻觉）
  application/tools/builtin.py    9 个 @tool（4 读 + 4 写 + policy_check）
  application/auth.py             ActorMiddleware 多租户鉴权
  infrastructure/vectorstore.py   PGVectorStore 封装（切片/检索/文档管理）
  infrastructure/agent/checkpoint.py  Redis checkpointer 工厂（TTL 600s）
  evals/                          LangSmith 离线评估（cases/dataset/target/evaluators/runner）
frontend/src/
  lib/sse-client.ts               fetch + ReadableStream SSE 客户端
  App.tsx                         聊天页 + 知识库管理 + 评估页
```

# LangChain/LangGraph 1.x 原生化重构方案

## 1. 目标与边界

**目标**：在保持现有功能完全不变的前提下，将自造的工具基座、Agent 装配、事件流等代码替换为 LangChain/LangGraph 1.x 原生能力，核心入口由 `langgraph.prebuilt.create_react_agent` 升级为 `langchain.agents.create_agent`，工具全部使用 `@tool`。

**保持不变（功能基线）**：

- 四分类拓扑：`intent_classify →（simple_qa / handoff / knowledge_qa: policy_lookup→rag_retrieve / task）→ compliance_check`。
- HITL：写操作必须页面确认，10 分钟超时，暂停态 Redis 持久化；确认/拒绝/超时三条路径。
- 工具审计（`tool_audit_logs`）与幂等（`idempotency_records`，24h TTL）语义。
- 向量库：`langchain_postgres.PGVectorStore`，表 `knowledge_vectors`，metadata 列 tenant_id/source/doc_name/title。
- 三租户硬隔离；工单号规则（RF-/EX-/RP-/CX-/HO-）与订单状态机。
- HTTP/SSE 接口、facade 事件协议（`node_start/node_end/reply_chunk/reply/confirmation_required/...`）、evals 手动触发与 ADMIN 限制。

**必须删除（符合 AGENTS.md「强依赖、不兜底」）**：离线 `MinimalStateGraph`、`HAS_LANGGRAPH/HAS_LANGSMITH` 空壳、provider 自造抽象层。

**明确不做**：不改前端；不改 DB 表结构、业务状态机与工单规则；不引入 langchain-classic；不把外层图整体塌缩成单个 agent；不新增任何兜底逻辑。

## 2. 调研结论

### 2.1 容器现状（实测 ai_cs_backend，Python 3.11.16）

| 包 | 当前版本 |
| --- | --- |
| langchain | 0.3.30 |
| langchain-core | 0.3.86 |
| langgraph | 0.6.11 |
| langgraph-checkpoint-redis | 0.3.6 |
| langchain-openai | 0.3.35 |
| langchain-postgres | 0.0.17 |
| langsmith | 0.13.0 |
| openai | 2.54.0 |

实测 `langchain.agents` 在 0.3.30 中**没有 `create_agent`**；它是 langchain 1.0（2025-10-22 发布）新增入口。

### 2.2 1.x 目标版本与兼容矩阵（PyPI 实测）

| 包 | 目标版本 | 关键约束 |
| --- | --- | --- |
| langchain | 1.4.3 | core>=1.6.3；langgraph>=1.2.11,<1.3 |
| langchain-core | 1.6.6 | pydantic>=2.7.4 |
| langgraph | 1.2.12 | core>=1.4.7；checkpoint>=4.1.0；prebuilt>=1.1.0 |
| langgraph-checkpoint-redis | 0.5.2 | checkpoint>=4.1.1；redis>=5.2.1；redisvl>=0.15 |
| langchain-openai | 1.6.7 | core>=1.6.6；openai>=2.45,<4 |
| langchain-postgres | 0.0.18 | core>=1.2.11 |
| langsmith | 0.14.2 | — |

- 全部包要求 **Python>=3.10**：容器 [Dockerfile](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/Dockerfile#L1) 已为 `python:3.11-slim`，无需改镜像；本地 `backend/.venv` 为 3.9，需用 3.11 重建。
- Redis 镜像不变：`redis-stack-server` 已含 RedisJSON + RediSearch，满足 0.5.x 要求。
- openai SDK 容器已是 2.54.0；保持 `<4` 跟随 langchain-openai 解析即可。

### 2.3 1.x API 关键变化（官方迁移文档）

1. `langgraph.prebuilt.create_react_agent` → **`langchain.agents.create_agent`**；`prompt` → `system_prompt`；pre/post-model hook → middleware。
2. **运行期上下文原生化**：`invoke(input, context=...)` + `context_schema`；工具函数通过 `runtime: ToolRuntime[ContextT]` 取 context/state/store/stream_writer/tool_call_id，无需 `Annotated`。
3. 自定义 middleware 的 **`wrap_tool_call`** 包住每次工具调用，可返回 `Command` 更新状态。
4. create_agent 的 state 仅支持 **TypedDict**（现有 [AgentState](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/schemas/agent.py) 已是 TypedDict，天然兼容）。
5. 流式节点名 `"agent"` → `"model"`（仅影响 facade 内部映射）。
6. **Redis checkpoint 存储格式自 0.1.0 起 breaking**，旧 checkpoint 不可读；TTL 配置单位为**分钟**（现有 600 秒 → `default_ttl=10`）；AsyncRedisSaver 走 `from_conn_string` + `asetup()`。
7. middleware 与 create_agent 编译图可整体作为外层 StateGraph 的子图节点，context 与 hooks 自动传播——这是本方案「外层图不动、task 节点换原生 agent」的官方依据。

## 3. 目标架构

外层确定性图拓扑与节点（intent_classify / policy_lookup / rag_retrieve / handoff / compliance_check）保持不变；task 节点由「每次调用时现建 create_react_agent 的闭包」替换为「模块级 `create_agent` 编译图作为原生子图节点」。

```text
START
  → intent_classify ── intent_router ──┐
      simple_qa    → compliance_check
      handoff      → handoff → compliance_check
      knowledge_qa → policy_lookup ── knowledge_router ──┐
                       hit → compliance_check
                       miss → rag_retrieve → compliance_check
      task         → task_agent (create_agent 子图)
                       tools = 9 个模块级无状态 @tool
                       middleware = ToolGovernanceMiddleware + @dynamic_prompt
                       → compliance_check
  → compliance_check → END
```

关键替换关系：

| 现有实现 | 原生替代 |
| --- | --- |
| [GuardedTool](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/base.py) + model_copy + bind_runtime | 模块级无状态 `@tool` + `ToolRuntime` 注入 |
| [ToolRunner](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/runner.py) 8 步流水线 | `ToolGovernanceMiddleware.wrap_tool_call` |
| [RequestRuntime contextvar](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/runtime_context.py) | 原生 `context` / `AgentRunContext` |
| [build_task_react_subgraph](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/nodes.py#L453-L688) 闭包 | `create_agent` 编译图作为 task 节点 |
| [StreamableCompiledGraphWrapper](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/graph.py#L506) + NODE_STREAM 队列 | langgraph 原生多流模式 `stream_mode=["debug","updates","messages"]` |
| provider 抽象（as_runnable/as_chat_model） | `ChatOpenAI` / `OpenAIEmbeddings` 直连 |
| 手写 JSON 意图解析 | `chat_model.with_structured_output(...)` |
| 离线 MinimalStateGraph | 删除 |

## 4. 详细设计

### 4.1 依赖升级 — [pyproject.toml](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/pyproject.toml)

- `requires-python = ">=3.10"`。
- `llm` extra 改为：
  - `langchain>=1.4,<2.0`、`langchain-openai>=1.6,<2.0`
  - `langgraph>=1.2,<1.3`、`langgraph-checkpoint-redis>=0.5`
  - `langsmith>=0.14`、`openai>=2.45,<4`、`tenacity>=8.0`
- `vector` extra：`langchain-postgres>=0.0.18`（pgvector 保留）。

### 4.2 原生运行期上下文（新增 `app/application/agent/context.py`）

```python
@dataclass(frozen=True)
class AgentRunContext:
    actor: Actor
    tenant_id: str
    thread_id: str
    service_actor: Actor
    effective_policy: TenantPolicy
    idempotency_salt: str
    session: AsyncSession
    conversation_repo: ConversationRepository
```

- facade 的 `invoke / astream_events / resume_stream` 每次入口构造一次，随 `graph.ainvoke(..., context=ctx)` / `graph.astream(..., context=ctx)` 传入。
- resume 是新 HTTP 请求、携带新 session → context 天然拿到 fresh session，**contextvar 彻底删除**（[runtime_context.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/runtime_context.py) 整文件删除）。
- 身份字段只存在于 context，工具签名中没有 tenant_id/actor 参数 → 模型无从越权注入，`FORBIDDEN_OVERRIDE_KEYS / apply_trusted_actor_override / bind_runtime / model_copy` 全部删除。

### 4.3 工具层重写 — [builtin.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/builtin.py)

- 保留现有 9 个 Pydantic Args 类与全部业务逻辑，工具改为模块级异步 `@tool` 函数：
  - 读：`order_query`、`product_list`、`product_query`、`current_time`
  - 写：`refund_request`、`exchange_request`、`repair_request`、`cancel_order`
  - 虚拟转正：`policy_check`（仍不写库，调 `_decide_policy` 纯函数）
- 签名只有「业务参数 + `runtime: ToolRuntime[AgentRunContext]`」；session/repo/actor/policy 全部取自 `runtime.context`。
- 写工具保留现有完整流程：加载订单与政策 → `interrupt(pending)` → resume 值判定 → 状态流转与工单生成；规则与文案逐字移植。
- middleware 不吞异常，写工具不再需要「绕过 ToolRunner」的特判。
- 工具执行记录仍使用 [ToolResult](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/schemas/tools.py)（`tool_name/success/data/...` 字段结构不变），保证 debug 面板、evals、前端消费兼容。
- 删除 [base.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/base.py) 与 [runner.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/runner.py)。

### 4.4 治理中间件（新增 `app/application/agent/governance.py`）

`ToolGovernanceMiddleware(AgentMiddleware)` 通过 `wrap_tool_call(request, handler)` 承接现有 ToolRunner 的完整语义：

1. 用 `idempotency_salt + tool_name + 参数哈希` 计算幂等 key；
2. 查 `idempotency_records`：命中成功记录 → 直接回传缓存结果（保持现有命中语义）；
3. 写 `tool_audit_logs`（running）；
4. `await handler`：
   - `GraphInterrupt/GraphBubbleUp` → 直接放行（HITL 暂停，audit 保持 running）；
   - 其他异常 → audit 收尾失败态后**原样抛出，不兜底**；
5. 成功 → 写 `idempotency_records`（24h TTL）+ audit 完成态；
6. 返回 `Command(update=...)` 做状态字段映射（替代 task_node 里的 call_history 回填）：
   - `tool_executions`：追加（middleware state_schema 声明该字段及累加 reducer）；
   - `policy_check` 结果 → `policy_decision`；
   - order/product 读结果 → `order_detail_json`；
   - 写工具成功 → `action_kind`（refund/exchange/repair/cancel）、`action_result_json`，并追加 `role=tool` 会话消息；
   - 结果含 `accepted=False` → `action_kind="aborted"` + `final_reply=标准拒绝文案`（保持现有拒绝路径，绕过 LLM 改写）。

### 4.5 task agent — 新增 `app/application/agent/task_agent.py`

```python
def build_task_agent(chat_model: BaseChatModel):
    return create_agent(
        model=chat_model,
        tools=ALL_TOOLS,
        system_prompt=None,
        middleware=[ToolGovernanceMiddleware(), _task_dynamic_prompt],
    )
```

- 现有 `_build_agent_system_prompt`（按 tenant_id/intent_hint/rag_hits/order_ref/policy 动态拼装）用 1.x 的 `@dynamic_prompt` 包装，从 state 现取现拼，prompt 内容与硬约束逐字保留。
- `build_customer_service_graph` 中 `builder.add_node("task", build_task_agent(chat_model))`，编译图即原生子图节点；interrupt 自动向外传播，不再需要手动扫描 `__interrupt__` 和 re-raise。
- 会话历史注入上移到 facade：构造初始 state 时用 `conversation_repo.list_messages(limit=20)` 转为 Human/AIMessage 写入 `messages`，尾部追加当前 HumanMessage；其他分支不使用 messages，无影响。

### 4.6 图装配精简 — [graph.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/graph.py)

重写为约 150 行，只保留 `build_customer_service_graph(...)`：

- 删除：`HAS_LANGGRAPH/HAS_LANGSMITH` try-except、离线 `MinimalStateGraph`（约 190 行）、`StreamableCompiledGraphWrapper`（约 630 行）、`_NodeStreamCtx` / NODE_STREAM / `emit_*`、`_diff_patch`、自定义 tracer config 拼装。
- 返回原生 compiled StateGraph（checkpointer 仍由 [checkpoint.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/infrastructure/agent/checkpoint.py) 注入）。

### 4.7 facade 适配 — [facade.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/facade.py)

- 构造函数：删除 `tool_registry` 入参；`chat_model` 类型改为 `BaseChatModel`；自动构建分类器改为 `LLMIntentClassifier(chat_model=chat_model)`。
- 三个公共方法签名与**对外事件协议完全不变**（SSE 层与 evals target 零改动）。
- 内部流式切换为原生单路多模式：

```python
async for mode, chunk in graph.astream(
    input_, stream_mode=["debug", "updates", "messages"], context=ctx, config=...
):
    ...
```

  - `debug` 模式的任务启动事件 → `node_start`；
  - `updates` 的 `{node: patch}` → `node_end`；
  - `messages` 的 `(AIMessageChunk, metadata)` → `reply_chunk`。
  - 三种事件的实际数据形状在实施第一步于容器实测固化（见第 6 节 Step 1）。
- pending 检测（`aget_state` + `tasks[].interrupts[].value`）、reply 选优（final_state + chunk 累计）逻辑保持不变。
- `_bind_node_contexts` 去掉 NODE_STREAM 旁路；保留现有节点异常处理（node_errors + 安全草稿是既有业务行为，GraphInterrupt 放行）。

### 4.8 provider / classifier / vectorstore / main 装配

- [providers.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/infrastructure/llm/providers.py)：删除 `BaseChatModelProvider/BaseEmbeddingProvider/OpenAI*` 及工厂（约 450 行）；`BaseRetriever` 领域端口保留。
- [main.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/main.py#L140-L225)：直接构建 `ChatOpenAI`（model/base_url/api_key/temperature）与 `OpenAIEmbeddings`（model，1536 维）；删除 `build_default_registry()`，facade 装配改为新构造函数。
- [classifiers.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/infrastructure/llm/classifiers.py)：`LLMIntentClassifier` 改为 `chat_model.with_structured_output(IntentCandidate)`；8s 超时用 `with_config(timeout=...)`；输入输出协议与失败行为不变。
- [vectorstore.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/infrastructure/vectorstore.py)：`KnowledgeVectorStore` 直接接收 LangChain `Embeddings`，删除 `LangChainEmbeddingsAdapter`；PGVectorStore 用法与 `PgVectorStoreRetriever` 不动。
- [api/tools.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/api/tools.py)：工具定义列表改为从原生工具对象（`.name/.description/get_input_schema()`）生成；[api/agent.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/api/agent.py#L183) 内的 facade 构造同步更新。
- [checkpoint.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/infrastructure/agent/checkpoint.py#L42-L49)：适配 0.5.2 API（`AsyncRedisSaver.from_conn_string` + `asetup`，TTL `default_ttl=10` 分钟）。

## 5. 受影响文件清单

| 文件 | 变更 |
| --- | --- |
| [pyproject.toml](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/pyproject.toml) | 版本与 Python 要求 |
| `app/application/agent/context.py` | **新增** AgentRunContext |
| `app/application/agent/governance.py` | **新增** ToolGovernanceMiddleware |
| `app/application/agent/task_agent.py` | **新增** create_agent 构建 |
| [graph.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/graph.py) | 大幅精简重写 |
| [facade.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/facade.py) | 构造函数 + 原生流 |
| [nodes.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/nodes.py) | 删除 task 子图闭包与历史加载（约 340 行），其余节点不动 |
| [builtin.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/builtin.py) | 重写为 @tool |
| tools/base.py、tools/runner.py | **删除** |
| agent/runtime_context.py | **删除** |
| [providers.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/infrastructure/llm/providers.py) | 删 provider 抽象，留 BaseRetriever |
| [classifiers.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/infrastructure/llm/classifiers.py) | with_structured_output |
| [vectorstore.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/infrastructure/vectorstore.py) | 删 embeddings adapter |
| [checkpoint.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/infrastructure/agent/checkpoint.py) | 适配 0.5.2 API |
| [main.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/main.py)、[api/tools.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/api/tools.py)、[api/agent.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/api/agent.py) | 装配适配 |
| [tests/unit/_fakes.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/tests/unit/_fakes.py) | 重写：FakeChatModel(BaseChatModel)、DeterministicFakeEmbeddings(Embeddings)，保留 FakeRetriever 与关键词规则 |
| [test_task4_tools.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/tests/unit/test_task4_tools.py) | 逐用例映射为 @tool + governance 测试；越权注入类用例转为结构性保证（删除） |
| [test_task7_graph.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/tests/unit/test_task7_graph.py)、[test_task76_classifiers.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/tests/unit/test_task76_classifiers.py)、[test_task5_rag.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/tests/unit/test_task5_rag.py)、[test_agent_refactor_ac.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/tests/unit/test_agent_refactor_ac.py) | 夹具与构造适配，断言不降级 |

## 6. 分步骤实施

| 步骤 | 内容 | 验证 |
| --- | --- | --- |
| 1 | pyproject 升级；重建本地 3.11 venv 与后端镜像；实测固化 `debug/updates/messages` 事件形状、AsyncRedisSaver 0.5.2 构造与 context 子图传播 | `import` 冒烟；pytest 记录基线（langgraph 1.x 官方称大体向后兼容，预期仅少量适配） |
| 2 | AgentRunContext + @tool 重写 + ToolGovernanceMiddleware；_fakes 重写；test_task4 等价重写 | tools/classifier 相关测试全绿 |
| 3 | task_agent.py（create_agent + dynamic_prompt）；task 节点替换；历史注入上移 facade | test_task7_graph、test_agent_refactor_ac 全绿 |
| 4 | graph.py 精简；facade 切换原生多流；删除 3 个文件 | `pytest` 全量 |
| 5 | providers/vectorstore/main/checkpoint 装配清理 | `pytest` 全量 + ruff |
| 6 | 重建镜像，端到端冒烟 | 见第 7 节 |

每步均执行：`cd backend && pytest`、`ruff check app tests`；容器代码无挂载，第 2~5 步通过 `docker compose up -d --build backend` 验证。

## 7. 验证策略

- 单测：`pytest` 全绿。已知既有失败 2 个（`test_unknown_route_is_404_unified`、`test_validation_error_format`，auth 中间件先拦截得 401）与本次无关，保持失败现状。
- lint：`ruff check app tests` 零告警。
- 容器：`docker compose up -d --build backend` 后 healthcheck 通过。
- 端到端（前端页面实操）：simple_qa / handoff / knowledge_qa / task 各一条；写操作确认通过、用户拒绝、10 分钟超时；跨租户互访被拒；评估手动触发一次；LangSmith trace 完整。
- 部署时清理旧格式 checkpoint（redis-stack 上删除 checkpoint 相关 key；其本身 10 分钟 TTL，影响仅限升级瞬间在途的确认）。

## 8. 风险与回退

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| Redis checkpoint 格式 breaking | 在途确认丢失 | 部署窗口清理 Redis；TTL 仅 10 分钟 |
| 1.x 内容块/事件形状差异 | facade 事件映射偏差 | Step 1 容器实测固化；SSE 全路径冒烟 |
| 测试夹具大改 | 掩盖真实回归 | 逐用例映射，断言不降级，不新增 skip |
| create_agent 子图 context 传播或 dynamic_prompt 与文档不符 | task 节点返工 | 局部退回 `create_react_agent`（langgraph 1.x 仍可用，仅 deprecation warning），不影响其余步骤 |
| 主版本升级面大 | 问题定位难 | 依赖升级独立为 Step 1 先行；全程可按步骤 git 回退 |

回退路径：git revert 至重构前提交，依赖 pin 回 0.3.x；Redis 旧数据随 TTL 自然清理。

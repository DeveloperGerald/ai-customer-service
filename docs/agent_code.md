# Agent 代码脉络梳理

> 范围：从用户在前端发送一条消息开始，到用户收到最终自然语言回复（或确认卡片/转人工卡片）结束。
> 目标：告诉你「什么时候调用什么方法/函数」，把整条链路打通。细节直接看对应文件即可。

---

## 1. 架构总览

后端 Agent 模块基于 **LangGraph StateGraph**，采用 **4 分类意图 + 政策优先 + ReAct 子图 + 统一合规** 的拓扑：

```
START
  → intent_classify ── intent_router (4 路) ──┐
      simple_qa    → compliance_check
      handoff      → handoff → compliance_check
      knowledge_qa → policy_lookup ── knowledge_router ──┐
                       hit  → compliance_check
                       miss → rag_retrieve → compliance_check
      task         → task (create_react_agent 子图，写工具内置 interrupt HITL)
                       → compliance_check
  → compliance_check (规则合规 + LLM 包装) → END
```

四个意图分类由 [LLMIntentClassifier](backend/app/infrastructure/llm/classifiers.py) 输出：
- `simple_qa` — 闲聊/引导，直接交 LLM
- `handoff` — 明确转人工
- `knowledge_qa` — 政策/FAQ 检索（先政策字段，未命中再 RAG）
- `task` — 订单/退换修/取消等需要工具调用的写操作

**所有分支最终汇总到 `compliance_check` 节点**，由它做规则合规 + LLM 包装后产出 `final_reply`，再回写 DB 一条 agent 消息。

---

## 2. 关键文件清单

| 层级 | 文件 | 作用 |
|------|------|------|
| HTTP 入口 | [backend/app/api/agent.py](backend/app/api/agent.py) | SSE 流式端点 + HITL confirm 端点 |
| Facade | [backend/app/application/agent/facade.py](backend/app/application/agent/facade.py) | 对外唯一入口 `CustomerServiceAgentFacade` |
| 图构建 | [backend/app/application/agent/graph.py](backend/app/application/agent/graph.py) | `build_customer_service_graph` + `StreamableCompiledGraphWrapper` |
| 节点 | [backend/app/application/agent/nodes.py](backend/app/application/agent/nodes.py) | 6 个节点函数 + 2 个 router + ReAct 子图构造 |
| 运行时上下文 | [backend/app/application/agent/runtime_context.py](backend/app/application/agent/runtime_context.py) | `RequestRuntime` contextvar（写工具 HITL resume 取 fresh session） |
| Checkpointer | [backend/app/infrastructure/agent/checkpoint.py](backend/app/infrastructure/agent/checkpoint.py) | Redis（`AsyncRedisSaver`，TTL 600s）/ Memory 兜底 |
| 工具 | [backend/app/application/tools/builtin.py](backend/app/application/tools/builtin.py) | `order_query`/`policy_check`/`refund_request`/`exchange_request`/... |
| State Schema | [backend/app/application/schemas/agent.py](backend/app/application/schemas/agent.py) | `AgentState` TypedDict + `AgentRunResult` |

---

## 3. 主流程：一条消息的完整生命周期

### Step 1：HTTP 入口（前端 → 后端）

前端发请求到：

```
POST /api/conversations/{thread_id}/stream
```

- 路由定义：[agent.py:621 `agent_invoke_stream`](backend/app/api/agent.py#L621)
- 鉴权：`Depends(require_actor)` 解析 JWT 得到 `Actor`（consumer/staff/admin）
- 会话：`Depends(_session)` 注入 `AsyncSession`
- 处理函数：把请求体包装成 `StreamingResponse`，生成器是 [_stream_agent_run](backend/app/api/agent.py#L344)
- 关键 SSE Headers（强制 flush，避免代理攒包破坏打字机）：
  ```
  Cache-Control: no-cache, no-store, must-revalidate, no-transform, private
  X-Accel-Buffering: no
  ```

### Step 2：SSE 生成器 _stream_agent_run

文件 [agent.py:344](backend/app/api/agent.py#L344)。

1. 先发 `start` 帧（含 `thread_id` / `user_input`）
2. 调用 `facade.astream_events(...)`，拿到事件迭代器
3. 遍历事件，按 `evt["type"]` 翻译成 SSE 帧：
   - `node_start` → `event: node` (phase=start)
   - `node_end` → `event: node` (phase=end, has_patch, has_error)
   - `tool` → `event: tool` (kind, payload)
   - `reply_chunk` → `event: reply_chunk` (text) ← 打字机增量
   - `escalated` → `event: escalated` (ticket_no, reason) ← 转人工卡片
   - `confirmation_required` → `event: confirmation_required` ← HITL 卡片，本轮不发 reply/done
   - `reply` → `event: reply` ← 完整回复（兼容旧前端）
   - `debug` → `event: debug` ← 决策依据面板
   - `done` → `event: done` + `data: [DONE]`
4. 每帧后追加 `_SSE_FLUSH_HINT`（`: sse-flush\n\n`）强制反向代理立即 flush

### Step 3：Facade.astream_events（核心调度入口）

文件 [facade.py:241 `astream_events`](backend/app/application/agent/facade.py#L241)。

调用链：

1. **初始化依赖**：
   - 从 DB 查/建 `service_actor`（同租户 STAFF）：[facade.py:270](backend/app/application/agent/facade.py#L270)
   - 从 DB 查 `effective_policy`（`TenantPolicy`）：[facade.py:280](backend/app/application/agent/facade.py#L280)
2. **构建节点上下文** `AgentNodeContext`：[facade.py:282](backend/app/application/agent/facade.py#L282)
   - 装配 `session / actor / tool_registry / retriever / conversation_repo / classifier / chat_model / rag_top_k / effective_policy`
3. **先写用户消息**到 conversation（失败 non-fatal）：[facade.py:300](backend/app/application/agent/facade.py#L300)
4. **绑定节点函数** `_bind_node_contexts(ctx)`：[facade.py:317 → L652](backend/app/application/agent/facade.py#L652)
   - 用 `functools.partial` 把 `ctx` 绑到每个节点函数
   - 外面再包一层 `_trace` wrapper：emit `node_start`/`node_end` + 异常兜底（非 `GraphInterrupt` 异常 → 写 `node_errors` + 返回安全草稿）
5. **构建图** `build_customer_service_graph(...)`：[facade.py:318](backend/app/application/agent/facade.py#L318)
6. **构建 initial state** `_fresh_turn_state(...)`：[facade.py:323 → L49](backend/app/application/agent/facade.py#L49)
   - 关键：每轮显式清空「输出型」字段（`final_reply`/`draft_reply`/`action_kind`...），避免上一轮 checkpointer 残留导致短路
   - 保留「上下文型」字段（`order_detail_json`/`rag_hits`/`tool_executions`...）跨轮积累
7. **设置 contextvar** `RequestRuntime`：[facade.py:354](backend/app/application/agent/facade.py#L354)
   - 写工具 `_arun` 通过 `get_current_request()` 拿 fresh session/actor
8. **yield start 事件**，然后调 `graph.astream_events(initial, ...)` 拿事件迭代器
9. **遍历事件** → 透传 yield 给 API 层
   - `token_chunk` 同时本地累计（`_acc_chunks`）做双保险，防止 final_state.patch 丢了 final_reply 时还能用打字机拼起来的文本兜底
10. **HITL 检测**：[facade.py:461 `_detect_interrupt_pending`](backend/app/application/agent/facade.py#L461)
    - 调 `graph.get_state(config)` → `StateSnapshot`
    - 若 `.next` 非空 → 从 `.tasks[*].interrupts[*].value` 取 pending payload
    - 有 pending → yield `confirmation_required` + return（**本轮不 yield reply/done**）
11. **最终收尾**：
    - 若 `escalated=True` → yield `escalated` 卡片
    - 选优 final_reply（`final_state.final_reply` vs 本地 `_acc_chunks` 拼接）→ yield `reply`
    - yield `debug` + `done`
12. **finally** `reset_current_request(token)`

### Step 4：图构建 build_customer_service_graph

文件 [graph.py:1139](backend/app/application/agent/graph.py#L1139)。

1. `StateGraph(AgentState)` 实例化 builder
2. `add_node` 6 个节点（从 `node_fns` dict 取）：`intent_classify / policy_lookup / rag_retrieve / task / handoff / compliance_check`
3. `add_edge(START, "intent_classify")`
4. `add_conditional_edges("intent_classify", intent_router)` ← 4 路分流
5. `add_conditional_edges("policy_lookup", knowledge_router)` ← policy_lookup 命中/未命中
6. 4 条汇聚边：
   - `handoff → compliance_check`
   - `rag_retrieve → compliance_check`
   - `task → compliance_check`
   - （simple_qa 直接路由到 `compliance_check`，不需要单独边）
7. `add_edge("compliance_check", END)`
8. **注入 checkpointer**：[graph.py:1196](backend/app/application/agent/graph.py#L1196)
   - `compile_kwargs["checkpointer"] = get_checkpointer()`（仅真 LangGraph 路径）
9. `StreamableCompiledGraphWrapper(inner)` 包装 → 统一对外暴露 `ainvoke` / `astream_events` / `get_state`

### Step 5：intent_classify 节点

文件 [nodes.py:118 `intent_classify_node`](backend/app/application/agent/nodes.py#L118)。

- 调 `ctx.classifier.aclassify(text, tenant_id, thread_id)`
- 输出 3 字段：`intent_candidate`（4 选 1）/ `order_ref_candidate`（订单号候选）/ `intent_hint`（refund/exchange/repair/cancel/order_status/product）
- classifier 为 None 或非法值 → 兜底 `simple_qa`

### Step 6：intent_router 条件边

文件 [nodes.py:145 `intent_router`](backend/app/application/agent/nodes.py#L145)。

```
simple_qa    → compliance_check
handoff      → handoff
knowledge_qa → policy_lookup
task         → task
```

---

## 4. 四大分支详解

### 4.1 simple_qa（直接交 LLM）

路由：`intent_classify → compliance_check`（无中间节点）

- `intent_classify` 不产出 `draft_reply`
- 进入 `compliance_check` 时 `draft_reply` 为空 → `draft_was_empty=True`
- `compliance_check_node` 内部：[nodes.py:654](backend/app/application/agent/nodes.py#L654)
  - `draft_reply` 空 + `draft_was_empty=True` + 非 error_draft → 走 LLM 直答路径
  - `chat_model.as_runnable().astream(...)` 流式产出 token
  - 每个 chunk 通过 `node_stream_emit_token(chunk, stream_chunks)` 旁路 emit → 包装层转成 `token_chunk` 事件 → 前端打字机
  - LLM 结果再过一次 `_apply_compliance_rules` 做规则合规

### 4.2 handoff（转人工）

节点 [nodes.py:290 `handoff_node`](backend/app/application/agent/nodes.py#L290)。

- 生成工单号 `HO-{TENANT}-{8hex}`
- 调 `conversation_repo.mark_escalated(...)` 把会话标记为已转人工
- 产出 `draft_reply = "已为您转接人工客服。工单号：xxx。"`
- 产出 `escalated=True` / `escalated_ticket_no` / `escalation_reason`
- 进入 `compliance_check`：
  - 上游 `final_reply` 未预设 → 走 LLM 包装路径，把 `escalated=True` 注入 `extra_context` 让 LLM 按系统提示词第 1 条输出转人工文案
- Facade 末尾检测 `escalated=True` → yield `escalated` 卡片事件（前端弹「转人工」卡片）

### 4.3 knowledge_qa（政策优先 → RAG 兜底）

两段子图：`policy_lookup` → `knowledge_router` → `rag_retrieve`（未命中时）→ `compliance_check`

#### 4.3.1 policy_lookup_node

文件 [nodes.py:168 `policy_lookup_node`](backend/app/application/agent/nodes.py#L168)。

1. 取 `ctx.effective_policy`（DB 查到的租户政策结构化字段：return_days / restocking_fee_pct_non_quality / warranty_days_quality / brand_name ...）
2. 构造判断 prompt，调 `ctx.chat_model.as_runnable().ainvoke(...)` 让 LLM 判断「能否用政策字段直接回答」
3. 能 → 输出 `policy_lookup_hit=True` + `policy_answer` + `draft_reply=answer_text` + `draft_context={"source":"policy"}`
4. 不能 / LLM 失败 → 输出 `policy_lookup_hit=False` → 走 RAG

#### 4.3.2 knowledge_router

文件 [nodes.py:243 `knowledge_router`](backend/app/application/agent/nodes.py#L243)。

```
policy_lookup_hit == True  → compliance_check（直接用政策字段作答）
policy_lookup_hit == False → rag_retrieve
```

#### 4.3.3 rag_retrieve_node

文件 [nodes.py:254 `rag_retrieve_node`](backend/app/application/agent/nodes.py#L254)。

- 调 `ctx.retriever.retrieve(tenant_id, query, top_k=4, similarity_threshold=0.5)`
- retriever 硬约束：必须带 `tenant_id` 过滤（多租户隔离，由 retriever 内部保证）
- 输出 `rag_hits` 列表 + `draft_context={"source":"rag"}`
- 注意：此节点**不产出 draft_reply**，留给 `compliance_check` 用 RAG 内容让 LLM 组织答案

#### 4.3.4 进入 compliance_check

- 政策命中：`draft_reply` 已有政策答案 + `draft_context.source="policy"` → LLM 润色 + 加合规约束（基于政策字段，不得编造政策外权益）
- RAG 命中：`draft_reply` 空 + `draft_context.source="rag"` → LLM 用 RAG 片段组织答案 + 合规约束（必须遵循 RAG 内容，不得编造未检索信息）

### 4.4 task（ReAct 子图 + 工具 + HITL）

构造函数 [nodes.py:380 `build_task_react_subgraph`](backend/app/application/agent/nodes.py#L380)，返回 `task_node` async 函数。

#### 4.4.1 子图组装

1. **构建工具集** `build_langchain_tools(adapter_ctx)`：
   - 只读：`order_query` / `product_list` / `product_query` / `policy_check`
   - 写：`refund_request` / `exchange_request` / `repair_request` / `cancel_order`（内置 `interrupt`）
2. **构建 system prompt** `_build_agent_system_prompt(...)`：[nodes.py:315](backend/app/application/agent/nodes.py#L315)
   - 硬约束调用顺序：`order_query` → `policy_check` → 写工具
   - 写工具会先 interrupt 等用户确认
3. **加载对话历史**（最近 20 条，跳过 tool 消息）→ 让 ReAct agent 能引用上一轮工具结果（如「都需要」追问）
4. **创建 ReAct agent**：`_lc_create_react_agent(llm_runnable, tools)` ← langchain 预置
5. **执行** `agent_graph.ainvoke({"messages": ...}, config={"recursion_limit": 15})` ← 最多 5 轮工具调用

#### 4.4.2 结果回填

[nodes.py:470-545](backend/app/application/agent/nodes.py#L470)：

- 遍历 `adapter_ctx.call_history` 提取：
  - `tool_executions` — 所有工具调用记录
  - `policy_decision` — 来自 `policy_check` 的 `PolicyDecision`
  - `order_detail_json` — 来自 `order_query`/`product_query`
  - `action_result_json` — 来自写工具（ticket_no 等）
- 推断 `action_kind`：refund/exchange/repair/cancel（按写工具名映射）
- 写工具成功 → 追加一条 `role=tool` 的 conversation message（审计追溯）
- `draft_reply` = ReAct 最终 AIMessage 内容
- 把子图 messages 写回外层 state（compliance 可读）

#### 4.4.3 HITL 触发点

写工具（如 `ExchangeRequestTool._arun`）执行流程 [builtin.py:292](backend/app/application/tools/builtin.py#L292)：

1. 参数校验 + 加载订单（`_aload_order_for_write`，跨租户/越权统一 404）
2. 调 `_decide_policy_for_write(...)` 做政策判定
3. 政策不允许 → `ToolExecutionError(REFUND_NOT_ELIGIBLE)`
4. 政策允许 → 构造 pending payload（`_build_pending`，含 `tool/args/summary/expires_at/order_no`）
5. **调 `interrupt(pending)`** ← LangGraph 暂停信号，子图传播到外层图，checkpointer 落 Redis
6. **节点从头重新执行**（LangGraph resume 行为）→ 重新走 1-5，第二次到 `interrupt` 时返回 `resume_value`
7. `_parse_resume_value(resume_value)` → 检查 `confirmed` 字段：
   - `True` → 生成工单号 `EX-{TENANT}-{8hex}`，返回成功 JSON
   - `False` → 返回 `accepted=False` + reason

**关键：写工具必须 override `_arun` 绕过 `ToolRunner`**（ToolRunner 的 `except Exception` 会吞掉 `GraphInterrupt`）。通过 `get_current_request()` 拿当前请求的 fresh session/actor，因为 resume 时节点重跑，原 invoke 的 session 已随旧 HTTP 请求关闭。

---

## 5. HITL 人在回路完整流程

### 5.1 暂停阶段（invoke 内）

1. task 子图执行到写工具 `_arun` → 调 `interrupt(pending)`
2. LangGraph 子图暂停 → 传播到外层图暂停
3. checkpointer 把整个 state 落 Redis（key = `thread_id`，TTL=600s）
4. `facade.astream_events` 收到图执行结束（非正常 done，是暂停结束）
5. **检测暂停态** `_detect_interrupt_pending`：[facade.py:461](backend/app/application/agent/facade.py#L461)
   - `graph.get_state(config)` → `StateSnapshot`
   - `.next` 非空 = 暂停态
   - 从 `.tasks[*].interrupts[*].value` 取 pending payload
6. yield `confirmation_required` 事件（含 `pending_action`）→ API 层翻译成 SSE 帧 → 前端弹确认卡片
7. **本轮不 yield reply/done**（前端卡片在等待用户点击）

### 5.2 恢复阶段（用户点击确认后）

前端发请求到：

```
POST /api/conversations/{thread_id}/actions/confirm
Body: { "decision": true|false, "reason": "..." }
```

- 路由定义：[agent.py:826 `agent_confirm_action`](backend/app/api/agent.py#L826)
- 调用 `facade.resume_stream(decision={"confirmed": ..., "reason": ...})`
- SSE 生成器：[_stream_agent_resume](backend/app/api/agent.py#L674)

#### Facade.resume_stream

文件 [facade.py:508 `resume_stream`](backend/app/application/agent/facade.py#L508)。

1. **先校验仍处于暂停态**：[facade.py:571](backend/app/application/agent/facade.py#L571)
   - `_detect_interrupt_pending` 返回 None（已超时/已处理）→ yield `error` + `done` 返回
2. yield `resume_started` 事件
3. 设置 `RequestRuntime` contextvar（**关键：resume 也要重新 set fresh session**）
4. **构造 Command**：`Command(resume=decision)` ← LangGraph 的 resume 信号
5. `graph.astream_events(command, thread_id=thread_id, ...)` 续跑
6. 事件透传：`node_start` / `node_end` / `reply_chunk` / `reply` / `escalated` / `debug` / `done`
7. finally `reset_current_request(token)`

### 5.3 续跑执行路径

LangGraph 从 checkpoint 恢复 state，重入 task 子图，从暂停的写工具节点重跑：

1. 写工具 `_arun` 重新执行（参数校验 → 加载订单 → policy check）
2. 到 `interrupt(pending)` 时，`pending` 不再触发暂停，而是返回上次 `Command(resume=...)` 传入的 `decision` dict
3. 根据 `decision.confirmed` 走确认/取消分支
4. 写工具返回 → ReAct agent 收到 ToolMessage → 决定是否继续调用其他工具或输出 Final Answer
5. 子图结束 → 外层图 `task → compliance_check → END`
6. `compliance_check` 产出 `final_reply`（带工单号 + 政策原因）

### 5.4 超时/已处理

- checkpointer TTL=600s（10min）→ Redis key 过期 → `get_state` 返回空/`.next` 空 → `_detect_interrupt_pending` 返回 None
- 用户重复点 confirm → 第二次 resume 时已无 pending → yield `error` + `done`

---

## 6. compliance_check 汇总节点

文件 [nodes.py:569 `compliance_check_node`](backend/app/application/agent/nodes.py#L569)。

所有 4 条分支最终都到这里。处理优先级：

1. **上游已写 final_reply**（handoff 预设文案 / 异常兜底草稿）→ 直通，只追加 agent 消息到 DB
2. **取 draft_reply**：优先从 state.draft_reply，否则从 `messages[-1].content`（task 子图最终答复）
3. **规则合规修整** `_apply_compliance_rules`：[nodes.py:780](backend/app/application/agent/nodes.py#L780)
   - 屏蔽 tenant_id 泄露（替换为 `***`）
   - 工单号一致性（action_result 有 ticket_no → draft 必须包含）
   - 写操作无 policy_decision.get(f"can_{action_kind}") 不得宣称「成功」
4. **LLM 包装**（若 chat_model 可用 + 非空草稿 + 非错误草稿）：
   - 拼 system prompt（`_build_compliance_prompt`，按 `draft_context.source` 加硬约束）
   - 加载最近 40 条对话历史（跳过 tool 消息）
   - 构造 `extra_context`：action_kind / ticket_no / policy_decision / action_result / order_detail / rag_hits / escalated
   - `runnable.astream(...)` 流式产出 token → 每块 `node_stream_emit_token(chunk, stream_chunks)` 旁路 emit
   - LLM 结果再过一次合规规则
5. **空草稿兜底**：`"抱歉，处理出现问题，请稍后重试或回复「人工」转人工。"`
6. **写 DB**：`conversation_repo.append_message(role="agent", content=final_reply, metadata={action_kind, ticket_no, policy_reason_code})`
7. 返回 `{final_reply, _stream_chunks: [], appended_messages: [...]}`

---

## 7. 流式与状态机制（幕后）

### 7.1 NODE_STREAM 旁路通道

文件 [graph.py:57 `_NodeStreamCtx`](backend/app/application/agent/graph.py#L57)。

**问题**：真 LangGraph 的 `StateGraph` 不支持节点函数返回 `AsyncIterator`，会丢弃节点内的 token 级流式 yield。

**方案**：
- 节点执行期间通过 `NODE_STREAM` contextvar 把 token / patch / node_start / node_end 写入请求专属 `asyncio.Queue`
- `StreamableCompiledGraphWrapper.astream_events` 在消费 LangGraph 原生 `astream_events(version="v2")` 事件流的同时，交错消费 NODE_STREAM queue
- 两条流合并成统一的 `token_chunk` / `node_start` / `node_end` 事件

关键函数：
- `node_stream_emit_token(text, snapshot)` — `compliance_check` 内 LLM 流式产出时调
- `node_stream_emit_node_start/end(name)` — `_bind_node_contexts` 的 `_trace` wrapper 调

### 7.2 StreamableCompiledGraphWrapper

文件 [graph.py:506](backend/app/application/agent/graph.py#L506)。

统一包装真 LangGraph 和 MinimalStateGraph 两种底层：
- 真 LangGraph：把原生 `on_chat_model_stream` 映射为 `token_chunk`，`on_chain_start/end` 映射为 `node_start/end`
- Minimal（离线/单测）：直接透传 Minimal 的 `astream_events`

关键：**token_chunk 唯一输出来源是 NODE_STREAM 旁路**，避免 wrapper 双重发射导致前端打字机「ItIt lookslooks」叠字 bug。

### 7.3 RequestRuntime contextvar

文件 [runtime_context.py](backend/app/application/agent/runtime_context.py)。

**动机**：LangGraph resume 时节点从头重跑，原 invoke 的 session 已失效。写工具必须 override `_arun` 绕过 ToolRunner（ToolRunner 会吞 `GraphInterrupt`）。

**用法**：
- facade 在 `invoke` / `astream_events` / `resume_stream` 最外层 `set_current_request(RequestRuntime(actor, session, thread_id, policy_override))`
- 写工具 `_arun` 内部 `rt = get_current_request()` 拿当前请求的 fresh session/actor
- finally `reset_current_request(token)`

### 7.4 Checkpointer

文件 [checkpoint.py](backend/app/infrastructure/agent/checkpoint.py)。

- 生产：`AsyncRedisSaver`（`langgraph-checkpoint-redis`），要求 `redis-stack-server`（含 RedisJSON + RediSearch）
- TTL=600s（10min），与写工具 pending 的 `expires_at` 对齐
- 单测：`MemorySaver` 兜底
- `configure_checkpointer(redis_url)` 在 app lifespan 调用一次
- `setup_checkpointer()` 调 `asetup()` 创建搜索索引（强依赖，失败直接抛）

---

## 8. 异常兜底约定

[facade.py:677 `_trace` wrapper](backend/app/application/agent/facade.py#L677)：

- **`GraphInterrupt` 必须放行**：HITL 暂停信号，必须冒泡到 checkpointer 落 Redis
- **其他 Exception 捕获**：
  - 写 `node_errors[node] = {type, message}`
  - 返回安全草稿 `draft_reply = "处理出现问题，请稍后重试或回复「人工」转人工。"`
  - `draft_context = {"source": "error", "node": node_name}`
- 保证异常分支仍汇入 `compliance_check` 而非 500（error draft 在 compliance 里会跳过 LLM 润色，直接透传安全文案，避免 LLM 幻觉假工单号）

---

## 9. 一图速查：典型场景的调用链

### 场景 A：用户问「你们退货几天内可以？」

```
POST /stream → _stream_agent_run → facade.astream_events
  → intent_classify → intent_router(knowledge_qa) → policy_lookup
  → knowledge_router(hit) → compliance_check
  → LLM 润色政策字段答案 → token_chunk 流式
  → reply → done
```

### 场景 B：用户问「我要退订单 abc-123」

```
POST /stream → facade.astream_events
  → intent_classify(task) → intent_router(task) → task_node
  → ReAct: order_query(abc-123) → policy_check(intent=refund)
  → policy allows → refund_request._arun → interrupt(pending)
  → checkpointer 落 Redis → facade 检测 pending
  → yield confirmation_required → 前端弹卡片（无 reply/done）

# 用户点确认
POST /actions/confirm → facade.resume_stream(Command(resume={"confirmed":true}))
  → task 子图重入 → refund_request 重跑 → interrupt 返回 decision
  → 生成工单号 RF-xxx → ReAct Final Answer
  → task → compliance_check → LLM 包装带工单号回复
  → reply_chunk → reply → done
```

### 场景 C：用户说「转人工」

```
POST /stream → facade.astream_events
  → intent_classify(handoff) → intent_router(handoff) → handoff_node
  → mark_escalated(ticket_no=HO-xxx) → draft_reply
  → compliance_check → LLM 包装转人工文案
  → escalated 卡片 + reply → done
```

### 场景 D：用户问「你好」

```
POST /stream → facade.astream_events
  → intent_classify(simple_qa) → intent_router(simple_qa) → compliance_check
  → draft_reply 空 → LLM 直答 → token_chunk 流式
  → reply → done
```

---

## 10. 调试建议

- **断点起点**：`facade.astream_events` 内的 `graph.astream_events(...)` 调用处（[facade.py:372](backend/app/application/agent/facade.py#L372)）
- **节点级日志**：每个节点的 `_trace` wrapper 都会 emit `node_start`/`node_end`，看 SSE 的 `event: node` 帧就能知道走到哪了
- **debug 帧**：`_build_debug` 把 intent / policy_decision / rag_hits / tool_executions / node_errors 全塞进 `event: debug`，前端「决策依据」面板直接看
- **HITL 卡住排查**：
  1. 看 Redis 是否有 checkpoint key（`get_state` 是否返回非空 `.next`）
  2. 看写工具是否真的调了 `interrupt`（不是被 ToolRunner 吞了）
  3. 看写工具是否 override 了 `_arun`（`_execute_impl` 路径不走 interrupt）
  4. 看 `RequestRuntime` 是否 set（`get_current_request` 抛 `LookupError` 说明 facade 没 set）

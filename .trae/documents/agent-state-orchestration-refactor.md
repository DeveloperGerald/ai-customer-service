# 状态编排重构：四分类意图 + 政策优先 + 人在回路 + 合规汇总 + 异常处理

## Context（为什么做）

当前售后客服 Agent 是 5 路意图分流（smalltalk/handoff/needs\_tools/unknown/faq\_only）+ 嵌套 ReAct 子图直接执行写工具的架构，存在以下问题与诉求：

1. 意图分类需收敛为 4 类（简单问答/转人工/知识问答/执行任务），路由更清晰。
2. 知识问答一律走向量库 RAG，没有先利用 `tenant_policy_configs` 结构化字段（return\_days / return\_policy\_type / custom\_product\_allowed / restocking\_fee\_pct / warranty\_days）直接回答，浪费一次检索且答案可能不如结构化字段准确。
3. 写操作（refund/exchange/repair）在 ReAct loop 内被 LLM 自主调用、无确认即执行，缺乏安全闸门。需引入「人在回路」：写操作必须用户在页面点击确认后才执行，10min 超时，状态可持久化。
4. 各分支回复散落，无统一合规校验出口。需统一汇总到合规检查节点再回复用户。
5. 节点异常会冒泡或被零散吞掉，无统一兜底。

用户已确认的三个关键决策：

* HITL 用 **LangGraph 原生** **`interrupt()`** **+ Redis checkpointer**（新增 `langgraph-checkpoint-redis` 依赖）。

* 知识问答的政策识别用 **LLM 判断**（是否可由结构化政策字段直接回答）。

* 合规检查节点 = **规则合规校验 + LLM 包装**，替代现 `llm_wrap_node`。

目标产出：四分类意图起手 → 知识问答政策优先 → 写操作 interrupt 确认 → 所有路径汇总合规节点回复 → 节点异常统一兜底。

## 新拓扑

```
START → intent_classify（4 分类: simple_qa / handoff / knowledge_qa / task）
intent_classify ── conditional router ──┐
  simple_qa    → compliance_check
  handoff      → handoff_node → compliance_check
  knowledge_qa → policy_lookup ── conditional ──┐
                   policy_hit  → compliance_check（政策字段直接作答）
                   policy_miss → rag_retrieve → compliance_check（RAG，回答必须遵循检索内容）
  task         → task_react_subgraph（create_react_agent：order_query/product_list/product_query/policy_check
                                     + refund_request/exchange_request/repair_request/cancel_order）
                   写工具 _execute_impl 内 interrupt(pending) → 外层图暂停 → [resume Command] → 写工具执行 → ReAct 继续
                → compliance_check
所有分支 → compliance_check（规则合规 + LLM 包装）→ END
```

关键点：
- `unknown` 不再单列：分类器输出 4 类，解析失败兜底为 `simple_qa`（交 LLM 引导澄清），去掉 `unknown_node`。
- **task 分支保留完整 ReAct（多工具迭代）**：用 langchain `create_react_agent` 作为外层图的**子图节点**（`builder.add_node("task", react_compiled)`），共享 Redis checkpointer。工具集含只读工具 + 全部写工具。LLM 可多轮调用工具（如 order_query → policy_check → refund_request），单工具不够时继续推理。
- **HITL 通过写工具内置 `interrupt()` 实现**：refund/exchange/repair/cancel_order 四个写工具在 `_execute_impl` 开头调 `decision = interrupt(pending_payload)` 暂停。`interrupt()` 在子图工具节点内触发 → 外层图暂停 → checkpointer 落 Redis。resume 经 `Command(resume={"confirmed":...})` 恢复，工具 interrupt 返回 decision，确认则执行、拒绝则返回取消结果，ReAct 继续 → 最终答复。这比"读/写分离 + 独立 confirm 节点"更贴合 ReAct 多工具语义（用户反馈 #1）。
- **状态桥接**：AgentState 增 `messages` 通道（`Annotated[list, add_messages]`），task 子图直接读写 messages；compliance_check 取 `draft_reply or messages[-1].content` 作为草稿。其余分支（simple_qa/knowledge_qa/handoff）写 `draft_reply`。

## 实施步骤（按文件）

### 1. 依赖与基建

* `backend/pyproject.toml`：新增 `langgraph-checkpoint-redis>=0.1.3`（已验证可装、兼容 langgraph 0.6.11；会带 `redisvl`、可能把 redis 升到 6.x）。

* 新增 `app/infrastructure/agent/checkpoint.py`：构建单例 Redis checkpointer。

  * 用 `langgraph.checkpoint.redis.RedisSaver`（或 async 变体），`from_url(settings.redis.url)`，配置 `ttl=600`（10min，秒）让暂停的 checkpoint 自动过期。

  * 暴露 `get_checkpointer()` 供 facade 注入；离线/未配置时退回 `MemorySaver`（单测友好）。

* `app/application/agent/graph.py`：

  * `build_customer_service_graph(...)` 新增 `checkpointer` 参数，`builder.compile(checkpointer=checkpointer)`。

  * `_build_langgraph_config` 把 `thread_id` 写入 `config["configurable"]["thread_id"]`（checkpointer 按 thread\_id 存取）。

  * 拓扑重写为新节点集与条件边（见下）。

  * `StreamableCompiledGraphWrapper` 增加：

    * `get_state(config)` 透传 → 用于 facade 检测 interrupt 与读取 pending payload。

    * `ainvoke`/`astream_events` 支持 `Command` resume 入参（resume 时传 `Command(resume=...)` 而非 state）。

### 2. Schema：`app/application/schemas/agent.py`
- `INTENT_CANDIDATES` 改为 `("simple_qa", "handoff", "knowledge_qa", "task")`。
- `AgentState.intent_candidate` Literal 同步为 4 类；`intent_hint` 新增 `cancel`（取消订单）。
- 新增字段：
  - `messages: Annotated[list, add_messages]`（task 子图用，create_react_agent 原生通道；非 task 分支不动）
  - `policy_answer: dict | None` / `policy_lookup_hit: bool | None`（knowledge_qa 路由用）
  - `draft_reply: str | None`（simple_qa/knowledge_qa/handoff 产出的草稿，供 compliance 包装）
  - `draft_context: dict | None`（rag_hits/policy_summary/tool_results/action_kind，供 compliance 选 prompt 约束）
- 保留 `node_errors`（已存在）用于异常记录。

### 3. 分类器：`app/infrastructure/llm/classifiers.py`
- `_INTENT_SYSTEM_PROMPT` 重写为 4 类路由意图：`simple_qa`（寒暄/简单问答）/ `handoff`（明确转人工）/ `knowledge_qa`（政策/规则咨询，不申请售后动作）/ `task`（申请售后、查订单、查商品、取消订单等读写操作）。
- 子意图 hint 扩展为 `{refund, exchange, repair, cancel, order_status, product}`，仅 `task` 时填写（`cancel`=取消订单）。
- `LLMIntentClassifier._parse_output` / `_INTENT_HINT_VALUES` / 白名单更新为 4 类；非法值兜底 `simple_qa`（替代旧 `faq_only` 兜底）。
- `intent_classify_node`（nodes.py）whitelist 同步为 4 类。

### 4. 节点：`app/application/agent/nodes.py`
重构节点集（删除 `smalltalk_node`/`unknown_node`/`faq_node`/`llm_wrap_node`，改造 `agent_node_factory` → `build_task_react_subgraph`）：

- **`policy_lookup_node`（新）**：取 `ctx.effective_policy`（TenantPolicy 结构化字段 return_days/return_policy_type/custom_product_allowed/restocking_fee_pct/warranty_days）+ 用户问题，调 LLM 判断该问题能否被结构化字段直接回答。命中则填 `policy_answer`（字段化文案）+ `policy_lookup_hit=True`；未命中 `policy_lookup_hit=False`。LLM 失败按 `policy_lookup_hit=False` 兜底走 RAG。
- **`rag_retrieve_node`**：保留，仅 knowledge_qa 未命中政策时调用（去掉 for_agent/for_faq 双名，统一一个节点）。
- **`build_task_react_subgraph(ctx)`（改自 `agent_node_factory`）**：构造 `create_react_agent` 子图（langchain 封装，满足用户反馈 #1 的多工具迭代）。
  - 工具集：只读 `order_query`/`product_list`/`product_query`/`policy_check` + 写 `refund_request`/`exchange_request`/`repair_request`/`cancel_order`。
  - system prompt 沿用现 `_build_agent_system_prompt` 的硬约束（policy_check 必须先于写工具；写工具成功后立即 Final Answer），并补充"写工具会先向用户确认，确认后才执行"。
  - **写工具内置 `interrupt()`（见步骤 5）**实现 HITL，无需独立 confirm/execute 节点。
  - 子图作为外层图节点 `task` 添加；共享 Redis checkpointer；interrupt 从子图传播到外层图，`Command(resume=)` 恢复。
  - 子图最终 AIMessage 写入 `messages` 通道；外层 `draft_context` 由 compliance 从 messages 末尾 + tool 历史提取。
- **`handoff_node`**：保留（立即 mark_escalated），产 draft 交 compliance 包装。
- **`compliance_check_node`（新，替代 llm_wrap）**：
  - 草稿来源：`draft_reply`（非 task 分支）或 `messages[-1].content`（task 子图最终答复）+ `draft_context`（rag_hits/policy_answer/tool_results/action_kind）。
  - 按 `draft_context.source` 拼 system prompt：RAG 来源强制「回答必须遵循检索到的内容」；政策来源用政策字段；写工具结果用工单号/金额。
  - 调 `ctx.chat_model` 生成 `final_reply`（沿用现 NODE_STREAM token 旁路保证打字机）。
  - **规则合规校验**（生成后）：① 屏蔽/重写 tenant_id 泄露；② 校验工单号格式与 `action_result_json.ticket_no` 一致，不一致改写安全文案；③ 校验不出现编造订单号；④ 写操作回复必须对应 `policy_decision`（无政策依据的写动作不得宣称成功）。
  - 违规且无法自动修整 → 降级安全文案 + `node_errors["compliance"]=...`。
  - 优先读 `state["final_reply"]`（handoff/异常预设文案）直通，跳过 LLM（沿用现 prefixed 逻辑）。
- **路由函数**：`intent_router`（4 路）、`knowledge_router`（hit/miss）。task 不再分叉（写操作的 confirm/reject 在子图内由 interrupt 处理，子图结束后统一 → compliance）。
- **节点异常统一包装**：在 facade `_bind_node_contexts` 的 `_trace` 装饰器里把「向上冒泡」改为「捕获 → 写 `node_errors[node]` + 返回 `{"draft_reply":"处理出现问题，请稍后重试或回复「人工」转人工"}`」，保证异常分支仍汇入 compliance_check 而非 500。关键节点（policy_lookup/compliance/task 子图）内部保留现有 try/except 细粒度兜底。

### 5. 写工具实现 + HITL：`app/application/tools/builtin.py` + `app/domain/repositories/order.py`
用户反馈 #2：实现换货/退货/取消订单接口并注册为工具。
- **新增 `cancel_order` 工具（`CancelOrderTool`）**：参数 `order_id` + `reason`。业务：仅当订单 `status ∈ {pending_payment, paid}` 时可取消 → 置 `status='cancelled'`；已 shipped/delivered 等禁止（引导走退款/换货）。返回 `{ticket_no: "CX-...", accepted, order_id, new_status}`。
- **`refund_request` 改为真实实现**：policy_check 允许后，置订单 `status='refunded'`（原 stub 仅返回 ticket_no）。返回含 `ticket_no`(RF-) + `new_status` + `refund_amount_cents`（来自 policy_decision）。
- **`exchange_request` 真实化**：订单状态不变更（换货不取消订单），但记录换货受理工单 EX- + 校验 policy can_exchange。返回 ticket_no + accepted。
- **`repair_request`**：保留（保修工单，不取消订单）。
- **OrderRepository 新增 `update_status(tenant_id, order_id, new_status)`**：带 tenant_id 强隔离 + 状态机校验；返回更新后 OrderRead。
- **`build_default_registry()`**：注册 `CancelOrderTool()`；`build_langchain_tools` 白名单加入 `cancel_order`。
- **HITL（四个写工具统一）**：`_execute_impl` 开头构造 `pending = {"tool": self.name, "args": args.dict(), "summary": <人类可读摘要>, "expires_at": <now+600s iso>}`，调 `from langgraph.types import interrupt; decision = interrupt(pending)`。
  - `decision.get("confirmed")` 为 True → 执行真实写逻辑；False/超时 → 返回 `{"accepted": False, "reason": decision.get("reason","user_cancelled")}`，ReAct 拿到结果继续生成答复。
  - 超时判定：`decision` 为空或 `now > expires_at` → 同拒绝。Redis checkpoint TTL=600s 保证暂停态自动过期，resume 时若 get_state 无 pending → 返回超时文案。

### 6. Facade：`app/application/agent/facade.py`
- `_bind_node_contexts`：节点字典替换为新节点集（intent_classify/policy_lookup/rag_retrieve/task=react子图/handoff/compliance_check）。
- `build_customer_service_graph` 传入 `checkpointer=get_checkpointer()`；task 节点用 `build_task_react_subgraph(ctx)` 返回的 compiled 子图。
- `invoke` / `astream_events`：
  - config 写入 `configurable.thread_id`。
  - `astream_events` 结束后调 `graph.get_state(config)`：若 `snap.next` 非空（暂停在 task 子图写工具 interrupt）→ 从 `snap.tasks[*].interrupts[*].value` 读 pending payload → yield 新事件 `{"type":"confirmation_required","pending_action":{...}}`（**不** yield reply/done）。
  - 新增 `resume_stream(decision)` 方法：用 `graph.astream_events(Command(resume=decision), config)` 恢复，复用现有事件映射产出 task续跑→compliance 的 token/reply/done。
  - 兼容旧前端：confirmation_required 帧后不强行发 reply，前端按事件类型切换 UI。

### 7. API：`app/api/agent.py`
- `_stream_agent_run` 增加 `confirmation_required` SSE 帧映射。
- 新增 `POST /api/conversations/{thread_id}/actions/confirm`：
  - body: `{decision: bool, action_id?: str}`。
  - 校验 thread 归属 → 调 `facade.resume_stream({"confirmed": decision})` → SSE 流式返回（与 stream 端点同协议：reply_chunk/reply/done）。
  - 若 `get_state` 显示无 pending（已超时/checkpoint TTL 过期）→ 返回 409/timed-out 文案。
- `AgentRunResponse`/SSE 协议文档补充 `confirmation_required` 事件。

### 8. 测试
- 更新 `tests/` 中意图分类断言（5 类 → 4 类，含 cancel hint）。
- 新增：policy_lookup 命中/未命中路由、task 写工具 interrupt 触发 confirmation_required、confirm resume 成功执行写工具（订单状态变更）、reject 返回取消、超时拒绝、节点异常兜底走 compliance。
- cancel_order 状态机单测（pending_payment/paid 可取消，shipped 不可）。
- 复用现 `MockLLM`/`MockRetriever`；checkpointer 在单测用 `MemorySaver`。

## HITL 流程时序

1. 用户「订单 A 我要退款」→ intent=task, hint=refund。
2. `task` 子图（create_react_agent）：LLM 推理 → order_query → policy_check（can_refund=True）→ LLM 决定调 refund_request。
3. refund_request `_execute_impl` 开头 `interrupt({tool, args, summary, expires_at})` → 子图 + 外层图暂停，checkpoint 写 Redis（TTL 600s）。
4. facade `get_state` 检测 `snap.next` 非空 → 从 `snap.tasks[*].interrupts[*].value` 取 pending → SSE `confirmation_required`（含 summary）→ 前端弹确认卡片。本轮不发 reply。
5. 用户 10min 内点确认 → `POST /actions/confirm {decision:true}` → `facade.resume_stream({"confirmed":True})` → `graph.astream_events(Command(resume=...), config)` → refund_request interrupt 返回 confirmed → 执行真实退款（订单 status→refunded）→ ReAct 继续 → LLM Final Answer → `compliance_check` 包装「工单号 RF-…」→ SSE reply_chunk/reply/done。
6. 拒绝：`decision:false` → refund_request 返回 `{accepted:false}` → ReAct 拿到结果 → compliance 输出「已为您取消本次操作」。
7. 超时：Redis checkpoint TTL 过期 / resume 时 `now > expires_at` 或 `get_state` 无 pending → 返回「操作已超时，请重新发起」。

## 关键复用点
- 写工具执行：`app/application/tools/builtin.py` 的 `RefundRequestTool`/`ExchangeRequestTool`/`RepairRequestTool`/`CancelOrderTool`（新）+ `build_adapter_context`/`ToolExecutionContext`（idempotency 复用）。
- ReAct 子图：langchain `create_react_agent`（现 `_run_react_loop` 已在用，改为子图节点形式）。
- 政策判定：`RefundQualificationService.decide`（`policy_check` 工具已封装）。
- 政策结构化字段：`TenantPolicy`（`app/domain/constants/policies.py`）+ `PolicyConfigRepository.get_effective_policy`。
- RAG：`BaseRetriever.retrieve` + `PGVectorStore`（`app/infrastructure/vectorstore.py`）。
- Token 流式：`NODE_STREAM` 旁路（`graph.py`），compliance_check 沿用 emit_token。
- 会话审计写消息：`ConversationRepository.append_message`（role=tool）。

## 验证
1. `cd backend && .venv/bin/ruff check . && .venv/bin/python -m pytest -q` 全绿。
2. 手动：用 `tenant_a` 消费者发「退款政策是什么」→ 命中 policy_lookup 直接答；发「你们售后有哪些规矩」→ policy miss → RAG 答。
3. 手动：发「订单 <A-ORD> 我要退款」→ 收到 `confirmation_required` SSE；调 `/actions/confirm {decision:true}` → 收到带 RF- 工单号的 reply，且订单 status 变 refunded；`decision:false` → 收到已取消文案。
4. 手动：cancel_order —— paid 状态订单调取消 → 确认后 status 变 cancelled；shipped 订单取消 → 被拒并引导退款。
5. 手动：确认后等 >10min 再 confirm → 超时文案。
6. 手动：断开 LLM（chat_model=None）触发节点异常 → 回复兜底安全文案而非 500。
7. 实施前先做 interrupt 子图传播 spike：验证 create_react_agent 作为子图节点 + 写工具 interrupt 能被外层 `Command(resume=)` 恢复；若该路径不通，回退为「写工具走独立 interrupt 节点 + 手动 bind_tools ReAct 循环」。


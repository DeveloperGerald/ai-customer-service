# LangGraph 编排重构 + LangChain Agent 引入 任务队列

> 按依赖顺序垂直切片：接口/schema → 测试 → 实现 → pytest+ruff → 验收。
> 每条 AC 对应 [spec.md](./spec.md)；每条 TR 为 rule 或 rubric。

---

## Task 1: 新增 LangChain 工具适配层（BaseTool → StructuredTool）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: None
- **Description**:
  - 新建文件 `backend/app/application/tools/langchain_adapter.py`：
    1. `build_langchain_tools(tool_registry, tool_runner_factory, actor, idempotency_ctx)`：遍历 ToolRegistry._tools，把每个 BaseTool 转为 LangChain StructuredTool。
    2. 参数 schema：BaseTool.param_schemas → 动态构造 Pydantic v2 BaseModel（`_mk_args_model(name, params)` 闭包），type/required/enum 对齐 ToolParamSchema。
    3. StructuredTool 的 `coroutine` / `func` 实际执行逻辑：
       - 读工具（category=read）：构造 ToolCallRequest(tool_name=name, arguments=args, idempotency_key=None, session_id=xxx)，调 `ToolRunner.run(actor, request)`，返回 ToolResult.data 的 JSON 字符串或 dict。
       - 写工具（category=write/escalation）：由 adapter 生成 idempotency_key = `f"agt-{idempotency_ctx.thread_id}-{step_idx}-{salt}"`（step_idx 由 adapter 内部原子计数器维护），再走 ToolRunner.run。
       - 统一捕获 ToolExecutionError / AppError：不抛裸异常，以 Observation 形式返回 `"[ToolError] code=... message=..."`，让 Agent ReAct 循环能消化。
    4. 新增虚拟只读工具 `policy_check`（不注册到 ToolRegistry，只在 adapter 里动态构造）：
       - name = "policy_check"，description = "根据订单详情 + 政策判定该订单能否退款/换货/维修（纯函数确定性计算，不写 DB）。必须在 order_query 成功后调用一次。"
       - args_schema = `{order_detail: dict, intent_hint: str}`（intent_hint 取 user_message 里的 refund/exchange/repair 关键词或 user_message 全文）
       - run 时直接调 `from app.application.agent.nodes import _decide_policy`，把 PolicyDecision.to_state_json() 作为 Observation 返回。
       - 可信身份覆盖：tenant_id 从 Actor 取，不从 LLM 参数取。
  - 依赖注入位置：adapter 在 facade 层构造 AgentNodeContext 时按需实例化（见 Task 5）。
- **Acceptance Criteria Addressed**: AC-2 (FR-7/8/9), AC-4 (FR-13/14/15)
- **Test Requirements**:
  - `rule` TR-1.1: `build_langchain_tools` 返回的列表中含 4 个真实工具（order_query / exchange_request / repair_request / refund_request）+ 1 个虚拟 policy_check；每个 StructuredTool.name 与 BaseTool.name 一一对应。
  - `rule` TR-1.2: 对 refund_request（写工具）模拟调用两次同参数 → idempotency_key 不同（步长计数器递增）但 ToolRunner 内部幂等缓存仍生效（因为幂等键是 adapter 生成的不同，幂等缓存不会命中——断言改为：**idempotency_key 本身格式合法且 thread_id 前缀正确**）。
  - `rule` TR-1.3: 虚拟 policy_check 传入一份合法 order_detail（含 signed_at_days_ago=3, is_custom=false）+ intent_hint="refund" → 返回的 Observation 含 can_refund=true（与 _decide_policy 结果一致，可直接比 JSON）。
  - `rule` TR-1.4: adapter 强制覆盖 LLM 传入的 tenant_id/user_id（可在 run 前 hook apply_trusted_actor_override 返回值检查 merged.tenant_id == actor.tenant_id，不依赖真实 DB）。
  - `rule` TR-1.5: `ruff check backend/app/application/tools/langchain_adapter.py` 退出码 0。
- **Notes**: refund_request 目前 ToolRegistry 里没有，Task 2 补齐后 Task 1 跑测试。

---

## Task 2: 抽 `refund_request` 为标准 BaseTool 并注册
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: None
- **Description**:
  - 在 `backend/app/application/tools/builtin.py` 新增类 `RefundRequestTool(BaseTool)`：
    - name = "refund_request"，category = "write"
    - description = "提交退款申请（写操作，占位 stub）。根据 policy_check 结果受理并生成工单号，真实对接在 T8。"
    - param_schemas = [
        `{order_id: string, required: true, description: "订单 UUID（必须是已查存在的订单 order_id）"}`,
        `{reason: string, required: true, enum: ["7_day_return","quality","wrong_good","other"], description: "退款原因"}`,
        `{remark: string, required: false, description: "≤500 字说明"}`
      ]
    - run() 逻辑对齐 nodes.refund_node（[nodes.py L392-L425](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/nodes.py#L392-L425)）：
      1. 校验 order_id 存在且 UUID 合法（不校验订单归属，OrderRepository 已在 order_query 保证，这里再次通过 OrderRepository.get_read_for_actor 检查以防御 Agent 幻觉订单号）。
      2. ticket_no = f"RF-{ctx.actor.tenant_id.upper()}-{ctx.call_id.hex[:8].upper()}"
      3. 返回 data dict = {ticket_no, accepted=true, message="退款申请已受理（占位 stub）", stub=true, tenant_id, order_id, refund_amount_cents=None, fee_pct=None, call_id=str(ctx.call_id)}
      4. **不写 conversation message append**（原 refund_node 写了一条 role=tool，这部分统一由 llm_wrap 的 final 写或 Agent 子图 outer wrapper 统一写审计，不在 tool.run 内重复写，避免与 ToolRunner audit 双写）。
  - 更新 `build_default_registry()`（[builtin.py L174-L181](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/builtin.py#L174-L181)），追加 `registry.register(RefundRequestTool())`。
- **Acceptance Criteria Addressed**: AC-2 (FR-8)
- **Test Requirements**:
  - `rule` TR-2.1: ToolRunner.run(actor, ToolCallRequest(tool_name="refund_request", arguments={order_id=合法存在的, reason="7_day_return"}, idempotency_key="test-refund-1")) → 返回 ToolResult.success=true，data.ticket_no 前缀 "RF-"，stub=true。
  - `rule` TR-2.2: 缺 idempotency_key → ToolRunner 立即抛 TOOL_IDEMPOTENCY_KEY_REQUIRED，不执行 run。
  - `rule` TR-2.3: 同 idempotency_key 跑两次（同参数）→ 第二次 ToolResult.from_idempotency_cache=true，call_id 不同但 ticket_no 相同。
  - `rule` TR-2.4: order_id 非法 UUID → 抛 VALIDATION_ERROR，audit failed。
  - `rule` TR-2.5: `ruff check backend/app/application/tools/builtin.py` 0 告警。
- **Notes**: 可先不跑真实 refund Node 的旧测试（Task 4 删旧 refund Node 时统一处理测试迁移）。

---

## Task 3: 重构 intent_classify_node 输出 5 大类
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: None
- **Description**:
  - 修改 `backend/app/application/agent/nodes.py`：
    1. 保留关键词组 `_REFUND_KEYWORDS / _EXCHANGE_KEYWORDS / _REPAIR_KEYWORDS / _HANDOFF_KEYWORDS / _IMAGE_HINTS / _SMALLTALK_KEYWORDS / _ORDER_NO_PATTERNS`（不变）。
    2. 重写 `_classify_intent(text: str)` 返回值：
       - 优先级保持：Handoff > Repair/Refund/Exchange（但不直接标 refund 标签，仅在内部记 intent_hint 用于 needs_tools 判定）> Smalltalk > FAQ 兜底。
       - **返回值定义**：`(intent: str, extra: {"order_ref": dict|None, "intent_hint": "refund"|"exchange"|"repair"|"order_status"|None})`
       - 分类规则（外层 intent 只有 5 种）：
         a. handoff：命中 _HANDOFF_KEYWORDS 或 _IMAGE_HINTS → `("handoff", ...)`
         b. unknown：IntentClassifierProtocol 输出的标签不在白名单且非 FAQ 常见问题（白名单 = {refund,exchange,repair,handoff,faq,order_status,smalltalk}）→ `("unknown", ...)`（保持现有逻辑）
         c. smalltalk：命中 _SMALLTALK_* 规则 → `("smalltalk", ...)`
         d. needs_tools：满足任一 → `("needs_tools", {order_ref, intent_hint=refund|exchange|repair|order_status})`
            - i. order_ref 非空（任何订单号）
            - ii. user_message 含"我要/申请/办理+退/换/修"等第一人称申请组合（正则可先 `(我要|我想|申请|帮我|办理).*(退|换|修|退款|换货|维修)`）
            - iii. 匹配 repair/refund/exchange 关键词 **且** 消息长度 > 15 字（强暗示要办理，不是问规则）
         e. faq_only：其他所有情况（包括匹配了 refund/exchange/repair 关键词但只有短句如"能退吗？"）→ `("faq_only", ...)`
    3. 更新 `intent_classify_node(state, ctx)`：调用 classifier 后若 classifier 只返回老的 7 标签，则用上面的规则再映射一层为 5 大类；返回 `{"intent_candidate": intent5, "order_ref_candidate": extra.order_ref, "intent_hint": extra.intent_hint}`（新增 `intent_hint` 字段，Agent 子图 system prompt 会用）。
    4. 同步更新 `AgentState` schema（[backend/app/application/schemas/agent.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/schemas/agent.py)），加 `intent_hint: str | None` 字段。
- **Acceptance Criteria Addressed**: AC-1 (FR-1/2/3/4)
- **Test Requirements**:
  - `rule` TR-3.1: 12 条样本映射全对（每条断言 intent_candidate ∈ 5 种，且 order_ref/intent_hint 正确）：
    | 输入 | 期望 intent | order_ref | intent_hint |
    |------|------------|-----------|-------------|
    | 你好 | smalltalk | None | None |
    | 谢谢再见 | smalltalk | None | None |
    | 人工 | handoff | None | None |
    | 转真人看看 | handoff | None | None |
    | 有图片帮我看看 | handoff | None | None |
    | 能退吗？ | faq_only | None | None |
    | 7 天能换货吗？| faq_only | None | None |
    | 订单号 A-ORD-202509-001 我要退款 | needs_tools | {order_no: A-ORD-202509-001} | refund |
    | 我想申请换货 A-ORD-xxx | needs_tools | {order_no: xxx} | exchange |
    | 帮我查一下订单 B-ORD-202509-003 | needs_tools | {order_no: B-ORD-202509-003} | order_status |
    | 佛串材质是什么 | unknown | None | None |
    | 订单号 A-ORD-202509-001 | needs_tools | {order_no: A-ORD-202509-001} | order_status |
  - `rule` TR-3.2: `ruff check backend/app/application/agent/nodes.py backend/app/application/schemas/agent.py` 0 告警。
- **Notes**: 先不管 action_branch_router 路由，Task 4 处理。

---

## Task 4: 重构 action_branch_router + 清理旧刚性动作节点
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 3
- **Description**:
  - 修改 `action_branch_router(state)`（[nodes.py L352-L386](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/nodes.py#L352-L386)）：
    - FR-6 优先级顺序：
      1. `intent == unknown` → 返回 `"unknown"`
      2. `intent == handoff` → 返回 `"handoff"`
      3. `intent == smalltalk` → 返回 `"smalltalk"`
      4. `intent == needs_tools` → 返回 `"agent"`（新节点名，见 Task 5）
      5. `intent == faq_only` → 返回 `"faq"`
      6. 兜底（理论不会到）→ `"faq"`
    - **删除**原有的政策判定分支、售后意图无订单号 → faq 等细粒度逻辑（交给 Agent）。
  - 清理旧动作节点（按 FR-5 拓扑不再在主路径上使用）：
    - 删除 `refund_node` / `exchange_node` / `repair_node` / `order_query_node` / `policy_decision_node` 函数定义。
    - 保留 `handoff_node` / `smalltalk_node` / `faq_node` / `unknown_node` 不动。
  - 对应 facade 层 node_fns 绑定也要调整（Task 6 build_customer_service_graph 连线更新）。
- **Acceptance Criteria Addressed**: AC-1 (FR-5/6)
- **Test Requirements**:
  - `rule` TR-4.1: 给定 state.intent_candidate 分别 = 5 种 → router 返回值依次 = "unknown"/"handoff"/"smalltalk"/"agent"/"faq"。
  - `rule` TR-4.2: unknown 排在 handoff 之前（即使 state 同时满足 handoff 关键词特征，但已经置 intent=unknown 的话优先 unknown——实际由 Task 3 的 _classify_intent 优先级保证，这里只测函数本身对入参的分支）。
  - `rule` TR-4.3: `ruff check` 0 告警，删除节点后 import 清理干净。
- **Notes**: 删除节点后相关的旧测试会失败，Task 8 统一迁移为 Agent 语义等价用例。

---

## Task 5: 新增 agent_node（包装 langgraph.prebuilt.create_react_agent 子图）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 1 (adapter), Task 2 (refund tool), Task 4 (router)
- **Description**:
  - 在 `backend/app/application/agent/nodes.py` 新增函数 `agent_node_factory(ctx: AgentNodeContext) -> Callable[[AgentState], Awaitable[dict]]`：
    1. 收集 adapted_langchain_tools（来自 Task 1 build_langchain_tools(registry=ctx.tool_registry, ...)）。
    2. 拿到 llm_runnable = `ctx.chat_model.as_runnable()`（若 chat_model is None 则走 MockChatModelProvider 兼容，as_runnable 返回 AIMessageChunk astream）。
    3. 调 `langgraph.prebuilt.create_react_agent(model=llm_runnable, tools=adapted_langchain_tools, state_modifier=build_agent_system_prompt(...))`
       - state_modifier 返回 LangChain messages 列表：[SystemMessage(...), *history_messages, HumanMessage(user_message)]
       - SystemMessage 内容（动态拼）：
         a. "你是手串售后客服 Agent。严格按以下步骤推理：1) 若需订单详情 → 调 order_query；2) 拿到 order_detail 后 → 必须调 policy_check 一次；3) policy_check 允许 → 可调对应 write 工具（refund_request/exchange_request/repair_request）；4) write 工具一旦成功立即 Final Answer，不再调用任何工具；5) 若最终工具不足，Final Answer 中引导用户「回复人工」。"
         b. 政策规则摘要（从 ctx.effective_policy 读：7 天窗口、定制款、质量问题、手续费比例，不用 LLM 算）。
         c. RAG hits 摘要：`"\n参考知识库片段：\n" + "\n".join(f"- [{hit.metadata.source} {hit.metadata.title}] {hit.content[:150]}" for hit in state.rag_hits[:3])`（state.rag_hits 由 FR-5 拓扑 needs_tools → rag_retrieve 在前）。
         d. 品牌名、slogan（与 llm_wrap system prompt 一致）。
         e. 硬约束："所有工具结果为 Observation，你不能编造订单号、退款金额、工单号；最终 Final Answer 必须以 JSON dict 形式输出 {\"tool_execution_summary\": str, \"next_action_kind\": \"refund\"|\"exchange\"|\"repair\"|\"faq_only\"|\"handoff\"} 或纯自然语言文本。"
    4. 构造 `agent_inner = create_react_agent(...)`；若 create_react_agent 不存在或 API 签名变动，则在 adapter 层抛出明确 NotImplemented（不回退到手写循环）。
    5. 返回的外层 `agent_node(state, ctx)` 函数逻辑：
       - 构造输入：`input = {"messages": [HumanMessage(content=state.user_message)]}` 或 create_react_agent 要求的入参格式（查 langgraph 文档）。
       - `config = {"recursion_limit": 5, "configurable": {"thread_id": state.thread_id}}`（FR-11 步数上限 5）。
       - 调 `await agent_inner.ainvoke(input, config)` → 得到最终 state 或 messages。
       - 解析结果：
         a. 从 adapted_langchain_tools 的调用审计或子图 state 中提取 `tool_executions: list[ToolResult]`（可以在 Task 1 adapter 里挂一个 in-memory list，每次 run 后 append；再由 agent_node 读取并清理）。
         b. 若最后一条 tool_execution 是 write 工具（refund/exchange/repair）成功，则：
            - `action_kind = {refund_request:refund, exchange_request:exchange, repair_request:repair}[tool.name]`
            - `action_result_json = tool.data`
            - 写 conversation_repo.append_message(role="tool", ...)（与旧 refund_node 行为对齐，放到这里统一写一次，避免 tool.run 里双写）。
         c. policy_decision：从 policy_check 的 ToolResult.data 提取（若有）。
         d. escalated/escalation_reason/escalated_ticket_no：若任一工具异常 + LLM final answer 里提到转人工，则置 escalated=True，复用 FR-12 兼容字段。
         e. Agent 产出的 final_answer 暂存到 `state._agent_final_answer_draft`（llm_wrap_node 会优先用 LLM 重新包装成自然语言，Agent final_answer 只作补充，不直接对外）。
       - 返回部分 state patch = `{tool_executions, policy_decision, action_kind, action_result_json, escalated, escalated_ticket_no, escalation_reason, _agent_final_answer_draft}`。
    6. 异常处理：agent_inner.ainvoke 任何异常 → 捕获后返回 patch = `{action_kind: "faq_only", tool_executions: [{error: exc_info, success: false}], node_errors: {agent: ...}}`，保证 llm_wrap 仍能产出非 9 字兜底回复。
- **Acceptance Criteria Addressed**: AC-2 (FR-9/10/11/12), AC-3, NFR-4
- **Test Requirements**:
  - `rule` TR-5.1: Mock llm_runnable 让它按固定 ReAct 顺序：Thought→order_query→Obs(订单 ok)→policy_check→Obs(can_refund=true)→refund_request→Obs(ticket=RF-XXX)→Final Answer。断言最终 patch.action_kind == "refund"，tool_executions 含 3 条 success=true。
  - `rule` TR-5.2: Mock policy_check 返回 can_refund=false + reason=out_of_window → Agent *不触发* refund_request；最终 patch.action_kind 为 "faq_only"，tool_executions 只有 order_query + policy_check。
  - `rule` TR-5.3: recursion_limit 设为 3，Mock Agent 让它无限循环（每次都调用同一个 order_query）→ 断言 ainvoke 会被 LangGraph/ReAct 截断抛循环超限异常，agent_node 捕获后返回 patch 含 action_kind="faq_only"，**不向外层再抛**。
  - `rule` TR-5.4: 写工具 refund_request 被调用成功 → conversation_repo.append_message 被调用一次（role=="tool"）且 ticket_no 正确。
  - `rule` TR-5.5: `ruff check backend/app/application/agent/nodes.py` 0 告警。
- **Notes**: create_react_agent 具体参数如果与 0.2 版本文档有出入，以安装版本为准，但**不得引入非 langgraph 官方的 Agent**。

---

## Task 6: 重构 build_customer_service_graph 连线（FR-5 拓扑）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 4 (router), Task 5 (agent_node)
- **Description**:
  - 修改 `backend/app/application/agent/graph.py` 的 `build_customer_service_graph(node_fns, router_fn)`：
    1. 前半段改为：`START → intent_classify → action_branch_router`（不再有 rag_retrieve/order_query/policy_decision 线性前缀）。
    2. 条件边来源从 "policy_decision" 改为 "intent_classify"（action_branch_router 仍然挂在 router_fn；注意 FR-5 是 intent_classify 后立即分支；如果 router_fn 仍然在 policy_decision 之后的旧挂载位置，则显式调整为 `builder.add_conditional_edges("intent_classify", router_fn)`）。
    3. 为每条分支补边：
       - `"smalltalk"` → smalltalk_node（保持）
       - `"handoff"` → handoff_node（保持）
       - `"agent"` → **先 → rag_retrieve → agent_node**（FR-5 needs_tools 先 RAG）
       - `"unknown"` → unknown_node（保持）
       - `"faq"` → **先 → rag_retrieve → faq_node**（FAQ 也需要 RAG）
    4. 所有分支（smalltalk_node / handoff_node / agent_node / unknown_node / faq_node）→ 汇总 → llm_wrap → END。
    5. 注意 `rag_retrieve` 在两条路径都会被进入：用条件边包装或在 router 返回值里区分 "faq_rag" 和 "agent_rag" 两个不同中间节点（其实是同一个 rag_retrieve_node 函数两次不同的上游来源，LangGraph 支持多前驱，直接让 agent 和 faq 分支都先指向 rag_retrieve 再往下游会让 rag_retrieve 变成多入口汇总点，会在非目标分支时错误执行——所以正确做法是让条件路由区分 6 个返回值：`smalltalk, handoff, agent_entry, unknown, faq_entry`，其中 agent_entry→rag_retrieve→agent_node，faq_entry→rag_retrieve→faq_node；否则需要复制两个 rag_retrieve node 实例（rag_retrieve_for_agent / rag_retrieve_for_faq），它们都调用同一个函数但节点名不同，便于拓扑唯一。推荐 **复制为两个不同节点名 + 同一 fn**：`builder.add_node("rag_retrieve_for_agent", rag_retrieve_node)` 和 `builder.add_node("rag_retrieve_for_faq", rag_retrieve_node)`，路由返回 `"agent_rag"` / `"faq_rag"`。
  - 同步更新 facade 层的 `node_fns` dict 注入（[backend/app/application/agent/facade.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/facade.py)）：新增 `agent_node` / `rag_retrieve_for_agent` / `rag_retrieve_for_faq` 绑定；删除旧 refund/exchange/repair/order_query/policy_decision 绑定。
- **Acceptance Criteria Addressed**: AC-1 (FR-5/6)
- **Test Requirements**:
  - `rule` TR-6.1: 构造最小 state + Mock node_fns + router 返回 5 种场景 → 检查 MinimalStateGraph 或真 LangGraph 的 node_start 事件序列：
    - smalltalk: ["intent_classify", "smalltalk", "llm_wrap"]（不含 rag_retrieve）
    - handoff: ["intent_classify", "handoff", "llm_wrap"]（不含 rag_retrieve）
    - unknown: ["intent_classify", "unknown", "llm_wrap"]（不含 rag_retrieve）
    - faq: ["intent_classify", "rag_retrieve_for_faq", "faq", "llm_wrap"]
    - needs_tools: ["intent_classify", "rag_retrieve_for_agent", "agent", "llm_wrap"]
  - `rule` TR-6.2: 分支互不干扰：断言 smalltalk 路径的 astream_events 中**不包含**任何 rag_retrieve 名的 node_start。
  - `rule` TR-6.3: `ruff check backend/app/application/agent/graph.py backend/app/application/agent/facade.py` 0 告警。
- **Notes**: graph.py 的 StreamableCompiledGraphWrapper 事件映射不需要改（节点名变只是 name 字符串，wrapper 对未知节点名也会透传）。

---

## Task 7: 调整 llm_wrap_node 兼容 Agent 产出 + 最终回复收口
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 5
- **Description**:
  - 修改 `backend/app/application/agent/nodes.py` 的 `llm_wrap_node(state, ctx)`：
    1. 读取 `state.tool_executions`（若存在）→ 合并到 extra_context，新增键 `tool_results` = list of ToolResult.data（只暴露 data，不暴露内部错误）。
    2. 若 `state.action_kind` is None 但存在 tool_executions → 从最后一条写工具名推断 action_kind（refund_request→refund 等）。
    3. 若 `state.final_reply` 已经有值（unknown_node 设置的）→ 直接返回 `{final_reply: state.final_reply, _stream_chunks=[]}`（不跑 LLM，保持 FR-16）。
    4. 若 `state._agent_final_answer_draft` 存在且 chat_model is None（mock 环境）→ 可直接把 draft 作为 final_reply 返回候选，加速 mock 演示（但优先走 LLM 包装；只有 chat_model None 时才省一次调用）。
    5. history 加载保持不变（conversation_repo.list_messages），但注意 Agent 写进 conversation 的 role=tool 消息要在 history 过滤里忽略（只取 user/assistant 对），防止 LLM 包装时把原始工具 JSON 塞进 prompt 导致回答变味。
  - 同步调整 `_fallback_template_reply`：新增 `tool_results=None` 参数，若有 write 工具结果则用对应模板（与旧的 refund/exchange/repair 模板等价，只是数据来源从 state.action_result_json 换成 tool_executions[-1].data，兼容两路径）。
- **Acceptance Criteria Addressed**: AC-2 (FR-17), AC-5
- **Test Requirements**:
  - `rule` TR-7.1: 注入 state.final_reply = "抱歉，没能理解您的意思" → llm_wrap 返回 patch.final_reply 与输入逐字相等，BaseChatModel 若带计数 wrapper 则断言计数=0。
  - `rule` TR-7.2: state.tool_executions = [order_ok, policy_ok, refund_ok]；state.action_kind = None → 推断得到 action_kind = "refund"，fallback 模板输出含 ticket_no。
  - `rule` TR-7.3: llm_wrap 产生的 appended_messages（若 append_message 成功）→ role == "agent"，metadata.action_kind ∈ {smalltalk,handoff,refund,exchange,repair,faq_only,unknown}（没有 "agent_xxx" 奇怪值）。
  - `rule` TR-7.4: `ruff check` 0 告警。

---

## Task 8: 测试迁移与新用例补全（回归保证 76 → ≥74 通过 + 新 AC 用例）
- **Status**: `pending`
- **Priority**: `high`
- **Depends On**: Task 1 - Task 7 全部
- **Description**:
  - 修复/迁移旧单测：
    1. `tests/unit/test_task4_tools.py`：新增 refund_request 工具 4 条用例（Task 2 TR-2.1~2.4 已覆盖，直接加）。
    2. `tests/unit/test_task7_graph.py`：
       - 重写测试路由逻辑：原测试 expect `refund/exchange/repair/faq` 等刚性标签 → 更新为 5 类路由 + 6 分支中间节点（faq_rag/agent_rag）。
       - 新增 AC-1 TR 样本断言（Task 3/4/6 的覆盖）。
       - 新增 AC-2 多轮 refund happy path（需要构造 seed 订单 + mock llm 固定 ReAct 推理链）。
       - 新增 AC-3 policy 不满足不触发写工具（policy_check can_refund=false）。
       - 新增 AC-5 unknown 固定文案 + LLM 不调用。
    3. `tests/unit/test_task76_classifiers.py`：验证 classifier → 5 大类映射正确（Task 3 TR-3.1 覆盖）。
    4. 其他测试文件（test_task8_refund.py / test_task10_agent_http.py / test_task5_rag.py 等）：
       - 若直接依赖 refund_node / exchange_node 等被删除的函数 → 迁移为通过 Agent 子图 + 写工具语义完成（即通过 facade.ainvoke 驱动，不再直接单测内部节点）。
       - test_task10_agent_http 的 SSE 流式集成测试：确保 token_chunk 事件能透 needs_tools 分支（AC-6 验证）。
  - 迁移策略：删除的函数对应测试删除或改造；保留的测试仅调整断言标签名（refund→agent+refund语义等价通过 tool_executions 结果断言），不可因为逻辑变化简单删除测试。
- **Acceptance Criteria Addressed**: AC-1~AC-7 (整体验收)
- **Test Requirements**:
  - `rule` TR-8.1: `cd backend && pytest tests/unit/test_task4_tools.py tests/unit/test_task7_graph.py tests/unit/test_task76_classifiers.py -q` 退出码 0。
  - `rule` TR-8.2: `cd backend && pytest -q` 全量套件，失败测试数 ≤ 2（且必须是明显非核心非安全测试的边缘用例；若 >2 则继续修直到通过）。
  - `rule` TR-8.3: `ruff check backend/app backend/tests` 退出码 0。
  - `rubric` TR-8.4: 测试可读性和意图可识别性；scale 1-5；1=测试变量名都是 a/b/c 看不懂；3=基本按 Given-When-Then 结构，部分注释；5=每条测试 docstring 写清对应 AC/TR，Given-When-Then 三段清晰；threshold >= 4；evidence 抽样 tests/unit/test_task7_graph.py 的 3 条关键测试 docstring。

---

## 总任务依赖图（DAG）

```
Task1 (LangChain adapter) ──┐
Task2 (refund BaseTool)   ──┤
Task3 (5-class intent)   ───┼─ Task5 (agent_node) ─┐
                            │                       │
Task4 (router + 清理节点) ──┘                     ├─ Task7 (llm_wrap 兼容)
                            │                       │
Task6 (graph 连线重构)  ───────────────────────────┤
                                                    │
Task8 (测试迁移 + 新补)  ◄──────────────────────────┘
```

建议执行顺序：Task3 → Task4 → Task2 → Task1 → Task5 → Task6 → Task7 → Task8。（Task1/2 与 3/4 互不依赖，可并行）。

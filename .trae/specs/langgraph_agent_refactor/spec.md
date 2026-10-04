# LangGraph 编排重构 + LangChain Agent 引入规范

## Overview
- **Summary**: 重构当前 LangGraph DAG 的前半段顺序执行链路，改为意图分流后按需进入 RAG / LangChain Agent 子图；引入 `langgraph.prebuilt.create_react_agent` 实现多轮工具调用（order_query → policy_decision 纯函数 → refund/exchange/repair），unknown 保持固定拒绝文案兜底。
- **Purpose**: 解决当前架构「闲聊/转人工也跑 RAG+查单」的低效问题，并把「刚性路由 → 硬编码动作节点」的无扩展模式替换为 Agent 智能决策 + 多轮工具编排，支持先查订单再判断退款换货的典型售后场景。
- **Target Users**: 面试官（演示 Agent 多轮推理）、技术评审（验证架构可扩展性）。

## Goals
1. **意图分流正确**：5 种意图（smalltalk / handoff / needs_tools / unknown / faq_only）在第一步即路由到对应分支，不再无条件执行 RAG / order_query / policy_decision。
2. **Agent 多轮工具调用**：needs_tools 分支通过 `langgraph.prebuilt.create_react_agent` 子图，自动按 order_query → 现有 `_decide_policy` 纯函数 → refund/exchange/repair 的顺序完成推理，步数上限 5。
3. **unknown 固定文案**：unknown 分支保持「抱歉，没能理解您的意思」固定回复，不调用 LLM。
4. **安全机制不降级**：Agent 调用的所有工具统一走 ToolRunner.run()，保留可信身份覆盖、写工具幂等、审计日志三大硬约束。
5. **向后兼容 Facade 层**：Facade 的 ainvoke / astream_events 接口签名不变，现有 76 个测试可通过少量数据打桩调整后全部通过。

## Non-Goals
1. 不引入 `AgentExecutor` / `create_tool_calling_agent` 等非 StateGraph 型 Agent，保持底层统一为 LangGraph。
2. 不改造 llm_wrap_node 的核心回复生成逻辑（仅调整 state 字段读取）。
3. 不做图片识别、真人客服接管 UI、工单后台（沿用项目既有占位）。
4. 不新增"聊天历史上下文驱动的多轮决策"（当前 step 仅根据用户本轮消息 + RAG + 工具结果推理；多轮状态由 Checkpoint 提供但不做复杂对话管理）。
5. 不保留 legacy 刚性路由模式作为 fallback；旧的 refund_node / exchange_node / repair_node / order_query_node / policy_decision_node 在主干中删除，仅作为兼容读取保留或删除。

## Background & Context
- 当前 DAG：START → intent_classify → rag_retrieve → order_query → policy_decision → action_branch_router（刚性 8 分支）→ llm_wrap → END。
- 存在的问题：
  1. 闲聊/转人工/unknown 也无条件执行 RAG + order_query，浪费 tokens 和延迟。
  2. refund/exchange/repair 分支为硬编码动作节点，无法支持「先 order_query 再看资格再决定工具」的多轮场景。
  3. unknown 之前改为固定拒绝文案（已在 project_memory 记录），本次重构保持不变。
- 已确认的 4 项决策：
  1. Agent 子图使用 `langgraph.prebuilt.create_react_agent`（LangGraph 官方 ReAct，底层为 StateGraph）。
  2. Agent 调完 order_query 后把结果传入现有纯函数 `_decide_policy()`，PolicyDecision 作为 Observation 返回给 Agent，不把规则交给 LLM 幻觉。
  3. needs_tools 分支在进入 Agent **之前**先跑 RAG 检索，命中片段拼进 Agent system prompt。
  4. 不保留旧模式 fallback，直接一次性切换。
- 项目依赖已满足：`langgraph>=0.2,<1.0`、`langchain>=0.3,<1.0`（backend/pyproject.toml L23-L26）。

## Functional Requirements

### 意图识别与分流
- **FR-1 意图识别输出 5 大类**：`intent_classify_node` 输出 `intent_candidate ∈ {smalltalk, handoff, needs_tools, unknown, faq_only}`，不再区分 refund/exchange/repair/order_status 细粒度。
- **FR-2 needs_tools 判定条件**：满足任一即判定 needs_tools：(a) order_ref_candidate 非空；(b) 用户消息包含"我要/申请/办理 + 退款/换货/维修"等第一人称申请词；(c) 含"查订单/看看这个订单"等明确工具意图。
- **FR-3 faq_only 判定**：只有政策/规则咨询（不含订单号、不含第一人称申请）才为 faq_only；纯关键词售后问句无订单号也走 faq_only（查 RAG 直接回答规则）。
- **FR-4 unknown 白名单**：非上述四种的任何意图一律 unknown。

### 图拓扑重构
- **FR-5 新图拓扑**：
  ```
  START → intent_classify → action_branch_router（5 路）
    ├─ smalltalk    → smalltalk_node  ─────────────────┐
    ├─ handoff      → handoff_node  ───────────────────┤
    ├─ needs_tools  → rag_retrieve → agent_node  ──────┤
    ├─ unknown      → unknown_node（固定文案）────────┤
    └─ faq_only     → rag_retrieve → faq_node  ───────┘
                                                       ↓
                                                  llm_wrap → END
  ```
- **FR-6 路由优先级**：unknown 最高（立即拒绝）→ handoff → smalltalk → needs_tools → faq_only（兜底），与 action_branch_router 检查顺序一致。

### LangChain Agent 子图
- **FR-7 工具适配层**：把 ToolRegistry 中每个 BaseTool 适配为 LangChain StructuredTool，参数 schema 由 BaseTool.param_schemas → 动态构造 Pydantic v2 Model；StructuredTool.coroutine 内部不直接调 BaseTool.run()，而是统一 `ToolRunner.run(Actor, ToolCallRequest)`。
- **FR-8 RefundRequestTool 标准化**：把 nodes.refund_node 的 stub 逻辑抽为标准 BaseTool（`refund_request`）并注册到 ToolRegistry；category=write，要求 idempotency_key。
- **FR-9 Agent 可用工具白名单**：needs_tools 分支默认暴露 4 个：`order_query`（read）、`policy_check`（read 包装纯函数）、`exchange_request`（write）、`repair_request`（write）、`refund_request`（write）。policy_check 为只读虚拟工具，不写 DB，内部直接调 `_decide_policy(tenant_id, order_detail, intent=...)` 并把 PolicyDecision JSON 作为 Observation 返回。
- **FR-10 Agent system prompt**：前置拼入 (a) 客服角色与语气约束；(b) 政策规则说明；(c) FR-5 分支进入前 RAG 的 rag_hits 内容摘要；(d) 写工具成功后立即 Final Answer 的硬约束。
- **FR-11 Agent 循环控制**：`max_steps=5`；写工具成功调用后下一步 prompt 注入 "已写工具 X 成功，立即 Final Answer"；Agent 结束后必须产出 `final_answer: str` 类型的输出或结束标志。
- **FR-12 Agent ↔ 外层 State 边界**：
  - 入参：`{messages: [...], extra: {tenant_id, user_message, rag_hits, brand_name, ...}}`
  - 回写到外层 AgentState：`tool_executions[]`、`policy_decision`、`action_kind`、`action_result_json`、`escalated/escalation_reason`（handoff 场景由 Agent 判断工具失败后也可置 escalated）。

### ToolRunner 集成约束
- **FR-13 身份覆盖**：Agent 适配层构造 ToolCallRequest 时不传 tenant_id/user_id；即使 LLM 幻觉填了也会被 `apply_trusted_actor_override` 覆盖为 Actor（已有机制）。
- **FR-14 幂等键生成**：Agent 每步 write 工具的 idempotency_key = `f"agt-{thread_id}-{step_idx}-{salt}"`，由适配层在 `policy_check` 不生成（read）、write 类必生成。
- **FR-15 审计**：所有通过 Agent 的工具调用都在 tool_audit_logs 有记录（已有 ToolRunner 机制自动保证）。

### unknown 兜底
- **FR-16 unknown 固定文案**：unknown_node 写入 `final_reply = "抱歉，没能理解您的意思"`，llm_wrap_node 优先读取此 final_reply，跳过 BaseChatModelProvider 调用（已实现，保持不变）。

### llm_wrap 兼容
- **FR-17 llm_wrap 状态读取调整**：如果 `state.tool_executions` 存在，就把它并到 extra_context.tool_results 中；action_kind 的取值顺序保持兼容：`state.action_kind → 从 tool_executions[-1] 推断 → faq_only`。

## Non-Functional Requirements
- **NFR-1 测试兼容**：原 backend/tests 下 76 个测试至少 74 个通过，其余 2 个因逻辑本质变化需重写为等价的 Agent 语义用例后通过。
- **NFR-2 打字机流式兼容**：needs_tools 分支 Agent 子图的 LLM token 也能通过现有 NODE_STREAM / astream_events v2 映射以 token_chunk 形式透出到前端，叠字率 0（每字最多出现一次）。
- **NFR-3 步数上限**：全图（外层 + Agent 子图合并）总步数 ≤ 外层节点数 + 5，不出现无限循环。
- **NFR-4 异常降级**：Agent 适配层 / create_react_agent 异常时，外层 try/except 捕获后 fallback 为 `action_kind=faq_only` + 一条 tool_execution error 记录，最终 llm_wrap 仍能产出非 9 字兜底的自然回复。
- **NFR-5 ruff 0 告警**：所有改动文件 ruff check 通过。
- **NFR-6 无新增硬编码**：一切业务实体（订单/政策/工单/消费者）从 DB/Redis/向量库获取，不写常量。

## Constraints
- **Technical**: 技术栈不可变（AGENTS.md）：Python 3.14 + FastAPI + LangChain 0.3 + LangGraph 0.2+ + PostgreSQL + Redis + pytest + ruff。Agent 子图必须用 `langgraph.prebuilt.create_react_agent`（用户决策 1），不可手写 ReAct 循环或换其他框架。
- **Business**: unknown 必须固定文案，不得用 LLM；多租户越权统一返回 404 语义（工具层 Repository 保证）。
- **Dependencies**: ToolRunner 现有机制必须保留，禁止绕过 ToolRunner 直接调用 BaseTool.run()（AC-4 安全约束）。

## Assumptions
1. `langgraph.prebuilt.create_react_agent` 在 `langgraph>=0.2,<1.0` 中存在且 API 稳定（若 API 签名变动则在适配层 wrapper，不回退到手写循环）。
2. 现有 `MockChatModelProvider.as_runnable()` 可在 Agent 子图中作为 model 传入；如 ReAct 格式严格需要 AIMessage 中带 Action/Observation 标记，则扩展 Mock 侧生成确定性的 ReAct 文本。
3. 前端端口 5174 不变，CORS 配置不动。

## Acceptance Criteria

### AC-1: 意图分流 5 路正确性（Rule）
- **Type**: `rule`
- **Given**: 已有多租户种子数据、Facade 初始化完成（可无真实 LLM，mock 即可）
- **When**: 分别给 5 类输入
  1. "你好"（smalltalk）
  2. "人工" / "转人工" / "有图片帮我看看"（handoff）
  3. "订单号 A-ORD-202509-001 要退款"（needs_tools）
  4. "佛串是什么材质做的"（unknown，非 FAQ 白名单词 + 无订单）
  5. "7 天能退吗"（faq_only，只有规则问句无订单）
- **Then**:
  - 路由到的节点序列与 FR-5 拓扑完全一致（可通过 `astream_events` 的 node_start 事件检查）
  - 场景 1/2/4 在路由后不触发 rag_retrieve / order_query（事件流中不出现 rag_retrieve node_start）
  - 场景 3 在路由后先 rag_retrieve 再 agent（事件顺序 rag_retrieve → agent node）
  - 场景 5 在路由后 rag_retrieve → faq_node → llm_wrap
- **Pass Condition**: 5 个子场景 pytest 断言全部通过（事件 node 序列比对 + state.action_kind 正确）
- **Evidence**: `backend/tests/unit/test_task7_graph.py` 新增 `test_intent_routing_5_branches` 用例运行输出。

### AC-2: Agent 多轮工具调用（order → policy → refund 链路，Rule）
- **Type**: `rule`
- **Given**: 存在符合退款条件的订单（已签收 3 天 + 非定制 + tenant_a 政策 7 天无理由），Facade 注入 Mock LLM，返回固定 ReAct 推理链：Thought(需查单) → Action(order_query) → Obs → Thought(调用 policy_check) → Obs(can_refund=true) → Action(refund_request) → Obs(ticket) → Final Answer。
- **When**: 用户输入 "订单号 A-ORD-202509-001 我要退款"（needs_tools）
- **Then**:
  1. `tool_audit_logs` 有三条记录：order_query（succeeded）、policy_check（succeeded，虚拟工具 category=read）、refund_request（succeeded）。
  2. `idempotency_records` 有 refund_request 对应的一条记录（写工具幂等）。
  3. 外层 state.action_kind == "refund"，action_result_json.ticket_no 以 "RF-" 开头。
  4. 最终 final_reply 含 "退款工单号：" 字样 + 具体单号（llm_wrap 或 Agent final_answer 任一输出即可，llm_wrap 优先）。
- **Pass Condition**: 4 条断言全部通过。
- **Evidence**: 新用例 `test_agent_multi_step_refund_happy_path` 运行输出 + DB 审计表/幂等表 select 结果快照。

### AC-3: Agent 多轮（policy 不满足 → 引导转人工，Rule）
- **Type**: `rule`
- **Given**: 订单已签收 30 天 + 非质量问题 → policy_check 返回 can_refund=false, reason_code=human_required_out_of_window
- **When**: 用户输入 "订单号 XXX 我要退款"（needs_tools）
- **Then**:
  1. tool_audit_logs 含 order_query + policy_check，**不含** refund_request（Agent 不允许违规调用写工具）。
  2. 外层 state.escalated 为 true，或 action_kind=faq_only 且 extra_context 含引导转人工说明。
  3. 最终回复包含 "转人工" 或 "回复「人工」" 字样。
- **Pass Condition**: 3 条断言通过。
- **Evidence**: `test_agent_policy_deny_skips_write_tool` 运行结果。

### AC-4: ToolRunner 安全机制未被绕过（Rule）
- **Type**: `rule`
- **Given**: Agent 中 LLM 幻觉在 order_query params 填入 `tenant_id=tenant_b`、`user_id=其他用户`（实际 Actor 为 tenant_a/user_a1）。
- **When**: 触发 needs_tools 分支执行 Agent。
- **Then**:
  1. apply_trusted_actor_override 生效：tool_audit_logs.tenant_id == ctx.actor.tenant_id（tenant_a），非 tenant_b。
  2. order_query 若查他人订单 → 返回 ResourceNotFound（越权 404 语义），不泄露存在。
  3. write 工具调用无 idempotency_key 时被 ToolRunner 拒绝（TOOL_IDEMPOTENCY_KEY_REQUIRED），Agent 收到错误后不重试写。
- **Pass Condition**: 3 条通过。
- **Evidence**: `test_agent_tool_runner_safety_guarantees` 运行输出。

### AC-5: unknown 固定文案（Rule）
- **Type**: `rule`
- **Given**: 输入为明显 unknown 意图（如 "佛串材质" 等不在 FAQ 词表的问句）。
- **When**: 走图。
- **Then**:
  1. state.final_reply == "抱歉，没能理解您的意思"（逐字相等，不允许 LLM 改措辞）。
  2. BaseChatModelProvider.achat/astream 在 unknown 分支 **不被调用**（可通过 mock provider call_count 断言 0，或 ctx.chat_model 注入时带 counter wrapper）。
- **Pass Condition**: 2 条通过。
- **Evidence**: `test_unknown_fixed_no_llm_call` 运行结果。

### AC-6: 流式打字机不叠字（Rubric）
- **Type**: `rubric`
- **Dimension**: needs_tools 分支 SSE 流式 token_chunk 事件的文本唯一性与顺序一致性
- **Scale**: 1-5
- **Anchors**: 1 = 叠字率 > 50%（如 ItIt lookslooks）；3 = 偶有重复（≤5%），不影响理解；5 = 逐字仅一次，顺序与原文 100% 一致（可通过合并 token_chunk 拼接后 == final_reply 断言）。
- **Pass Threshold**: >= 4
- **Evidence**: `test_agent_stream_token_no_duplicate` 中，将全部 token_chunk 拼接得到 S，与 final_reply 归一化比较，字符级编辑距离 / len(final_reply) ≤ 0.01。

### AC-7: 测试回归 + 代码质量（Rule）
- **Type**: `rule`
- **Given**: 完整 backend/tests 套件。
- **When**: 运行 `cd backend && pytest -q` 和 `ruff check backend/app backend/tests`。
- **Then**:
  1. pytest 退出码 0（原 76 个中因逻辑变化需重写的 ≤ 2 个）。
  2. ruff 退出码 0，无告警。
- **Pass Condition**: 2 条通过。
- **Evidence**: 命令行运行输出拷贝。

## Open Questions
- [x] Q1: Agent 子图实现方式？→ `langgraph.prebuilt.create_react_agent`（用户决策 1）。
- [x] Q2: 政策判定放哪里？→ 调现有 `_decide_policy` 纯函数（用户决策 2）。
- [x] Q3: RAG 注入时机？→ needs_tools 进 Agent 前先 RAG（用户决策 3）。
- [x] Q4: 是否保留旧模式？→ 不保留，旧节点删除或仅作兼容包装删除（用户决策 4）。

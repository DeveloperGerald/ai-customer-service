# LangSmith 离线质量评估实施计划

## Repository Research

- **Tracing 已打通**：[main.py#L228-L258](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/main.py#L228-L258) 启动时注入 `LANGCHAIN_*`；[graph.py#L1145-L1203](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/graph.py#L1145-L1203) 构图自动挂 checkpointer 与 tracer；用户确认真实对话 trace 完整上报。
- **被测对象**：四分类图 `intent_classify →（simple_qa / handoff / knowledge_qa: policy_lookup→rag_retrieve / task: create_react_agent 子图）→ compliance_check`。
- **Facade 入口**：[facade.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/agent/facade.py) 提供 `invoke()` 与 `astream_events()`（async generator，含 `confirmation_required`、`reply_chunk`、`node_end` 等事件）。**本次评估只使用 `astream_events()`，不考虑 invoke 入口**；HITL 恢复走 `resume_stream()`（首期不启用）。
- **状态字段**：`intent_candidate`（4 大类）、`intent_hint`（refund/exchange/repair/cancel/order_status/product）、`order_ref_candidate`、`rag_hits`、`tool_executions`、`action_kind`、`action_result_json`、`escalated*`，足以支撑确定性断言。
- **LangSmith SDK**：容器内 langsmith 0.4.37，提供 `Client / AsyncClient / evaluate / aevaluate / EvaluationResult`。
- **前端**：React 18 + Tailwind，无图表库；"工具审计大屏"是 admin-only 占位页（[App.tsx#L1334-L1350](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/frontend/src/App.tsx#L1334-L1350)）。
- **演示身份固定**：3 租户 consumer 的 actor_id 在 [demo-tokens.ts](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/frontend/src/demo-tokens.ts) 中固定，已核实与 DB users 完全一致：
  - tenant_a / tenant_a_consumer / `3f88a233-4d11-50e6-926b-e0ddd2838c0c`
  - tenant_b / tenant_b_consumer / `d5f58850-d733-5150-9b07-f96fa2d53b41`
  - tenant_c / tenant_c_consumer / `ce37ef2f-e00a-54b3-ace9-9eb2cb673409`
- **DB 订单盘点（已实查 ai_cs_demo，共 24 单，买家均为上述 consumer）**：

  | 租户 | pending_payment | paid | shipped | delivered | 其他 |
  | --- | --- | --- | --- | --- | --- |
  | tenant_a | 011, 012 | 014 | 017 | 001, 002, 020 | 023 completed / 026 refunded / 029 cancelled |
  | tenant_b | 013 | 016 | 019 | 021 | 003,024 completed / 027 refunded / 030 cancelled |
  | tenant_c | — | 015 | 018 | 022 | 004,025 completed / 028 cancelled |

  订单号格式 `A/B/C-ORD-202509-XXX`，order_id 为确定 UUID（如 013 = `bbbb0202-0000-0000-0000-000000000013`）。
- **商品盘点（已实查）**：每租户 on_sale 4/3/3 条、off_sale 各 1 条，可支撑 product_list/product_query 与状态过滤用例。

## 已确认的决策

1. 手动触发，不做定时任务。
2. Judge 首期用主模型，但模型/key/base_url 全部可配置，后期可换。
3. 首期范围：意图分类、知识问答、任务类。
4. 原"工具审计大屏"改名"评估测试"，仅含离线质量评估；不做运行期工具审计区。
5. 评估不区分租户（用例自带 tenant_id，结果全局展示）。
6. LangSmith 使用 SaaS 版。

## Files and Modules

### 后端新增

- `backend/app/evals/__init__.py`
- `backend/app/evals/cases/*.json`：golden 用例（`intent.json` / `knowledge.json` / `task.json`），用 stdlib json，不引入 pyyaml。
- `backend/app/evals/schema.py`：用例结构 Pydantic 模型（case_id、tags、tenant_id、message、expected：intent/hint/tool/tool_args/reference_answer/must_contain/must_not_contain、auto_confirm）。
- `backend/app/evals/dataset.py`：本地用例 ↔ LangSmith Dataset 同步（按 metadata.case_id diff：新增/更新/删除）。
- `backend/app/evals/target.py`：aevaluate 的 async target：建临时线程 → **仅消费 `facade.astream_events()`** 收集结果（final_reply、node_end patch、tool 事件）；遇 `confirmation_required` 时记录 pending（首期不 resume、不真正执行写操作，断言"正确暂停"即终点）。
- `backend/app/evals/evaluators.py`：
  - 确定性：`intent_match`（大类 + hint）、`task_tool_match`（选对工具 + 关键参数 + 写操作必先暂停）、`answer_contains`（must_contain / must_not_contain）。
  - LLM-as-judge：`knowledge_correctness`（对照参考答案打分 0~1）、`faithfulness`（结论必须由 rag_hits 支撑）。
- `backend/app/evals/judge.py`：Judge 模型客户端（OpenAI 兼容协议），配置缺省时继承 LLM 设置，输出结构化 JSON（带 1 次重试）。
- `backend/app/evals/runner.py`：编排入口 `async run_evaluation(tags=None)`：同步数据集 → aevaluate → 返回实验名/URL；模块级并发锁与运行状态。
- `backend/app/api/evaluations.py`：admin-only 接口（见下）。

### 后端修改

- `backend/app/config.py`：新增 `EvaluationSettings`（dataset_name、judge_model=""（空=主模型）、judge_api_key/base_url 可选覆盖、concurrency=4），挂到 `Settings.evaluation`，环境变量前缀 `EVALUATION__`。
- `backend/app/main.py`：注册 evaluations 路由。
- `backend/app/application/agent/facade.py`：**零改动**；target 仅作为现有 `astream_events()` 的消费方。

### 前端修改

- `frontend/src/App.tsx`：NavKey `audit` → `eval`，标签改"评估测试"（🧪），仍 admin-only；删除 PlaceholderPage 的 audit 分支，接入真实页面。
- `frontend/src/components/EvaluationPage.tsx`（新增）：
  - 「运行评估」按钮 → POST 触发，运行中每 2s 轮询状态并显示进度；并发禁用按钮。
  - 实验列表下拉选择；实验详情：各评估器汇总分（纯 CSS 进度条/百分比）、用例明细表（输入、期望、实际输出、各指标对错、judge 理由、LangSmith trace 跳转链接）。
  - 仅显示必要信息，不加描述性文案。

### API 设计

- `POST /api/evaluations/run`（body 可选 tags）→ `{status, experiment_name}`；已有运行中 → 409。
- `GET  /api/evaluations/status` → `{status: idle|running|done|failed, total, completed, error, experiment_name, url}`。
- `GET  /api/evaluations/experiments` → 最近实验列表（name、时间、数据集、用例数、url）。
- `GET  /api/evaluations/experiments/{name}` → 汇总：各评估器平均分、用例级明细（输入/期望/输出/各 feedback 分数与理由/trace url）。

## 用例规划（首期约 35 条）

- **意图分类 ~12 条**：覆盖 4 大类；task/knowledge 用例同时断言大类与 hint；含寒暄、投诉转人工、政策咨询、含订单号的售后申请。
- **知识问答 ~13 条**：从现有 FAQ 文档出题，含参考答案与关键事实点；覆盖政策固定题（走 policy_lookup）与 RAG 题；2 条边界题（知识库无答案时不应编造）。
- **任务类 ~10 条**（用例直接绑定上表真实订单/商品）：
  - 只读订单：order_query 查 A-ORD-202509-001（tenant_a，delivered），断言工具选择与订单参数。
  - 商品：product_list（tenant_a 在售列表）、product_query（按 SKU 查具体商品），断言工具与参数。
  - 写操作（均断言：选对写工具、订单参数正确、**先产生 confirmation_required 暂停且不真正写库**）：
    - refund：A-ORD-202509-014（tenant_a，paid）、B-ORD-202509-021（tenant_b，delivered）
    - exchange：A-ORD-202509-002（tenant_a，delivered）、C-ORD-202509-022（tenant_c，delivered）
    - repair：B-ORD-202509-019（tenant_b，shipped）
    - cancel：B-ORD-202509-013（tenant_b，pending_payment）、A-ORD-202509-011（tenant_a，pending_payment）
  - 写操作前置校验依赖订单当前状态；评估期间不执行 resume，订单状态不会被评估改变。

## Implementation Steps

1. `EvaluationSettings` 配置 + `.env.example`（根 & backend）补项。
2. 用例 schema + JSON 用例文件（订单/商品已实查，task 用例按上表具体单号绑定）。
3. dataset 同步：Client 建数据集/示例的 diff 逻辑。
4. Judge 客户端（可配置模型，缺省继承主模型）。
5. 评估器：先 3 个确定性，再 2 个 LLM judge。
6. Target：临时线程 + astream_events 收集 + confirmation 识别。
7. Runner：aevaluate 编排 + 并发锁 + 运行状态。
8. HTTP API：run/status/experiments/detail。
9. 前端：导航改名 + EvaluationPage（触发/轮询/汇总/明细）。
10. 单测与全量验证。

## Dependencies and Considerations

- langsmith 已是环境依赖（tracing 已验证），不加新第三方包；用例用 JSON。
- Target 直接走真实 DB/Redis/向量库全链路（符合"强依赖不兜底"约定）；每用例独立临时线程，避免互相污染。
- Eval actor：用租户固定 consumer（demo actor_id），helper 保证 user 存在（缺失则创建），保证写工具的订单归属校验可通过。
- 实验结果聚合优先用 LangSmith Client（list projects/runs/feedback）；实施时先验证 0.4.37 的实际方法名，必要时通过 Redis 记录本地实验元数据兜底。
- 评估会产生以 consumer 身份命名的临时会话行；接受该噪声（真实路径一致），标题统一前缀便于辨识。
- 前端无图表库，指标条用 Tailwind 实现，不新增依赖。

## Validation

- 新增单测（mock Client/facade）：dataset diff、确定性评估器对错分支、target 事件收集与暂停识别、4 个 API 的正常/并发冲突路径；遵循现有 `test_task10_agent_http.py` 的 override 模式。
- `cd backend && pytest` 全量回归（记录两条已知 pre-existing 401 失败与本次无关）。
- ruff / mypy 项目脚本检查。
- 手动端到端：页面触发实验 → 轮询到完成 → 查看汇总与明细 → 点击 trace 链接跳转 LangSmith SaaS 验证。

## Risks

- **LangSmith 列表/feedback API 与版本有差异**：实施第一步先在 venv 内核实方法签名；聚合接口以 feedback 数据为准，本地 Redis 元数据兜底实验列表。
- **Judge JSON 输出不稳定**：强约束 prompt + 1 次重试；仍失败则该条评估记 error，不中断整轮实验。
- **写工具前置校验失败（政策不允许/状态不符）导致未产生暂停**：用例已按订单状态矩阵选择；个别因业务规则被拒属于被测系统真实输出，评估器如实记失败，不绕过校验、不改业务状态机。
- **后台任务阻塞/泄漏 session**：runner 统一在 finally 关闭 session；并发锁保证同时只有一个实验；任务持有 app 强引用避免被 GC。

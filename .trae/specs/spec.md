# 手串售后智能客服 Agent - 面试演示 MVP 规范

## Overview
- **Summary**: 构建一个面试演示用的多租户手串售后智能客服 MVP。优先跑通核心技术亮点链路，预留清晰的扩展接口，在有限时间内最大化展示架构设计能力与工程质量。
- **Purpose**: 用于技术面试演示，展示多租户隔离、LangGraph Agent 编排、RAG 知识溯源、安全工具调用、状态持久化恢复、SSE 流式输出等关键能力。
- **Target Users**: 面试官、技术评审人员。

## Goals
1. **跑通 3 条核心演示链路**（政策问答 + 订单物流查询 + 模拟退款流程），覆盖面试最关心的技术点
2. **架构分层清晰**，代码结构体现分层设计思想，预留未实现模块的扩展占位
3. **关键安全约束落地**（多租户隔离、工具幂等与审计、写操作确认）
4. **工程质量达标**（类型注解、Google docstring、单元测试、ruff 规范）
5. **可运行 demo**：Docker Compose 一键启动，有种子数据，演示脚本可复现

## Non-Goals
1. 不实现完整 5 条链路的所有细节（人工交接仅实现 schema 和占位逻辑，不做队列 UI；多轮流式仅做基础版）
2. 不做真实 LLM/embedding 采购和线上调优（用 mock 或轻量模型，配置留真实接入位）
3. 不做生产级高可用、容灾、压力测试
4. 不做复杂工单后台、真人接管回复功能
5. 不做 320 条完整评测集（仅覆盖核心链路的关键测试）
6. 不做图片识别/上传入口（按 D4 说明不接收文件）

## Background & Context
- 项目技术栈已锁定：Python 3.14 + FastAPI + LangChain + LangGraph + PostgreSQL(pgvector) + Redis + React + Vite + SSE + pytest + Docker Compose
- 所有基础文件目前为空，从零开始
- D1-D4 已确认：
  - D1：首版优先 5 条核心链路（MVP 裁剪为 3 条完整 + 2 条占位）
  - D2：人工交接模拟入队，客服可看上下文，首版不实现真人回复
  - D3：明确的转人工操作立即执行（无需额外用户确认，但仍需鉴权、幂等、审计）
  - D4：首版不接收文件，说明为模拟交接，不显示不可用上传入口
- 编码规范：类型注解 + Pydantic v2 + Google docstring + async/await + structlog JSON + 自定义异常 + .env 配置

## Functional Requirements

### 核心链路（完整实现）
- **FR-1 政策问答（RAG）**：三租户对同一问题返回各自租户的政策引用，无依据时明确说明不编造。检索强制 tenant_id 过滤。
- **FR-2 订单物流查询**：缺少 order_no 时追问；仅查询当前消费者有权限的订单；跨租户/同租户他人订单不可见。
- **FR-3 模拟退款（写操作安全）**：查订单 → 获取政策依据 → 校验资格/金额 → 展示确认卡片 → 用户显式确认（action_id） → 创建模拟申请 → 可查询状态。全程校验幂等、审计、确认生命周期。

### 占位链路（schema + 空实现 + 预留接口）
- **FR-4 人工交接（占位）**：定义完整 schema、迁移、状态枚举；工具框架中注册 handoff 工具；调用时返回"已模拟入队"状态；waiting_human 状态阻止自动业务写入；不做队列查询 UI。
- **FR-5 多轮状态恢复（基础）**：checkpoint 持久化；断线后 request 查询返回结果；session 内槽位保留。

### 工程基础设施
- **FR-6 身份与租户**：演示令牌签发/校验；请求头 tenant_id 与 token 绑定校验；三租户种子账号（消费者/客服/管理员）。
- **FR-7 工具框架**：BaseTool 协议 + allowlist；Pydantic 参数校验；可信身份注入（不从模型参数取身份）；工具审计日志（成功/失败/拒绝）；通用幂等中间件。
- **FR-8 Agent 编排（LangGraph）**：身份校验节点 → 路由分支（RAG/订单/退款/交接）→ 确认暂停/恢复 → 统一生成节点；步数上限终止。
- **FR-9 HTTP + SSE**：聊天输入 schema；8 种 SSE 事件（meta/token/citation/tool_call/tool_result/confirmation/handoff/error/done）；会话/请求查询接口。
- **FR-10 前端（基础演示 UI）**：身份选择入口；聊天窗口；引用展示；确认卡片组件；工具执行状态提示。
- **FR-11 可观测（基础）**：structlog JSON 日志绑定 request/session/trace_id；敏感字段脱敏；LangSmith 接入配置位（可关闭降级）。

## Non-Functional Requirements
- **NFR-1 代码结构清晰度**：目录分层一目了然（api / core / domain / infrastructure / agents / tests），新读者 5 分钟内能看懂架构。
- **NFR-2 类型安全**：所有函数有类型注解，Pydantic v2 schema 全覆盖接口和状态。
- **NFR-3 测试覆盖核心**：关键安全约束（越权、幂等、确认绕过）有 pytest 单测；路由主要分支覆盖。
- **NFR-4 一键可运行**：`docker compose up` 后 2 分钟内能打开 UI，跑通 demo 脚本。
- **NFR-5 预留扩展点明确**：未实现模块通过抽象基类/协议/占位文件标明扩展位置，注释说明实现思路。
- **NFR-6 无硬编码密钥**：所有配置通过 .env / 环境变量注入。

## Constraints
- **Technical**: 严格使用 AGENTS.md 锁定的技术栈，不引入额外替代框架（如不用 Django 替代 FastAPI，不用 MySQL 替代 PostgreSQL）。
- **Business**: 订单/退款/物流为模拟业务，不对接真实外部系统；不做图片识别；人工交接为模拟入队。
- **Dependencies**: 优先验证 LangGraph PostgreSQL checkpointer 与 Python 3.14、pgvector 的兼容性。

## Assumptions
1. 面试演示环境有 Docker Compose，可访问外部 LLM API（若模型 mock 也可接受）。
2. 面试官可接受"此模块已预留扩展点，完整实现思路为 xxx"的说明，不要求逐行代码。
3. 演示账号使用服务器签发的固定演示令牌，不做真实登录/注册。

## Acceptance Criteria

### AC-1: 政策问答多租户隔离
- **Type**: `rule`
- **Given**: 三租户 tenant_a/b/c 各有差异化政策种子文档，已导入分块向量化
- **When**: 分别用三租户合法身份提问同一政策问题（如"七天无理由退换政策是什么"）
- **Then**: 每个返回的 citation 仅来自对应租户知识库；同问句三租户答案内容与各自政策一致；不出现跨租户引用
- **Pass Condition**: pytest 测试用例 `test_rag_tenant_isolation` 断言通过，人工抽查 citation 无越权
- **Evidence**: `tests/test_rag.py` 运行输出 + 三次 API 调用响应的 citation 列表

### AC-2: 订单查询权限隔离
- **Type**: `rule`
- **Given**: tenant_a 有消费者 user_a1（订单 OA1、OA2）和 user_a2（订单 OA3）；tenant_b 有订单 OB1
- **When**:
  1. user_a1 查询 OA1（自己的）→ 返回详情
  2. user_a1 查询 OA3（同租户他人）→ 返回统一不可见（不泄露是否存在）
  3. user_a1 查询 OB1（跨租户）→ 返回统一不可见
  4. user_a1 不提供 order_no → 追问获取
- **Then**: 四种场景均符合预期
- **Pass Condition**: pytest `test_order_permission_*` 4 个用例全部通过
- **Evidence**: `tests/test_orders.py` 运行输出

### AC-3: 退款确认生命周期与幂等
- **Type**: `rule`
- **Given**: 存在符合退款资格的订单，用户走完资格校验
- **When**:
  1. 返回 confirmation 卡片（含 action_id、金额、订单、政策依据）
  2. 未用 confirm 事件、仅发"好的"消息 → 不触发退款写入
  3. 用 confirm+正确 action_id → 创建一条模拟退款申请，action 和 refund 表均有记录
  4. 同一 action_id 重复 confirm → 返回原结果，不重复创建（查 idempotency_records）
  5. 已确认 action 改金额再确认 → 冲突，旧确认失效
  6. cancel 事件 + action_id → pending 转 cancelled，不创建退款
- **Then**: 6 个子场景全部符合预期
- **Pass Condition**: pytest `test_refund_lifecycle_*` 6 个用例全部通过
- **Evidence**: `tests/test_refunds.py` 运行输出 + 数据库表记录快照

### AC-4: 工具调用审计与身份注入
- **Type**: `rule`
- **Given**: allowlist 中注册 order_query 和 refund_create 工具
- **When**:
  1. 合法调用 order_query → tool_audit_logs 写入成功记录（含脱敏参数摘要、trace_id、身份）
  2. 模型在参数中指定其他用户的 user_id → 工具框架拒绝，注入服务端身份，记录拒绝审计
  3. 调用未注册工具 → allowlist 拒绝，审计记录拒绝
  4. 工具内部抛异常 → 记录失败审计，错误分类正确
- **Then**: 每次调用 tool_audit_logs 均有对应记录，身份/状态/参数分类正确
- **Pass Condition**: pytest `test_tool_audit_*` 4 个用例通过
- **Evidence**: `tests/test_tools.py` 运行输出 + audit 表记录

### AC-5: LangGraph 编排流程正确性
- **Type**: `rule`
- **Given**: FastAPI 应用 + LangGraph 图已初始化，可处理会话请求
- **When**:
  1. 政策问答消息 → 经过 identity → route → rag → generate 节点，返回 citation + answer
  2. 订单查询缺 order_no → identity → understand → clarify 节点，返回追问
  3. 完整退款请求 → identity → order_query → policy_check → pending_action_create → pause（返回 confirmation）→ confirm 事件 → refund_write → generate
  4. 超过步数上限 → 终止并返回超限错误
- **Then**: 节点执行路径符合预期，状态机正确流转
- **Pass Condition**: pytest `test_graph_routes_*` 通过，LangSmith/LangGraph trace 节点顺序正确
- **Evidence**: `tests/test_graph.py` 运行输出 + trace 节点日志

### AC-6: 架构分层与扩展点清晰度
- **Type**: `rubric`
- **Dimension**: 代码结构分层清晰程度与扩展点可识别性
- **Scale**: 1-5
- **Anchors**:
  - 1 = 目录混乱，所有代码堆在 main.py，看不出分层；未实现模块完全缺失
  - 3 = 有基本目录（api/domain/infrastructure），但模块间耦合明显；未实现模块无占位
  - 5 = 严格分层（api → application → domain → infrastructure），依赖方向清晰（外层依赖内层抽象）；未实现模块（工单/换货/真人接管）有抽象基类占位文件，写明实现思路注释
- **Pass Threshold**: >= 4
- **Evidence**: 目录树截图 + 关键占位文件内容（如 `backend/app/domain/services/abc.py` 中 BaseTicketService 抽象）

### AC-7: 代码工程质量
- **Type**: `rubric`
- **Dimension**: 类型安全、文档、测试规范程度
- **Scale**: 1-5
- **Anchors**:
  - 1 = 基本无类型注解，函数无 docstring，没有 pytest 测试
  - 3 = 核心路径有类型注解和 docstring，少量 happy path 测试通过，ruff 有少量告警
  - 5 = 所有公开函数有 Google docstring + 类型注解，Pydantic schema 覆盖所有接口/状态；安全约束单测覆盖；ruff 0 告警，pytest 核心测试全部通过
- **Pass Threshold**: >= 4
- **Evidence**: `pytest` 运行结果 + `ruff check` 结果 + 核心模块抽样检查

### AC-8: Docker Compose 一键可演示
- **Type**: `rule`
- **Given**: 空环境安装 Docker Compose，填写 .env（可跳过真实 LLM key，启用 mock）
- **When**: 执行 `docker compose up --build`，等待启动完成
- **Then**:
  1. 后端 `/health` 返回 200
  2. 前端页面可打开，显示身份选择入口
  3. 选择 tenant_a 消费者身份，输入"退换货政策是什么"，得到带引用的回答
  4. 输入"查订单 A1001"（种子订单）返回物流详情
  5. 按演示脚本走完退款流程（确认 → 成功）
- **Pass Condition**: 5 个手动步骤全部完成，不出现需要手动改代码/改 SQL 的报错
- **Evidence**: 终端启动日志截图 + 5 步演示过程的响应记录

### AC-9: 多轮状态恢复基础能力
- **Type**: `rule`
- **Given**: 会话 S1 走到退款 confirmation 暂停状态（checkpoint 已保存，action_id=ACT123），进程退出重启
- **When**: 客户端发送 confirm 事件 + action_id=ACT123 + session_id=S1（带 request_id）
- **Then**: 从 checkpoint 恢复，直接执行 refund_write，不重新跑前面节点；最终创建退款记录，幂等保证
- **Pass Condition**: pytest `test_checkpoint_recovery_refund` 通过（模拟进程重启）
- **Evidence**: `tests/test_checkpoint.py` 运行输出 + 两次 run_id 对比（重启后新 run_id 但复用 action 结果）

### AC-10: 人工交接占位与状态阻断
- **Type**: `rule`
- **Given**: 用户发"转人工"或"珠子裂了"明确交接触发词
- **When**: 调用 handoff 工具
- **Then**:
  1. handoffs 表写入交接记录（含上下文引用、幂等键、交接原因）
  2. 会话状态转 waiting_human
  3. SSE 发出 handoff 事件，说明"已模拟入队，首版不提供真人回复"
  4. waiting_human 状态下再次要求退款 → 拒绝自动写入，提示已在交接中
  5. 重复交接请求（同会话同原因）→ 复用记录，不重复写 handoffs
- **Pass Condition**: pytest `test_handoff_*` 5 个用例通过
- **Evidence**: `tests/test_handoff.py` 运行输出 + handoffs 表记录

## Open Questions
- [x] D1: 首版范围（五条链路优先，MVP 裁剪为 3 完整+2 占位）→ 已确认
- [x] D2: 人工交接深度（模拟入队，不做真人回复）→ 已确认
- [x] D3: 转人工是否额外确认（明确转人工立即执行，但仍需鉴权/幂等/审计）→ 已确认
- [x] D4: 图片入口（不接收文件，无上传 UI）→ 已确认
- [ ] 真实 LLM/embedding 供应商偏好？（默认用 mock 可切换，配置留接入位，不影响 MVP 交付）

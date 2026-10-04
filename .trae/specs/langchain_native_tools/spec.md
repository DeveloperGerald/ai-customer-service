# 工具层 LangChain 原生模式重构 - PRD（激进精简版）

## Overview
- **Summary**: 彻底删除自定义 `BaseTool` ABC、手写 `param_schemas` dict、`langchain_adapter.py` 适配层与 `DuckTool` / `HAS_LANGCHAIN` 兼容分支。全新 `GuardedTool` **直接继承 `langchain_core.tools.BaseTool`**，在 `_arun` 统一入口挂接三大硬约束（可信身份/审计/幂等），强制运行环境必须安装 `langchain_core`（HAS_LANGCHAIN 恒为 true，不做离线兼容）。
- **Purpose**: 把 4 层包装（自定义 BaseTool→手写 param_schemas dict→逆向重建 Pydantic→StructuredTool.from_function）压缩为 **1 层直接继承 LangChain BaseTool**，代码净删除 ≥ 150 行，面试时能演示"LangChain 原生 + 企业级约束装饰器注入"的清晰叙事。
- **Target Users**: 面试评审者（看代码架构）；后续维护者（读工具定义零认知负担）。

## Goals
1. **工具定义 = 纯 LangChain 写法**：每个工具只需 `name` / `description` / `args_schema`（Pydantic 类） / `_execute_impl(ctx, args)`，与 LangChain 官方文档完全一致。
2. **三条企业级硬约束零改动失效**：可信身份覆盖、`tool_audit_logs` 审计、写工具 `idempotency_records` 幂等缓存与冲突检测，行为与重构前字节级对齐。
3. **全链路接口兼容**：HTTP (`/api/tools` list/call)、Agent `_rule_based_react_pipeline` 鸭子调用、`create_react_agent` 标准工具输入三者契约不变。
4. **激进代码删除**：`langchain_adapter.py` 整体删除、旧 `BaseTool` 抽象类整体删除、`_build_args_model` / `_build_single_adapted_tool` / `DuckTool` / `HAS_LANGCHAIN` 全部删除。
5. **接受少量测试代码改写**：`test_task4_tools.py` 中自定义 `OKTool` 子类从继承旧 `BaseTool` 改为继承新 `GuardedTool`，业务断言零修改。

## Non-Goals
1. 不改变 4 个内置工具的业务逻辑（order_query/exchange/repair/refund 的校验、工单号前缀、返回字段）。
2. 不替换 `ToolRunner` 内部 SQL 审计/幂等实现（仅重构调用接口与 GuardedTool 绑定方式）。
3. 不重写 `_decide_policy` 纯函数（policy_check 仅调整包装方式）。
4. 不新增第三方依赖（仅要求已有的 `langchain_core` 必须 import 成功，不再 try-except 降级）。

## Background & Context
- **现有链路（4 层包装冗余）**：
  - [base.py:82 `BaseTool`](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/base.py#L82-L120)：抽象类，要求手写 `param_schemas: list[dict]` + `run(ctx, arguments: dict)` 自己解包。
  - [builtin.py](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/builtin.py)：4 个工具各自手写 10~25 行 `param_schemas` dict（描述同名字段 name/type/required/enum/description）。
  - [langchain_adapter.py:56 `_build_args_model`](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/langchain_adapter.py#L56-L82)：用 `pydantic.create_model` 把上面的 dict **逆向重建回 Pydantic BaseModel**（30 行胶水）。
  - [langchain_adapter.py:233 `_build_single_adapted_tool`](file:///Users/w7v2i9aa2/Workspace/ai-customer-service/backend/app/application/tools/langchain_adapter.py#L233-L269)：再用 `StructuredTool.from_function` 包第 4 层（40 行胶水）。
  - 同时为离线测试写了 `HAS_LANGCHAIN` try-except + `DuckTool` 鸭子类（复制两份 50 行胶水）。
- **硬约束保留（AGENTS.md / project_memory 无商量余地）**：
  1. 可信身份覆盖：`apply_trusted_actor_override` 必须被调用，`tenant_id/user_id/buyer_id` 等身份键永远以 Actor 为准。
  2. 审计：每次工具调用必写 `tool_audit_logs`（running→succeeded/failed + duration_ms）。
  3. 幂等：写工具检查/写入 `idempotency_records`，同 key 不同 arguments 报 409 冲突。
  4. policy_check：虚拟工具，不注册到 ToolRegistry，不写 DB，调 `_decide_policy` 纯函数。
- **用户明确选择**：方案 C（直接继承 LangChain BaseTool） + 强制 HAS_LANGCHAIN=true（离线 DuckTool 整条链路删除，不兼容） + 激进精简（删除旧 BaseTool，接受测试改写）。

## Functional Requirements
- **FR-1 GuardedTool 基类（直接继承 langchain_core.tools.BaseTool，不做任何兼容降级）**
  - 顶部 `from langchain_core.tools import BaseTool as _LCBaseTool`（直接 import，失败就 ImportError，**不再 try-except HAS_LANGCHAIN**）。
  - `GuardedTool(_LCBaseTool)` 抽象类：
    - 新字段 `category: Literal["read","write","escalation"]`，默认 "read"（使用 Pydantic 类属性；与旧 BaseTool.category 语义完全一致）。
    - 新字段 `args_schema: ClassVar[type[BaseModel]]` 或 Pydantic Field（LangChain 原生写法，**替代旧 param_schemas**）。
    - 新方法 `requires_idempotency_key() -> bool`：`category in {"write","escalation"}`。
    - 抽象方法 `async def _execute_impl(self, ctx: ToolExecutionContext, args: BaseModel) -> dict[str, Any]`（业务实现直接拿强类型 args 对象，不再 dict.get）。
    - 最终方法 `async def _arun(self, *args, **kwargs)`（覆写 LangChain BaseTool 的统一入口）：从 runtime binding 取 actor/session/adapter_ctx → 校验 args → 调用 ToolRunner 等价流水线 → return observation json str。
    - 公共方法 `def bind_runtime(self, actor, session, adapter_ctx) -> "GuardedTool"`：就地绑定运行时上下文，返回 self（**不通过 RunnableConfig 透传**，显式绑定更直观、面试好讲）。
- **FR-2 ToolRunner 流水线在 GuardedTool._arun 中复用（三条硬约束无差别挂接）**
  - `_arun` 中：
    1. 构造 Pydantic args：`self.args_schema.model_validate(kwargs or args)`（与 LangChain 默认传入策略一致）。
    2. 生成 idempotency_key：写工具自动 `adapter_ctx.step_counter += 1` → 格式 `agt-{thread_id}-{step_idx}-{salt}`（沿用旧格式，保证缓存命中）。
    3. 委托 ToolRunner.run()：构造 `ToolCallRequest(tool_name=self.name, arguments=args_dict, idempotency_key=idem_or_none, session_id=thread_id)`，用已绑定的 session 构造 `ToolRunner(session, registry=SingleToolRegistry(self))` 完整跑**完全相同**的流水线代码（即旧 runner.py 的 8 步），**不重写任何审计/幂等 SQL**。
  - `_format_observation`（旧 adapter.py:99）逻辑下沉到 base.py 成为模块级函数，供 GuardedTool 调用。
- **FR-3 ToolDefinition.params 自动从 args_schema.model_json_schema() 推导（替代 param_schemas）**
  - `GuardedTool.definition() -> ToolDefinition`：
    - `params`：遍历 `args_schema.model_json_schema()["properties"]` 的键 → 映射成 list[ToolParamSchema]（支持 string/integer/number/boolean/array/object 六种 type，从 JSON Schema type 映射；支持 required 从 `model_json_schema()["required"]` 取；支持 enum 从 JSON Schema enum 取；description 原样带）。
    - `category` / `name` / `description`：与旧 BaseTool.definition 一致。
  - ToolRegistry.list_definitions() 零改动（直接调用每个 GuardedTool.definition()）。
- **FR-4 4 个内置工具迁移（独立 Pydantic Args 类 + _execute_impl 强类型）**
  - 新建 4 个独立 Pydantic Args 类（`OrderQueryArgs` / `ExchangeRequestArgs` / `RepairRequestArgs` / `RefundRequestArgs`），字段的 description / enum / required / Optional 与旧 `param_schemas` dict 语义逐字节对齐。
  - 4 个工具类改为继承 GuardedTool：**删除 `param_schemas` 属性**，`name/description/category` 仍为类字段，`args_schema = OrderQueryArgs`，业务实现改 `_execute_impl(ctx, args: OrderQueryArgs)` → 直接用 `args.order_id`、`args.reason` 等属性，不再 `arguments.get()`。
  - `build_default_registry()` 注册顺序、工具名不变。
- **FR-5 policy_check 虚拟工具：独立 GuardedTool 子类（不 register，不写 DB）**
  - 新建 `PolicyCheckTool(GuardedTool)`：
    - `name="policy_check"`，category="read"，args_schema = 单独的 `PolicyCheckArgs(order_detail: dict, intent_hint: Optional[Literal["refund","exchange","repair"]]="refund")`（描述字段保留旧文本）。
    - `_execute_impl(ctx, args)`：同步调用 `_decide_policy(tenant_id, args.order_detail, intent=args.intent_hint)`，返回 `decision.to_state_json()`。
    - **不走 ToolRunner 流水线**（不写 audit_logs、不触发幂等），改为在 `_arun` 内直接执行并写入 `adapter_ctx.call_history`（保持旧 behavior）。
- **FR-6 AdapterContext + build_langchain_tools 下沉到 base.py，删除 langchain_adapter.py**
  - `AdapterContext` dataclass（actor / tool_runner_ref? / thread_id / tenant_id / policy_override / salt / step_counter / call_history）整体从 langchain_adapter.py 移到 base.py。
  - `build_adapter_context(**kwargs) -> AdapterContext` 同步移动。
  - `build_langchain_tools(adapter_ctx) -> list[GuardedTool]`：
    1. 白名单 `["order_query","exchange_request","repair_request","refund_request"]` 从全局 ToolRegistry 取出 GuardedTool 实例。
    2. **每个工具调用 .bind_runtime(adapter_ctx.actor, session_from_runner?, adapter_ctx)**，保证 `_arun` 里上下文已就绪。
    3. 单独 new 一个 `PolicyCheckTool()` 并 `.bind_runtime(actor, session_ref, adapter_ctx)`，**不 register**，append 到末尾，顺序 = 白名单 4 个 + policy_check（与旧版完全一致）。
  - 删除 `langchain_adapter.py` 文件。
- **FR-7 鸭子调用契约不变（给 _rule_based_react_pipeline 用）**
  - GuardedTool 对象上必须仍有：
    - `t.args_schema`：Pydantic 类（LangChain BaseTool 默认即有，无需额外）。
    - `t.ainvoke(input: dict | BaseModel) -> str`：LangChain BaseTool 默认 ainvoke 返回 _arun 的 return（我们 _arun 返回 observation 字符串，完美匹配）。
    - `t.name` / `t.description`：同上。
  - 旧 adapter 代码里 `t.coroutine(**kwargs)` 鸭子属性若被测试用到，可在 GuardedTool 里加 `@property def coroutine(self)` 返回 `self._arun`（轻量兼容，不写 if/else 分支）。
- **FR-8 HTTP API 契约兼容（list_definitions + ToolRunner.run）**
  - `GET /api/tools` 仍调用 `build_default_registry().list_definitions()`，字段集合 name/description/category/params[].{name,type,description,required,enum} 与旧版 JSON Schema 完全一致。
  - `POST /api/tools/call` 仍走 `ToolRunner(session, registry).run(actor, ToolCallRequest)`，为兼容此路径：
    - ToolRunner 内部把 `tool.run(ctx, arguments_dict)` 调用改成：
      ```python
      if isinstance(tool, GuardedTool):
          args = tool.args_schema.model_validate(arguments_dict)
          data = await tool._execute_impl(ctx, args)
      else:
          # 旧分支直接删除，不再存在 else（激进策略）
          raise TypeError("only GuardedTool supported")
      ```
    - 等价于 ToolRunner 只接受 GuardedTool（删除旧 BaseTool 抽象兼容）。
- **FR-9 测试改写范围（仅 test_task4_tools.py 中 OKTool 子类）**
  - test_task4_tools.py:247 自定义测试子类 `OKTool(BaseTool)` → 改为 `OKTool(GuardedTool)`。
  - `OKTool.param_schemas` → 改为定义独立 Pydantic `OKToolArgs(x: str)` + `args_schema = OKToolArgs`。
  - `OKTool.run(ctx, arguments: dict)` → 改为 `_execute_impl(ctx, args: OKToolArgs) -> {"ok": True, "x": args.x}`。
  - **其余断言（身份覆盖/幂等必填/缓存命中/冲突/审计落表）完全不改写**，保证测试语义对齐。

## Non-Functional Requirements
- **NFR-1 零语义回归**：pytest 99+ 用例全部通过（仅 test_task4_tools.py:247 类继承调整）；`test_agent_refactor_ac.py` 退款 Happy Path、政策拒绝、unknown 固定回复三条用例断言零修改通过。
- **NFR-2 代码净删除 ≥ 150 行**：base.py + builtin.py + runner.py 合计 LOC（扣除空行/注释）相比重构前净减少 ≥ 150 行；且 `langchain_adapter.py`（360+ 行）被整个文件删除。
- **NFR-3 面试叙事清晰**：
  - 第一句话术：「这 4 个工具都是直接继承 LangChain BaseTool 的，声明 name、description、args_schema、实现一个方法就够了；身份/审计/幂等三件套统一在 GuardedTool._arun 入口挂接，业务工具零感知」。
  - 每个工具完整定义（不含 docstring） ≤ 25 行代码。
- **NFR-4 Ruff 零违规，类型注解完整**：ruff check `backend/app backend/tests` 无 issue；所有函数 Google 风格 docstring（保持一致风格）。
- **NFR-5 LangSmith Trace 更强**：因为 GuardedTool 本身是 LangChain Runnable，每次工具调用会自动生成 LangSmith Tool Span，**不再需要额外适配**（比之前更好，这是原生模式的福利）。

## Constraints
- **Technical**: `from langchain_core.tools import BaseTool` 不得 try-except HAS_LANGCHAIN，ImportError 直接抛（强制满足用户要求）。
- **Technical**: 删除旧 `BaseTool` 抽象类、`param_schemas` 属性、`DuckTool` 类与相关 if/else 分支，不保留"两套工具类并存"的中间兼容态。
- **Technical**: 上下文注入**选显式 `.bind_runtime()` 模式**，不选 RunnableConfig.configurable 透传（更直观 + 面试讲法清晰）。
- **Technical**: ToolRunner 的 SQL 审计/幂等实现代码（runner.py:56-324）**不允许重写**，只能 GuardedTool 委托调用，保证数据层零语义漂移。
- **Business**: policy_check 工具永远不得写入 `tool_audit_logs` 或 `idempotency_records`（虚拟工具属性）。
- **Business**: 退款工单号前缀 `RF-`、换货 `EX-`、维修 `RP-`、格式 `{PREFIX}-{tenant_id.upper()}-{call_id.hex[:8].upper()}` 保持不变。

## Assumptions
1. 测试环境（pytest / CI）在此次重构后会安装现有的 `langchain_core` 依赖（项目已有，仅删除 try-except 不装也会报错）。
2. LangChain 0.3+ 版本 `BaseTool._arun(self, *args: Any, **kwargs: Any)` 签名稳定且与 ainvoke 语义一致。
3. `GuardedTool.bind_runtime()` 就地绑定 actor/session/adapter_ctx 后不会因为 LangChain 内部 ainvoke 机制重新复制实例而丢失（若复制则改为 return 新的 shallow copy，bind 返回新对象）。

## Open Questions
- 无（用户已明确方案 C + 强制 HAS_LANGCHAIN + 激进精简）。

---

## Acceptance Criteria

### AC-1 GuardedTool 直接继承 LangChain BaseTool + HAS_LANGCHAIN 恒真
- **Type**: `rule`
- **Given**: 新 GuardedTool 基类完成
- **When**: 静态检查 + 运行时导入
- **Then**: ① `issubclass(GuardedTool, langchain_core.tools.BaseTool)` 为 True；②全项目 grep `HAS_LANGCHAIN\|DuckTool\|_build_args_model\|_build_single_adapted_tool` 结果 = 0 行；③删除文件 `backend/app/application/tools/langchain_adapter.py` 存在于 git diff deleted list
- **Pass Condition**: 以上三条同时成立
- **Evidence**: `python -c "from app.application.tools.base import GuardedTool; from langchain_core.tools import BaseTool; print(issubclass(GuardedTool, BaseTool))"` 输出 True；`grep -rn "HAS_LANGCHAIN\|class DuckTool" backend/app backend/tests` 无输出；`git status | grep "deleted.*langchain_adapter.py"` 有输出

### AC-2 工具定义纯 Pydantic args_schema，无手写 param_schemas
- **Type**: `rule`
- **Given**: 4 个内置工具迁移完毕
- **When**: 代码静态检查 + definition() 输出对比
- **Then**: ① builtin.py 中 `param_schemas` 文本出现次数 = 0；② 存在 `OrderQueryArgs / ExchangeRequestArgs / RepairRequestArgs / RefundRequestArgs` 4 个 Pydantic 类且分别为 4 个工具的 args_schema；③ `OrderQueryTool().definition().params` 字段 name=order_id/order_no 且 required=False；④ `ExchangeRequestTool().definition().params[1].name == "reason"` 且 enum == ["quality","size","wrong_good","other"] 且 required=True
- **Pass Condition**: 四条同时成立
- **Evidence**: `pytest -k "test_ac2_args_schema"` 通过 + builtin.py grep `param_schemas` 零结果

### AC-3 三条硬约束字节级保留（身份覆盖 / 审计 / 幂等）
- **Type**: `rule`
- **Given**: 通过 GuardedTool 调用一个写工具 OKTool（测试类）；ToolRunner 代码未重写（仅委托）
- **When**: ①调用时传参数 `tenant_id=bogus, user_id=bogus, x=hello`；②执行成功；③相同 idempotency_key 再调用一次；④同 key 但不同 x 再调用
- **Then**: ① trusted_args 写入 audit_logs 的 tenant_id/actor_id 为 Actor 的（而不是 bogus）；② audit_logs 新增 2 条 running→succeeded 的 ORM 记录；③第 2 次调用 ToolResult.from_idempotency_cache = True；④第 3 次调用 error_code = TOOL_IDEMPOTENCY_CONFLICT
- **Pass Condition**: 四个断言对应旧 test_task4_tools.py 的 TR4-1/TR4-3/TR4-4/TR4-5/TR4-6，只需改 OKTool 继承方式，其余断言通过
- **Evidence**: `pytest backend/tests/unit/test_task4_tools.py` 全绿（≥ 10 个用例）

### AC-4 policy_check 虚拟工具正确：不注册 + 不写 DB + 走纯函数
- **Type**: `rule`
- **Given**: Agent 节点执行 `build_langchain_tools(adapter_ctx)` 返回列表
- **When**: ①检查 ToolRegistry.list_definitions() 返回；②直接调用 policy_check.ainvoke({order_detail: {...}, intent_hint:"refund"})；③统计 FakeSession.add 中 ToolAuditLogORM 新增数
- **Then**: ① `list_definitions()` 不包含 policy_check 条目；② adapter_ctx.call_history 末尾新增一条 success=True 的 ToolResult（tool_name=policy_check，data 含 can_refund/can_exchange/can_repair 字段）；③ FakeSession.add 累计 ToolAuditLogORM 新增数 = 0；④ policy_check.tool_runner_ref 或内部执行路径调用了 `_decide_policy`（可通过 mock 验证）
- **Pass Condition**: 四条同时成立
- **Evidence**: `pytest -k "test_ac4_policy_check_virtual"` 通过

### AC-5 HTTP API 契约兼容
- **Type**: `rule`
- **Given**: FastAPI TestClient 启动
- **When**: ① GET `/api/tools`；② POST `/api/tools/call` 对 refund_request 不传 idempotency_key；③ 同接口合法传 payload
- **Then**: ① list 返回 JSON Schema 字段集合（key set 深比较）= 重构前；② ②响应 error_code = TOOL_IDEMPOTENCY_KEY_REQUIRED；③ ③响应 success=True，data.ticket_no 前缀 RF-
- **Pass Condition**: 三条同时成立
- **Evidence**: `pytest backend/tests/unit/test_task10_agent_http.py` 全绿

### AC-6 Agent 全链路（_rule_based_react_pipeline + 构建工具链）无回归
- **Type**: `rule`
- **Given**: Agent 节点运行在规则模式（chat_model=None）
- **When**: ① order_ref_candidate 合法 + intent_hint=refund + policy_override 允许退款；②policy_override 禁止退款；③unknown 意图分支
- **Then**: ① adapter_ctx.call_history 顺序 = [order_query, policy_check, refund_request] 三条；最终 action_kind=refund_request，action_result_json.ticket_no 前缀 RF-；② call_history 前两条；final_reply 含"暂不符合办理条件"；③ final_reply 为固定字符串"抱歉，没能理解您的意思"（不触发工具调用）
- **Pass Condition**: 三条场景对应 `test_agent_refactor_ac.py` 的三个典型用例全部通过，断言零修改
- **Evidence**: `pytest backend/tests/unit/test_agent_refactor_ac.py` 全绿（23+ 个用例）

### AC-7 全量 pytest 零回归 + Ruff 零违规
- **Type**: `rule`
- **Given**: 重构完成
- **When**: `cd backend && pytest -x` 与 `cd backend && ruff check app tests`
- **Then**: pytest exit code=0, passed count ≥ 99；ruff exit code=0
- **Pass Condition**: 两者同时成立
- **Evidence**: 任务 7 完成证据中附 stdout 摘要（passed count + ruff 0 issues）

### AC-8 代码净删除 ≥ 150 行 + 工具定义单类 ≤ 25 行
- **Type**: `rubric`
- **Dimension**: 冗余代码删除幅度 + 工具类简洁度
- **Scale**: 1-5
- **Anchors**:
  - 1 = 删除 < 50 行；工具单类 ≥ 60 行
  - 3 = 删除 50-149 行；工具单类 30-59 行
  - 5 = 删除 ≥ 150 行 且 langchain_adapter.py 整体删除；工具单类 ≤ 25 行（不含 docstring/注释）
- **Pass Threshold**: >= 4
- **Evidence**: `git diff --stat backend/app/application/tools/` 显示 deleted lines ≥ 150；`wc -l` 对 4 个工具类统计（不含 docstring）均 ≤ 25

### AC-9 面试叙事清晰度
- **Type**: `rubric`
- **Dimension**: GuardedTool 基类与工具写法的叙事可讲法
- **Scale**: 1-5
- **Anchors**:
  - 1 = 仍需讲解 2+ 个自定义中间层 / 兼容分支（HAS_LANGCHAIN try-except 等）
  - 3 = 讲解 1 个 GuardedTool 层，但业务工具 _execute_impl 仍需显式调用约束函数
  - 5 = 业务工具零感知（声明式 4 件套写好就行），约束挂接在 GuardedTool._arun 一句"在入口统一注入"讲完，能 60 秒内讲完"写法 + 企业级约束"
- **Pass Threshold**: >= 4
- **Evidence**: 代码审阅 + 注释配合：GuardedTool._arun 开头 5 行注释清晰列出"①-⑧ 8 步与 ToolRunner 一致"的挂接说明

---

_共 9 条 AC（7 rule + 2 rubric），覆盖功能/兼容/质量/可讲法。批准后进入 tasks 实施。_

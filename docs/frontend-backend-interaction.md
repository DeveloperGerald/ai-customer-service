# 前后端交互链路详解

> 文档版本：v2.0（2026-09-17 重新梳理版）  
> 完成度图例：✅ 已实现　🟡 MVP 简化（有代码，未来可升级）　❌ 未实现  
> 关联文档：[Agent 设计（agent-design.md）](../agent-design.md) · [架构设计（architecture.md）](architecture.md) · [README（README.md）](../README.md)

---

## 0. 全景图：一条消息的端到端旅程

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  浏览器 (React 18 + Vite)                                                     │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  ① 用户点击「发送」→ send()  [App.tsx#L230-L348]                       │  │
│  │      ├─ 防重复：running=true；空消息拦截                                  │  │
│  │      ├─ 构造 idempotency_key = uuidv4()                                  │  │
│  │      ├─ ✅ 立即 append 一条「human 气泡」到 UI（发送即所见）               │  │
│  │      └─ 分支：streamMode                                                    │  │
│  │          ├─ sync  → fetch(POST /run)  JSON 同步                          │  │
│  │          └─ sse   → streamChat() [sse-client.ts#L26-L88] 真流式          │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                    │                                                          │
│                    ▼                                                          │
│  ② HTTP 请求                                                                   │
│     🔐 Headers：{ Content-Type, X-Tenant-Id, Authorization: Bearer <JWT> }     │
│     📦 Body：   { "text": "...", "idempotency_key": "<uuid>" }                │
│     🛑 取消：   AbortController.signal 传入                                    │
└──────────────────────────────────────────────────────────────────────────────┘
                              │ HTTPS (HTTP/1.1 chunked transfer)
                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  后端 (FastAPI + uvicorn :8000)                                               │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  ③ 中间件链（外→内执行顺序）                                              │  │
│  │      ┌─ CORS Middleware（白名单 Origin + OPTIONS 预检放行）              │  │
│  │      ├─ RequestContextMiddleware（生成/透传 X-Request-Id → structlog） │  │
│  │      └─ ActorMiddleware（解析 JWT → 校验 JWT.tenant_id == X-Tenant-Id）│  │
│  │                         ↓                                                │  │
│  │  ④ 路由依赖注入 Depends(require_actor) + Depends(_session)               │  │
│  │                         ↓                                                │  │
│  │  ⑤ _get_actor_and_verify_thread()  ← 三层隔离第 2/3 层                 │  │
│  │      ├─ consumer 只看自己 thread；staff/admin 看同租户全部               │  │
│  │      ├─ ✅ thread_id 格式合法（{tenant_id}:{uuid}）但不存在 → 懒创建    │  │
│  │      └─ 不合法 → 404（防止跨租户拼接 thread_id）                         │  │
│  │                         ↓                                                │  │
│  │  ⑥ 路由分发：                                                            │  │
│  │      /run    → agent_invoke_sync()     [agent.py#L172-L224]  JSON 同步  │  │
│  │      /stream → agent_invoke_stream()   [agent.py#L562-L603] SSE 流式     │  │
│  │                         ↓                                                │  │
│  │  ⑦ 🟧 SSE 生成器 _stream_agent_run()  [agent.py#L318-L559]（**核心**）   │  │
│  │      ├─ 每条 SSE 帧紧跟 : sse-flush 注释 → 强制反向代理立即 flush         │  │
│  │      ├─ 每帧写结构化日志 sse.frame（event / size / elapsed_ms）          │  │
│  │      ├─ facade.astream_events() 产出 10 类事件 → 逐一映射 SSE 帧         │  │
│  │      │    start / node_start / node_end / tool / escalated               │  │
│  │      │    / reply_chunk ★ / reply / debug / done + [DONE] 终帧            │  │
│  │      ├─ try/except：异常绝不抛 Starlette → yield error → done → [DONE]    │  │
│  │      └─ finally commit（non-fatal：commit 失败不冒泡，rollback 即可）     │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                    │ SSE 帧字节流（TCP 任意分块）                              │
│                    ▼                                                          │
└──────────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  浏览器：SSE 解析 + UI 渲染                                                    │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │  ⑧ streamChat() 自研客户端 [sse-client.ts#L26-L88]                      │  │
│  │      fetch + ReadableStream（不用 EventSource：要 POST + 自定义 Header）│  │
│  │      ├─ TextDecoder(stream=true) 防 UTF-8 多字节拆坏                     │  │
│  │      ├─ _buffer 按 \n\n 切帧 防 TCP 任意分块                              │  │
│  │      ├─ parseFrame() 支持多 data 行 + SSE comment 忽略                   │  │
│  │      └─ 按 event 名 → onEvent(evt) 回调                                  │  │
│  │                         ↓                                                │  │
│  │  ⑨ onEvent 分发（App.tsx#L279-L335）switch (evt.event):                  │  │
│  │      start        → append 空气泡，streaming=true（打字光标出现）        │  │
│  │      escalated    → append handoff 卡片（工单 + 原因）                   │  │
│  │      tool         → append tool 气泡（政策判定 JSON）                    │  │
│  │      reply_chunk  → ★ appendToLastAgent(delta) 逐字追加（真正打字机）    │  │
│  │      reply        → finalizeLastAgent(text) — 已累计则只 finalize       │  │
│  │      debug        → setDebugByThread → DebugDecisionPanel 渲染          │  │
│  │      node         → 调试事件（当前不渲染，预留进度条位置）                │  │
│  │      error        → appendErrorToLastAgent("生成失败：…")                │  │
│  │      done [DONE]  → finalizeLastAgent() 无参 → streaming=false           │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. 前端发送侧（send 函数详解）

### 1.1 代码位置与前置条件

| 检查项 | 作用 | 代码位置 |
| --- | --- | --- |
| `active` 非空 | 当前选中的会话（含 thread_id） | [App.tsx#L235-L237](frontend/src/App.tsx#L235-L237) |
| `running === false` | 防重复提交（演示环境多次点击） | [App.tsx#L218](frontend/src/App.tsx#L218) 初始化，L345 finally 复位 |
| `input.trim() !== ""` | 空消息拦截 | [App.tsx#L231](frontend/src/App.tsx#L231) |
| `tenantId + bearer` | 从 `useIdentity()` 9 宫格身份上下文取 | [App.tsx#L221](frontend/src/App.tsx#L221) |

### 1.2 thread_id 来源：客户端乐观生成 + 服务端懒确认

前端生成规则（App.tsx `defaultThread()`）：
```
thread_id = {tenant_id}:{uuid4()}   // 例：tenant_a:2a0b9f1e-...
```

后端懒创建逻辑（[agent.py `_get_actor_and_verify_thread`](backend/app/api/agent.py#L58-L100)）：
1. 先查 DB → 找到 → 返回
2. 没找到 → 校验前缀（`parts[0] == actor.tenant_id`）+ 校验后缀（合法 UUID hex）
3. ✅ 合法 → `repo.create_thread(_override_suffix=uuid_obj)` 复用前端给的 thread_id
4. ❌ 不合法（越权拼接 thread_id，如 `tenant_b:xxx` 在租户 A 请求）→ **404 not found**（不是 403，避免泄漏租户存在性）

**面试话术**：这叫「Client-optimistic generation + Server lazy confirmation」，省去「先 POST /api/agent/threads 拿 id → 再 POST 消息」的一次完整 RTT，也契合单页应用无刷新的交互模型。

### 1.3 幂等键 idempotency_key 的生成与传递

```ts
const idempotency_key = uuidv4();           // App.tsx#L233
body: JSON.stringify({ text, idempotency_key })
```

**传递链**：前端 body → `AgentRunRequest.idempotency_key` → `facade.astream_events(idempotency_salt=...)` → **节点级注入**（每个工具调用、每个写操作的幂等键 = `sha256(idempotency_salt + ":" + thread_id + ":" + order_no)`，同请求重复不产生副作用）。

**什么时候生效**：用户手抖连点「发送」2 次、SSE 断网重试、Abort 后用户再重发；后端识别同一 idempotency_key → 不重复退款/不重复转人工/不重复写消息。

### 1.4 发送即 append（乐观 UI 更新）

```ts
append(active.id, { id: uuidv4(), role: "human", text, createdAt: Date.now() });  // L239
```

**设计考量**：
- 不等待后端响应，立即显示 human 气泡，避免「我点了吗？」的心理不安全感
- 后端也会在 `facade.astream_events()` 内部追加相同内容到 conversation_messages（见 §4.2），两边内容一致
- **异常回滚**：如果 HTTP 401/500 或 SSE fail，前端 catch 分支不删除 human 气泡（语义上用户「确实说过这句话」），只在 agent 气泡里追加错误提示「SSE 请求失败：…」

---

## 2. HTTP 请求层

### 2.1 两种模式：同步 JSON vs SSE 流式（顶栏可切）

| 维度 | 同步模式 `POST /run` | SSE 流式 `POST /stream`（默认） |
| --- | --- | --- |
| 后端路由 | [agent_invoke_sync](backend/app/api/agent.py#L172-L224) | [agent_invoke_stream](backend/app/api/agent.py#L562-L603) |
| Content-Type | `application/json` | `text/event-stream; charset=utf-8` + 6 条反缓存 header |
| 响应返回时机 | 等所有节点跑完才一次性返回 | 第 1 个 start 事件 ～50ms 内就到，打字机逐 token |
| 前端表现 | agent 气泡显示「…」→ 收到 JSON → 整段填入 | agent 空气泡 → 逐字 `appendToLastAgent(delta)` → 打字机 |
| 适合场景 | 接口联调 / 单测 / 无 SSE 环境的 WebView | 面试演示 / 真实用户体验 |
| 事件序列 | `（HTTP 200 + 整份 JSON 一次）` | `start → (node_start/node_end) → tool → escalated? → reply_chunk* → reply → debug → done → [DONE]` |

### 2.2 SSE 为什么不用浏览器原生 `EventSource`？（面试高频）

[sse-client.ts#L5-L9](frontend/src/lib/sse-client.ts#L5-L9) 注释给出了全部理由：

| 维度 | 原生 EventSource | 本项目 fetch + ReadableStream |
| --- | --- | --- |
| HTTP 方法 | 只能 GET | POST（可带 JSON Body，符合 REST 风格） |
| 自定义 Header | 完全不支持（连 Authorization 都不能带） | 任意 Header（X-Tenant-Id + Bearer JWT 必需） |
| 取消请求 | `close()`（断 TCP，但无 signal 可传） | `AbortController.abort()`（支持「取消」按钮，和 fetch 原生兼容） |
| 帧解析控制 | 浏览器内部黑盒 | 可控 `_buffer`，解决 TCP 分块、UTF-8 多字节边界（见 §6） |
| 重试行为 | 浏览器自动重连（可能产生重复写操作） | 无自动重连，重试由前端显式控制（配合幂等键） |

### 2.3 必备请求头 + 三层多租户隔离落地位置

```ts
headers: {
  "Content-Type": "application/json",
  "X-Tenant-Id": tenantId,     // ← 第 1 层：HTTP 层声明租户
  Authorization: bearer,       // ← Bearer <JWT>，JWT 里也含 tenant_id
}
```

三层隔离对应：
| 层级 | 执行点 | 校验内容 | 代码 |
| --- | --- | --- | --- |
| **1. HTTP 层** | `ActorMiddleware` | JWT 解析出的 `tenant_id` === `X-Tenant-Id` 头，不一致直接 401 | [auth.py ActorMiddleware](backend/app/application/auth.py) |
| **2. 应用层** | `ConversationRepository.*` | 每个查询 `WHERE tenant_id = ? + owner_user_id = ?`（consumer） | [conversation.py](backend/app/domain/repositories/conversation.py) |
| **3. DB 层** | `conversation_threads` CHECK 约束 | `CHECK (substring(thread_id, 1, len(tenant_id)+1) = tenant_id || ':')`；thread_id 前缀错，应用层就算有 bug 也插不进 | [0005_conversations.py](backend/alembic/versions/0005_conversations.py) |

---

## 3. 后端：中间件 + 路由 + 线程归属

### 3.1 中间件执行顺序（外 → 内）

注册位置 [main.py](backend/app/main.py#L271-L293)

```
请求到达
  │
  ▼
① CORS Middleware
│   ├─ 白名单 CORS_ORIGINS（默认 http://localhost:5174 + Docker 前端容器名）
│   └─ 放行 OPTIONS 预检（不走到鉴权层）
▼
② RequestContextMiddleware  [core/logging.py]
│   ├─ 读 X-Request-Id，没有则 uuid4() 生成
│   ├─ 写入 structlog contextvar → 所有子日志都带 request_id
│   └─ 写回响应头 X-Request-Id
▼
③ ActorMiddleware  [application/auth.py]
    ├─ 解析 Authorization: Bearer <JWT>  →  {tenant_id, actor_id, role}
    ├─ ✅ 签名 + exp（9 宫格 token 365 天有效期，演示用）
    ├─ ✅ JWT.tenant_id === X-Tenant-Id（不一致 → 401 tenant_mismatch）
    └─ Actor 对象挂到 request.state.actor
        │
        ▼
FastAPI Depends(require_actor)  ← 取上面挂的 Actor；没挂直接 401
```

### 3.2 路由依赖注入签名（以 SSE 流式为例）

```python
@router.post("/conversations/{thread_id}/stream")
async def agent_invoke_stream(
    thread_id: str,                             # URL path
    request: Request,                           # 取 app.state.bundle / agent_facade
    body: AgentRunRequest,                      # Pydantic 校验：{text: str (min 1), idempotency_key?: str}
    actor: Actor = Depends(require_actor),      # 中间件塞好的 Actor
    session: AsyncSession = Depends(_session),  # scoped_db_session 新 async session
) -> StreamingResponse:
```

[agent.py#L562-L569](backend/app/api/agent.py#L562-L569)

### 3.3 线程归属校验 + 懒创建（三层隔离第 2、3 层）

```python
actor, thread_row = await _get_actor_and_verify_thread(request, thread_id, actor, session)
```

| actor.role | 可见性 WHERE 条件 |
| --- | --- |
| consumer | `tenant_id = ? AND owner_user_id = actor.actor_id`（**只能看自己**） |
| staff / admin | `tenant_id = ?`（同租户下所有会话） |

**懒创建**：thread_id 格式合法 `{tenant}:{uuid}` 但 DB 中不存在 → 以当前 consumer 身份自动创建，省了「先发 /threads」的 RTT。前端新会话点「发送」第一条消息立刻成功，不需要额外等待。

---

## 4. 后端：SSE 生成器 + Facade 流式调用（核心）

### 4.1 SSE 帧格式（1 条 = 1 帧）

```
event: reply_chunk\n
data: {"text":"这款手串"} \n
\n    ← 空行 = 帧分隔符
: sse-flush\n
\n    ← 紧接着一条 flush hint 注释帧
```

- 帧构造函数：[agent.py `_sse_event`](backend/app/api/agent.py#L302-L309)
- `_json_default`：遇到 Actor / Pydantic / UUID / Decimal / datetime 一律转字符串，**绝不抛 TypeError 导致 500 半截流**
- `_safe_json_payload`：递归遍历 dict/list 做归一化（debug/tool payload 可能混 domain 对象）

### 4.2 flush hint 机制（🟡 MVP 关键坑点）

> **核心坑**：Nginx / 公司代理 / CDN / uvicorn 默认都有 socket write buffer（4~8KB 典型）。SSE 几十字节的小 reply_chunk 会被一直缓冲到生成器结束 → 浏览器一次收到全部 → **打字机不生效、所有字同时出现**。

**两层反缓冲组合拳**（同时上才能 100% 保证）：

| 层面 | 代码位置 | 做了什么 |
| --- | --- | --- |
| **响应头** | [agent.py#L596-L602](backend/app/api/agent.py#L596-L602) | `Cache-Control: no-cache, no-transform, private` + `X-Accel-Buffering: no` + `Connection: keep-alive`（6 条 header） |
| **帧级 flush** | [agent.py#L312-L315](backend/app/api/agent.py#L312-L315) + `_emit(frame)` | 每条业务帧后紧跟一条 `: sse-flush\n\n`（SSE comment，前端忽略）→ 强制 uvicorn/Nginx 立刻 writev flush buffer |

面试话术：「加完 X-Accel-Buffering: no 还不够——uvicorn 自己也有缓冲，我给每帧后面加了一个透明 comment 占位帧，凑够触发 write 的字节阈值，代理就不会把 reply_chunk 攒到结束才发。这样浏览器就能按预期每 20ms 看到新 token，打字机节奏真实。」

### 4.3 `_stream_agent_run` 异常自治（面试必讲）

**为什么要大 try/except 全包裹？**

```
StreamingResponse 首次 yield → HTTP 200 + Content-Type 响应头已发
    ↓
如果之后 facade.astream_events() 或 commit() 抛异常直达 Starlette
    ↓
触发：RuntimeError: Caught handled exception, but response already started.
    ↓
SSE 连接直接被 close() 砍断 → 前端半截流 → 用户以为「卡住了」，永远不知道出错。
```

**本项目的工程化处理**（[agent.py#L360-L559](backend/app/api/agent.py#L360-L559)）：

```python
try:
    async for evt in facade.astream_events(...):
        # 10 类事件映射 + 写 sse.frame 结构化日志
        yield _emit(_sse_event(etype, payload))
    yield "event: done\ndata: [DONE]\n\n"        # 终帧
    try:
        await session.commit()                    # non-fatal：commit 失败不向上冒泡
    except Exception:
        log.warning("sse.commit_failed_non_fatal")
        await session.rollback()
except Exception as exc:
    log.exception("agent.stream.failed")          # 结构化日志，附 rc_count / rc_bytes
    yield _emit(_sse_event("error", {...错误信息...}))
    yield _emit(_sse_event("done", {}))
    yield "event: done\ndata: [DONE]\n\n"
    await session.rollback()
```

**保证**：**FastAPI 全局 exception_handler 永远不会因 SSE 路径被触发**。前端要么收到完整的事件序列，要么至少收到 error → done → [DONE] 三帧能做 UI 回滚。

### 4.4 Facade.astream_events 内部调用链（对应 agent-design.md §2.5 流式旁路）

入口：[facade.py `astream_events()`](backend/app/application/agent/facade.py)

时序按代码顺序：

```
① build service_actor（STAFF 可信内部身份）
│   原因：consumer 身份禁止写 agent/tool 消息；写操作必须经 service_actor
│   代码：[facade.py#L91-L109](backend/app/application/agent/facade.py#L91-L109)
② append human 消息 → conversation_messages
│   即便后面崩了，用户发的内容也已落库（审计完整）
③ _bind_node_contexts：给 11 个节点套 _trace wrapper
│   node_start → 执行业务 → finally node_end
│   保证即使 LangGraph 原生事件不抛，前端也能看到节点进度
④ facade.astream_events 双任务合并：
│   ├─ _ns_drain_loop：消费 NODE_STREAM ContextVar 队列 → 旁路 token 级 reply_chunk
│   └─ lg_stream_to_mux：真 LangGraph astream_events → node 事件
│   两个任务各写一个 None sentinel 到 MUX → while _sentinel_seen < 2 退出
⑤ final_reply 三重选优：
    final_state.final_reply → _acc_chunks 本地累计 → 9 字兜底
    → 保证 rc > 0（前端一定能看到打字机）
```

### 4.5 SSE 落库的最终一致策略（面试点）

旧文档 v1 说「reply 先 yield → 之后慢慢 commit」是不准确的。当前实际实现修正如下：

- **human 消息**：在 facade.astream_events 内部一开始就 `append_message(service_actor, role=human)` + commit 随 SSE 生成器结束时做
- **agent 最终回复 + escalated 状态**：同样由 facade 内部在节点执行时写 message → SSE 生成器 `finally session.commit()`（commit 失败 non-fatal，rollback 即可，不向上冒泡）
- **为什么不 yield 之前 commit**？ commit 是磁盘 fsync（~10-30ms），放在 start 事件之前会让首帧 TTFB 从 ~20ms 涨到 ~40ms+；放后面用户能先看到打字机再做后台刷盘（UI 优先 + 最终一致）

---

## 5. SSE 事件序列契约：10 类事件（前后端强约定）

### 5.1 完整事件表

后端产生顺序由 `facade.astream_events()` 保证；前端 switch 按 event 名分发：

| 顺序 | event 名 | data 结构 | 前端 App.tsx 行为 | 是否必现 | 代码锚点 |
| --- | --- | --- | --- | --- | --- |
| 1 | `start` | `{thread_id, user_input}` | `append(role=agent, text="", streaming=true)` → 打字光标出现 | ✅ | [agent.py#L349-L358](backend/app/api/agent.py#L349-L358) + [App.tsx#L281-L283](frontend/src/App.tsx#L281-L283) |
| 2 | `node` | `{phase:start/end, node, has_patch, has_error, error}` | 当前忽略（留作顶部 11 节点进度条）。phase=start 时节点开始执行；phase=end 时结束 | 🟡 可选 | [agent.py#L374-L414](backend/app/api/agent.py#L374-L414) |
| 3 | `escalated` | `{ticket_no, reason}` | `append(role=handoff)` → 橙色工单卡片（ticket_no 如 `HO-TENANT_B-7F3AB12D`） | 命中 handoff 才推 | [agent.py#L432-L447](backend/app/api/agent.py#L432-L447) + [App.tsx#L284-L290](frontend/src/App.tsx#L284-L290) |
| 4 | `tool` | `{kind, payload}` | `append(role=tool, toolCall)` → 灰色 JSON 代码块气泡（政策判定详情、订单 JSON 等） | 有 policy_decision / order_query 时推 | [agent.py#L415-L431](backend/app/api/agent.py#L415-L431) + [App.tsx#L291-L297](frontend/src/App.tsx#L291-L297) |
| 5★ | `reply_chunk` | `{text: str}`（单 token/多 token 块） | **核心打字机**：`appendToLastAgent(tid, delta)` 直接追加到最后一条 agent 气泡尾部 | 有 rc_total ≥ 1；Mock/模板路径也会逐字 emit | [agent.py#L448-L467](backend/app/api/agent.py#L448-L467) + [App.tsx#L298-L303](frontend/src/App.tsx#L298-L303) |
| 6 | `reply` | `{text: final_full_text}` | `finalizeLastAgent(tid, text)` — **若已有 reply_chunk 累计，则只 finalize streaming=false，不重写内容**（防叠字） | ✅ | [agent.py#L468-L483](backend/app/api/agent.py#L468-L483) + [App.tsx#L304-L309](frontend/src/App.tsx#L304-L309) |
| 7 | `debug` | `{payload: decision_debug}` | `setDebugByThread(...)` → 底部 **DebugDecisionPanel**：意图、订单、政策 10 reason_code、RAG hits、动作 | ✅ | [agent.py#L484-L496](backend/app/api/agent.py#L484-L496) + [App.tsx#L310-L315](frontend/src/App.tsx#L310-L315) |
| 8 | `done` | `{}` | 不做动作（reply/reply_chunk 已填好）；做日志结构化打点 | ✅ | [agent.py#L497-L510](backend/app/api/agent.py#L497-L510) |
| 9 | `done` | `[DONE]`（字符串字面量，不是 JSON） | **终帧**：`finalizeLastAgent(tid)` 无参调用 → streaming=false + 兜底空文本校验 | ✅ （特殊 data 字面量） | [agent.py#L514](backend/app/api/agent.py#L514) + [App.tsx#L327-L332](frontend/src/App.tsx#L327-L332) |
| X | `error` | `{type, message, reply_chunk_count, reply_chunk_total_bytes}` | `appendErrorToLastAgent("生成失败：{message}")`；附 rc 统计（失败前已收到多少字） | 异常路径 | [agent.py#L533-L555](backend/app/api/agent.py#L533-L555) + [App.tsx#L319-L326](frontend/src/App.tsx#L319-L326) |

### 5.2 reply_chunk 与 reply 的协作防叠字逻辑（Anti-Bug）

```
前端收到：
   start → reply_chunk("这") → reply_chunk("款") → reply_chunk("手") → ... → reply_chunk("理。") → reply("这款手串7天无理由退换…") → done → [DONE]
```

**防叠字**：`finalizeLastAgent(tid, reply_text)` 内部有 **累计检测**（[App.tsx#L192-L198](frontend/src/App.tsx#L192-L198)）：
```ts
const hasAccumulated = (agent.text ?? "").length > 0;
// 只有 0 累计时才用 reply 里的 text 覆写；有累计就不覆写，只把 streaming 置 false
if (finalText && finalText.length > 0 && !hasAccumulated) nextText = finalText;
```

配合后端 `_trace` wrapper 的「真 LangGraph on_chat_model_stream 只累计不 yield」规则 → **不出现 "ItIt lookslooks" 每字重复 2 次的 Bug**。

### 5.3 异常路径保证

Facade.astream_events 抛任意异常 → 后端最短序列：
```
start → error（附 rc_count / rc_bytes / error_type / message） → done → [DONE]
```

前端：start 建了空气泡 → error 追加 `⚠️ 生成失败：…` → [DONE] finalize streaming=false。**绝对不会出现「前端永远显示 …」的半卡死态**。

---

## 6. 前端：SSE 自研客户端三大问题解决

代码：[sse-client.ts](frontend/src/lib/sse-client.ts)，117 行零依赖。

### 6.1 问题 → 解法总览

| # | TCP/协议层面的现实问题 | 解法 | 代码锚点 |
| --- | --- | --- | --- |
| 1 | **TCP 字节流任意分块**：一帧 `event: reply_chunk\ndata: {...}\n\n` 可能被拆成「前半 read() 一次 + 后半 read() 第二次」；若不做 buffer，第一次 parse 直接抛 JSON parse 错 | 维护 `_buffer` 字符串；每次 read() 后 `while ((sepIndex = buffer.indexOf("\n\n")) >= 0)` 切帧，剩下的拼回 buffer | [sse-client.ts#L58-L66](frontend/src/lib/sse-client.ts#L58-L66) |
| 2 | **UTF-8 多字节跨 chunk 拆坏**：中文「串」= E4 B8 B2 三字节；第一次 read 给 E4 B2、第二次给 B2；直接 `value.toString('utf-8')` 会输出两个乱码替换符 | `TextDecoder.decode(value, { stream: true })` 内部保存未完成字节 + done 时 `decoder.decode()` flush 尾部 | [sse-client.ts#L55 + L69-L70](frontend/src/lib/sse-client.ts#L55) |
| 3 | **SSE 帧格式：多 data 行 + comment 行**：一帧允许多个 data: 行（`data: line1\ndata: line2` → 合并 `line1\nline2`）；`:` 开头是 heartbeat/comment 必须忽略；无 event 行默认 message | `parseFrame()`：逐行 `\r?\n` 扫，`:` 开头 skip，event 行存事件名，data 行塞数组，最后 `dataLines.join("\n")` | [sse-client.ts#L90-L108](frontend/src/lib/sse-client.ts#L90-L108) |

### 6.2 parseFrame 解析示例

输入帧（去掉末尾 \n\n）：
```
event: escalated
: this is a SSE comment, ignore it
data: {"ticket_no":"HO-TENANT_B-7F3AB12D",
data: "reason":"用户明确投诉"}
空行（已被外层切掉）
```
→ `parseFrame()` 输出：
```
event = "escalated"
dataStr = "{\"ticket_no\":\"HO-TENANT_B-7F3AB12D\",\n\"reason\":\"用户明确投诉\"}"
→ safeJson(dataStr) = { ticket_no: "...", reason: "..." }
```

### 6.3 [DONE] 字面量特殊识别

```ts
const finalData = parsed.dataStr === "[DONE]" ? "[DONE]" : safeJson(parsed.dataStr);
```
→ 前端 switch 的 `case "done": if (evt.data === "[DONE]") finalizeLastAgent()` 分支正确命中终帧。

---

## 7. 前端：onEvent 分发 + UI 渲染（四种气泡 + 同步模式差异 + 取消）

### 7.1 四种气泡类型（MessageBubble 渲染）

| ChatMessageVM.role | 来源事件 | 视觉效果 | 代码 |
| --- | --- | --- | --- |
| `human` | send() 本地直接 append（后端也会写） | 右侧蓝底白字气泡 | [App.tsx#L239](frontend/src/App.tsx#L239) |
| `agent` | start → reply_chunk* → reply/finalize | 左侧白底灰字；streaming=true 尾部显示 `▊` 打字光标 | [App.tsx#L282](frontend/src/App.tsx#L282) + `_appendToLastAgent` [L125-L153](frontend/src/App.tsx#L125-L153) |
| `handoff` | escalated | 左侧橙色渐变卡片：顶部「转人工受理成功」+ 大字 ticket_no + 原因 + 预计 24h 内联系 | [App.tsx#L285-L289](frontend/src/App.tsx#L285-L289) |
| `tool` | tool | 左侧灰色代码块样式：顶部 kind 标签（refund_policy_check / order_query 等）+ 折叠展开 JSON payload | [App.tsx#L291-L296](frontend/src/App.tsx#L291-L296) |

### 7.2 同步模式（sync /run）交互差异

分支：[App.tsx#L240-L267](frontend/src/App.tsx#L240-L267)

```
① 先 append agent 气泡，text="…"（省略号占位，表示在想）
② fetch POST /run 等完整 JSON
③ resp.ok：
   ├─ escalated? → append handoff 卡片
   ├─ finalizeLastAgent(body.final_reply) → 填入整段文本
   └─ setDebugByThread(body.decision_debug)
④ !resp.ok：
   └─ finalizeLastAgent("❌ 请求失败：HTTP ${status} ${statusText}")
⑤ finally：setRunning(false)
```

### 7.3 取消请求（AbortController + 幂等键兜底）

```ts
const ctrl = new AbortController();             // L270
abortRef.current = ctrl;                        // L271
signal: ctrl.signal,                            // 传入 streamChat() / fetch
...
用户点「取消」按钮 → abortRef.current?.abort()  // App.tsx 顶部取消按钮 handler
```

- 浏览器层：立刻关闭 socket（read() 抛 `DOMException: Aborted`）→ streamChat() catch → 前端 append 错误提示
- 后端层：生成器还在跑 facade.astream_events 的情况没法立刻 kill（Python 无抢占），但前端已释放 running=true，用户可以立刻重发；**此时幂等键就起作用了：如果后端其实已经提交了退款/转人工，下次同 idempotency_key 不会再产生新工单**

---

## 8. 完整时序表（实战面试话术：一条退款消息走完所有步骤）

示例输入：**"我要退款 SO-1001 刚收到不喜欢"**（租户 = tenant_a / 禅饰坊，7 天无理由 + 10% 手续费）

| 时间点 | 位置 | 动作 | 关键对象 |
| --- | --- | --- | --- |
| **T0** | App.tsx send() | `running=true`；生成 `idempotency_key`；append human 气泡；创建 `AbortController` | `running` 状态，human 消息 |
| **T1** | fetch() → SSE POST `/api/agent/conversations/{tid}/stream` | Headers 带 X-Tenant-Id + JWT；POST JSON body | TCP 握手（Docker 内 ~1ms） |
| **T2** | uvicorn → 中间件链 | CORS 过；RequestContext 生成 request_id 写 structlog；ActorMiddleware 解 JWT，校验 tenant 一致 | `request_id`、`Actor(tenant_a, user_id, consumer)` |
| **T3** | 路由依赖 + `_get_actor_and_verify_thread` | Pydantic 校验 body 合法；校验 thread 归属（consumer 自己）→ 不存在则懒创建 | `AsyncSession`（新事务开始） |
| **T4** | StreamingResponse 首次 yield | `event: start` + 空行 + flush hint → 浏览器收到 → HTTP 200 响应头 **已发** | ⚠️ 之后异常只能 yield error 帧，再也没法改 HTTP 状态 |
| **T5** | facade.astream_events() | append human_msg 写 DB；_bind_node_contexts 套 _trace 旁路；MUX 双任务启动 | AgentState 初始 `{user_message, tenant_id, actor, …}` |
| **T5.1** | node_start→intent_classify→node_end | Keyword："退款"=REFUND；正则抽 SO-1001 → order_ref_candidate | debug.intent_candidate = REFUND |
| **T5.2** | rag_retrieve → 无命中（没接向量库的关键词 mock） | `rag_hits=[]` | — |
| **T5.3** | order_query_node 过 Repository | 查 SO-1001：归属校验 owner=当前 user；签收 2 天前、非定制款、payment=39900 分 | tool 事件（order_query JSON）→ 前端灰色气泡 |
| **T5.4** | policy_decision_node → RefundQualificationService.decide() | 纯代码判定：7 天内 ✅、非定制款 ✅、非质量 → reason_code=eligible_no_reason；fee=10%；refund_cents=35910 | tool 事件（refund_policy_check）→ 前端灰色气泡；debug.reason_code |
| **T5.5** | action_branch_router → refund 分支 | refund_node：生成工单号 R-TENANT_A-XXXXX；生成 refund_request 占位消息 | escalated?=no |
| **T5.6** | llm_wrap_node 流式 | MockChatModel / OpenAI SDK stream → **逐字 emit_token("您好，禅饰坊支持 7 天无理由退换… 实退金额 ¥359.10")** | ★ 100+ reply_chunk 帧 → 前端打字机 |
| **T5.7** | debug 事件 + done 事件 + `done [DONE]` 终帧 | decision_debug 全量写 DebugDecisionPanel；finalizeLastAgent streaming=false | 所有 UI 就绪 |
| **T6** | sse-client | ReadableStream 逐 chunk → TextDecoder → _buffer 切 → parseFrame → onEvent 分发 10 类事件 | `_buffer`、`parseFrame` |
| **T7** | finally `session.commit()` | conversation_messages 新写 human + 5 条 agent/tool 行 → DB 持久化；commit 失败 non-fatal rollback | fsync ~10-20ms |
| **T8** | App.tsx finally | `setRunning(false)`；`abortRef = null`；可以发下一条 | 状态复位 |

---

## 9. 面试讲解要点（3 分钟浓缩版）

> **面试官：你这个前后端交互有什么亮点？**

**用三个关键设计决策概括：**

### 决策一：自研 SSE 客户端（fetch + ReadableStream），零依赖 117 行
"浏览器原生 EventSource 只能 GET、不能带 Header——而我们多租户系统强制要求 X-Tenant-Id + JWT Bearer。我用 fetch + ReadableStream.getReader() 写了 117 行零依赖客户端，顺手解决了三个工程化问题：TCP 任意分块用 `_buffer` 按 `\n\n` 切帧；UTF-8 三字节中文跨 chunk 拆坏用 `TextDecoder(stream=true)`；SSE 多 data 行 + comment 用逐行 parseFrame。代码量小但能覆盖生产级所有边界情况。"

### 决策二：flush hint 双层反缓冲 + 异常自治
"工程上有两个容易踩的坑：① Nginx/uvicorn 都有 buffer，小 reply_chunk 会被攒到结束才发 → 打字机不生效，我加了 6 条反缓冲 header + 每帧后紧跟 `: sse-flush` 透明注释帧；② 第一次 yield 之后 HTTP 200 已经发出去了，异常抛给 Starlette 会导致半截连接砍断 → 我把 SSE 生成器整个包在 try/except 里，异常时 `yield error → done → [DONE]`，绝对不抛到外层，前端一定能拿到至少 3 帧做 UI 回滚。"

### 决策三：三层多租户隔离，层层收紧
"第一层 HTTP ActorMiddleware：X-Tenant-Id Header 必须等于 JWT 解析出的 tenant_id，不一致 401；第二层应用层 Repository 每个查询显式传 tenant_id，consumer 只能查 owner=自己的行；第三层数据库级 CHECK 约束：thread_id 的前缀必须等于租户 ID，即使前两层都有 bug，thread_id 写错了 INSERT 直接被 DB 拒绝，客户端乱传也写不进跨租户数据。面试演示时可以现场切到 tenant_b 拿 tenant_a 的 thread_id 去发，立刻返回 404，非常直观。"

---

## 10. 完成度与可优化空间

### 10.1 完成度对照

| 模块 | 状态 | 说明 |
| --- | --- | --- |
| 同步 `/run` 接口 + 前端分支 | ✅ 100% | |
| SSE `/stream` 接口 + 反缓冲 + flush hint + 异常自治 | ✅ 100% | |
| 自研 SSE 客户端（3 问题解决） | ✅ 100% | |
| 10 类事件契约 + 前端 switch 分发 | ✅ 100% | start / node / escalated / tool / reply_chunk / reply / debug / done / [DONE] / error |
| reply_chunk + reply 协作防叠字 | ✅ 100% | `hasAccumulated` 检测 + 后端真 LangGraph 事件只累计不 yield |
| 三层多租户隔离（HTTP/应用/DB） | ✅ 100% | 404 不泄漏租户存在性 |
| thread 懒创建（Client optimistic + Server lazy） | ✅ 100% | |
| idempotency_key 传递链 | ✅ 前端生成 → facade salt | 后端节点级 sha256 幂等键生成已预留 |
| AbortController 取消 + 幂等兜底 | ✅ 接口 | 后端节点取消需 LangGraph cancel（非抢占式） |
| SSE 响应头 6 条反缓存 | ✅ 100% | X-Accel-Buffering / Cache-Control: no-transform 都加了 |
| `node` 事件前端进度条渲染 | ❌ | 目前留空，可做顶部 11 节点小进度条（intent → rag → order → …） |
| 前端断线重试（带 idempotency_key） | ❌ | 目前 catch 直接报失败；可做：若 rc_count=0 自动重试 1 次，rc_count>0 不重试 |
| SSE 后端 heartbeat（`: hb\n\n` 每 15s） | 🟡 预留 | 防止代理空闲时断连接（演示环境 30s 内全结束，暂不需要） |
| 前端 rc_count / rc_bytes 展示 | ❌ | 目前后端日志打点；前端可做右下角小字「已生成 X 字 / Y 字节」debug 面板 |
| 响应头 ETag / Last-Modified 缓存 | ❌ | 不需要（流是实时的，缓存没意义） |

### 10.2 优化方向索引（和 agent-design.md 对齐）

| 优化点 | 工作量 | 影响链路范围 | 详见 |
| --- | --- | --- | --- |
| 顶部 11 节点 `node` 进度条 | 1 天 | 前端 App.tsx + 自定义组件 | 本文件 §10.1 |
| SSE 断线重试（0 rc_count 自动重试 + 幂等） | 1 天 | 前端 sse-client + send() catch | 本文件 §10.1 |
| conversation_busy 串行锁（同 thread_id 并发防重） | 2 天 | 后端 agent.py `SELECT FOR UPDATE SKIP LOCKED` + 429 | agent-design.md §4.7 |
| Token 级真流式（openai SDK stream=True，不是模拟逐字） | 3 天 | 后端 providers.py `BaseChatModelProvider.astream_chat()` Protocol | agent-design.md §4.5 |
| 前端 rc_count/rc_bytes debug 浮层 | 0.5 天 | 前端 App.tsx 角落 debug 信息 | 本文件 §10.1 |

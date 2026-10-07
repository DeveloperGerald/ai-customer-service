import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { flushSync } from "react-dom";
import { IdentityProvider, useIdentity } from "./identity/IdentityContext";
import { IdentitySwitcher } from "./components/IdentitySwitcher";
import { ChatMessageVM, MessageBubble } from "./components/MessageBubble";
import { DebugDecisionPanel } from "./components/DebugDecisionPanel";
import { EvaluationPage } from "./components/EvaluationPage";
import { streamChat } from "./lib/sse-client";
import type { Role } from "./demo-tokens";

type NavKey = "chat" | "orders" | "policy" | "knowledge" | "eval";

const NAV: { key: NavKey; label: string; icon: string; desc: string }[] = [
  { key: "chat", label: "智能客服", icon: "💬", desc: "" },
  { key: "orders", label: "我的订单", icon: "📦", desc: "" },
  {
    key: "policy",
    label: "售后政策管理",
    icon: "⚙️",
    desc: "",
  },
  {
    key: "knowledge",
    label: "FAQ / 知识库",
    icon: "📚",
    desc: "",
  },
  { key: "eval", label: "评估测试", icon: "🧪", desc: "" },
];

// 各角色可见的导航 tab：
//   consumer → 智能客服 + 我的订单；staff → 售后政策管理 + FAQ/知识库；
//   admin    → 售后政策管理 + FAQ/知识库 + 评估测试
const NAV_BY_ROLE: Record<Role, NavKey[]> = {
  consumer: ["chat", "orders"],
  staff: ["policy", "knowledge"],
  admin: ["policy", "knowledge", "eval"],
};

const DEFAULT_NAV_BY_ROLE: Record<Role, NavKey> = {
  consumer: "chat",
  staff: "policy",
  admin: "policy",
};

// GET /api/conversations 返回的会话线程 DTO（字段与后端 ConversationThreadRead 对齐）
interface ConversationThreadDTO {
  thread_id: string;
  title: string | null;
  initial_user_message: string | null;
  status: "open" | "escalated" | "closed";
  created_at: string;
  updated_at: string;
  last_message_at: string | null;
}

// GET /api/conversations/{thread_id}/messages 返回的消息 DTO
interface ConversationMessageDTO {
  message_id: string;
  role: "human" | "agent" | "tool";
  content: string;
  tool_name: string | null;
  created_at: string;
}

interface ThreadVM {
  id: string;
  title: string;
  createdAt: number;
  summary?: string;
}

function toThreadVM(row: ConversationThreadDTO): ThreadVM {
  const ts = new Date(row.last_message_at ?? row.created_at).getTime();
  return {
    id: row.thread_id,
    title: row.title?.trim() || "",
    createdAt: Number.isFinite(ts) ? ts : Date.now(),
    summary: row.initial_user_message ?? undefined,
  };
}

function historyMessageToVM(m: ConversationMessageDTO): ChatMessageVM {
  const createdAt = new Date(m.created_at).getTime();
  const base = {
    id: m.message_id,
    createdAt: Number.isFinite(createdAt) ? createdAt : Date.now(),
  };
  if (m.role === "human") return { ...base, role: "human", text: m.content };
  if (m.role === "tool") {
    return {
      ...base,
      role: "tool",
      text: m.content,
      toolCall: { kind: m.tool_name ?? "tool", payload: null },
    };
  }
  return { ...base, role: "agent", text: m.content };
}

function uuidv4() {
  // 轻量 uuid4（演示用，不引 crypto 外部库）
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

// 组件级 helper：从消息列表尾部找到最近一条 role === "agent" 的索引。
// 提模块级（不在组件内）避免每次 render 重建 → 被 useCallback deps 反复标记变更。
function _lastAgentIndex(list: ChatMessageVM[]): number {
  for (let i = list.length - 1; i >= 0; i--)
    if (list[i].role === "agent") return i;
  return -1;
}

// 模块级：同一页面生命周期内按 (tenant, actor) 去重自动创建的会话。
// 组件重复挂载（React StrictMode / Fast Refresh / 重挂载）时复用同一会话，
// 避免每次挂载都 POST 出重复的空会话；整页刷新后 Map 重置，重新建新会话。
const autoBootedThreads = new Map<string, ThreadVM>();
const autoBootPromises = new Map<string, Promise<ThreadVM | null>>();

const EXAMPLE_PROMPTS: string[] = [
  "你好，你们支持几天无理由退货？",
  "我要退款 订单 SO-1001 刚收到不喜欢",
  "手串珠子裂了 SO-1002 想换货",
  "帮我转人工，我要投诉",
  "我的订单要维修，超过保修一个月了",
];

export default function App() {
  return (
    <IdentityProvider>
      <Shell />
    </IdentityProvider>
  );
}

function Shell() {
  const { current, tenantId, bearer } = useIdentity();
  const isConsumer = current.role === "consumer";
  const allowedNavs = NAV_BY_ROLE[current.role];
  const [nav, setNav] = useState<NavKey>("chat");

  // 切换身份后，若当前 nav 不在新角色可见范围内，收敛到该角色默认页
  useEffect(() => {
    setNav((prev) =>
      allowedNavs.includes(prev) ? prev : DEFAULT_NAV_BY_ROLE[current.role],
    );
  }, [current.role, allowedNavs]);

  const [threads, setThreads] = useState<ThreadVM[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  const [threadsLoading, setThreadsLoading] = useState(false);
  const [threadsError, setThreadsError] = useState<string | null>(null);
  const [creatingThread, setCreatingThread] = useState(false);
  // 新建会话防重入：用 ref 而非 state 做守卫，保证 newThread 的回调身份稳定
  // （否则其随 creatingThread 变化，会让依赖它的 effect 反复重跑）
  const creatingRef = useRef(false);
  // 已拉取过历史消息的 thread_id（避免选中时重复拉取覆盖本地流式消息）
  const hydratedRef = useRef<Set<string>>(new Set());

  const active = threads.find((t) => t.id === activeId) ?? null;
  const [messagesByThread, setMessagesByThread] = useState<
    Record<string, ChatMessageVM[]>
  >({});
  const messages = active ? (messagesByThread[active.id] ?? []) : [];

  // 拉取真实会话列表：GET /api/conversations（后端对 consumer 强制 owner_user_id 过滤）
  const loadThreads = useCallback(
    async (opts?: { keepActive?: boolean }) => {
      setThreadsLoading(true);
      setThreadsError(null);
      try {
        const resp = await fetch("/api/conversations?limit=50", {
          headers: { "X-Tenant-Id": tenantId, Authorization: bearer },
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        const rows = (await resp.json()) as ConversationThreadDTO[];
        const next = rows.map(toThreadVM);
        setThreads(next);
        setActiveId((prev) => {
          if (opts?.keepActive && prev && next.some((t) => t.id === prev))
            return prev;
          return next[0]?.id ?? "";
        });
      } catch (e) {
        setThreadsError(e instanceof Error ? e.message : String(e));
      } finally {
        setThreadsLoading(false);
      }
    },
    [tenantId, bearer],
  );

  // 选中某条历史会话时，拉取后端落库的消息（仅首次选中拉取，之后以本地流式状态为准）
  useEffect(() => {
    if (!isConsumer || !activeId || hydratedRef.current.has(activeId)) return;
    hydratedRef.current.add(activeId);
    let cancelled = false;
    fetch(
      `/api/conversations/${encodeURIComponent(activeId)}/messages?limit=100`,
      {
        headers: { "X-Tenant-Id": tenantId, Authorization: bearer },
      },
    )
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
        return (await r.json()) as ConversationMessageDTO[];
      })
      .then((rows) => {
        if (cancelled) return;
        setMessagesByThread((prev) =>
          prev[activeId]
            ? prev
            : { ...prev, [activeId]: rows.map(historyMessageToVM) },
        );
      })
      .catch(() => {
        // 拉取失败允许下次选中重试
        hydratedRef.current.delete(activeId);
      });
    return () => {
      cancelled = true;
    };
  }, [activeId, isConsumer, tenantId, bearer]);

  const append = useCallback(
    (
      tid: string,
      partial: Partial<ChatMessageVM> & Pick<ChatMessageVM, "role">,
    ) => {
      setMessagesByThread((prev) => {
        const list = prev[tid] ?? [];
        const next = {
          id: uuidv4(),
          createdAt: Date.now(),
          text: "",
          ...partial,
        } as ChatMessageVM;
        return { ...prev, [tid]: [...list, next] };
      });
    },
    [],
  );

  const appendToLastAgent = useCallback((tid: string, textDelta: string) => {
    if (!textDelta) return;
    // 用 flushSync + queueMicrotask 拆开 React 18 连续 setState 的自动批处理。
    // SSE reply_chunk 默认 10~20ms 间隔 1 块；如果直接 setMessagesByThread，在 fetch ReadableStream 的同 1 个
    // microtask queue 里会被 React 18 concurrent scheduler 合并成 1 次 render → 表现为"所有文字同时出现"。
    // queueMicrotask(flushSync(...)) 强制每个 chunk 立刻 commit，保证打字机节奏肉眼可见。
    queueMicrotask(() => {
      flushSync(() => {
        setMessagesByThread((prev) => {
          const list = [...(prev[tid] ?? [])];
          const i = _lastAgentIndex(list);
          if (i < 0) {
            list.push({
              id: uuidv4(),
              createdAt: Date.now(),
              role: "agent",
              streaming: true,
              text: textDelta,
            });
            return { ...prev, [tid]: list };
          }
          const agent = list[i];
          if (!agent.streaming && agent.text.length > 0) {
            list.push({
              id: uuidv4(),
              createdAt: Date.now(),
              role: "agent",
              streaming: true,
              text: textDelta,
            });
            return { ...prev, [tid]: list };
          }
          list[i] = { ...agent, text: agent.text + textDelta };
          return { ...prev, [tid]: list };
        });
      });
    });
  }, []);

  const appendErrorToLastAgent = useCallback(
    (tid: string, errorText: string) => {
      setMessagesByThread((prev) => {
        const list = [...(prev[tid] ?? [])];
        const i = _lastAgentIndex(list);
        const msg = i >= 0 ? list[i] : null;
        if (!msg || (!msg.streaming && msg.text.length > 0)) {
          list.push({
            id: uuidv4(),
            createdAt: Date.now(),
            role: "agent",
            streaming: false,
            text: errorText,
          });
          return { ...prev, [tid]: list };
        }
        const base = msg.text || "";
        list[i] = {
          ...msg,
          streaming: false,
          text: base ? `${base}\n\n⚠️ ${errorText}` : `⚠️ ${errorText}`,
        };
        return { ...prev, [tid]: list };
      });
    },
    [],
  );

  const finalizeLastAgent = useCallback((tid: string, finalText?: string) => {
    setMessagesByThread((prev) => {
      const list = [...(prev[tid] ?? [])];
      const i = _lastAgentIndex(list);
      if (i < 0) {
        // 根本没有 agent 气泡 → 兜底塞一条（防止 start 丢帧 + reply 正常的极端情况）
        list.push({
          id: uuidv4(),
          createdAt: Date.now(),
          role: "agent",
          streaming: false,
          text: finalText ?? "（无回复）",
        });
        return { ...prev, [tid]: list };
      }
      const agent = list[i];
      const hasAccumulated = (agent.text ?? "").length > 0;
      let nextText = agent.text;
      if (finalText && finalText.length > 0) {
        // 有 reply_chunk 累计：用 finalText 仅当累计和它严重不一致（例如累计只收到一半）才修正
        if (!hasAccumulated) {
          nextText = finalText;
        }
      }
      if (!nextText) {
        nextText = "（服务端无回复）";
      }
      list[i] = { ...agent, streaming: false, text: nextText };
      return { ...prev, [tid]: list };
    });
  }, []);

  // HITL 暂停时：本轮没有回复文本，移除 start 时创建的空 streaming agent 占位，
  // 避免 [DONE] 终帧把它 finalize 成"（服务端无回复）"
  const removeLastEmptyStreamingAgent = useCallback((tid: string) => {
    setMessagesByThread((prev) => {
      const list = [...(prev[tid] ?? [])];
      const i = _lastAgentIndex(list);
      if (i < 0) return prev;
      const agent = list[i];
      if (agent.streaming && !(agent.text ?? "").trim()) {
        list.splice(i, 1);
        return { ...prev, [tid]: list };
      }
      return prev;
    });
  }, []);

  // HITL 确认卡片状态更新：pending → approved/declined/timeout/error
  const updateConfirmation = useCallback(
    (
      tid: string,
      msgId: string,
      patch: Partial<NonNullable<ChatMessageVM["confirmation"]>>,
    ) => {
      setMessagesByThread((prev) => {
        const list = [...(prev[tid] ?? [])];
        const i = list.findIndex((m) => m.id === msgId);
        if (i < 0) return prev;
        const m = list[i];
        if (!m.confirmation) return prev;
        list[i] = { ...m, confirmation: { ...m.confirmation, ...patch } };
        return { ...prev, [tid]: list };
      });
    },
    [],
  );

  const [debugByThread, setDebugByThread] = useState<Record<string, any>>({});
  const activeDebug = active ? (debugByThread[active.id] ?? null) : null;

  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, messages]);

  const [input, setInput] = useState<string>("");
  const [running, setRunning] = useState(false);
  const [streamMode] = useState<"sync" | "sse">("sse");
  const abortRef = useRef<AbortController | null>(null);

  // 新建会话：真实调用 POST /api/conversations，由后端生成 thread_id。
  // 返回新建的会话对象（失败/并发去重时返回 null）
  const newThread = useCallback(async (): Promise<ThreadVM | null> => {
    if (creatingRef.current) return null;
    creatingRef.current = true;
    setCreatingThread(true);
    setThreadsError(null);
    try {
      const resp = await fetch("/api/conversations", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Tenant-Id": tenantId,
          Authorization: bearer,
        },
        body: JSON.stringify({}),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
      const row = (await resp.json()) as ConversationThreadDTO;
      const nt = toThreadVM(row);
      // 新建的空会话无需拉历史
      hydratedRef.current.add(nt.id);
      setThreads((prev) =>
        prev.some((t) => t.id === nt.id) ? prev : [nt, ...prev],
      );
      setActiveId(nt.id);
      setNav("chat");
      setInput("");
      return nt;
    } catch (e) {
      setThreadsError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      creatingRef.current = false;
      setCreatingThread(false);
    }
  }, [bearer, tenantId]);

  // consumer 首次加载/切换身份：自动创建并打开一个新会话，再拉取会话列表。
  // 新建的空会话 last_message_at 为 NULL，后端按 NULLS LAST 排序置底，且会话数
  // 超过 limit=50 时会被截断 —— 因此拉取列表后必须本地补回并置顶，保证打开的是新会话。
  useEffect(() => {
    if (!isConsumer) {
      setThreads([]);
      setActiveId("");
      setThreadsError(null);
      return;
    }
    const bootKey = `${current.tenant_id}:${current.actor_id}`;
    hydratedRef.current = new Set();
    setMessagesByThread({});
    const ensureTop = (t: ThreadVM) => {
      setThreads((prev) => {
        const i = prev.findIndex((x) => x.id === t.id);
        if (i === 0) return prev;
        if (i > 0) {
          const list = [...prev];
          const [m] = list.splice(i, 1);
          return [m, ...list];
        }
        return [t, ...prev];
      });
    };
    void (async () => {
      let nt = autoBootedThreads.get(bootKey) ?? null;
      if (!nt) {
        let p = autoBootPromises.get(bootKey);
        if (!p) {
          p = newThread().then((t) => {
            if (t) autoBootedThreads.set(bootKey, t);
            else autoBootPromises.delete(bootKey); // 失败允许下次重试
            return t;
          });
          autoBootPromises.set(bootKey, p);
        }
        nt = await p;
      }
      if (!nt) {
        // 创建失败（后端不可用等）：退回加载历史列表
        await loadThreads();
        return;
      }
      hydratedRef.current.add(nt.id);
      ensureTop(nt);
      setActiveId(nt.id);
      await loadThreads({ keepActive: true });
      ensureTop(nt);
      // keepActive 可能因新会话被 limit=50 截断而回退选中旧会话，这里强制回到新会话
      setActiveId(nt.id);
    })();
  }, [isConsumer, current.actor_id, current.tenant_id, loadThreads, newThread]);

  const send = useCallback(async () => {
    if (!active || running || !input.trim()) return;
    const text = input.trim();
    setInput("");
    setRunning(true);
    const idempotency_key = uuidv4();

    append(active.id, { role: "human", text });

    try {
      if (streamMode === "sync") {
        // 同步 run
        append(active.id, { role: "agent", text: "…", streaming: true });
        const resp = await fetch(`/api/agent/conversations/${active.id}/run`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Tenant-Id": tenantId,
            Authorization: bearer,
          },
          body: JSON.stringify({ text, idempotency_key }),
        });
        if (!resp.ok) {
          finalizeLastAgent(
            active.id,
            `❌ 请求失败：HTTP ${resp.status} ${resp.statusText}`,
          );
        } else {
          const body = await resp.json();
          if (body.escalated && body.escalated_ticket_no) {
            append(active.id, {
              role: "handoff",
              handoff: {
                ticket_no: body.escalated_ticket_no,
                reason: body.escalation_reason,
              },
              text: "",
            });
          }
          finalizeLastAgent(active.id, body.final_reply ?? "(空响应)");
          if (body.decision_debug) {
            setDebugByThread((prev) => ({
              ...prev,
              [active.id]: body.decision_debug,
            }));
          }
        }
      } else {
        // SSE 流式
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        // 本轮是否暂停在 HITL 确认卡片：暂停后无回复文本，[DONE] 时不再 finalize 占位气泡
        let hitlPaused = false;

        await streamChat({
          url: `/api/agent/conversations/${active.id}/stream`,
          tenantId,
          bearer,
          body: { text, idempotency_key },
          signal: ctrl.signal,
          onEvent: (evt) => {
            switch (evt.event) {
              case "start":
                append(active.id, { role: "agent", text: "", streaming: true });
                break;
              case "escalated":
                append(active.id, {
                  role: "handoff",
                  handoff: {
                    ticket_no: evt.data.ticket_no,
                    reason: evt.data.reason,
                  },
                  text: "",
                });
                break;
              case "tool":
                append(active.id, {
                  role: "tool",
                  toolCall: {
                    kind: evt.data.kind ?? "tool",
                    payload: evt.data.payload ?? null,
                  },
                  text: "",
                });
                break;
              case "confirmation_required": {
                // HITL 暂停：写工具 interrupt 触发，本轮只有确认卡片、没有回复文本
                hitlPaused = true;
                removeLastEmptyStreamingAgent(active.id);
                const pa = evt.data?.pending_action ?? {};
                append(active.id, {
                  role: "confirmation",
                  text: "",
                  confirmation: {
                    tool: pa.tool ?? "unknown",
                    args: pa.args ?? pa.arguments ?? {},
                    actionLabel: pa.action_label,
                    orderNo: pa.order_no,
                    fields: Array.isArray(pa.fields) ? pa.fields : undefined,
                    expiresAt: pa.expires_at,
                    status: "pending",
                  },
                });
                break;
              }
              case "resume_started":
                // /actions/confirm 端点专用：续跑已开始，后续走 reply_chunk/reply/done
                // 主动新建一条 streaming agent 气泡承接后续 reply_chunk（与 start 行为对齐）
                append(active.id, { role: "agent", text: "", streaming: true });
                break;
              case "reply_chunk": {
                // ★ 真正的 token 级增量：直接追加到最后一条 agent 消息尾部
                const delta = evt.data?.text ?? "";
                if (delta) appendToLastAgent(active.id, delta);
                break;
              }
              case "reply": {
                const text2 = evt.data?.text ?? "";
                // 如果已经收到了 reply_chunk，则 reply 仅做 finalize 不重写内容（向后兼容）
                finalizeLastAgent(active.id, text2);
                break;
              }
              case "debug":
                if (evt.data && evt.data !== "[DONE]") {
                  const payload = evt.data.payload ?? evt.data;
                  setDebugByThread((prev) => ({
                    ...prev,
                    [active.id]: payload,
                  }));
                  // 方案 A：把 RAG 命中作为引用角标挂到本轮最后一条 agent 消息
                  const hits = Array.isArray(payload?.rag_hits)
                    ? payload.rag_hits
                    : null;
                  if (hits && hits.length > 0) {
                    setMessagesByThread((prev) => {
                      const list = [...(prev[active.id] ?? [])];
                      const i = _lastAgentIndex(list);
                      if (i < 0) return prev;
                      const m = list[i];
                      if (m.role !== "agent") return prev;
                      // 后端 rag_retrieve_node 顶层字段：chunk_id/content/similarity/metadata{doc_name,title}
                      // 兼容 vectorstore.search 直返的顶层 title/source
                      const citations = hits.map((h: any) => {
                        const meta =
                          h && typeof h.metadata === "object" ? h.metadata : {};
                        return {
                          chunk_id:
                            typeof h?.chunk_id === "string"
                              ? h.chunk_id
                              : undefined,
                          content:
                            typeof h?.content === "string" ? h.content : "",
                          title:
                            (typeof meta.title === "string" && meta.title) ||
                            (typeof h?.title === "string" && h.title) ||
                            undefined,
                          doc_name:
                            typeof meta.doc_name === "string"
                              ? meta.doc_name
                              : undefined,
                          similarity:
                            typeof h?.similarity === "number"
                              ? h.similarity
                              : undefined,
                        };
                      });
                      list[i] = { ...m, citations };
                      return { ...prev, [active.id]: list };
                    });
                  }
                }
                break;
              case "node":
                // 调试事件：节点开始/结束。可在顶部做小进度条（当前仅记录，不打扰用户）
                break;
              case "error": {
                const t =
                  (evt.data?.message as string) ||
                  (typeof evt.data === "string" ? evt.data : "") ||
                  "服务端未返回错误详情";
                appendErrorToLastAgent(active.id, `生成失败：${t}`);
                break;
              }
              case "done":
                if (evt.data === "[DONE]") {
                  // HITL 暂停本轮无回复（占位已移除），不要再兜底出"（服务端无回复）"
                  if (!hitlPaused) finalizeLastAgent(active.id);
                }
                break;
              default:
                break;
            }
          },
        }).catch((err) => {
          appendErrorToLastAgent(
            active.id,
            `SSE 请求失败：${err?.message ?? String(err)}（演示场景：后端 HTTP 尚未启动时此错误为预期，可启动 backend:8000 或使用 Mock 面板开启本地模拟）`,
          );
        });
      }
    } finally {
      setRunning(false);
      abortRef.current = null;
      // 一轮对话结束后刷新真实会话列表（后端 last_message_at 会变化 → 列表重排）
      void loadThreads({ keepActive: true });
    }
  }, [
    active,
    append,
    appendErrorToLastAgent,
    appendToLastAgent,
    bearer,
    finalizeLastAgent,
    input,
    loadThreads,
    removeLastEmptyStreamingAgent,
    running,
    streamMode,
    tenantId,
  ]);

  // HITL 写操作确认：用户点击确认卡片按钮后，POST /actions/confirm 续跑暂停的图
  // 该端点也返回 SSE 流，事件序列与 /stream 一致（reply_chunk/reply/done/error）
  const confirmAction = useCallback(
    async (msgId: string, decision: boolean, reason?: string) => {
      if (!active || running) return;
      const tid = active.id;
      // 立刻把卡片状态切到 approved/declined，防止重复点击
      updateConfirmation(tid, msgId, {
        status: decision ? "approved" : "declined",
        reason,
      });
      setRunning(true);
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      try {
        await streamChat({
          url: `/api/agent/conversations/${tid}/actions/confirm`,
          tenantId,
          bearer,
          body: decision
            ? { decision: true }
            : { decision: false, reason: reason ?? "用户取消" },
          signal: ctrl.signal,
          onEvent: (evt) => {
            switch (evt.event) {
              case "resume_started":
                // 续跑已开始：新建 streaming agent 气泡承接后续 reply_chunk
                append(tid, { role: "agent", text: "", streaming: true });
                break;
              case "reply_chunk": {
                const delta = evt.data?.text ?? "";
                if (delta) appendToLastAgent(tid, delta);
                break;
              }
              case "reply": {
                const text2 = evt.data?.text ?? "";
                finalizeLastAgent(tid, text2);
                break;
              }
              case "error": {
                const t =
                  (evt.data?.message as string) ||
                  (typeof evt.data === "string" ? evt.data : "") ||
                  "服务端未返回错误详情";
                // 服务端检测无 pending（超时/已处理）→ 标记卡片为 timeout
                if (/超时|已被处理|过期|已处理/.test(t)) {
                  updateConfirmation(tid, msgId, {
                    status: "timeout",
                    reason: t,
                  });
                } else {
                  updateConfirmation(tid, msgId, {
                    status: "error",
                    reason: t,
                  });
                }
                appendErrorToLastAgent(tid, `确认操作失败：${t}`);
                break;
              }
              case "done":
                if (evt.data === "[DONE]") {
                  finalizeLastAgent(tid);
                }
                break;
              default:
                break;
            }
          },
        }).catch((err) => {
          appendErrorToLastAgent(
            tid,
            `确认请求失败：${err?.message ?? String(err)}`,
          );
        });
      } finally {
        setRunning(false);
        abortRef.current = null;
        void loadThreads({ keepActive: true });
      }
    },
    [
      active,
      running,
      append,
      appendErrorToLastAgent,
      appendToLastAgent,
      bearer,
      finalizeLastAgent,
      loadThreads,
      tenantId,
      updateConfirmation,
    ],
  );

  return (
    <div className="flex h-full min-h-screen flex-col bg-gradient-to-br from-brand-50/70 via-white to-slate-50">
      {/* ---------- 顶部栏 ---------- */}
      <header className="flex items-center justify-between border-b border-brand-100 bg-white/80 px-6 py-3 shadow-sm backdrop-blur">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-500 text-lg font-bold text-white shadow-sm">
            串
          </div>
          <div>
            <div className="text-sm font-bold text-slate-800">
              手串售后智能客服 · 演示 MVP
            </div>
          </div>
        </div>
        <div className="flex items-center gap-4">
          <IdentitySwitcher />
        </div>
      </header>

      <div className="flex min-h-0 flex-1 gap-4 overflow-hidden px-6 py-4">
        {/* ---------- 左栏：导航 + 会话列表 ---------- */}
        <aside className="flex min-h-0 w-64 flex-col gap-4">
          <nav className="rounded-xl border border-slate-200 bg-white p-2 shadow-sm">
            {NAV.filter((item) => allowedNavs.includes(item.key)).map(
              (item) => (
                <button
                  key={item.key}
                  onClick={() => setNav(item.key)}
                  className={
                    "mb-0.5 flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition " +
                    (nav === item.key
                      ? "bg-brand-500 text-white shadow"
                      : "text-slate-700 hover:bg-brand-50")
                  }
                >
                  <span className="text-base">{item.icon}</span>
                  <div className="flex-1">
                    <div className="font-medium">{item.label}</div>
                    <div
                      className={
                        "text-[10px] " +
                        (nav === item.key ? "text-white/80" : "text-slate-400")
                      }
                    >
                      {item.desc}
                    </div>
                  </div>
                </button>
              ),
            )}
          </nav>

          {/* 会话列表：仅消费者身份展示，数据来自 GET /api/conversations */}
          {isConsumer && (
            <div className="flex min-h-0 flex-1 flex-col rounded-xl border border-slate-200 bg-white shadow-sm">
              <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
                <div className="text-xs font-semibold text-slate-600">
                  会话列表
                </div>
                <button
                  type="button"
                  disabled={creatingThread}
                  onClick={() => void newThread()}
                  className="rounded-md bg-brand-500 px-2 py-1 text-[11px] font-medium text-white shadow hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-slate-300"
                >
                  {creatingThread ? "创建中…" : "＋ 新建"}
                </button>
              </div>
              <ul className="scrollbar-thin min-h-0 flex-1 space-y-1 overflow-y-auto p-2 text-xs">
                {threadsLoading ? (
                  <li className="px-2 py-6 text-center text-[11px] text-slate-400">
                    会话加载中…
                  </li>
                ) : threadsError ? (
                  <li className="space-y-2 px-2 py-3 text-center">
                    <div className="text-[11px] text-red-600">
                      会话加载失败：{threadsError}
                    </div>
                    <button
                      type="button"
                      onClick={() => void loadThreads()}
                      className="rounded-md border border-slate-200 px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-50"
                    >
                      重试
                    </button>
                  </li>
                ) : threads.length === 0 ? (
                  <li className="px-2 py-6 text-center text-[11px] text-slate-400">
                    暂无会话，点击「＋ 新建」开始对话
                  </li>
                ) : (
                  threads.map((t) => {
                    const isActive = t.id === activeId;
                    const d = new Date(t.createdAt);
                    const dayLabel = `${d.getMonth() + 1}/${d.getDate()}`;
                    // 无自定义标题时直接展示首条消息截断（历史数据写死的「新会话」也视为无标题）
                    const title = t.title && t.title !== "新会话" ? t.title : "";
                    return (
                      <li key={t.id}>
                        <button
                          type="button"
                          onClick={() => setActiveId(t.id)}
                          className={
                            "w-full rounded-lg px-2 py-2 text-left transition " +
                            (isActive
                              ? "bg-brand-500 text-white shadow"
                              : "hover:bg-slate-100 text-slate-700")
                          }
                        >
                          <div className="flex items-center justify-between">
                            <span className="truncate font-medium">
                              {title || t.summary || "新会话"}
                            </span>
                            <span
                              className={
                                "ml-2 shrink-0 text-[10px] " +
                                (isActive ? "text-white/70" : "text-slate-400")
                              }
                            >
                              {dayLabel}
                            </span>
                          </div>
                          {title && t.summary ? (
                            <div
                              className={
                                "mt-1 truncate text-[10px] " +
                                (isActive ? "text-white/80" : "text-slate-400")
                              }
                            >
                              {t.summary}
                            </div>
                          ) : null}
                        </button>
                      </li>
                    );
                  })
                )}
              </ul>
            </div>
          )}
        </aside>

        {/* ---------- 主区：聊天 / 其他占位页 ---------- */}
        <main className="flex min-w-0 flex-1 flex-col gap-4">
          {nav === "chat" ? (
            <>
              <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
                <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
                  <div>
                    <div className="text-sm font-semibold text-slate-800">
                      {active
                        ? active.title || active.summary || "新会话"
                        : "请选择或新建会话"}
                    </div>
                    {/* <div className="text-[11px] text-slate-400">
                      thread_id ={" "}
                      <span className="font-mono">{active?.id ?? "—"}</span>
                      <span className="mx-2 text-slate-300">|</span>
                      租户：
                      <span className="font-medium text-brand-700">
                        {current.tenant_name}
                      </span>
                      <span className="mx-2 text-slate-300">|</span>
                      身份：
                      <span className="font-medium">{current.username}</span>
                    </div> */}
                  </div>
                </div>

                <div
                  ref={scrollRef}
                  className="min-h-0 flex-1 space-y-1 overflow-y-auto bg-slate-50/60 px-4 py-4"
                >
                  {!active ? (
                    <NoThread
                      loading={threadsLoading}
                      creating={creatingThread}
                      onCreate={() => void newThread()}
                    />
                  ) : messages.length === 0 ? (
                    <EmptyChat onPick={setInput} />
                  ) : (
                    messages.map((m) => (
                      <MessageBubble
                        key={m.id}
                        msg={m}
                        onConfirm={(msgId, d, r) =>
                          void confirmAction(msgId, d, r)
                        }
                      />
                    ))
                  )}
                </div>

                {active && (
                  <div className="border-t border-slate-100 bg-white p-3">
                    <div className="flex items-end gap-2">
                      <textarea
                        rows={2}
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" && !e.shiftKey) {
                            e.preventDefault();
                            void send();
                          }
                        }}
                        placeholder={
                          running
                            ? "Agent 处理中…（可点击 [取消] 中止）"
                            : "输入消息后回车发送；Shift+Enter 换行"
                        }
                        className="min-h-[48px] flex-1 resize-none rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-brand-400 focus:ring-2 focus:ring-brand-200"
                      />
                      {running ? (
                        <button
                          type="button"
                          onClick={() => {
                            abortRef.current?.abort();
                            setRunning(false);
                          }}
                          className="h-[48px] shrink-0 rounded-lg border border-red-200 bg-red-50 px-4 text-sm font-medium text-red-600 hover:bg-red-100"
                        >
                          取消
                        </button>
                      ) : (
                        <button
                          type="button"
                          onClick={() => void send()}
                          disabled={!input.trim()}
                          className="h-[48px] shrink-0 rounded-lg bg-brand-500 px-5 text-sm font-semibold text-white shadow transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-slate-300"
                        >
                          发送
                        </button>
                      )}
                    </div>
                  </div>
                )}
              </section>

              <section className="shrink-0">
                <DebugDecisionPanel debug={activeDebug} tenant={tenantId} />
              </section>
            </>
          ) : nav === "orders" ? (
            <MyOrdersPage key="orders" />
          ) : nav === "policy" ? (
            <PolicyPage key="policy" />
          ) : nav === "eval" ? (
            <EvaluationPage key="eval" />
          ) : (
            <KnowledgePage key="knowledge" />
          )}
        </main>
      </div>

      {/* <footer className="border-t border-slate-100 bg-white/80 px-6 py-2 text-[11px] text-slate-400">
        演示令牌（本地开发，请勿提交真实密钥）· 此页仅用于面试展示
      </footer> */}
    </div>
  );
}

function NoThread({
  loading,
  creating,
  onCreate,
}: {
  loading: boolean;
  creating: boolean;
  onCreate: () => void;
}) {
  return (
    <div className="mx-auto flex max-w-xl flex-col items-center gap-4 py-16 text-center">
      <div className="text-5xl">💬</div>
      <div className="text-base font-semibold text-slate-700">
        {loading ? "正在加载会话…" : "还没有选中的会话"}
      </div>
      {!loading && (
        <button
          type="button"
          disabled={creating}
          onClick={onCreate}
          className="rounded-lg bg-brand-500 px-5 py-2 text-sm font-semibold text-white shadow transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {creating ? "创建中…" : "＋ 新建会话"}
        </button>
      )}
    </div>
  );
}

function EmptyChat({ onPick }: { onPick: (s: string) => void }) {
  return (
    <div className="mx-auto flex max-w-2xl flex-col items-center gap-4 py-10 text-center">
      <div className="text-5xl">📿</div>
      <div>
        <div className="text-xl font-semibold text-slate-800">
          您好，我是智能客服「小串」
        </div>
        <div className="mt-1 text-xs text-slate-500">
          多租户三层隔离 · 7 类意图可插拔识别 · 结构化政策 + RAG 混合架构 ·
          转人工卡片展示（不接入真实真人）
        </div>
      </div>
      <div className="grid w-full grid-cols-1 gap-2 sm:grid-cols-2">
        {EXAMPLE_PROMPTS.map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => onPick(p)}
            className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-left text-xs text-slate-700 shadow-sm transition hover:border-brand-300 hover:bg-brand-50"
          >
            {p}
          </button>
        ))}
      </div>
    </div>
  );
}

const ORDER_STATUS_LABEL: Record<string, string> = {
  pending_payment: "待付款",
  paid: "已付款",
  shipped: "已发货",
  delivered: "已签收",
  completed: "已完成",
  refunded: "已退款",
  cancelled: "已取消",
};

const ORDER_STATUS_BADGE_CLS: Record<string, string> = {
  pending_payment: "bg-amber-50 text-amber-700 border-amber-200",
  paid: "bg-blue-50 text-blue-700 border-blue-200",
  shipped: "bg-indigo-50 text-indigo-700 border-indigo-200",
  delivered: "bg-emerald-50 text-emerald-700 border-emerald-200",
  completed: "bg-slate-50 text-slate-700 border-slate-200",
  refunded: "bg-rose-50 text-rose-700 border-rose-200",
  cancelled: "bg-slate-50 text-slate-400 border-slate-200",
};

interface OrderListItem {
  order_id: string;
  tenant_id: string;
  order_no: string;
  buyer_user_id: string;
  status: keyof typeof ORDER_STATUS_LABEL;
  total_amount_yuan: number;
  product_type_summary: string | null;
  created_at: string;
}

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function MyOrdersPage() {
  const { current, bearer, tenantId } = useIdentity();
  const [orders, setOrders] = useState<OrderListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch("/api/orders?limit=50", {
      headers: { "X-Tenant-Id": tenantId, Authorization: bearer },
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
        return (await r.json()) as OrderListItem[];
      })
      .then((data) => {
        if (!cancelled) setOrders(data);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [current.actor_id, tenantId, bearer]);

  return (
    <section className="flex min-h-full flex-col gap-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="text-3xl">📦</div>
        <div>
          <div className="text-lg font-semibold text-slate-800">我的订单</div>
          {/* <div className="mt-0.5 text-xs text-slate-500">
            {current.tenant_name} · {current.username}（{current.role}）·
            仅展示当前用户订单
          </div> */}
        </div>
      </div>

      {loading && <div className="text-sm text-slate-500">加载中…</div>}

      {!loading && error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
          加载失败：{error}
        </div>
      )}

      {!loading && !error && orders.length === 0 && (
        <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50 p-4 text-xs text-slate-500">
          暂无订单
        </div>
      )}

      {!loading && !error && orders.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-slate-200">
          <table className="w-full text-left text-xs">
            <thead className="bg-slate-50 text-slate-500">
              <tr>
                <th className="px-3 py-2 font-medium">订单号</th>
                <th className="px-3 py-2 font-medium">商品类型</th>
                <th className="px-3 py-2 font-medium">状态</th>
                <th className="px-3 py-2 text-right font-medium">金额</th>
                <th className="px-3 py-2 font-medium">下单时间</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {orders.map((o) => (
                <tr key={o.order_id} className="hover:bg-slate-50/60">
                  <td className="px-3 py-2 font-mono text-slate-800">
                    {o.order_no}
                  </td>
                  <td className="px-3 py-2 text-slate-600">
                    {o.product_type_summary ?? "-"}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={`inline-block rounded border px-2 py-0.5 text-[11px] ${
                        ORDER_STATUS_BADGE_CLS[o.status] ??
                        "bg-slate-50 text-slate-700 border-slate-200"
                      }`}
                    >
                      {ORDER_STATUS_LABEL[o.status] ?? o.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right text-slate-800">
                    ¥ {(o.total_amount_yuan / 100).toFixed(2)}
                  </td>
                  <td className="px-3 py-2 text-slate-500">
                    {formatDateTime(o.created_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* <div className="rounded-lg border border-dashed border-brand-300 bg-brand-50/50 p-4 text-xs text-brand-800">
        ℹ️ 此页仅作展示，不提供其他操作。
      </div> */}
    </section>
  );
}

// GET /api/management/tenants/{tenant_id}/policy 返回的售后政策配置 DTO
interface PolicyConfigDTO {
  tenant_id: string;
  return_days: number | null;
  return_policy_type: "no_reason" | "quality_only" | "hybrid";
  restocking_fee_pct_non_quality: number;
  warranty_days_quality: number;
  custom_product_allowed_return: boolean;
  updated_by: string | null;
  updated_at: string;
  created_at: string;
}

const POLICY_TYPE_LABEL: Record<PolicyConfigDTO["return_policy_type"], string> = {
  no_reason: "7 天无理由退货",
  quality_only: "仅质量问题可退",
  hybrid: "质量问题 + 非质量退货（收手续费）",
};

function PolicyPage() {
  const { current, bearer, tenantId } = useIdentity();
  const [config, setConfig] = useState<PolicyConfigDTO | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(`/api/management/tenants/${tenantId}/policy`, {
        headers: { "X-Tenant-Id": tenantId, Authorization: bearer },
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
      setConfig((await resp.json()) as PolicyConfigDTO);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [tenantId, bearer]);

  useEffect(() => {
    void load();
  }, [load]);

  const rows: { label: string; hint: string; value: React.ReactNode }[] = config
    ? [
        {
          label: "售后类型",
          hint: "return_policy_type",
          value: (
            <span className="inline-block rounded border border-brand-200 bg-brand-50 px-2 py-0.5 text-[12px] font-medium text-brand-700">
              {POLICY_TYPE_LABEL[config.return_policy_type]}
            </span>
          ),
        },
        {
          label: "非质量退货窗口",
          hint: "return_days",
          value:
            config.return_days == null ? (
              <span className="text-rose-600">不支持非质量退货</span>
            ) : (
              <span className="text-slate-800">签收后 {config.return_days} 天内</span>
            ),
        },
        {
          label: "非质量退货手续费率",
          hint: "restocking_fee_pct_non_quality",
          value: <span className="text-slate-800">{config.restocking_fee_pct_non_quality}%</span>,
        },
        {
          label: "质量问题保修期",
          hint: "warranty_days_quality",
          value: <span className="text-slate-800">{config.warranty_days_quality} 天</span>,
        },
        {
          label: "定制款非质量退货",
          hint: "custom_product_allowed_return",
          value: config.custom_product_allowed_return ? (
            <span className="text-emerald-600">允许</span>
          ) : (
            <span className="text-rose-600">不允许（定制款仅质量问题可退）</span>
          ),
        },
      ]
    : [];

  return (
    <section className="flex min-h-full flex-col gap-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="text-3xl">⚙️</div>
        <div>
          <div className="text-lg font-semibold text-slate-800">售后政策管理</div>
          {/* <div className="mt-0.5 text-xs text-slate-500">
            {current.tenant_name} · {current.username}（{current.role}）· 结构化政策配置（Agent
            政策判定节点直接读取）
          </div> */}
        </div>
      </div>

      {loading && <div className="text-sm text-slate-500">加载中…</div>}

      {!loading && error && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
          <span>加载失败：{error}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="shrink-0 rounded-md border border-red-200 bg-white px-2 py-1 font-medium text-red-600 hover:bg-red-50"
          >
            重试
          </button>
        </div>
      )}

      {!loading && !error && config && (
        <>
          <div className="overflow-hidden rounded-lg border border-slate-200">
            <table className="w-full text-left text-xs">
              <thead className="bg-slate-50 text-slate-500">
                <tr>
                  <th className="w-56 px-3 py-2 font-medium">配置项</th>
                  <th className="px-3 py-2 font-medium">当前值</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {rows.map((r) => (
                  <tr key={r.hint} className="hover:bg-slate-50/60">
                    <td className="px-3 py-2.5 align-top">
                      <div className="font-medium text-slate-700">{r.label}</div>
                      <div className="mt-0.5 font-mono text-[10px] text-slate-400">{r.hint}</div>
                    </td>
                    <td className="px-3 py-2.5 text-[13px]">{r.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="text-[11px] text-slate-400">
            最后更新：{formatDateTime(config.updated_at)}
            <span className="mx-2 text-slate-300">|</span>
            更新人：
            <span className="font-mono">{config.updated_by ?? "系统默认"}</span>
          </div>
        </>
      )}

      {/* <div className="rounded-lg border border-dashed border-brand-300 bg-brand-50/50 p-4 text-xs text-brand-800">
        ℹ️ 数据来自{" "}
        <span className="font-mono">
          GET /api/management/tenants/{tenantId}/policy
        </span>
        ，按租户隔离；仅同租户 staff/admin 可修改（PUT）。
      </div> */}
    </section>
  );
}

// GET /api/management/tenants/{tenant_id}/knowledge/documents 返回的文档级列表项
interface KnowledgeDocumentDTO {
  tenant_id: string;
  doc_name: string;
  source: "policy_manual" | "faq" | "operation_doc";
  title: string | null;
  chunks: number;
}

const KNOWLEDGE_SOURCE_LABEL: Record<KnowledgeDocumentDTO["source"], string> = {
  faq: "FAQ 问答",
  policy_manual: "政策手册",
  operation_doc: "操作文档",
};

function KnowledgePage() {
  const { current, bearer, tenantId } = useIdentity();
  const [docs, setDocs] = useState<KnowledgeDocumentDTO[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState<KnowledgeDocumentDTO["source"]>("faq");
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  /** 当前展开查看的文档内容 */
  const [expanded, setExpanded] = useState<{
    docName: string;
    content: string;
  } | null>(null);
  const [contentLoading, setContentLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(
        `/api/management/tenants/${tenantId}/knowledge/documents`,
        { headers: { "X-Tenant-Id": tenantId, Authorization: bearer } },
      );
      if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
      setDocs((await resp.json()) as KnowledgeDocumentDTO[]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [tenantId, bearer]);

  useEffect(() => {
    void load();
  }, [load]);

  const upload = useCallback(async () => {
    if (!file || uploading) return;
    setUploading(true);
    setError(null);
    setNotice(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("source", source);
      const resp = await fetch(
        `/api/management/tenants/${tenantId}/knowledge/documents`,
        {
          method: "POST",
          // multipart 边界由浏览器自动设置，勿手动指定 Content-Type
          headers: { "X-Tenant-Id": tenantId, Authorization: bearer },
          body: fd,
        },
      );
      if (!resp.ok) {
        let detail = `HTTP ${resp.status} ${resp.statusText}`;
        try {
          const body = await resp.json();
          if (body?.detail)
            detail =
              typeof body.detail === "string"
                ? body.detail
                : JSON.stringify(body.detail);
        } catch {
          /* 非 JSON 错误体，保留 HTTP 状态描述 */
        }
        throw new Error(detail);
      }
      const body = (await resp.json()) as { doc_name: string; chunks: number };
      setNotice(
        `已上传「${body.doc_name}」，写入 ${body.chunks} 个切片（同名文档已覆盖旧版本）`,
      );
      setFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  }, [file, uploading, source, tenantId, bearer, load]);

  /** 展开/收起文档内容（切片按写入顺序拼接） */
  const toggleContent = useCallback(
    async (docName: string) => {
      if (expanded?.docName === docName) {
        setExpanded(null);
        return;
      }
      setContentLoading(true);
      setError(null);
      try {
        const resp = await fetch(
          `/api/management/tenants/${tenantId}/knowledge/documents/${encodeURIComponent(docName)}`,
          { headers: { "X-Tenant-Id": tenantId, Authorization: bearer } },
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        const body = (await resp.json()) as { content: string };
        setExpanded({ docName, content: body.content });
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setContentLoading(false);
      }
    },
    [expanded, tenantId, bearer],
  );

  return (
    <section className="flex min-h-full flex-col gap-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="flex items-center gap-3">
        <div className="text-3xl">📚</div>
        <div>
          <div className="text-lg font-semibold text-slate-800">
            FAQ / 知识库管理
          </div>
          {/* <div className="mt-0.5 text-xs text-slate-500">
            {current.tenant_name} · {current.username}（{current.role}）
          </div> */}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-slate-200 bg-slate-50/60 p-3">
        <input
          ref={fileInputRef}
          type="file"
          accept=".md,.markdown,.txt"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="text-xs text-slate-600 file:mr-2 file:rounded-md file:border-0 file:bg-brand-50 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-brand-700 hover:file:bg-brand-100"
        />
        <select
          value={source}
          onChange={(e) =>
            setSource(e.target.value as KnowledgeDocumentDTO["source"])
          }
          className="rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs text-slate-700 outline-none focus:border-brand-400"
        >
          {(
            Object.entries(KNOWLEDGE_SOURCE_LABEL) as [
              KnowledgeDocumentDTO["source"],
              string,
            ][]
          ).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={!file || uploading}
          onClick={() => void upload()}
          className="rounded-lg bg-brand-500 px-4 py-1.5 text-xs font-semibold text-white shadow transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {uploading ? "上传中…" : "上传文档"}
        </button>
      </div>

      {notice && (
        <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 text-xs text-emerald-700">
          {notice}
        </div>
      )}

      {loading && <div className="text-sm text-slate-500">加载中…</div>}

      {!loading && error && (
        <div className="flex items-center justify-between gap-3 rounded-lg border border-red-200 bg-red-50 p-3 text-xs text-red-700">
          <span>操作失败：{error}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="shrink-0 rounded-md border border-red-200 bg-white px-2 py-1 font-medium text-red-600 hover:bg-red-50"
          >
            重试
          </button>
        </div>
      )}

      {!loading && !error && docs.length === 0 && (
        <div className="rounded-lg border border-dashed border-slate-300 bg-slate-50 p-4 text-xs text-slate-500">
          暂无知识文档，请先上传
        </div>
      )}

      {!loading && docs.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-slate-200">
          <table className="w-full text-left text-xs">
            <thead className="bg-slate-50 text-slate-500">
              <tr>
                <th className="px-3 py-2 font-medium">文档名</th>
                <th className="px-3 py-2 font-medium">标题</th>
                <th className="px-3 py-2 font-medium">来源</th>
                <th className="px-3 py-2 text-right font-medium">切片数</th>
                <th className="px-3 py-2 text-right font-medium">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {docs.map((d) => (
                <tr key={d.doc_name} className="hover:bg-slate-50/60">
                  <td className="px-3 py-2 font-mono text-slate-800">
                    {d.doc_name}
                  </td>
                  <td className="px-3 py-2 text-slate-600">{d.title ?? "-"}</td>
                  <td className="px-3 py-2">
                    <span className="inline-block rounded border border-brand-200 bg-brand-50 px-2 py-0.5 text-[11px] text-brand-700">
                      {KNOWLEDGE_SOURCE_LABEL[d.source] ?? d.source}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right text-slate-800">
                    {d.chunks}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      disabled={contentLoading}
                      onClick={() => void toggleContent(d.doc_name)}
                      className="rounded-md border border-slate-200 bg-white px-2 py-1 text-[11px] font-medium text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
                    >
                      {expanded?.docName === d.doc_name ? "收起" : "查看内容"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {expanded && (
        <div className="rounded-lg border border-slate-200 bg-slate-50/60">
          <div className="flex items-center justify-between border-b border-slate-200 px-3 py-2">
            <span className="font-mono text-xs font-medium text-slate-700">
              {expanded.docName}
            </span>
            <button
              type="button"
              onClick={() => setExpanded(null)}
              className="rounded-md border border-slate-200 bg-white px-2 py-0.5 text-[11px] text-slate-500 hover:bg-slate-50"
            >
              关闭
            </button>
          </div>
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words p-3 text-xs text-slate-700">
{expanded.content}
          </pre>
        </div>
      )}
    </section>
  );
}

import { useEffect, useMemo, useState } from "react";

export interface ConfirmationField {
  label: string;
  value: string;
}

/** RAG 命中引用：debug 事件 payload.rag_hits[*] 的精简结构 */
export interface Citation {
  chunk_id?: string;
  content: string;
  title?: string;
  doc_name?: string;
  similarity?: number;
}

export interface ChatMessageVM {
  id: string;
  role: "human" | "agent" | "tool" | "handoff" | "system" | "confirmation";
  text: string;
  createdAt: number;
  /** 流式接收中：显示打字机光标 */
  streaming?: boolean;
  /** 转人工事件 payload */
  handoff?: { ticket_no: string; reason?: string };
  /** 工具调用结果（T10 policy_decision） */
  toolCall?: { kind: string; payload: any };
  /** HITL 写操作确认卡片：后端 emit confirmation_required 时渲染 */
  confirmation?: {
    tool: string;
    args: any;
    /** 中文操作名，如 退款申请/换货申请/维修申请/取消订单 */
    actionLabel?: string;
    /** 业务订单号（卡片突出展示） */
    orderNo?: string;
    /** 结构化详情行（原因/金额/工单号等） */
    fields?: ConfirmationField[];
    /** 确认截止时间 ISO（TTL 10min，用于倒计时） */
    expiresAt?: string;
    status: "pending" | "approved" | "declined" | "timeout" | "error";
    reason?: string;
  };
  /** RAG 引用角标：本轮回复结束时由 debug 事件携带，挂在最后一条 agent 消息上 */
  citations?: Citation[];
}

function formatTime(ts: number) {
  const d = new Date(ts);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

export function MessageBubble({
  msg,
  onConfirm,
}: {
  msg: ChatMessageVM;
  /** HITL 确认卡片按钮回调；仅 role === "confirmation" 且 status === "pending" 时使用 */
  onConfirm?: (msgId: string, decision: boolean, reason?: string) => void;
}) {
  const isMe = msg.role === "human";

  /** 流式占位阶段（尚无真实文本）：显示“正在思考中”而非裸光标 */
  const thinking = !!msg.streaming && (!msg.text || msg.text === "…");

  const textClass = useMemo(() => {
    if (msg.role === "system") return "italic text-slate-500";
    if (msg.streaming && !thinking)
      return "typewriter whitespace-pre-wrap break-words";
    return "whitespace-pre-wrap break-words";
  }, [msg.role, msg.streaming, thinking]);

  if (msg.role === "confirmation" && msg.confirmation) {
    return (
      <ConfirmationCard
        c={msg.confirmation}
        onConfirm={(decision, reason) => onConfirm?.(msg.id, decision, reason)}
      />
    );
  }

  if (msg.role === "handoff" || msg.handoff) {
    const handoff = msg.handoff;
    return (
      <div className="mx-auto my-2 max-w-md rounded-xl border border-amber-200 bg-gradient-to-br from-amber-50 to-orange-50 p-4 text-sm shadow-sm ring-1 ring-amber-100">
        <div className="mb-2 flex items-center gap-2">
          <span className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-amber-500 text-sm font-bold text-white">
            人
          </span>
          <div className="font-semibold text-amber-900">已接入人工客服队列</div>
        </div>
        {handoff ? (
          <>
            <div className="mb-1 flex items-center gap-2 text-xs text-amber-800">
              工单号：
              <span className="rounded bg-amber-100 px-1.5 py-0.5 font-mono text-amber-900">
                {handoff.ticket_no}
              </span>
            </div>
            {handoff.reason ? (
              <div className="mt-2 text-xs text-amber-800/90">接入原因：{handoff.reason}</div>
            ) : null}
          </>
        ) : null}
      </div>
    );
  }

  if (msg.role === "tool") {
    return (
      <div className="my-2 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs text-slate-600">
        <div className="mb-1 font-semibold text-slate-700">
          🧰 工具结果
          {msg.toolCall?.kind ? <span className="ml-2 text-slate-400">({msg.toolCall.kind})</span> : null}
        </div>
        <pre className="max-h-40 overflow-auto rounded border border-slate-200 bg-white p-2 text-[11px] text-slate-700">
{msg.toolCall && typeof msg.toolCall.payload === "object"
  ? JSON.stringify(msg.toolCall.payload, null, 2)
  : msg.text}
        </pre>
      </div>
    );
  }

  const hasCitations = !isMe && !thinking && Array.isArray(msg.citations) && msg.citations.length > 0;

  return (
    <div className={"my-2 flex " + (isMe ? "justify-end" : "justify-start")}>
      <div
        className={
          "max-w-[80%] rounded-2xl px-4 py-3 text-sm shadow-sm " +
          (isMe
            ? "rounded-br-sm bg-brand-500 text-white"
            : "rounded-bl-sm border border-slate-200 bg-white text-slate-800")
        }
      >
        <div className={textClass}>
          {thinking ? (
            <span className="text-slate-400">
              正在思考中
              <span className="thinking-dots" aria-hidden="true">
                <i>.</i>
                <i>.</i>
                <i>.</i>
              </span>
            </span>
          ) : (
            msg.text || " "
          )}
          {hasCitations ? <Citations citations={msg.citations!} /> : null}
        </div>
        <div
          className={
            "mt-1 text-[10px] " + (isMe ? "text-white/70" : "text-slate-400")
          }
        >
          {formatTime(msg.createdAt)}
        </div>
      </div>
    </div>
  );
}

/** RAG 引用角标 + hover tooltip：纯 CSS 实现，无第三方依赖 */
function Citations({ citations }: { citations: Citation[] }) {
  return (
    <sup className="ml-1 inline-flex align-top">
      {citations.map((c, i) => {
        const idx = i + 1;
        const title = c.title?.trim() || c.doc_name?.trim() || "";
        const sim =
          typeof c.similarity === "number"
            ? `${(c.similarity * 100).toFixed(0)}%`
            : null;
        return (
          <span key={i} className="group relative ml-0.5 align-top">
            <span className="cursor-default rounded px-1 text-[10px] font-medium text-brand-600 ring-1 ring-inset ring-brand-200 group-hover:bg-brand-100">
              [{idx}]
            </span>
            <div
              role="tooltip"
              className="pointer-events-none absolute bottom-full left-1/2 z-20 mb-1 w-72 -translate-x-1/2 scale-95 rounded-lg border border-slate-200 bg-white p-2 text-[11px] text-slate-700 opacity-0 shadow-lg transition-all duration-100 group-hover:pointer-events-auto group-hover:scale-100 group-hover:opacity-100"
            >
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="font-mono text-[10px] text-brand-700">
                  引用 {idx}
                </span>
                {sim ? (
                  <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] text-emerald-700">
                    相似度 {sim}
                  </span>
                ) : null}
              </div>
              {title ? (
                <div className="mb-1 truncate font-medium text-slate-800">
                  {title}
                </div>
              ) : null}
              <div className="max-h-40 overflow-auto whitespace-pre-wrap break-words leading-relaxed text-slate-600">
                {c.content?.trim() || "（空切片）"}
              </div>
            </div>
          </span>
        );
      })}
    </sup>
  );
}

/** HITL 写操作确认卡片：操作名 + 订单号 + 结构化详情 + 10min 倒计时 */
function ConfirmationCard({
  c,
  onConfirm,
}: {
  c: NonNullable<ChatMessageVM["confirmation"]>;
  onConfirm: (decision: boolean, reason?: string) => void;
}) {
  const actionLabel = c.actionLabel || "写操作";
  const pending = c.status === "pending";

  // 剩余确认秒数（expiresAt 为 UTC ISO）；仅 pending 态计时
  const [remaining, setRemaining] = useState<number | null>(
    c.expiresAt
      ? Math.max(0, Math.floor((new Date(c.expiresAt).getTime() - Date.now()) / 1000))
      : null,
  );
  useEffect(() => {
    if (!pending || !c.expiresAt) return;
    const tick = () =>
      setRemaining(
        Math.max(0, Math.floor((new Date(c.expiresAt!).getTime() - Date.now()) / 1000)),
      );
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [pending, c.expiresAt]);
  const expired = remaining !== null && remaining <= 0;
  const countdown =
    remaining !== null
      ? `${String(Math.floor(remaining / 60)).padStart(2, "0")}:${String(
          remaining % 60,
        ).padStart(2, "0")}`
      : null;

  const statusText =
    c.status === "approved"
      ? "✓ 已确认，正在执行…"
      : c.status === "declined"
        ? `✗ 已取消${c.reason ? `：${c.reason}` : ""}`
        : c.status === "timeout"
          ? "⚠ 操作已超时，请重新发起"
          : c.status === "error"
            ? `⚠ ${c.reason ?? "执行失败"}`
            : null;

  return (
    <div className="mx-auto my-2 w-full max-w-md rounded-xl border border-brand-200 bg-white p-4 text-sm shadow-sm">
      <div className="mb-3 flex items-center gap-2">
        <span className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-brand-500 text-sm font-bold text-white">
          !
        </span>
        <div className="font-semibold text-slate-800">请确认：{actionLabel}</div>
      </div>

      {c.orderNo ? (
        <div className="mb-2 flex items-center gap-2 rounded-lg bg-brand-50 px-3 py-2">
          <span className="text-xs text-slate-500">订单号</span>
          <span className="font-mono text-sm font-semibold text-brand-900">
            {c.orderNo}
          </span>
        </div>
      ) : null}

      {c.fields && c.fields.length > 0 ? (
        <dl className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {c.fields.map((f, i) => (
            <div
              key={i}
              className="flex items-start justify-between gap-3 px-3 py-1.5 text-xs"
            >
              <dt className="shrink-0 text-slate-500">{f.label}</dt>
              <dd className="break-all text-right font-medium text-slate-800">
                {f.value}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {pending ? (
        expired ? (
          <div className="mt-3 text-center text-xs text-slate-400">
            ⚠ 操作已超时，请重新发起
          </div>
        ) : (
          <>
            {countdown !== null ? (
              <div className="mt-2 text-right text-[11px] text-slate-400">
                {countdown} 后超时
              </div>
            ) : null}
            <div className="mt-2 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={() => onConfirm(false, "用户取消")}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={() => onConfirm(true)}
                className="rounded-lg bg-brand-500 px-3 py-1.5 text-xs font-semibold text-white shadow transition hover:bg-brand-600"
              >
                确认执行
              </button>
            </div>
          </>
        )
      ) : (
        <div className="mt-2 text-[11px] text-slate-500">{statusText}</div>
      )}
    </div>
  );
}

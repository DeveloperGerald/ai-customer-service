import { useMemo } from "react";

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
    status: "pending" | "approved" | "declined" | "timeout" | "error";
    reason?: string;
  };
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
    const c = msg.confirmation;
    const pending = c.status === "pending";
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
      <div className="mx-auto my-2 max-w-md rounded-xl border border-brand-200 bg-gradient-to-br from-brand-50 to-white p-4 text-sm shadow-sm ring-1 ring-brand-100">
        <div className="mb-2 flex items-center gap-2">
          <span className="inline-flex h-7 w-7 items-center justify-center rounded-full bg-brand-500 text-sm font-bold text-white">
            !
          </span>
          <div className="font-semibold text-brand-900">写操作需要您确认</div>
        </div>
        <div className="mb-1 text-xs text-brand-800">
          工具：<span className="font-mono">{c.tool}</span>
        </div>
        <pre className="max-h-40 overflow-auto rounded border border-brand-200 bg-white p-2 text-[11px] text-slate-700">
{typeof c.args === "object" && c.args !== null ? JSON.stringify(c.args, null, 2) : String(c.args)}
        </pre>
        {pending ? (
          <div className="mt-3 flex items-center justify-end gap-2">
            <button
              type="button"
              onClick={() => onConfirm?.(msg.id, false, "用户取消")}
              className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
            >
              取消
            </button>
            <button
              type="button"
              onClick={() => onConfirm?.(msg.id, true)}
              className="rounded-lg bg-brand-500 px-3 py-1.5 text-xs font-semibold text-white shadow transition hover:bg-brand-600"
            >
              确认执行
            </button>
          </div>
        ) : (
          <div className="mt-2 text-[11px] text-slate-500">{statusText}</div>
        )}
      </div>
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
            msg.text || " "
          )}
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

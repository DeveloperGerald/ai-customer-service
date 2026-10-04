import React from "react";

/** 前端「决策依据」调试面板（面试演示 #2 关键面板）：
 *  - intent / reason_code / 金额计算等
 *  - 纯展示，后端同步响应 decision_debug / SSE debug 事件 payload 直接渲染
 */
export function DebugDecisionPanel({ debug, tenant }: { debug: any; tenant: string }) {
  const pretty = (v: any) =>
    v === undefined || v === null
      ? ""
      : typeof v === "object"
        ? JSON.stringify(v, null, 2)
        : String(v);

  if (!debug) {
    return (
      <div className="rounded-lg border border-dashed border-slate-200 bg-slate-50 p-3 text-xs text-slate-400">
        暂无决策依据；发送消息后将展示 Agent 内部判定过程（意图分类、RAG 引用、退款资格、是否转人工等）。
      </div>
    );
  }

  const policy = debug.policy_decision ?? null;
  const intent = debug.intent ?? "";
  const action = debug.action ?? "";
  const router_path = debug.router_path ?? null;

  return (
    <div></div>
//     <div className="rounded-xl border border-brand-100 bg-white p-4 shadow-sm">
//       <div className="mb-2 flex items-center justify-between">
//         <div className="flex items-center gap-2 text-sm font-semibold text-brand-800">
//           <span>🛠️ 决策依据 Debug Panel</span>
//           <span className="rounded bg-brand-50 px-2 py-0.5 text-[11px] font-medium text-brand-700">
//             {tenant}
//           </span>
//         </div>
//         <span className="text-[10px] text-slate-400">仅本地演示；生产可关闭</span>
//       </div>

//       <div className="grid grid-cols-2 gap-2 text-[11px]">
//         <Badge label="意图 intent" value={intent} />
//         <Badge label="动作 action" value={action} />
//       </div>
//       {router_path ? (
//         <div className="mt-2 rounded border border-brand-100 bg-brand-50/60 p-2 text-[11px] text-brand-800">
//           Router 命中路径：<span className="font-mono">{router_path}</span>
//         </div>
//       ) : null}

//       {policy ? (
//         <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-[11px] text-slate-700">
//           <div className="mb-2 flex items-center justify-between">
//             <span className="font-semibold text-slate-800">Policy（退款资格判定）</span>
//             <span className="rounded bg-slate-200/80 px-1.5 py-0.5 font-mono text-slate-700">
//               {policy.reason_code ?? "unknown"}
//             </span>
//           </div>
//           <div className="grid grid-cols-2 gap-1.5">
//             <KV k="可退 can_refund" v={Boolean(policy.can_refund) ? "✅ 是" : "否"} />
//             <KV k="可换 can_exchange" v={Boolean(policy.can_exchange) ? "✅ 是" : "否"} />
//             <KV k="可修 can_repair" v={Boolean(policy.can_repair) ? "✅ 是" : "否"} />
//             <KV
//               k="举证要求"
//               v={
//                 Boolean(policy.requires_quality_evidence) ? "需提供质量凭证" : "无额外凭证"
//               }
//             />
//             {typeof policy.restocking_fee_pct === "number" ? (
//               <KV k="手续费 %" v={`${policy.restocking_fee_pct}%`} />
//             ) : null}
//             {typeof policy.refund_amount_cents === "number" ? (
//               <KV
//                 k="预计退款"
//                 v={`¥${(policy.refund_amount_cents / 100).toFixed(2)}（${policy.refund_amount_cents} cents）`}
//               />
//             ) : null}
//           </div>
//           {policy.reason_human_readable ? (
//             <div className="mt-2 rounded border border-slate-200 bg-white p-2 text-[11px] italic text-slate-600">
//               人读解释：{policy.reason_human_readable}
//             </div>
//           ) : null}
//           {policy.debug ? (
//             <details className="mt-2 text-[11px] text-slate-500">
//               <summary className="cursor-pointer">扩展字段 debug_extra</summary>
//               <pre className="mt-1 overflow-auto rounded border border-slate-200 bg-white p-2">
// {pretty(policy.debug)}
//               </pre>
//             </details>
//           ) : null}
//         </div>
//       ) : null}

//       <details className="mt-3 text-[11px] text-slate-500">
//         <summary className="cursor-pointer">完整 payload（debug JSON）</summary>
//         <pre className="mt-1 max-h-60 overflow-auto rounded border border-slate-200 bg-slate-50 p-2 text-[11px] text-slate-700">
// {pretty(debug)}
//         </pre>
//       </details>
//     </div>
  );
}

function Badge({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-slate-200 bg-white px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 truncate font-mono text-slate-700">{value || "—"}</div>
    </div>
  );
}

function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-2 rounded border border-white bg-white/70 px-1.5 py-1">
      <span className="text-slate-500">{k}</span>
      <span className="font-medium text-slate-800">{v}</span>
    </div>
  );
}

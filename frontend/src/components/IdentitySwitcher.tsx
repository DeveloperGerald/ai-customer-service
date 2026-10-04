import React, { useMemo, useState } from "react";
import { useIdentity } from "../identity/IdentityContext";
import { Role } from "../demo-tokens";

const ROLE_LABEL: Record<Role, string> = {
  consumer: "消费者",
  staff: "客服",
  admin: "管理员",
};

/**
 * 顶部身份切换栏（面试演示 #1 关键组件）：3 行（租户）× 3 列（角色）的 9 宫格选择，
 * 点击切换身份后，所有 HTTP 请求的 Authorization + X-Tenant-Id 都会跟着变。
 */
export function IdentitySwitcher() {
  const { current, all, setCurrent } = useIdentity();
  const [open, setOpen] = useState(false);

  const tenants = useMemo(() => {
    const map = new Map<string, typeof all>();
    for (const d of all) {
      const list = map.get(d.tenant_id) ?? [];
      list.push(d);
      map.set(d.tenant_id, list);
    }
    return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [all]);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        className="flex items-center gap-3 rounded-lg border border-brand-200 bg-white px-3 py-2 shadow-sm transition hover:border-brand-400 hover:shadow"
      >
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-brand-500 text-sm font-bold text-white">
          {current.tenant_name.slice(0, 1)}
        </div>
        <div className="text-left text-sm">
          <div className="font-semibold text-brand-800">
            {current.tenant_name}
            <span className="ml-2 rounded bg-brand-100 px-1.5 py-0.5 text-xs font-medium text-brand-700">
              {ROLE_LABEL[current.role]}
            </span>
          </div>
          <div className="text-xs text-slate-500">切换身份</div>
        </div>
      </button>

      {open ? (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-50 mt-2 w-[420px] rounded-xl border border-brand-100 bg-white p-4 shadow-2xl ring-1 ring-black/5">
            <div className="mb-3 flex items-center justify-between">
              <div className="text-sm font-semibold text-slate-800">演示身份选择</div>
              {/* <div className="text-[11px] text-slate-400">3 租户 × 3 角色</div> */}
            </div>
            <div className="space-y-3">
              {tenants.map(([tid, rows]) => {
                const meta = rows[0];
                return (
                  <div key={tid} className="rounded-lg border border-slate-100 bg-slate-50/60 p-2">
                    <div className="mb-2 px-1 text-xs font-semibold text-slate-600">
                      {meta.tenant_name}
                      {/* <span className="ml-2 text-slate-400">tenant_id = {tid}</span> */}
                    </div>
                    <div className="grid grid-cols-3 gap-2">
                      {(["consumer", "staff", "admin"] as Role[]).map(role => {
                        const item = rows.find(r => r.role === role)!;
                        const active =
                          current.tenant_id === item.tenant_id && current.role === item.role;
                        return (
                          <button
                            key={`${tid}-${role}`}
                            onClick={() => {
                              setCurrent(item);
                              setOpen(false);
                            }}
                            className={
                              "rounded-md border px-2 py-2 text-center text-xs transition " +
                              (active
                                ? "border-brand-500 bg-brand-500 text-white shadow"
                                : "border-slate-200 bg-white text-slate-700 hover:border-brand-300 hover:bg-brand-50")
                            }
                          >
                            <div className="font-semibold">{ROLE_LABEL[role]}</div>
                            {/* <div className="mt-0.5 text-[10px] opacity-80">{item.username}</div> */}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}

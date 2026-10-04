import { useCallback, useEffect, useRef, useState } from "react";
import { useIdentity } from "../identity/IdentityContext";

type RunStatus = "idle" | "running" | "done" | "failed";

interface EvalStatus {
  status: RunStatus;
  total: number;
  completed: number;
  error: string | null;
  experiment_name: string | null;
  url: string | null;
}

interface ExperimentListItem {
  name: string;
  start_time: string | null;
  run_count: number;
  url: string | null;
}

interface Feedback {
  key: string;
  score: number | null;
  comment: string | null;
}

interface CaseRow {
  case_id: string;
  tenant_id: string;
  message: string;
  expected: Record<string, unknown>;
  final_reply: string | null;
  confirmation_required: boolean | null;
  feedbacks: Feedback[];
  trace_url: string | null;
}

interface ExperimentDetail {
  name: string;
  evaluator_scores: Record<string, { mean: number | null; count: number }>;
  cases: CaseRow[];
}

const EVALUATOR_LABELS: Record<string, string> = {
  intent_match: "意图准确",
  task_tool_match: "工具/参数",
  answer_contains: "关键事实",
  knowledge_correctness: "回答正确",
  faithfulness: "检索忠实",
};

export function EvaluationPage() {
  const { tenantId, bearer } = useIdentity();
  const [status, setStatus] = useState<EvalStatus | null>(null);
  const [experiments, setExperiments] = useState<ExperimentListItem[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [listLoading, setListLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [actionError, setActionError] = useState<string>("");
  const timerRef = useRef<number | null>(null);

  const request = useCallback(
    async <T,>(path: string, init?: RequestInit): Promise<T> => {
      const resp = await fetch(path, {
        ...init,
        headers: {
          "Content-Type": "application/json",
          "X-Tenant-Id": tenantId,
          Authorization: bearer,
          ...(init?.headers ?? {}),
        },
      });
      if (!resp.ok) {
        const body = (await resp.json().catch(() => null)) as
          | { message?: string }
          | null;
        throw new Error(body?.message ?? `HTTP ${resp.status}`);
      }
      return (await resp.json()) as T;
    },
    [tenantId, bearer],
  );

  const refreshExperiments = useCallback(async () => {
    const list = await request<ExperimentListItem[]>("/api/evaluations/experiments");
    setExperiments(list);
    return list;
  }, [request]);

  const stopPolling = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    stopPolling();
    timerRef.current = window.setInterval(async () => {
      const current = await request<EvalStatus>("/api/evaluations/status");
      setStatus(current);
      if (current.status !== "running") {
        stopPolling();
        const list = await refreshExperiments();
        const name = current.experiment_name;
        if (name && list.some((item) => item.name === name)) {
          setSelected(name);
        }
      }
    }, 2000);
  }, [request, stopPolling, refreshExperiments]);

  useEffect(() => {
    void (async () => {
      try {
        const [current, list] = await Promise.all([
          request<EvalStatus>("/api/evaluations/status"),
          refreshExperiments(),
        ]);
        setStatus(current);
        if (current.status === "running") {
          startPolling();
        } else if (list.length > 0) {
          setSelected(list[0].name);
        }
      } catch (exc) {
        setActionError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        setListLoading(false);
      }
    })();
    return stopPolling;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    void (async () => {
      setDetailLoading(true);
      try {
        setDetail(
          await request<ExperimentDetail>(
            `/api/evaluations/experiments/${encodeURIComponent(selected)}`,
          ),
        );
      } catch (exc) {
        setActionError(exc instanceof Error ? exc.message : String(exc));
      } finally {
        setDetailLoading(false);
      }
    })();
  }, [selected, request]);

  const triggerRun = useCallback(async () => {
    setActionError("");
    try {
      await request("/api/evaluations/run", { method: "POST", body: "{}" });
      const current = await request<EvalStatus>("/api/evaluations/status");
      setStatus(current);
      startPolling();
    } catch (exc) {
      setActionError(exc instanceof Error ? exc.message : String(exc));
    }
  }, [request, startPolling]);

  const running = status?.status === "running";

  return (
    <section className="flex min-h-full flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
        <button
          type="button"
          onClick={() => void triggerRun()}
          disabled={running}
          className="rounded-lg bg-brand-500 px-4 py-2 text-sm font-semibold text-white transition hover:bg-brand-600 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          运行评估
        </button>
        {running && (
          <span className="text-sm text-slate-600">
            {status?.completed ?? 0}/{status?.total ?? 0}
          </span>
        )}
        {status?.url && (
          <a
            href={status.url}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-brand-600 hover:underline"
          >
            {status.experiment_name}
          </a>
        )}
        {status?.status === "failed" && (
          <span className="text-sm text-red-600">{status.error}</span>
        )}
        {actionError && (
          <span className="text-sm text-red-600">{actionError}</span>
        )}
        <select
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
          className="ml-auto rounded-lg border border-slate-200 px-3 py-2 text-sm outline-none focus:border-brand-400"
        >
          {experiments.length === 0 && <option value="">无实验</option>}
          {experiments.map((item) => (
            <option key={item.name} value={item.name}>
              {item.name}（{item.run_count}）
            </option>
          ))}
        </select>
      </div>

      {listLoading && (
        <div className="rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-500 shadow-sm">
          正在加载实验列表…
        </div>
      )}

      {detailLoading && (
        <div className="rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-500 shadow-sm">
          正在加载实验明细…
        </div>
      )}

      {detail && !detailLoading && (
        <div className="grid grid-cols-1 gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm md:grid-cols-5">
          {Object.entries(detail.evaluator_scores).map(([key, stats]) => (
            <ScoreBar
              key={key}
              label={EVALUATOR_LABELS[key] ?? key}
              mean={stats.mean}
            />
          ))}
        </div>
      )}

      {detail && !detailLoading && (
        <div className="overflow-auto rounded-xl border border-slate-200 bg-white shadow-sm">
          <table className="w-full min-w-[1000px] text-left text-sm">
            <thead className="border-b border-slate-200 text-xs text-slate-500">
              <tr>
                <th className="px-3 py-2 font-medium">用例</th>
                <th className="px-3 py-2 font-medium">期望</th>
                <th className="px-3 py-2 font-medium">实际回复</th>
                <th className="px-3 py-2 font-medium">指标</th>
                <th className="px-3 py-2 font-medium">Trace</th>
              </tr>
            </thead>
            <tbody>
              {detail.cases.map((row) => (
                <tr
                  key={row.case_id}
                  className="border-b border-slate-100 align-top last:border-b-0"
                >
                  <td className="px-3 py-2">
                    <div className="font-medium text-slate-700">
                      {row.case_id}
                    </div>
                    <div className="text-xs text-slate-400">{row.tenant_id}</div>
                    <div className="mt-1 max-w-[220px] text-slate-600">
                      {row.message}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <ExpectedView expected={row.expected} />
                  </td>
                  <td className="px-3 py-2">
                    <div className="max-w-[300px] text-slate-600">
                      {row.final_reply ??
                        (row.confirmation_required ? "（等待确认）" : "—")}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex flex-col gap-1">
                      {row.feedbacks.map((feedback) => (
                        <div key={feedback.key} className="text-xs">
                          <span className="text-slate-500">
                            {EVALUATOR_LABELS[feedback.key] ?? feedback.key}：
                          </span>
                          <span
                            className={
                              feedback.score === null
                                ? "text-slate-400"
                                : feedback.score >= 0.8
                                  ? "text-emerald-600"
                                  : feedback.score >= 0.5
                                    ? "text-amber-600"
                                    : "text-red-600"
                            }
                          >
                            {feedback.score === null
                              ? "—"
                              : Math.round(feedback.score * 100)}
                          </span>
                          {feedback.comment && (
                            <span className="ml-1 text-slate-400">
                              {feedback.comment}
                            </span>
                          )}
                        </div>
                      ))}
                    </div>
                  </td>
                  <td className="px-3 py-2">
                    {row.trace_url && (
                      <a
                        href={row.trace_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-brand-600 hover:underline"
                      >
                        查看
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function ScoreBar({ label, mean }: { label: string; mean: number | null }) {
  const pct = mean === null ? null : Math.round(mean * 100);
  const color =
    pct === null
      ? "bg-slate-200"
      : pct >= 80
        ? "bg-emerald-500"
        : pct >= 50
          ? "bg-amber-500"
          : "bg-red-500";
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="text-slate-600">{label}</span>
        <span className="font-semibold text-slate-700">
          {pct === null ? "—" : pct}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full ${color}`}
          style={{ width: `${pct ?? 0}%` }}
        />
      </div>
    </div>
  );
}

function ExpectedView({ expected }: { expected: Record<string, unknown> }) {
  const lines: string[] = [];
  for (const key of [
    "intent",
    "hint",
    "tool",
    "order_id",
    "require_confirmation",
    "reference_answer",
    "must_contain",
    "must_not_contain",
  ]) {
    const value = expected[key];
    if (value === null || value === undefined) continue;
    if (Array.isArray(value)) {
      if (value.length === 0) continue;
      lines.push(`${key}: ${value.join(" / ")}`);
    } else {
      lines.push(`${key}: ${String(value)}`);
    }
  }
  return (
    <div className="flex max-w-[240px] flex-col gap-0.5 text-xs text-slate-600">
      {lines.map((line) => (
        <span key={line}>{line}</span>
      ))}
    </div>
  );
}

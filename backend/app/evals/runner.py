"""离线评估编排入口与实验结果查询。

- 手动触发：POST /api/evaluations/run
- 模块级 asyncio 锁：同时只允许一个实验
- 进度：aevaluate(blocking=False) 的异步结果逐行计数
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from langsmith import Client, aevaluate

from app.application.agent.facade import CustomerServiceAgentFacade
from app.config import Settings
from app.evals.dataset import load_cases, sync_dataset
from app.evals.evaluators import build_evaluators
from app.evals.target import make_async_target
from app.infrastructure.db.engine import InfrastructureBundle

EXPERIMENT_PREFIX = "ai-cs-eval"

_lock = asyncio.Lock()
_state: dict[str, Any] = {
    "status": "idle",  # idle|running|done|failed
    "total": 0,
    "completed": 0,
    "error": None,
    "experiment_name": None,
    "url": None,
}


class EvaluationRunningError(Exception):
    """已有一个评估实验在运行。"""


def get_status() -> dict[str, Any]:
    return dict(_state)


async def run_evaluation(
    *,
    settings: Settings,
    bundle: InfrastructureBundle,
    facade: CustomerServiceAgentFacade,
    tags: list[str] | None = None,
) -> None:
    """执行一轮离线评估（调用方负责以 asyncio task 方式运行）。"""
    if _lock.locked():
        raise EvaluationRunningError("已有评估在运行")

    async with _lock:
        _state.update(
            status="running",
            total=0,
            completed=0,
            error=None,
            experiment_name=None,
            url=None,
        )
        try:
            cases = load_cases(tags)
            _state["total"] = len(cases)
            if not cases:
                _state.update(
                    status="failed",
                    error=f"没有匹配 tags={tags} 的评估用例，请检查用例文件或标签",
                )
                return
            # 新实验即将产生：清掉查询缓存，避免列表/详情短 TTL 内仍是旧数据
            _experiments_cache.clear()
            _detail_cache.clear()

            client = Client()
            dataset_name = settings.evaluation.dataset_name
            sync_dataset(client, cases, dataset_name)
            dataset = client.read_dataset(dataset_name=dataset_name)
            wanted_ids = {c.case_id for c in cases}
            examples = [
                ex
                for ex in client.list_examples(dataset_id=dataset.id)
                if (ex.metadata or {}).get("case_id") in wanted_ids
            ]

            target = make_async_target(settings=settings, facade=facade, bundle=bundle)
            evaluators = build_evaluators(settings)
            results = await aevaluate(
                target,
                data=examples,
                evaluators=evaluators,
                experiment_prefix=EXPERIMENT_PREFIX,
                max_concurrency=settings.evaluation.concurrency,
                client=client,
                blocking=False,
            )
            _state.update(
                experiment_name=results.experiment_name,
                url=results.url,
            )
            async for _row in results:
                _state["completed"] += 1
            await results.wait()
            _state["status"] = "done"
        except Exception as exc:
            _state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            raise


# ============================================================================
# 实验查询
# ============================================================================


# 实验查询（LangSmith 外部 API，均带 TTL 缓存：列表 30s / 详情 15s）
_EXPERIMENTS_TTL = 30.0
_DETAIL_TTL = 15.0
_experiments_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_detail_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cache_get(cache: dict[str, Any], ttl: float, key: str) -> Any | None:
    """读取未过期缓存项，过期或不存在返回 None。"""
    entry = cache.get(key)
    if entry is None or time.monotonic() - entry[0] > ttl:
        return None
    return entry[1]


def _cache_set(cache: dict[str, Any], value: Any, key: str) -> None:
    cache[key] = (time.monotonic(), value)


def list_experiments(settings: Settings, limit: int = 20) -> list[dict[str, Any]]:
    """最近实验列表（绑定当前 golden 数据集，带 30s TTL 缓存）。

    LangSmith 是外部 API：实验列表页每次打开都实时拉全量会明显卡顿，
    已结束的实验基本不可变，缓存 30s 足够新鲜。
    """
    cached = _cache_get(_experiments_cache, _EXPERIMENTS_TTL, f"list:{limit}")
    if cached is not None:
        return cached
    client = Client()
    projects = list(
        client.list_projects(
            reference_dataset_name=settings.evaluation.dataset_name,
            limit=limit,
        )
    )
    items: list[dict[str, Any]] = [
        {
            "name": project.name,
            "start_time": project.start_time.isoformat() if project.start_time else None,
            "run_count": project.run_count,
            "url": _project_url(client, project),
        }
        for project in projects
    ]
    _cache_set(_experiments_cache, items, f"list:{limit}")
    return items


def get_experiment_detail(settings: Settings, name: str) -> dict[str, Any]:
    """实验详情：评估器均分 + 用例级明细（带 15s TTL 缓存）。

    15s TTL 兼顾刚结束实验的 judge 反馈异步物化窗口与页面重复打开的响应速度。
    """
    cached = _cache_get(_detail_cache, _DETAIL_TTL, name)
    if cached is not None:
        return cached
    client = Client()
    results = client.get_experiment_results(name=name)

    run_ids: list[Any] = []
    examples_with_runs: list[Any] = []
    for item in results["examples_with_runs"]:
        examples_with_runs.append(item)
        root = _root_run(item.runs)
        if root is not None:
            run_ids.append(root.id)

    feedback_by_run: dict[Any, list[dict[str, Any]]] = {rid: [] for rid in run_ids}
    for fb in client.list_feedback(run_ids=run_ids):
        feedback_by_run.setdefault(fb.run_id, []).append(
            {"key": fb.key, "score": fb.score, "comment": fb.comment}
        )

    case_rows: list[dict[str, Any]] = []
    for item in examples_with_runs:
        root = _root_run(item.runs)
        actual = (root.outputs if root is not None else None) or {}
        trace_url = client.get_run_url(run=root) if root is not None else None
        case_rows.append(
            {
                "case_id": (item.metadata or {}).get("case_id"),
                "tenant_id": item.inputs.get("tenant_id"),
                "message": item.inputs.get("message"),
                "expected": item.outputs or {},
                "final_reply": actual.get("final_reply"),
                "confirmation_required": actual.get("confirmation_required"),
                "feedbacks": feedback_by_run.get(root.id if root is not None else None, []),
                "trace_url": trace_url,
            }
        )

    all_feedback = [fb for rows in feedback_by_run.values() for fb in rows]
    detail = {
        "name": name,
        "evaluator_scores": _aggregate_feedback(all_feedback),
        "cases": case_rows,
    }
    _cache_set(_detail_cache, detail, name)
    return detail


def _aggregate_feedback(feedback_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """从逐用例反馈实时聚合 {key: {mean, count}}。

    不直接用 get_experiment_results 的 feedback_stats：实验刚结束、judge
    LLM 反馈还在异步写回时，该快照可能只覆盖部分用例且 avg=None，导致前端
    把已出分的指标误显示为「-」。这里与逐用例明细同源于 list_feedback。
    口径与 LangSmith 一致：count 含 N/A（score=None）反馈，mean 只对非 None
    分数求平均，全部为 N/A 时 mean=None。
    """
    counts: dict[str, int] = {}
    scored: dict[str, list[float]] = {}
    for fb in feedback_rows:
        key = fb.get("key")
        if not isinstance(key, str):
            continue
        counts[key] = counts.get(key, 0) + 1
        if fb.get("score") is not None:
            scored.setdefault(key, []).append(float(fb["score"]))
    return {
        key: {
            "mean": (sum(scored[key]) / len(scored[key])) if key in scored else None,
            "count": counts[key],
        }
        for key in counts
    }


def _root_run(runs: Any) -> Any | None:
    if not runs:
        return None
    for run in runs:
        if run.parent_run_id is None:
            return run
    return runs[0]


def _project_url(client: Client, project: Any) -> str | None:
    """实验的 Web URL：优先用 SDK 自带的 project.url（零额外请求），
    SDK 未水合 _host_url 时回退查一次根 run。"""
    url = getattr(project, "url", None)
    if url:
        return url
    runs = list(client.list_runs(project_id=project.id, is_root=True, limit=1))
    if not runs:
        return None
    return client.get_run_url(run=runs[0])

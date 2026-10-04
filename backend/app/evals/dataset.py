"""本地 JSON 用例 ↔ LangSmith Dataset 同步。

按 metadata.case_id 做 diff：
  - 本地有、远端无 → create_example
  - 本地有、远端有但 inputs/outputs 变化 → 删除后重建（langsmith 无 update_example）
  - 远端有、本地无（本次选中范围外的 case 保留；用例文件中已删除的才删除）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langsmith import Client

from app.evals.schema import EvalCase

_CASES_DIR = Path(__file__).parent / "cases"
_CASE_FILES = ("intent.json", "knowledge.json", "task.json")


def load_cases(tags: list[str] | None = None) -> list[EvalCase]:
    """从 cases/*.json 加载全部用例；给 tags 时按 tag 过滤（命中任一即选中）。"""
    cases: list[EvalCase] = []
    for fname in _CASE_FILES:
        raw = json.loads((_CASES_DIR / fname).read_text(encoding="utf-8"))
        cases.extend(EvalCase.model_validate(item) for item in raw)
    if tags:
        cases = [c for c in cases if any(t in c.tags for t in tags)]
    return cases


def load_all_case_ids() -> set[str]:
    """加载用例文件中的全部 case_id（用于识别远端的过期示例）。"""
    return {c.case_id for c in load_cases()}


def example_inputs(case: EvalCase) -> dict[str, Any]:
    return {"tenant_id": case.tenant_id, "message": case.message}


def example_outputs(case: EvalCase) -> dict[str, Any]:
    return case.expected.model_dump(exclude_none=True)


def sync_dataset(client: Client, cases: list[EvalCase], dataset_name: str) -> None:
    """把 cases 同步到指定 LangSmith 数据集（不存在则创建）。"""
    if not client.has_dataset(dataset_name=dataset_name):
        client.create_dataset(dataset_name, description="智能客服离线评估 golden set")

    existing: dict[str, Any] = {
        _case_id_from_example(ex): ex
        for ex in client.list_examples(dataset_name=dataset_name)
    }
    wanted: dict[str, EvalCase] = {c.case_id: c for c in cases}
    all_local_ids = load_all_case_ids()

    # 删除：用例文件中已不存在的过期示例
    stale = [cid for cid, ex in existing.items() if cid not in all_local_ids]
    for cid in stale:
        client.delete_example(existing[cid].id)

    for case_id, case in wanted.items():
        old = existing.get(case_id)
        if old is None:
            client.create_example(
                inputs=example_inputs(case),
                outputs=example_outputs(case),
                metadata={"case_id": case.case_id, "tags": case.tags},
                dataset_name=dataset_name,
            )
            continue
        if (
            old.inputs == example_inputs(case)
            and _normalize_outputs(old.outputs) == example_outputs(case)
        ):
            continue
        client.delete_example(old.id)
        client.create_example(
            inputs=example_inputs(case),
            outputs=example_outputs(case),
            metadata={"case_id": case.case_id, "tags": case.tags},
            dataset_name=dataset_name,
        )


def _normalize_outputs(outputs: Any) -> dict[str, Any]:
    return outputs if isinstance(outputs, dict) else {}


def _case_id_from_example(example: Any) -> str:
    metadata = example.metadata or {}
    return str(metadata.get("case_id"))

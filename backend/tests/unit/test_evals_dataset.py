"""dataset 同步与用例加载单测。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.evals.dataset import (
    example_inputs,
    example_outputs,
    load_all_case_ids,
    load_cases,
    sync_dataset,
)


def test_load_cases_all_and_tag_filter() -> None:
    all_cases = load_cases()
    assert len(all_cases) == 35

    intent_cases = load_cases(["intent"])
    assert len(intent_cases) == 12
    assert all("intent" in c.tags for c in intent_cases)

    knowledge_cases = load_cases(["knowledge"])
    assert len(knowledge_cases) == 13

    task_cases = load_cases(["task"])
    assert len(task_cases) == 10

    assert len(load_all_case_ids()) == 35


def test_sync_dataset_creates_when_missing() -> None:
    client = MagicMock()
    client.has_dataset.return_value = False
    client.list_examples.return_value = iter([])
    cases = load_cases(["intent"])

    sync_dataset(client, cases, "ds")

    client.create_dataset.assert_called_once()
    assert client.create_example.call_count == 12
    # metadata 必须带 case_id
    for call in client.create_example.call_args_list:
        assert call.kwargs["metadata"]["case_id"].startswith("intent-")


def test_sync_dataset_keeps_unchanged_examples() -> None:
    cases = load_cases(["intent"])
    remote = [
        SimpleNamespace(
            id=f"ex-{c.case_id}",
            inputs=example_inputs(c),
            outputs=example_outputs(c),
            metadata={"case_id": c.case_id},
        )
        for c in cases
    ]
    client = MagicMock()
    client.has_dataset.return_value = True
    client.list_examples.return_value = iter(remote)

    sync_dataset(client, cases, "ds")

    client.create_example.assert_not_called()
    client.delete_example.assert_not_called()


def test_sync_dataset_replaces_changed_example() -> None:
    cases = load_cases(["intent"])
    changed = cases[0]
    remote = [
        SimpleNamespace(
            id="ex-changed",
            inputs=example_inputs(changed),
            outputs={"intent": "handoff"},
            metadata={"case_id": changed.case_id},
        )
    ]
    client = MagicMock()
    client.has_dataset.return_value = True
    client.list_examples.return_value = iter(remote)

    sync_dataset(client, cases, "ds")

    client.delete_example.assert_called_once_with("ex-changed")
    created_cases = [
        call.kwargs for call in client.create_example.call_args_list
    ]
    assert any(
        c["metadata"]["case_id"] == changed.case_id for c in created_cases
    )


def test_sync_dataset_deletes_stale_but_keeps_out_of_scope() -> None:
    cases = load_cases(["intent"])
    stale = SimpleNamespace(
        id="ex-stale",
        inputs={},
        outputs={},
        metadata={"case_id": "gone-case"},
    )
    # 本次只跑 intent，但 knowledge 用例在本地文件里仍存在 → 不应删除
    out_of_scope = SimpleNamespace(
        id="ex-knowledge",
        inputs={},
        outputs={},
        metadata={"case_id": "knowledge-001"},
    )
    unchanged_remote = [
        SimpleNamespace(
            id=f"ex-{c.case_id}",
            inputs=example_inputs(c),
            outputs=example_outputs(c),
            metadata={"case_id": c.case_id},
        )
        for c in cases
    ]
    client = MagicMock()
    client.has_dataset.return_value = True
    client.list_examples.return_value = iter(
        [stale, out_of_scope, *unchanged_remote]
    )

    sync_dataset(client, cases, "ds")

    client.delete_example.assert_called_once_with("ex-stale")

"""评估器单测：确定性评估器对错分支 + LLM judge 正常/错误路径。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.evals.evaluators import (
    _faithfulness,
    _knowledge_correctness,
    answer_contains,
    intent_match,
    task_tool_match,
)
from app.evals.judge import JudgeOutputError


def _run(outputs: dict[str, Any]) -> Any:
    return SimpleNamespace(outputs=outputs)


def _example(outputs: dict[str, Any]) -> Any:
    return SimpleNamespace(outputs=outputs)


# ---------- intent_match ----------


@pytest.mark.asyncio
async def test_intent_match_pass() -> None:
    result = await intent_match(
        _run({"intent_candidate": "task", "intent_hint": "refund"}),
        _example({"intent": "task", "hint": "refund"}),
    )
    assert result["score"] == 1


@pytest.mark.asyncio
async def test_intent_match_fail_intent_and_hint() -> None:
    result = await intent_match(
        _run({"intent_candidate": "simple_qa", "intent_hint": None}),
        _example({"intent": "task", "hint": "cancel"}),
    )
    assert result["score"] == 0
    assert "intent" in result["comment"]
    assert "hint" in result["comment"]


@pytest.mark.asyncio
async def test_intent_match_na() -> None:
    result = await intent_match(_run({}), _example({}))
    assert result["score"] is None


# ---------- task_tool_match ----------


@pytest.mark.asyncio
async def test_task_tool_match_read_pass() -> None:
    outputs = {
        "tool_executions": [
            {
                "tool_name": "order_query",
                "success": True,
                "data": {
                    "order_no": "A-ORD-202509-001",
                    "status": "delivered",
                },
            }
        ]
    }
    expected = {
        "tool": "order_query",
        "tool_result_contains": {
            "order_no": "A-ORD-202509-001",
            "status": "delivered",
        },
    }
    result = await task_tool_match(_run(outputs), _example(expected))
    assert result["score"] == 1


@pytest.mark.asyncio
async def test_task_tool_match_read_missing_tool() -> None:
    result = await task_tool_match(
        _run({"tool_executions": []}),
        _example({"tool": "order_query"}),
    )
    assert result["score"] == 0


@pytest.mark.asyncio
async def test_task_tool_match_write_pass() -> None:
    outputs = {
        "confirmation_required": True,
        "pending_action": {
            "tool": "refund_request",
            "args": {"order_id": "aaaa0101-0000-0000-0000-000000000014"},
        },
    }
    expected = {
        "tool": "refund_request",
        "require_confirmation": True,
        "order_id": "aaaa0101-0000-0000-0000-000000000014",
    }
    result = await task_tool_match(_run(outputs), _example(expected))
    assert result["score"] == 1


@pytest.mark.asyncio
async def test_task_tool_match_write_without_confirmation() -> None:
    result = await task_tool_match(
        _run({"confirmation_required": False, "pending_action": None}),
        _example(
            {
                "tool": "cancel_order",
                "require_confirmation": True,
                "order_id": "x",
            }
        ),
    )
    assert result["score"] == 0
    assert "confirmation_required" in result["comment"]


# ---------- answer_contains ----------


@pytest.mark.asyncio
async def test_answer_contains_pass_and_fail() -> None:
    expected = {
        "must_contain": ["7天"],
        "must_not_contain": ["30天"],
    }
    ok = await answer_contains(
        _run({"final_reply": "支持7天无理由"}),
        _example(expected),
    )
    assert ok["score"] == 1

    bad = await answer_contains(
        _run({"final_reply": "支持30天无理由"}),
        _example(expected),
    )
    assert bad["score"] == 0


@pytest.mark.asyncio
async def test_answer_contains_na() -> None:
    result = await answer_contains(_run({}), _example({}))
    assert result["score"] is None


# ---------- LLM judges ----------


@pytest.mark.asyncio
async def test_knowledge_correctness_scored() -> None:
    judge = SimpleNamespace(
        score_json=AsyncMock(return_value={"score": 0.5, "reason": "缺要点"})
    )
    evaluate = _knowledge_correctness(judge)
    result = await evaluate(
        _run({"final_reply": "可以退"}),
        _example({"reference_answer": "7天无理由"}),
    )
    assert result["score"] == 0.5
    assert result["comment"] == "缺要点"


@pytest.mark.asyncio
async def test_knowledge_correctness_error_then_none() -> None:
    judge = SimpleNamespace(
        score_json=AsyncMock(side_effect=JudgeOutputError("bad"))
    )
    evaluate = _knowledge_correctness(judge)
    result = await evaluate(
        _run({"final_reply": "x"}),
        _example({"reference_answer": "ref"}),
    )
    assert result["score"] is None


@pytest.mark.asyncio
async def test_faithfulness_with_hits() -> None:
    judge = SimpleNamespace(
        score_json=AsyncMock(return_value={"score": 1, "reason": "OK"})
    )
    evaluate = _faithfulness(judge)
    result = await evaluate(
        _run(
            {
                "final_reply": "结论",
                "rag_hits": [{"content": "片段"}],
            }
        ),
        _example({}),
    )
    assert result["score"] == 1


@pytest.mark.asyncio
async def test_faithfulness_without_hits_na() -> None:
    judge = SimpleNamespace()
    evaluate = _faithfulness(judge)
    result = await evaluate(_run({"rag_hits": []}), _example({}))
    assert result["score"] is None

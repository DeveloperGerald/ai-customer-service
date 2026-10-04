"""离线评估器。

确定性：
  - intent_match     大类 + hint
  - task_tool_match  选对工具 + 关键结果/参数；写操作必先暂停
  - answer_contains  must_contain / must_not_contain
LLM-as-judge：
  - knowledge_correctness  对照参考答案 0~1
  - faithfulness           结论必须由 rag_hits 支撑
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.config import Settings
from app.evals.judge import JudgeClient, JudgeOutputError

Evaluator = Any


def build_evaluators(settings: Settings) -> Sequence[Evaluator]:
    """构造全部评估器（judge 共享一个客户端）。"""
    judge = JudgeClient(settings)
    return [
        intent_match,
        task_tool_match,
        answer_contains,
        _knowledge_correctness(judge),
        _faithfulness(judge),
    ]


# ============================================================================
# 确定性评估器
# ============================================================================


async def intent_match(run: Any, example: Any) -> dict[str, Any]:
    expected = example.outputs or {}
    actual = run.outputs or {}
    want_intent = expected.get("intent")
    if want_intent is None:
        return {"key": "intent_match", "score": None, "comment": "N/A"}

    failures: list[str] = []
    got_intent = actual.get("intent_candidate")
    if got_intent != want_intent:
        failures.append(f"intent: 期望 {want_intent}，实际 {got_intent}")

    want_hint = expected.get("hint")
    if want_hint is not None:
        got_hint = actual.get("intent_hint")
        if got_hint != want_hint:
            failures.append(f"hint: 期望 {want_hint}，实际 {got_hint}")

    if failures:
        return {"key": "intent_match", "score": 0, "comment": "；".join(failures)}
    return {"key": "intent_match", "score": 1, "comment": "OK"}


async def task_tool_match(run: Any, example: Any) -> dict[str, Any]:
    expected = example.outputs or {}
    actual = run.outputs or {}
    want_tool = expected.get("tool")
    if want_tool is None:
        return {"key": "task_tool_match", "score": None, "comment": "N/A"}

    if expected.get("require_confirmation"):
        failures = _check_pending_write(expected, actual, want_tool)
    else:
        failures = _check_read_tool(expected, actual, want_tool)

    if failures:
        return {"key": "task_tool_match", "score": 0, "comment": "；".join(failures)}
    return {"key": "task_tool_match", "score": 1, "comment": "OK"}


async def answer_contains(run: Any, example: Any) -> dict[str, Any]:
    expected = example.outputs or {}
    actual = run.outputs or {}
    must_contain = expected.get("must_contain") or []
    must_not_contain = expected.get("must_not_contain") or []
    if not must_contain and not must_not_contain:
        return {"key": "answer_contains", "score": None, "comment": "N/A"}

    reply = str(actual.get("final_reply") or "")
    failures = [f"缺少「{s}」" for s in must_contain if s not in reply]
    failures += [f"出现禁用内容「{s}」" for s in must_not_contain if s in reply]

    if failures:
        return {"key": "answer_contains", "score": 0, "comment": "；".join(failures)}
    return {"key": "answer_contains", "score": 1, "comment": "OK"}


def _check_read_tool(expected: dict[str, Any], actual: dict[str, Any], want_tool: str) -> list[str]:
    executions = actual.get("tool_executions") or []
    matched = [
        e for e in executions
        if isinstance(e, dict) and e.get("tool_name") == want_tool and e.get("success")
    ]
    if not matched:
        return [f"未成功执行工具 {want_tool}"]

    failures: list[str] = []
    for key, value in (expected.get("tool_result_contains") or {}).items():
        if not any(_nested_contains(e.get("data"), key, value) for e in matched):
            failures.append(f"工具结果缺少 {key}={value}")
    return failures


def _check_pending_write(
    expected: dict[str, Any], actual: dict[str, Any], want_tool: str
) -> list[str]:
    failures: list[str] = []
    if actual.get("confirmation_required") is not True:
        failures.append("未产生 confirmation_required 暂停")

    pending = actual.get("pending_action") or {}
    if pending.get("tool") != want_tool:
        failures.append(f"暂停工具: 期望 {want_tool}，实际 {pending.get('tool')}")

    want_order_id = expected.get("order_id")
    if want_order_id is not None:
        args = pending.get("args") or {}
        got_order_id = args.get("order_id") if isinstance(args, dict) else None
        if got_order_id != want_order_id:
            failures.append(f"order_id: 期望 {want_order_id}，实际 {got_order_id}")
    return failures


def _nested_contains(obj: Any, key: str, value: Any) -> bool:
    """递归在 data（dict/list 嵌套）中查找 key == value。"""
    if isinstance(obj, dict):
        if key in obj and str(obj[key]) == str(value):
            return True
        return any(_nested_contains(v, key, value) for v in obj.values())
    if isinstance(obj, list):
        return any(_nested_contains(v, key, value) for v in obj)
    return False


# ============================================================================
# LLM-as-judge 评估器
# ============================================================================


def _knowledge_correctness(judge: JudgeClient):
    system = (
        "你是严格的客服回答质量评估专家。根据【参考答案】评估【实际回答】："
        "关键要点是否完整、是否与参考答案矛盾、是否答非所问。"
        "如果实际回复已经完整回答了用户的问题，且与参考答案无矛盾，则可以接受缺少参考答案中的部分信息。"
        "如果实际回复增加了额外信息，但增加的信息并非明确的内容或具体的政策，则可以适当接受。"
        "评分：1=所有关键要点正确且无矛盾；0.1～0.9=部分正确或表述不完整；0=错误、矛盾或答非所问。"
        '只输出 JSON：{"score": 0到1之间的数, "reason": "简短中文理由"}。'
    )

    async def evaluate(run: Any, example: Any) -> dict[str, Any]:
        expected = example.outputs or {}
        reference = expected.get("reference_answer")
        if reference is None:
            return {"key": "knowledge_correctness", "score": None, "comment": "N/A"}
        actual = run.outputs or {}
        user = (
            f"【参考答案】{reference}\n"
            f"【实际回答】{actual.get('final_reply') or ''}"
        )
        try:
            result = await judge.score_json(system=system, user=user)
        except JudgeOutputError as exc:
            return {"key": "knowledge_correctness", "score": None, "comment": str(exc)}
        return {
            "key": "knowledge_correctness",
            "score": _clamp_score(result.get("score")),
            "comment": str(result.get("reason") or ""),
        }

    return evaluate


def _faithfulness(judge: JudgeClient):
    system = (
        "你是检索问答的忠实性评估专家。判断【回答】中的事实性结论是否都能由【检索片段】"
        "支撑，是否存在片段之外的编造。"
        "评分：1=全部结论有据可依；0.5=大部分有据但有轻微外推；0=包含无依据的关键结论或编造。"
        '只输出 JSON：{"score": 0到1之间的数, "reason": "简短中文理由"}。'
    )

    async def evaluate(run: Any, example: Any) -> dict[str, Any]:
        actual = run.outputs or {}
        hits = actual.get("rag_hits") or []
        if not hits:
            return {"key": "faithfulness", "score": None, "comment": "N/A"}
        context = "\n".join(
            f"[{i + 1}] {h.get('content', '')}" for i, h in enumerate(hits) if isinstance(h, dict)
        )
        user = f"【检索片段】\n{context}\n【回答】{actual.get('final_reply') or ''}"
        try:
            result = await judge.score_json(system=system, user=user)
        except JudgeOutputError as exc:
            return {"key": "faithfulness", "score": None, "comment": str(exc)}
        return {
            "key": "faithfulness",
            "score": _clamp_score(result.get("score")),
            "comment": str(result.get("reason") or ""),
        }

    return evaluate


def _clamp_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, score))

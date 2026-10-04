"""离线评估用例 schema。

用例文件为 cases/ 下的 JSON（stdlib json，不引入 yaml）：
  - intent.json    意图分类
  - knowledge.json 知识问答
  - task.json      任务类（工具选择 / 关键参数 / HITL 暂停）
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExpectedOutcome(BaseModel):
    """单条用例的期望结果（字段全部可选，按用例类型取所需）。"""

    # 意图：4 大类 simple_qa/handoff/knowledge_qa/task
    intent: str | None = None
    # 意图子类 refund/exchange/repair/cancel/order_status/product
    hint: str | None = None
    # 期望被选择的工具名（order_query/product_list/product_query/
    # refund_request/exchange_request/repair_request/cancel_order）
    tool: str | None = None
    # 只读工具：期望在工具返回 data 中（含嵌套）出现的键值对
    tool_result_contains: dict[str, Any] = Field(default_factory=dict)
    # 写工具：期望写入的订单 UUID
    order_id: str | None = None
    # 写操作是否必须先产生 confirmation_required 暂停
    require_confirmation: bool = False
    # 知识问答参考答案（LLM judge 对照）
    reference_answer: str | None = None
    # 回复必须包含 / 不得包含的字符串
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)


class EvalCase(BaseModel):
    """一条离线评估用例。"""

    case_id: str
    tags: list[str]
    tenant_id: str
    message: str
    expected: ExpectedOutcome

"""Agent 层 Schema（T7）：
- State：LangGraph State（TypedDict，兼容 LangGraph StateGraph）。
- PolicyDecision：结构化退款/换货/维修资格判定结果（从政策数值+订单属性计算，面试展示点）。
- AgentRunResult：Facade 对外的最终返回（含最终回复、是否转人工、工单编号、中间决策 debug 字段，便于前端展示决策原因）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import add_messages
from pydantic import BaseModel, Field

from app.application.schemas.conversation import ConversationMessageRead
from app.domain.repositories.identity import Actor

# =========================================================================
# 1. LangGraph State（TypedDict；LangGraph>=0.2 支持 TypedDict/Pydantic/dataclass 均可）
# =========================================================================


INTENT_CANDIDATES: tuple[str, ...] = (
    "simple_qa",
    "handoff",
    "knowledge_qa",
    "task",
)
ActionKindCandidates: tuple[str, ...] = (
    "refund",
    "exchange",
    "repair",
    "cancel",
    "handoff",
    "knowledge_qa",
    "simple_qa",
    "unknown",
)


class AgentState(TypedDict, total=False):
    """LangGraph 运行时状态（所有字段可逐步填充；total=False 允许缺失）。

    填充顺序（4 分类拓扑）：
      actor/tenant_id/thread_id/user_message
        → intent_classify → intent_candidate(4 大类)/order_ref_candidate/intent_hint
        → intent_router 路由：
            simple_qa    → compliance_check
            handoff      → handoff_node → compliance_check
            knowledge_qa → policy_lookup →(hit) compliance_check /(miss) rag_retrieve → compliance_check
            task         → task_react_subgraph(create_react_agent，写工具内置 interrupt)
                           → compliance_check
        → compliance_check（规则合规 + LLM 包装）→ final_reply

    task 子图通过 messages 通道与外层交互；compliance 取 draft_reply 或 messages[-1].content。
    """

    actor: Actor
    tenant_id: str
    thread_id: str
    user_message: str

    # task 子图（create_agent）原生消息通道；非 task 分支不动
    messages: Annotated[list, add_messages]

    intent_candidate: Literal[
        "simple_qa",
        "handoff",
        "knowledge_qa",
        "task",
    ]
    order_ref_candidate: dict[str, Any] | None
    intent_hint: Literal["refund", "exchange", "repair", "cancel", "order_status", "product"] | None

    # knowledge_qa 政策优先：policy_lookup 命中标志 + 结构化政策答案
    policy_lookup_hit: bool | None
    policy_answer: dict[str, Any] | None

    rag_hits: list[dict[str, Any]]
    order_detail_json: dict[str, Any] | None

    policy_decision: dict[str, Any] | None

    action_kind: Literal[
        "refund", "exchange", "repair", "cancel", "handoff", "knowledge_qa", "simple_qa", "unknown"
    ]
    action_result_json: dict[str, Any] | None
    tool_executions: list[dict[str, Any]]
    _agent_final_answer_draft: str | None

    # 各分支产出草稿 + 结构化上下文（供 compliance_check 选 prompt 约束）
    draft_reply: str | None
    draft_context: dict[str, Any] | None

    escalated: bool
    escalated_ticket_no: str | None
    escalation_reason: str | None

    final_reply: str
    appended_messages: list[ConversationMessageRead]

    node_errors: dict[str, dict[str, Any]]
    _stream_chunks: list[str]


# =========================================================================
# 2. PolicyDecision（结构化判定结果，面试讲解点）
# =========================================================================


@dataclass
class PolicyDecision:
    """基于订单属性 + 租户政策数值字段计算出的资格判定。

    字段设计与 TENANT_POLICIES 对齐：
      - return_days / return_policy_type / restocking_fee_pct_non_quality / warranty_days_quality
    """

    can_refund: bool
    can_exchange: bool
    can_repair: bool
    requires_quality_evidence: bool
    restocking_fee_pct: int
    refund_amount_cents: int | None
    reason_code: Literal[
        "eligible_no_reason",
        "eligible_quality",
        "eligible_warranty_repair",
        "policy_not_allowed",
        "custom_product_excluded",
        "window_expired",
        "human_required_missing_info",
        "policy_info_no_reason",
        "policy_info_quality_only",
        "policy_info_mixed",
    ]
    reason_human_readable: str
    debug: dict[str, Any] = field(default_factory=dict)

    def to_state_json(self) -> dict[str, Any]:
        """序列化为 AgentState.policy_decision 存储的 dict。"""
        return {
            "can_refund": self.can_refund,
            "can_exchange": self.can_exchange,
            "can_repair": self.can_repair,
            "requires_quality_evidence": self.requires_quality_evidence,
            "restocking_fee_pct": self.restocking_fee_pct,
            "refund_amount_cents": self.refund_amount_cents,
            "reason_code": self.reason_code,
            "reason_human_readable": self.reason_human_readable,
            "debug": self.debug,
        }


# =========================================================================
# 3. Facade 返回（HTTP/SSE 层最终消费）
# =========================================================================


@dataclass
class AgentRunResult:
    """CustomerServiceAgentFacade 对外返回值。

    - final_reply: 用户最终看到的自然语言回复
    - escalated / escalated_ticket_no: D3 转人工结果（立即执行，不需二次确认）
    - decision_debug: 可选；前端「查看决策依据」面板展示用（面试讲解关键）
    """

    final_reply: str
    escalated: bool = False
    escalated_ticket_no: str | None = None
    escalation_reason: str | None = None
    decision_debug: dict[str, Any] | None = None


# =========================================================================
# 4. T10 HTTP 请求/响应（Pydantic v2）
# =========================================================================


class AgentRunRequest(BaseModel):
    """用户一条新消息：POST /api/conversations/{thread_id}/run 请求体。"""

    text: str = Field(..., min_length=1, max_length=2000, description="用户本次输入的自然语言消息")
    idempotency_key: str | None = Field(
        default=None,
        max_length=128,
        description="写操作幂等键（前端 uuid 生成，重复调用保证副作用只执行一次）",
    )


class AgentConfirmRequest(BaseModel):
    """HITL 写操作确认：POST /api/conversations/{thread_id}/actions/confirm 请求体。

    用户在页面上点击「确认/取消」后，前端用此端点 resume 暂停的图。
    - decision=true  → 执行真实写操作（订单状态流转）
    - decision=false → 取消本次操作，ReAct 拿到结果继续生成回复
    超时（checkpointer TTL=600s 过期）→ 服务端返回 409，前端展示「操作已超时」。
    """

    decision: bool = Field(..., description="true=确认执行写操作；false=取消")
    reason: str | None = Field(
        default=None,
        max_length=500,
        description="用户取消原因（可选，仅 decision=false 时填）",
    )


class AgentRunResponse(BaseModel):
    """同步 Agent 调用的最终响应。"""

    final_reply: str
    escalated: bool = False
    escalated_ticket_no: str | None = None
    escalation_reason: str | None = None
    decision_debug: dict[str, Any] | None = Field(
        default=None,
        description="前端决策依据 debug 面板；本地/演示环境非空，生产可通过配置关闭",
    )

"""意图分类器：仅保留 LLMIntentClassifier 一种实现。

设计原则：
    1. 抽象 IntentClassifierProtocol：aclassify(text)->(intent4, order_ref, hint) 单一方法
       intent4 ∈ {simple_qa, handoff, knowledge_qa, task}（图路由的 4 大类）；
       hint ∈ {refund, exchange, repair, cancel, order_status, product, None}（子意图提示，供 task 分支预填）。
    2. 唯一实现 LLMIntentClassifier：调用 ChatModel 做意图识别 + 订单号抽取 + 子意图提示。
       LLM 调用失败/超时/返回非法值时，兜底返回 ("simple_qa", order_ref, None)，不再回退到关键词法。
    3. 订单号提取（3 种正则模式）保留在本模块，避免「intent 识别」与「订单号抽取」分裂。
"""

from __future__ import annotations

import json as _json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.application.schemas.agent import INTENT_CANDIDATES
from app.core.logging import get_logger

log = get_logger("agent.intent.classifiers")

_ORDER_NO_PATTERNS = (
    re.compile(r"(?:订单号|订单编号|单号)[:：]?\s*([A-Za-z0-9\-_]{6,40})"),
    re.compile(r"\b([A-Z]{1,3}-ORD-[0-9]{4,8}-[0-9]{3,6})\b", re.IGNORECASE),
    re.compile(r"#?([A-Z]{1,4}[0-9]{6,20})\b"),
)

_INTENT_HINT_VALUES = {"refund", "exchange", "repair", "cancel", "order_status", "product"}


def _extract_order_ref(text: str) -> dict[str, Any] | None:
    """从消息文本里提取订单号（保持 3 正则不变）。"""
    for pat in _ORDER_NO_PATTERNS:
        m = pat.search(text)
        if m:
            return {"order_no": m.group(1)}
    return None


# =========================================================================
# 1. 协议（统一签名）
# =========================================================================


class IntentClassifierProtocol(ABC):
    """意图识别协议：只暴露一个异步方法。

    返回 (intent5, order_ref, hint)：直接对齐 LangGraph state 的
    intent_candidate / order_ref_candidate / intent_hint，零转换成本。
    """

    @abstractmethod
    async def aclassify(
        self,
        text: str,
        **kwargs: Any,
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        """对用户单轮消息执行意图识别 + 订单号抽取 + 子意图提示。

        Keyword Args:
            history (list[dict] | None): 最近若干轮对话历史，每项
                ``{"role": "user"|"assistant", "content": str}``，供分类器结合上下文判断意图。
                None/空表示无历史可参考。典型用途：用户先说「取消订单」、本轮只发订单号时，
                避免被误判为 order_status。
            tenant_id (str): 租户 ID（仅供日志/上下文，不参与分类）。
            thread_id (str): 会话线程 ID（仅供日志/上下文）。

        Returns:
            (intent4, order_ref, hint)
            intent4 必须是 INTENT_CANDIDATES（4 大类 simple_qa/handoff/knowledge_qa/task）的成员之一；
            order_ref 为空时返回 None，非空形如 ``{"order_no": "A-ORD-..."}``；
            hint 为子意图 {refund, exchange, repair, cancel, order_status, product} 或 None。
        """


# =========================================================================
# 2. LLM 实现：调用聊天模型做意图分类
# =========================================================================


_INTENT_SYSTEM_PROMPT = """你是电商售后场景的意图分类器。严格按以下候选意图输出，禁止输出候选外的值。

路由意图（intent，必须选其一，共 4 类）：
  - simple_qa: 简单问答/寒暄/礼貌/与店铺无关的闲聊/极短无意义文本（如「你好」「在吗」「好的谢谢」「拜拜」「你是机器人吗」）。不需要工具、不需要检索知识库即可回应。
  - handoff: 用户明确要求人工客服、真人、投诉、找老板，或提到「图片/截图/照片/拍照/img」（我们不做图片识别，直接转人工）
  - knowledge_qa: 知识问答——政策/规则咨询、常见问题（不申请售后动作，只是问规则/政策/流程，如「退货政策是什么」「保修期多久」「定制款能退吗」「怎么退款」）
  - task: 执行任务——需要调用工具的读写操作：申请售后（退款/退货/换货/维修）、取消订单、查询订单状态/物流、查询商品信息/商品目录。判断信号：含订单号，或第一人称申请（我要/我想/申请/帮我…退/换/修/取消），或查在售商品/商品列表/商品材质规格库存价格

子意图（hint，仅当 intent=task 或 knowledge_qa 时填写，否则 null）：
  - refund: 退款/退货
  - exchange: 换货
  - repair: 维修
  - cancel: 取消订单
  - order_status: 查订单状态/物流（有订单号但无售后关键词时）
  - product: 商品信息咨询/商品目录查询（材质/规格/库存/价格/款式/有哪些在售商品/商品列表等，非售后动作）

对话历史判断（必须严格遵守）：
  - 若提供了对话历史（user/assistant 历史 messages），必须结合历史判断用户真实意图，
    不能只看当前这一条消息。
  - 典型场景：历史中用户说「取消订单/退款/换货/维修」，本轮只发一个订单号或极短确认语——
    intent 仍为 task，hint 沿用历史中的售后子意图（cancel/refund/exchange/repair），而不是 order_status。
  - 仅当历史中也没有售后意图、本轮确实只是查订单状态/物流时，才用 hint=order_status。
  - order_no 仅从当前用户消息提取，不从历史中提取。

订单号（order_no）正则匹配优先级：
  1. 「订单号/订单编号/单号: XXX」后紧跟的 6-40 位字母数字-下划线串
  2. 形如 AA-ORD-0001-001 的结构化单号（字母前缀-ORD-数字-数字）
  3. #A123456 或 A1234567890 的字母+数字串（1-4 字母 + 6-20 数字）

输出严格 JSON（不要 Markdown 代码块，不要解释）：
{"intent": "4类候选值之一", "hint": "子意图或null", "order_no": "提取到的单号字符串或 null"}
"""


@dataclass
class LLMIntentClassifier(IntentClassifierProtocol):
    """LLM 意图分类器：调用 ChatModel 做意图识别 + 订单号抽取 + 子意图提示。

    优势：
      - 能理解「我想把这条退掉，订单 A-ORD-0001 帮我处理下」这种非显式关键词
      - 能区分「你们退款政策是什么？」(knowledge_qa) 与「订单 A 我要退款」(task+refund)
      - 图片/附件类表达更鲁棒（「我发了张照片你看看」也能识别转人工）

    Args:
        chat_model: BaseChatModel 实例（langchain_openai.ChatOpenAI 等）
        timeout_seconds: 单次调用超时（超过则兜底 simple_qa）
    """

    chat_model: Any
    timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        from langchain_core.language_models import BaseChatModel

        if not isinstance(self.chat_model, BaseChatModel):
            raise TypeError(
                "LLMIntentClassifier.chat_model 必须是 langchain_core BaseChatModel 实例"
            )

    async def aclassify(
        self, text: str, **kwargs: Any
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        stripped = (text or "").strip()
        if not stripped:
            return "simple_qa", None, None

        try:
            import asyncio

            raw = await asyncio.wait_for(
                self._call_llm(stripped, **kwargs),
                timeout=self.timeout_seconds,
            )
            intent, hint, order_no = self._parse_output(raw)
            if intent not in INTENT_CANDIDATES:
                raise ValueError(f"LLM 返回非法 intent: {intent}")
            if hint is not None and hint not in _INTENT_HINT_VALUES:
                hint = None
            order_ref = {"order_no": order_no} if order_no else _extract_order_ref(stripped)
            return intent, order_ref, hint
        except Exception as exc:
            log.exception(
                "llm_intent.fallback_simple_qa",
                error_type=type(exc).__name__,
            )
            return "simple_qa", _extract_order_ref(stripped), None

    # ---- internal ----
    async def _call_llm(self, text: str, **kwargs: Any) -> str:
        _ = kwargs.get("tenant_id"), kwargs.get("thread_id")  # 仅供日志语义，不参与分类
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        messages: list[Any] = [SystemMessage(content=_INTENT_SYSTEM_PROMPT)]
        for item in kwargs.get("history") or []:
            role = str(item.get("role") or "user")
            content = str(item.get("content") or "")
            messages.append(
                AIMessage(content=content) if role == "assistant" else HumanMessage(content=content)
            )
        messages.append(HumanMessage(content=text))

        result = await self.chat_model.ainvoke(messages)
        content = result.content
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content)

    @staticmethod
    def _parse_output(raw: str) -> tuple[str, str | None, str | None]:
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`")
            if s.lower().startswith("json"):
                s = s[4:].strip()
        try:
            obj = _json.loads(s)
            intent = str(obj.get("intent", "")).strip().lower() or "simple_qa"
            hint = obj.get("hint")
            if hint is not None:
                hint = str(hint).strip().lower() or None
                if hint not in _INTENT_HINT_VALUES:
                    hint = None
            order_no = obj.get("order_no")
            if order_no is not None:
                order_no = str(order_no).strip() or None
            return intent, hint, order_no
        except Exception:
            match = re.search(r'"intent"\s*:\s*"([a-z_]+)"', s, re.IGNORECASE)
            intent = match.group(1).lower() if match else "simple_qa"
            match_h = re.search(r'"hint"\s*:\s*"([a-z_]+)"', s, re.IGNORECASE)
            hint = match_h.group(1).lower() if match_h else None
            if hint is not None and hint not in _INTENT_HINT_VALUES:
                hint = None
            match2 = re.search(r'"order_no"\s*:\s*"([^"]+)"', s)
            order_no = match2.group(1) if match2 else None
            return intent, hint, order_no

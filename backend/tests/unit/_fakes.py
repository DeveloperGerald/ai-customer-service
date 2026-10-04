"""测试用 Fake 模型（仅用于单元测试，不进生产代码）。

LangChain 1.x 原生重构后的 Fake 三件套：
- FakeChatModel(BaseChatModel)：单类双模式
    * 未 bind_tools（模板模式）：按 SystemMessage 分发——
      意图分类 JSON（「意图分类器」）/ 政策判断 JSON（「政策判断助手」）/
      compliance 模板回复（解析【参考上下文】序列化文本）
    * bind_tools 后（agent 模式）：create_agent 决策链
      order_query → policy_check → *_request → final
- FakeEmbeddings(Embeddings)：文本哈希确定性伪向量（PGVectorStore 直接消费）
- FakeRetriever：固定 RAG 命中
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import math
import random
import re
from typing import Any

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel

from app.infrastructure.llm.providers import BaseRetriever


class FakeRetriever(BaseRetriever):
    """测试用 Fake Retriever：返回固定的 RAG 命中。"""

    def __init__(self, hits: list[dict] | None = None) -> None:
        self._hits = hits or [
            {
                "chunk_id": "fake-1",
                "tenant_id": "tenant_a",
                "content": "手串品牌支持7天无理由退货，定制款不支持非质量退货。",
                "similarity": 0.85,
                "metadata": {"source": "policy_manual", "title": "售后政策"},
            },
            {
                "chunk_id": "fake-2",
                "tenant_id": "tenant_a",
                "content": "问：你们支持几天无理由退货？答：支持7天无理由退货。",
                "similarity": 0.80,
                "metadata": {"source": "faq", "title": "无理由退货"},
            },
        ]

    async def retrieve(
        self,
        *,
        tenant_id: str,
        query: str,
        top_k: int,
        similarity_threshold: float,
    ) -> list[dict]:
        return [h for h in self._hits if (h.get("similarity") or 0) >= similarity_threshold][:top_k]


class FakeEmbeddings(Embeddings):
    """按文本哈希生成确定性伪向量（同步 Embeddings 协议，dim 参数化）。"""

    def __init__(self, dim: int = 256) -> None:
        self.dim = int(dim)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)

    def _embed_one(self, text: str) -> list[float]:
        seed_bytes = hashlib.sha256(text.encode("utf-8")).digest()
        rng = random.Random(seed_bytes)
        raw = [rng.uniform(-1.0, 1.0) for _ in range(self.dim)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [round(v / norm, 7) for v in raw]


# ---- 意图分类关键词规则（与 LLM 意图识别的 4 分类对齐）----
_INTENT_ORDER_PATTERNS = (
    re.compile(r"(?:订单号|订单编号|单号)[:：]?\s*([A-Za-z0-9\-_]{6,40})"),
    re.compile(r"\b([A-Z]{1,3}-ORD-[0-9]{4,8}-[0-9]{3,6})\b", re.IGNORECASE),
    re.compile(r"#?([A-Z]{1,4}[0-9]{6,20})\b"),
)
_INTENT_HANDOFF_KW = ("人工", "真人", "客服", "投诉", "转人工", "找客服", "投诉你们", "找老板")
_INTENT_IMAGE_KW = ("图片", "截图", "照片", "拍照", "图", "img", "image")
_INTENT_REPAIR_KW = ("修", "维修", "坏了", "裂了", "断了", "脱落", "开线", "珠裂", "绳断", "配件掉")
_INTENT_REFUND_KW = ("退", "退款", "退货", "退钱", "不想要", "拒收")
_INTENT_EXCHANGE_KW = ("换", "换货", "换款", "换尺寸", "换颜色", "调换")
_INTENT_PRODUCT_KW = (
    "材质", "规格", "尺寸", "大小", "库存", "有货", "价格", "多少钱", "款式",
    "成色", "重量", "长度", "直径", "珠子", "颗", "什么料", "什么材质",
    "商品", "在售", "列表", "卖什么", "产品", "目录", "上架", "新品",
)
_INTENT_SMALLTALK_KW = (
    "你好", "您好", "哈喽", "hi", "hello", "在吗", "在不", "有人吗", "谢谢", "感谢",
    "辛苦了", "再见", "拜拜", "麻烦了", "打扰了",
)
_INTENT_SMALLTALK_PUNCT = re.compile(r"^[\s!！。,，?？~\-的呢了嘛啊吧呀哇耶]+$")
_INTENT_FIRST_PERSON = re.compile(
    r"(我要|我想|申请|帮我|办理|给我处理|麻烦处理).*(退|换|修|退款|退货|换货|调换|维修)"
)
_INTENT_FAQ_MARKERS = (
    "退", "换", "修", "退款", "退货", "换货", "维修", "政策", "规则", "保修", "质量",
    "几天", "多久", "可以", "能退", "能换", "能修", "七天", "7天", "无理由", "多少",
)


def _fake_classify_intent(text: str) -> tuple[str, str | None, str | None]:
    """关键词规则意图分类，返回新 4 分类（simple_qa/handoff/knowledge_qa/task）。

    与 LLMIntentClassifier 的 4 类候选对齐，供 FakeChatModel 在单测中
    模拟 LLM 意图识别的 JSON 输出。
    """
    stripped = text.strip()
    order_no: str | None = None
    for pat in _INTENT_ORDER_PATTERNS:
        m = pat.search(stripped)
        if m:
            order_no = m.group(1)
            break
    lower = stripped.lower()
    for kw in _INTENT_HANDOFF_KW:
        if kw in lower:
            return ("handoff", None, order_no)
    if any(kw in lower for kw in _INTENT_IMAGE_KW):
        return ("handoff", None, order_no)
    hint: str | None = None
    if any(kw in stripped for kw in _INTENT_REPAIR_KW):
        hint = "repair"
    if any(kw in stripped for kw in _INTENT_REFUND_KW):
        hint = "refund"
    if any(kw in stripped for kw in _INTENT_EXCHANGE_KW):
        hint = "exchange"
    if order_no:
        if hint is None:
            hint = "order_status"
        return ("task", hint, order_no)
    if hint is not None and _INTENT_FIRST_PERSON.search(stripped):
        return ("task", hint, None)
    if hint is not None and len(stripped) > 15:
        return ("task", hint, None)
    if _INTENT_SMALLTALK_PUNCT.match(stripped):
        return ("simple_qa", None, None)
    if any(kw in lower for kw in _INTENT_SMALLTALK_KW) and len(stripped) <= 20:
        return ("simple_qa", None, None)
    if len(stripped) <= 4:
        return ("simple_qa", None, None)
    if hint is None:
        for kw in _INTENT_PRODUCT_KW:
            if kw in stripped:
                return ("task", "product", None)
    if any(kw in stripped for kw in _INTENT_FAQ_MARKERS):
        return ("knowledge_qa", hint, None)
    # 兜底：无意义/随机文本归 simple_qa（与 LLMIntentClassifier 的 fallback 一致）
    return ("simple_qa", None, None)


def _split_human_readable(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "。！？!?\n":
            if buf:
                out.append(buf)
                buf = ""
            out.append(ch)
        else:
            buf += ch
        i += 1
    if buf:
        out.append(buf)
    return [c for c in out if c]


def _extract_tool_data(raw: Any) -> dict[str, Any]:
    """从 ToolMessage.content 中提取工具返回的 data 字段。

    ToolMessage.content 通常是 JSON 字符串：{"tool": "...", "success": true, "data": {...}}，
    也可能直接是 dict。
    """
    if isinstance(raw, dict):
        return raw.get("data", raw) if "data" in raw else raw
    if isinstance(raw, str):
        try:
            obj = _json.loads(raw)
            if isinstance(obj, dict) and "data" in obj:
                data = obj["data"]
                return data if isinstance(data, dict) else {"value": data}
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _extract_order_no(text: str) -> str:
    """从文本中提取订单号（优先匹配结构化单号）。"""
    m = re.search(r"\b([A-Z]{1,3}-ORD-[0-9]{4,8}-[0-9]{3,6})\b", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(r"(?:订单号|订单编号|单号)[:：]?\s*([A-Za-z0-9\-_]{6,40})", text)
    if m2:
        return m2.group(1)
    m3 = re.search(r"(\d{6,})", text)
    if m3:
        return m3.group(1)
    m4 = re.search(r"#?([A-Z]{1,4}[0-9]{6,20})\b", text)
    if m4:
        return m4.group(1)
    return ""


# ----------------------------------------------------------------------------
# 【参考上下文】序列化文本解析（compliance_check 节点经 _format_extra_context 拼装）
# ----------------------------------------------------------------------------


def _parse_context_sections(ctx_str: str) -> dict[str, list[str]]:
    """把 _format_extra_context 输出按 【标题】 分节。

    返回 {标题（去掉「（...）」说明后缀）: [行, ...]}。
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []
    for line in ctx_str.split("\n"):
        if line.startswith("【") and "】" in line:
            if current is not None:
                sections.setdefault(current, []).extend(buf)
            full_title = line[1 : line.index("】")]
            current = full_title.split("（")[0].strip()
            buf = []
            rest = line[line.index("】") + 1 :]
            if rest.strip():
                buf.append(rest)
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections.setdefault(current, []).extend(buf)
    return sections


def _parse_kv_block(lines: list[str]) -> dict[str, str]:
    """解析「  - key: value」键值行块。"""
    out: dict[str, str] = {}
    for ln in lines:
        s = ln.strip()
        if s.startswith("- ") and ":" in s:
            k, _, v = s[2:].partition(":")
            out[k.strip()] = v.strip()
    return out


class FakeChatModel(BaseChatModel):  # type: ignore[misc]
    """测试用假聊天模型（单类双模式）。

    未 bind_tools → 模板模式（意图分类 JSON / 政策判断 JSON / compliance 模板）；
    bind_tools 后 → agent 模式（根据已执行工具序列决策下一步）。
    """

    bound_tools: Any = None  # bind_tools 记录；None = 模板模式

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "bound_tools", None)

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def bind_tools(self, tools: Any, **kwargs: Any) -> FakeChatModel:
        # create_agent 会调用 bind_tools；记录标志切换到 agent 决策模式。
        object.__setattr__(self, "bound_tools", tools)
        return self

    def _generate(self, messages: Any, stop: Any = None, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("sync _generate not used in tests")

    # ---- 消息形状提取 ----

    @staticmethod
    def _system_content(messages: list[Any]) -> str:
        from langchain_core.messages import SystemMessage

        for m in messages:
            if isinstance(m, SystemMessage):
                c = m.content
                return c if isinstance(c, str) else str(c)
        return ""

    @staticmethod
    def _last_human_content(messages: list[Any]) -> str:
        from langchain_core.messages import HumanMessage

        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                c = m.content
                return c if isinstance(c, str) else str(c)
        return ""

    # ---- 模板模式 ----

    def _template_reply(self, messages: list[Any]) -> str:
        system = self._system_content(messages)
        user = self._last_human_content(messages)
        if "意图分类器" in system:
            intent4, hint, order_no = _fake_classify_intent(user)
            return _json.dumps(
                {"intent": intent4, "hint": hint, "order_no": order_no},
                ensure_ascii=False,
            )
        if "政策判断助手" in system:
            return self._policy_judge_reply(user)
        return self._compliance_reply(system, user)

    def _policy_judge_reply(self, judge_prompt: str) -> str:
        """policy_lookup 判断：只对「用户问题：」段做关键词匹配（字段说明不参与）。"""
        m = re.search(r"用户问题：(.+?)(?:\n\n【硬约束】|\Z)", judge_prompt, re.DOTALL)
        q = m.group(1) if m else judge_prompt
        if any(kw in q for kw in ("7天", "7 天", "无理由", "几天", "退货", "能退", "可以退")):
            return _json.dumps(
                {"can_answer": True, "answer": "本品牌支持 7 天无理由退货。"},
                ensure_ascii=False,
            )
        return _json.dumps({"can_answer": False}, ensure_ascii=False)

    def _compliance_reply(self, system: str, user: str) -> str:
        """compliance_check 模板：解析【参考上下文】分节，按旧 provider 模板产出。"""
        brand = ""
        m = re.search(r"当前服务品牌：(\S+?)[（\n]", system or "")
        if m:
            brand = f"【{m.group(1)}】"
        user_text, _, ctx_str = user.partition("\n\n---\n【参考上下文】")
        sections = _parse_context_sections(ctx_str)

        draft = "\n".join(sections.get("草稿回复", [])).strip()
        policy = _parse_kv_block(sections.get("政策判定结果", []))
        action = _parse_kv_block(sections.get("工具/工单执行结果", []))
        rag_lines = [
            re.sub(r"^\s*\[\d+\](\((sim=[\d.]+)\))?", "", ln).strip()
            for ln in sections.get("知识库检索片段", [])
        ]
        rag_hits = [ln for ln in rag_lines if ln]

        ticket_no = action.get("ticket_no", "")

        # 1. 转人工（与旧 provider escalated 分支一致）
        if "已转人工" in sections:
            esc_lines = sections["已转人工"]
            esc_kv: dict[str, str] = {}
            for part in ",".join(x.strip() for x in esc_lines if x.strip()).split(","):
                k, _, v = part.partition("=")
                esc_kv[k.strip()] = v.strip()
            reason = esc_kv.get("原因") or "您已转人工。"
            esc_ticket = esc_kv.get("工单号") or "(未知)"
            return (
                f"{brand}{reason}\n\n人工工单号：{esc_ticket}\n客服将在 5-10 分钟内接入，请稍候。"
            )

        # 2. 写工具已执行（工单模板，与旧 refund/exchange/repair 分支一致）
        if ticket_no:
            reason = policy.get("reason_human_readable", "")
            amount = action.get("refund_amount_cents")
            if "换" in reason:
                return (
                    f"{brand}{reason or '换货已受理。'}\n\n"
                    f"🔁 换货工单号：{ticket_no}\n"
                    f"客服将在 24 小时内联系您确认新款式/尺寸。"
                )
            if "修" in reason:
                return (
                    f"{brand}{reason or '维修已受理。'}\n\n"
                    f"🔧 维修工单号：{ticket_no}\n"
                    f"将在 2 个工作日内通过短信给您发送寄回地址。"
                )
            amount_str = f"{int(amount) / 100:.2f} 元" if amount else "按实际支付金额核算"
            return (
                f"{brand}{reason or '退款已受理。'}\n\n"
                f"✅ 退款工单号：{ticket_no}\n"
                f"预计退款金额：{amount_str}"
            )

        # 3. 有草稿 → 直接润色透传（品牌前缀）
        if draft:
            return f"{brand}{draft}"

        # 4. 商品列表（订单/商品详情 JSON 携带 items）
        detail_json = "\n".join(sections.get("订单/商品详情", [])).strip()
        if detail_json:
            try:
                detail = _json.loads(detail_json)
            except Exception:
                detail = None
            if isinstance(detail, dict) and isinstance(detail.get("items"), list):
                items = detail["items"]
                if items:
                    names = "、".join(
                        str(it.get("product_name") or it.get("name") or it) for it in items[:8]
                    )
                    more = "等" if len(items) > 8 else ""
                    return (
                        f"{brand}目前在售商品有：{names}{more}。"
                        f"如需了解某款的材质/规格/库存/价格，可直接告诉我商品名。"
                    )

        # 5. 政策判定原因（排除「未查询到订单」兜底文案）
        reason = policy.get("reason_human_readable", "")
        if reason and reason != "未查询到对应订单，请提供订单号或转人工协助。":
            lines = [f"{brand}{reason}"]
            lines.extend(f"  · {h}" for h in rag_hits[:3])
            return "\n".join(lines)

        # 6. RAG 整理模板
        if rag_hits:
            lines = [f"{brand}根据您的问题，整理如下信息："]
            lines.extend(f"  · {h}" for h in rag_hits[:3])
            return "\n".join(lines)

        # 7. simple_qa：polite → 不客气模板；否则欢迎模板
        user_text_stripped = (user_text or "").strip()
        polite = any(k in user_text_stripped for k in ("谢谢", "感谢", "辛苦", "麻烦", "再见", "拜拜"))
        if polite:
            return (
                f"{brand}不客气，很高兴为您服务～\n\n"
                f"后续如有售后需求（退款/换货/维修），随时告诉我订单号或描述问题。"
                f"也可以直接回复「人工」接入真人客服。"
            )
        return (
            f"{brand}请问需要办理什么业务？\n"
            f"  · 退款 / 换货 / 维修 → 请提供订单号 + 问题描述\n"
            f"  · 政策咨询 → 直接提问即可\n"
            f"  · 人工客服 → 随时回复「人工」"
        )

    # ---- agent 模式（create_agent 决策链）----

    def _decide_next(
        self, user_query: str, executed: list[str], tool_results: dict[str, Any]
    ) -> dict[str, Any]:
        """根据用户 query + 已执行工具，决定下一步动作。"""
        q = user_query.lower()

        # 工具还没调过 → 决定第一个工具
        if not executed:
            if any(kw in user_query for kw in _INTENT_REFUND_KW):
                return {"type": "tool_call", "tool": "order_query", "args": {"order_no": _extract_order_no(user_query)}}
            if any(kw in user_query for kw in _INTENT_EXCHANGE_KW):
                return {"type": "tool_call", "tool": "order_query", "args": {"order_no": _extract_order_no(user_query)}}
            if any(kw in user_query for kw in _INTENT_REPAIR_KW):
                return {"type": "tool_call", "tool": "order_query", "args": {"order_no": _extract_order_no(user_query)}}
            if any(kw in user_query for kw in _INTENT_PRODUCT_KW):
                return {"type": "tool_call", "tool": "product_list", "args": {}}
            if any(kw in q for kw in ("人工", "客服", "转人工")):
                return {"type": "final", "content": "好的，已为您转接人工客服，请稍候。"}
            if any(kw in q for kw in ("你好", "在吗", "您好", "hi", "hello")):
                return {"type": "final", "content": "您好！我是禅饰坊客服，有什么可以帮您？"}
            # FAQ / 其他 → 直接给最终回复
            return {"type": "final", "content": self._faq_reply(user_query)}

        # 已调过 order_query → 下一步 policy_check
        if "order_query" in executed and "policy_check" not in executed:
            action = "refund"
            if any(kw in user_query for kw in _INTENT_EXCHANGE_KW):
                action = "exchange"
            elif any(kw in user_query for kw in _INTENT_REPAIR_KW):
                action = "repair"
            order_detail = _extract_tool_data(tool_results.get("order_query", ""))
            return {
                "type": "tool_call",
                "tool": "policy_check",
                "args": {"order_detail": order_detail, "intent_hint": action},
            }

        # 已调过 action_request → 最终回复（必须在 policy_check 检查之前）
        action_tool = next((t for t in executed if t.endswith("_request")), None)
        if action_tool:
            action = action_tool.replace("_request", "")
            result_data = _extract_tool_data(tool_results.get(action_tool, ""))
            ticket_no = str(result_data.get("ticket_no", ""))
            if action == "refund":
                content = f"【禅饰坊】退款申请已受理。\n\n💰 退款工单号：{ticket_no}\n预计 1-3 个工作日原路退回。"
            elif action == "exchange":
                content = f"【禅饰坊】换货申请已受理。\n\n🔁 换货工单号：{ticket_no}\n客服将在 24 小时内联系您确认新款式/尺寸。"
            else:
                content = f"【禅饰坊】维修申请已受理。\n\n🔧 维修工单号：{ticket_no}\n将在 2 个工作日内通过短信给您发送寄回地址。"
            return {"type": "final", "content": content}

        # 已调过 policy_check → 根据结果决定
        if "policy_check" in executed:
            policy_data = _extract_tool_data(tool_results.get("policy_check", ""))
            can_refund = bool(policy_data.get("can_refund"))
            action = "refund"
            if any(kw in user_query for kw in _INTENT_EXCHANGE_KW):
                action = "exchange"
            elif any(kw in user_query for kw in _INTENT_REPAIR_KW):
                action = "repair"

            if action == "refund" and not can_refund:
                return {"type": "final", "content": "抱歉，当前订单不符合退款条件，建议您联系人工客服协助处理。"}
            if action == "exchange" and not can_refund:
                return {"type": "final", "content": "抱歉，当前订单不符合换货条件，建议您联系人工客服协助处理。"}

            order_detail = _extract_tool_data(tool_results.get("order_query", ""))
            order_id = str(order_detail.get("order_id") or order_detail.get("id") or "")
            if action == "refund":
                req_args = {"order_id": order_id, "reason": "7_day_return"}
            elif action == "exchange":
                req_args = {"order_id": order_id, "reason": "size"}
            else:  # repair
                req_args = {"order_id": order_id, "issue_desc": "商品损坏，需要维修"}
            return {
                "type": "tool_call",
                "tool": f"{action}_request",
                "args": req_args,
            }

        # product_list 已调过 → 最终回复
        if "product_list" in executed:
            return {
                "type": "final",
                "content": "【禅饰坊】目前在售多款手串，如需了解具体款式的材质、规格、库存或价格，请直接告诉我商品名。",
            }

        # 兜底
        return {"type": "final", "content": "【禅饰坊】已收到您的请求，正在为您处理。"}

    def _faq_reply(self, user_query: str) -> str:
        """FAQ / 兜底回复（复用 FakeRetriever 的命中内容）。"""
        _ = user_query
        return (
            "【禅饰坊】根据您的问题，整理如下信息：\n"
            "  · 手串品牌支持7天无理由退货，定制款不支持非质量退货。\n"
            "  · 问：你们支持几天无理由退货？答：支持7天无理由退货。"
        )

    # ---- BaseChatModel 入口 ----

    async def _agenerate(self, messages: list[Any], stop: Any = None, **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        if self.bound_tools is None:
            content = self._template_reply(messages)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

        # agent 模式：从消息历史提取用户 query + 已执行工具
        from langchain_core.messages import HumanMessage, ToolMessage

        user_query = ""
        for m in messages:
            if isinstance(m, HumanMessage):
                user_query = str(m.content)
                break
        executed_tools: list[str] = []
        tool_results: dict[str, Any] = {}
        for m in messages:
            if isinstance(m, ToolMessage):
                executed_tools.append(m.name or "")
                tool_results[m.name or ""] = m.content

        next_action = self._decide_next(user_query, executed_tools, tool_results)
        if next_action["type"] == "tool_call":
            ai_msg = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": next_action["tool"],
                        "args": next_action["args"],
                        "id": f"call_{next_action['tool']}_{len(executed_tools)}",
                    }
                ],
            )
        else:
            ai_msg = AIMessage(content=next_action["content"])
        return ChatResult(generations=[ChatGeneration(message=ai_msg)])

    async def _astream(self, messages: list[Any], stop: Any = None, **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        result = await self._agenerate(messages, stop=stop, **kwargs)
        msg = result.generations[0].message
        if getattr(msg, "tool_calls", None):
            # agent 模式工具调用：yield 带 tool_call_chunks 的 chunk
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": tc["name"],
                            "args": str(tc.get("args", {})),
                            "id": tc.get("id"),
                            "index": i,
                        }
                        for i, tc in enumerate(msg.tool_calls)
                    ],
                )
            )
            return
        content = str(msg.content or "")
        full = "".join(_split_human_readable(content))
        rng = random.Random(hashlib.sha256(full.encode("utf-8")).digest()[:8])
        for part in _split_human_readable(content):
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))
            await asyncio.sleep(rng.uniform(0.008, 0.028))

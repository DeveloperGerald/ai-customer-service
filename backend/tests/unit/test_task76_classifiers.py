"""T7.6 意图分类器单元测试（Protocol + LLMIntentClassifier + Fake Provider + Facade 注入）。

覆盖策略：
    - 纯分类器（不启动 LangGraph）：LLMIntentClassifier + FakeChatModelProvider 关键词规则
      覆盖 4 大类意图 × 订单号抽取 × product hint
    - Facade 级联（真实走图）：注入 StaticClassifier 强制 simple_qa；注入 SpyClassifier 验证 aclassify 被调用
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.application.agent.facade import CustomerServiceAgentFacade
from app.infrastructure.llm.classifiers import (
    IntentClassifierProtocol,
    LLMIntentClassifier,
    _extract_order_ref,
)
from tests.unit._fakes import FakeChatModel
from tests.unit.test_task7_graph import (
    OWNER_A1,
    TENANT_A,
    _make_facade,
    _make_session_with_order,
)


def _llm_classifier() -> LLMIntentClassifier:
    """用 FakeChatModel 构造 LLMIntentClassifier（离线可复现）。"""
    return LLMIntentClassifier(chat_model=FakeChatModel())


# ========================================================================
# TR76-1 LLMIntentClassifier（Fake Provider）：4 大类意图各 1 case
# ========================================================================


@pytest.mark.asyncio
async def test_tr76_kw_classifier_refund_and_exchange() -> None:
    cls = _llm_classifier()
    intent, _, hint = await cls.aclassify("A-ORD-202509-001 我要退货退款，不喜欢")
    assert intent == "task"
    assert hint == "refund"

    intent2, _, hint2 = await cls.aclassify("想换尺寸，这条太大啦")
    assert intent2 == "knowledge_qa"
    assert hint2 == "exchange"


@pytest.mark.asyncio
async def test_tr76_kw_classifier_repair_and_handoff() -> None:
    cls = _llm_classifier()
    intent, _, hint = await cls.aclassify("手串的绳断了，珠裂了一颗，帮维修")
    assert intent == "task"
    assert hint == "repair"

    # 关键词触发 D3
    intent2, _, _ = await cls.aclassify("转人工，我要投诉")
    assert intent2 == "handoff"
    # 图片触发 D3（AGENTS 约束，虽非关键词，但作为图片提示直接转人工）
    intent3, _, _ = await cls.aclassify("有截图为证！")
    assert intent3 == "handoff"


@pytest.mark.asyncio
async def test_tr76_kw_classifier_order_status_and_simple_qa_and_knowledge() -> None:
    cls = _llm_classifier()
    # 含订单号 → task + hint=order_status
    intent, order_ref, hint = await cls.aclassify("帮我看看 A-ORD-202509-001 订单的售后进度")
    assert intent == "task"
    assert hint == "order_status"
    assert order_ref is not None and "order_no" in order_ref

    # 打招呼 + 极短文本 → simple_qa
    intent2, _, _ = await cls.aclassify("你好呀～")
    assert intent2 == "simple_qa"
    intent3, _, _ = await cls.aclassify("！？？")
    assert intent3 == "simple_qa"
    intent4, _, _ = await cls.aclassify("ok")
    assert intent4 == "simple_qa"

    # 不命中任何关键词 + 不是短文本 → knowledge_qa 兜底
    intent5, _, hint5 = await cls.aclassify(
        "请问一般沉香手串平时如何保养？存放湿度温度大概多少？能长期佩戴洗澡吗？"
    )
    assert intent5 == "knowledge_qa"
    assert hint5 is None


@pytest.mark.asyncio
async def test_tr76_product_hint_routes_to_task() -> None:
    """商品信息咨询（材质/规格/库存/价格等，含目录/列表查询）→ task + hint=product。

    task 分支进入 ReAct 子图，可调用 product_list / product_query 工具查实时商品数据。
    """
    cls = _llm_classifier()
    intent, _, hint = await cls.aclassify("这款手串是什么材质的？规格多大？")
    assert intent == "task"
    assert hint == "product"

    intent2, _, hint2 = await cls.aclassify("库存还有吗？多少钱一条？")
    assert intent2 == "task"
    assert hint2 == "product"

    # 商品目录/列表查询
    intent3, _, hint3 = await cls.aclassify("有哪些在售商品")
    assert intent3 == "task"
    assert hint3 == "product"

    intent4, _, hint4 = await cls.aclassify("商品列表给我看看")
    assert intent4 == "task"
    assert hint4 == "product"


# ========================================================================
# TR76-2 订单号抽取 3 正则全部覆盖
# ========================================================================


@pytest.mark.parametrize(
    ("text", "expected_no"),
    [
        ("订单号：A-ORD-202509-001 快处理", "A-ORD-202509-001"),
        ("单号ORD2025090001X处理", "ORD2025090001X"),
        ("#B88820251001", "B88820251001"),
        ("这里什么都没有", None),
    ],
)
def test_tr76_order_ref_extract_three_patterns(text: str, expected_no: str | None) -> None:
    result = _extract_order_ref(text)
    if expected_no is None:
        assert result is None
    else:
        assert result is not None
        assert result["order_no"] == expected_no


# ========================================================================
# TR76-3 StaticClassifier：恒返回指定 intent（替代已移除的 NullIntentClassifier）
# ========================================================================


class _StaticClassifier:
    """鸭子类型 IntentClassifierProtocol：恒返回固定 intent4，用于隔离 LLM 调用。"""

    def __init__(self, intent4: str = "knowledge_qa") -> None:
        self._intent4 = intent4

    async def aclassify(self, text: str, **_: Any):
        return (self._intent4, _extract_order_ref(text), None)


@pytest.mark.asyncio
async def test_tr76_null_classifier_preset_intent() -> None:
    # 默认 knowledge_qa
    c1 = _StaticClassifier()
    intent1, _, _ = await c1.aclassify("我要退款")  # 即使命中 refund 关键词，仍返回预设
    assert intent1 == "knowledge_qa"

    # 预设 simple_qa
    c2 = _StaticClassifier(intent4="simple_qa")
    intent2, ref, _ = await c2.aclassify("  A-ORD-XXX-001  ")  # 订单号仍能被抽出
    assert intent2 == "simple_qa"
    assert ref is None or "order_no" in ref


# ========================================================================
# TR76-4 Facade 可插拔注入：用 StaticClassifier(simple_qa) 覆盖默认 LLM 分类器
# ========================================================================


@pytest.mark.asyncio
async def test_tr76_facade_inject_null_forces_simple_qa_even_on_refund_keyword() -> None:
    """Facade 注入 StaticClassifier(simple_qa) 后，即使说「我要退款」也被强置 simple_qa。

    这是「可插拔」能力的演示测试：生产只需替换 classifier 参数即可切到自定义实现。
    """
    tid = f"{TENANT_A}:tr76-inject-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(
        owner=OWNER_A1, thread_id=tid, order_result=None
    )
    facade = _make_facade()  # 默认 LLMIntentClassifier（Fake Provider）
    # 覆盖为 Static 强置 simple_qa（不重写 facade，改传新实例）
    facade2 = CustomerServiceAgentFacade(
        retriever=facade.retriever,
        classifier=_StaticClassifier(intent4="simple_qa"),
        chat_model=facade.chat_model,
    )
    # 即使发一条明显是 refund 的消息，intent 也会被 StaticClassifier 置 simple_qa
    out = await facade2.invoke(
        actor=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid,
        user_message="我要全额退款！不要了！（A-ORD-202509-001）",
        session=sess,
    )
    assert out.escalated is False
    assert out.decision_debug is not None
    # simple_qa → _build_debug 兼容层回填为旧标签 smalltalk
    assert out.decision_debug["intent_candidate"] == "smalltalk"
    # simple_qa 分支不写 action_kind（state 为 None → _build_debug 过滤掉）
    assert out.decision_debug.get("action_kind") is None
    # simple_qa 分支不走 order_query → order_detail_json 必然空
    assert (out.decision_debug.get("order_detail_json") or None) is None


# ========================================================================
# TR76-5 Facade 注入 Spy：验证意图分类只被调用一次（节点幂等性 demo）
# ========================================================================


class _SpyClassifier(IntentClassifierProtocol):
    """测试用分类器：把调用次数和参数写入实例状态，便于断言。"""

    def __init__(self, wrapped: IntentClassifierProtocol) -> None:
        self.wrapped = wrapped
        self.call_count = 0
        self.last_kwargs: dict[str, Any] = {}

    async def aclassify(
        self, text: str, **kwargs: Any
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        self.call_count += 1
        self.last_kwargs = dict(kwargs)
        return await self.wrapped.aclassify(text, **kwargs)


@pytest.mark.asyncio
async def test_tr76_classifier_called_exactly_once_and_has_tenant_id_context() -> None:
    tid = f"{TENANT_A}:tr76-spy-{uuid4().hex[:6]}"
    sess, _ = _make_session_with_order(
        owner=OWNER_A1, thread_id=tid, order_result=None
    )
    facade = _make_facade()
    spy = _SpyClassifier(_llm_classifier())
    facade2 = CustomerServiceAgentFacade(
        retriever=facade.retriever,
        classifier=spy,
        chat_model=facade.chat_model,
    )
    await facade2.invoke(
        actor=OWNER_A1,
        tenant_id=TENANT_A,
        thread_id=tid,
        user_message="hi！",
        session=sess,
    )
    # 关键断言：一次 invoke 过程中 classifier 只会被调用 1 次（避免 LLM 烧钱）
    assert spy.call_count == 1
    # tenant_id 被透传（未来 LLM 版做 few-shot 会用到）
    assert spy.last_kwargs.get("tenant_id") == TENANT_A
    assert spy.last_kwargs.get("thread_id") == tid


# ========================================================================
# TR76-6 自定义 Protocol 实例也能注入（Duck Typing，无需继承 ABC）
# ========================================================================


@pytest.mark.asyncio
async def test_tr76_ducktype_protocol_no_abc_inherit_still_works() -> None:
    """证明 Protocol 设计是 duck-type：即使类不写继承 (ABC) 也能作为分类器注入。"""

    class _DuckClassifier:
        async def aclassify(
            self, text: str, **_: Any
        ) -> tuple[str, dict[str, Any] | None, str | None]:
            return "handoff", None, None

    tid = f"{TENANT_A}:tr76-duck-{uuid4().hex[:6]}"
    sess, added = _make_session_with_order(
        owner=OWNER_A1, thread_id=tid, order_result=None
    )
    facade = _make_facade()
    facade2 = CustomerServiceAgentFacade(
        retriever=facade.retriever,
        classifier=_DuckClassifier(),  # type: ignore[arg-type] - 故意不继承证明 duck-type
        chat_model=facade.chat_model,
    )
    out = await facade2.invoke(
        actor=OWNER_A1,  # 必须和线程 owner 一致；consumer 不能写别人的 thread
        tenant_id=TENANT_A,
        thread_id=tid,
        user_message="whatever text（鸭子类型，意图被强置 handoff）",
        session=sess,
    )
    assert out.escalated is True
    assert (out.escalated_ticket_no or "").startswith("HO-")
    _ = added


# ========================================================================
# TR76-7 LLMIntentClassifier（Fake Provider）12 条 4 大类样本（T8.7 验收用）
# ========================================================================


@pytest.mark.parametrize(
    ("text", "expected_intent"),
    [
        ("转人工，我要投诉，客服太慢了", "handoff"),
        ("有张截图你帮我看看（[图片]）", "handoff"),
        ("A-ORD-202509-001 我要退货退款，收到就不喜欢", "task"),
        ("我想换货，这个款式不太合适，尺寸太大", "task"),
        ("你好呀～很高兴认识你", "simple_qa"),
        ("OK", "simple_qa"),
        ("7天无理由规则是什么？", "knowledge_qa"),
        ("沉香手串平时怎么保养？存放温度湿度大概多少", "knowledge_qa"),
        ("我今天吃火锅，好开心", "simple_qa"),
        ("abc123xyz999 随机乱码一堆", "simple_qa"),
        ("#B88820251001 珠裂了一颗，想修一下", "task"),
        ("保修时长一般多久？", "knowledge_qa"),
    ],
)
@pytest.mark.asyncio
async def test_tr76_classify_intent_4_categories_12_samples(text: str, expected_intent: str) -> None:
    cls = _llm_classifier()
    intent_4, order_ref, hint = await cls.aclassify(text)
    assert intent_4 == expected_intent, f"text={text!r} expected={expected_intent} got={intent_4}"
    assert isinstance(order_ref, (dict, type(None)))
    assert isinstance(hint, (str, type(None)))
    if "A-ORD-" in text or "#B" in text or "订单号" in text:
        assert order_ref is not None, f"text={text!r} 应该抽出订单号，但 order_ref=None"
        assert "order_no" in order_ref
    if ("退" in text or "退款" in text) and expected_intent != "simple_qa":
        assert hint in (None, "refund") or intent_4 in {"knowledge_qa", "simple_qa"}
    if ("修" in text or "裂" in text) and expected_intent == "task":
        assert hint == "repair"

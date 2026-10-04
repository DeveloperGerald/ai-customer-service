"""LangGraph StateGraph 构建（四分类意图 + 原生 create_agent 子图）。

图拓扑：
  START
    → intent_classify ── intent_router (4 路) ──┐
        simple_qa    → compliance_check
        handoff      → handoff → compliance_check
        knowledge_qa → policy_lookup ── knowledge_router ──┐
                         hit  → compliance_check
                         miss → rag_retrieve → compliance_check
        task         → task（原生 create_agent 子图，写工具内置 interrupt HITL）
                         → compliance_check
    → compliance_check (规则合规 + LLM 包装) → END

流式（原生多流，facade 消费 astream_unified 的统一事件协议）：
  graph.astream(input, config, stream_mode=["debug", "messages"])：
  - debug        {type:"task", payload:{name}}            → node_start
                 {type:"task_result", payload:{name, result}} → node_end（result 即 patch）
  - messages     (AIMessageChunk, metadata)               → reply_chunk
                 （仅透传 langgraph_node == "compliance_check" 的流；task 子图内
                  agent 的中间流不透传，避免双重打字机）

HITL：
  - 写工具 refund/exchange/repair/cancel_order 内 langgraph.types.interrupt(pending)
  - interrupt 冒泡到外层图暂停，checkpointer 落 Redis（TTL 600s）
  - facade 用 aget_state 检测 snap.next 非空 → yield confirmation_required
  - resume 用 Command(resume=...) 恢复（interrupt() 直接返回 resume 值）
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from langchain_core.messages import AIMessageChunk
from langchain_core.tracers import LangChainTracer
from langgraph.graph import END, START, StateGraph

from app.application.schemas.agent import AgentState


def _build_langgraph_config(
    base_config: dict[str, Any] | None = None,
    *,
    run_metadata: dict[str, Any] | None = None,
    run_name: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """构造传给 CompiledGraph.ainvoke/astream/aget_state 的 Config。

    - metadata：LangSmith UI 按 tenant_id / thread_id 过滤（已有 key 不覆盖）
    - configurable.thread_id：checkpointer 按此存取 checkpoint（HITL resume 必需）
    - callbacks：追加 LangChainTracer（发不发请求由 LANGSMITH_TRACING/Key 控制）
    """
    cfg: dict[str, Any] = dict(base_config or {})
    if run_metadata:
        merged_meta: dict[str, Any] = dict(cfg.get("metadata") or {})
        for k, v in run_metadata.items():
            merged_meta.setdefault(k, v)
        cfg["metadata"] = merged_meta
    if thread_id:
        configurable = cfg.get("configurable")
        if not isinstance(configurable, dict):
            configurable = {}
        configurable.setdefault("thread_id", thread_id)
        cfg["configurable"] = configurable
    if run_name:
        cfg.setdefault("run_name", run_name)
    # LangSmith trace 开关：LangChainTracer 直读原始 env（LANGSMITH_TRACING_V2 等），
    # 仅在显式开启 tracing 且配置了 API Key 时挂载（测试环境 conftest 会清空这些 env）。
    import os

    def _env_on(name: str) -> bool:
        return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes"}

    tracing_enabled = (
        _env_on("LANGSMITH_TRACING_V2")
        or _env_on("LANGSMITH_TRACING")
        or _env_on("LANGCHAIN_TRACING_V2")
    ) and bool(os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY"))
    if tracing_enabled:
        try:
            tracer = LangChainTracer()
            existing = cfg.get("callbacks")
            if existing is None:
                cfg["callbacks"] = [tracer]
            elif isinstance(existing, list):
                if not any(isinstance(c, LangChainTracer) for c in existing):
                    cfg["callbacks"] = [*existing, tracer]
            else:
                cfg["callbacks"] = [existing, tracer]
        except Exception:  # pragma: no cover - tracer 构造失败不影响主流程
            pass
    return cfg


async def astream_unified(
    graph: Any,
    state: Any,
    config: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """消费原生 astream 多流，映射为统一事件协议。

    state 可为 dict（初始运行）或 langgraph.types.Command（HITL resume）。
    事件：{"type":"node_start","node":...} / {"type":"node_end","node":...,"patch":...}
          / {"type":"token_chunk","text":...}
    """
    async for mode, evt in graph.astream(state, config=config, stream_mode=["debug", "messages"]):
        if mode == "debug":
            if not isinstance(evt, dict):
                continue
            etype = evt.get("type")
            payload = evt.get("payload") or {}
            name = payload.get("name") or ""
            if etype == "task" and name:
                yield {"type": "node_start", "node": name}
            elif etype == "task_result" and name:
                result = payload.get("result")
                yield {
                    "type": "node_end",
                    "node": name,
                    "patch": result if isinstance(result, dict) else None,
                }
        elif mode == "messages":
            chunk, meta = evt
            # 仅透传 compliance_check 的 token 流（task 子图内 agent 的流不透传）
            if not isinstance(meta, dict) or meta.get("langgraph_node") != "compliance_check":
                continue
            text = ""
            if isinstance(chunk, AIMessageChunk):
                c = chunk.content
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    for part in c:
                        if isinstance(part, dict) and "text" in part:
                            text += str(part["text"])
            elif isinstance(chunk, str):
                text = chunk
            if text:
                yield {"type": "token_chunk", "text": text}


def build_customer_service_graph(
    node_fns: dict[str, Callable[[dict[str, Any]], Any]],
    intent_router: Callable[[dict[str, Any]], str],
    knowledge_router: Callable[[dict[str, Any]], str],
    *,
    checkpointer: Any | None = None,
) -> Any:
    """构建售后客服 StateGraph（4 分类新拓扑 + 政策优先 + create_agent 子图 + 合规汇总）。

    Args:
        node_fns: 节点名 -> 带 ctx 绑定后的 async fn（facade._bind_node_contexts 产物）。
        intent_router: intent_classify 的 4 路路由。
        knowledge_router: policy_lookup 的 hit/miss 路由。
        checkpointer: langgraph checkpointer（Redis/Memory）。None 时调 get_checkpointer()。

    Returns:
        CompiledStateGraph：ainvoke / astream / aget_state 原生入口。
    """
    if checkpointer is None:
        from app.infrastructure.agent.checkpoint import get_checkpointer

        checkpointer = get_checkpointer()

    builder = StateGraph(AgentState)
    for name, fn in node_fns.items():
        builder.add_node(name, fn)
    builder.add_edge(START, "intent_classify")
    builder.add_conditional_edges("intent_classify", intent_router)
    builder.add_conditional_edges("policy_lookup", knowledge_router)
    builder.add_edge("handoff", "compliance_check")
    builder.add_edge("rag_retrieve", "compliance_check")
    builder.add_edge("task", "compliance_check")
    builder.add_edge("compliance_check", END)
    compile_kwargs: dict[str, Any] = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer
    return builder.compile(**compile_kwargs)

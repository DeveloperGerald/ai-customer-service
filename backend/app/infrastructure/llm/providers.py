"""Chat / Embedding 模型构建 + RAG 检索协议（LangChain 1.x 原生）。

- build_chat_model：langchain_openai.ChatOpenAI（智谱 GLM / DeepSeek / 硅基流动均兼容 OpenAI 协议）
- build_embeddings：langchain_openai.OpenAIEmbeddings（PGVectorStore 直接消费）
- BaseRetriever：检索协议（实现见 app/infrastructure/vectorstore.py，
  基于 langchain_postgres.PGVectorStore，强制 tenant_id 过滤）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.config import LLMSettings

# =========================================================================
# 1. Chat 模型（langchain_openai.ChatOpenAI）
# =========================================================================


def build_chat_model(settings: LLMSettings) -> Any:
    """构建 ChatOpenAI（BaseChatModel 子类，支持 bind_tools / astream / context）。

    仅支持 OpenAI 兼容协议；未配置 API Key 时抛出 ConfigError，应用启动失败。
    """
    if settings.provider.value != "openai":
        from app.core.errors import ConfigError

        raise ConfigError(
            "LLM provider 仅支持 openai（LLM__PROVIDER=openai），"
            f"当前值：{settings.provider.value}",
        )
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not key:
        from app.core.errors import ConfigError

        raise ConfigError(
            "OpenAI API Key 未配置（LLM__OPENAI_API_KEY），无法构建 ChatOpenAI。",
        )

    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.chat_model,
        api_key=key,
        base_url=settings.openai_base_url,
        temperature=settings.chat_temperature,
        max_tokens=settings.chat_max_tokens,
        timeout=settings.embedding_request_timeout,
    )


# =========================================================================
# 2. Embedding 模型（langchain_openai.OpenAIEmbeddings）
# =========================================================================


def build_embeddings(settings: LLMSettings) -> Any:
    """构建 OpenAIEmbeddings（langchain Embeddings，PGVectorStore 直接消费）。

    仅支持 OpenAI 兼容协议；未配置 API Key 时抛出 ConfigError，应用启动失败。
    dimensions 用 settings.embedding_dim（智谱 embedding-3 / OpenAI text-embedding-3
    均原生支持 dimensions 参数）。
    """
    if settings.embedding_provider.value != "openai":
        from app.core.errors import ConfigError

        raise ConfigError(
            "Embedding provider 仅支持 openai（LLM__EMBEDDING_PROVIDER=openai），"
            f"当前值：{settings.embedding_provider.value}",
        )
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not key:
        from app.core.errors import ConfigError

        raise ConfigError(
            "OpenAI API Key 未配置（LLM__OPENAI_API_KEY），无法使用 OpenAIEmbeddings。",
        )

    from langchain_openai import OpenAIEmbeddings

    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=key,
        base_url=settings.openai_base_url,
        dimensions=settings.embedding_dim,
        timeout=settings.embedding_request_timeout,
        chunk_size=settings.embedding_batch_size,
    )


# =========================================================================
# 3. extra_context 序列化（compliance_check LLM 包装输入拼装用）
# =========================================================================


def _format_extra_context(ctx: dict[str, Any]) -> str:
    """把 extra_context（RAG、政策、工具结果）序列化为模型易读的文本。"""
    lines: list[str] = []

    # 草稿回复：上游节点（task/policy/rag）产出的草稿，LLM 应基于此润色而非从头生成
    draft_reply = ctx.get("draft_reply")
    if isinstance(draft_reply, str) and draft_reply.strip():
        lines.append("【草稿回复（请基于此润色/补充，不要丢弃关键信息）】")
        lines.append(f"  {draft_reply}")

    # 订单/商品详情：工具调用返回的结构化数据，LLM 必须引用此数据回复用户
    order_detail = ctx.get("order_detail")
    if isinstance(order_detail, dict) and order_detail:
        import json as _json

        lines.append("【订单/商品详情（工具查询结果，回复用户时必须引用此数据，不得编造）】")
        lines.append(f"  {_json.dumps(order_detail, ensure_ascii=False, default=str)}")

    policy = ctx.get("policy_decision")
    if isinstance(policy, dict) and policy:
        lines.append("【政策判定结果】")
        for k, v in policy.items():
            if k == "debug":
                continue
            lines.append(f"  - {k}: {v}")

    action_result = ctx.get("action_result")
    if isinstance(action_result, dict) and action_result:
        lines.append("【工具/工单执行结果】")
        for k, v in action_result.items():
            lines.append(f"  - {k}: {v}")

    rag_hits = ctx.get("rag_hits")
    if isinstance(rag_hits, list) and rag_hits:
        lines.append("【知识库检索片段（按相关度排序）】")
        for i, hit in enumerate(rag_hits[:5], 1):
            content = hit.get("content", "")
            sim = hit.get("similarity")
            prefix = f"  [{i}]"
            if sim is not None:
                prefix += f"(sim={sim:.3f})"
            lines.append(f"{prefix} {content}")

    escalated = ctx.get("escalated")
    if escalated:
        ticket = ctx.get("escalated_ticket_no") or "(未知)"
        reason = ctx.get("escalation_reason") or ""
        lines.append(f"【已转人工】工单号={ticket}, 原因={reason}")

    return "\n".join(lines)


# =========================================================================
# 4. RAG 检索协议
# =========================================================================


class BaseRetriever(ABC):
    """检索器抽象，强制加 tenant_id 过滤（多租户硬隔离）。

    具体实现（PGVectorStore 版）见 app/infrastructure/vectorstore.py。
    """

    @abstractmethod
    async def retrieve(
        self,
        *,
        tenant_id: str,
        query: str,
        top_k: int,
        similarity_threshold: float,
    ) -> list[dict[str, Any]]:
        """返回证据 chunk 列表，按相似度从高到低排序。

        Args:
            tenant_id: 检索租户（必须来自 HTTP 鉴权 Actor）。
            query: 用户检索词。
            top_k: 返回最多条数。
            similarity_threshold: 余弦相似度下限。

        Returns:
            每项至少包含
            {"chunk_id","tenant_id","title","content","source","similarity","metadata"}。
        """

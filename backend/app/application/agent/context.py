"""Agent 运行期原生上下文（LangChain 1.x `context` 机制）。

facade 每次入口（invoke / astream_events / resume_stream）构造一次，随
`graph.astream(..., context=ctx)` 传入；task 子图的 @tool 通过
`runtime: ToolRuntime[AgentRunContext]` 取用，替代旧的 contextvar 旁路。

resume 是新 HTTP 请求、携带新 session → 直接构造新 context 即可，
身份字段只存在于 context，工具签名无 tenant_id/actor 参数。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.constants.policies import TenantPolicy
from app.domain.repositories.conversation import ConversationRepository
from app.domain.repositories.identity import Actor


@dataclass(frozen=True)
class AgentRunContext:
    actor: Actor
    tenant_id: str
    thread_id: str
    service_actor: Actor
    effective_policy: TenantPolicy
    idempotency_salt: str
    session: AsyncSession
    conversation_repo: ConversationRepository

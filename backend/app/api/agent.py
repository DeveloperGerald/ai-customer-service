"""Agent 对话 HTTP 接口（T10 同步 + SSE 流式 + HITL 确认）。

提供：
    POST   /api/conversations/{thread_id}/run           同步调用 Agent（普通 HTTP JSON 响应）
    GET/POST /api/conversations/{thread_id}/stream      SSE 流式：start → reply → debug → done + [DONE]
    POST   /api/conversations/{thread_id}/actions/confirm  HITL 写操作确认（resume 暂停的图）

所有接口强制：X-Tenant-Id + JWT Actor Middleware；越权一律 ResourceNotFound（404）。
"""

from __future__ import annotations

import json
import uuid as _uuid_mod
from collections.abc import AsyncGenerator
from typing import Any, Final

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import require_actor
from app.application.schemas.agent import (
    AgentConfirmRequest,
    AgentRunRequest,
    AgentRunResponse,
)
from app.application.schemas.conversation import (
    ConversationMessageCreate,
    ConversationThreadCreate,
)
from app.application.schemas.identity import Role
from app.core.logging import get_logger
from app.domain.repositories.conversation import ConversationRepository
from app.domain.repositories.identity import Actor, UserRepository
from app.infrastructure.db.engine import scoped_db_session

router = APIRouter(prefix="/api/agent", tags=["agent"])


# =========================================================================
# 依赖注入辅助函数（单测可 MonkeyPatch 替换）
# =========================================================================


def _is_not_found(exc: BaseException) -> bool:
    name = type(exc).__name__
    return "NotFound" in name or "ResourceNotFound" in name or name == "NoResultFound"


async def _session(request: Request) -> AsyncSession:
    async with scoped_db_session(request.app.state.bundle.infra) as sess:
        yield sess


async def _get_actor_and_verify_thread(
    request: Request,
    thread_id: str,
    actor: Actor,
    session: AsyncSession,
) -> tuple[Actor, Any]:
    """校验 thread_id 归属（consumer=自己；staff/admin=同租户），失败统一 404。

    懒创建策略：若 thread_id 格式合法（前缀=当前租户 tenant_id + UUID 后缀）但 DB
    中不存在，则以当前 actor 身份自动创建一条新线程并沿用原始 thread_id（满足前
    端先生成 thread_id 再发消息的演示模式）。

    返回 (actor, thread_row) 供后续调用 facade 使用。
    """
    from app.domain.repositories.conversation import _normalize_thread_id

    normalized = _normalize_thread_id(thread_id)
    repo = ConversationRepository(session)
    try:
        thread = await repo.get_thread(actor, actor.tenant_id, normalized)
    except Exception as exc:
        if not _is_not_found(exc):
            raise
        parts = normalized.split(":", 1)
        suffix_hex = parts[1] if len(parts) == 2 else ""
        prefix_ok = len(parts) == 2 and parts[0] == actor.tenant_id
        override_suffix = None
        try:
            override_suffix = _uuid_mod.UUID(hex=suffix_hex)
        except Exception:
            override_suffix = None
        if prefix_ok and override_suffix is not None:
            create_payload = ConversationThreadCreate(title="新会话")
            thread = await repo.create_thread(
                actor,
                actor.tenant_id,
                create_payload,
                _override_suffix=override_suffix,
            )
        else:
            raise HTTPException(status_code=404, detail="not found") from exc
    await session.commit()
    return actor, thread


async def _build_service_actor(tenant_id: str, session: AsyncSession) -> Actor:
    """可信 service actor：同租户 STAFF 身份，绕开 consumer 不能写 agent 消息的仓储限制。

    数据来源：users 表，约定 username = f"{tenant_id}_staff"（与 seed_tenants.py 一致）。
    若 DB 中尚未 seed → 自动创建一个同租户 STAFF 角色的系统内部账号，保证 FK 不悬空。
    """
    user_repo = UserRepository(session)
    service_staff = await user_repo.get_or_create_system_staff(tenant_id)
    return Actor(
        actor_id=str(service_staff.user_id),
        tenant_id=tenant_id,
        role=Role.STAFF,
    )


def _build_facade(request: Request):
    """从 app.state 取出预构建的 CustomerServiceAgentFacade 单例（真实 retriever/classifier）。

    优先级：
      1. agent_facade_overrides  —— 单测场景：conftest 注入自定义 facade
      2. agent_facade_singleton  —— 生产场景：main.py lifespan 已注入 PgVectorStoreRetriever + LLMIntentClassifier
      3. 懒实例化兜底             —— 仅在 lifespan 注入完全失败（缺配置/缺依赖）时走到本分支
           ❗ 兜底分支 = LLMIntentClassifier + Mock RAG（不做向量检索：向量库需 lifespan 初始化，
              此分支本就代表 lifespan 装配失败，不再重复建库），不是你想要的真实组件
              想强制用真实实例？ 检查启动日志里的 facade.wired 行，确认 retriever_enabled=true / classifier_mode=llm。
    """
    state = request.app.state
    overrides = getattr(state, "agent_facade_overrides", None)
    if overrides is not None:
        return overrides

    cached = getattr(state, "agent_facade_singleton", None)
    if cached is not None:
        return cached

    from app.application.agent.facade import CustomerServiceAgentFacade
    from app.infrastructure.llm.classifiers import (
        IntentClassifierProtocol,
        LLMIntentClassifier,
    )
    from app.infrastructure.llm.providers import (
        build_chat_model,
    )

    # ---------- 真实依赖注入（兜底分支，不再 None） ----------
    bundle = getattr(state, "bundle", None)
    settings = getattr(bundle, "settings", None) if bundle is not None else None
    llm_settings = getattr(settings, "llm", None) if settings is not None else None
    agent_settings = getattr(settings, "agent", None) if settings is not None else None

    # 1) Retriever：优先复用 lifespan 初始化好的向量库；懒构建路径不做向量检索
    retriever = None
    vector_store = getattr(state.bundle, "vector_store", None) if bundle is not None else None
    if vector_store is not None:
        from app.infrastructure.vectorstore import PgVectorStoreRetriever

        retriever = PgVectorStoreRetriever(vector_store)

    # 2) Classifier：仅使用 LLMIntentClassifier；若 chat_model 构建失败则为 None
    classifier: IntentClassifierProtocol | None = None
    chat_model = None
    if llm_settings is not None:
        chat_model = build_chat_model(llm_settings)
        if chat_model is not None:
            try:
                classifier = LLMIntentClassifier(
                    chat_model=chat_model,
                    timeout_seconds=8.0,
                )
            except Exception:
                classifier = None

    # 3) RAG 参数
    rag_top_k = getattr(agent_settings, "rag_top_k", 5) if agent_settings is not None else 5
    rag_similarity_threshold = (
        getattr(agent_settings, "rag_similarity_threshold", 0.6)
        if agent_settings is not None
        else 0.6
    )

    facade = CustomerServiceAgentFacade(
        retriever=retriever,
        classifier=classifier,
        chat_model=chat_model,
        rag_top_k=rag_top_k,
        rag_similarity_threshold=rag_similarity_threshold,
    )
    state.agent_facade_singleton = facade
    return facade


# =========================================================================
# 路由 1：同步 JSON 响应
# =========================================================================


@router.post("/conversations/{thread_id}/run", response_model=AgentRunResponse)
async def agent_invoke_sync(
    thread_id: str,
    request: Request,
    body: AgentRunRequest,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> AgentRunResponse:
    """同步调用 Agent 处理一条用户消息。

    - 校验 thread_id 归属
    - 追加 user_message → 调 facade.invoke → 追加 assistant_message
    - 返回 AgentRunResponse（final_reply + escalated + decision_debug）
    """
    from app.domain.repositories.conversation import _normalize_thread_id

    actor, _thread = await _get_actor_and_verify_thread(request, thread_id, actor, session)
    facade = _build_facade(request)
    normalized_thread_id = _normalize_thread_id(thread_id)

    repo = ConversationRepository(session)
    try:
        result = await facade.invoke(
            tenant_id=actor.tenant_id,
            actor=actor,
            thread_id=normalized_thread_id,
            user_message=body.text,
            session=session,
            idempotency_salt=body.idempotency_key or "",
        )
    except Exception as exc:
        if _is_not_found(exc):
            raise HTTPException(status_code=404, detail="not found") from exc
        raise

    service_actor = await _build_service_actor(actor.tenant_id, session)
    await repo.append_message(
        service_actor,
        actor.tenant_id,
        normalized_thread_id,
        ConversationMessageCreate(role="agent", content=result.final_reply),
    )
    if result.escalated and result.escalated_ticket_no:
        await repo.mark_escalated(
            service_actor, actor.tenant_id, normalized_thread_id, result.escalated_ticket_no
        )
    await session.commit()

    return AgentRunResponse(
        final_reply=result.final_reply,
        escalated=result.escalated,
        escalated_ticket_no=result.escalated_ticket_no,
        escalation_reason=result.escalation_reason,
        decision_debug=result.decision_debug,
    )


# =========================================================================
# 路由 2：SSE 流式响应（start → reply → debug → done + [DONE]）
# =========================================================================


SSE_CONTENT_TYPE = "text/event-stream; charset=utf-8"


def _json_default(obj: Any) -> Any:
    """JSON 序列化兜底：Pydantic/dataclass → dict；UUID/datetime/Decimal → 原生可序列化；其他 → repr(str)。

    保证 SSE / JSON 响应遇到 Actor / ConversationMessage 等复杂对象时绝不抛错，
    防止前端收到「生成失败：Object of type X is not JSON serializable」。
    """
    import dataclasses as _dc

    # 1. dataclass（Actor 等）
    if _dc.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _json_default(v) for k, v in _dc.asdict(obj).items()}
    # 2. Pydantic v1/v2 通用（有 model_dump 优先 v2，否则 dict）
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    dict_fn = getattr(obj, "dict", None)
    if callable(dict_fn):
        try:
            return dict_fn()
        except Exception:
            pass
    # 3. 标量类
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    # 4. datetime/date/Decimal/UUID 等都在 str 里兜住
    try:
        return str(obj)
    except Exception:
        return repr(obj)


def _safe_json_payload(data: Any) -> Any:
    """对 SSE debug/payload 等可能混杂 domain/Pydantic/dataclass 对象的结构做一次安全归一化。

    - 遍历 dict/list，把 Pydantic / dataclass / Actor / UUID 等转成 JSON 可序列化值；
    - 保留 dict/list 的嵌套结构，单值走 _json_default。
    """
    import dataclasses as _dc

    if _dc.is_dataclass(data) and not isinstance(data, type):
        return {k: _safe_json_payload(v) for k, v in _dc.asdict(data).items()}
    if isinstance(data, dict):
        return {str(k): _safe_json_payload(v) for k, v in data.items()}
    if isinstance(data, (list, tuple, set, frozenset)):
        return [_safe_json_payload(v) for v in data]
    if data is None or isinstance(data, (bool, int, float, str)):
        return data
    dump = getattr(data, "model_dump", None)
    if callable(dump):
        try:
            return _safe_json_payload(dump(mode="json"))
        except TypeError:
            return _safe_json_payload(dump())
    dict_fn = getattr(data, "dict", None)
    if callable(dict_fn):
        try:
            return _safe_json_payload(dict_fn())
        except Exception:
            pass
    return _json_default(data)


def _sse_event(event: str, data: Any) -> str:
    """组装一条 SSE 帧：event:xxx\\ndata:<json>\\n\\n。

    失败安全：即使 data 里混有 Actor / ConversationMessage / UUID 等非 JSON 原生类型，
    也通过 _json_default 转成字符串，绝不抛出 TypeError。
    """
    body = json.dumps(data, ensure_ascii=False, default=_json_default)
    return f"event: {event}\ndata: {body}\n\n"


# SSE 强制 flush comment：每业务帧后紧跟一行透明注释帧，强制反向代理（Nginx/CDN）与 uvicorn
# 立刻把 socket buffer 刷走。否则代理默认攒到 4~8KB 才 flush，SSE 几十字节的小帧会被一直
# 缓冲到生成器结束，浏览器端一次性收到所有 reply_chunk → 打字机不生效、整段文字同时出现。
_SSE_FLUSH_HINT: Final[str] = ": sse-flush\n\n"


async def _stream_agent_run(
    actor: Actor,
    thread_id: str,
    user_input: str,
    session: AsyncSession,
    facade: Any,
    idempotency_key: str | None,
    repo: ConversationRepository,
) -> AsyncGenerator[str]:
    """真正的流式 SSE 生成器：逐节点 + 逐 token 推送。

    关键保证（面试演示用）：
    - 每帧后跟一条 flush-hint，强制反向代理/uvicorn 立即 flush → 打字机节奏真实可见。
    - 所有帧写结构化日志（event / size / elapsed_ms），复现时一眼能定位 backend 是否按预期产出 reply_chunk。
    - 整条链路 try/except：任何异常都 yield `error → done → [DONE]`，绝不抛 Starlette。
    - 仓储 commit 失败 non-fatal（会话内容已经被前端流式展示出来了），不向上冒泡。
    """
    import time as _time
    from collections.abc import AsyncIterator

    from app.domain.repositories.conversation import _normalize_thread_id

    log = get_logger("agent.sse")
    normalized_thread_id = _normalize_thread_id(thread_id)
    t0 = _time.perf_counter()
    rc_total = 0
    rc_bytes = 0

    def _emit(frame: str) -> str:
        return frame + _SSE_FLUSH_HINT

    sf = _sse_event("start", {"thread_id": normalized_thread_id, "user_input": user_input})
    log.info(
        "sse.frame",
        tenant_id=actor.tenant_id,
        thread_id=normalized_thread_id,
        sse_event="start",
        size_bytes=len(sf),
        elapsed_ms=int((_time.perf_counter() - t0) * 1000),
    )
    yield _emit(sf)

    try:
        events_it: AsyncIterator[dict] = facade.astream_events(
            tenant_id=actor.tenant_id,
            actor=actor,
            thread_id=normalized_thread_id,
            user_message=user_input,
            session=session,
            idempotency_salt=idempotency_key or "",
        )
        async for evt in events_it:
            etype = evt.get("type")
            if etype == "start":
                if not evt.get("thread_id"):
                    continue
            elif etype == "node_start":
                frame = _sse_event(
                    "node",
                    {"phase": "start", "node": evt.get("node", "")},
                )
                log.info(
                    "sse.frame",
                    tenant_id=actor.tenant_id,
                    thread_id=normalized_thread_id,
                    sse_event="node:start",
                    node=evt.get("node"),
                    size_bytes=len(frame),
                    elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                )
                yield _emit(frame)
                continue
            elif etype == "node_end":
                node_err = evt.get("error")
                frame = _sse_event(
                    "node",
                    {
                        "phase": "end",
                        "node": evt.get("node", ""),
                        "has_patch": bool(evt.get("patch")),
                        "has_error": bool(node_err),
                        "error": node_err,
                    },
                )
                log.info(
                    "sse.frame",
                    tenant_id=actor.tenant_id,
                    thread_id=normalized_thread_id,
                    sse_event="node:end",
                    node=evt.get("node"),
                    has_patch=bool(evt.get("patch")),
                    has_error=bool(node_err),
                    size_bytes=len(frame),
                    elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                )
                yield _emit(frame)
                continue
            elif etype == "tool":
                payload_safe = _safe_json_payload(evt.get("payload") or {})
                frame = _sse_event(
                    "tool",
                    {"kind": evt.get("kind", "tool"), "payload": payload_safe},
                )
                log.info(
                    "sse.frame",
                    tenant_id=actor.tenant_id,
                    thread_id=normalized_thread_id,
                    sse_event="tool",
                    kind=evt.get("kind"),
                    size_bytes=len(frame),
                    elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                )
                yield _emit(frame)
                continue
            elif etype == "escalated":
                frame = _sse_event(
                    "escalated",
                    {"ticket_no": evt.get("ticket_no"), "reason": evt.get("reason")},
                )
                log.info(
                    "sse.frame",
                    tenant_id=actor.tenant_id,
                    thread_id=normalized_thread_id,
                    sse_event="escalated",
                    ticket_no=evt.get("ticket_no"),
                    size_bytes=len(frame),
                    elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                )
                yield _emit(frame)
                continue
            elif etype == "confirmation_required":
                # HITL 暂停：写工具 interrupt 触发，前端弹确认卡片；本轮不发 reply/done
                pending = evt.get("pending_action") or {}
                frame = _sse_event(
                    "confirmation_required",
                    {
                        "thread_id": evt.get("thread_id", normalized_thread_id),
                        "pending_action": _safe_json_payload(pending),
                    },
                )
                log.info(
                    "sse.frame",
                    tenant_id=actor.tenant_id,
                    thread_id=normalized_thread_id,
                    sse_event="confirmation_required",
                    tool=pending.get("tool") if isinstance(pending, dict) else None,
                    size_bytes=len(frame),
                    elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                )
                yield _emit(frame)
                continue
            elif etype == "resume_started":
                # HITL resume 已开始（/actions/confirm 端点的响应流专用）
                frame = _sse_event(
                    "resume_started",
                    {
                        "thread_id": evt.get("thread_id", normalized_thread_id),
                        "decision": _safe_json_payload(evt.get("decision") or {}),
                    },
                )
                yield _emit(frame)
                continue
            elif etype == "reply_chunk":
                text = evt.get("text", "") or ""
                if not text:
                    continue
                rc_total += 1
                rc_bytes += len(text)
                frame = _sse_event("reply_chunk", {"text": text})
                if rc_total == 1 or rc_total % 10 == 0:
                    log.info(
                        "sse.frame",
                        tenant_id=actor.tenant_id,
                        thread_id=normalized_thread_id,
                        sse_event="reply_chunk",
                        rc_index=rc_total,
                        rc_bytes=rc_bytes,
                        size_bytes=len(frame),
                        elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                    )
                yield _emit(frame)
                continue
            elif etype == "reply":
                final_text = evt.get("text", "") or ""
                frame = _sse_event("reply", {"text": final_text})
                log.info(
                    "sse.frame",
                    tenant_id=actor.tenant_id,
                    thread_id=normalized_thread_id,
                    sse_event="reply",
                    final_len=len(final_text),
                    reply_chunk_count=rc_total,
                    reply_chunk_total_bytes=rc_bytes,
                    size_bytes=len(frame),
                    elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                )
                yield _emit(frame)
                continue
            elif etype == "debug":
                payload = _safe_json_payload(evt.get("payload") or {})
                frame = _sse_event("debug", {"payload": payload})
                log.info(
                    "sse.frame",
                    tenant_id=actor.tenant_id,
                    thread_id=normalized_thread_id,
                    sse_event="debug",
                    size_bytes=len(frame),
                    elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                )
                yield _emit(frame)
                continue
            elif etype == "done":
                frame = _sse_event("done", {})
                log.info(
                    "sse.frame",
                    tenant_id=actor.tenant_id,
                    thread_id=normalized_thread_id,
                    sse_event="done",
                    reply_chunk_count=rc_total,
                    reply_chunk_total_bytes=rc_bytes,
                    size_bytes=len(frame),
                    elapsed_ms=int((_time.perf_counter() - t0) * 1000),
                )
                yield _emit(frame)
                continue
            else:
                yield _emit(_sse_event("stream_event", _safe_json_payload(evt)))

        yield "event: done\ndata: [DONE]\n\n"
        log.info(
            "sse.done",
            tenant_id=actor.tenant_id,
            thread_id=normalized_thread_id,
            reply_chunk_count=rc_total,
            reply_chunk_total_bytes=rc_bytes,
            total_ms=int((_time.perf_counter() - t0) * 1000),
        )

        try:
            await session.commit()
        except Exception:
            log.warning("sse.commit_failed_non_fatal", tenant_id=actor.tenant_id)
            try:
                await session.rollback()
            except Exception:
                pass

    except Exception as exc:
        log.exception(
            "agent.stream.failed",
            tenant_id=actor.tenant_id,
            thread_id=normalized_thread_id,
            error_type=type(exc).__name__,
            reply_chunk_count=rc_total,
            reply_chunk_total_bytes=rc_bytes,
            total_ms=int((_time.perf_counter() - t0) * 1000),
        )
        ef = _sse_event(
            "error",
            {
                "type": type(exc).__name__,
                "message": str(exc),
                "reply_chunk_count": rc_total,
                "reply_chunk_total_bytes": rc_bytes,
            },
        )
        yield _emit(ef)
        df = _sse_event("done", {})
        yield _emit(df)
        yield "event: done\ndata: [DONE]\n\n"
        try:
            await session.rollback()
        except Exception:
            pass


@router.post("/conversations/{thread_id}/stream")
async def agent_invoke_stream(
    thread_id: str,
    request: Request,
    body: AgentRunRequest,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> StreamingResponse:
    """SSE 流式调用 Agent。

    事件序列（前端按 event 名分发）：
        start                 → {thread_id, user_input}
        escalated             → {ticket_no, reason}   （可选；命中转人工分支时出现）
        tool                  → {kind, payload}       （可选；当 policy_decision / tool_call 时）
        confirmation_required  → {thread_id, pending_action}  （可选；写工具 HITL 暂停时出现）
                                                          前端弹确认卡片；本轮不发 reply/done。
                                                          用户点击后调 /actions/confirm 续跑。
        reply_chunk           → {text}                 (逐 token；compliance_check 产出)
        reply                 → {text}                 （最终自然语言回复，兼容旧版前端）
        debug                 → {payload}              （前端「决策依据」面板）
        done                  → {} / [DONE]
    """
    actor, _ = await _get_actor_and_verify_thread(request, thread_id, actor, session)
    facade = _build_facade(request)
    repo = ConversationRepository(session)

    gen = _stream_agent_run(
        actor=actor,
        thread_id=thread_id,
        user_input=body.text,
        session=session,
        facade=facade,
        idempotency_key=body.idempotency_key,
        repo=repo,
    )
    # SSE 必须显式关闭所有层的 body 缓冲：反向代理（Nginx）、CDN、uvicorn、浏览器。
    # 少任何一个 header 都会导致几十字节的小 reply_chunk 被攒到 4KB / 结束时才一起 flush，
    # 表现就是"光标闪一会儿 → 所有文字同时出现"，完全没有打字机节奏。
    sse_headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate, no-transform, private",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Accel-Charset": "utf-8",
    }
    return StreamingResponse(gen, media_type=SSE_CONTENT_TYPE, headers=sse_headers)


# =========================================================================
# 路由 3：HITL 写操作确认（resume 暂停的图）
# =========================================================================


async def _stream_agent_resume(
    actor: Actor,
    thread_id: str,
    decision_payload: dict[str, Any],
    session: AsyncSession,
    facade: Any,
    repo: ConversationRepository,
) -> AsyncGenerator[str]:
    """HITL resume 的 SSE 生成器：与 _stream_agent_run 同协议产出续跑事件。

    facade.resume_stream 内部会先调 get_state 校验是否仍处于暂停态：
      - 无 pending（已超时 / 已被处理）→ yield error + done
      - 有 pending → 用 Command(resume=decision) 恢复 → reply_chunk/reply/done
    """
    import time as _time
    from collections.abc import AsyncIterator

    from app.domain.repositories.conversation import _normalize_thread_id

    log = get_logger("agent.sse.resume")
    normalized_thread_id = _normalize_thread_id(thread_id)
    t0 = _time.perf_counter()
    rc_total = 0
    rc_bytes = 0

    def _emit(frame: str) -> str:
        return frame + _SSE_FLUSH_HINT

    sf = _sse_event("resume_start", {"thread_id": normalized_thread_id, "decision": decision_payload})
    log.info(
        "sse.frame",
        tenant_id=actor.tenant_id,
        thread_id=normalized_thread_id,
        sse_event="resume_start",
        decision=decision_payload,
        size_bytes=len(sf),
        elapsed_ms=int((_time.perf_counter() - t0) * 1000),
    )
    yield _emit(sf)

    try:
        events_it: AsyncIterator[dict] = facade.resume_stream(
            actor=actor,
            tenant_id=actor.tenant_id,
            thread_id=normalized_thread_id,
            session=session,
            decision=decision_payload,
        )
        async for evt in events_it:
            etype = evt.get("type")
            if etype == "resume_started":
                # facade 内部已发，SSE 层忽略（避免重复）
                continue
            if etype == "node_start":
                yield _emit(_sse_event("node", {"phase": "start", "node": evt.get("node", "")}))
                continue
            if etype == "node_end":
                yield _emit(
                    _sse_event(
                        "node",
                        {
                            "phase": "end",
                            "node": evt.get("node", ""),
                            "has_patch": bool(evt.get("patch")),
                            "has_error": bool(evt.get("error")),
                            "error": evt.get("error"),
                        },
                    )
                )
                continue
            if etype == "reply_chunk":
                text = evt.get("text", "") or ""
                if not text:
                    continue
                rc_total += 1
                rc_bytes += len(text)
                yield _emit(_sse_event("reply_chunk", {"text": text}))
                continue
            if etype == "reply":
                yield _emit(_sse_event("reply", {"text": evt.get("text", "")}))
                continue
            if etype == "escalated":
                yield _emit(
                    _sse_event(
                        "escalated",
                        {"ticket_no": evt.get("ticket_no"), "reason": evt.get("reason")},
                    )
                )
                continue
            if etype == "debug":
                payload = _safe_json_payload(evt.get("payload") or {})
                yield _emit(_sse_event("debug", {"payload": payload}))
                continue
            if etype == "error":
                yield _emit(
                    _sse_event(
                        "error",
                        {"message": evt.get("message", "操作已超时或已被处理。")},
                    )
                )
                continue
            if etype == "done":
                yield _emit(_sse_event("done", {}))
                continue
            yield _emit(_sse_event("stream_event", _safe_json_payload(evt)))

        yield "event: done\ndata: [DONE]\n\n"
        log.info(
            "sse.resume.done",
            tenant_id=actor.tenant_id,
            thread_id=normalized_thread_id,
            reply_chunk_count=rc_total,
            reply_chunk_total_bytes=rc_bytes,
            total_ms=int((_time.perf_counter() - t0) * 1000),
        )

        try:
            await session.commit()
        except Exception:
            log.warning("sse.resume.commit_failed_non_fatal", tenant_id=actor.tenant_id)
            try:
                await session.rollback()
            except Exception:
                pass

    except Exception as exc:
        log.exception(
            "agent.resume.failed",
            tenant_id=actor.tenant_id,
            thread_id=normalized_thread_id,
            error_type=type(exc).__name__,
            error=str(exc),
            reply_chunk_count=rc_total,
            reply_chunk_total_bytes=rc_bytes,
            total_ms=int((_time.perf_counter() - t0) * 1000),
        )
        yield _emit(
            _sse_event(
                "error",
                {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "reply_chunk_count": rc_total,
                },
            )
        )
        yield _emit(_sse_event("done", {}))
        yield "event: done\ndata: [DONE]\n\n"
        try:
            await session.rollback()
        except Exception:
            pass


@router.post("/conversations/{thread_id}/actions/confirm")
async def agent_confirm_action(
    thread_id: str,
    request: Request,
    body: AgentConfirmRequest,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_session),
) -> StreamingResponse:
    """HITL 写操作确认：用户在页面上点击「确认/取消」后续跑暂停的图。

    流程：
      1. 校验 thread_id 归属
      2. 调 facade.resume_stream({"confirmed": body.decision, "reason": body.reason})
         → facade 内部先 get_state 校验暂停态（无 pending → yield error + done）
         → 用 Command(resume=decision) 恢复 → 续跑 task → compliance → SSE 流式
      3. 超时（checkpointer TTL=600s 过期）→ facade 检测无 pending → 返回 error 事件

    SSE 事件序列与 /stream 端点一致（reply_chunk/reply/done/error）。
    """
    actor, _ = await _get_actor_and_verify_thread(request, thread_id, actor, session)
    facade = _build_facade(request)
    repo = ConversationRepository(session)

    decision_payload: dict[str, Any] = {"confirmed": body.decision}
    if body.reason:
        decision_payload["reason"] = body.reason

    gen = _stream_agent_resume(
        actor=actor,
        thread_id=thread_id,
        decision_payload=decision_payload,
        session=session,
        facade=facade,
        repo=repo,
    )
    sse_headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate, no-transform, private",
        "Pragma": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
        "X-Accel-Charset": "utf-8",
    }
    return StreamingResponse(gen, media_type=SSE_CONTENT_TYPE, headers=sse_headers)

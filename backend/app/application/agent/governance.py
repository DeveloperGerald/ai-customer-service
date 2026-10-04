"""工具治理中间件：审计（tool_audit_logs）+ 幂等（idempotency_records）。

承接旧 ToolRunner 的完整语义，通过 AgentMiddleware.awrap_tool_call
包住 task agent 内每次工具调用：

1. 写工具生成幂等 key（agt-{thread_id}-{seq}-{salt}）；
2. 写工具 resume 时复用暂停前那条 running 审计行（不新增悬垂行）；
3. 查 idempotency_records：命中成功 → 回传缓存结果；参数哈希不一致 → 失败 observation；
4. 写 tool_audit_logs（running）；
5. 执行 handler：
   - GraphBubbleUp（含 GraphInterrupt = HITL 暂停）→ 放行，audit 保持 running；
   - 业务/未知异常 → audit 收尾失败态，回传失败 observation（同旧 ToolRunner）；
6. 成功 → 写 idempotency_records（写工具，24h TTL）+ audit 完成态；
7. 返回 Command 做状态字段映射：tool_executions / policy_decision /
   order_detail_json / action_kind / action_result_json，accepted=False →
   action_kind="aborted" + 标准拒绝 final_reply。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.application.agent.context import AgentRunContext
from app.application.schemas.conversation import ConversationMessageCreate
from app.application.schemas.tools import ToolCallError, ToolResult
from app.core.errors import AppBaseError, ErrorCode
from app.domain.models.tools import IdempotencyRecordORM, ToolAuditLogORM
from app.domain.repositories.conversation import ConversationRepository

# 幂等缓存过期时间
_IDEMPOTENCY_TTL = dt.timedelta(hours=24)

_WRITE_TOOLS = frozenset({
    "refund_request",
    "exchange_request",
    "repair_request",
    "cancel_order",
})

_ACTION_KIND_BY_TOOL = {
    "refund_request": "refund",
    "exchange_request": "exchange",
    "repair_request": "repair",
    "cancel_order": "cancel",
}

_ABORT_LABEL = {
    "cancel_order": "取消订单",
    "refund_request": "退款",
    "exchange_request": "换货",
    "repair_request": "维修",
}


def _hash_arguments(tool_name: str, arguments: dict[str, Any]) -> str:
    raw = tool_name + "::" + json.dumps(
        arguments, ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _error_observation(tool_name: str, err: ToolCallError) -> str:
    """旧 base.format_observation 的失败分支形状。"""
    return json.dumps(
        {
            "tool": tool_name,
            "success": False,
            "error_code": err.code,
            "error": err.message,
        },
        ensure_ascii=False,
        default=str,
    )


def _error_from_exception(exc: Exception) -> ToolCallError:
    """旧 ToolRunner 的错误分类：AppBaseError 精确取码，其余统一包装。"""
    if isinstance(exc, AppBaseError):
        code = getattr(exc, "code", None) or ErrorCode.TOOL_EXECUTION_ERROR
        return ToolCallError(
            code=str(code.value) if hasattr(code, "value") else str(code),
            message=str(getattr(exc, "message", str(exc))),
            details=getattr(exc, "details", None),
            retryable=bool(getattr(exc, "retryable", False)),
        )
    return ToolCallError(
        code=str(ErrorCode.TOOL_EXECUTION_ERROR.value),
        message="工具执行出现未知异常",
        details={"exc_type": type(exc).__name__, "exc": str(exc)},
        retryable=False,
    )


def _order_no_in_user_messages(state: Any, order_no: str) -> bool:
    """检查订单号是否真实出现在任意一条用户消息中（防模型编造）。"""
    target = order_no.strip().upper()
    for m in state.get("messages") or []:
        if getattr(m, "type", None) in ("human", "user") and target in str(
            getattr(m, "content", "")
        ).upper():
            return True
    return False


class ToolGovernanceMiddleware(AgentMiddleware):
    """审计 + 幂等治理中间件（无状态，随 create_agent 编译）。"""

    async def awrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        tool_call = request.tool_call
        tool_name = tool_call["name"]
        args: dict[str, Any] = dict(tool_call.get("args") or {})
        args_hash = _hash_arguments(tool_name, args)

        run_ctx: AgentRunContext = request.runtime.context
        session = run_ctx.session
        actor = run_ctx.actor

        started_at = dt.datetime.now(dt.timezone.utc)
        is_write = tool_name in _WRITE_TOOLS

        idem_key: str | None = None
        if is_write:
            prior_writes = [
                e
                for e in (request.state.get("tool_executions") or [])
                if isinstance(e, dict) and e.get("tool_name") in _WRITE_TOOLS
            ]
            salt = run_ctx.idempotency_salt or "default"
            idem_key = f"agt-{run_ctx.thread_id}-{len(prior_writes) + 1}-{salt}"

        # 写工具 resume：复用暂停前的 running 审计行
        log_orm: ToolAuditLogORM | None = None
        if is_write:
            log_orm = await self._load_running_log(
                session, actor.tenant_id, idem_key
            )
            if log_orm is not None and log_orm.arguments_hash != args_hash:
                err = ToolCallError(
                    code=str(ErrorCode.TOOL_IDEMPOTENCY_CONFLICT.value),
                    message="同一 idempotency_key 传入了不同 arguments（参数哈希不一致）",
                    details={
                        "expected_hash": log_orm.arguments_hash,
                        "provided_hash": args_hash,
                    },
                    retryable=False,
                )
                return await self._finish_failed(
                    request=request,
                    log_orm=log_orm,
                    err=err,
                    tool_name=tool_name,
                )
        if log_orm is None:
            log_orm = ToolAuditLogORM(
                call_id=uuid4(),
                tenant_id=actor.tenant_id,
                actor_id=UUID(actor.actor_id),
                session_id=run_ctx.thread_id,
                tool_name=tool_name,
                idempotency_key=idem_key,
                arguments_hash=args_hash,
                arguments_json=args,
                status="running",
                started_at=started_at,
            )
            session.add(log_orm)
            await session.flush()

        call_id = log_orm.call_id

        # 防幻觉护栏：order_query 的 order_no 必须真实出现在用户消息中。
        # GLM-4-Flash 偶发在用户未提供订单号时编造（如 A-ORD-202509-001），
        # 不仅浪费一次 DB 查询，还会让模型基于不存在的订单继续推理。
        if tool_name == "order_query":
            order_no_arg = args.get("order_no")
            if order_no_arg and not _order_no_in_user_messages(request.state, str(order_no_arg)):
                err = ToolCallError(
                    code=str(ErrorCode.VALIDATION_ERROR.value),
                    message=(
                        f"订单号 {order_no_arg} 未出现在任何用户消息中（疑似模型编造）。"
                        "禁止编造订单号：请直接回复用户，请其提供真实订单号。"
                    ),
                    details={"order_no": str(order_no_arg)},
                    retryable=True,
                )
                return await self._finish_failed(
                    request=request,
                    log_orm=log_orm,
                    err=err,
                    tool_name=tool_name,
                )

        # 幂等记录检查（写工具）
        from_cache = False
        data: dict[str, Any]
        cached: IdempotencyRecordORM | None = None
        if is_write:
            cached = await self._load_idempotency_record(
                session, actor.tenant_id, idem_key
            )
            if cached is not None:
                if cached.arguments_hash != args_hash:
                    err = ToolCallError(
                        code=str(ErrorCode.TOOL_IDEMPOTENCY_CONFLICT.value),
                        message="同一 idempotency_key 传入了不同 arguments（参数哈希不一致）",
                        details={
                            "expected_hash": cached.arguments_hash,
                            "provided_hash": args_hash,
                        },
                        retryable=False,
                    )
                    return await self._finish_failed(
                        request=request,
                        log_orm=log_orm,
                        err=err,
                        tool_name=tool_name,
                    )
                data = dict(cached.cached_result_json or {"cache_hit": True})
                from_cache = True

        if not from_cache:
            try:
                tool_message = await handler(request)
            except GraphBubbleUp:
                # HITL 暂停：audit 保持 running，外层 API 提交后落 Redis
                raise
            except Exception as exc:
                return await self._finish_failed(
                    request=request,
                    log_orm=log_orm,
                    err=_error_from_exception(exc),
                    tool_name=tool_name,
                )
            data = json.loads(tool_message.content)
        else:
            tool_message = ToolMessage(
                content=json.dumps(data, ensure_ascii=False, default=str),
                name=tool_name,
                tool_call_id=tool_call.get("id"),
            )

        finished_at = dt.datetime.now(dt.timezone.utc)
        duration_ms = max(
            0, int((finished_at - started_at).total_seconds() * 1000)
        )

        accepted = data.get("accepted")
        update: dict[str, Any] = {"messages": [tool_message]}

        execution = ToolResult(
            tool_name=tool_name,
            call_id=call_id,
            success=True,
            data=data,
            from_idempotency_cache=from_cache,
            started_at=started_at,
            finished_at=finished_at,
        ).model_dump(mode="json")
        executions = list(request.state.get("tool_executions") or [])
        executions.append(execution)
        update["tool_executions"] = executions

        if tool_name == "policy_check":
            update["policy_decision"] = data
        if tool_name in {"order_query", "product_query", "product_list"}:
            update["order_detail_json"] = data

        if is_write:
            if accepted is False:
                # 用户拒绝：标准拒绝文案直写 final_reply，绕过 LLM 改写
                label = _ABORT_LABEL.get(tool_name, "售后")
                order_no = data.get("order_no") or ""
                order_part = f"（订单号 {order_no}）" if order_no else ""
                abort_reply = (
                    f"您已取消本次{label}操作{order_part}，未执行任何处理，"
                    "订单状态保持不变。如需继续，请重新发起。"
                )
                update["action_kind"] = "aborted"
                update["action_result_json"] = data
                update["draft_reply"] = abort_reply
                update["final_reply"] = abort_reply
            elif accepted is True:
                action_kind = _ACTION_KIND_BY_TOOL[tool_name]
                update["action_kind"] = action_kind
                update["action_result_json"] = data
                await self._append_tool_conversation_message(
                    run_ctx, data, tool_name
                )
                # 首次成功写幂等记录（DB UQ 兜底防并发）
                if not from_cache:
                    await session.execute(
                        insert(IdempotencyRecordORM)
                        .values(
                            tenant_id=actor.tenant_id,
                            idempotency_key=idem_key,
                            tool_name=tool_name,
                            call_id=call_id,
                            arguments_hash=args_hash,
                            cached_result_json=data,
                            expires_at=finished_at + _IDEMPOTENCY_TTL,
                        )
                        .on_conflict_do_nothing(
                            index_elements=["tenant_id", "idempotency_key"]
                        )
                    )

        log_orm.status = "succeeded"
        log_orm.result_json = data
        log_orm.finished_at = finished_at
        log_orm.duration_ms = duration_ms
        log_orm.error_code = None
        await session.flush()

        return Command(update=update)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _finish_failed(
        self,
        *,
        request: Any,
        log_orm: ToolAuditLogORM,
        err: ToolCallError,
        tool_name: str,
    ) -> Command:
        """audit 收尾失败态 + 回传失败 observation（旧 ToolRunner._finalize_err 语义）。"""
        finished_at = dt.datetime.now(dt.timezone.utc)
        duration_ms = max(
            0, int((finished_at - log_orm.started_at).total_seconds() * 1000)
        )
        log_orm.status = "failed"
        log_orm.result_json = {"error": err.model_dump(mode="json")}
        log_orm.finished_at = finished_at
        log_orm.duration_ms = duration_ms
        log_orm.error_code = err.code
        session: Any = request.runtime.context.session
        await session.flush()

        tool_message = ToolMessage(
            content=_error_observation(tool_name, err),
            name=tool_name,
            tool_call_id=request.tool_call.get("id"),
        )
        execution = ToolResult(
            tool_name=tool_name,
            call_id=log_orm.call_id,
            success=False,
            data=None,
            error=err,
            from_idempotency_cache=False,
            started_at=log_orm.started_at,
            finished_at=finished_at,
        ).model_dump(mode="json")
        executions = list(request.state.get("tool_executions") or [])
        executions.append(execution)
        return Command(
            update={"messages": [tool_message], "tool_executions": executions}
        )

    async def _load_running_log(
        self, session: Any, tenant_id: str, idem_key: str | None
    ) -> ToolAuditLogORM | None:
        if not idem_key:
            return None
        stmt = select(ToolAuditLogORM).where(
            ToolAuditLogORM.tenant_id == tenant_id,
            ToolAuditLogORM.idempotency_key == idem_key,
            ToolAuditLogORM.status == "running",
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _load_idempotency_record(
        self, session: Any, tenant_id: str, key: str
    ) -> IdempotencyRecordORM | None:
        stmt = select(IdempotencyRecordORM).where(
            IdempotencyRecordORM.tenant_id == tenant_id,
            IdempotencyRecordORM.idempotency_key == key,
            IdempotencyRecordORM.expires_at >= dt.datetime.now(dt.timezone.utc),
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _append_tool_conversation_message(
        self,
        run_ctx: AgentRunContext,
        data: dict[str, Any],
        tool_name: str,
    ) -> None:
        metadata: dict[str, Any] = {}
        for k in ("ticket_no", "order_id", "refund_amount_cents", "new_status"):
            if k in data:
                metadata[k] = data[k]
        repo = ConversationRepository(run_ctx.session)
        await repo.append_message(
            run_ctx.service_actor,
            run_ctx.tenant_id,
            run_ctx.thread_id,
            ConversationMessageCreate(
                role="tool",
                content=json.dumps(data, ensure_ascii=False, default=str),
                tool_name=tool_name,
                tool_call_id=f"call-{uuid4().hex}",
                metadata=metadata,
            ),
        )

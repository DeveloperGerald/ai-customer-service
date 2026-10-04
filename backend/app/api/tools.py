"""工具调用 HTTP API：
- GET  /api/tools       从 ALL_TOOLS 原生工具对象生成白名单定义
- POST /api/tools/call  读工具直调（governance 审计 + 参数校验 + 身份注入）

写工具（refund/exchange/repair/cancel）不支持 HTTP 直调：
按 AGENTS.md 约束，写操作必须经对话流程页面点击确认（HITL interrupt）后执行。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Union, get_args, get_origin
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Request
from langchain.agents.middleware.types import ToolCallRequest as GovernanceToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agent.context import AgentRunContext
from app.application.agent.governance import ToolGovernanceMiddleware
from app.application.auth import require_actor
from app.application.schemas.identity import Role
from app.application.schemas.tools import (
    ToolCallError,
    ToolCallRequest,
    ToolDefinition,
    ToolParamSchema,
    ToolResult,
)
from app.application.tools.builtin import ALL_TOOLS
from app.core.errors import ErrorCode
from app.domain.repositories.identity import Actor
from app.domain.repositories.policy import PolicyConfigRepository
from app.infrastructure.db.engine import scoped_db_session

router = APIRouter(prefix="/api/tools", tags=["tools"])

_TOOL_BY_NAME = {t.name: t for t in ALL_TOOLS}
# 与 governance._WRITE_TOOLS 保持一致（写工具一律拒绝 HTTP 直调）
_WRITE_TOOL_NAMES = {"refund_request", "exchange_request", "repair_request", "cancel_order"}

_JSON_TYPE_MAP: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}


async def _db_session(request: Request) -> AsyncSession:
    async with scoped_db_session(request.app.state.bundle.infra) as sess:
        yield sess


def _unwrap_optional(annotation: Any) -> Any:
    """Optional[X] → X；用于参数类型展示。"""
    if get_origin(annotation) is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _param_schema(name: str, field: Any) -> ToolParamSchema:
    annotation = _unwrap_optional(field.annotation)
    origin = get_origin(annotation)
    json_type = _JSON_TYPE_MAP.get(annotation) or _JSON_TYPE_MAP.get(origin) or "string"
    enum: list[str] | None = None
    extra = field.json_schema_extra
    if isinstance(extra, dict) and isinstance(extra.get("enum"), list):
        enum = [str(e) for e in extra["enum"]]
    elif origin is not None:
        literal_args = [a for a in get_args(annotation) if isinstance(a, str)]
        if literal_args:
            enum = literal_args
    return ToolParamSchema(
        name=name,
        type=json_type,  # type: ignore[arg-type]
        description=field.description or "",
        required=field.is_required(),
        enum=enum,
    )


def _tool_definition(tool: Any) -> ToolDefinition:
    schema_cls = tool.get_input_schema()
    params = [_param_schema(name, f) for name, f in schema_cls.model_fields.items()]
    return ToolDefinition(
        name=tool.name,
        description=tool.description or "",
        category="write" if tool.name in _WRITE_TOOL_NAMES else "read",
        params=sorted(params, key=lambda p: (not p.required, p.name)),
    )


def _failed_result(tool_name: str, call_id: UUID, code: ErrorCode, message: str) -> ToolResult:
    now = _utcnow()
    return ToolResult(
        tool_name=tool_name,
        call_id=call_id,
        success=False,
        error=ToolCallError(code=code.value, message=message),
        started_at=now,
        finished_at=now,
    )


def _utcnow() -> Any:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc)


@router.get("", response_model=list[ToolDefinition])
async def list_tools(actor: Actor = Depends(require_actor)) -> list[ToolDefinition]:
    """查询当前系统开放的工具列表（含参数 schema 与读写分类）。"""
    return [_tool_definition(t) for t in ALL_TOOLS]


@router.post("/call", response_model=ToolResult)
async def call_tool(
    body: ToolCallRequest,
    actor: Actor = Depends(require_actor),
    session: AsyncSession = Depends(_db_session),
) -> ToolResult:
    """直调一个读工具（审计落 tool_audit_logs，身份字段强制用 Actor 覆盖）。

    - 未注册/不在白名单 → TOOL_NOT_FOUND / TOOL_NOT_ALLOWLISTED
    - 写工具 → TOOL_NOT_ALLOWLISTED（写操作必须走对话流程页面确认）
    - 参数非法 → TOOL_ARG_VALIDATION_FAILED
    - 执行失败 → success=false + error（由 governance 审计 failed）
    """
    call_id = uuid4()
    tool = _TOOL_BY_NAME.get(body.tool_name)
    if tool is None:
        return _failed_result(body.tool_name, call_id, ErrorCode.TOOL_NOT_FOUND, "工具不存在")
    if tool.name in _WRITE_TOOL_NAMES:
        return _failed_result(
            body.tool_name,
            call_id,
            ErrorCode.TOOL_NOT_ALLOWLISTED,
            "写操作必须通过对话流程并经页面确认执行，不支持直调",
        )

    try:
        validated = tool.args_schema.model_validate(body.arguments)
    except ValidationError as exc:
        return _failed_result(
            body.tool_name,
            call_id,
            ErrorCode.TOOL_ARG_VALIDATION_FAILED,
            f"参数校验失败：{exc.error_count()} 处错误，首个：{exc.errors()[0]['msg']}",
        )
    kwargs = validated.model_dump()

    tenant_id = actor.tenant_id
    thread_id = body.session_id or f"{tenant_id}:tools-api-{uuid4().hex[:8]}"
    agent_ctx = AgentRunContext(
        actor=actor,
        tenant_id=tenant_id,
        thread_id=thread_id,
        service_actor=Actor(actor_id=str(actor.actor_id), tenant_id=tenant_id, role=Role.STAFF),
        effective_policy=await PolicyConfigRepository(session).get_effective_policy(tenant_id),
        idempotency_salt="",
        session=session,
        conversation_repo=None,
    )
    tool_runtime = ToolRuntime(
        state={},
        tool_call_id=str(call_id),
        config={},
        context=agent_ctx,
        store=None,
        stream_writer=lambda *_: None,
    )
    gov_request = GovernanceToolCallRequest(
        tool_call={"name": tool.name, "args": body.arguments, "id": str(call_id), "type": "tool_call"},
        tool=tool,
        state={},
        runtime=SimpleNamespace(context=agent_ctx),
    )

    async def handler(req: GovernanceToolCallRequest) -> ToolMessage:
        # 直调绕过 ToolNode（其空 args_schema 分支会丢弃注入参数），手工注入 runtime
        out = await tool.coroutine(**kwargs, runtime=tool_runtime)
        return ToolMessage(
            content=json.dumps(out, ensure_ascii=False, default=str),
            name=tool.name,
            tool_call_id=req.tool_call["id"],
        )

    out = await ToolGovernanceMiddleware().awrap_tool_call(gov_request, handler)
    if isinstance(out, Command):
        executions = list(out.update.get("tool_executions") or [])
        if executions:
            return ToolResult.model_validate(executions[0])
    return _failed_result(
        body.tool_name,
        call_id,
        ErrorCode.TOOL_EXECUTION_ERROR,
        "工具执行未产生结果",
    )

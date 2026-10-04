from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, ClassVar, TypeVar

from fastapi import Depends, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.application.schemas.identity import (
    DemoTokenClaims,
    Role,
    parse_bearer_token,
    verify_demo_token,
)
from app.config import Settings
from app.core.errors import AuthError, ErrorCode
from app.core.logging import TENANT_ID_CONTEXT, get_logger
from app.domain.repositories.identity import Actor

T = TypeVar("T")

REQUEST_ACTOR_CONTEXT: ContextVar[Actor | None] = ContextVar("request_actor", default=None)
REQUEST_CLAIMS_CONTEXT: ContextVar[DemoTokenClaims | None] = ContextVar("request_claims", default=None)

_log = get_logger("app.auth")


class ActorMiddleware(BaseHTTPMiddleware):
    """对除 /health, /docs, /openapi.json 以外的接口强制鉴权。

    流程：
    1. 跳过白名单路径（/health /docs /openapi.json /redoc /favicon.ico）
    2. 从 Authorization 解析 token → 得到 claims
    3. 校验 Header 中的 X-Tenant-Id 与 claims.tenant_id **完全一致**，否则 401
    4. 把 Actor / Claims 写进 ContextVar，业务层通过依赖注入读取
    """

    WHITE_LIST: ClassVar[frozenset[str]] = frozenset(
        {"/health", "/docs", "/openapi.json", "/redoc", "/favicon.ico"}
    )

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path in self.WHITE_LIST or request.url.path.startswith("/static/"):
            return await call_next(request)

        tenant_header = self._settings.security.tenant_id_header
        raw_tenant = request.headers.get(tenant_header, "").strip().lower()
        auth_header = request.headers.get(self._settings.security.auth_header, "").strip()
        try:
            if not raw_tenant:
                raise AuthError(
                    ErrorCode.AUTH_TENANT_MISMATCH,
                    message=f"缺少请求头 {tenant_header}。",
                )
            token = parse_bearer_token(auth_header)
            claims = verify_demo_token(self._settings.security, token)
            if claims.tenant_id.lower() != raw_tenant:
                raise AuthError(
                    ErrorCode.AUTH_TENANT_MISMATCH,
                    message="请求头租户与令牌租户不一致。",
                    details={
                        "header_tenant": raw_tenant,
                        "token_tenant": claims.tenant_id,
                    },
                )
            actor = Actor(
                actor_id=claims.actor_id,
                tenant_id=claims.tenant_id,
                role=Role(claims.role),
            )
        except AuthError as exc:
            _log.warning("auth.rejected", path=request.url.path, reason=exc.code.value)
            body = {
                "code": exc.code.value,
                "message": exc.message,
                "request_id": request.headers.get(self._settings.security.request_id_header),
                "details": exc.details,
            }
            return JSONResponse(status_code=exc.http_status, content=body)

        TENANT_ID_CONTEXT.set(actor.tenant_id)
        REQUEST_ACTOR_CONTEXT.set(actor)
        REQUEST_CLAIMS_CONTEXT.set(claims)
        return await call_next(request)


# ---------- FastAPI 依赖注入 ----------


def _get_from_context(var: ContextVar[T | None]) -> T:
    value = var.get()
    if value is None:
        raise AuthError(ErrorCode.AUTH_MISSING, message="未检测到请求身份上下文。")
    return value


def require_actor(_: Request = ...) -> Actor:
    """FastAPI Depends：要求当前请求已通过 ActorMiddleware。"""
    return _get_from_context(REQUEST_ACTOR_CONTEXT)


def require_claims(_: Request = ...) -> DemoTokenClaims:
    """返回完整 DemoTokenClaims，适用于需要 jti/exp 等细粒度字段的场景。"""
    return _get_from_context(REQUEST_CLAIMS_CONTEXT)


# 类型别名：供 Depends(...) 使用
RequireActor: Any = Depends(require_actor)
RequireClaims: Any = Depends(require_claims)

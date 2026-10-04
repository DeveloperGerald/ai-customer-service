from __future__ import annotations

import logging
import math
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config import LogLevel, Settings

REQUEST_ID_CONTEXT: ContextVar[str] = ContextVar("request_id", default="")
TRACE_ID_CONTEXT: ContextVar[str] = ContextVar("trace_id", default="")
TENANT_ID_CONTEXT: ContextVar[str] = ContextVar("tenant_id", default="")
SESSION_ID_CONTEXT: ContextVar[str] = ContextVar("session_id", default="")

_SENSITIVE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "***jwt***"),
    (re.compile(r"\b(?:1[3-9]\d{9}|(?:(?:\+?86)?1[3-9]\d{9}))\b"), "***phone***"),
    (re.compile(r"((?:省|市|区|县|路|街|巷|号|层|栋|室)[^,，\n]{0,30}){2,}"), "***addr***"),
    (
        re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd)([\"':=\s]+)([^\s,\"'&]{2,})"),
        r"\1\2***",
    ),
)


def _sanitize_message(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog Processor：递归脱敏敏感字段。

    对 event_dict 中的所有字符串值应用正则替换；
    对 dict / list 递归遍历，避免嵌套值漏脱敏。
    """

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            for pattern, repl in _SENSITIVE_PATTERNS:
                value = pattern.sub(repl, value)
            return value
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_walk(item) for item in value]
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return str(value)
        return value

    return {k: _walk(v) for k, v in event_dict.items()}


def _bind_context_vars(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog Processor：把 ContextVar 当前值绑定到事件字典。"""
    if rid := REQUEST_ID_CONTEXT.get():
        event_dict.setdefault("request_id", rid)
    if tid := TRACE_ID_CONTEXT.get():
        event_dict.setdefault("trace_id", tid)
    if t := TENANT_ID_CONTEXT.get():
        event_dict.setdefault("tenant_id", t)
    if s := SESSION_ID_CONTEXT.get():
        event_dict.setdefault("session_id", s)
    return event_dict


def _add_logger_name_safe(logger: Any, _: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """兼容 PrintLogger（无 name 属性）与 stdlib Logger 的 logger_name 处理器。

    structlog.stdlib.add_logger_name 要求底层 logger 有 `.name` 属性，
    但 PrintLoggerFactory 返回的 PrintLogger 不提供，直接使用会抛 AttributeError，
    进而导致所有 HTTP 请求在日志阶段就 500。
    """
    try:
        event_dict.setdefault("logger", logger.name)
    except AttributeError:
        pass
    return event_dict


def configure_logging(settings: Settings) -> None:
    """按配置初始化全局日志输出。

    - LOCAL/TEST 可选人类可读；其余环境强制 JSON 输出。
    - 所有事件都会绑定上下文变量并脱敏。
    - 根 logging 走 structlog，避免第三方日志漏脱敏。
    """
    force_json = settings.json_log or settings.app_env not in {"local", "test"}

    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        _bind_context_vars,
        structlog.stdlib.add_log_level,
        _add_logger_name_safe,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        _sanitize_message,
    ]
    if force_json:
        processors.append(structlog.processors.format_exc_info)
        processors.append(structlog.processors.JSONRenderer(serializer=_json_dumps))
    else:
        processors.append(
            structlog.dev.ConsoleRenderer(
                colors=sys.stdout.isatty(),
                exception_formatter=structlog.dev.plain_traceback,
            )
        )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level.value)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    _redirect_stdlib_logging(force_json, settings.log_level)


def _json_dumps(obj: Any, **kwargs: Any) -> str:
    """优先使用 orjson，回退 stdlib json。"""
    try:
        import orjson

        return orjson.dumps(obj).decode()
    except Exception:  # pragma: no cover - 后备路径
        import json

        return json.dumps(obj, ensure_ascii=False, default=str)


def _redirect_stdlib_logging(force_json: bool, level: LogLevel) -> None:
    """让标准库 logging 走同一套 structlog processor。"""
    level_int = logging.getLevelName(level.value)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level_int)

    formatter = None
    if force_json:
        from pythonjsonlogger import jsonlogger

        formatter = jsonlogger.JsonFormatter(
            "%(timestamp)s %(level)s %(name)s %(message)s",
            rename_fields={"levelname": "level", "asctime": "timestamp"},
        )
    handler = logging.StreamHandler(sys.stdout)
    if formatter:
        handler.setFormatter(formatter)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "urllib3"):
        logging.getLogger(noisy).setLevel(max(level_int, logging.WARNING))


def generate_request_id() -> str:
    """生成请求 ID，格式 `req_{uuid 紧凑}`。"""
    return f"req_{uuid.uuid4().hex}"


def generate_trace_id() -> str:
    """生成追踪 ID，格式 `trace_{uuid 紧凑}`。"""
    return f"trace_{uuid.uuid4().hex}"


def get_logger(name: str, **bindings: Any) -> structlog.stdlib.BoundLogger:
    """创建带绑定字段的 structlog logger 快捷函数。"""
    return structlog.get_logger(name).bind(**bindings)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """在每个请求进入时生成或继承 request_id/trace_id，写入 ContextVar。

    响应头会回写 `X-Request-Id` 与 `X-Trace-Id`，供客户端追踪。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        request_id_header: str,
        trace_id_header: str = "X-Trace-Id",
    ) -> None:
        super().__init__(app)
        self.request_id_header: str = request_id_header
        self.trace_id_header: str = trace_id_header

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.perf_counter()
        request_id = request.headers.get(self.request_id_header) or generate_request_id()
        trace_id = request.headers.get(self.trace_id_header) or generate_trace_id()
        tenant_id = request.headers.get(TENANT_ID_CONTEXT.get.__name__[:-5], "")

        REQUEST_ID_CONTEXT.set(request_id)
        TRACE_ID_CONTEXT.set(trace_id)
        if tenant_id:
            TENANT_ID_CONTEXT.set(tenant_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, trace_id=trace_id, tenant_id=tenant_id or None
        )

        log = get_logger("app.http")
        try:
            response: Response = await call_next(request)
            duration_ms = (time.perf_counter() - start) * 1000.0
            log.info(
                "http.request.done",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )
        except Exception:
            duration_ms = (time.perf_counter() - start) * 1000.0
            log.exception(
                "http.request.failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round(duration_ms, 2),
            )
            raise
        finally:
            pass

        response.headers[self.request_id_header] = request_id
        response.headers[self.trace_id_header] = trace_id
        return response

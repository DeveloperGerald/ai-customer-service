from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import AppEnv, Settings, load_settings
from app.core.errors import (
    AppBaseError,
    AuthError,
    ConfigError,
    ErrorCode,
    ErrorDisplay,
    PermissionDeniedError,
)
from app.core.infrastructure import InfrastructureBundle
from app.core.logging import (
    REQUEST_ID_CONTEXT,
    RequestContextMiddleware,
    configure_logging,
    get_logger,
)


class AppState:
    """FastAPI app.state 的类型化容器，避免到处使用 Any 字符串索引。"""

    def __init__(self, settings: Settings, infra: InfrastructureBundle) -> None:
        self.settings: Settings = settings
        self.infra: InfrastructureBundle = infra
        # 知识向量库（lifespan 内初始化；单测/降级场景为 None）
        self.vector_store: Any | None = None


def _state_from_app(app: FastAPI) -> AppState:
    """以类型安全的方式从 FastAPI app.state 取出 AppState。"""
    state: AppState = app.state.bundle
    return state


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI 生命周期：启动前加载配置、初始化长生命周期依赖；关闭时释放。"""
    log = get_logger("app.lifespan")
    settings: Settings
    try:
        settings = load_settings()
    except (ValidationError, ConfigError) as exc:
        # 配置错误属于启动期致命错误。使用 rich 形式输出，便于本地排查。
        raise SystemExit(f"[FATAL] 配置加载失败：{exc}") from exc

    configure_logging(settings)
    _configure_langsmith(settings, log)

    bundle = InfrastructureBundle(settings)
    try:
        await bundle.start()
    except Exception as exc:
        log.fatal("infra.start.failed", error=str(exc))
        raise SystemExit(f"[FATAL] 基础设施启动失败：{exc}") from exc

    app.state.bundle = AppState(settings, bundle)

    # ---- 人在回路 checkpointer：Redis（redis-stack-server，TTL 10min）----
    # 必须在 _wire_production_dependencies 之前装配：facade 构图时 get_checkpointer() 会取此单例。
    # Redis 是项目硬依赖（AGENTS.md），装配失败直接抛异常阻止启动。
    from app.infrastructure.agent.checkpoint import (
        configure_checkpointer,
        setup_checkpointer,
    )

    redis_url = (
        settings.redis.url.get_secret_value() if settings.redis.url else None
    )
    configure_checkpointer(redis_url)
    await setup_checkpointer()

    # 单测场景下依赖注入失败可降级（允许跳过），其他环境必须报错阻止启动
    try:
        await _wire_production_dependencies(app, settings, bundle, log)
    except Exception as exc:
        if settings.app_env == AppEnv.TEST:
            _ = exc
            log.exception("facade.wire.skipped")
        else:
            log.fatal("facade.wire.failed", error=str(exc))
            raise SystemExit(f"[FATAL] 生产依赖注入失败：{exc}") from exc

    log.info(
        "app.started",
        app_env=settings.app_env.value,
        app_name=settings.app_name,
        tracing=settings.langsmith.tracing_enabled,
        llm_provider=settings.llm.provider.value,
    )
    try:
        yield
    finally:
        try:
            await bundle.stop()
        except Exception as exc:
            _ = exc
            log.exception("app.shutdown.infra_error")
        log.info("app.stopped")


async def _wire_production_dependencies(
    app: FastAPI, settings: Settings, bundle: Any, log: Any
) -> None:
    """按配置懒构建真实的 CustomerServiceAgentFacade 单例。

    真实 vs MVP 切换矩阵：
    ┌──────────────┬────────────────────────────────┬─────────────────────────────┐
    │ 组件         │  真接入（下面）                 │  MVP（agent.py 懒加载）     │
    ├──────────────┼────────────────────────────────┼─────────────────────────────┤
    │ tool_registry│  OrderQueryTool 真读 Postgres  │  同样（但会走真 DB 读订单）  │
    ├──────────────┼────────────────────────────────┼─────────────────────────────┤
    │ retriever    │  PgVectorStoreRetriever +      │  None（FAQ 只走模板不检索） │
    │              │  KnowledgeVectorStore          │                             │
    ├──────────────┼────────────────────────────────┼─────────────────────────────┤
    │ classifier   │  LLMIntentClassifier           │  None（节点兜底 unknown）    │
    ├──────────────┼────────────────────────────────┼─────────────────────────────┤
    │ chat_model   │  OpenAI 兼容协议 / Mock 兜底   │  None（节点内部走模板）     │
    └──────────────┴────────────────────────────────┴─────────────────────────────┘

    单测环境（app_env=TEST）下各组件初始化失败可降级；
    其他环境（local/staging/prod）下任何组件异常都会 raise，导致服务启动失败。

    生产环境复用 bundle.db_engine 构建 KnowledgeVectorStore（复用连接池，检索强隔离）。
    """
    is_test_env = settings.app_env == AppEnv.TEST

    try:
        from app.application.agent.facade import CustomerServiceAgentFacade
        from app.infrastructure.llm.classifiers import (
            LLMIntentClassifier,
        )
        from app.infrastructure.llm.providers import (
            build_chat_model,
            build_embeddings,
        )
    except Exception as exc:
        if is_test_env:
            _ = exc
            log.exception("facade.wire.import_failed")
            return
        raise

    # ---- Embeddings：未配置 API Key 时直接抛错，应用启动失败 ----
    embeddings = build_embeddings(settings.llm)

    # ---- Chat Model（BaseChatModel）：未配置 API Key 时直接抛错，应用启动失败 ----
    chat_model = build_chat_model(settings.llm)

    # ---- KnowledgeVectorStore + Retriever：单测失败降级为 None，其他环境直接抛错 ----
    vector_store: Any | None = None
    retriever: Any | None = None
    try:
        from app.infrastructure.vectorstore import (
            KnowledgeVectorStore,
            PgVectorStoreRetriever,
        )

        if bundle.db_engine is None:
            raise RuntimeError("db engine 未初始化，无法构建知识向量库")
        vector_store = KnowledgeVectorStore(
            bundle.db_engine,
            embeddings,
            vector_size=settings.llm.embedding_dim,
        )
        await vector_store.initialize()
        retriever = PgVectorStoreRetriever(vector_store)
        app.state.bundle.vector_store = vector_store
    except Exception as exc:
        if is_test_env:
            _ = exc
            log.exception("facade.wire.vector_store_init_failed")
            vector_store = None
            retriever = None
        else:
            log.fatal("facade.wire.vector_store_init_failed", error=str(exc))
            raise

    # ---- Classifier：使用 LLMIntentClassifier；失败降级为 None（节点兜底 unknown）----
    classifier = None
    try:
        classifier = LLMIntentClassifier(
            chat_model=chat_model,
            timeout_seconds=8.0,
        )
        log.info("facade.wire.classifier_llm_enabled")
    except Exception as exc:
        if is_test_env:
            _ = exc
            log.exception("facade.wire.classifier_llm_init_failed")
            classifier = None
        else:
            log.fatal("facade.wire.classifier_init_failed", error=str(exc))
            raise

    facade = CustomerServiceAgentFacade(
        retriever=retriever,
        classifier=classifier,
        chat_model=chat_model,
        rag_top_k=settings.agent.rag_top_k,
        rag_similarity_threshold=settings.agent.rag_similarity_threshold,
    )
    app.state.agent_facade_singleton = facade
    log.info(
        "facade.wired",
        llm_provider=settings.llm.provider.value,
        embedding_provider=settings.llm.embedding_provider.value,
        chat_model=settings.llm.chat_model,
        retriever_enabled=retriever is not None,
        classifier_mode="llm" if classifier is not None else "none",
    )


def _configure_langsmith(settings: Settings, log: Any) -> None:
    """按配置有条件启用 LangSmith tracing；未配置则显式降级，避免 LangChain 警告。"""
    import os

    enabled = settings.langsmith.tracing_enabled and (
        settings.langsmith.api_key is not None
        and settings.langsmith.api_key.get_secret_value().strip() != ""
    )
    if enabled:
        # LangSmith >=0.3 只认 LANGCHAIN_* 前缀（原 LANGSMITH_* 已弃用）
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGCHAIN_ENDPOINT", settings.langsmith.endpoint)
        os.environ.setdefault("LANGCHAIN_PROJECT", settings.langsmith.project)
        key = settings.langsmith.api_key.get_secret_value() if settings.langsmith.api_key else ""
        os.environ.setdefault("LANGCHAIN_API_KEY", key)
        log.info(
            "langsmith.enabled",
            project=settings.langsmith.project,
            endpoint=settings.langsmith.endpoint,
        )
    else:
        for _k in (
            "LANGCHAIN_TRACING_V2",
            "LANGCHAIN_API_KEY",
            "LANGCHAIN_ENDPOINT",
            "LANGCHAIN_PROJECT",
            "LANGSMITH_TRACING",
            "LANGSMITH_API_KEY",
        ):
            os.environ.pop(_k, None)
        log.info("langsmith.disabled", reason="api_key 未配置或 tracing_enabled=false")


def _build_error_response(
    exc: Exception,
    *,
    default_code: ErrorCode,
    default_status: int,
    app_env: str,
    message: str | None = None,
    details: dict[str, Any] | None = None,
) -> tuple[ErrorDisplay, int]:
    """统一把各种异常转换成 ErrorDisplay + HTTP 状态码。"""
    code = default_code
    msg = message or str(exc)
    stack_trace: str | None = None
    final_details = details

    if isinstance(exc, AppBaseError):
        code = exc.code
        msg = exc.message
        final_details = exc.details or final_details
        status = exc.http_status
    else:
        status = default_status
        if app_env in {"local", "test"}:
            import traceback

            stack_trace = traceback.format_exc()
    err = ErrorDisplay(
        code=code,
        message=msg,
        request_id=REQUEST_ID_CONTEXT.get() or None,
        trace_id=None,
        details=final_details,
        stack_trace=stack_trace,
    )
    return err, status


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。集中注册中间件、异常处理器和路由。"""
    app = FastAPI(
        title="手串售后智能客服 Agent API",
        description="面试演示型多租户手串售后智能客服。所有接口要求 X-Tenant-Id + 演示令牌。",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    # ----- 中间件（顺序重要：外层先执行）-----
    settings_probe: Settings | None = None
    try:
        settings_probe = load_settings()
    except Exception:
        pass
    cors_origins = settings_probe.cors_origins if settings_probe else []
    req_header = settings_probe.security.request_id_header if settings_probe else "X-Request-Id"
    tenant_header = settings_probe.security.tenant_id_header if settings_probe else "X-Tenant-Id"

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*", req_header, tenant_header],
        expose_headers=[req_header, "X-Trace-Id"],
    )
    app.add_middleware(RequestContextMiddleware, request_id_header=req_header)
    if settings_probe is not None:
        from app.application.auth import ActorMiddleware

        app.add_middleware(ActorMiddleware, settings=settings_probe)

    # ----- 路由（基础 + 占位）-----
    from app.api.agent import router as agent_router
    from app.api.conversations import router as conversations_router
    from app.api.evaluations import router as evaluations_router
    from app.api.health import router as health_router
    from app.api.knowledge import router as knowledge_router
    from app.api.management_knowledge import router as mgmt_knowledge_router
    from app.api.management_policy import router as mgmt_policy_router
    from app.api.orders import router as orders_router
    from app.api.products import router as products_router
    from app.api.tools import router as tools_router

    app.include_router(health_router)
    app.include_router(mgmt_policy_router)
    app.include_router(mgmt_knowledge_router)
    app.include_router(orders_router)
    app.include_router(products_router)
    app.include_router(tools_router)
    app.include_router(evaluations_router)
    app.include_router(knowledge_router)
    app.include_router(conversations_router)
    app.include_router(agent_router)

    # ----- 异常处理器 -----
    @app.exception_handler(AppBaseError)
    async def handle_app_error(_: Request, exc: AppBaseError) -> JSONResponse:
        env = "local"
        try:
            env = _state_from_app(app).settings.app_env.value
        except Exception:
            pass
        body, status = _build_error_response(
            exc, default_code=ErrorCode.INTERNAL_ERROR, default_status=500, app_env=env
        )
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        env = "local"
        try:
            env = _state_from_app(app).settings.app_env.value
        except Exception:
            pass
        details = {"errors": exc.errors()}
        body, status = _build_error_response(
            exc,
            default_code=ErrorCode.VALIDATION_ERROR,
            default_status=422,
            app_env=env,
            message="请求参数不合法",
            details=details,
        )
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    @app.exception_handler(ValidationError)
    async def handle_pydantic_validation_error(_: Request, exc: ValidationError) -> JSONResponse:
        env = "local"
        try:
            env = _state_from_app(app).settings.app_env.value
        except Exception:
            pass
        body, status = _build_error_response(
            exc,
            default_code=ErrorCode.VALIDATION_ERROR,
            default_status=500,
            app_env=env,
            message="内部校验失败",
            details={"errors": exc.errors()},
        )
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    @app.exception_handler(AuthError)
    async def handle_auth_error(_: Request, exc: AuthError) -> JSONResponse:
        env = "local"
        try:
            env = _state_from_app(app).settings.app_env.value
        except Exception:
            pass
        body, status = _build_error_response(
            exc, default_code=exc.code, default_status=401, app_env=env
        )
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    @app.exception_handler(PermissionDeniedError)
    async def handle_permission_error(_: Request, exc: PermissionDeniedError) -> JSONResponse:
        env = "local"
        try:
            env = _state_from_app(app).settings.app_env.value
        except Exception:
            pass
        body, status = _build_error_response(
            exc, default_code=ErrorCode.PERMISSION_DENIED, default_status=403, app_env=env
        )
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        env = "local"
        try:
            env = _state_from_app(app).settings.app_env.value
        except Exception:
            pass
        mapping: dict[int, ErrorCode] = {
            401: ErrorCode.AUTH_MISSING,
            403: ErrorCode.PERMISSION_DENIED,
            404: ErrorCode.RESOURCE_NOT_FOUND,
            405: ErrorCode.VALIDATION_ERROR,
            409: ErrorCode.CONFLICT_PAYLOAD,
        }
        body, status = _build_error_response(
            exc,
            default_code=mapping.get(exc.status_code, ErrorCode.INTERNAL_ERROR),
            default_status=exc.status_code,
            app_env=env,
            message=str(exc.detail)
            if isinstance(exc.detail, str)
            else ErrorCode.INTERNAL_ERROR.value,
        )
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        env = "local"
        try:
            env = _state_from_app(app).settings.app_env.value
        except Exception:
            pass
        get_logger("app.http").exception("unhandled_exception", error_type=type(exc).__name__)
        body, status = _build_error_response(
            exc,
            default_code=ErrorCode.INTERNAL_ERROR,
            default_status=500,
            app_env=env,
            message="内部错误，请稍后重试或联系管理员",
        )
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"))

    return app


app = create_app()

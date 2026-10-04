from __future__ import annotations

# Python 3.9 下 `typing.get_type_hints()` 无法解析 PEP 604 的 `X | None` 字符串化注解，
# 而 LangGraph 在构建 StateGraph 时会调用标准库 get_type_hints(AgentState)。
# eval_type_backport.install_patch() 会 monkey patch 标准库 typing 相关内部函数，
# 让它在 3.9 环境下也能正确处理 PEP 604 / `list[...]` / `dict[...]` 等新式语法注解求值。
# 注：项目正式生产环境按 AGENTS.md 要求运行在 Python 3.14+，此 patch 仅沙盒测试兜底。
import sys as _sys

if _sys.version_info < (3, 10):  # pragma: no cover - 仅 3.9 分支触发
    try:
        from eval_type_backport import install_patch as _install_backport_patch

        _install_backport_patch()
    except Exception:  # noqa: BLE001
        pass

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.config import (
    AgentSettings,
    AppEnv,
    DatabaseSettings,
    EmbeddingProvider,
    LangSmithSettings,
    LLMProvider,
    LLMSettings,
    LogLevel,
    RedisSettings,
    SecuritySettings,
    Settings,
)
from app.core.infrastructure import InfrastructureBundle
from app.core.logging import configure_logging
from app.main import AppState


# ---------- 测试配置：强制 TEST 环境，不读真实 .env ----------
def pytest_configure() -> None:
    """测试启动前把所有「真 env/.env」能覆盖的关键字段强制覆盖为测试值。

    必须在 pytest_configure（任何 test file / fixture import 之前）执行，原因：
      1) Pydantic v2 SettingsConfigDict.env_file 会在「Settings 实例化时」立即读取文件，
         而很多测试会「在 import 阶段」走 `from app.config import load_settings`，
         导致 conftest test_settings fixture 根本还没被调用，settings 就被 backend/.env
         / 仓库根 .env 里填好的 SECURITY__DEMO_TOKEN_SECRET / LANGSMITH__TRACING_ENABLED
         / DB__URL 污染了。
      2) backend/.env vs 仓库根 .env 两边的值只要有一处不一致，后续「测试代码用
         fixture settings.security 签 token → ActorMiddleware 用 env 里的另一套
         security 验签」 → 100% 401 AUTH_TOKEN_INVALID。
      3) LANGSMITH__* 被覆盖为 false/空 → pytest 绝对不会联网发 trace（零副作用）。
    """
    os.environ["APP_ENV"] = "test"
    os.environ["JSON_LOG"] = "false"

    for _k, _v in (
        ("SECURITY__DEMO_TOKEN_SECRET", "test-secret-at-least-32-characters-long-123"),
        ("SECURITY__DEMO_TOKEN_TTL_SECONDS", "3600"),
        ("LANGSMITH__TRACING_ENABLED", "false"),
        ("LANGSMITH__API_KEY", ""),
        ("LANGSMITH__ENDPOINT", "https://api.smith.langchain.com"),
        ("LANGSMITH__PROJECT", "ai-customer-service-test"),
        ("DB__URL", "postgresql+psycopg://postgres:postgres@localhost:5432/ai_cs_test"),
        ("DB__POOL_SIZE", "2"),
        ("DB__MAX_OVERFLOW", "1"),
        ("DB__ECHO", "false"),
        ("REDIS__URL", "redis://localhost:6379/15"),
        ("REDIS__DECODE_RESPONSES", "false"),
        ("LLM__PROVIDER", "mock"),
        ("LLM__EMBEDDING_PROVIDER", "mock"),
        ("LLM__CHAT_TEMPERATURE", "0.0"),
        ("LLM__CHAT_MAX_TOKENS", "64"),
        ("AGENT__MAX_GRAPH_STEPS", "10"),
    ):
        os.environ[_k] = _v

    # LangChainTracer 直读原始 env（非 pydantic Settings）：测试必须清空，保证零联网
    for _raw in (
        "LANGSMITH_TRACING_V2",
        "LANGSMITH_TRACING",
        "LANGCHAIN_TRACING_V2",
        "LANGSMITH_API_KEY",
        "LANGCHAIN_API_KEY",
    ):
        os.environ.pop(_raw, None)


# ---------- Settings / 基础设施 fixtures ----------


@pytest.fixture(scope="session")
def event_loop_policy() -> Any:
    """pytest-asyncio 需要的显式 loop policy（Python 3.14 保留 uvloop 可选）。"""
    import sys

    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """返回无需外部真实 DB/Redis 也能加载的 Settings。

    通过环境变量覆盖 url 字段可指向真实测试实例；
    默认指向占位 URL，触发实际连接前会失败（非连接性测试不依赖真实连接）。
    """
    return Settings(
        app_env=AppEnv.TEST,
        app_name="ai-cs-test",
        log_level=LogLevel.WARNING,
        json_log=False,
        cors_origins=[],
        db=DatabaseSettings(
            url=SecretStr(
                os.environ.get(
                    "TEST_DB_URL",
                    "postgresql+psycopg://postgres:postgres@localhost:5432/ai_cs_test",
                )
            ),
            pool_size=2,
            max_overflow=1,
        ),
        redis=RedisSettings(
            url=SecretStr(os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")),
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        ),
        security=SecuritySettings(
            demo_token_secret=SecretStr("test-secret-at-least-32-characters-long-123"),
            demo_token_ttl_seconds=3600,
        ),
        llm=LLMSettings(
            provider=LLMProvider.OPENAI,
            embedding_provider=EmbeddingProvider.OPENAI,
            openai_api_key="test-fake-key",
        ),
        langsmith=LangSmithSettings(tracing_enabled=False),
        agent=AgentSettings(max_graph_steps=10),
    )


@pytest.fixture(scope="session")
def _configure_test_logging(test_settings: Settings) -> None:
    """会话级配置日志，避免每个测试重复配置。"""
    configure_logging(test_settings)


@pytest_asyncio.fixture(scope="function")
async def infra_bundle(test_settings: Settings) -> AsyncIterator[InfrastructureBundle]:
    """提供启动好的 InfrastructureBundle（连接尝试失败不阻塞，由各测试按需处理）。"""
    bundle = InfrastructureBundle(test_settings)
    try:
        await bundle.start()
    except Exception:
        bundle.db_engine = None
        bundle.db_session_factory = None
        bundle.redis = None
    yield bundle
    try:
        await bundle.stop()
    except Exception:
        pass


# ---------- FastAPI app + httpx client fixtures ----------


def _build_test_app(test_settings: Settings, infra_bundle: InfrastructureBundle) -> FastAPI:
    """构造带测试夹具 settings 的 FastAPI app。

    关键：create_app() 内部会再调一次 load_settings()（用于 CORS + ActorMiddleware 的密钥）。
    TR-7 引入双 env_file 后，backend/.env / 仓库根 .env 可能已经有真实的 SECURITY__DEMO_TOKEN_SECRET，
    导致 ActorMiddleware 用「真实 env 里的密钥」来 verify token，但外层测试代码用「test_settings」
    的密钥来 issue token → 签名不一致 → 所有 HTTP 测试 401 AUTH_TOKEN_INVALID。
    解决办法：在 Python 进程级把 ActorMiddleware 里用的 SecuritySettings 锁死为
    test_settings.security（通过 monkeypatch verify_demo_token 第一参数强转 + load_settings 替换），
    保证「签名用的密钥」=「验签用的密钥」100% 一致。
    """
    from _pytest.monkeypatch import MonkeyPatch

    from app import main as _main_mod
    from app import config as _config_mod
    from app.application import schemas as _schemas_mod  # import 包级名字，避免循环

    mp = MonkeyPatch()
    try:
        def _patched_load(*_a: Any, **_k: Any) -> Settings:
            return test_settings

        # 锁死 load_settings()：create_app 里 settings_probe / 任何路由里的 load_settings 都返回 fixture 值
        mp.setattr(_config_mod, "load_settings", _patched_load)
        mp.setattr(_main_mod, "load_settings", _patched_load)

        # ⚠️  最关键：ActorMiddleware 在 dispatch() 里会调用
        #     verify_demo_token(self._settings.security, token)
        #     self._settings.security 是 create_app() load_settings 出来的，即使我们 patch 了
        #     load_settings，某些测试里（_wire_app_with_session）又会在测试函数内部再调用
        #     create_app() 重新走一遍，如果这期间 pytest_configure 覆盖 env 和 backend/.env
        #     里实际 SECURITY__DEMO_TOKEN_SECRET 不一致，还是会失败。
        #     终极兜底：monkeypatch identity.verify_demo_token，忽略第一个 settings 参数，
        #     强制使用 test_settings.security 验签。
        from app.application.schemas import identity as _identity_mod

        _orig_verify = _identity_mod.verify_demo_token

        def _verify_with_fixture_sec(_ignored_settings: Any, token: str):
            return _orig_verify(test_settings.security, token)

        mp.setattr(_identity_mod, "verify_demo_token", _verify_with_fixture_sec)

        # 同样防 app.api.knowledge / app.api.health 里再 import verify_demo_token 引用
        # （模块导入缓存）：把 identity 模块公开到 schemas 包属性（如果原来有），实际 import 走 identity 子模块
        try:
            mp.setattr(_schemas_mod.identity, "verify_demo_token", _verify_with_fixture_sec)
        except Exception:
            pass

        from app.main import create_app

        app = create_app()
        app.state.bundle = AppState(test_settings, infra_bundle)
    finally:
        mp.undo()
    return app


@pytest.fixture(scope="function")
def test_app(
    test_settings: Settings,
    infra_bundle: InfrastructureBundle,
    _configure_test_logging: None,
) -> FastAPI:
    """返回已注入 settings + infra 的 FastAPI 实例（未真正 run lifespan）。"""
    return _build_test_app(test_settings, infra_bundle)


@pytest_asyncio.fixture
async def client(test_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """基于 httpx.AsyncClient 的 ASGI 传输层测试客户端。"""
    transport = ASGITransport(app=test_app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------- 轻量辅助 ----------


@pytest.fixture
def sample_demo_token_secret(test_settings: Settings) -> str:
    return test_settings.security.demo_token_secret.get_secret_value()


def anyio_backend() -> Iterator[str]:
    yield "asyncio"

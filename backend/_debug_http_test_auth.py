"""排查 HTTP 测试为什么 AUTH_TOKEN_INVALID / AUTH_TENANT_MISMATCH 9 fail。"""
import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 模拟 conftest.py pytest_configure
os.environ["APP_ENV"] = "test"
os.environ["JSON_LOG"] = "false"
for _k, _v in (
    ("SECURITY__DEMO_TOKEN_SECRET", "test-secret-at-least-32-characters-long-123"),
    ("LANGSMITH__TRACING_ENABLED", "false"),
    ("LANGSMITH__API_KEY", ""),
    ("DB__URL", "postgresql+psycopg://postgres:postgres@localhost:5432/ai_cs_test"),
    ("REDIS__URL", "redis://localhost:6379/15"),
):
    os.environ[_k] = _v

import pytest
from httpx import ASGITransport, AsyncClient
from app.main import create_app, AppState
from app.config import (
    Settings, AppEnv, LogLevel, DatabaseSettings, RedisSettings,
    SecuritySettings, LLMSettings, LangSmithSettings, AgentSettings,
    EmbeddingProvider,
)
from pydantic import SecretStr
from app.core.infrastructure import InfrastructureBundle
from app.core.logging import configure_logging
from app.application.schemas.identity import issue_demo_token, Role
from app.application.auth import ActorMiddleware
from app.config import load_settings

# 1) 构造测试夹具 settings
fixture_settings = Settings(
    app_env=AppEnv.TEST,
    app_name="ai-cs-test",
    log_level=LogLevel.WARNING,
    json_log=False,
    cors_origins=[],
    db=DatabaseSettings(url=SecretStr(os.environ["DB__URL"]), pool_size=2, max_overflow=1),
    redis=RedisSettings(url=SecretStr(os.environ["REDIS__URL"]), socket_connect_timeout=0.5, socket_timeout=0.5),
    security=SecuritySettings(demo_token_secret=SecretStr(os.environ["SECURITY__DEMO_TOKEN_SECRET"]), demo_token_ttl_seconds=3600),
    llm=LLMSettings(provider=LLMSettings.model_fields["provider"].default, embedding_provider=EmbeddingProvider.MOCK),
    langsmith=LangSmithSettings(tracing_enabled=False),
    agent=AgentSettings(max_graph_steps=10),
)
print(f"[fixture_settings] demo_token_secret (len={len(fixture_settings.security.demo_token_secret.get_secret_value())}) last6={fixture_settings.security.demo_token_secret.get_secret_value()[-6:]}")

# 2) 模拟 conftest _build_test_app
app = create_app()
configure_logging(fixture_settings)
bundle = InfrastructureBundle(fixture_settings)
# 不启动 bundle，测试不依赖真实 DB/Redis
app.state.bundle = AppState(fixture_settings, bundle)

# 3) 用 fixture_settings 签一个 token
bundle_claims = issue_demo_token(fixture_settings.security, tenant_id="tenant_a", actor_id="user_a1", role=Role.CONSUMER)
print(f"[token-signed] using fixture settings → jti={bundle_claims.claims.jti} token_len={len(bundle_claims.access_token)}")

# 4) 现在拿 ActorMiddleware 里的 settings（就是 create_app 里 settings_probe=load_settings() 的那个），看看它是啥
middleware_instances = [mw for mw in app.user_middleware if getattr(mw.cls, "__name__", "") == "ActorMiddleware"]
print(f"\n[app.user_middleware] ActorMiddleware 数量={len(middleware_instances)}")
if middleware_instances:
    mw = middleware_instances[0]
    # Starlette Middleware 对象：cls=类, kwargs=参数字典 (not .options)
    kwargs = getattr(mw, "kwargs", {})
    mw_settings: Settings = kwargs["settings"]
    print(f"  ActorMiddleware 所用 settings demo_token_secret (len={len(mw_settings.security.demo_token_secret.get_secret_value())}) last6={mw_settings.security.demo_token_secret.get_secret_value()[-6:]}")
    print(f"  secrets EQUAL? {fixture_settings.security.demo_token_secret.get_secret_value() == mw_settings.security.demo_token_secret.get_secret_value()}")
    print(f"  ActorMiddleware 所有 middleware.kwargs keys = {sorted(kwargs.keys())}")
else:
    print("  !!! No ActorMiddleware found!!!")
    print("  middlewares=", [getattr(mw.cls, "__name__", str(mw.cls)) for mw in app.user_middleware])

# 5) 模拟 HTTP 调用：传 X-Tenant-Id=tenant_a + Authorization=Bearer <fixture签好的token>
async def main():
    transport = ASGITransport(app=app)
    headers = {
        "X-Tenant-Id": "tenant_a",
        "Authorization": f"Bearer {bundle_claims.access_token}",
    }
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        print("\n[test] GET /health (白名单应该 200)")
        r = await c.get("/health")
        print("  status=", r.status_code, "body=", r.text[:200])
        print("\n[test] POST /_test_validation （用 test_task1_smoke 同款挂载）")
        from fastapi import FastAPI
        from pydantic import BaseModel

        class _Body(BaseModel):
            amount: int

        @app.post("/_test_validation")
        def _echo(body: _Body) -> dict:
            return body.model_dump()

        r = await c.post("/_test_validation", json={"amount": "not-a-number"}, headers=headers)
        print("  status=", r.status_code, "body=", r.text[:500])
        print("\n[test] GET /definitely-not-exist (tr1.3)")
        r = await c.get("/definitely-not-exist", headers=headers)
        print("  status=", r.status_code, "body=", r.text[:500])

asyncio.run(main())

from __future__ import annotations

from app.config import Settings


def test_settings_constructs_without_env_files(test_settings: Settings) -> None:
    """TR-1.1 辅助：settings 可从 fixture 构造，说明必填字段可被校验。"""
    assert test_settings.app_env.value == "test"
    assert test_settings.db.url.get_secret_value().startswith("postgresql+psycopg://")
    assert test_settings.redis.url.get_secret_value().startswith("redis://")
    # DEMO_TOKEN_SECRET < 32 字符时 Pydantic 应报错
    from pydantic import SecretStr, ValidationError

    from app.config import SecuritySettings

    try:
        SecuritySettings(demo_token_secret=SecretStr("too-short"))
    except ValidationError:
        return
    raise AssertionError("SecuritySettings 未校验 demo_token_secret 最小长度")


async def test_health_endpoint_ok(client) -> None:
    """TR-1.2：/health 返回 200 + status=ok，且带 request_id。"""
    resp = await client.get("/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert "request_id" in body and body["request_id"] is not None
    assert "x-request-id" in {k.lower(): v for k, v in resp.headers.items()}


async def test_unknown_route_is_404_unified(client) -> None:
    """TR-1.3：未知路由 404 统一使用 ErrorDisplay 格式，不暴露堆栈。"""
    resp = await client.get("/definitely-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    # 键名匹配 ErrorDisplay schema
    for key in ("code", "message", "request_id"):
        assert key in body, key
    assert body["code"] == "RESOURCE_NOT_FOUND"
    # TEST 环境默认带 stack_trace；PROD 的保障由 app_env 决定，这里不做强断言
    assert "traceback" not in (body.get("message") or "")


async def test_validation_error_format(client) -> None:
    """TR-1.3（补充）：FastAPI 参数校验失败返回 422 + 稳定错误码 VALIDATION_ERROR。

    利用 /health 不接受 query 参数的特性；另外补一个显式的带 Pydantic 错误场景。
    这里直接调一个不存在且有类型约束的接口，通过在 test_app 临时挂路由实现。
    """
    from fastapi import FastAPI
    from pydantic import BaseModel

    app: FastAPI = client._transport.app  # type: ignore[attr-defined]

    class _Body(BaseModel):
        amount: int

    @app.post("/_test_validation")
    def _echo(body: _Body) -> dict:
        return body.model_dump()

    resp = await client.post("/_test_validation", json={"amount": "not-a-number"})
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "details" in body and "errors" in body["details"]


async def test_request_id_echoed_in_response_headers(client) -> None:
    """TR-1.4 辅助：请求头 X-Request-Id 传入时响应头回写相同值。"""
    custom = "req_my-custom-request-id-0001"
    resp = await client.get("/health", headers={"X-Request-Id": custom})
    assert resp.headers.get("x-request-id") == custom
    assert resp.json()["request_id"] == custom

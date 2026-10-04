from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.config import AppEnv

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="健康检查",
    description="不经过鉴权。用于部署探针、CI/CD 验证服务已启动。",
)
async def health(request: Request) -> dict[str, Any]:
    """HTTP 200 `{"status": "ok"}`；同时回显 app_env、request_id 便于排查。"""
    app_env: AppEnv = AppEnv.LOCAL
    try:
        from app.main import _state_from_app

        state = _state_from_app(request.app)
        app_env = state.settings.app_env
    except Exception:
        pass
    from app.core.logging import REQUEST_ID_CONTEXT

    return {
        "status": "ok",
        "app_env": app_env.value,
        "request_id": REQUEST_ID_CONTEXT.get() or None,
    }

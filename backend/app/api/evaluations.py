"""离线质量评估接口（admin-only）。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.application.auth import require_actor
from app.application.schemas.identity import Role
from app.domain.repositories.identity import Actor
from app.evals.runner import (
    get_experiment_detail,
    get_status,
    list_experiments,
    run_evaluation,
)

router = APIRouter(prefix="/api/evaluations", tags=["evaluations"])


def require_admin(actor: Actor = Depends(require_actor)) -> Actor:
    if actor.role != Role.ADMIN:
        from app.core.errors import PermissionDeniedError

        raise PermissionDeniedError("仅管理员可操作")
    return actor


class RunRequestBody(BaseModel):
    tags: list[str] | None = None


@router.post("/run")
async def trigger_run(
    request: Request,
    body: RunRequestBody = RunRequestBody(),
    _: Actor = Depends(require_admin),
) -> dict[str, Any]:
    if get_status()["status"] == "running":
        raise HTTPException(status_code=409, detail="已有评估在运行")

    app_bundle = request.app.state.bundle
    facade = request.app.state.agent_facade_singleton
    task = asyncio.create_task(
        run_evaluation(
            settings=app_bundle.settings,
            bundle=app_bundle.infra,
            facade=facade,
            tags=body.tags,
        )
    )
    # 持有强引用，避免任务被 GC
    request.app.state.evaluation_task = task
    return {"status": "running"}


@router.get("/status")
async def read_status(_: Actor = Depends(require_admin)) -> dict[str, Any]:
    return get_status()


@router.get("/experiments")
async def read_experiments(
    request: Request,
    _: Actor = Depends(require_admin),
) -> list[dict[str, Any]]:
    settings = request.app.state.bundle.settings
    # LangSmith SDK 为同步 HTTP 客户端：丢线程池执行，避免阻塞事件循环拖慢其他请求
    return await asyncio.to_thread(list_experiments, settings)


@router.get("/experiments/{name}")
async def read_experiment_detail(
    name: str,
    request: Request,
    _: Actor = Depends(require_admin),
) -> dict[str, Any]:
    settings = request.app.state.bundle.settings
    return await asyncio.to_thread(get_experiment_detail, settings, name)

"""评估 API 单测：触发/并发冲突/权限/状态/实验查询。"""

from __future__ import annotations

from typing import Any

import pytest

from app.api import evaluations as eval_api
from app.application.schemas.identity import Role, issue_demo_token

ADMIN_ID = "11111111-1111-4111-8111-111111111111"
CONSUMER_ID = "3f88a233-4d11-50e6-926b-e0ddd2838c0c"


def _headers(settings: Any, *, role: Role, actor_id: str) -> dict[str, str]:
    token = issue_demo_token(
        settings.security,
        tenant_id="tenant_a",
        actor_id=actor_id,
        role=role,
    )
    return {
        "X-Tenant-Id": "tenant_a",
        "Authorization": f"Bearer {token.access_token}",
    }


@pytest.fixture
def _admin_headers(test_settings: Any) -> dict[str, str]:
    return _headers(
        test_settings, role=Role.ADMIN, actor_id=ADMIN_ID
    )


@pytest.fixture
def _consumer_headers(test_settings: Any) -> dict[str, str]:
    return _headers(
        test_settings, role=Role.CONSUMER, actor_id=CONSUMER_ID
    )


@pytest.fixture
def _wired(
    monkeypatch: pytest.MonkeyPatch, test_app: Any
) -> Any:
    async def _fake_run_evaluation(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(eval_api, "run_evaluation", _fake_run_evaluation)
    test_app.state.agent_facade_singleton = object()
    return test_app


@pytest.mark.asyncio
async def test_trigger_run_ok(
    _wired: Any, _admin_headers: dict[str, str]
) -> None:
    resp = await _wired_client_post(
        _wired, _admin_headers, "/api/evaluations/run", "{}"
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"


@pytest.mark.asyncio
async def test_trigger_run_conflict(
    _wired: Any,
    _admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        eval_api,
        "get_status",
        lambda: {"status": "running"},
    )
    resp = await _wired_client_post(
        _wired, _admin_headers, "/api/evaluations/run", "{}"
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_trigger_run_forbidden_for_consumer(
    _wired: Any, _consumer_headers: dict[str, str]
) -> None:
    resp = await _wired_client_post(
        _wired, _consumer_headers, "/api/evaluations/run", "{}"
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_status(
    _wired: Any,
    _admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        eval_api,
        "get_status",
        lambda: {"status": "idle", "total": 0, "completed": 0},
    )
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=_wired),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as client:
        resp = await client.get(
            "/api/evaluations/status", headers=_admin_headers
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "idle"


@pytest.mark.asyncio
async def test_list_and_detail_experiments(
    _wired: Any,
    _admin_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        eval_api,
        "list_experiments",
        lambda _settings: [{"name": "exp-1"}],
    )
    monkeypatch.setattr(
        eval_api,
        "get_experiment_detail",
        lambda _settings, _name: {"name": _name, "cases": []},
    )
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=_wired),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as client:
        list_resp = await client.get(
            "/api/evaluations/experiments", headers=_admin_headers
        )
        detail_resp = await client.get(
            "/api/evaluations/experiments/exp-1",
            headers=_admin_headers,
        )
    assert list_resp.status_code == 200
    assert list_resp.json() == [{"name": "exp-1"}]
    assert detail_resp.status_code == 200
    assert detail_resp.json()["name"] == "exp-1"


async def _wired_client_post(
    app: Any, headers: dict[str, str], path: str, body: str
) -> Any:
    from httpx import ASGITransport, AsyncClient

    headers = {"content-type": "application/json", **headers}
    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as client:
        return await client.post(path, headers=headers, content=body)

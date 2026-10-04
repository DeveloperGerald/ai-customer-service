"""商品目录（products）单测：Repository 过滤 / 工具注册与执行 / 路由挂载。

覆盖：
- TP-01 get_read_by_id 命中 → ProductRead 字段正确映射
- TP-02 get_read_by_id 不存在 → ResourceNotFound
- TP-03 get_read_by_sku 命中
- TP-04 get_read_by_sku 不存在 → ResourceNotFound
- TP-05 list 名称模糊 + 分类 + 状态：SQL 含 tenant 过滤 / ILIKE 转义 / 分类状态 / 分页
- TP-06 list 无过滤条件默认只带 tenant_id
- TP-07 product_query 工具按 product_id 查询成功
- TP-08 product_query 工具非法 UUID → ToolExecutionError(VALIDATION_ERROR)
- TP-09 product_query 工具按 sku_code 查询成功；无参数报错；SKU 不存在 → ResourceNotFound
- TP-10 product_list 工具返回 items/count；非法 status 报错
- TP-11 ALL_TOOLS 含 product_list / product_query；OpenAPI 已挂载两个商品路由
"""

from __future__ import annotations

import datetime as _dt
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from langgraph.prebuilt.tool_node import ToolRuntime
from sqlalchemy.dialects import postgresql

from app.application.agent.context import AgentRunContext
from app.application.schemas.identity import Role
from app.application.schemas.product import ProductQueryFilter
from app.application.tools.builtin import (
    ALL_TOOLS,
    product_list,
    product_query,
)
from app.core.errors import ResourceNotFoundError, ToolExecutionError
from app.domain.constants.policies import TENANT_POLICIES
from app.domain.models.product import ProductORM
from app.domain.repositories.identity import Actor
from app.domain.repositories.product import ProductRepository

TENANT_A_CONSUMER = Actor(
    actor_id="a0000000-0000-0000-0000-000000000001",
    tenant_id="tenant_a",
    role=Role.CONSUMER,
)


def _make_product(
    *,
    product_id: str | None = None,
    tenant_id: str = "tenant_a",
    product_name: str = "禅饰坊·天然南红玛瑙手串 满肉柿子红 12mm",
    sku_code: str = "SKU-A-NANHONG-007",
    category: str = "南红",
    status: str = "on_sale",
    specs: str | None = "12mm·满肉柿子红",
    price_yuan: int = 1299_00,
    stock: int = 25,
) -> ProductORM:
    now = _dt.datetime.now(_dt.timezone.utc)
    return ProductORM(
        product_id=UUID(product_id) if product_id else uuid4(),
        tenant_id=tenant_id,
        product_name=product_name,
        sku_code=sku_code,
        category=category,
        status=status,
        specs=specs,
        price_yuan=price_yuan,
        stock=stock,
        created_at=now,
        updated_at=now,
    )


def _tool_rt(session: Any) -> ToolRuntime:
    """构造工具直调用的 ToolRuntime（工具现为模块级 @tool，runtime 经参数注入）。"""
    ctx = AgentRunContext(
        actor=TENANT_A_CONSUMER,
        tenant_id="tenant_a",
        thread_id="tenant_a:test-thread",
        service_actor=Actor(
            actor_id="a0000000-0000-0000-0000-000000000002",
            tenant_id="tenant_a",
            role=Role.STAFF,
        ),
        effective_policy=TENANT_POLICIES["tenant_a"],
        idempotency_salt="",
        session=session,
        conversation_repo=None,  # type: ignore[arg-type]
    )
    return ToolRuntime(
        state={},
        tool_call_id=str(uuid4()),
        config={},
        context=ctx,
        store=None,
        stream_writer=lambda *_: None,
    )


# ============================================================================
# TP-01 / TP-02：按主键读
# ============================================================================


@pytest.mark.asyncio
async def test_tp01_get_read_by_id_ok_maps_fields() -> None:
    product = _make_product(product_id="11111111-1111-1111-1111-111111111111")
    session = AsyncMock()
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = product
    session.execute = AsyncMock(return_value=mr)

    repo = ProductRepository(session)
    read = await repo.get_read_by_id("tenant_a", product.product_id)
    assert read.product_id == product.product_id
    assert read.tenant_id == "tenant_a"
    assert read.sku_code == "SKU-A-NANHONG-007"
    assert read.status == "on_sale"
    assert read.price_yuan == 1299_00
    assert read.stock == 25
    assert read.specs == "12mm·满肉柿子红"


@pytest.mark.asyncio
async def test_tp02_get_read_by_id_not_found_raises() -> None:
    session = AsyncMock()
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mr)

    repo = ProductRepository(session)
    with pytest.raises(ResourceNotFoundError):
        await repo.get_read_by_id("tenant_a", UUID("22222222-2222-2222-2222-222222222222"))


# ============================================================================
# TP-03 / TP-04：按 SKU 读
# ============================================================================


@pytest.mark.asyncio
async def test_tp03_get_read_by_sku_ok() -> None:
    product = _make_product(sku_code="SKU-B-CUSTOM-2025", tenant_id="tenant_b")
    session = AsyncMock()
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = product
    session.execute = AsyncMock(return_value=mr)

    repo = ProductRepository(session)
    read = await repo.get_read_by_sku("tenant_b", "SKU-B-CUSTOM-2025")
    assert read.sku_code == "SKU-B-CUSTOM-2025"
    assert read.tenant_id == "tenant_b"


@pytest.mark.asyncio
async def test_tp04_get_read_by_sku_not_found_raises() -> None:
    session = AsyncMock()
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=mr)

    repo = ProductRepository(session)
    with pytest.raises(ResourceNotFoundError):
        await repo.get_read_by_sku("tenant_a", "SKU-NOT-EXIST")


# ============================================================================
# TP-05：list 名称模糊 + 分类 + 状态（断言实际生成的 SQL）
# ============================================================================


@pytest.mark.asyncio
async def test_tp05_list_fuzzy_name_category_status_builds_sql() -> None:
    product = _make_product()
    captured: dict[str, Any] = {}

    async def exec_capture(stmt: Any, *a: Any, **kw: Any) -> MagicMock:
        captured["stmt"] = stmt
        mr = MagicMock()
        mr.scalars.return_value.all.return_value = [product]
        return mr

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=exec_capture)
    repo = ProductRepository(session)

    flt = ProductQueryFilter(name_like="南红", category="南红", status="on_sale", limit=10, offset=20)
    items = await repo.list_products("tenant_a", flt=flt)

    assert len(items) == 1
    assert items[0].sku_code == "SKU-A-NANHONG-007"

    sql = str(
        captured["stmt"].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "products.tenant_id = 'tenant_a'" in sql
    assert "like" in sql and "%南红%" in sql
    assert "products.category = '南红'" in sql
    assert "products.status = 'on_sale'" in sql
    assert "limit 10" in sql and "offset 20" in sql
    # 排序：新建商品优先
    assert "order by products.created_at desc" in sql


@pytest.mark.asyncio
async def test_tp06_list_escapes_sql_wildcards_and_default_filter_only_tenant() -> None:
    captured: dict[str, Any] = {}

    async def exec_capture(stmt: Any, *a: Any, **kw: Any) -> MagicMock:
        captured["stmt"] = stmt
        mr = MagicMock()
        mr.scalars.return_value.all.return_value = []
        return mr

    session = AsyncMock()
    session.execute = AsyncMock(side_effect=exec_capture)
    repo = ProductRepository(session)

    # 用户输入含通配符 % 与 _，必须被反斜杠转义后再作为绑定参数发送
    await repo.list_products("tenant_a", flt=ProductQueryFilter(name_like="50%_off"))
    compiled_escaped = captured["stmt"].compile(dialect=postgresql.dialect())
    assert "%50\\%\\_off%" in compiled_escaped.params.values()
    assert "ESCAPE" in str(compiled_escaped)

    # 默认过滤器：除 tenant_id 外不追加任何条件
    await repo.list_products("tenant_a")
    default_sql = str(
        captured["stmt"].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "products.tenant_id = 'tenant_a'" in default_sql
    assert "like" not in default_sql
    # status/category 仅出现在 SELECT 列，WHERE 中不能有比较条件
    assert "products.status =" not in default_sql
    assert "products.category =" not in default_sql


# ============================================================================
# TP-07 / TP-08 / TP-09：ProductQueryTool
# ============================================================================


@pytest.mark.asyncio
async def test_tp07_product_query_tool_by_id_ok() -> None:
    product = _make_product(product_id="33333333-3333-3333-3333-333333333333")
    session = AsyncMock()
    mr = MagicMock()
    mr.scalar_one_or_none.return_value = product
    session.execute = AsyncMock(return_value=mr)

    result = await product_query.coroutine(
        product_id="33333333-3333-3333-3333-333333333333",
        sku_code=None,
        runtime=_tool_rt(session),
    )
    assert result["sku_code"] == "SKU-A-NANHONG-007"
    assert result["price_yuan"] == 1299_00
    # 工具返回不允许携带伪造身份（tenant_id 来自 Actor，不在入参里）
    assert result["tenant_id"] == "tenant_a"


@pytest.mark.asyncio
async def test_tp08_product_query_tool_bad_uuid_raises_validation_error() -> None:
    session = AsyncMock()
    with pytest.raises(ToolExecutionError):
        await product_query.coroutine(
            product_id="not-a-uuid",
            sku_code=None,
            runtime=_tool_rt(session),
        )
    # UUID 非法不执行任何 SQL
    assert session.execute.call_count == 0


@pytest.mark.asyncio
async def test_tp09_product_query_tool_by_sku_ok_missing_and_no_args() -> None:
    product = _make_product(sku_code="SKU-A-STANDARD-001", product_name="禅饰坊·星月菩提 108 颗手串")
    session = AsyncMock()
    mr_hit = MagicMock()
    mr_hit.scalar_one_or_none.return_value = product
    mr_miss = MagicMock()
    mr_miss.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(side_effect=[mr_hit, mr_miss])

    ok = await product_query.coroutine(
        product_id=None,
        sku_code="SKU-A-STANDARD-001",
        runtime=_tool_rt(session),
    )
    assert ok["product_name"] == "禅饰坊·星月菩提 108 颗手串"

    with pytest.raises(ResourceNotFoundError):
        await product_query.coroutine(
            product_id=None,
            sku_code="SKU-MISSING",
            runtime=_tool_rt(session),
        )

    with pytest.raises(ToolExecutionError):
        await product_query.coroutine(product_id=None, sku_code=None, runtime=_tool_rt(session))


# ============================================================================
# TP-10：ProductListTool
# ============================================================================


@pytest.mark.asyncio
async def test_tp10_product_list_tool_returns_items_and_count() -> None:
    p1 = _make_product(product_id="44444444-4444-4444-4444-444444444444")
    p2 = _make_product(
        product_id="55555555-5555-5555-5555-555555555555",
        sku_code="SKU-A-PUTI-001",
        product_name="禅饰坊·星月菩提 108 颗手串（通货）",
        category="菩提",
        price_yuan=399_00,
        stock=60,
    )
    session = AsyncMock()
    mr = MagicMock()
    mr.scalars.return_value.all.return_value = [p1, p2]
    session.execute = AsyncMock(return_value=mr)

    result = await product_list.coroutine(
        name="手串",
        category=None,
        status=None,
        limit=10,
        runtime=_tool_rt(session),
    )
    assert result["count"] == 2
    assert {item["sku_code"] for item in result["items"]} == {
        "SKU-A-NANHONG-007",
        "SKU-A-PUTI-001",
    }

    # 非法 status 直接校验失败，不执行 SQL
    session_invalid = AsyncMock()
    with pytest.raises(ToolExecutionError):
        await product_list.coroutine(
            name=None,
            category=None,
            status="deleted",
            limit=10,
            runtime=_tool_rt(session_invalid),
        )
    assert session_invalid.execute.call_count == 0


# ============================================================================
# TP-11：模块级工具集 + 路由挂载
# ============================================================================


def test_tp11_default_tools_contain_product_tools() -> None:
    names = {t.name for t in ALL_TOOLS}
    assert {"product_list", "product_query"} <= names
    list_tool = next(t for t in ALL_TOOLS if t.name == "product_list")
    assert set(list_tool.args_schema.model_fields.keys()) >= {"name", "category", "status", "limit"}


async def test_tp11_products_routes_mounted(client) -> None:
    """OpenAPI 中应出现商品 list/get 两个路由（不触库，仅验挂载与 query 参数声明）。"""
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "/api/products" in paths
    assert "/api/products/{product_id}" in paths
    list_params = {p["name"] for p in paths["/api/products"]["get"].get("parameters", [])}
    assert {"name", "category", "status", "limit", "offset"} <= list_params

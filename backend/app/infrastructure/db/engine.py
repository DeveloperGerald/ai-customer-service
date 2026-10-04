from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import DatabaseSettings, RedisSettings, Settings
from app.core.errors import ConfigError


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类。

    集中配置命名约定、type_annotation_map 等，后续模块的 models 均继承此类。
    """


class InfrastructureBundle:
    """数据库/Redis/模型客户端等长生命周期依赖的集合。

    通过 FastAPI lifespan 在启动时初始化、关闭时释放；
    业务层通过 DI 容器或 app.state 读取，不做全局单例，以便测试隔离。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings: Final[Settings] = settings
        self.db_engine: AsyncEngine | None = None
        self.db_session_factory: async_sessionmaker[AsyncSession] | None = None
        self.redis: Redis | None = None

    # ---------- 启动/关闭 ----------
    async def start(self) -> None:
        """按配置初始化 DB engine / sessionmaker / Redis。"""
        self.db_engine = _create_db_engine(self.settings.db)
        self.db_session_factory = async_sessionmaker(
            bind=self.db_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        self.redis = _create_redis_client(self.settings.redis)

    async def stop(self) -> None:
        """释放资源。任何单一资源失败不阻塞其他资源释放。"""
        errors: list[Exception] = []
        if self.db_engine is not None:
            try:
                await self.db_engine.dispose()
            except Exception as exc:  # pragma: no cover - 资源释放兜底
                errors.append(exc)
        if self.redis is not None:
            try:
                await self.redis.aclose()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
        if errors:
            raise errors[0]


def _create_db_engine(cfg: DatabaseSettings) -> AsyncEngine:
    url = cfg.url.get_secret_value().strip()
    if not url:
        raise ConfigError(
            "Database URL 为空，请检查 DB__URL 配置。",
            details={"field": "db.url"},
        )
    if not (url.startswith("postgresql+psycopg://") or url.startswith("postgresql+asyncpg://")):
        raise ConfigError(
            "仅支持 psycopg3 异步驱动，URL 前缀需为 postgresql+psycopg://",
            details={"field": "db.url", "provided_prefix": url.split(":", 1)[0]},
        )
    try:
        return create_async_engine(
            url,
            pool_size=cfg.pool_size,
            max_overflow=cfg.max_overflow,
            pool_recycle=cfg.pool_recycle,
            pool_pre_ping=True,
            echo=cfg.echo,
            future=True,
        )
    except Exception as exc:
        raise ConfigError(
            f"创建数据库引擎失败：{exc}",
            details={"field": "db.url"},
        ) from exc


def _create_redis_client(cfg: RedisSettings) -> Redis:
    url = cfg.url.get_secret_value().strip()
    if not url:
        raise ConfigError(
            "Redis URL 为空，请检查 REDIS__URL 配置。",
            details={"field": "redis.url"},
        )
    try:
        return Redis.from_url(
            url,
            decode_responses=cfg.decode_responses,
            socket_connect_timeout=cfg.socket_connect_timeout,
            socket_timeout=cfg.socket_timeout,
            auto_close_connection_pool=True,
        )
    except Exception as exc:
        raise ConfigError(
            f"创建 Redis 客户端失败：{exc}",
            details={"field": "redis.url"},
        ) from exc


@asynccontextmanager
async def scoped_db_session(bundle: InfrastructureBundle) -> AsyncIterator[AsyncSession]:
    """提供事务边界的 scoped session。

    用法::

        async with scoped_db_session(bundle) as session:
            ...

    正常退出自动 commit；异常时 rollback。
    """
    if bundle.db_session_factory is None:
        raise ConfigError("InfrastructureBundle 尚未启动，无法获取 DB session。")
    session: AsyncSession = bundle.db_session_factory()
    try:
        yield session
        try:
            await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            await session.close()
        except Exception:
            pass

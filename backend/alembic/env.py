from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import load_settings
from app.core.infrastructure import Base
# 显式 import 所有 ORM model 模块，确保 Base.metadata 能收集到它们。
import app.domain.models.identity  # noqa: F401
import app.domain.models.product  # noqa: F401
# 后续每新增一个 models 模块，这里按 alphabetical import。


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_sqlalchemy_url() -> str:
    """优先从 .env 加载真实 DB URL，覆盖 alembic.ini 中的占位。"""
    try:
        settings = load_settings()
        return settings.db.url.get_secret_value()
    except Exception:  # noqa: BLE001 - 允许在 CI/离线环境通过 alembic.ini 回退
        return config.get_main_option("sqlalchemy.url", "")


def run_migrations_offline() -> None:
    """以离线模式执行迁移，输出 SQL 供 DBA 审查。"""
    url = _get_sqlalchemy_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """以在线模式执行迁移。"""
    configuration = config.get_section(config.config_ini_section, {})
    if not configuration.get("sqlalchemy.url"):
        configuration["sqlalchemy.url"] = _get_sqlalchemy_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

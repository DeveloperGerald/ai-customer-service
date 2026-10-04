"""LangGraph checkpointer 工厂：人在回路（HITL）状态持久化。

设计：
  - 生产：AsyncRedisSaver（langgraph-checkpoint-redis），按 thread_id 存取 Agent 暂停态；
    配置 ttl 让暂停的 checkpoint 在 10min 后自动过期（HITL 超时）。
    要求 Redis 带 RedisJSON + RediSearch 模块（redis-stack-server）。
  - 单测/离线：MemorySaver（进程内，不持久化，满足单测隔离）。
  - 通过 configure_checkpointer(redis_url) 在 app 启动时注入；get_checkpointer() 兜底 MemorySaver（仅单测）。

强依赖：redis 是项目硬依赖（AGENTS.md），生产环境 Redis 不可用直接抛异常，不兜底。
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

log = get_logger("agent.checkpoint")

_CHECKPOINTER: Any | None = None
_DEFAULT_TTL_SECONDS = 600  # 10min，与写工具 pending 的 expires_at 对齐


def configure_checkpointer(redis_url: str | None, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> Any:
    """在 app 启动时调用：按 redis_url 构建单例 checkpointer。

    - redis_url 为空 → MemorySaver（单测场景）。
    - redis_url 非空 → AsyncRedisSaver（要求 redis-stack-server）；构建失败直接抛异常。
    幂等：已配置则直接返回现有实例。
    """
    global _CHECKPOINTER
    if _CHECKPOINTER is not None:
        return _CHECKPOINTER
    if not redis_url:
        from langgraph.checkpoint.memory import MemorySaver

        _CHECKPOINTER = MemorySaver()
        log.info("checkpoint.memory_saver", reason="no_redis_url")
        return _CHECKPOINTER

    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    _CHECKPOINTER = AsyncRedisSaver(
        redis_url,
        ttl={"default_ttl": ttl_seconds, "refresh_ttl": ttl_seconds},
    )
    log.info("checkpoint.redis_saver", ttl_seconds=ttl_seconds)
    return _CHECKPOINTER


def get_checkpointer() -> Any:
    """供 facade/agent 构建图时取用；未配置则兜底 MemorySaver（单测场景）。"""
    if _CHECKPOINTER is None:
        return configure_checkpointer(None)
    return _CHECKPOINTER


def reset_checkpointer() -> None:
    """单测隔离用：清空单例。"""
    global _CHECKPOINTER
    _CHECKPOINTER = None


async def setup_checkpointer() -> None:
    """app lifespan 调用：为 Redis checkpointer 创建搜索索引。失败直接抛异常（强依赖）。"""
    cp = get_checkpointer()
    asetup = getattr(cp, "asetup", None)
    if callable(asetup):
        await asetup()

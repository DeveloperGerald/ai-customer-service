from __future__ import annotations

import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogLevel(str, Enum):
    """日志级别枚举。"""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AppEnv(str, Enum):
    """运行环境枚举。"""

    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PROD = "prod"


class DatabaseSettings(BaseModel):
    """数据库配置。"""

    url: SecretStr = Field(
        ...,
        description="PostgreSQL DSN，要求使用 psycopg3 风格：postgresql+psycopg://user:pass@host/db",
    )
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=20, ge=0, le=100)
    pool_recycle: int = Field(default=1800, ge=60)
    echo: bool = False

    model_config = ConfigDict(frozen=True)


class RedisSettings(BaseModel):
    """Redis 配置。"""

    url: SecretStr = Field(
        ...,
        description="Redis URL：redis://:pass@host:port/db",
    )
    decode_responses: bool = True
    socket_connect_timeout: float = 2.0
    socket_timeout: float = 3.0

    model_config = ConfigDict(frozen=True)


class SecuritySettings(BaseModel):
    """安全相关配置。"""

    demo_token_secret: SecretStr = Field(
        ...,
        description="演示令牌 HS256 签名密钥，生产级应使用非对称密钥。",
        min_length=32,
    )
    demo_token_ttl_seconds: int = Field(default=86400 * 30, ge=300)
    request_id_header: str = "X-Request-Id"
    tenant_id_header: str = "X-Tenant-Id"
    auth_header: str = "Authorization"

    model_config = ConfigDict(frozen=True)


class LLMProvider(str, Enum):
    """LLM 供应商枚举。"""

    OPENAI = "openai"


class EmbeddingProvider(str, Enum):
    """Embedding 供应商枚举。"""

    OPENAI = "openai"


class LLMSettings(BaseModel):
    """大模型与 Embedding 配置。"""

    provider: LLMProvider = LLMProvider.OPENAI
    embedding_provider: EmbeddingProvider = EmbeddingProvider.OPENAI

    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    chat_model: str = "gpt-4o-mini"
    chat_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    chat_max_tokens: int = Field(default=1024, ge=64, le=16384)
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = Field(default=1536, ge=128, le=8192)
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    embedding_request_timeout: float = 15.0

    model_config = ConfigDict(frozen=True)


class LangSmithSettings(BaseModel):
    """LangSmith 可观测配置。"""

    api_key: SecretStr | None = None
    endpoint: str = "https://api.smith.langchain.com"
    project: str = "ai-customer-service-demo"
    tracing_enabled: bool = False

    model_config = ConfigDict(frozen=True)


class AgentSettings(BaseModel):
    """Agent 编排相关参数。"""

    max_graph_steps: int = Field(default=20, ge=5, le=200)
    clarification_max_count: int = Field(default=3, ge=1, le=10)
    intent_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    rag_top_k: int = Field(default=4, ge=1, le=20)
    rag_similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    pending_action_ttl_seconds: int = Field(default=1800, ge=60)
    tool_default_timeout_seconds: float = Field(default=15.0, ge=1.0, le=300.0)

    model_config = ConfigDict(frozen=True)


class EvaluationSettings(BaseModel):
    """离线质量评估配置（LangSmith Experiment）。"""

    dataset_name: str = "ai-cs-golden"
    # Judge 模型：留空时使用主聊天模型（llm.chat_model）及其 api_key/base_url
    judge_model: str = ""
    judge_api_key: SecretStr | None = None
    judge_base_url: str = ""
    concurrency: int = Field(default=4, ge=1, le=20)

    model_config = ConfigDict(frozen=True)


_CONFIG_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _CONFIG_DIR.parent
_REPO_ROOT_DIR = _BACKEND_DIR.parent

_ENV_FILES_DEFAULT: tuple[str, ...] = (
    str(_REPO_ROOT_DIR / ".env"),
    str(_BACKEND_DIR / ".env"),
)


def _env_files_for_settings() -> tuple[str, ...]:
    """根据当前 APP_ENV / TEST__DISABLE_ENV_FILE 决定是否加载 .env 文件。

    TEST 环境下必须禁用文件读取，否则 pytest_configure 通过 os.environ 强制注入的
    demo_token_secret / db_url 等值会被 backend/.env 里的真实值覆盖，
    导致「测试夹具签的 token」和「ActorMiddleware 实际验签的密钥」不一致 → HTTP 测试全 401。
    """
    app_env = os.getenv("APP_ENV", "local").lower()
    disable_env_file = (
        app_env == "test"
        or os.getenv("TEST__DISABLE_ENV_FILE", "false").lower() in {"1", "true", "yes", "on"}
    )
    return () if disable_env_file else _ENV_FILES_DEFAULT


class Settings(BaseSettings):
    """应用全局配置。

    所有字段必填的，未填写会在加载时抛出 ValidationError。
    敏感字段使用 SecretStr，禁止直接打印或写入日志。

    Pydantic v2 BaseSettings 直接从环境变量解析；嵌套字段（db / redis / security 等）
    用 env_nested_delimiter="__"：DB__URL 映射到 settings.db.url。
    """

    app_name: str = "ai-customer-service-backend"
    app_env: AppEnv = AppEnv.LOCAL
    log_level: LogLevel = LogLevel.INFO
    json_log: bool = True
    api_prefix: str = "/api"

    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5174", "http://localhost:3000"],
    )

    db: DatabaseSettings
    redis: RedisSettings
    security: SecuritySettings
    llm: LLMSettings = Field(default_factory=LLMSettings)
    langsmith: LangSmithSettings = Field(default_factory=LangSmithSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    evaluation: EvaluationSettings = Field(default_factory=EvaluationSettings)

    model_config = SettingsConfigDict(
        frozen=True,
        # Pydantic-settings 2.2+ 原生支持多文件元组：后者覆盖前者；
        # 两个文件都不存在也不报错（env_file_optional 仅 2.3+，这里靠 env 缺省值兜底）。
        env_file=_env_files_for_settings(),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )


def load_settings() -> Settings:
    """从环境变量和 .env 文件加载配置。

    Returns:
        已校验的 Settings 实例。

    Raises:
        pydantic.ValidationError: 当必填配置缺失或不合法时抛出。
    """
    return Settings()

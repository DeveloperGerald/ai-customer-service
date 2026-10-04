from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

import jwt
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import SecuritySettings
from app.core.errors import AuthError, ConfigError, ErrorCode

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class Role(str, Enum):
    """系统角色枚举。与 PRD 2.3 节一致。"""

    CONSUMER = "consumer"
    STAFF = "staff"
    ADMIN = "admin"
    AGENT_ENGINEER = "agent_engineer"


# ---------- 租户相关 ----------

class TenantBase(BaseModel):
    """租户共享字段。"""

    name: str = Field(..., min_length=1, max_length=100, description="租户展示名称，如 '禅饰坊（tenant_a）'。")
    display_name: str | None = Field(default=None, max_length=200, description="对外展示名。")
    description: str | None = Field(default=None, max_length=1000, description="政策差异说明，便于面试演示对比三租户。")
    is_active: bool = True


class TenantCreate(TenantBase):
    """创建租户请求。"""

    tenant_id: str = Field(..., min_length=2, max_length=50, pattern=r"^[a-z0-9_]+$")


class TenantRead(TenantBase):
    """租户响应。"""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    created_at: datetime


# ---------- 用户相关 ----------

class UserBase(BaseModel):
    """用户共享字段。手机号不是查询凭证，仅展示用。"""

    username: str = Field(..., min_length=2, max_length=80)
    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=32)
    role: Role = Role.CONSUMER
    is_active: bool = True

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        if not _EMAIL_RE.match(v):
            raise ValueError("invalid email format")
        return v


class UserCreate(UserBase):
    """创建用户请求。"""

    user_id: str | None = Field(default=None, min_length=4, max_length=64)


class UserRead(UserBase):
    """用户响应。对外展示脱敏：手机号/邮箱隐去中间字符。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: str
    tenant_id: str
    created_at: datetime
    updated_at: datetime

    @field_validator("user_id", mode="before")
    @classmethod
    def _coerce_user_id(cls, v: object) -> str:
        if isinstance(v, str):
            return v
        return str(v)

    @field_validator("phone", mode="after")
    @classmethod
    def _mask_phone(cls, v: str | None) -> str | None:
        if not v:
            return v
        if len(v) >= 7:
            return v[:3] + "****" + v[-4:]
        return "***"

    @field_validator("email", mode="after")
    @classmethod
    def _mask_email(cls, v: str | None) -> str | None:
        if not v or "@" not in v:
            return v
        local, _, domain = v.partition("@")
        if len(local) <= 2:
            return "***@" + domain
        return local[:2] + "***@" + domain


# ---------- 演示令牌 ----------

_VALID_TENANT_KEY = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
_VALID_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{3,63}$")


class DemoTokenClaims(BaseModel):
    """演示令牌中携带的可信身份。由服务端签名，客户端不可篡改。

    Attributes:
        jti: 令牌唯一 ID，便于未来加入黑名单。
        actor_id: 对应用户 user_id（支持 UUID 字符串）。
        tenant_id: 所属租户。签发时必须与账号租户绑定一致。
        role: 角色。
        issued_at: 签发时间。
        expires_at: 过期时间。
    """

    jti: str
    actor_id: str
    tenant_id: str
    role: Role
    issued_at: datetime
    expires_at: datetime

    @field_validator("tenant_id")
    @classmethod
    def _check_tenant_key(cls, v: str) -> str:
        if not _VALID_TENANT_KEY.match(v):
            raise ValueError("tenant_id must match ^[a-z][a-z0-9_]{2,31}$")
        return v

    @field_validator("actor_id")
    @classmethod
    def _check_actor_id(cls, v: str) -> str:
        if not _VALID_ACTOR_ID.match(v):
            raise ValueError("actor_id must match ^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$")
        return v


class DemoTokenBundle(BaseModel):
    """签发令牌的返回体。用于 seed 脚本输出，便于前端硬编码演示令牌。"""

    access_token: str
    token_type: str = "Bearer"
    expires_in_seconds: int
    claims: DemoTokenClaims
    debug_info: dict[str, Any] | None = None


# ---------- 签发 / 验证 ----------

def issue_demo_token(
    settings: SecuritySettings,
    *,
    tenant_id: str,
    actor_id: str,
    role: Role,
    extra_ttl: timedelta | None = None,
) -> DemoTokenBundle:
    """签发一个演示用 JWT（HS256）。

    Args:
        settings: 安全配置，包含签名密钥和默认 TTL。
        tenant_id: 所属租户 ID，必须与 actor_id 绑定。
        actor_id: 用户 ID。
        role: 角色枚举。
        extra_ttl: 可选的额外 TTL；默认使用 settings.demo_token_ttl_seconds。

    Returns:
        包含访问令牌和声明的结构体。

    Raises:
        ConfigError: 签名密钥长度不足 32。
    """
    secret = settings.demo_token_secret.get_secret_value()
    if len(secret) < 32:
        raise ConfigError("DEMO_TOKEN_SECRET 长度不足 32，存在安全风险。")
    now = datetime.now(timezone.utc)
    ttl = timedelta(seconds=settings.demo_token_ttl_seconds)
    if extra_ttl is not None:
        ttl += extra_ttl
    expires = now + ttl
    claims = DemoTokenClaims(
        jti=uuid4().hex,
        actor_id=actor_id,
        tenant_id=tenant_id,
        role=role,
        issued_at=now,
        expires_at=expires,
    )
    payload = {
        "jti": claims.jti,
        "sub": claims.actor_id,
        "tenant_id": claims.tenant_id,
        "role": claims.role.value,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "type": "demo_access",
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return DemoTokenBundle(
        access_token=token,
        expires_in_seconds=int(ttl.total_seconds()),
        claims=claims,
    )


def verify_demo_token(settings: SecuritySettings, token: str) -> DemoTokenClaims:
    """验证演示令牌合法性与有效性，返回结构化 claims。

    Args:
        settings: 安全配置。
        token: 从 Authorization: Bearer <token> 提取的纯 token 字符串。

    Returns:
        解析后的 DemoTokenClaims。

    Raises:
        AuthError: 过期、签名错误、字段缺失、类型非 demo_access。
    """
    secret = settings.demo_token_secret.get_secret_value()
    if not token:
        raise AuthError(ErrorCode.AUTH_MISSING, message="缺少 Authorization: Bearer <token>。")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={
                "require": ["exp", "iat", "sub", "tenant_id", "role"],
                "verify_signature": True,
                "verify_exp": True,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError(ErrorCode.AUTH_TOKEN_EXPIRED, message="令牌已过期。") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(ErrorCode.AUTH_TOKEN_INVALID, message=f"令牌无效：{exc}") from exc
    if payload.get("type") != "demo_access":
        raise AuthError(ErrorCode.AUTH_TOKEN_INVALID, message="令牌类型不匹配。")
    try:
        claims = DemoTokenClaims(
            jti=payload["jti"],
            actor_id=payload["sub"],
            tenant_id=payload["tenant_id"],
            role=Role(payload["role"]),
            issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=timezone.utc),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc),
        )
    except Exception as exc:
        raise AuthError(ErrorCode.AUTH_TOKEN_INVALID, message="令牌字段不完整。") from exc
    return claims


def parse_bearer_token(auth_header: str | None) -> str:
    """从请求头 Authorization 值中提取 Bearer token 纯字符串。

    Raises:
        AuthError: 格式错误或缺失。
    """
    if not auth_header:
        raise AuthError(ErrorCode.AUTH_MISSING)
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise AuthError(ErrorCode.AUTH_MISSING, message="Authorization 必须为 Bearer <token>。")
    return parts[1]

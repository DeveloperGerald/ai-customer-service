from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

PolicyType = Literal["no_reason", "quality_only", "hybrid"]
ChunkSource = Literal["policy_manual", "faq", "operation_doc"]


class PolicyTypeE(Enum):
    NO_REASON = "no_reason"
    QUALITY_ONLY = "quality_only"
    HYBRID = "hybrid"


class ChunkSourceE(Enum):
    POLICY_MANUAL = "policy_manual"
    FAQ = "faq"
    OPERATION_DOC = "operation_doc"


_VALID_POLICY_TYPES = {"no_reason", "quality_only", "hybrid"}
_VALID_CHUNK_SOURCES = {"policy_manual", "faq", "operation_doc"}


# ============================================================================
# 一、结构化配置 TenantPolicyConfig
# ============================================================================


class PolicyConfigBase(BaseModel):
    """售后政策共享字段（staff/admin 可写）。"""

    return_days: int | None = Field(
        default=None,
        ge=0,
        le=365,
        description="非质量退货支持的退货窗口（天）。None 表示完全不支持非质量退货。",
    )
    return_policy_type: PolicyType = Field(
        default="hybrid",
        description="售后类型：no_reason(7天无理由) / quality_only(仅质量问题) / hybrid(质量+非质量收手续费)",
    )
    restocking_fee_pct_non_quality: int = Field(
        default=0,
        ge=0,
        le=100,
        description="非质量退货手续费率（0-100，百分比整数），例如 10 = 10%。",
    )
    warranty_days_quality: int = Field(
        default=30,
        ge=0,
        le=365 * 3,
        description="质量问题保修期（天）。",
    )
    custom_product_allowed_return: bool = Field(
        default=False,
        description="定制款是否允许非质量退货。默认 False = 定制款仅质量可退。",
    )

    @field_validator("return_policy_type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in _VALID_POLICY_TYPES:
            raise ValueError(f"return_policy_type 必须在 {sorted(_VALID_POLICY_TYPES)}")
        return v

    @field_validator("return_days")
    @classmethod
    def _consistent_return_days(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            return None
        return v


class PolicyConfigUpdate(PolicyConfigBase):
    """PUT /api/management/tenants/{tenant_id}/policy 请求体。

    所有字段选填；传什么改什么，不传的保留原值。
    """

    return_days: int | None = Field(default=None, ge=0, le=365)
    return_policy_type: PolicyType | None = Field(default=None)
    restocking_fee_pct_non_quality: int | None = Field(default=None, ge=0, le=100)
    warranty_days_quality: int | None = Field(default=None, ge=0, le=1095)
    custom_product_allowed_return: bool | None = Field(default=None)


class PolicyConfigRead(PolicyConfigBase):
    """售后配置对外响应。"""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    updated_by: UUID | None = Field(description="最后修改该配置的 user_id（可为空代表系统默认）。")
    updated_at: datetime
    created_at: datetime


# ============================================================================
# 二、知识库 Chunk（政策全文 + FAQ 共用一张表）
# ============================================================================


class KnowledgeChunkBase(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000, description="段落原文")
    source: ChunkSource = Field(default="faq", description="来源：policy_manual / faq / operation_doc")
    title: str | None = Field(default=None, max_length=255, description="可选标题")

    @field_validator("source")
    @classmethod
    def _check_source(cls, v: str) -> str:
        if v not in _VALID_CHUNK_SOURCES:
            raise ValueError(f"source 必须在 {sorted(_VALID_CHUNK_SOURCES)}")
        return v


class KnowledgeChunkCreate(KnowledgeChunkBase):
    """POST /api/management/tenants/{tenant_id}/knowledge/chunks 请求体。

    created_by 不可由客户端传入，服务端从 Actor 中自动注入（防伪造上传者）。
    """

    pass


class KnowledgeChunkRead(KnowledgeChunkBase):
    model_config = ConfigDict(from_attributes=True)

    chunk_id: UUID
    tenant_id: str
    created_by: UUID
    created_at: datetime

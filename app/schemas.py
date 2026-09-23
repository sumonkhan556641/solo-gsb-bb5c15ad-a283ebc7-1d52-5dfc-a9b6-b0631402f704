"""Pydantic 模型：事件、策略、评估结果与校验结果。"""

from __future__ import annotations

from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Sensitivity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RetentionUnit(StrEnum):
    DAYS = "days"
    MONTHS = "months"
    YEARS = "years"


class RetentionAction(StrEnum):
    EXPIRE = "expire"
    DELETE = "delete"
    ANONYMIZE = "anonymize"
    ARCHIVE = "archive"
    LEGAL_HOLD = "legal_hold"


class Specificity(IntEnum):
    """匹配维度具体度，用于排序与解释。数值越大越具体。"""

    WILDCARD = 0
    EXACT = 1


class EventIn(BaseModel):
    """客户端提交的待评估事件。"""

    event_type: str = Field(..., min_length=1, max_length=128, examples=["login"])
    tenant_id: str = Field(..., min_length=1, max_length=128, examples=["tenant-a"])
    sensitivity: Sensitivity = Field(..., examples=["high"])
    occurred_at: datetime = Field(
        ...,
        description="事件发生时间（ISO 8601）。不带时区的朴素时间按 UTC 处理。",
        examples=["2026-09-22T08:00:00Z"],
    )

    @field_validator("event_type", "tenant_id")
    @classmethod
    def _strip_required(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v


class PolicyBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, examples=["pci-audit"])
    description: str = Field("", max_length=2000)
    event_type: str | None = Field(
        None, description="事件类型；null/空串表示通配任意事件类型"
    )
    tenant_id: str | None = Field(None, description="租户 ID；null/空串表示通配任意租户")
    sensitivity: Sensitivity | None = Field(
        None, description="敏感级别；null 表示通配任意敏感级别"
    )
    priority: int = Field(
        100, ge=1, le=10000, description="优先级，数值越小优先级越高（1 最高）"
    )
    retain_duration: int | None = Field(
        None, ge=1, le=100000, description="保留时长；keep_forever=true 时必须为空"
    )
    retain_unit: RetentionUnit | None = Field(None, description="时长单位")
    keep_forever: bool = Field(
        False, description="永久保留（如法律保全）；为 true 时不计算到期时间"
    )
    action: RetentionAction = Field(
        RetentionAction.EXPIRE, description="到期时执行的动作"
    )
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空字符串")
        return v

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("event_type", "tenant_id")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
        return v or None

    def semantic_errors(self) -> list[dict[str, Any]]:
        """JSON Schema 无法表达的跨字段语义校验，返回字段级错误列表。"""
        errors: list[dict[str, Any]] = []
        duration = self.retain_duration
        unit = self.retain_unit
        if self.keep_forever:
            if duration is not None or unit is not None:
                errors.append(
                    {
                        "field": "retain_duration/retain_unit",
                        "message": "keep_forever=true 时必须同时省略 retain_duration 与 retain_unit",
                    }
                )
        else:
            missing = [
                name
                for name, value in (
                    ("retain_duration", duration),
                    ("retain_unit", unit),
                )
                if value is None
            ]
            if missing:
                errors.append(
                    {
                        "field": "/".join(missing),
                        "message": "非永久保留策略必须同时提供 retain_duration 与 retain_unit",
                    }
                )
        return errors


class PolicyCreate(PolicyBase):
    pass


class PolicyUpdate(BaseModel):
    """PUT 更新：所有字段显式提供（全量更新）。"""

    description: str | None = Field("", max_length=2000)
    event_type: str | None = None
    tenant_id: str | None = None
    sensitivity: Sensitivity | None = None
    priority: int = Field(100, ge=1, le=10000)
    retain_duration: int | None = Field(None, ge=1, le=100000)
    retain_unit: RetentionUnit | None = None
    keep_forever: bool = False
    action: RetentionAction = RetentionAction.EXPIRE
    enabled: bool = True

    @field_validator("description")
    @classmethod
    def _strip_description(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else (v or "")

    @field_validator("event_type", "tenant_id")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
        return v or None

    def to_policy_base(self, name: str) -> PolicyBase:
        data = self.model_dump()
        # PolicyBase.description 为必填非空字段，None 规范化为空串
        data["description"] = data.get("description") or ""
        return PolicyBase(name=name, **data)


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    event_type: str | None
    tenant_id: str | None
    sensitivity: Sensitivity | None
    priority: int
    retain_duration: int | None
    retain_unit: RetentionUnit | None
    keep_forever: bool
    action: RetentionAction
    enabled: bool
    created_at: datetime
    updated_at: datetime


class MatchedRule(BaseModel):
    """命中规则链中的单条规则及其匹配解释。"""

    rank: int = Field(..., description="在规则链中的名次，1 = 生效策略")
    policy_id: int
    name: str
    priority: int
    specificity_score: int = Field(..., description="精确匹配维度数 (0-3)，越大越具体")
    matched_dimensions: dict[str, str] = Field(
        ..., description="各维度的匹配方式：exact（精确）或 wildcard（通配）"
    )
    retain_duration: int | None
    retain_unit: RetentionUnit | None
    keep_forever: bool
    action: RetentionAction
    explanation: str = Field(..., description="该规则为何命中以及如何命中的中文解释")


class EvaluationResponse(BaseModel):
    """保留策略评估结果。"""

    event: dict[str, Any]
    matched: bool = Field(..., description="是否命中至少一条策略")
    winning_policy_id: int | None
    winning_policy_name: str | None
    expires_at: datetime | None = Field(
        None, description="按生效策略计算出的数据到期时间（UTC，ISO 8601）"
    )
    retain_period: str | None = Field(None, description="人类可读的保留时长，如 90 days")
    action: RetentionAction | None
    keep_forever: bool
    match_count: int
    rule_chain: list[MatchedRule] = Field(
        ..., description="全部命中策略，按匹配度、优先级排序"
    )
    decision_explanation: str = Field(
        ..., description="为什么选择生效策略以及到期时间如何计算"
    )


class ValidationIssue(BaseModel):
    field: str
    message: str


class ValidationResponse(BaseModel):
    """策略校验接口的响应。"""

    valid: bool
    policy: dict[str, Any]
    errors: list[ValidationIssue]
    name_available: bool | None = Field(
        None, description="新建场景下名称是否未被占用；仅在 check_name=true 时返回"
    )

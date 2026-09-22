"""请求/响应模型。

策略匹配条件说明：
- event_type / tenant 给出集合时为“其中之一”匹配；空集合表示该维度不限定（通配）。
- sensitivity 给出集合时表示命中其中之一；min_sensitivity 表示命中等级不低于该值。
- sensitivity 与 min_sensitivity 同时设置时为“且”关系。
- "*" 作为单元素集合时等价于通配（仅做可读性用途）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .retention import parse_period

Action = Literal["delete", "anonymize", "archive", "legal_hold"]
NameStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
DescriptionStr = Annotated[str, StringConstraints(max_length=2000)]


def _normalize_token_list(value: object, *, lower: bool = False) -> list[str]:
    """把字符串/列表归一成去重、去空串的有序列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("必须是字符串或字符串列表")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("列表元素必须是字符串")
        token = item.strip().lower() if lower else item.strip()
        if token and token not in result:
            result.append(token)
    return result


class ConditionSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: list[str] = Field(default_factory=list, description="事件类型集合，空表示不限定")
    tenant: list[str] = Field(default_factory=list, description="租户集合，空表示不限定")
    sensitivity: list[str] = Field(
        default_factory=list, description="敏感级别集合（任一命中即可），空表示不限定"
    )
    min_sensitivity: str | None = Field(
        default=None, description="最低敏感级别（含），与 sensitivity 为且关系"
    )

    @field_validator("event_type", "tenant", mode="before")
    @classmethod
    def _parse_identifier_lists(cls, value: object) -> list[str]:
        return _normalize_token_list(value)

    @field_validator("sensitivity", mode="before")
    @classmethod
    def _parse_sensitivity_list(cls, value: object) -> list[str]:
        return _normalize_token_list(value, lower=True)

    @field_validator("min_sensitivity", mode="before")
    @classmethod
    def _normalize_min_sensitivity(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("min_sensitivity 必须是字符串")
        token = value.strip().lower()
        return token or None


class PolicyBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NameStr
    description: DescriptionStr = ""
    conditions: ConditionSet = Field(default_factory=ConditionSet)
    action: Action = "delete"
    retention_period: str | None = Field(
        default=None,
        description=(
            "保留时长：ISO-8601 时长（P1Y2M30D / PT12H）或简写（90d/6m/1y/12h）；"
            "action=legal_hold 时可省略表示无限期"
        ),
    )
    legal_hold: bool = False
    priority: int = Field(default=100, ge=0, le=1_000_000, description="数值越大优先级越高")
    enabled: bool = True

    @field_validator("retention_period")
    @classmethod
    def _canonicalize_period(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed, _ = parse_period(value)
        if parsed is None:
            raise ValueError(f"无法解析保留时长：{value}（示例：P90D、6m、1y）")
        return parsed.canonical()


class PolicyCreate(PolicyBase):
    """创建策略的请求体。"""


class PolicyRead(PolicyBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class PolicyUpdate(BaseModel):
    """PATCH 更新：仅传入需要修改的字段，conditions 整体替换。"""

    model_config = ConfigDict(extra="forbid")

    name: NameStr | None = None
    description: DescriptionStr | None = None
    conditions: ConditionSet | None = None
    action: Action | None = None
    retention_period: str | None = None
    legal_hold: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1_000_000)
    enabled: bool | None = None

    @field_validator("retention_period")
    @classmethod
    def _canonicalize_period(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed, _ = parse_period(value)
        if parsed is None:
            raise ValueError(f"无法解析保留时长：{value}（示例：P90D、6m、1y）")
        return parsed.canonical()


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: NameStr
    tenant: NameStr
    sensitivity: NameStr
    occurred_at: datetime = Field(
        ..., description="事件发生时间，ISO-8601；不带时区时按 UTC 处理"
    )

    @field_validator("sensitivity")
    @classmethod
    def _lower_sensitivity(cls, value: str) -> str:
        return value.strip().lower()


class RuleHit(BaseModel):
    policy_id: int
    name: str
    priority: int
    specificity: int = Field(description="条件匹配度得分，越高越具体")
    rank: int = Field(description="在候选链中的位次（1 为最终生效规则）")
    selected: bool = Field(description="是否为最终生效（命中链头部）规则")
    action: str
    legal_hold: bool
    retention_period: str | None
    expires_at: datetime | None = Field(description="按该规则计算的到期时间；无限期为 null")
    matched: bool = Field(default=True, description="true=命中，false=未命中（仅解释用）")
    match_detail: dict[str, str] = Field(
        default_factory=dict,
        description="每个维度的匹配解释，如 event_type=exact、tenant=wildcard",
    )
    reason: str = ""


class ResolveResponse(BaseModel):
    event: dict[str, str]
    expires_at: datetime | None = Field(description="最终到期时间；无限期为 null")
    action: str | None = Field(description="最终生效动作；无命中时为 null")
    legal_hold: bool
    retention_period: str | None
    rule_chain: list[RuleHit] = Field(description="命中的规则链，按匹配度/优先级排序，第 1 条生效")
    unmatched: list[RuleHit] = Field(default_factory=list, description="未命中策略的逐维解释")
    explanation: list[str] = Field(description="人类可读的整体裁决解释")
    fallback_applied: bool = Field(description="是否没有任何命中、回退到默认保留期")


class IssueItem(BaseModel):
    code: str
    field: str | None = None
    message: str


class ValidateResponse(BaseModel):
    valid: bool
    canonical_policy: dict | None = Field(
        default=None, description="归一化后的策略（retention_period 已转标准形式）"
    )
    issues: list[IssueItem] = Field(default_factory=list)
    warnings: list[IssueItem] = Field(default_factory=list)


class MessageResponse(BaseModel):
    detail: str

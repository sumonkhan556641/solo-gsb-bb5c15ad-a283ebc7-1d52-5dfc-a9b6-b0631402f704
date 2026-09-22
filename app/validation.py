"""策略语义校验：结构/类型由 Pydantic 负责，本模块负责跨字段与上下文规则。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import Settings
from .retention import parse_period


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    field: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _condition_is_wildcard(conditions: dict[str, Any]) -> bool:
    return not (
        conditions.get("event_type")
        or conditions.get("tenant")
        or conditions.get("sensitivity")
        or conditions.get("min_sensitivity")
    )


def _condition_covers(broader: dict[str, Any], narrower: dict[str, Any]) -> bool:
    """broader 的条件集合是否覆盖 narrower（即 narrower 命中的事件 broader 必命中）。

    空集合代表通配（最大集合）：
    - broader 为空 → 必然覆盖；
    - broader 非空、narrower 为空（通配）→ 不可能覆盖。
    """
    for key in ("event_type", "tenant", "sensitivity"):
        broader_set = set(broader.get(key) or [])
        narrower_set = set(narrower.get(key) or [])
        if not broader_set:
            continue
        if not narrower_set:
            return False
        if not narrower_set.issubset(broader_set):
            return False

    min_broader = broader.get("min_sensitivity")
    min_narrower = narrower.get("min_sensitivity")
    if min_broader:
        # 阈值覆盖：阈值更低（或相等）时覆盖面更大
        from .config import settings as global_settings

        rank = global_settings.sensitivity_rank
        if min_broader not in rank:
            return False
        if not min_narrower:
            # narrower 没有阈值：若它也没限定 sensitivity 列表（即该维度通配），
            # 则 broader 的阈值无法覆盖；否则列表元素必须都 >= broader 阈值
            if not (narrower.get("sensitivity") or []):
                return False
            for level in narrower.get("sensitivity") or []:
                if level not in rank or rank[level] < rank[min_broader]:
                    return False
        else:
            if min_narrower not in rank or rank[min_narrower] < rank[min_broader]:
                return False
    return True


def semantic_validate(
    policy: Any,
    settings: Settings,
    *,
    existing: list[dict[str, Any]] | None = None,
    self_id: int | None = None,
    check_duplicate_name: bool = False,
) -> tuple[list[Issue], list[Issue]]:
    """返回 (errors, warnings)。errors 为空时策略有效。

    policy 可以是 PolicyCreate / PolicyBase / PolicyRead（pydantic 模型）或 dict。
    创建/更新路径依赖数据库唯一约束返回 409，因此 check_duplicate_name
    默认为关闭；仅校验接口开启它做前置提示。
    """
    data = policy.model_dump() if hasattr(policy, "model_dump") else dict(policy)
    conditions = data.get("conditions") or {}
    errors: list[Issue] = []
    warnings: list[Issue] = []

    # 1) 保留时长与动作的一致性
    raw_period = data.get("retention_period")
    if raw_period is not None:
        parsed, error = parse_period(raw_period)
        if parsed is None:
            errors.append(Issue("invalid_retention_period", error, "retention_period"))
        else:
            if data.get("action") == "legal_hold" and not parsed.indefinite:
                warnings.append(
                    Issue(
                        "legal_hold_with_period",
                        "action=legal_hold 通常应配合无限期保留（retention_period=indefinite 或 null）",
                        "retention_period",
                    )
                )
    else:
        if data.get("action") != "legal_hold" and not data.get("legal_hold"):
            errors.append(
                Issue(
                    "missing_retention_period",
                    "非 legal_hold 动作必须给出 retention_period",
                    "retention_period",
                )
            )

    # 2) legal_hold 标志与动作一致性
    if data.get("legal_hold") and data.get("action") != "legal_hold":
        warnings.append(
            Issue(
                "legal_hold_flag_without_action",
                "legal_hold=true 但 action 不是 legal_hold，冻结标志不会阻止自动删除",
                "legal_hold",
            )
        )
    if data.get("action") == "legal_hold" and not data.get("legal_hold"):
        warnings.append(
            Issue(
                "legal_hold_action_without_flag",
                "action=legal_hold 时建议同时设置 legal_hold=true",
                "legal_hold",
            )
        )

    # 3) 敏感级别名称必须在服务配置中
    unknown = [
        level
        for level in conditions.get("sensitivity", [])
        if level not in settings.sensitivity_rank
    ]
    if unknown:
        errors.append(
            Issue(
                "unknown_sensitivity",
                f"未知敏感级别：{'、'.join(unknown)}；已配置等级：{'、'.join(settings.sensitivity_levels)}",
                "conditions.sensitivity",
            )
        )
    minimum = conditions.get("min_sensitivity")
    if minimum and minimum not in settings.sensitivity_rank:
        errors.append(
            Issue(
                "unknown_sensitivity",
                f"未知阈值敏感级别：{minimum}；已配置等级：{'、'.join(settings.sensitivity_levels)}",
                "conditions.min_sensitivity",
            )
        )
    # sensitivity 列表里若所有元素都低于 min_sensitivity，条件永远不可能同时满足
    if conditions.get("sensitivity") and minimum and not unknown:
        rank = settings.sensitivity_rank
        if not any(rank[level] >= rank[minimum] for level in conditions["sensitivity"]):
            errors.append(
                Issue(
                    "unsatisfiable_sensitivity",
                    f"sensitivity 集合中没有任何级别 >= min_sensitivity={minimum}，条件永不成立",
                    "conditions.sensitivity",
                )
            )

    # 4) 与现有策略重名（仅校验接口启用；写入路径由 DB 唯一约束返回 409）
    if check_duplicate_name:
        for other in existing or []:
            if other.get("id") == self_id:
                continue
            if other.get("name") == data.get("name"):
                errors.append(
                    Issue("duplicate_name", f"已存在同名策略：{data.get('name')}", "name")
                )

    # 5) 警告：全通配策略
    if _condition_is_wildcard(conditions):
        warnings.append(
            Issue(
                "wildcard_policy",
                "该策略没有任何条件限定，将命中所有事件；通常只应用作低优先级兜底规则",
                "conditions",
            )
        )

    # 6) 警告：被同优先级或更高优先级、条件覆盖本策略的其他策略遮蔽
    new_priority = data.get("priority", 100)
    for other in existing or []:
        if other.get("id") == self_id or not other.get("enabled", True):
            continue
        if other["priority"] < new_priority:
            continue
        other_conditions = other.get("conditions") or {}
        if not _condition_covers(other_conditions, conditions):
            continue
        # 两条件互相覆盖（等价）且优先级相同时为完全遮蔽；
        # 更宽条件 + 同优先级匹配度更低，不构成遮蔽。
        equivalent = _condition_covers(conditions, other_conditions)
        if other["priority"] > new_priority:
            relation = "条件覆盖本策略且优先级更高，命中事件将永远先生效"
        elif equivalent and other["priority"] == new_priority:
            # 完全等价的策略只对“先存在者”提示一次，避免双向告警
            if self_id is not None and self_id < other["id"]:
                continue
            relation = "条件与优先级都相同，永远同时命中，先创建者会恒先生效"
        else:
            continue
        warnings.append(
            Issue(
                "shadowed_policy",
                f"本策略可能被已有策略 {other['name']!r}(id={other['id']}) 遮蔽：对方{relation}",
                None,
            )
        )

    return errors, warnings

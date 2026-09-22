"""运行期配置：全部通过环境变量注入，提供安全默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# 敏感度等级顺序：索引越大越敏感，min_sensitivity 比较基于该顺序
DEFAULT_SENSITIVITY_LEVELS = ["public", "internal", "confidential", "secret"]


def _parse_levels(raw: str) -> list[str]:
    levels = [item.strip().lower() for item in raw.split(",") if item.strip()]
    if len(levels) < 2:
        raise ValueError("RETENTION_SENSITIVITY_LEVELS 至少需要两个等级")
    if len(set(levels)) != len(levels):
        raise ValueError("RETENTION_SENSITIVITY_LEVELS 中存在重复等级")
    return levels


def _parse_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise ValueError("默认保留天数不能为负数")
    return value


@dataclass(frozen=True)
class Settings:
    """服务配置。"""

    database_path: str
    sensitivity_levels: list[str] = field(default_factory=lambda: list(DEFAULT_SENSITIVITY_LEVELS))
    seed_sample_policies: bool = True
    default_retention_days: int = 90
    timezone_header_name: str = "X-Event-Timezone"

    @property
    def sensitivity_rank(self) -> dict[str, int]:
        return {level: index for index, level in enumerate(self.sensitivity_levels)}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    """从环境变量读取配置；非法值直接抛错，避免带病启动。"""
    levels = _parse_levels(
        os.environ.get("RETENTION_SENSITIVITY_LEVELS", ",".join(DEFAULT_SENSITIVITY_LEVELS))
    )
    default_days = _parse_int(os.environ.get("RETENTION_DEFAULT_DAYS", "90"))
    return Settings(
        database_path=os.environ.get("RETENTION_DB_PATH", "/data/retention.db"),
        sensitivity_levels=levels,
        seed_sample_policies=_env_bool("RETENTION_SEED_SAMPLE", True),
        default_retention_days=default_days,
    )


settings = load_settings()

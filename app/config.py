"""运行期配置。

所有配置均可通过环境变量覆盖，默认值保证开箱即用：

* ``RETENTION_DB_PATH``  —— SQLite 数据库文件路径（目录不存在会自动创建）；
* ``SEED_ON_STARTUP``    —— 数据库为空时是否写入内置示例策略 (true/1/yes)；
* ``RETENTION_TIMEZONE`` —— 仅用于示例/文档展示，时间统一以 UTC 存储与返回。
"""

from __future__ import annotations

import os


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """进程级配置（从环境变量读取，容器里通过 environment / .env 覆盖）。"""

    def __init__(self) -> None:
        self.db_path: str = os.getenv("RETENTION_DB_PATH", "/data/policies.db")
        self.seed_on_startup: bool = _as_bool(os.getenv("SEED_ON_STARTUP"), True)
        self.app_name: str = "Data Retention Policy Service"
        self.default_timezone: str = os.getenv("RETENTION_TIMEZONE", "UTC")


settings = Settings()

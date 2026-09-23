"""FastAPI 应用入口。

启动流程（lifespan）：
1. 创建 SQLite 数据库与表结构（幂等）；
2. 表为空且 ``SEED_ON_STARTUP`` 为真时写入示例策略。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import settings
from .database import connection, init_db
from .routers import evaluation, policies
from .seed import seed_if_empty

logger = logging.getLogger("retention")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.seed_on_startup:
        with connection() as conn:
            inserted = seed_if_empty(conn)
        if inserted:
            logger.info("首次启动：已写入 %d 条示例策略 -> %s", inserted, settings.db_path)
    logger.info("数据保留策略服务就绪，数据库：%s", settings.db_path)
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "数据保留策略评估服务：按事件类型 / 租户 / 敏感级别匹配保留策略，"
        "以匹配度与优先级解析生效规则，计算到期时间，返回规则链与解释。"
        "交互式文档见 /docs，OpenAPI 描述见 /openapi.json。"
    ),
    lifespan=lifespan,
)

app.include_router(evaluation.router)
app.include_router(policies.router)


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.get("/", tags=["meta"], summary="服务简介")
def root() -> dict[str, str]:
    return {
        "service": settings.app_name,
        "docs": "/docs",
        "evaluate": "POST /evaluate",
        "validate": "POST /policies/validate",
        "policies": "GET/POST /policies",
    }

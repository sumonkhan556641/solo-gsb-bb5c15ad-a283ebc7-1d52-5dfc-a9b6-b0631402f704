"""FastAPI 应用入口：依赖与 SQLite 初始化均纳入启动流程。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import db
from .config import settings
from .routers import evaluate, policies

logger = logging.getLogger("retention")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时建库建表（并按需写入种子策略），关闭时释放连接。"""
    logger.info("初始化 SQLite 数据库：%s", settings.database_path)
    app.state.db = db.connect()
    db.init_db(app.state.db)
    logger.info("数据库就绪（敏感度等级：%s）", "、".join(settings.sensitivity_levels))
    try:
        yield
    finally:
        app.state.db.close()
        logger.info("数据库连接已关闭")


app = FastAPI(
    title="数据保留策略评估服务",
    description=(
        "提交事件（类型/租户/敏感级别/发生时间），服务按条件匹配度与优先级解析保留策略，"
        "计算到期时间，返回命中规则链与逐维解释。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(policies.router)
app.include_router(evaluate.router)


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", tags=["meta"], include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "retention-policy", "docs": "/docs", "health": "/health"}

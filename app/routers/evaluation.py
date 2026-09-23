"""数据保留评估接口。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from ..database import get_db
from ..retention import evaluate
from ..schemas import EventIn, EvaluationResponse

router = APIRouter(tags=["evaluation"])


@router.post(
    "/evaluate",
    response_model=EvaluationResponse,
    summary="评估单条事件的保留策略",
    description=(
        "提交事件类型、租户、敏感级别与发生时间；服务匹配全部启用的策略，"
        "按匹配度（精确维度数）与优先级解析出生效规则，计算到期时间，"
        "并返回命中的完整规则链与中文解释。"
    ),
)
def evaluate_event(
    event: EventIn, conn: sqlite3.Connection = Depends(get_db)
) -> EvaluationResponse:
    return evaluate(event, conn)

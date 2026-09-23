"""策略管理与策略校验接口。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..crud import (
    create_policy,
    delete_policy,
    get_policy,
    list_policies,
    name_exists,
    update_policy,
)
from ..database import get_db
from ..schemas import (
    PolicyCreate,
    PolicyOut,
    PolicyUpdate,
    ValidationIssue,
    ValidationResponse,
)

router = APIRouter(prefix="/policies", tags=["policies"])


def _conflict_409(name: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=[{"field": "name", "message": f"策略名称 {name!r} 已存在，名称必须唯一"}],
    )


@router.post(
    "/validate",
    response_model=ValidationResponse,
    status_code=status.HTTP_200_OK,
    summary="校验一份策略（不写入数据库）",
    description=(
        "检查跨字段语义（永久保留/保留时长互斥等）。Pydantic 类型与范围错误"
        "仍以 422 返回；语义错误同样以 422 返回并在 errors 中列出。"
        "check_name=true 时额外检查名称是否已被占用。"
    ),
)
def validate_policy(
    policy: PolicyCreate,
    check_name: bool = Query(
        False, description="是否同时检查 name 在数据库中的唯一性"
    ),
    conn: sqlite3.Connection = Depends(get_db),
) -> ValidationResponse:
    issues = [ValidationIssue(**e) for e in policy.semantic_errors()]
    name_available: bool | None = None
    if check_name and not issues:
        name_available = not name_exists(conn, policy.name)
        if not name_available:
            issues.append(
                ValidationIssue(field="name", message=f"策略名称 {policy.name!r} 已存在")
            )
    response = ValidationResponse(
        valid=not issues,
        policy=policy.model_dump(mode="json"),
        errors=issues,
        name_available=name_available if check_name else None,
    )
    if issues:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=response.model_dump(mode="json")
        )
    return response


@router.get("", response_model=list[PolicyOut], summary="列出全部策略")
def get_policies(conn: sqlite3.Connection = Depends(get_db)) -> list[PolicyOut]:
    return list_policies(conn)


@router.post(
    "",
    response_model=PolicyOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建策略",
)
def post_policy(
    policy: PolicyCreate, conn: sqlite3.Connection = Depends(get_db)
) -> PolicyOut:
    semantic = policy.semantic_errors()
    if semantic:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=semantic
        )
    if name_exists(conn, policy.name):
        raise _conflict_409(policy.name)
    return create_policy(conn, policy)


@router.get("/{policy_id}", response_model=PolicyOut, summary="按 ID 获取策略")
def read_policy(policy_id: int, conn: sqlite3.Connection = Depends(get_db)) -> PolicyOut:
    policy = get_policy(conn, policy_id)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="策略不存在")
    return policy


@router.put("/{policy_id}", response_model=PolicyOut, summary="全量更新策略")
def put_policy(
    policy_id: int,
    payload: PolicyUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> PolicyOut:
    existing = get_policy(conn, policy_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="策略不存在")
    merged = payload.to_policy_base(existing.name)
    semantic = merged.semantic_errors()
    if semantic:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=semantic
        )
    return update_policy(conn, policy_id, merged)  # type: ignore[arg-type]


@router.delete(
    "/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除策略",
)
def remove_policy(policy_id: int, conn: sqlite3.Connection = Depends(get_db)) -> None:
    if not delete_policy(conn, policy_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="策略不存在")

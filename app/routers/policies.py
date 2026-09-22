"""策略管理接口。"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Response, status

from .. import db
from ..deps import get_db
from ..schemas import (
    IssueItem,
    PolicyCreate,
    PolicyRead,
    PolicyUpdate,
    ValidateResponse,
)
from ..validation import semantic_validate
from ..config import settings

router = APIRouter(prefix="/policies", tags=["policies"])


def _issues_to_items(issues) -> list[dict]:
    return [IssueItem(**issue.as_dict()).model_dump() for issue in issues]


def _validate_or_raise(
    payload: PolicyCreate,
    conn: sqlite3.Connection,
    *,
    self_id: int | None = None,
) -> None:
    errors, warnings = semantic_validate(
        payload,
        settings,
        existing=db.list_policies(conn),
        self_id=self_id,
    )
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"issues": _issues_to_items(errors), "warnings": _issues_to_items(warnings)},
        )


@router.get("", response_model=list[PolicyRead], summary="列出全部策略")
def list_all_policies(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return db.list_policies(conn)


@router.post(
    "/validate",
    response_model=ValidateResponse,
    summary="校验策略（不落库）",
)
def validate_policy(payload: PolicyCreate, conn: sqlite3.Connection = Depends(get_db)) -> ValidateResponse:
    """仅做校验，不写数据库；始终返回 200，以 valid 字段表达结论。"""
    errors, warnings = semantic_validate(
        payload,
        settings,
        existing=db.list_policies(conn),
        check_duplicate_name=True,
    )
    return ValidateResponse(
        valid=not errors,
        canonical_policy=payload.model_dump(mode="json") if not errors else None,
        issues=_issues_to_items(errors),
        warnings=_issues_to_items(warnings),
    )


@router.post(
    "",
    response_model=PolicyRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建策略",
)
def create_policy(payload: PolicyCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    _validate_or_raise(payload, conn)
    try:
        return db.create_policy(conn, payload.model_dump(mode="json"))
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"策略名称已存在：{payload.name}",
        ) from exc


@router.get("/{policy_id}", response_model=PolicyRead, summary="获取单条策略")
def read_policy(policy_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    policy = db.get_policy(conn, policy_id)
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="策略不存在")
    return policy


@router.patch("/{policy_id}", response_model=PolicyRead, summary="局部更新策略")
def patch_policy(
    policy_id: int,
    payload: PolicyUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    current = db.get_policy(conn, policy_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="策略不存在")

    fields = payload.model_dump(exclude_unset=True, mode="json")
    if fields:
        merged = {**current, **fields}
        # 用合并后的完整视图做语义校验
        from ..schemas import PolicyBase

        merged_for_validation = {k: v for k, v in merged.items() if k in PolicyBase.model_fields}
        candidate = PolicyBase.model_validate(merged_for_validation)
        errors, _warnings = semantic_validate(
            candidate,
            settings,
            existing=db.list_policies(conn),
            self_id=policy_id,
        )
        if errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"issues": _issues_to_items(errors)},
            )
        try:
            updated = db.update_policy(conn, policy_id, fields)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="策略名称已存在"
            ) from exc
        if updated is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="策略不存在")
        return updated
    return current


@router.delete(
    "/{policy_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除策略",
)
def remove_policy(policy_id: int, conn: sqlite3.Connection = Depends(get_db)) -> Response:
    if not db.delete_policy(conn, policy_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="策略不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)

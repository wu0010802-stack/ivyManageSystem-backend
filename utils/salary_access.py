"""薪資資料存取共用守衛。

業務規則：
- admin / hr 角色為「全員薪資視野」（FULL_SALARY_ROLES）
- 其他持 SALARY_READ 的角色僅可看自己（透過 employee_id 比對）
- 對於跨員工的彙總/匯出（如 finance-summary、exports/overtimes），
  即使持有 SALARY_READ 也須是 admin/hr 角色才能看到逐員金額；否則欄位遮罩。

此模組由 IDOR audit Phase 2 從 api/salary.py 拔出，供 employees.py / employees_docs.py /
reports.py / exports.py 共用，避免「次要 perm 拿到薪資金額」的同型重複漏洞（F-012/13/14/31/36）。
"""

from __future__ import annotations
from typing import Optional, Iterable
from fastapi import HTTPException

# admin / hr 全員薪資視野
FULL_SALARY_ROLES = frozenset({"admin", "hr"})


def has_full_salary_view(current_user: dict) -> bool:
    """admin/hr 一律可看全員薪資金額。"""
    return current_user.get("role") in FULL_SALARY_ROLES


def can_view_salary_of(current_user: dict, target_employee_id: int) -> bool:
    """是否可看指定員工的薪資金額。

    - admin/hr：一律可
    - 其他角色：必須是自己（current_user.employee_id == target_employee_id）
    """
    if has_full_salary_view(current_user):
        return True
    return current_user.get("employee_id") == target_employee_id


def resolve_salary_viewer_employee_id(current_user: dict) -> Optional[int]:
    """回傳 None 表示可看全員（admin/hr）；否則回傳鎖定的 employee_id。

    若身份不明（非 admin/hr 且無 employee_id），raise 403。
    """
    if has_full_salary_view(current_user):
        return None
    viewer = current_user.get("employee_id")
    if viewer is None:
        raise HTTPException(status_code=403, detail="無法識別員工身分，禁止查詢薪資")
    return viewer


def enforce_self_or_full_salary(current_user: dict, target_employee_id: int) -> None:
    """非 admin/hr 查他人薪資 → 403。"""
    viewer = resolve_salary_viewer_employee_id(current_user)
    if viewer is None:
        return
    if viewer != target_employee_id:
        raise HTTPException(status_code=403, detail="僅可查詢本人薪資")


def enforce_full_salary_view(
    current_user: dict, *, detail: str = "此功能僅限 admin/hr 使用"
) -> None:
    """要求 admin/hr 角色（用於跨員工彙總/匯出端點）。非 admin/hr → 403。"""
    if not has_full_salary_view(current_user):
        raise HTTPException(status_code=403, detail=detail)


def mask_dict_fields(row: dict, fields: Iterable[str], *, placeholder=None) -> dict:
    """回傳新 dict，將 fields 中存在的 key 替換為 placeholder。"""
    fields_set = set(fields)
    return {k: (placeholder if k in fields_set else v) for k, v in row.items()}

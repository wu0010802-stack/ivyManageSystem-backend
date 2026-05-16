"""考核員工屬性推斷 helpers — role_group / classroom_id。

從 Employee 物件推斷其 RoleGroup 與所屬班級。提取自 api/appraisal/__init__.py
的 _build_role_resolver / _build_classroom_resolver，讓非 participant 的
員工也能取得對應指標。
"""

from __future__ import annotations

from typing import Optional

from models.appraisal import RoleGroup
from models.employee import Employee


def infer_role_group(employee: Employee) -> RoleGroup:
    """Excel 匯入沿用的推斷邏輯（同 _build_role_resolver）。"""
    if employee.supervisor_role:
        return RoleGroup.SUPERVISOR
    cat = (employee.staff_role_category or "").lower()
    if cat in ("kitchen", "driver"):
        return RoleGroup.COOK
    if cat in ("office",):
        return RoleGroup.STAFF
    if cat in ("assistant_educare",):
        return RoleGroup.ASSISTANT
    if cat in ("head_teacher", "teacher"):
        return RoleGroup.HEAD_TEACHER
    # Why: 未知 staff_role_category 不應默默落入獎金率最高的 HEAD_TEACHER；
    # fallback 改 STAFF 保守處理，避免「資料缺漏 → 獎金浮報」（P1-4 bug-sweep 2026-05-16）。
    return RoleGroup.STAFF


def infer_classroom_id(employee: Employee) -> Optional[int]:
    return employee.classroom_id


__all__ = ["infer_role_group", "infer_classroom_id"]

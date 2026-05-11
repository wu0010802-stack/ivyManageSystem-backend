"""懲處抵扣計算與標記。

語意：每筆懲處在「下一個獎金發放月」一次性從節慶+超額獎金扣減。
扣完即 mark applied，剩餘額度（獎金不足時）不滾入下次。

注意：本階段先實作節慶+超額抵扣。主管紅利月扣未實作（業主未強要求）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from models.disciplinary import (
    ACTION_TYPE_MAJOR,
    ACTION_TYPE_MINOR,
    ACTION_TYPE_WARNING,
    DisciplinaryAction,
)


def resolve_default_amount(action_type: str, bonus_config) -> float:
    """DisciplinaryAction.deduction_amount=0 時的 fallback。"""
    if not bonus_config:
        # 純常數 fallback（業主慣例）
        return {
            ACTION_TYPE_WARNING: 1000.0,
            ACTION_TYPE_MINOR: 3000.0,
            ACTION_TYPE_MAJOR: 0.0,
        }.get(action_type, 0.0)
    mapping = {
        ACTION_TYPE_WARNING: getattr(bonus_config, "warning_deduction", 1000),
        ACTION_TYPE_MINOR: getattr(bonus_config, "minor_offense_deduction", 3000),
        ACTION_TYPE_MAJOR: getattr(bonus_config, "major_offense_deduction", 0),
    }
    return float(mapping.get(action_type, 0) or 0)


def _effective_amount(action: DisciplinaryAction, bonus_config) -> float:
    """個案金額；若為 0 用 BonusConfig 預設。"""
    amount = float(action.deduction_amount or 0)
    if amount > 0:
        return amount
    return resolve_default_amount(action.action_type, bonus_config)


def get_pending_actions(
    session: Session, employee_id: int, until_date
) -> list[DisciplinaryAction]:
    """指定員工在 until_date（含）前所有未抵扣懲處，按發生日排序。"""
    return (
        session.query(DisciplinaryAction)
        .filter(
            DisciplinaryAction.employee_id == employee_id,
            DisciplinaryAction.action_date <= until_date,
            DisciplinaryAction.applied_to_salary_id.is_(None),
        )
        .order_by(DisciplinaryAction.action_date, DisciplinaryAction.id)
        .all()
    )


def compute_total_pending_deduction(
    session: Session,
    employee_id: int,
    until_date,
    bonus_config,
) -> float:
    """總額。發放月計算節慶+超額之後，呼叫此函式取得應扣減值。"""
    actions = get_pending_actions(session, employee_id, until_date)
    return sum(_effective_amount(a, bonus_config) for a in actions)


def apply_deductions(
    session: Session,
    employee_id: int,
    salary_record_id: int,
    until_date,
    available_bonus: float,
    bonus_config,
    *,
    actor: Optional[str] = None,
) -> float:
    """將員工的 pending 懲處依序抵扣 available_bonus，回傳實際抵扣總額。

    若獎金不足以全額抵扣，最後一筆會被截斷（applied_amount < deduction_amount），
    後續 pending 仍標記為 applied 但 applied_amount=0（不滾入下次發放期，業主慣例）。

    Args:
        salary_record_id: 抵扣到的 salary_record id
        available_bonus: 可抵扣總額（通常 = festival + overtime + 主管紅利）
        bonus_config: BonusConfig instance（讀預設金額）
        actor: 操作者 username（寫入 updated_by）
    """
    actions = get_pending_actions(session, employee_id, until_date)
    if not actions:
        return 0.0

    now = datetime.now()
    applied_total = 0.0
    remaining = max(0.0, available_bonus)

    for a in actions:
        target = _effective_amount(a, bonus_config)
        actual = min(target, remaining)
        a.applied_to_salary_id = salary_record_id
        a.applied_at = now
        a.applied_amount = actual
        if actor:
            a.updated_by = actor
        applied_total += actual
        remaining -= actual

    return applied_total

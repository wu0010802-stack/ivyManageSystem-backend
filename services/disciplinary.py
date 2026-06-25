"""懲處抵扣計算與標記。

語意：每筆懲處在「下一個獎金發放月」一次性從節慶+超額獎金扣減。
扣完即 mark applied，剩餘額度（獎金不足時）不滾入下次。

注意：本階段先實作節慶+超額抵扣。主管紅利月扣未實作（業主未強要求）。

merit 類型（嘉獎/小功/大功）為獎勵紀錄（考核加分用），不參與薪資扣款。
"""

from __future__ import annotations

from datetime import datetime
from utils.taipei_time import now_taipei_naive
from typing import Optional

from sqlalchemy.orm import Session

from models.disciplinary import (
    ACTION_TYPE_MAJOR,
    ACTION_TYPE_MINOR,
    ACTION_TYPE_WARNING,
    MERIT_ACTION_TYPES,
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
    """個案金額；若為 0 用 BonusConfig 預設。

    merit 為獎勵紀錄（嘉獎/小功/大功），永不產生薪資扣款；防 HR 誤填金額。
    """
    # merit 類型一律回 0，無論 deduction_amount 填了什麼
    if action.action_type in MERIT_ACTION_TYPES:
        return 0.0
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


def get_deductible_actions(
    session: Session, employee_id: int, until_date, salary_record_id=None
) -> list[DisciplinaryAction]:
    """發放月薪資（重）計算可抵扣的懲處：pending（未抵扣）+ 已抵扣到本 record 者。

    Why（2026-06-25 修補）：發放月薪資首次計算後，懲處被標 applied 綁定到當月
    SalaryRecord；之後該月任何假單/加班/考勤異動會觸發「重算」。若重算只取
    pending（applied_to_salary_id IS NULL），已 applied 的懲處取不到 → 不再扣減
    → festival/overtime 以 raw 覆寫先前 reduced 值 → 懲處被靜默還原、員工多領。

    納入「已抵扣到 salary_record_id」者，使重算可重複扣減同一筆懲處（冪等），且
    不會誤抓別月 record 的懲處（applied_to_salary_id 指向別 record 者排除）。
    salary_record_id=None（首次計算、record 尚未存在）時退化為僅 pending。
    """
    from sqlalchemy import or_

    cond = DisciplinaryAction.applied_to_salary_id.is_(None)
    if salary_record_id is not None:
        cond = or_(cond, DisciplinaryAction.applied_to_salary_id == salary_record_id)
    return (
        session.query(DisciplinaryAction)
        .filter(
            DisciplinaryAction.employee_id == employee_id,
            DisciplinaryAction.action_date <= until_date,
            cond,
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
    # 用 deductible（pending + 已抵扣到本 record）而非僅 pending：重算發放月時
    # 已 applied 的懲處須被重新標記到同一 record（冪等），否則 _adjust 已重新扣減、
    # 此處卻取不到 → applied_amount 不更新、ledger 與實際扣薪脫節。
    actions = get_deductible_actions(session, employee_id, until_date, salary_record_id)
    if not actions:
        return 0.0

    now = now_taipei_naive()
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

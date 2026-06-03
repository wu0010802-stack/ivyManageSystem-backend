"""B5 ⑤a 考勤扣款 attendance_deductions（純計算函式，不寫 settlement）。

年終 Excel「遲到一覽表」的扣款是**獨立定額罰則**（業主 2026-06-02 確認），
**不是** payroll（services/salary/deduction.py）的「按分鐘比例 / 病假半薪 /
未打卡不扣」scheme——那套是月薪扣款，與年終無關，費率不可重用。

定額罰則（費率取最新 active BonusConfig）：
  - 遲到   = -late_deduction_per_time / 次（Excel -50/次）
  - 未打卡 = -missing_punch_deduction_per_time / 次（業主確認納入，Excel -50/次）
            → 與遲到**合併進 late 欄**（settlement 只有 deduction_late 一欄）；
              calc_meta 分開記 late_count / missing_punch_count 供稽核。
  - 事假   = -(approved personal 假時數 /8 天) × personal_leave_deduction_per_day
  - 病假   = -(approved sick 假時數 /8 天) × sick_leave_deduction_per_day（全扣，非半薪）
  - 會議缺席 = -COUNT(MeetingRecord attended=False) × meeting_absence_penalty

白名單（繞過 leave_type enum gap；leave_type 是 String(20) 自由字串）：
  請假**只扣** leave_type IN ('personal','sick')；其餘一律不扣
  （生理假 menstrual / 特休 annual / 產假 maternity / 陪產 paternity /
    補休 compensatory / 育嬰 / 安胎 / 產檢 / 家庭照顧… 全不扣）。

期間（業主確認，對齊 B3/proration）= 民國曆年 Jan–Dec：
  date(cycle.academic_year+1911, 1, 1) ~ date(..., 12, 31)。
  **不是** Excel 表頭的 Feb–Jan。所有 Attendance/LeaveRecord/MeetingRecord
  查詢限定此期間（Attendance.attendance_date / MeetingRecord.meeting_date /
  LeaveRecord.start_date 落在期間內）。

回傳 dataclass（**不寫 settlement**）。所有值皆為**負值或 0**（罰則）。

B7 wiring（sign convention，已對齊 engine.py 查證）：本函式回傳**負值**，
   與 services/year_end/engine.py 既有慣例一致——compute_deduction_total 直接
   Σ 各 DeductionBreakdown 欄（**不取負**，docstring「結果為負或 0」），
   compute_payable_amount 為 subtotal **+** deduction_total（相加非相減）。
   故 B7 把本結果的 late→late_early / personal_leave / sick_leave / meeting
   **直接填入 DeductionBreakdown（不可再取負）**，無 double-negate 風險。
   override（calc_meta deduction_<x>_override 優先）由 B7 處理；
   本函式不處理 override、不讀寫 settlement。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.attendance import Attendance
from models.config import BonusConfig
from models.event import MeetingRecord
from models.leave import LeaveRecord
from models.year_end import YearEndCycle
from services.salary.constants import DEFAULT_MEETING_ABSENCE_PENALTY

logger = logging.getLogger(__name__)

_Q2 = Decimal("0.01")

# 白名單：只扣事假/病假；其餘假別一律不扣（繞過 leave_type enum gap）
_PERSONAL = "personal"
_SICK = "sick"

# 假時數 → 天數換算（每日 8 小時）
_HOURS_PER_DAY = Decimal("8")


def _q2(x) -> Decimal:
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


@dataclass
class AttendanceDeductions:
    """⑤a 考勤扣款推導結果（per-employee，純計算）。

    所有金額皆為**負值或 0**（定額罰則）。欄位對齊 settlement / DeductionBreakdown：
      late          → deduction_late（遲到 + 未打卡合併）
      personal_leave→ deduction_personal_leave
      sick_leave    → deduction_sick_leave
      meeting       → deduction_meeting
    calc_meta 記 late_count / missing_punch_count / personal_days / sick_days /
      meeting_absence_count + 套用費率，供稽核與 grid 顯示。
    """

    late: Decimal = Decimal("0.00")
    personal_leave: Decimal = Decimal("0.00")
    sick_leave: Decimal = Decimal("0.00")
    meeting: Decimal = Decimal("0.00")
    calc_meta: dict = field(default_factory=dict)


def _period_bounds(cycle: YearEndCycle) -> tuple[date, date]:
    """民國曆年 Jan–Dec（對齊 B3/proration）。"""
    year = cycle.academic_year + 1911
    return date(year, 1, 1), date(year, 12, 31)


def _latest_active_bonus_config(db: Session) -> Optional[BonusConfig]:
    return db.scalar(
        select(BonusConfig)
        .where(BonusConfig.is_active.is_(True))
        .order_by(BonusConfig.id.desc())
        .limit(1)
    )


def _rate(cfg: Optional[BonusConfig], attr: str, default) -> Decimal:
    """從 BonusConfig 取費率（None / 無 cfg → default）。"""
    if cfg is None:
        return Decimal(str(default))
    val = getattr(cfg, attr, None)
    return Decimal(str(val)) if val is not None else Decimal(str(default))


def _count_late(db: Session, emp_id: int, start: date, end: date) -> int:
    """遲到次數 = COUNT(Attendance WHERE is_late AND 期間內)。"""
    return (
        db.scalar(
            select(func.count(Attendance.id)).where(
                Attendance.employee_id == emp_id,
                Attendance.is_late.is_(True),
                Attendance.attendance_date >= start,
                Attendance.attendance_date <= end,
            )
        )
        or 0
    )


def _count_missing_punch(db: Session, emp_id: int, start: date, end: date) -> int:
    """未打卡次數 = SUM(is_missing_punch_in) + SUM(is_missing_punch_out) 期間內。

    DB 模型用 boolean 欄 is_missing_punch_in / is_missing_punch_out（非 count 欄；
    task 文字引用的 missing_punch_in_count 是 AttendanceResult parser 物件屬性，
    不是 Attendance 表）。每列貢獻 0/1/2 次。
    """
    rows = db.execute(
        select(Attendance.is_missing_punch_in, Attendance.is_missing_punch_out).where(
            Attendance.employee_id == emp_id,
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
        )
    ).all()
    return sum((1 if r[0] else 0) + (1 if r[1] else 0) for r in rows)


def _leave_hours(
    db: Session, emp_id: int, leave_type: str, start: date, end: date
) -> Decimal:
    """期間內、status='approved'、指定假別的請假總時數。

    跨期假單以 **start_date 落在期間內** 為準（settlement_builder 無 leave-specific
    跨期 helper；以授權 fallback 處理，見 module docstring）。
    """
    total = db.scalar(
        select(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).where(
            LeaveRecord.employee_id == emp_id,
            LeaveRecord.leave_type == leave_type,
            LeaveRecord.status == "approved",
            LeaveRecord.start_date >= start,
            LeaveRecord.start_date <= end,
        )
    )
    return Decimal(str(total or 0))


def _count_meeting_absence(db: Session, emp_id: int, start: date, end: date) -> int:
    """會議缺席次數 = COUNT(MeetingRecord WHERE attended=False AND 期間內)。"""
    return (
        db.scalar(
            select(func.count(MeetingRecord.id)).where(
                MeetingRecord.employee_id == emp_id,
                MeetingRecord.attended.is_(False),
                MeetingRecord.meeting_date >= start,
                MeetingRecord.meeting_date <= end,
            )
        )
        or 0
    )


def _compute(
    db: Session,
    emp_id: int,
    start: date,
    end: date,
    cfg: Optional[BonusConfig],
) -> AttendanceDeductions:
    """共用計算核心（per-employee / batch 皆走此函式）。"""
    late_rate = _rate(cfg, "late_deduction_per_time", 50)
    miss_rate = _rate(cfg, "missing_punch_deduction_per_time", 50)
    personal_rate = _rate(cfg, "personal_leave_deduction_per_day", 500)
    sick_rate = _rate(cfg, "sick_leave_deduction_per_day", 500)
    meeting_penalty = _rate(
        cfg, "meeting_absence_penalty", DEFAULT_MEETING_ABSENCE_PENALTY
    )

    late_count = _count_late(db, emp_id, start, end)
    missing_count = _count_missing_punch(db, emp_id, start, end)
    # 遲到 + 未打卡合併進 late 欄（settlement 只有 deduction_late）
    late_amount = -_q2(
        Decimal(late_count) * late_rate + Decimal(missing_count) * miss_rate
    )

    personal_hours = _leave_hours(db, emp_id, _PERSONAL, start, end)
    personal_days = personal_hours / _HOURS_PER_DAY
    personal_amount = -_q2(personal_days * personal_rate)

    sick_hours = _leave_hours(db, emp_id, _SICK, start, end)
    sick_days = sick_hours / _HOURS_PER_DAY
    sick_amount = -_q2(sick_days * sick_rate)

    meeting_absence = _count_meeting_absence(db, emp_id, start, end)
    meeting_amount = -_q2(Decimal(meeting_absence) * meeting_penalty)

    return AttendanceDeductions(
        late=late_amount,
        personal_leave=personal_amount,
        sick_leave=sick_amount,
        meeting=meeting_amount,
        calc_meta={
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "late_count": late_count,
            "missing_punch_count": missing_count,
            "late_rate": float(late_rate),
            "missing_punch_rate": float(miss_rate),
            # 原始時數（未四捨五入），讓金額可被外部精確重建：
            #   personal_leave = personal_hours / 8 × personal_rate（全精度後 _q2）
            #   sick_leave     = sick_hours     / 8 × sick_rate    （同上）
            "personal_hours": float(personal_hours),
            "personal_days": float(_q2(personal_days)),
            "personal_rate": float(personal_rate),
            "sick_hours": float(sick_hours),
            "sick_days": float(_q2(sick_days)),
            "sick_rate": float(sick_rate),
            "meeting_absence_count": meeting_absence,
            "meeting_penalty": float(meeting_penalty),
        },
    )


def derive_attendance_deductions(
    db: Session, cycle: YearEndCycle, emp
) -> AttendanceDeductions:
    """⑤a per-employee 考勤扣款純計算（主要介面）。

    回傳 AttendanceDeductions（值皆負或 0）。**不寫 settlement、不處理 override**
    （由 B7 在 gather_deductions wiring 處理）。
    """
    start, end = _period_bounds(cycle)
    cfg = _latest_active_bonus_config(db)
    return _compute(db, emp.id, start, end, cfg)


def derive_all_attendance_deductions(
    db: Session, cycle: YearEndCycle, employees: list | None = None
) -> dict[int, AttendanceDeductions]:
    """cycle 級別便利函式（可選；B7 批次用以降低 per-employee call overhead）。

    employees 不傳時計算全體 active 員工。費率與 active BonusConfig 只取一次。
    回 dict[employee_id, AttendanceDeductions]。per-employee 版才是主要介面。
    """
    start, end = _period_bounds(cycle)
    cfg = _latest_active_bonus_config(db)

    if employees is None:
        from models.employee import Employee

        employees = list(
            db.scalars(
                select(Employee).where(Employee.is_active.is_(True))  # noqa: E712
            ).all()
        )

    return {emp.id: _compute(db, emp.id, start, end, cfg) for emp in employees}

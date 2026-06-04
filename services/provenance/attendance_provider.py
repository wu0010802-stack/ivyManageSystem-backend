"""考勤扣款 provenance provider。

value 取自既有 attendance_deductions.derive_attendance_deductions（權威、零漂移），
本 provider 另查逐筆 rows 組 source_records，並組 DerivedValue。

對帳保證：_q2(Σ source_records.amount) == value
  - 遲到/未打卡/會議：每筆 = 定額 → Σ 嚴格相等。
  - 事假/病假：每筆 amount = 原始未進位 -(hours/8 × rate)，Σ 後 _q2 == value
    （引擎是 sum(hours)/8×rate 再 _q2，distributive 後相等）。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.attendance import Attendance
from models.employee import Employee
from models.event import MeetingRecord
from models.leave import LeaveRecord
from models.year_end import YearEndCycle
from schemas.provenance import DerivedValue, SourceRecord
from services.year_end.auto_derive.attendance_deductions import (
    derive_attendance_deductions,
)

_Q2 = Decimal("0.01")
_HOURS_PER_DAY = Decimal("8")


def _q2(x) -> Decimal:
    return Decimal(str(x)).quantize(_Q2, rounding=ROUND_HALF_UP)


def _late_records(
    db: Session,
    emp_id: int,
    start: date,
    end: date,
    late_rate: Decimal,
    miss_rate: Decimal,
) -> list[SourceRecord]:
    # 遲到用 late_rate、未打卡用 miss_rate（兩費率可不同 → 保 Σ == 引擎 value）
    out: list[SourceRecord] = []
    rows = db.execute(
        select(
            Attendance.id,
            Attendance.attendance_date,
            Attendance.is_late,
            Attendance.is_missing_punch_in,
            Attendance.is_missing_punch_out,
        ).where(
            Attendance.employee_id == emp_id,
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
        )
    ).all()
    for r in rows:
        if r.is_late:
            out.append(
                SourceRecord(
                    date=r.attendance_date,
                    label="遲到",
                    amount=-late_rate,
                    module="attendance",
                    source_id=r.id,
                )
            )
        if r.is_missing_punch_in:
            out.append(
                SourceRecord(
                    date=r.attendance_date,
                    label="未打卡(上班)",
                    amount=-miss_rate,
                    module="attendance",
                    source_id=r.id,
                )
            )
        if r.is_missing_punch_out:
            out.append(
                SourceRecord(
                    date=r.attendance_date,
                    label="未打卡(下班)",
                    amount=-miss_rate,
                    module="attendance",
                    source_id=r.id,
                )
            )
    return out


def _leave_records(
    db: Session,
    emp_id: int,
    leave_type: str,
    label: str,
    start: date,
    end: date,
    rate: Decimal,
) -> list[SourceRecord]:
    out: list[SourceRecord] = []
    rows = db.execute(
        select(LeaveRecord.id, LeaveRecord.start_date, LeaveRecord.leave_hours).where(
            LeaveRecord.employee_id == emp_id,
            LeaveRecord.leave_type == leave_type,
            LeaveRecord.status == "approved",
            LeaveRecord.start_date >= start,
            LeaveRecord.start_date <= end,
        )
    ).all()
    for r in rows:
        hours = Decimal(str(r.leave_hours or 0))
        raw = -(hours / _HOURS_PER_DAY * rate)  # 未進位，保 Σ 後 _q2 == value
        out.append(
            SourceRecord(
                date=r.start_date,
                label=f"{label} {hours}h",
                amount=raw,
                module="leave",
                source_id=r.id,
            )
        )
    return out


def _meeting_records(
    db: Session,
    emp_id: int,
    start: date,
    end: date,
    penalty: Decimal,
) -> list[SourceRecord]:
    out: list[SourceRecord] = []
    rows = db.execute(
        select(MeetingRecord.id, MeetingRecord.meeting_date).where(
            MeetingRecord.employee_id == emp_id,
            MeetingRecord.attended.is_(False),
            MeetingRecord.meeting_date >= start,
            MeetingRecord.meeting_date <= end,
        )
    ).all()
    for r in rows:
        out.append(
            SourceRecord(
                date=r.meeting_date,
                label="會議缺席",
                amount=-penalty,
                module="meeting",
                source_id=r.id,
            )
        )
    return out


def _dv(
    key: str,
    value: Decimal,
    summary: str,
    breakdown: dict,
    records: list[SourceRecord],
    deep_link: str,
) -> DerivedValue:
    if not records and value == 0:
        summary = "無紀錄"
    return DerivedValue(
        key=key,
        value=value,
        formula_summary=summary,
        breakdown=breakdown,
        source_records=records,
        deep_link=deep_link,
    )


def derive_attendance_provenance(
    db: Session, cycle: YearEndCycle, emp: Employee
) -> dict[str, DerivedValue]:
    """回傳 {key -> DerivedValue}：attendance_late / personal_leave / sick_leave /
    meeting_absence。value 來自既有引擎（零漂移），source_records 為逐筆。"""
    base = derive_attendance_deductions(db, cycle, emp)
    m = base.calc_meta
    start = date.fromisoformat(m["period_start"])
    end = date.fromisoformat(m["period_end"])
    late_rate = Decimal(str(m["late_rate"]))
    miss_rate = Decimal(str(m["missing_punch_rate"]))
    personal_rate = Decimal(str(m["personal_rate"]))
    sick_rate = Decimal(str(m["sick_rate"]))
    meeting_penalty = Decimal(str(m["meeting_penalty"]))
    dl = f"/attendance?employee_id={emp.id}&start={start.isoformat()}&end={end.isoformat()}"
    leave_dl = (
        f"/leaves?employee_id={emp.id}&start={start.isoformat()}&end={end.isoformat()}"
    )

    late_recs = _late_records(db, emp.id, start, end, late_rate, miss_rate)
    personal_recs = _leave_records(
        db, emp.id, "personal", "事假", start, end, personal_rate
    )
    sick_recs = _leave_records(db, emp.id, "sick", "病假", start, end, sick_rate)
    meeting_recs = _meeting_records(db, emp.id, start, end, meeting_penalty)

    return {
        "attendance_late": _dv(
            "attendance_late",
            base.late,
            f"遲到 {m['late_count']} 次 × −{late_rate} + 未打卡 {m['missing_punch_count']} 次 × −{miss_rate} · {m['period_start']}~{m['period_end']}",
            {
                "late_count": m["late_count"],
                "missing_punch_count": m["missing_punch_count"],
                "late_rate": m["late_rate"],
                "missing_punch_rate": m["missing_punch_rate"],
            },
            late_recs,
            dl,
        ),
        "personal_leave": _dv(
            "personal_leave",
            base.personal_leave,
            f"事假 {m['personal_days']} 天 × −{personal_rate}",
            {
                "personal_days": m["personal_days"],
                "personal_rate": m["personal_rate"],
            },
            personal_recs,
            leave_dl,
        ),
        "sick_leave": _dv(
            "sick_leave",
            base.sick_leave,
            f"病假 {m['sick_days']} 天 × −{sick_rate}",
            {
                "sick_days": m["sick_days"],
                "sick_rate": m["sick_rate"],
            },
            sick_recs,
            leave_dl,
        ),
        "meeting_absence": _dv(
            "meeting_absence",
            base.meeting,
            f"會議缺席 {m['meeting_absence_count']} 次 × −{meeting_penalty}",
            {"meeting_absence_count": m["meeting_absence_count"]},
            meeting_recs,
            dl,
        ),
    }

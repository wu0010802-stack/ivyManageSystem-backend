"""api/parent_portal/attendance.py — 家長端學生出席查詢。

- GET /api/parent/attendance/daily：單日出席
- GET /api/parent/attendance/monthly：單月出席（按日清單 + 各狀態統計）

兩端點皆強制經過 _assert_student_owned，禁止跨家長存取。
"""

import calendar
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import StudentAttendance, get_session
from utils.auth import require_parent_role

from ._shared import _assert_student_owned

router = APIRouter(prefix="/attendance", tags=["parent-attendance"])

_VALID_STATUSES = ["出席", "缺席", "病假", "事假", "遲到"]


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="日期格式錯誤，請使用 YYYY-MM-DD")


@router.get("/daily")
def get_daily_attendance(
    student_id: int = Query(..., gt=0),
    target_date: Optional[str] = Query(
        None, alias="date", description="YYYY-MM-DD；不填則為今天"
    ),
    current_user: dict = Depends(require_parent_role()),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _assert_student_owned(session, user_id, student_id)
        d = _parse_date(target_date) if target_date else date.today()
        record = (
            session.query(StudentAttendance)
            .filter(
                StudentAttendance.student_id == student_id,
                StudentAttendance.date == d,
            )
            .first()
        )
        if record is None:
            return {
                "student_id": student_id,
                "date": d.isoformat(),
                "status": None,
                "remark": None,
            }
        return {
            "student_id": student_id,
            "date": d.isoformat(),
            "status": record.status,
            "remark": record.remark,
        }
    finally:
        session.close()


@router.get("/monthly")
def get_monthly_attendance(
    student_id: int = Query(..., gt=0),
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(require_parent_role()),
):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _assert_student_owned(session, user_id, student_id)
        first_day = date(year, month, 1)
        last_day = date(year, month, calendar.monthrange(year, month)[1])
        records = (
            session.query(StudentAttendance)
            .filter(
                StudentAttendance.student_id == student_id,
                StudentAttendance.date >= first_day,
                StudentAttendance.date <= last_day,
            )
            .order_by(StudentAttendance.date.asc())
            .all()
        )
        items = []
        counts = {s: 0 for s in _VALID_STATUSES}
        for r in records:
            items.append(
                {
                    "date": r.date.isoformat() if r.date else None,
                    "status": r.status,
                    "remark": r.remark,
                }
            )
            if r.status in counts:
                counts[r.status] += 1
        return {
            "student_id": student_id,
            "year": year,
            "month": month,
            "items": items,
            "counts": counts,
            "recorded_days": len(items),
        }
    finally:
        session.close()

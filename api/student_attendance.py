"""
Student attendance router — 學生每日出席紀錄
"""

import logging
from datetime import datetime, date as date_type
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from models.database import get_session, Student, StudentAttendance
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["student-attendance"])

VALID_STATUSES = {"出席", "缺席", "病假", "事假", "遲到"}


# ============ Pydantic Models ============

class AttendanceEntry(BaseModel):
    student_id: int
    status: str = "出席"
    remark: Optional[str] = None


class BatchSaveRequest(BaseModel):
    date: str
    entries: List[AttendanceEntry]


# ============ Routes ============

@router.get("/student-attendance")
async def get_daily_attendance(
    date: str = Query(..., description="YYYY-MM-DD"),
    classroom_id: int = Query(...),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """取得指定日期與班級的出席清單，未點名的學生也會回傳（status=None）"""
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式錯誤，請使用 YYYY-MM-DD")

    session = get_session()
    try:
        students = (
            session.query(Student)
            .filter(Student.classroom_id == classroom_id, Student.is_active == True)
            .order_by(Student.student_id)
            .all()
        )

        existing = {
            r.student_id: r
            for r in session.query(StudentAttendance)
            .filter(
                StudentAttendance.date == target_date,
                StudentAttendance.student_id.in_([s.id for s in students]),
            )
            .all()
        }

        result = []
        for s in students:
            rec = existing.get(s.id)
            result.append({
                "student_id": s.id,
                "student_no": s.student_id,
                "name": s.name,
                "status": rec.status if rec else None,
                "remark": rec.remark if rec else None,
            })

        return {"date": date, "classroom_id": classroom_id, "records": result}
    finally:
        session.close()


@router.post("/student-attendance/batch")
async def batch_save_attendance(
    payload: BatchSaveRequest,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_WRITE)),
):
    """批量儲存（upsert）一個日期的出席記錄"""
    try:
        target_date = datetime.strptime(payload.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式錯誤，請使用 YYYY-MM-DD")

    invalid = [e.status for e in payload.entries if e.status not in VALID_STATUSES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"無效的出席狀態：{invalid}")

    user_id = current_user.get("id")
    session = get_session()
    try:
        existing = {
            r.student_id: r
            for r in session.query(StudentAttendance)
            .filter(
                StudentAttendance.date == target_date,
                StudentAttendance.student_id.in_([e.student_id for e in payload.entries]),
            )
            .all()
        }

        for entry in payload.entries:
            if entry.student_id in existing:
                rec = existing[entry.student_id]
                rec.status = entry.status
                rec.remark = entry.remark
                rec.recorded_by = user_id
            else:
                rec = StudentAttendance(
                    student_id=entry.student_id,
                    date=target_date,
                    status=entry.status,
                    remark=entry.remark,
                    recorded_by=user_id,
                )
                session.add(rec)

        session.commit()
        logger.info("學生出席批量儲存：date=%s count=%d operator=%s",
                    payload.date, len(payload.entries), current_user.get("username"))
        return {"message": "儲存成功", "saved": len(payload.entries)}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"儲存失敗: {str(e)}")
    finally:
        session.close()


@router.get("/student-attendance/monthly")
async def get_monthly_summary(
    classroom_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """取得班級整月出席統計（每位學生各狀態次數）"""
    from calendar import monthrange
    _, days_in_month = monthrange(year, month)
    start = date_type(year, month, 1)
    end = date_type(year, month, days_in_month)

    session = get_session()
    try:
        students = (
            session.query(Student)
            .filter(Student.classroom_id == classroom_id, Student.is_active == True)
            .order_by(Student.student_id)
            .all()
        )
        student_ids = [s.id for s in students]

        records = (
            session.query(StudentAttendance)
            .filter(
                StudentAttendance.student_id.in_(student_ids),
                StudentAttendance.date >= start,
                StudentAttendance.date <= end,
            )
            .all()
        )

        # 統計各學生各狀態次數
        counts: dict[int, dict[str, int]] = {s.id: {} for s in students}
        for r in records:
            counts[r.student_id][r.status] = counts[r.student_id].get(r.status, 0) + 1

        result = []
        for s in students:
            c = counts[s.id]
            result.append({
                "student_id": s.id,
                "student_no": s.student_id,
                "name": s.name,
                "出席": c.get("出席", 0),
                "缺席": c.get("缺席", 0),
                "病假": c.get("病假", 0),
                "事假": c.get("事假", 0),
                "遲到": c.get("遲到", 0),
                "未點名": days_in_month - sum(c.values()),
            })

        return {
            "year": year,
            "month": month,
            "classroom_id": classroom_id,
            "days_in_month": days_in_month,
            "students": result,
        }
    finally:
        session.close()

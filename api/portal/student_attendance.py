"""
Portal - 教師學生點名端點
"""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from openpyxl import Workbook
from pydantic import BaseModel

from models.database import get_session, Student, StudentAttendance
from services.student_attendance_report import (
    build_monthly_attendance_report,
    invalidate_student_attendance_report_caches,
)
from utils.auth import get_current_user
from ._shared import _get_employee
from .incidents import _get_teacher_classroom_ids
from api.exports import _export_rate_limit, _to_response
from api.student_attendance import _fetch_class_data, _write_class_sheet

logger = logging.getLogger(__name__)

router = APIRouter()

VALID_STATUSES = {"出席", "缺席", "病假", "事假", "遲到"}


# ============ Pydantic Models ============

class AttendanceEntryPortal(BaseModel):
    student_id: int
    status: str = "出席"
    remark: Optional[str] = None


class BatchSaveRequestPortal(BaseModel):
    date: str
    classroom_id: int
    entries: List[AttendanceEntryPortal]


# ============ Routes ============

@router.get("/my-class-attendance")
def get_my_class_attendance(
    date: str = Query(..., description="YYYY-MM-DD"),
    classroom_id: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """教師取得自己班級指定日期的出席清單，未點名的學生也會回傳（status=None）"""
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式錯誤，請使用 YYYY-MM-DD")

    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        if classroom_id not in classroom_ids:
            raise HTTPException(status_code=403, detail="無權查看此班級的點名資料")

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

        records = []
        for s in students:
            rec = existing.get(s.id)
            records.append({
                "student_id": s.id,
                "student_no": s.student_id,
                "name": s.name,
                "status": rec.status if rec else None,
                "remark": rec.remark if rec else None,
            })

        return {"date": date, "classroom_id": classroom_id, "records": records}
    finally:
        session.close()


@router.post("/class-attendance/batch")
def batch_save_class_attendance(
    payload: BatchSaveRequestPortal,
    current_user: dict = Depends(get_current_user),
):
    """教師批量儲存（upsert）班級一個日期的出席記錄"""
    try:
        target_date = datetime.strptime(payload.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式錯誤，請使用 YYYY-MM-DD")

    invalid_statuses = [e.status for e in payload.entries if e.status not in VALID_STATUSES]
    if invalid_statuses:
        raise HTTPException(status_code=400, detail=f"無效的出席狀態：{invalid_statuses}")

    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        if payload.classroom_id not in classroom_ids:
            raise HTTPException(status_code=403, detail="無權操作此班級的點名資料")

        # 驗證所有 student_id 皆屬於該班級（防跨班操作）
        entry_student_ids = [e.student_id for e in payload.entries]
        valid_students = session.query(Student.id).filter(
            Student.id.in_(entry_student_ids),
            Student.classroom_id == payload.classroom_id,
            Student.is_active == True,
        ).all()
        valid_ids = {s.id for s in valid_students}
        unauthorized = [sid for sid in entry_student_ids if sid not in valid_ids]
        if unauthorized:
            raise HTTPException(status_code=403, detail=f"以下學生不屬於該班級：{unauthorized}")

        user_id = current_user.get("id")
        existing = {
            r.student_id: r
            for r in session.query(StudentAttendance)
            .filter(
                StudentAttendance.date == target_date,
                StudentAttendance.student_id.in_(entry_student_ids),
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
        invalidate_student_attendance_report_caches(session)
        logger.info(
            "教師學生點名儲存：emp=%s classroom_id=%d date=%s count=%d",
            emp.name, payload.classroom_id, payload.date, len(payload.entries),
        )
        return {"message": "儲存成功", "saved": len(payload.entries)}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="儲存失敗")
    finally:
        session.close()


@router.get("/my-class-attendance/monthly")
def get_my_class_attendance_monthly(
    classroom_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    current_user: dict = Depends(get_current_user),
):
    """教師取得班級整月出席統計、出席率與連缺告警。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        if classroom_id not in classroom_ids:
            raise HTTPException(status_code=403, detail="無權查看此班級的統計資料")

        return build_monthly_attendance_report(session, classroom_id, year, month)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    finally:
        session.close()


@router.get("/my-class-attendance/export")
def export_my_class_attendance(
    classroom_id: int = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    _rl=Depends(_export_rate_limit),
    current_user: dict = Depends(get_current_user),
):
    """教師匯出自己班級的月出席 Excel。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        if classroom_id not in classroom_ids:
            raise HTTPException(status_code=403, detail="無權匯出此班級的出席資料")

        # 取得班級名稱（供 sheet 標題使用）
        from models.database import Classroom
        cr = session.query(Classroom).filter(Classroom.id == classroom_id).first()
        classroom_name = cr.name if cr else str(classroom_id)

        report_data = _fetch_class_data(session, classroom_id, year, month)

        wb = Workbook()
        ws_raw = wb.active
        ws_raw.title = classroom_name[:31]
        _write_class_sheet(ws_raw, report_data, year, month)

        logger.info(
            "教師匯出學生出席月報：emp=%s classroom_id=%d year=%d month=%d",
            emp.name, classroom_id, year, month,
        )
        filename = f"{year}年{month}月_{classroom_name}_出席月報.xlsx"
        return _to_response(wb, filename)
    finally:
        session.close()

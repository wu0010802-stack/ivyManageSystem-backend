"""
api/student_change_logs.py — 學生異動紀錄 CRUD 端點
"""

import csv
import io
import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import or_

from models.database import get_session, Student, Classroom
from models.student_log import StudentChangeLog, CHANGE_LOG_REASON_OPTIONS, EVENT_TYPES
from utils.academic import resolve_academic_term_filters
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.portfolio_access import (
    assert_student_access,
    student_ids_in_scope,
)

logger = logging.getLogger(__name__)

EXPORT_MAX_ROWS = 5000


def _csv_safe(value) -> str:
    """防 CSV injection：若字串開頭為 =, +, -, @, Tab, CR，前綴單引號。"""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


router = APIRouter(prefix="/api/students/change-logs", tags=["student-change-logs"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChangeLogCreate(BaseModel):
    student_id: int
    school_year: int
    semester: int
    event_type: str
    event_date: str  # "YYYY-MM-DD"
    classroom_id: Optional[int] = None
    from_classroom_id: Optional[int] = None
    to_classroom_id: Optional[int] = None
    reason: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v):
        if v not in EVENT_TYPES:
            raise ValueError(f"event_type 必須為 {EVENT_TYPES} 其中之一")
        return v

    @field_validator("event_date")
    @classmethod
    def validate_date(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("event_date 格式必須為 YYYY-MM-DD")
        return v

    @field_validator("school_year")
    @classmethod
    def validate_school_year(cls, v):
        if not (100 <= v <= 200):
            raise ValueError("school_year 應為民國年（100~200）")
        return v

    @field_validator("semester")
    @classmethod
    def validate_semester(cls, v):
        if v not in (1, 2):
            raise ValueError("semester 必須為 1 或 2")
        return v


class ChangeLogUpdate(BaseModel):
    event_type: Optional[str] = None
    event_date: Optional[str] = None
    classroom_id: Optional[int] = None
    from_classroom_id: Optional[int] = None
    to_classroom_id: Optional[int] = None
    reason: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v):
        if v is not None and v not in EVENT_TYPES:
            raise ValueError(f"event_type 必須為 {EVENT_TYPES} 其中之一")
        return v

    @field_validator("event_date")
    @classmethod
    def validate_date(cls, v):
        if v is not None:
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                raise ValueError("event_date 格式必須為 YYYY-MM-DD")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_log(
    log: StudentChangeLog,
    student_name: str,
    classroom_name: str,
    from_classroom_name: Optional[str] = None,
    to_classroom_name: Optional[str] = None,
) -> dict:
    return {
        "id": log.id,
        "student_id": log.student_id,
        "student_name": student_name,
        "school_year": log.school_year,
        "semester": log.semester,
        "event_type": log.event_type,
        "event_date": log.event_date.isoformat() if log.event_date else None,
        "classroom_id": log.classroom_id,
        "classroom_name": classroom_name,
        "from_classroom_id": log.from_classroom_id,
        "from_classroom_name": from_classroom_name,
        "to_classroom_id": log.to_classroom_id,
        "to_classroom_name": to_classroom_name,
        "reason": log.reason,
        "notes": log.notes,
        "recorded_by": log.recorded_by,
        "source": log.source or "manual",
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/options")
async def get_change_log_options(
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得 event_type 與對應 reason 選項（供前端下拉）"""
    return {
        "event_types": EVENT_TYPES,
        "reason_options": CHANGE_LOG_REASON_OPTIONS,
    }


@router.get("/summary")
async def get_change_logs_summary(
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    classroom_id: Optional[int] = Query(
        None,
        description="同時比對 classroom_id / from_classroom_id / to_classroom_id（語意：與此班級相關的所有異動）",
    ),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得指定學期各異動類型數量統計（可選以班級維度過濾）"""
    resolved_year, resolved_semester = resolve_academic_term_filters(
        school_year, semester
    )
    session = get_session()
    try:
        query = session.query(StudentChangeLog).filter(
            StudentChangeLog.school_year == resolved_year,
            StudentChangeLog.semester == resolved_semester,
        )
        if classroom_id:
            query = query.filter(
                or_(
                    StudentChangeLog.classroom_id == classroom_id,
                    StudentChangeLog.from_classroom_id == classroom_id,
                    StudentChangeLog.to_classroom_id == classroom_id,
                )
            )
        # F-022：非 admin/hr/supervisor 一律以 student_ids_in_scope 限縮
        scope = student_ids_in_scope(session, current_user)
        if scope is not None:
            if not scope:
                return {
                    "school_year": resolved_year,
                    "semester": resolved_semester,
                    "classroom_id": classroom_id,
                    "summary": {et: 0 for et in EVENT_TYPES},
                    "total": 0,
                }
            query = query.filter(StudentChangeLog.student_id.in_(scope))

        logs = query.all()
        summary = {et: 0 for et in EVENT_TYPES}
        for log in logs:
            if log.event_type in summary:
                summary[log.event_type] += 1

        return {
            "school_year": resolved_year,
            "semester": resolved_semester,
            "classroom_id": classroom_id,
            "summary": summary,
            "total": len(logs),
        }
    finally:
        session.close()


@router.get("")
async def get_change_logs(
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    event_type: Optional[List[str]] = Query(None),
    classroom_id: Optional[int] = Query(
        None,
        description="同時比對 classroom_id / from_classroom_id / to_classroom_id（語意：與此班級相關的所有異動）",
    ),
    student_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得學生異動紀錄列表（分頁）"""
    resolved_year, resolved_semester = resolve_academic_term_filters(
        school_year, semester
    )
    session = get_session()
    try:
        query = session.query(StudentChangeLog).filter(
            StudentChangeLog.school_year == resolved_year,
            StudentChangeLog.semester == resolved_semester,
        )
        if event_type:
            query = query.filter(StudentChangeLog.event_type.in_(event_type))
        if classroom_id:
            query = query.filter(
                or_(
                    StudentChangeLog.classroom_id == classroom_id,
                    StudentChangeLog.from_classroom_id == classroom_id,
                    StudentChangeLog.to_classroom_id == classroom_id,
                )
            )
        if student_id:
            query = query.filter(StudentChangeLog.student_id == student_id)
        # F-022：非 admin/hr/supervisor 一律以 student_ids_in_scope 限縮
        scope = student_ids_in_scope(session, current_user)
        if scope is not None:
            if not scope:
                return {
                    "items": [],
                    "total": 0,
                    "page": page,
                    "page_size": page_size,
                    "school_year": resolved_year,
                    "semester": resolved_semester,
                    "classroom_id": classroom_id,
                }
            query = query.filter(StudentChangeLog.student_id.in_(scope))

        total = query.count()
        logs = (
            query.order_by(
                StudentChangeLog.event_date.desc(), StudentChangeLog.id.desc()
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        # 批次取得學生姓名與班級名稱（含 from / to）
        student_ids = {log.student_id for log in logs}
        classroom_ids = set()
        for log in logs:
            for cid in (log.classroom_id, log.from_classroom_id, log.to_classroom_id):
                if cid:
                    classroom_ids.add(cid)

        students_map = (
            {
                s.id: s.name
                for s in session.query(Student)
                .filter(Student.id.in_(student_ids))
                .all()
            }
            if student_ids
            else {}
        )
        classrooms_map = (
            {
                c.id: c.name
                for c in session.query(Classroom)
                .filter(Classroom.id.in_(classroom_ids))
                .all()
            }
            if classroom_ids
            else {}
        )

        items = [
            _serialize_log(
                log,
                students_map.get(log.student_id, ""),
                classrooms_map.get(log.classroom_id, ""),
                from_classroom_name=(
                    classrooms_map.get(log.from_classroom_id)
                    if log.from_classroom_id
                    else None
                ),
                to_classroom_name=(
                    classrooms_map.get(log.to_classroom_id)
                    if log.to_classroom_id
                    else None
                ),
            )
            for log in logs
        ]

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "school_year": resolved_year,
            "semester": resolved_semester,
            "classroom_id": classroom_id,
        }
    finally:
        session.close()


@router.get("/export")
def export_change_logs(
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    event_type: Optional[List[str]] = Query(None),
    classroom_id: Optional[int] = Query(
        None,
        description="同時比對 classroom_id / from_classroom_id / to_classroom_id",
    ),
    student_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """匯出異動紀錄為 CSV，上限 5000 筆。"""
    resolved_year, resolved_semester = resolve_academic_term_filters(
        school_year, semester
    )
    session = get_session()
    try:
        query = session.query(StudentChangeLog).filter(
            StudentChangeLog.school_year == resolved_year,
            StudentChangeLog.semester == resolved_semester,
        )
        if event_type:
            query = query.filter(StudentChangeLog.event_type.in_(event_type))
        if classroom_id:
            query = query.filter(
                or_(
                    StudentChangeLog.classroom_id == classroom_id,
                    StudentChangeLog.from_classroom_id == classroom_id,
                    StudentChangeLog.to_classroom_id == classroom_id,
                )
            )
        if student_id:
            query = query.filter(StudentChangeLog.student_id == student_id)
        # F-022：非 admin/hr/supervisor 一律以 student_ids_in_scope 限縮
        scope = student_ids_in_scope(session, current_user)
        if scope is not None:
            if not scope:
                buf = io.StringIO()
                buf.write("﻿")
                writer = csv.writer(buf)
                writer.writerow(
                    [
                        "異動日期",
                        "學生姓名",
                        "學生編號",
                        "異動類型",
                        "現班",
                        "原班",
                        "新班",
                        "原因",
                        "備註",
                        "建立時間",
                    ]
                )
                filename = (
                    f"student_change_logs_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                )
                return StreamingResponse(
                    iter([buf.getvalue()]),
                    media_type="text/csv; charset=utf-8",
                    headers={
                        "Content-Disposition": f'attachment; filename="{filename}"'
                    },
                )
            query = query.filter(StudentChangeLog.student_id.in_(scope))

        total = query.count()
        if total > EXPORT_MAX_ROWS:
            raise HTTPException(
                status_code=400,
                detail=f"符合條件的紀錄有 {total} 筆，超過匯出上限 {EXPORT_MAX_ROWS} 筆，請縮小篩選範圍",
            )

        logs = query.order_by(
            StudentChangeLog.event_date.desc(), StudentChangeLog.id.desc()
        ).all()

        student_ids = {log.student_id for log in logs}
        classroom_ids = set()
        for log in logs:
            for cid in (log.classroom_id, log.from_classroom_id, log.to_classroom_id):
                if cid:
                    classroom_ids.add(cid)

        students_map = (
            {
                s.id: (s.name, s.student_id)
                for s in session.query(Student)
                .filter(Student.id.in_(student_ids))
                .all()
            }
            if student_ids
            else {}
        )
        classrooms_map = (
            {
                c.id: c.name
                for c in session.query(Classroom)
                .filter(Classroom.id.in_(classroom_ids))
                .all()
            }
            if classroom_ids
            else {}
        )

        buf = io.StringIO()
        buf.write("\ufeff")  # Excel 開 UTF-8 CSV 需要 BOM
        writer = csv.writer(buf)
        writer.writerow(
            [
                "異動日期",
                "學生姓名",
                "學生編號",
                "異動類型",
                "現班",
                "原班",
                "新班",
                "原因",
                "備註",
                "建立時間",
            ]
        )
        for log in logs:
            student_name, student_code = students_map.get(log.student_id, ("", ""))
            writer.writerow(
                [
                    _csv_safe(log.event_date.isoformat() if log.event_date else ""),
                    _csv_safe(student_name),
                    _csv_safe(student_code),
                    _csv_safe(log.event_type),
                    _csv_safe(classrooms_map.get(log.classroom_id, "")),
                    _csv_safe(
                        classrooms_map.get(log.from_classroom_id, "")
                        if log.from_classroom_id
                        else ""
                    ),
                    _csv_safe(
                        classrooms_map.get(log.to_classroom_id, "")
                        if log.to_classroom_id
                        else ""
                    ),
                    _csv_safe(log.reason),
                    _csv_safe(log.notes),
                    _csv_safe(
                        log.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        if log.created_at
                        else ""
                    ),
                ]
            )

        filename = f"student_change_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        session.close()


@router.post("", status_code=201)
async def create_change_log(
    item: ChangeLogCreate,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """手動補登學生異動紀錄（僅寫入稽核紀錄，不會改學生狀態）。

    - 強制 source='manual'
    - 限制 event_date <= 今天：未來事件請走 /students/{id}/lifecycle
    """
    session = get_session()
    try:
        # F-022：直接 assert_student_access — 學生不存在 → 404；無權 → 403
        student = assert_student_access(session, current_user, item.student_id)

        event_date = datetime.strptime(item.event_date, "%Y-%m-%d").date()
        if event_date > date.today():
            raise HTTPException(
                status_code=400,
                detail="補登只能寫歷史事件；未來狀態變更請用「變更狀態」功能",
            )

        log = StudentChangeLog(
            student_id=item.student_id,
            school_year=item.school_year,
            semester=item.semester,
            event_type=item.event_type,
            event_date=event_date,
            classroom_id=item.classroom_id,
            from_classroom_id=item.from_classroom_id,
            to_classroom_id=item.to_classroom_id,
            reason=item.reason,
            notes=item.notes,
            recorded_by=current_user.get("user_id"),
            source="manual",
        )
        session.add(log)
        session.commit()
        logger.info(
            "手動補登學生異動：student_id=%s event_type=%s operator=%s",
            item.student_id,
            item.event_type,
            current_user.get("username"),
        )
        return {"message": "異動紀錄新增成功", "id": log.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("建立異動紀錄失敗")
        raise HTTPException(status_code=500, detail="建立失敗，請稍後再試")
    finally:
        session.close()


@router.put("/{log_id}")
async def update_change_log(
    log_id: int,
    item: ChangeLogUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """編輯異動紀錄（僅限手動補登的紀錄）"""
    session = get_session()
    try:
        log = (
            session.query(StudentChangeLog)
            .filter(StudentChangeLog.id == log_id)
            .first()
        )
        if not log:
            raise HTTPException(status_code=404, detail="找不到異動紀錄")
        # F-022：先檢查 caller 是否可存取對應學生（跨班禁止）
        assert_student_access(session, current_user, log.student_id)
        if (log.source or "manual") == "lifecycle":
            raise HTTPException(
                status_code=403,
                detail="系統自動產生的異動紀錄為稽核軌跡，不可編輯",
            )

        update_data = item.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if key == "event_date" and value:
                setattr(log, key, datetime.strptime(value, "%Y-%m-%d").date())
            else:
                setattr(log, key, value)

        session.commit()
        return {"message": "異動紀錄已更新", "id": log_id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("更新異動紀錄失敗")
        raise HTTPException(status_code=500, detail="更新失敗，請稍後再試")
    finally:
        session.close()


@router.delete("/{log_id}")
async def delete_change_log(
    log_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """刪除異動紀錄（僅限手動補登的紀錄）"""
    session = get_session()
    try:
        log = (
            session.query(StudentChangeLog)
            .filter(StudentChangeLog.id == log_id)
            .first()
        )
        if not log:
            raise HTTPException(status_code=404, detail="找不到異動紀錄")
        # F-022：先檢查 caller 是否可存取對應學生（跨班禁止）
        assert_student_access(session, current_user, log.student_id)
        if (log.source or "manual") == "lifecycle":
            raise HTTPException(
                status_code=403,
                detail="系統自動產生的異動紀錄為稽核軌跡，不可刪除",
            )

        session.delete(log)
        session.commit()
        return {"message": "異動紀錄已刪除", "id": log_id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.exception("刪除異動紀錄失敗")
        raise HTTPException(status_code=500, detail="刪除失敗，請稍後再試")
    finally:
        session.close()

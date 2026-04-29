"""
Portal - 才藝報名查詢（教師查看班上學生報名狀況）及才藝點名
"""

import logging
from collections import defaultdict
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from models.database import get_session, Classroom
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    ActivityAttendance,
    RegistrationCourse,
)
from utils.auth import get_current_user
from ._shared import _get_employee, _get_teacher_classroom_ids
from api.activity._shared import _build_session_detail_response

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/activity/registrations")
def get_portal_activity_registrations(
    current_user: dict = Depends(get_current_user),
):
    """取得當前教師管理班級的學生才藝報名列表"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        # 找出教師管理的班級（主教、副教、藝術教師皆可查）
        classrooms = (
            session.query(Classroom)
            .filter(
                Classroom.is_active.is_(True),
                or_(
                    Classroom.head_teacher_id == emp.id,
                    Classroom.assistant_teacher_id == emp.id,
                    Classroom.art_teacher_id == emp.id,
                ),
            )
            .all()
        )

        if not classrooms:
            return {"classrooms": [], "registrations": []}

        class_names = [c.name for c in classrooms]
        classroom_ids = [c.id for c in classrooms]

        # 查詢班級內學生的報名資料（以 classroom_id FK 比對，避免字串比對在轉班後失準）
        regs = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.classroom_id.in_(classroom_ids),
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.match_status != "rejected",
            )
            .order_by(
                ActivityRegistration.class_name, ActivityRegistration.student_name
            )
            .all()
        )

        reg_ids = [r.id for r in regs]

        # 批次查詢課程關聯
        course_map: dict[int, list] = defaultdict(list)
        if reg_ids:
            rc_rows = (
                session.query(
                    RegistrationCourse.registration_id,
                    RegistrationCourse.id.label("rc_id"),
                    RegistrationCourse.status,
                    RegistrationCourse.course_id,
                    ActivityCourse.name.label("course_name"),
                )
                .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
                .filter(RegistrationCourse.registration_id.in_(reg_ids))
                .order_by(RegistrationCourse.registration_id, RegistrationCourse.id)
                .all()
            )

            # 計算候補排位（依課程分組，按 rc_id 排序）
            waitlist_rows: dict[int, list] = defaultdict(list)
            for row in rc_rows:
                if row.status == "waitlist":
                    waitlist_rows[row.course_id].append(row.rc_id)

            # rc_id → position
            waitlist_position_map: dict[int, int] = {}
            for course_id, rc_ids in waitlist_rows.items():
                sorted_ids = sorted(rc_ids)
                for pos, rc_id in enumerate(sorted_ids, start=1):
                    waitlist_position_map[rc_id] = pos

            for row in rc_rows:
                entry = {
                    "course_name": row.course_name,
                    "status": row.status,
                    "waitlist_position": (
                        waitlist_position_map.get(row.rc_id)
                        if row.status == "waitlist"
                        else None
                    ),
                }
                course_map[row.registration_id].append(entry)

        result = []
        for r in regs:
            result.append(
                {
                    "id": r.id,
                    "student_name": r.student_name,
                    "class_name": r.class_name,
                    "is_paid": r.is_paid,
                    "courses": course_map.get(r.id, []),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )

        # 摘要統計
        total_enrolled = sum(
            1 for r in result for c in r["courses"] if c["status"] == "enrolled"
        )
        total_waitlist = sum(
            1 for r in result for c in r["courses"] if c["status"] == "waitlist"
        )
        total_paid = sum(1 for r in result if r["is_paid"])

        return {
            "classrooms": class_names,
            "registrations": result,
            "summary": {
                "total_registrations": len(result),
                "total_enrolled": total_enrolled,
                "total_waitlist": total_waitlist,
                "total_paid": total_paid,
            },
        }
    finally:
        session.close()


# ── 才藝點名（Portal） ─────────────────────────────────────────────────────────


class PortalAttendanceRecordItem(BaseModel):
    registration_id: int
    is_present: bool
    notes: Optional[str] = ""


class PortalBatchAttendanceUpdate(BaseModel):
    records: List[PortalAttendanceRecordItem]


def _get_teacher_class_names(session, emp_id: int) -> list[str]:
    """取得教師管轄班級名稱列表（向下相容；新程式請直接用 _get_teacher_classroom_ids）"""
    classroom_ids = _get_teacher_classroom_ids(session, emp_id)
    if not classroom_ids:
        return []
    classrooms = session.query(Classroom).filter(Classroom.id.in_(classroom_ids)).all()
    return [c.name for c in classrooms]


@router.get("/activity/attendance/sessions")
def portal_list_sessions(
    course_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: dict = Depends(get_current_user),
):
    """取得含自班學生的場次列表"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)
        if not classroom_ids:
            return []

        # 取得自班有報名的課程 ID（以 classroom_id FK 比對）
        enrolled_course_ids = [
            row.course_id
            for row in session.query(RegistrationCourse.course_id)
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                ActivityRegistration.classroom_id.in_(classroom_ids),
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.match_status != "rejected",
                RegistrationCourse.status == "enrolled",
            )
            .distinct()
            .all()
        ]
        if not enrolled_course_ids:
            return []

        query = (
            session.query(
                ActivitySession.id,
                ActivitySession.course_id,
                ActivitySession.session_date,
                ActivitySession.notes,
                ActivitySession.created_by,
                ActivitySession.created_at,
                ActivityCourse.name.label("course_name"),
            )
            .join(ActivityCourse, ActivitySession.course_id == ActivityCourse.id)
            .filter(ActivitySession.course_id.in_(enrolled_course_ids))
        )
        if course_id:
            query = query.filter(ActivitySession.course_id == course_id)
        if start_date:
            query = query.filter(ActivitySession.session_date >= start_date)
        if end_date:
            query = query.filter(ActivitySession.session_date <= end_date)
        rows = query.order_by(
            ActivitySession.session_date.desc(), ActivitySession.id.desc()
        ).all()

        # 計算自班出席統計（以 classroom_id FK 比對）
        session_ids = [r.id for r in rows]
        class_reg_ids = set(
            row.id
            for row in session.query(ActivityRegistration.id)
            .filter(
                ActivityRegistration.classroom_id.in_(classroom_ids),
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.match_status != "rejected",
            )
            .all()
        )

        attendance_stats: dict[int, dict] = {}
        if session_ids and class_reg_ids:
            raw_atts = (
                session.query(
                    ActivityAttendance.session_id, ActivityAttendance.is_present
                )
                .filter(
                    ActivityAttendance.session_id.in_(session_ids),
                    ActivityAttendance.registration_id.in_(class_reg_ids),
                )
                .all()
            )
            for att in raw_atts:
                if att.session_id not in attendance_stats:
                    attendance_stats[att.session_id] = {"recorded": 0, "present": 0}
                attendance_stats[att.session_id]["recorded"] += 1
                if att.is_present:
                    attendance_stats[att.session_id]["present"] += 1

        result = []
        for r in rows:
            stat = attendance_stats.get(r.id, {"recorded": 0, "present": 0})
            result.append(
                {
                    "id": r.id,
                    "course_id": r.course_id,
                    "course_name": r.course_name,
                    "session_date": (
                        r.session_date.isoformat() if r.session_date else None
                    ),
                    "notes": r.notes or "",
                    "created_by": r.created_by,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "recorded_count": stat["recorded"],
                    "present_count": stat["present"],
                }
            )
        return result
    finally:
        session.close()


@router.get("/activity/attendance/sessions/{session_id}")
def portal_get_session_detail(
    session_id: int,
    group_by: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """場次詳情（僅含自班學生；classroom_id FK 比對）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        # F-010：「場次不存在」與「場次不含自班學生」collapse 為同一 generic 403，
        # 避免透過 status code 差異枚舉 ActivitySession id 與 course_name 中介資料。
        if not sess:
            raise HTTPException(status_code=403, detail="查無此場次或無權存取")

        group_key = "classroom" if group_by == "classroom" else None
        response = _build_session_detail_response(
            session,
            sess,
            classroom_ids_filter=classroom_ids,
            group_by=group_key,
        )
        # 若教師對此場次無自班學生，視同無權查閱：不外露 course_name / 日期等中介資料。
        if not response.get("students"):
            raise HTTPException(status_code=403, detail="查無此場次或無權存取")
        return response
    finally:
        session.close()


@router.put("/activity/attendance/sessions/{session_id}/records")
def portal_batch_update_attendance(
    session_id: int,
    body: PortalBatchAttendanceUpdate,
    current_user: dict = Depends(get_current_user),
):
    """批次點名（只能更新自班學生；classroom_id FK 比對）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        if not sess:
            raise HTTPException(status_code=404, detail="找不到場次")

        # 驗證所有 registration_id 都屬於自班（classroom_id FK 比對），
        # 並排除已軟刪/被拒絕的報名，避免對離園或 rejected 學生寫入 attendance。
        if body.records:
            req_reg_ids = [item.registration_id for item in body.records]
            if not classroom_ids:
                raise HTTPException(status_code=403, detail="包含無權操作的學生記錄")
            allowed_regs = (
                session.query(ActivityRegistration.id)
                .filter(
                    ActivityRegistration.id.in_(req_reg_ids),
                    ActivityRegistration.classroom_id.in_(classroom_ids),
                    ActivityRegistration.is_active.is_(True),
                    ActivityRegistration.match_status != "rejected",
                )
                .all()
            )
            allowed_ids = {r.id for r in allowed_regs}
            forbidden = [rid for rid in req_reg_ids if rid not in allowed_ids]
            if forbidden:
                raise HTTPException(status_code=403, detail="包含無權操作的學生記錄")

        operator = current_user.get("username")

        # 批次查詢現有記錄，避免 N+1
        req_reg_ids = [item.registration_id for item in body.records]
        existing_map = {
            a.registration_id: a
            for a in session.query(ActivityAttendance)
            .filter(
                ActivityAttendance.session_id == session_id,
                ActivityAttendance.registration_id.in_(req_reg_ids),
            )
            .all()
        }

        # 冗餘寫入 student_id（與管理端點名一致）
        reg_student_map = (
            dict(
                session.query(ActivityRegistration.id, ActivityRegistration.student_id)
                .filter(ActivityRegistration.id.in_(req_reg_ids))
                .all()
            )
            if req_reg_ids
            else {}
        )

        for item in body.records:
            existing = existing_map.get(item.registration_id)
            if existing:
                existing.is_present = item.is_present
                existing.notes = item.notes or ""
                existing.recorded_by = operator
                if existing.student_id is None:
                    existing.student_id = reg_student_map.get(item.registration_id)
            else:
                att = ActivityAttendance(
                    session_id=session_id,
                    registration_id=item.registration_id,
                    student_id=reg_student_map.get(item.registration_id),
                    is_present=item.is_present,
                    notes=item.notes or "",
                    recorded_by=operator,
                )
                session.add(att)

        session.commit()
        return {"ok": True, "updated": len(body.records)}
    finally:
        session.close()

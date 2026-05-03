"""
Portal - my students endpoint + 個別學生彙總（detail）+ 揭露電話端點

隱私規範：
1. 完整地址絕對不從 portal API 回傳（即使有 STUDENTS_READ 也不出）
2. 電話欄位（parent_phone、emergency_contact_phone、guardians[].phone）
   預設遮罩；前端需呼叫 POST /reveal-phone 才能取得真實號碼
3. 健康/特殊需求依 STUDENTS_HEALTH_READ / STUDENTS_SPECIAL_NEEDS_READ 遮罩
4. 不導入繳費資訊（FEES_READ 由後台獨立管控，portal 端完全不出 fees）
"""

from collections import defaultdict
from datetime import date as date_cls
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import or_

from models.database import (
    Classroom,
    Guardian,
    Student,
    StudentAssessment,
    StudentAttendance,
    StudentClassroomTransfer,
    StudentContactBookEntry,
    StudentIncident,
    get_session,
)
from models.portfolio import (
    StudentAllergy,
    StudentMedicationOrder,
    StudentObservation,
)
from utils.audit import write_audit_in_session
from utils.auth import get_current_user
from utils.masking import mask_phone
from utils.permissions import Permission
from utils.portfolio_access import (
    can_view_student_health,
    can_view_student_special_needs,
)

from ._shared import _get_employee, _get_teacher_classroom_ids

router = APIRouter()


# ────────────────────────────────────────────────────────────────────────
# 共用 helpers
# ────────────────────────────────────────────────────────────────────────


def _is_admin_like(current_user: dict) -> bool:
    role = current_user.get("role")
    if role in ("admin", "supervisor"):
        return True
    perms = int(current_user.get("permissions", 0) or 0)
    return perms < 0  # admin: -1


def _classroom_role(emp_id: int, classroom: Classroom) -> str:
    """從教師視角判斷在該班的角色標籤。"""
    if classroom.head_teacher_id == emp_id:
        return "主教老師"
    if classroom.assistant_teacher_id == emp_id:
        return "助教老師"
    if classroom.art_teacher_id == emp_id:
        return "美語老師"
    return "教師"


def _month_window(today: date_cls) -> tuple[date_cls, date_cls]:
    """回傳 (本月 1 號, 今天)。"""
    return today.replace(day=1), today


def _aggregate_attendance_this_month(
    session, student_ids: list[int], today: date_cls
) -> dict[int, dict]:
    """單次 IN 查詢，回傳 {student_id: {rate, last_absent_date, total, present}}。

    出席率分母：當月有紀錄的天數（不含未排課/未紀錄日）。
    出席分子：status == 出席 的天數。
    """
    if not student_ids:
        return {}
    start, end = _month_window(today)
    rows = (
        session.query(StudentAttendance)
        .filter(
            StudentAttendance.student_id.in_(student_ids),
            StudentAttendance.date >= start,
            StudentAttendance.date <= end,
        )
        .all()
    )
    by_student: dict[int, list[StudentAttendance]] = defaultdict(list)
    for r in rows:
        by_student[r.student_id].append(r)

    result: dict[int, dict] = {}
    for sid in student_ids:
        records = by_student.get(sid, [])
        total = len(records)
        present = sum(1 for r in records if r.status == "出席")
        absent_dates = [r.date for r in records if r.status == "缺席"]
        rate = round(present / total * 100, 1) if total else None
        result[sid] = {
            "attendance_rate_this_month": rate,
            "last_absent_date": (
                max(absent_dates).isoformat() if absent_dates else None
            ),
            "_total": total,
            "_present": present,
        }
    return result


def _aggregate_health_alerts(
    session,
    student_ids: list[int],
    current_user: dict,
) -> dict[int, dict]:
    """{student_id: {has_health_alert, health_alert_count}}.

    缺 STUDENTS_HEALTH_READ 一律回 false / 0。
    特殊需求依 STUDENTS_SPECIAL_NEEDS_READ。
    """
    if not student_ids:
        return {}

    can_health = can_view_student_health(current_user)
    can_special = can_view_student_special_needs(current_user)

    # 預設 false / 0；無權者直接回
    result: dict[int, dict] = {
        sid: {"has_health_alert": False, "health_alert_count": 0} for sid in student_ids
    }
    if not can_health and not can_special:
        return result

    # 抓 active allergies / medication orders（30 天內）
    if can_health:
        allergies = (
            session.query(StudentAllergy.student_id)
            .filter(
                StudentAllergy.student_id.in_(student_ids),
                StudentAllergy.active.is_(True),
            )
            .all()
        )
        for (sid,) in allergies:
            result[sid]["health_alert_count"] += 1
            result[sid]["has_health_alert"] = True

        today = date_cls.today()
        meds = (
            session.query(StudentMedicationOrder.student_id)
            .filter(
                StudentMedicationOrder.student_id.in_(student_ids),
                StudentMedicationOrder.order_date >= today - timedelta(days=7),
            )
            .all()
        )
        for (sid,) in meds:
            result[sid]["health_alert_count"] += 1
            result[sid]["has_health_alert"] = True

    if can_special:
        rows = (
            session.query(Student.id, Student.special_needs)
            .filter(Student.id.in_(student_ids))
            .all()
        )
        for sid, sn in rows:
            if sn:
                result[sid]["health_alert_count"] += 1
                result[sid]["has_health_alert"] = True

    return result


# ────────────────────────────────────────────────────────────────────────
# /api/portal/my-students
# ────────────────────────────────────────────────────────────────────────


@router.get("/my-students")
def get_my_students(
    classroom_id: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user),
):
    """取得教師所屬班級的學生資料（精簡欄位 + 健康/出席聚合）。

    隱私：不回 address；parent_phone 走 mask_phone。
    """
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        query = session.query(Classroom).filter(
            Classroom.is_active == True,  # noqa: E712
            or_(
                Classroom.head_teacher_id == emp.id,
                Classroom.assistant_teacher_id == emp.id,
                Classroom.art_teacher_id == emp.id,
            ),
        )
        if classroom_id:
            query = query.filter(Classroom.id == classroom_id)

        classrooms = query.all()

        if not classrooms:
            return {
                "employee_name": emp.name,
                "classrooms": [],
                "total_students": 0,
            }

        classroom_ids = [cr.id for cr in classrooms]
        all_students = (
            session.query(Student)
            .filter(
                Student.classroom_id.in_(classroom_ids),
                Student.is_active == True,  # noqa: E712
            )
            .order_by(Student.name)
            .all()
        )
        student_ids = [s.id for s in all_students]
        students_by_classroom: dict[int, list[Student]] = defaultdict(list)
        for s in all_students:
            students_by_classroom[s.classroom_id].append(s)

        today = date_cls.today()
        attendance_map = _aggregate_attendance_this_month(session, student_ids, today)
        health_map = _aggregate_health_alerts(session, student_ids, current_user)

        result = []
        for cr in classrooms:
            role = _classroom_role(emp.id, cr)
            students = students_by_classroom[cr.id]

            result.append(
                {
                    "classroom_id": cr.id,
                    "classroom_name": cr.name,
                    "role": role,
                    "student_count": len(students),
                    "students": [
                        {
                            "id": s.id,
                            "student_id": s.student_id,
                            "name": s.name,
                            "gender": s.gender,
                            "birthday": (
                                s.birthday.isoformat() if s.birthday else None
                            ),
                            "enrollment_date": (
                                s.enrollment_date.isoformat()
                                if s.enrollment_date
                                else None
                            ),
                            "lifecycle_status": s.lifecycle_status,
                            "parent_name": s.parent_name,
                            "parent_phone_masked": mask_phone(s.parent_phone),
                            "status_tag": s.status_tag,
                            "notes": s.notes,
                            "has_health_alert": health_map.get(s.id, {}).get(
                                "has_health_alert", False
                            ),
                            "health_alert_count": health_map.get(s.id, {}).get(
                                "health_alert_count", 0
                            ),
                            "attendance_rate_this_month": attendance_map.get(
                                s.id, {}
                            ).get("attendance_rate_this_month"),
                            "last_absent_date": attendance_map.get(s.id, {}).get(
                                "last_absent_date"
                            ),
                        }
                        for s in students
                    ],
                }
            )

        return {
            "employee_name": emp.name,
            "classrooms": result,
            "total_students": sum(c["student_count"] for c in result),
        }
    finally:
        session.close()


# ────────────────────────────────────────────────────────────────────────
# /api/portal/students/{student_id}/detail
# ────────────────────────────────────────────────────────────────────────


_STUDENT_DETAIL_LOOKBACK_DAYS = 30
_CONTACT_BOOK_RECENT_LIMIT = 5
_ASSESSMENT_RECENT_LIMIT = 6
_TRANSFER_HISTORY_LIMIT = 20


def _build_transfer_history(
    session,
    student_id: int,
    teacher_classroom_ids: Optional[list[int]],
) -> list[dict]:
    """取得學生轉班歷史，限制只回該老師班級相關段（admin 不限）。"""
    query = session.query(StudentClassroomTransfer).filter(
        StudentClassroomTransfer.student_id == student_id
    )
    if teacher_classroom_ids is not None:
        query = query.filter(
            or_(
                StudentClassroomTransfer.from_classroom_id.in_(teacher_classroom_ids),
                StudentClassroomTransfer.to_classroom_id.in_(teacher_classroom_ids),
            )
        )
    transfers = (
        query.order_by(StudentClassroomTransfer.transferred_at.desc())
        .limit(_TRANSFER_HISTORY_LIMIT)
        .all()
    )
    if not transfers:
        return []

    classroom_ids: set[int] = set()
    for t in transfers:
        if t.from_classroom_id:
            classroom_ids.add(t.from_classroom_id)
        if t.to_classroom_id:
            classroom_ids.add(t.to_classroom_id)
    name_map: dict[int, str] = {}
    if classroom_ids:
        rows = (
            session.query(Classroom.id, Classroom.name)
            .filter(Classroom.id.in_(classroom_ids))
            .all()
        )
        name_map = {cid: cname for cid, cname in rows}

    return [
        {
            "transferred_at": (
                t.transferred_at.isoformat() if t.transferred_at else None
            ),
            "from_classroom_id": t.from_classroom_id,
            "from_classroom_name": (
                name_map.get(t.from_classroom_id) if t.from_classroom_id else None
            ),
            "to_classroom_id": t.to_classroom_id,
            "to_classroom_name": name_map.get(t.to_classroom_id),
        }
        for t in transfers
    ]


@router.get("/students/{student_id}/detail")
def get_student_detail(
    student_id: int,
    current_user: dict = Depends(get_current_user),
):
    """單一學生彙總頁：基本資料 + 健康 + 30 天出席/觀察/事件 + 評量 + 近期聯絡簿。

    教師僅可查自己班級的學生；admin/supervisor 可跨班。
    隱私：不回 address；電話走 mask_phone（需另呼叫 reveal-phone 揭露）。
    """
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="學生不存在")

        emp = _get_employee(session, current_user)
        is_admin = _is_admin_like(current_user)
        if is_admin:
            teacher_classroom_ids = None
        else:
            teacher_classroom_ids = _get_teacher_classroom_ids(session, emp.id)
            if student.classroom_id not in teacher_classroom_ids:
                raise HTTPException(status_code=403, detail="此學生不在您管轄班級")

        classroom = (
            session.query(Classroom)
            .filter(Classroom.id == student.classroom_id)
            .first()
            if student.classroom_id
            else None
        )
        classroom_role = (
            _classroom_role(emp.id, classroom) if (classroom and not is_admin) else None
        )

        guardians = (
            session.query(Guardian)
            .filter(
                Guardian.student_id == student.id,
                Guardian.deleted_at.is_(None),
            )
            .order_by(
                Guardian.is_primary.desc(),
                Guardian.sort_order.asc(),
                Guardian.id.asc(),
            )
            .all()
        )

        allergies = (
            session.query(StudentAllergy)
            .filter(
                StudentAllergy.student_id == student.id,
                StudentAllergy.active.is_(True),
            )
            .order_by(StudentAllergy.id.desc())
            .all()
        )

        today = date_cls.today()
        active_med_orders = (
            session.query(StudentMedicationOrder)
            .filter(
                StudentMedicationOrder.student_id == student.id,
                StudentMedicationOrder.order_date >= today - timedelta(days=7),
            )
            .order_by(StudentMedicationOrder.order_date.desc())
            .all()
        )

        start = today - timedelta(days=_STUDENT_DETAIL_LOOKBACK_DAYS)
        attendance_rows = (
            session.query(StudentAttendance)
            .filter(
                StudentAttendance.student_id == student.id,
                StudentAttendance.date >= start,
                StudentAttendance.date <= today,
            )
            .order_by(StudentAttendance.date.asc())
            .all()
        )
        att_summary = {"present": 0, "absent": 0, "late": 0, "leave": 0}
        for r in attendance_rows:
            if r.status == "出席":
                att_summary["present"] += 1
            elif r.status == "缺席":
                att_summary["absent"] += 1
            elif r.status == "遲到":
                att_summary["late"] += 1
            elif r.status in ("病假", "事假"):
                att_summary["leave"] += 1

        # 本月出席率（與列表一致的口徑）
        month_attendance = _aggregate_attendance_this_month(
            session, [student.id], today
        ).get(student.id, {})

        incidents = (
            session.query(StudentIncident)
            .filter(
                StudentIncident.student_id == student.id,
                StudentIncident.occurred_at >= start,
            )
            .order_by(StudentIncident.occurred_at.desc())
            .all()
        )

        observations = (
            session.query(StudentObservation)
            .filter(
                StudentObservation.student_id == student.id,
                StudentObservation.observation_date >= start,
                StudentObservation.deleted_at.is_(None),
            )
            .order_by(StudentObservation.observation_date.desc())
            .all()
        )

        assessments = (
            session.query(StudentAssessment)
            .filter(StudentAssessment.student_id == student.id)
            .order_by(StudentAssessment.assessment_date.desc())
            .limit(_ASSESSMENT_RECENT_LIMIT)
            .all()
        )

        recent_cb = (
            session.query(StudentContactBookEntry)
            .filter(
                StudentContactBookEntry.student_id == student.id,
                StudentContactBookEntry.deleted_at.is_(None),
            )
            .order_by(StudentContactBookEntry.log_date.desc())
            .limit(_CONTACT_BOOK_RECENT_LIMIT)
            .all()
        )

        transfer_history = _build_transfer_history(
            session, student.id, teacher_classroom_ids
        )

        # 健康欄位遮罩（detail dict 用 allergy_text/medication_text key）
        can_health = can_view_student_health(current_user)
        can_special = can_view_student_special_needs(current_user)
        allergy_text = student.allergy if can_health else None
        medication_text = student.medication if can_health else None
        special_needs = student.special_needs if can_special else None

        return {
            "student": {
                "id": student.id,
                "student_id": student.student_id,
                "name": student.name,
                "gender": student.gender,
                "birthday": student.birthday.isoformat() if student.birthday else None,
                "enrollment_date": (
                    student.enrollment_date.isoformat()
                    if student.enrollment_date
                    else None
                ),
                "lifecycle_status": student.lifecycle_status,
                "status_tag": student.status_tag,
                # 紅線：address 不回傳
                "notes": student.notes,
                "allergy_text": allergy_text,  # deprecated（依 STUDENTS_HEALTH_READ）
                "medication_text": medication_text,  # deprecated（依 STUDENTS_HEALTH_READ）
                "special_needs": special_needs,  # 依 STUDENTS_SPECIAL_NEEDS_READ
                "emergency_contact_name": student.emergency_contact_name,
                "emergency_contact_phone_masked": mask_phone(
                    student.emergency_contact_phone
                ),
                "emergency_contact_relation": student.emergency_contact_relation,
                "parent_name": student.parent_name,
                "parent_phone_masked": mask_phone(student.parent_phone),
            },
            "classroom": (
                {
                    "id": classroom.id,
                    "name": classroom.name,
                    "viewer_role": classroom_role,
                }
                if classroom
                else None
            ),
            "guardians": [
                {
                    "id": g.id,
                    "name": g.name,
                    "phone_masked": mask_phone(g.phone),
                    "email": g.email,
                    "relation": g.relation,
                    "is_primary": bool(g.is_primary),
                    "is_emergency": bool(g.is_emergency),
                    "can_pickup": bool(g.can_pickup),
                    "user_id": g.user_id,
                }
                for g in guardians
            ],
            "health": {
                "allergies": [
                    {
                        "id": a.id,
                        "allergen": a.allergen,
                        "severity": a.severity,
                        "reaction": a.reaction_symptom,
                        "first_aid_note": a.first_aid_note,
                    }
                    for a in allergies
                ],
                "recent_medication_orders": [
                    {
                        "id": o.id,
                        "order_date": o.order_date.isoformat(),
                        "medication_name": o.medication_name,
                        "dose": o.dose,
                        "time_slots": o.time_slots,
                        "source": o.source,
                        "note": o.note,
                    }
                    for o in active_med_orders
                ],
            },
            "attendance_30d": {
                "summary": att_summary,
                "by_day": [
                    {
                        "date": r.date.isoformat(),
                        "status": r.status,
                        "remark": r.remark,
                    }
                    for r in attendance_rows
                ],
            },
            "attendance_this_month": {
                "rate": month_attendance.get("attendance_rate_this_month"),
                "last_absent_date": month_attendance.get("last_absent_date"),
            },
            "transfer_history": transfer_history,
            "recent_incidents_30d": [
                {
                    "id": i.id,
                    "incident_date": (
                        i.occurred_at.date().isoformat() if i.occurred_at else None
                    ),
                    "type": i.incident_type,
                    "severity": i.severity,
                    "description": i.description,
                }
                for i in incidents
            ],
            "recent_observations_30d": [
                {
                    "id": o.id,
                    "observation_date": (
                        o.observation_date.isoformat() if o.observation_date else None
                    ),
                    "domain": o.domain,
                    "narrative": o.narrative,
                    "rating": o.rating,
                    "is_highlight": bool(o.is_highlight),
                }
                for o in observations
            ],
            "recent_assessments": [
                {
                    "id": a.id,
                    "semester": a.semester,
                    "assessment_type": a.assessment_type,
                    "domain": a.domain,
                    "rating": a.rating,
                    "assessment_date": (
                        a.assessment_date.isoformat() if a.assessment_date else None
                    ),
                }
                for a in assessments
            ],
            "contact_book_recent": [
                {
                    "id": e.id,
                    "log_date": e.log_date.isoformat(),
                    "published_at": (
                        e.published_at.isoformat() if e.published_at else None
                    ),
                    "mood": e.mood,
                    "teacher_note": e.teacher_note,
                }
                for e in recent_cb
            ],
        }
    finally:
        session.close()


# ────────────────────────────────────────────────────────────────────────
# POST /api/portal/students/{student_id}/reveal-phone
# ────────────────────────────────────────────────────────────────────────


_VALID_REVEAL_TARGETS = ("guardian", "emergency", "parent")


class RevealPhoneRequest(BaseModel):
    target: str
    guardian_id: Optional[int] = None

    @field_validator("target")
    @classmethod
    def validate_target(cls, v: str) -> str:
        if v not in _VALID_REVEAL_TARGETS:
            raise ValueError(f"target 必須為 {', '.join(_VALID_REVEAL_TARGETS)}")
        return v


@router.post("/students/{student_id}/reveal-phone")
def reveal_student_phone(
    student_id: int,
    payload: RevealPhoneRequest,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """揭露學生關係人完整電話。

    Why 同交易 audit：揭露 PII 是高敏感事件，必須留下不可推卸的軌跡。
    AuditMiddleware 是 fire-and-forget（threadpool 故障可能丟失），所以這裡
    用 write_audit_in_session 在同交易寫 AuditLog（audit 與 commit 共生死）。
    """
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="學生不存在")

        emp = _get_employee(session, current_user)
        if not _is_admin_like(current_user):
            classroom_ids = _get_teacher_classroom_ids(session, emp.id)
            if student.classroom_id not in classroom_ids:
                raise HTTPException(status_code=403, detail="此學生不在您管轄班級")

        if payload.target == "parent":
            phone = student.parent_phone
        elif payload.target == "emergency":
            phone = student.emergency_contact_phone
        else:  # guardian
            if not payload.guardian_id:
                raise HTTPException(
                    status_code=400, detail="target=guardian 必須提供 guardian_id"
                )
            guardian = (
                session.query(Guardian)
                .filter(
                    Guardian.id == payload.guardian_id,
                    Guardian.student_id == student.id,
                    Guardian.deleted_at.is_(None),
                )
                .first()
            )
            if not guardian:
                raise HTTPException(status_code=404, detail="找不到對應的監護人")
            phone = guardian.phone

        if not phone:
            raise HTTPException(status_code=404, detail="該對象未填寫電話")

        write_audit_in_session(
            session,
            request,
            action="REVEAL",
            entity_type="student",
            entity_id=student.id,
            summary=f"揭露學生關係人電話（target={payload.target}）",
            changes={
                "target": payload.target,
                "guardian_id": payload.guardian_id,
                "student_id": student.id,
                "student_name": student.name,
            },
        )
        session.commit()

        return {
            "target": payload.target,
            "guardian_id": payload.guardian_id,
            "phone": phone,
        }
    finally:
        session.close()

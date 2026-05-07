"""
Student management router
"""

import logging
import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from utils.errors import raise_safe_500
from pydantic import BaseModel, Field, field_validator
from typing import Literal
from sqlalchemy import func, or_

from models.database import get_session, Student, Classroom, StudentClassroomTransfer
from models.dismissal import StudentDismissalCall
from models.guardian import GUARDIAN_RELATIONS, Guardian
from models.classroom import LIFECYCLE_STATUSES
from services.student_lifecycle import LifecycleTransitionError, transition
from services.student_profile import assemble_profile
from utils.academic import resolve_current_academic_term, resolve_academic_term_filters
from utils.auth import require_staff_permission
from utils.error_messages import STUDENT_NOT_FOUND
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access, mask_student_health_fields

logger = logging.getLogger(__name__)


def _cancel_active_dismissal_calls(session, student: Student) -> list[dict]:
    """取消學生所有進行中（pending/acknowledged）的接送通知。

    必須在 session.commit() 前呼叫（需要 student.name 還在 session 中）。
    回傳每筆被取消通知的 WS 廣播 payload，供呼叫端 commit 後廣播。
    """
    calls = (
        session.query(StudentDismissalCall)
        .filter(
            StudentDismissalCall.student_id == student.id,
            StudentDismissalCall.status.in_(["pending", "acknowledged"]),
        )
        .all()
    )

    broadcasts = []
    for call in calls:
        call.status = "cancelled"
        broadcasts.append(
            {
                "classroom_id": call.classroom_id,
                "event": {
                    "type": "dismissal_call_cancelled",
                    "payload": {
                        "id": call.id,
                        "student_id": call.student_id,
                        "student_name": student.name,
                        "classroom_id": call.classroom_id,
                        "status": "cancelled",
                        "requested_at": call.requested_at.isoformat(),
                    },
                },
            }
        )

    if broadcasts:
        logger.warning(
            "學生刪除/離園：自動取消 %d 筆進行中接送通知，student_id=%s name=%s",
            len(broadcasts),
            student.id,
            student.name,
        )
    return broadcasts


def get_classroom_student_ids_at_date(
    session, classroom_id: int, at_date: date
) -> list[int]:
    """回傳在 at_date 當天歸屬於 classroom_id 的學生 ID 列表。

    查詢邏輯：
    1. 若學生有轉班記錄，取 at_date 當天或之前最後一筆，
       判斷其 to_classroom_id 是否為目標班級。
    2. 若學生從未轉班，直接以當前 classroom_id 判斷。

    班級統計報表（出席、事件等）呼叫此函式取得「當時」的學生列表，
    避免轉班後歷史記錄跟著學生跑到新班級。
    """
    at_dt = datetime.combine(at_date, datetime.max.time())

    # 找所有曾經有轉班記錄的學生
    transferred_student_ids_q = session.query(
        StudentClassroomTransfer.student_id
    ).distinct()

    # 子查詢：每個學生在 at_dt 之前的最後一次轉班時間
    latest_transfer_sq = (
        session.query(
            StudentClassroomTransfer.student_id,
            func.max(StudentClassroomTransfer.transferred_at).label("last_at"),
        )
        .filter(StudentClassroomTransfer.transferred_at <= at_dt)
        .group_by(StudentClassroomTransfer.student_id)
        .subquery()
    )

    # 在 at_dt 當天最後轉入 classroom_id 的學生
    ids_via_transfer = [
        row.student_id
        for row in session.query(StudentClassroomTransfer.student_id)
        .join(
            latest_transfer_sq,
            (StudentClassroomTransfer.student_id == latest_transfer_sq.c.student_id)
            & (StudentClassroomTransfer.transferred_at == latest_transfer_sq.c.last_at),
        )
        .filter(StudentClassroomTransfer.to_classroom_id == classroom_id)
        .all()
    ]

    # 從未轉班且當前 classroom_id 符合的學生
    ids_no_transfer = [
        row.id
        for row in session.query(Student.id)
        .filter(
            Student.classroom_id == classroom_id,
            Student.is_active == True,
            ~Student.id.in_(transferred_student_ids_q),
        )
        .all()
    ]

    return list(set(ids_via_transfer) | set(ids_no_transfer))


router = APIRouter(prefix="/api", tags=["students"])


# ============ Pydantic Models ============


class StudentCreate(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=50)
    gender: Optional[str] = None
    birthday: Optional[date] = None
    classroom_id: Optional[int] = None
    enrollment_date: Optional[date] = None
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    status_tag: Optional[str] = None
    allergy: Optional[str] = None
    medication: Optional[str] = None
    special_needs: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_relation: Optional[str] = None

    @field_validator("student_id", mode="before")
    @classmethod
    def strip_student_id(cls, v):
        if isinstance(v, str):
            v = v.strip()
        if not v:
            raise ValueError("學號不可為空")
        return v

    @field_validator(
        "birthday",
        "enrollment_date",
        mode="before",
    )
    @classmethod
    def empty_string_date_as_none(cls, v):
        if v == "" or v is None:
            return None
        return v

    @field_validator(
        "gender",
        "parent_name",
        "address",
        "notes",
        "status_tag",
        "allergy",
        "medication",
        "special_needs",
        "emergency_contact_name",
        "emergency_contact_relation",
        mode="before",
    )
    @classmethod
    def empty_string_as_none(cls, v):
        if v == "":
            return None
        return v

    @field_validator("parent_phone", "emergency_contact_phone", mode="before")
    @classmethod
    def validate_phone(cls, v):
        if v is None or v == "":
            return None
        if not re.match(r"^[\d\-\+\(\)\s]{7,20}$", v):
            raise ValueError("電話格式不正確（僅允許數字、-、+、()、空格，長度 7-20）")
        return v


class StudentUpdate(BaseModel):
    student_id: Optional[str] = Field(None, min_length=1, max_length=20)
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    gender: Optional[str] = None
    birthday: Optional[date] = None
    classroom_id: Optional[int] = None
    enrollment_date: Optional[date] = None
    parent_name: Optional[str] = None
    parent_phone: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    status_tag: Optional[str] = None
    allergy: Optional[str] = None
    medication: Optional[str] = None
    special_needs: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_relation: Optional[str] = None

    @field_validator("student_id", mode="before")
    @classmethod
    def strip_student_id(cls, v):
        if v is None:
            return v
        if isinstance(v, str):
            v = v.strip()
        if not v:
            raise ValueError("學號不可為空")
        return v

    @field_validator(
        "birthday",
        "enrollment_date",
        mode="before",
    )
    @classmethod
    def empty_string_date_as_none(cls, v):
        if v == "" or v is None:
            return None
        return v

    @field_validator(
        "gender",
        "parent_name",
        "address",
        "notes",
        "status_tag",
        "allergy",
        "medication",
        "special_needs",
        "emergency_contact_name",
        "emergency_contact_relation",
        mode="before",
    )
    @classmethod
    def empty_string_as_none(cls, v):
        if v == "":
            return None
        return v

    @field_validator("parent_phone", "emergency_contact_phone", mode="before")
    @classmethod
    def validate_phone(cls, v):
        if v is None or v == "":
            return None
        if not re.match(r"^[\d\-\+\(\)\s]{7,20}$", v):
            raise ValueError("電話格式不正確（僅允許數字、-、+、()、空格，長度 7-20）")
        return v


class StudentGraduate(BaseModel):
    graduation_date: str
    status: Literal["已畢業", "已轉出"]
    reason: Optional[str] = None
    notes: Optional[str] = None


class StudentBulkTransfer(BaseModel):
    student_ids: list[int]
    target_classroom_id: int


class LifecycleTransitionRequest(BaseModel):
    to_status: Literal[
        "prospect",
        "enrolled",
        "active",
        "on_leave",
        "transferred",
        "withdrawn",
        "graduated",
    ]
    effective_date: Optional[date] = None
    reason: Optional[str] = Field(None, max_length=50)
    notes: Optional[str] = Field(None, max_length=500)


class GuardianCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=100)
    relation: Optional[str] = Field(None, max_length=20)
    is_primary: bool = False
    is_emergency: bool = False
    can_pickup: bool = False
    custody_note: Optional[str] = Field(None, max_length=500)
    sort_order: int = Field(0, ge=0, le=999)

    @field_validator("phone", mode="before")
    @classmethod
    def _validate_phone(cls, v):
        if v is None or v == "":
            return None
        if not re.match(r"^[\d\-\+\(\)\s]{7,20}$", v):
            raise ValueError("電話格式不正確（僅允許數字、-、+、()、空格，長度 7-20）")
        return v

    @field_validator("relation")
    @classmethod
    def _validate_relation(cls, v):
        if v in (None, ""):
            return None
        if v not in GUARDIAN_RELATIONS:
            raise ValueError(f"關係需為：{GUARDIAN_RELATIONS}")
        return v


class GuardianUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    email: Optional[str] = Field(None, max_length=100)
    relation: Optional[str] = Field(None, max_length=20)
    is_primary: Optional[bool] = None
    is_emergency: Optional[bool] = None
    can_pickup: Optional[bool] = None
    custody_note: Optional[str] = Field(None, max_length=500)
    sort_order: Optional[int] = Field(None, ge=0, le=999)

    @field_validator("phone", mode="before")
    @classmethod
    def _validate_phone(cls, v):
        if v is None or v == "":
            return None
        if not re.match(r"^[\d\-\+\(\)\s]{7,20}$", v):
            raise ValueError("電話格式不正確（僅允許數字、-、+、()、空格，長度 7-20）")
        return v


# ============ Routes ============


@router.get("/students")
async def get_students(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    classroom_id: Optional[int] = None,
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    search: Optional[str] = None,
    is_active: Optional[bool] = Query(True),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得學生列表（分頁）。is_active=true 為在讀，is_active=false 為已離園"""
    session = get_session()
    try:
        q = session.query(Student).filter(Student.is_active == is_active)

        # 只有明確指定學年/學期時才套用學期過濾，避免預設行為隱性遮蔽資料
        if school_year is not None or semester is not None:
            resolved_school_year, resolved_semester = resolve_academic_term_filters(
                school_year, semester
            )
            q = q.outerjoin(
                Classroom,
                Student.classroom_id == Classroom.id,
            ).filter(
                or_(
                    Student.classroom_id.is_(None),
                    (
                        (Classroom.school_year == resolved_school_year)
                        & (Classroom.semester == resolved_semester)
                    ),
                ),
            )

        if classroom_id is not None:
            q = q.filter(Student.classroom_id == classroom_id)
        if search:
            like = f"%{search}%"
            q = q.filter(
                (Student.name.ilike(like))
                | (Student.student_id.ilike(like))
                | (Student.parent_name.ilike(like))
            )

        total = q.count()
        students = q.order_by(Student.id).offset(skip).limit(limit).all()

        items = []
        for s in students:
            row = {
                "id": s.id,
                "student_id": s.student_id,
                "name": s.name,
                "gender": s.gender,
                "birthday": s.birthday.isoformat() if s.birthday else None,
                "classroom_id": s.classroom_id,
                "enrollment_date": (
                    s.enrollment_date.isoformat() if s.enrollment_date else None
                ),
                "graduation_date": (
                    s.graduation_date.isoformat() if s.graduation_date else None
                ),
                "status": s.status,
                "parent_name": s.parent_name,
                "parent_phone": s.parent_phone,
                "address": s.address,
                "status_tag": s.status_tag,
                "allergy": s.allergy,
                "medication": s.medication,
                "special_needs": s.special_needs,
                "emergency_contact_name": s.emergency_contact_name,
                "emergency_contact_phone": s.emergency_contact_phone,
                "emergency_contact_relation": s.emergency_contact_relation,
                "is_active": s.is_active,
            }
            items.append(mask_student_health_fields(row, current_user))
        return {"items": items, "total": total, "skip": skip, "limit": limit}
    finally:
        session.close()


# ============ 學生紀錄聚合端點（事件 + 評量 + 異動統一時間軸）============


@router.get("/students/records")
async def get_student_records_timeline(
    type: Optional[list[str]] = Query(
        None,
        description="可多選：incident / assessment / change_log。未指定代表全部。",
    ),
    classroom_id: Optional[int] = Query(None),
    student_id: Optional[int] = Query(None),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """跨三模型的學生紀錄時間軸（事件 / 評量 / 異動）。"""
    from services.student_records_timeline import list_timeline

    session = get_session()
    try:
        return list_timeline(
            session,
            types=type,
            classroom_id=classroom_id,
            student_id=student_id,
            date_from=date_from,
            date_to=date_to,
            school_year=school_year,
            semester=semester,
            page=page,
            page_size=page_size,
            current_user=current_user,  # F-024：viewer-side 班級 scope
        )
    finally:
        session.close()


@router.get("/students/{student_id}")
async def get_student(
    student_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """取得單一學生詳細資料"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)
        payload = {
            "id": student.id,
            "student_id": student.student_id,
            "name": student.name,
            "gender": student.gender,
            "birthday": student.birthday.isoformat() if student.birthday else None,
            "classroom_id": student.classroom_id,
            "enrollment_date": (
                student.enrollment_date.isoformat() if student.enrollment_date else None
            ),
            "parent_name": student.parent_name,
            "parent_phone": student.parent_phone,
            "address": student.address,
            "notes": student.notes,
            "allergy": student.allergy,
            "medication": student.medication,
            "special_needs": student.special_needs,
            "emergency_contact_name": student.emergency_contact_name,
            "emergency_contact_phone": student.emergency_contact_phone,
            "emergency_contact_relation": student.emergency_contact_relation,
            "is_active": student.is_active,
        }
        return mask_student_health_fields(payload, current_user)
    finally:
        session.close()


@router.post("/students", status_code=201)
async def create_student(
    item: StudentCreate,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """新增學生"""
    session = get_session()
    try:
        # 檢查學號是否重複
        existing = (
            session.query(Student).filter(Student.student_id == item.student_id).first()
        )
        if existing:
            raise HTTPException(status_code=400, detail="學號已存在")

        data = item.model_dump()

        student = Student(**data)
        session.add(student)
        session.flush()  # 取得 student.id

        # 自動寫入「入學」異動紀錄
        from models.student_log import StudentChangeLog

        school_year, semester = resolve_current_academic_term()
        enrollment_date = student.enrollment_date or date.today()
        change_log = StudentChangeLog(
            student_id=student.id,
            school_year=school_year,
            semester=semester,
            event_type="入學",
            event_date=enrollment_date,
            classroom_id=student.classroom_id,
            reason="新生報名",
            recorded_by=current_user.get("user_id"),
        )
        session.add(change_log)
        session.commit()
        return {"message": "學生新增成功", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="新增失敗")
    finally:
        session.close()


@router.put("/students/{student_id}")
async def update_student(
    student_id: int,
    item: StudentUpdate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """更新學生資料

    audit（2026-05-07 P1）：在 request.state.audit_changes 寫入 before/after diff，
    讓 AuditMiddleware 把家長姓名/電話/地址/緊急聯絡人/班級等敏感欄位的具體
    變動寫進 audit_logs。否則只剩動作標籤，無法事後溯回誰把家長電話從 A 改成 B。
    """
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)

        update_data = item.model_dump(exclude_unset=True)
        old_classroom_id = student.classroom_id

        # 變更前 snapshot — 只取會被 update_data 影響的欄位，避免 dict 過大
        before_snapshot = {
            key: getattr(student, key, None)
            for key in update_data.keys()
            if hasattr(student, key)
        }

        NULLABLE_FK_FIELDS = {"classroom_id"}
        for key, value in update_data.items():
            if value is not None or key in NULLABLE_FK_FIELDS:
                setattr(student, key, value)

        # 同步：classroom_id 有異動時，更新該生當學期才藝報名的班級快照
        if "classroom_id" in update_data and student.classroom_id != old_classroom_id:
            from api.activity._shared import sync_registrations_on_student_transfer

            synced = sync_registrations_on_student_transfer(
                session, student.id, student.classroom_id
            )
            if synced:
                logger.info(
                    "學生轉班同步才藝報名：student_id=%s 更新 %s 筆",
                    student.id,
                    synced,
                )

        # 計算 diff：只收真有差異的欄位（避免 audit changes 充斥沒變動的列）
        diff: dict = {}
        for key in before_snapshot.keys():
            old_val = before_snapshot.get(key)
            new_val = getattr(student, key, None)
            if old_val != new_val:
                diff[key] = {"before": old_val, "after": new_val}
        if diff:
            request.state.audit_changes = diff
        request.state.audit_entity_id = student.id

        session.commit()
        return {"message": "學生資料更新成功", "id": student.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="更新失敗")
    finally:
        session.close()


@router.delete("/students/{student_id}")
async def delete_student(
    student_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """刪除學生（軟刪除）。同時取消該學生所有進行中的接送通知並推送 WS 事件。"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)

        # commit 前取消進行中通知，並收集廣播資料（需要 student.name）
        dismissal_broadcasts = _cancel_active_dismissal_calls(session, student)

        student.is_active = False
        student.status = "已刪除"

        # 同步：刪除學生時軟刪該生當學期才藝報名
        from api.activity._shared import sync_registrations_on_student_deactivate

        synced = sync_registrations_on_student_deactivate(
            session, student.id, current_user=current_user
        )
        if synced:
            logger.info(
                "學生刪除同步才藝報名：student_id=%s 軟刪 %s 筆",
                student.id,
                synced,
            )

        session.commit()
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="刪除失敗")
    finally:
        session.close()

    # WS 廣播在 session 關閉後執行，避免長時間佔用連線
    if dismissal_broadcasts:
        from api.dismissal_ws import manager as dismissal_manager

        for item in dismissal_broadcasts:
            await dismissal_manager.broadcast(item["classroom_id"], item["event"])

    return {"message": "學生已刪除", "id": student_id}


@router.post("/students/{student_id}/graduate")
async def graduate_student(
    student_id: int,
    item: StudentGraduate,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """設定學生畢業或轉出，並標記為非在讀。同時取消進行中的接送通知並推送 WS 事件。"""
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)
        if not student.is_active:
            raise HTTPException(status_code=400, detail="該學生已非在讀狀態")

        graduation_date = datetime.strptime(item.graduation_date, "%Y-%m-%d").date()
        if student.enrollment_date and graduation_date < student.enrollment_date:
            raise HTTPException(status_code=400, detail="離園日期不可早於入學日期")

        # commit 前取消進行中通知（需要 student.name）
        dismissal_broadcasts = _cancel_active_dismissal_calls(session, student)

        student.graduation_date = graduation_date
        student.status = item.status
        student.is_active = False

        # 自動寫入異動紀錄（畢業/退學/轉出）
        from models.student_log import StudentChangeLog

        status_to_event = {"已畢業": "畢業", "已退學": "退學", "已轉出": "轉出"}
        event_type = status_to_event.get(item.status, "退學")
        school_year, semester = resolve_current_academic_term()
        change_log = StudentChangeLog(
            student_id=student.id,
            school_year=school_year,
            semester=semester,
            event_type=event_type,
            event_date=graduation_date,
            classroom_id=student.classroom_id,
            from_classroom_id=(
                student.classroom_id if item.status == "已轉出" else None
            ),
            reason=item.reason,
            notes=item.notes,
            recorded_by=current_user.get("user_id"),
        )
        session.add(change_log)

        # 同步：離園時軟刪該生當學期才藝報名
        from api.activity._shared import sync_registrations_on_student_deactivate

        synced = sync_registrations_on_student_deactivate(
            session, student.id, current_user=current_user
        )
        if synced:
            logger.info(
                "學生離園同步才藝報名：student_id=%s status=%s 軟刪 %s 筆",
                student.id,
                item.status,
                synced,
            )

        session.commit()
        logger.warning(
            "學生離園：id=%s name=%s status=%s operator=%s",
            student.id,
            student.name,
            item.status,
            current_user.get("username"),
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="操作失敗")
    finally:
        session.close()

    if dismissal_broadcasts:
        from api.dismissal_ws import manager as dismissal_manager

        for broadcast_item in dismissal_broadcasts:
            await dismissal_manager.broadcast(
                broadcast_item["classroom_id"], broadcast_item["event"]
            )

    return {"message": f"已設定為「{item.status}」", "id": student_id}


@router.post("/students/bulk-transfer")
async def bulk_transfer_students(
    item: StudentBulkTransfer,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """批次轉班。

    audit（2026-05-07 P1）：在 request.state.audit_changes 寫入 student_ids
    與 from→to classroom 摘要，讓 AuditMiddleware 把這次大規模轉班動作的
    細節寫入 audit_logs（既有 StudentClassroomTransfer 是逐筆業務 audit，
    本處再多一層整體動作軌跡，事後篩 entity_type=student 可一次撈到）。
    """
    session = get_session()
    try:
        if not item.student_ids:
            raise HTTPException(status_code=400, detail="請先選擇學生")

        target_classroom = (
            session.query(Classroom)
            .filter(
                Classroom.id == item.target_classroom_id,
                Classroom.is_active == True,
            )
            .first()
        )
        if not target_classroom:
            raise HTTPException(status_code=400, detail="班級不存在或已停用")

        students = (
            session.query(Student)
            .filter(
                Student.id.in_(item.student_ids),
                Student.is_active == True,
            )
            .all()
        )
        existing_ids = {student.id for student in students}
        missing_ids = [
            student_id
            for student_id in item.student_ids
            if student_id not in existing_ids
        ]
        if missing_ids:
            raise HTTPException(
                status_code=404, detail=f"找不到在讀學生：{missing_ids}"
            )

        operator_id = current_user.get("user_id")
        now = datetime.now()
        moved_count = 0
        moved_student_ids: list[int] = []
        per_student_changes: list[dict] = []
        for student in students:
            if student.classroom_id == item.target_classroom_id:
                continue
            session.add(
                StudentClassroomTransfer(
                    student_id=student.id,
                    from_classroom_id=student.classroom_id,
                    to_classroom_id=item.target_classroom_id,
                    transferred_at=now,
                    transferred_by=operator_id,
                )
            )
            per_student_changes.append(
                {
                    "student_id": student.id,
                    "from_classroom_id": student.classroom_id,
                    "to_classroom_id": item.target_classroom_id,
                }
            )
            student.classroom_id = item.target_classroom_id
            moved_count += 1
            moved_student_ids.append(student.id)

        # 同步：把這批學生當學期的才藝報名改到新班級
        if moved_student_ids:
            from api.activity._shared import sync_registrations_on_student_transfer

            activity_synced = 0
            for sid in moved_student_ids:
                activity_synced += sync_registrations_on_student_transfer(
                    session, sid, item.target_classroom_id
                )
            if activity_synced:
                logger.info(
                    "批次轉班同步才藝報名：target_classroom_id=%s 更新 %s 筆",
                    item.target_classroom_id,
                    activity_synced,
                )

        if per_student_changes:
            request.state.audit_changes = {
                "action": "bulk_transfer",
                "target_classroom_id": item.target_classroom_id,
                "moved_count": moved_count,
                "transfers": per_student_changes,
            }

        session.commit()
        logger.info(
            "學生批次轉班：target_classroom_id=%s moved=%s operator=%s",
            item.target_classroom_id,
            moved_count,
            current_user.get("username"),
        )
        return {
            "message": "學生轉班成功",
            "moved_count": moved_count,
            "target_classroom_id": item.target_classroom_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="轉班失敗")
    finally:
        session.close()


# ============ 學生檔案聚合端點 ============


@router.get("/students/{student_id}/profile")
async def get_student_profile(
    student_id: int,
    timeline_limit: int = Query(20, ge=1, le=100),
    incident_limit: int = Query(5, ge=1, le=50),
    fee_period: Optional[str] = Query(None, description="None 表示聚合所有歷史費用"),
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
):
    """學生完整檔案聚合（basic + lifecycle + health + guardians + 各種 summary）。"""
    session = get_session()
    try:
        profile = assemble_profile(
            session,
            student_id,
            timeline_limit=timeline_limit,
            incident_limit=incident_limit,
            fee_period=fee_period,
        )
        if profile is None:
            raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)
        # 缺 STUDENTS_HEALTH_READ / STUDENTS_SPECIAL_NEEDS_READ 時遮罩 health 欄位
        if "health" in profile:
            profile["health"] = mask_student_health_fields(
                profile["health"], current_user
            )
        return profile
    finally:
        session.close()


# ============ 學生生命週期端點 ============


@router.post("/students/{student_id}/lifecycle")
async def transition_student_lifecycle(
    student_id: int,
    item: LifecycleTransitionRequest,
    current_user: dict = Depends(
        require_staff_permission(Permission.STUDENTS_LIFECYCLE_WRITE)
    ),
):
    """執行學生生命週期狀態轉移（退學/休學/畢業/轉出/復學等）。

    副作用：轉入終態 (withdrawn/transferred/graduated) 時：
    - 取消該生進行中的接送通知 + WS 廣播
    - 軟刪該生當學期才藝報名
    """
    session = get_session()
    dismissal_broadcasts: list[dict] = []
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if student is None:
            raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)

        terminal_like = {"withdrawn", "transferred", "graduated"}
        if item.to_status in terminal_like:
            dismissal_broadcasts = _cancel_active_dismissal_calls(session, student)

        try:
            result = transition(
                session,
                student,
                to_status=item.to_status,
                effective_date=item.effective_date,
                reason=item.reason,
                notes=item.notes,
                recorded_by=current_user.get("user_id"),
            )
        except LifecycleTransitionError as e:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(e))

        if item.to_status in terminal_like:
            from api.activity._shared import sync_registrations_on_student_deactivate

            synced = sync_registrations_on_student_deactivate(
                session, student.id, current_user=current_user
            )
            if synced:
                logger.info(
                    "學生生命週期轉入終態，同步軟刪才藝報名：student_id=%s to=%s 軟刪 %s 筆",
                    student.id,
                    item.to_status,
                    synced,
                )

        session.commit()
        logger.warning(
            "學生生命週期轉移：id=%s name=%s %s→%s operator=%s",
            student.id,
            student.name,
            result.from_status,
            result.to_status,
            current_user.get("username"),
        )
        response = {
            "message": f"已轉為 {result.to_status}",
            "student_id": result.student_id,
            "from_status": result.from_status,
            "to_status": result.to_status,
            "event_type": result.event_type,
            "change_log_id": result.change_log_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="狀態轉移失敗")
    finally:
        session.close()

    if dismissal_broadcasts:
        from api.dismissal_ws import manager as dismissal_manager

        for broadcast_item in dismissal_broadcasts:
            await dismissal_manager.broadcast(
                broadcast_item["classroom_id"], broadcast_item["event"]
            )

    return response


# ============ 監護人（Guardian）端點 ============


def _sync_primary_guardian_to_student(session, student: Student) -> None:
    """雙寫相容：把 is_primary 監護人資訊同步回寫 students.parent_name/phone。"""
    primary = (
        session.query(Guardian)
        .filter(
            Guardian.student_id == student.id,
            Guardian.deleted_at.is_(None),
            Guardian.is_primary == True,  # noqa: E712
        )
        .first()
    )
    student.parent_name = primary.name if primary else None
    student.parent_phone = primary.phone if primary else None


def _serialize_guardian(g: Guardian) -> dict:
    return {
        "id": g.id,
        "student_id": g.student_id,
        "name": g.name,
        "phone": g.phone,
        "email": g.email,
        "relation": g.relation,
        "is_primary": bool(g.is_primary),
        "is_emergency": bool(g.is_emergency),
        "can_pickup": bool(g.can_pickup),
        "custody_note": g.custody_note,
        "sort_order": g.sort_order,
    }


@router.get("/students/{student_id}/guardians")
async def list_guardians(
    student_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.GUARDIANS_READ)),
):
    session = get_session()
    try:
        # F-025：班級 scope 守衛 — 教師 / 自訂角色不可跨班讀家長 PII
        assert_student_access(session, current_user, student_id)
        rows = (
            session.query(Guardian)
            .filter(
                Guardian.student_id == student_id,
                Guardian.deleted_at.is_(None),
            )
            .order_by(
                Guardian.is_primary.desc(),
                Guardian.sort_order.asc(),
                Guardian.id.asc(),
            )
            .all()
        )
        return {"items": [_serialize_guardian(g) for g in rows]}
    finally:
        session.close()


@router.post("/students/{student_id}/guardians", status_code=201)
async def create_guardian(
    student_id: int,
    item: GuardianCreate,
    current_user: dict = Depends(require_staff_permission(Permission.GUARDIANS_WRITE)),
):
    session = get_session()
    try:
        student = session.query(Student).filter(Student.id == student_id).first()
        if student is None:
            raise HTTPException(status_code=404, detail=STUDENT_NOT_FOUND)

        # 若設為主要聯絡人，先將其他主要聯絡人降級
        if item.is_primary:
            session.query(Guardian).filter(
                Guardian.student_id == student_id,
                Guardian.deleted_at.is_(None),
                Guardian.is_primary == True,  # noqa: E712
            ).update({"is_primary": False}, synchronize_session=False)

        guardian = Guardian(student_id=student_id, **item.model_dump())
        session.add(guardian)
        session.flush()

        _sync_primary_guardian_to_student(session, student)

        session.commit()
        logger.info(
            "新增監護人：student_id=%s guardian_id=%s operator=%s",
            student_id,
            guardian.id,
            current_user.get("username"),
        )
        return _serialize_guardian(guardian)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="新增監護人失敗")
    finally:
        session.close()


@router.patch("/students/guardians/{guardian_id}")
async def update_guardian(
    guardian_id: int,
    item: GuardianUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.GUARDIANS_WRITE)),
):
    session = get_session()
    try:
        guardian = (
            session.query(Guardian)
            .filter(Guardian.id == guardian_id, Guardian.deleted_at.is_(None))
            .first()
        )
        if guardian is None:
            raise HTTPException(status_code=404, detail="監護人不存在或已刪除")

        data = item.model_dump(exclude_unset=True)
        # 驗證 relation
        if "relation" in data and data["relation"] is not None:
            if data["relation"] not in GUARDIAN_RELATIONS:
                raise HTTPException(
                    status_code=400,
                    detail=f"關係需為：{GUARDIAN_RELATIONS}",
                )

        # 若設為主要，先把同學生其他主要降級
        if data.get("is_primary") is True:
            session.query(Guardian).filter(
                Guardian.student_id == guardian.student_id,
                Guardian.id != guardian.id,
                Guardian.deleted_at.is_(None),
                Guardian.is_primary == True,  # noqa: E712
            ).update({"is_primary": False}, synchronize_session=False)

        for key, value in data.items():
            setattr(guardian, key, value)

        student = (
            session.query(Student).filter(Student.id == guardian.student_id).first()
        )
        if student is not None:
            _sync_primary_guardian_to_student(session, student)

        session.commit()
        return _serialize_guardian(guardian)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="更新監護人失敗")
    finally:
        session.close()


@router.delete("/students/guardians/{guardian_id}")
async def delete_guardian(
    guardian_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.GUARDIANS_WRITE)),
):
    """軟刪除監護人。"""
    session = get_session()
    try:
        guardian = (
            session.query(Guardian)
            .filter(Guardian.id == guardian_id, Guardian.deleted_at.is_(None))
            .first()
        )
        if guardian is None:
            raise HTTPException(status_code=404, detail="監護人不存在或已刪除")

        guardian.deleted_at = datetime.now()
        guardian.is_primary = False  # 軟刪後不再是主要聯絡人

        student = (
            session.query(Student).filter(Student.id == guardian.student_id).first()
        )
        if student is not None:
            _sync_primary_guardian_to_student(session, student)

        session.commit()
        return {"message": "監護人已刪除", "id": guardian_id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="刪除監護人失敗")
    finally:
        session.close()

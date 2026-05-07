"""api/parent_portal/activity.py — 家長端才藝課（登入版）。

Batch 7 範圍（plan 確認）：
- list courses（依學期過濾）
- 登入版報名：student_id 直接帶、parent_phone 從 Guardian 取、
  match_status='manual'、pending_review=False
- 列出家長所有子女的報名（單次查詢、用 student_id 過濾）
- 候補升正式（promoted_pending → enrolled，由家長確認）
- 報名繳費歷史（read-only；MVP 不含線上金流，員工 operator 欄位不揭露）
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func

from models.activity import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    ActivitySupply,
    RegistrationCourse,
    RegistrationSupply,
)
from models.database import Guardian, Student, get_session
from utils.auth import require_parent_role

from ._shared import _assert_student_owned, _get_parent_student_ids

router = APIRouter(prefix="/activity", tags=["parent-activity"])


class RegisterPayload(BaseModel):
    student_id: int = Field(..., gt=0)
    school_year: int = Field(..., ge=100, le=200)  # 民國
    semester: int = Field(..., ge=1, le=2)
    course_ids: list[int] = Field(default_factory=list)
    supply_ids: list[int] = Field(default_factory=list)


@router.get("/courses")
def list_courses(
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=2),
    current_user: dict = Depends(require_parent_role()),
):
    session = get_session()
    try:
        q = session.query(ActivityCourse).filter(ActivityCourse.is_active == True)
        if school_year is not None:
            q = q.filter(ActivityCourse.school_year == school_year)
        if semester is not None:
            q = q.filter(ActivityCourse.semester == semester)
        courses = q.order_by(ActivityCourse.name.asc()).all()

        # 計算每個 course 已報名（enrolled + promoted_pending）人數，用於前端顯示是否額滿
        if not courses:
            return {"items": [], "total": 0}
        course_ids = [c.id for c in courses]
        enrolled_counts = dict(
            session.query(
                RegistrationCourse.course_id, func.count(RegistrationCourse.id)
            )
            .filter(
                RegistrationCourse.course_id.in_(course_ids),
                RegistrationCourse.status.in_(("enrolled", "promoted_pending")),
            )
            .group_by(RegistrationCourse.course_id)
            .all()
        )
        items = [
            {
                "id": c.id,
                "name": c.name,
                "price": c.price,
                "sessions": c.sessions,
                "capacity": c.capacity,
                "school_year": c.school_year,
                "semester": c.semester,
                "allow_waitlist": bool(c.allow_waitlist),
                "description": c.description,
                "video_url": c.video_url,
                "enrolled_count": enrolled_counts.get(c.id, 0),
                "is_full": enrolled_counts.get(c.id, 0) >= (c.capacity or 0),
            }
            for c in courses
        ]
        return {"items": items, "total": len(items)}
    finally:
        session.close()


def _registration_summary(session, reg: ActivityRegistration) -> dict:
    """組合 registration 與 enrolled/waitlist courses 摘要。"""
    courses = (
        session.query(RegistrationCourse, ActivityCourse)
        .join(ActivityCourse, ActivityCourse.id == RegistrationCourse.course_id)
        .filter(RegistrationCourse.registration_id == reg.id)
        .all()
    )
    return {
        "id": reg.id,
        "student_id": reg.student_id,
        "student_name": reg.student_name,
        "school_year": reg.school_year,
        "semester": reg.semester,
        "is_paid": bool(reg.is_paid),
        "paid_amount": reg.paid_amount or 0,
        "match_status": reg.match_status,
        "pending_review": bool(reg.pending_review),
        "courses": [
            {
                "registration_course_id": rc.id,
                "course_id": rc.course_id,
                "course_name": c.name,
                "status": rc.status,
                "price_snapshot": rc.price_snapshot,
                "promoted_at": rc.promoted_at.isoformat() if rc.promoted_at else None,
                "confirm_deadline": (
                    rc.confirm_deadline.isoformat() if rc.confirm_deadline else None
                ),
            }
            for rc, c in courses
        ],
    }


@router.get("/my-registrations")
def my_registrations(current_user: dict = Depends(require_parent_role())):
    user_id = current_user["user_id"]
    session = get_session()
    try:
        _, student_ids = _get_parent_student_ids(session, user_id)
        if not student_ids:
            return {"items": [], "total": 0}
        rows = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_id.in_(student_ids),
                ActivityRegistration.is_active == True,
            )
            .order_by(ActivityRegistration.created_at.desc())
            .all()
        )
        return {
            "items": [_registration_summary(session, r) for r in rows],
            "total": len(rows),
        }
    finally:
        session.close()


@router.post("/register", status_code=201)
def register_courses(
    payload: RegisterPayload,
    current_user: dict = Depends(require_parent_role()),
):
    """登入版報名：student_id 必為自己小孩、parent_phone 自動從 Guardian 帶入。"""
    if not payload.course_ids and not payload.supply_ids:
        raise HTTPException(status_code=400, detail="至少需選擇一門課程或一項用品")

    user_id = current_user["user_id"]
    session = get_session()
    try:
        _assert_student_owned(session, user_id, payload.student_id, for_write=True)

        student = (
            session.query(Student).filter(Student.id == payload.student_id).first()
        )
        if student is None:
            raise HTTPException(status_code=404, detail="找不到學生")

        # 防同學期重複報名
        existing = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_id == payload.student_id,
                ActivityRegistration.school_year == payload.school_year,
                ActivityRegistration.semester == payload.semester,
                ActivityRegistration.is_active == True,
            )
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=400, detail="該學期已有活的報名，請先取消既有報名再重新提交"
            )

        guardian = (
            session.query(Guardian)
            .filter(
                Guardian.user_id == user_id,
                Guardian.student_id == payload.student_id,
                Guardian.deleted_at.is_(None),
            )
            .order_by(Guardian.is_primary.desc())
            .first()
        )
        parent_phone = guardian.phone if guardian else None

        reg = ActivityRegistration(
            student_name=student.name,
            birthday=(student.birthday.isoformat() if student.birthday else None),
            class_name=None,  # 由 classroom_id 解析；不寫快照避免老資料
            email=None,
            is_paid=False,
            paid_amount=0,
            is_active=True,
            school_year=payload.school_year,
            semester=payload.semester,
            student_id=student.id,
            parent_phone=parent_phone,
            classroom_id=student.classroom_id,
            pending_review=False,  # 登入版視為已驗證
            match_status="manual",
        )
        session.add(reg)
        session.flush()

        # 加入課程：依容量決定 enrolled / waitlist
        for course_id in payload.course_ids:
            course = (
                session.query(ActivityCourse)
                .filter(
                    ActivityCourse.id == course_id, ActivityCourse.is_active == True
                )
                .first()
            )
            if course is None:
                raise HTTPException(
                    status_code=400, detail=f"找不到課程 id={course_id}"
                )
            enrolled_count = (
                session.query(func.count(RegistrationCourse.id))
                .filter(
                    RegistrationCourse.course_id == course_id,
                    RegistrationCourse.status.in_(("enrolled", "promoted_pending")),
                )
                .scalar()
                or 0
            )
            if enrolled_count < (course.capacity or 0):
                status = "enrolled"
            elif course.allow_waitlist:
                status = "waitlist"
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"課程「{course.name}」已額滿且不開放候補",
                )
            session.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course_id,
                    status=status,
                    price_snapshot=course.price or 0,
                )
            )

        for supply_id in payload.supply_ids:
            supply = (
                session.query(ActivitySupply)
                .filter(
                    ActivitySupply.id == supply_id, ActivitySupply.is_active == True
                )
                .first()
            )
            if supply is None:
                raise HTTPException(
                    status_code=400, detail=f"找不到用品 id={supply_id}"
                )
            session.add(
                RegistrationSupply(
                    registration_id=reg.id,
                    supply_id=supply_id,
                    price_snapshot=supply.price or 0,
                )
            )

        session.commit()
        session.refresh(reg)
        return _registration_summary(session, reg)
    finally:
        session.close()


class ConfirmPromotionPayload(BaseModel):
    course_id: int = Field(..., gt=0)


@router.post("/registrations/{registration_id}/confirm-promotion")
def confirm_promotion(
    registration_id: int,
    payload: ConfirmPromotionPayload,
    current_user: dict = Depends(require_parent_role()),
):
    """家長確認候補升正式：promoted_pending → enrolled。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        # F-003：「報名不存在」「未綁定學生」「不屬於本家庭」一律 generic 403，
        # 避免透過 status code/detail 差異枚舉 ActivityRegistration id 存在性。
        _, owned_student_ids = _get_parent_student_ids(session, user_id)
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active == True,
            )
            .first()
        )
        if (
            reg is None
            or reg.student_id is None
            or reg.student_id not in owned_student_ids
        ):
            raise HTTPException(status_code=403, detail="查無此資料或無權存取")

        rc = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == registration_id,
                RegistrationCourse.course_id == payload.course_id,
            )
            .first()
        )
        if rc is None:
            raise HTTPException(status_code=404, detail="找不到該報名課程")
        if rc.status != "promoted_pending":
            raise HTTPException(status_code=400, detail=f"狀態為 {rc.status}，無法確認")
        if rc.confirm_deadline and rc.confirm_deadline < datetime.now():
            raise HTTPException(status_code=400, detail="確認期限已過")
        rc.status = "enrolled"
        session.commit()
        return {"status": "ok", "registration_course_id": rc.id}
    finally:
        session.close()


@router.get("/registrations/{registration_id}/payments")
def registration_payments(
    registration_id: int,
    current_user: dict = Depends(require_parent_role()),
):
    """報名繳費歷史；不揭露 operator 等員工欄位。"""
    user_id = current_user["user_id"]
    session = get_session()
    try:
        # F-003：「報名不存在」「未綁定學生」「不屬於本家庭」一律 generic 403。
        _, owned_student_ids = _get_parent_student_ids(session, user_id)
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active == True,
            )
            .first()
        )
        if (
            reg is None
            or reg.student_id is None
            or reg.student_id not in owned_student_ids
        ):
            raise HTTPException(status_code=403, detail="查無此資料或無權存取")

        rows = (
            session.query(ActivityPaymentRecord)
            .filter(
                ActivityPaymentRecord.registration_id == registration_id,
                ActivityPaymentRecord.voided_at.is_(None),
            )
            .order_by(
                ActivityPaymentRecord.payment_date.asc(),
                ActivityPaymentRecord.id.asc(),
            )
            .all()
        )
        return {
            "registration_id": registration_id,
            "items": [
                {
                    "type": r.type,
                    "amount": r.amount,
                    "payment_date": (
                        r.payment_date.isoformat() if r.payment_date else None
                    ),
                    "payment_method": r.payment_method,
                    "receipt_no": r.receipt_no,
                }
                for r in rows
            ],
        }
    finally:
        session.close()

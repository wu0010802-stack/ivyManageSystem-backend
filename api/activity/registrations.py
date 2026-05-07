"""
api/activity/registrations.py — 報名管理 CRUD core + items（courses/supplies）

含後台手動建立報名、列表/詳情/更新/作廢、子項加減（課程/用品）等核心 CRUD。

已拆出之子模組：
- registrations_static.py    batch-payment / export / payment-report
- registrations_pending.py   pending / match / reject / rematch / force-accept / restore
- registrations_payments.py  payment ledger（單筆 PUT/payment、payments 明細）

_lock_registration helper 仍保留在本檔，供 items 端點與 registrations_payments.py
共用（後者透過 sibling import 取用）。
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func
from sqlalchemy.exc import CompileError, IntegrityError, OperationalError

from models.database import (
    get_session,
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
    RegistrationSupply,
    ActivityPaymentRecord,
    RegistrationChange,
    ActivitySupply,
    ActivitySession,
    ActivityAttendance,
)
from services.activity_service import activity_service
from utils.errors import raise_safe_500
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.portfolio_access import can_view_guardian_pii, can_view_student_pii
from utils.finance_guards import (
    FINANCE_APPROVAL_THRESHOLD,
    require_finance_approve,
)

from ._shared import (
    PaymentUpdate,
    RemarkUpdate,
    BatchPaymentUpdate,
    AddPaymentRequest,
    AdminRegistrationPayload,
    AdminRegistrationBasicUpdate,
    AddCourseRequest,
    AddSupplyRequest,
    VoidPaymentRequest,
    SYSTEM_RECONCILE_METHOD,
    MIN_REFUND_REASON_LENGTH,
    _not_found,
    _derive_payment_status,
    _compute_is_paid,
    _calc_total_amount,
    _invalidate_activity_dashboard_caches,
    _invalidate_finance_summary_cache,
    _batch_calc_total_amounts,
    _build_registration_filter_query,
    _fetch_reg_course_names,
    _require_active_classroom,
    _require_daily_close_unlocked,
    _attach_courses,
    _attach_supplies,
    _match_student_id,
    has_payment_approve,
    require_refund_reason,
    require_approve_for_large_refund,
    require_approve_for_cumulative_refund,
    TAIPEI_TZ,
    get_line_service,
    today_taipei,
)
from utils.academic import resolve_academic_term_filters

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 靜態路由（batch-payment / export / payment-report）已拆至 registrations_static.py ──


# ── 後台手動新增報名 ─────────────────────────────────────────────────────────


@router.post("/registrations", status_code=201)
async def admin_create_registration(
    body: AdminRegistrationPayload,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台手動新增報名（不受報名開放時間限制，需 ACTIVITY_WRITE 權限）"""
    # 空報名守衛：至少要選 1 門課程，避免產生 total_amount=0 的殼子後又被 POS 誤收款
    if not body.courses:
        raise HTTPException(status_code=400, detail="請至少選擇一門課程再新增報名")

    session = get_session()
    try:
        sy, sem = resolve_academic_term_filters(body.school_year, body.semester)

        existing = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_name == body.name,
                ActivityRegistration.birthday == body.birthday,
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.school_year == sy,
                ActivityRegistration.semester == sem,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=400, detail="此學生本學期已有有效報名，請改用編輯功能"
            )

        classroom = _require_active_classroom(session, body.class_)

        course_names = [item.name for item in body.courses]
        if len(course_names) != len(set(course_names)):
            raise HTTPException(status_code=400, detail="課程清單中有重複項目")
        supply_names = [item.name for item in body.supplies]
        if len(supply_names) != len(set(supply_names)):
            raise HTTPException(status_code=400, detail="用品清單中有重複項目")

        courses_by_name = (
            {
                c.name: c
                for c in session.query(ActivityCourse)
                .filter(
                    ActivityCourse.name.in_(course_names),
                    ActivityCourse.is_active.is_(True),
                    ActivityCourse.school_year == sy,
                    ActivityCourse.semester == sem,
                )
                .with_for_update()
                .all()
            }
            if course_names
            else {}
        )

        supplies_by_name = (
            {
                s.name: s
                for s in session.query(ActivitySupply)
                .filter(
                    ActivitySupply.name.in_(supply_names),
                    ActivitySupply.is_active.is_(True),
                    ActivitySupply.school_year == sy,
                    ActivitySupply.semester == sem,
                )
                .all()
            }
            if supply_names
            else {}
        )

        _reg_course_ids = [c.id for c in courses_by_name.values()]
        enrolled_count_map = (
            dict(
                session.query(
                    RegistrationCourse.course_id, func.count(RegistrationCourse.id)
                )
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .filter(
                    RegistrationCourse.course_id.in_(_reg_course_ids),
                    RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
                    ActivityRegistration.is_active.is_(True),
                )
                .group_by(RegistrationCourse.course_id)
                .all()
            )
            if _reg_course_ids
            else {}
        )

        matched_student_id = _match_student_id(session, body.name, body.birthday)

        operator = current_user.get("username", "")
        reg = ActivityRegistration(
            student_name=body.name,
            birthday=body.birthday,
            class_name=classroom.name,
            email=body.email or None,
            remark=body.remark or None,
            school_year=sy,
            semester=sem,
            student_id=matched_student_id,
        )
        session.add(reg)
        session.flush()

        has_waitlist, waitlist_course_names = _attach_courses(
            session, reg.id, body.courses, courses_by_name, enrolled_count_map
        )
        _attach_supplies(session, reg.id, body.supplies, supplies_by_name)

        activity_service.log_change(
            session,
            reg.id,
            reg.student_name,
            "後台新增報名",
            f"班級：{classroom.name}，課程：{'、'.join(course_names) or '無'}，用品：{'、'.join(supply_names) or '無'}",
            operator,
        )

        session.commit()
        _invalidate_activity_dashboard_caches(session)
        logger.warning(
            "後台新增報名：id=%s student=%s operator=%s",
            reg.id,
            reg.student_name,
            operator,
        )

        return {
            "message": ("新增成功（部分課程進入候補）" if has_waitlist else "新增成功"),
            "id": reg.id,
            "waitlisted": has_waitlist,
            "waitlist_courses": waitlist_course_names,
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error("後台新增報名失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()


# ── 動態路由 /{registration_id}/... ─────────────────────────────────────────


@router.get("/registrations")
async def get_registrations(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = None,
    payment_status: Optional[str] = None,
    course_id: Optional[int] = None,
    classroom_name: Optional[str] = None,
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    match_status: Optional[str] = Query(
        None, pattern="^(matched|pending|manual|rejected|unmatched)$"
    ),
    include_inactive: bool = Query(False),
    student_id: Optional[int] = Query(
        None, gt=0, description="指定在校學生 ID，查詢其歷史報名紀錄"
    ),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得報名列表（分頁、搜尋、付款狀態、課程、班級、學期、匹配狀態篩選）。

    school_year 與 semester：可同時給或同時不給（不給則預設當前學期）。
    match_status：篩選自動匹配/手動綁定/待審核/拒絕等狀態。
    include_inactive：列出 rejected（軟刪除）的 registration 時需設 True。
    student_id：查詢單一學生的歷史報名紀錄（跨學期）；提供時通常搭配
      school_year=None、semester=None 才能看全部學期。
    """
    from ._shared import _build_registration_filter_query as _q_builder
    from utils.academic import resolve_academic_term_filters

    session = get_session()
    try:
        # 指定學生查全部學期時，顯式傳 None 避免被預設學期覆蓋
        if student_id is not None and school_year is None and semester is None:
            sy, sem = None, None
        else:
            sy, sem = resolve_academic_term_filters(school_year, semester)
        q = _q_builder(
            session,
            search=search,
            payment_status=payment_status,
            course_id=course_id,
            classroom_name=classroom_name,
            school_year=sy,
            semester=sem,
            match_status=match_status,
            include_inactive=include_inactive,
            student_id=student_id,
        )
        total = q.count()
        regs = (
            q.order_by(ActivityRegistration.created_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )
        reg_ids = [r.id for r in regs]

        course_count_map = {}
        supply_count_map = {}
        course_name_map: dict[int, list[str]] = defaultdict(list)
        course_amount_map = {}
        supply_amount_map = {}

        if reg_ids:
            # 查詢一：course_stats — 一次撈出所有課程資訊，Python 端同時建立 3 個 map
            course_stats = (
                session.query(
                    RegistrationCourse.registration_id,
                    RegistrationCourse.status,
                    ActivityCourse.name,
                    RegistrationCourse.price_snapshot,
                )
                .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
                .filter(RegistrationCourse.registration_id.in_(reg_ids))
                .all()
            )
            _course_count: dict = defaultdict(int)
            _course_amount: dict = defaultdict(int)
            for registration_id, status, course_name, price_snapshot in course_stats:
                _course_count[registration_id] += 1
                course_name_map[registration_id].append(
                    f"{course_name}（候補）" if status == "waitlist" else course_name
                )
                if status == "enrolled":
                    _course_amount[registration_id] += price_snapshot or 0
            course_count_map = dict(_course_count)
            course_amount_map = dict(_course_amount)

            # 查詢二：supply_stats — 一次撈出所有用品資訊，Python 端同時建立 2 個 map
            supply_stats = (
                session.query(
                    RegistrationSupply.registration_id,
                    RegistrationSupply.price_snapshot,
                )
                .filter(RegistrationSupply.registration_id.in_(reg_ids))
                .all()
            )
            _supply_count: dict = defaultdict(int)
            _supply_amount: dict = defaultdict(int)
            for registration_id, price_snapshot in supply_stats:
                _supply_count[registration_id] += 1
                _supply_amount[registration_id] += price_snapshot or 0
            supply_count_map = dict(_supply_count)
            supply_amount_map = dict(_supply_amount)

        # F-026：缺 STUDENTS_READ / GUARDIANS_READ 時遮罩對應 PII
        can_see_student = can_view_student_pii(current_user)
        can_see_guardian = can_view_guardian_pii(current_user)

        items = []
        for r in regs:
            paid_amount = r.paid_amount or 0
            total_amount = (course_amount_map.get(r.id, 0) or 0) + (
                supply_amount_map.get(r.id, 0) or 0
            )
            items.append(
                {
                    "id": r.id,
                    "student_name": r.student_name,
                    "student_id": r.student_id if can_see_student else None,
                    "birthday": r.birthday if can_see_student else None,
                    "class_name": r.class_name,
                    "classroom_id": r.classroom_id if can_see_student else None,
                    "parent_phone": r.parent_phone if can_see_guardian else None,
                    "match_status": r.match_status,
                    "pending_review": r.pending_review,
                    "is_active": r.is_active,
                    "email": r.email if can_see_guardian else None,
                    "is_paid": r.is_paid,
                    "paid_amount": paid_amount,
                    "total_amount": total_amount,
                    "payment_status": _derive_payment_status(paid_amount, total_amount),
                    "remark": r.remark or "",
                    "school_year": r.school_year,
                    "semester": r.semester,
                    "course_count": course_count_map.get(r.id, 0),
                    "supply_count": supply_count_map.get(r.id, 0),
                    "course_names": "、".join(course_name_map.get(r.id, [])),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    "reviewed_by": r.reviewed_by,
                    "reviewed_at": (
                        r.reviewed_at.isoformat() if r.reviewed_at else None
                    ),
                }
            )
        return {
            "items": items,
            "total": total,
            "skip": skip,
            "limit": limit,
            "school_year": sy,
            "semester": sem,
        }
    finally:
        session.close()


@router.get("/registrations/{registration_id}")
async def get_registration_detail(
    registration_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得報名詳情（含課程/用品/修改紀錄）"""
    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        if not reg:
            raise _not_found("報名資料")

        rc_rows = (
            session.query(RegistrationCourse, ActivityCourse)
            .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
            .filter(RegistrationCourse.registration_id == registration_id)
            .all()
        )
        courses = [
            {
                "id": rc.id,
                "course_id": ac.id,
                "name": ac.name,
                "price": rc.price_snapshot,
                "status": rc.status,
                "confirm_deadline": (
                    rc.confirm_deadline.isoformat()
                    if rc.status == "promoted_pending" and rc.confirm_deadline
                    else None
                ),
            }
            for rc, ac in rc_rows
        ]

        rs_rows = (
            session.query(RegistrationSupply, ActivitySupply)
            .join(ActivitySupply, RegistrationSupply.supply_id == ActivitySupply.id)
            .filter(RegistrationSupply.registration_id == registration_id)
            .all()
        )
        supplies = [
            {
                "id": rs.id,
                "supply_id": sp.id,
                "name": sp.name,
                "price": rs.price_snapshot,
            }
            for rs, sp in rs_rows
        ]

        changes = (
            session.query(RegistrationChange)
            .filter(RegistrationChange.registration_id == registration_id)
            .order_by(RegistrationChange.created_at.desc())
            .limit(20)
            .all()
        )
        change_list = [
            {
                "id": ch.id,
                "change_type": ch.change_type,
                "description": ch.description,
                "changed_by": ch.changed_by,
                "created_at": ch.created_at.isoformat() if ch.created_at else None,
            }
            for ch in changes
        ]

        total_amount = sum(c["price"] for c in courses if c["status"] == "enrolled")
        total_amount += sum(s["price"] for s in supplies)
        paid_amount = reg.paid_amount or 0

        # F-026：缺 STUDENTS_READ / GUARDIANS_READ 時遮罩對應 PII
        can_see_student = can_view_student_pii(current_user)
        can_see_guardian = can_view_guardian_pii(current_user)
        return {
            "id": reg.id,
            "student_name": reg.student_name,
            "student_id": reg.student_id if can_see_student else None,
            "birthday": reg.birthday if can_see_student else None,
            "class_name": reg.class_name,
            "classroom_id": reg.classroom_id if can_see_student else None,
            "parent_phone": reg.parent_phone if can_see_guardian else None,
            "match_status": reg.match_status,
            "pending_review": reg.pending_review,
            "reviewed_by": reg.reviewed_by,
            "reviewed_at": reg.reviewed_at.isoformat() if reg.reviewed_at else None,
            "email": reg.email if can_see_guardian else None,
            "is_paid": reg.is_paid,
            "paid_amount": paid_amount,
            "payment_status": _derive_payment_status(paid_amount, total_amount),
            "remark": reg.remark or "",
            "courses": courses,
            "supplies": supplies,
            "changes": change_list,
            "total_amount": total_amount,
            "created_at": reg.created_at.isoformat() if reg.created_at else None,
            "updated_at": reg.updated_at.isoformat() if reg.updated_at else None,
        }
    finally:
        session.close()


@router.put("/registrations/{registration_id}/remark")
async def update_remark(
    registration_id: int,
    body: RemarkUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """更新備註"""
    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        if not reg:
            raise _not_found("報名資料")

        reg.remark = body.remark
        activity_service.log_change(
            session,
            registration_id,
            reg.student_name,
            "更新備註",
            f"備註更新為：{body.remark}",
            current_user.get("username", ""),
        )
        session.commit()
        return {"message": "備註更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/registrations/{registration_id}")
async def update_registration_basic(
    registration_id: int,
    body: AdminRegistrationBasicUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台編輯報名基本欄位（姓名、生日、班級、Email）。
    學期不可變更，若需更改請重新建立報名。"""
    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        if not reg:
            raise _not_found("報名資料")

        classroom = _require_active_classroom(session, body.class_)

        # 同學期內姓名+生日不得重複於另一筆
        dup = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id != registration_id,
                ActivityRegistration.student_name == body.name,
                ActivityRegistration.birthday == body.birthday,
                ActivityRegistration.school_year == reg.school_year,
                ActivityRegistration.semester == reg.semester,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        if dup:
            raise HTTPException(
                status_code=400, detail="本學期已有另一筆相同姓名與生日的報名"
            )

        diffs: list[str] = []
        if reg.student_name != body.name:
            diffs.append(f"姓名：{reg.student_name} → {body.name}")
            reg.student_name = body.name
        if (reg.birthday or "") != body.birthday:
            diffs.append(f"生日：{reg.birthday or '—'} → {body.birthday}")
            reg.birthday = body.birthday
        if (reg.class_name or "") != classroom.name:
            diffs.append(f"班級：{reg.class_name or '—'} → {classroom.name}")
            reg.class_name = classroom.name
        new_email = body.email or None
        if (reg.email or None) != new_email:
            diffs.append(f"Email：{reg.email or '—'} → {new_email or '—'}")
            reg.email = new_email

        # 姓名+生日變更時重新匹配 student_id
        if any(d.startswith("姓名") or d.startswith("生日") for d in diffs):
            reg.student_id = _match_student_id(session, body.name, body.birthday)

        if diffs:
            activity_service.log_change(
                session,
                registration_id,
                reg.student_name,
                "編輯基本資料",
                "；".join(diffs),
                current_user.get("username", ""),
            )
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {"message": "基本資料更新成功", "changed": len(diffs)}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/registrations/{registration_id}/courses", status_code=201)
async def add_registration_course(
    registration_id: int,
    body: AddCourseRequest,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台為既有報名追加一筆課程（額滿時自動候補）。

    併發保護：鎖 reg 行，與 remove_registration_supply 對稱，避免與
    POS checkout / update_payment 並發時 is_paid 旗標短暫錯誤。
    """
    session = get_session()
    try:
        reg = _lock_registration(session, registration_id)
        if not reg:
            raise _not_found("報名資料")

        course = (
            session.query(ActivityCourse)
            .filter(
                ActivityCourse.id == body.course_id,
                ActivityCourse.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not course:
            raise _not_found("課程")

        # 不允許跨學期追加
        if course.school_year != reg.school_year or course.semester != reg.semester:
            raise HTTPException(status_code=400, detail="課程學期與報名學期不一致")

        # 不允許重複報名同課程
        exists = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == registration_id,
                RegistrationCourse.course_id == course.id,
            )
            .first()
        )
        if exists:
            raise HTTPException(status_code=400, detail="此報名已含該課程")

        # 佔容量 = enrolled + promoted_pending
        enrolled_count = (
            session.query(func.count(RegistrationCourse.id))
            .join(
                ActivityRegistration,
                RegistrationCourse.registration_id == ActivityRegistration.id,
            )
            .filter(
                RegistrationCourse.course_id == course.id,
                RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
                ActivityRegistration.is_active.is_(True),
            )
            .scalar()
            or 0
        )
        capacity = course.capacity if course.capacity is not None else 30
        if enrolled_count < capacity:
            status = "enrolled"
        elif course.allow_waitlist:
            status = "waitlist"
        else:
            raise HTTPException(
                status_code=400, detail=f"課程「{course.name}」已額滿且不開放候補"
            )

        paid_amount = reg.paid_amount or 0
        before_total = _calc_total_amount(session, registration_id)

        rc = RegistrationCourse(
            registration_id=registration_id,
            course_id=course.id,
            status=status,
            price_snapshot=course.price,
        )
        session.add(rc)
        session.flush()

        # 新增課程後可能把原本已繳清改為部分繳費；算出欠款供管理員即時追繳
        total_amount = _calc_total_amount(session, registration_id)
        reg.is_paid = _compute_is_paid(paid_amount, total_amount)
        outstanding_amount = max(0, total_amount - paid_amount)
        debt_delta = max(0, (total_amount - paid_amount) - (before_total - paid_amount))

        label = "候補" if status == "waitlist" else "正式"
        log_detail = f"課程「{course.name}」（{label}，價 ${course.price}）"
        if debt_delta > 0:
            log_detail += (
                f"，產生欠款 NT${debt_delta}（累計欠款 NT${outstanding_amount}）"
            )
        activity_service.log_change(
            session,
            registration_id,
            reg.student_name,
            "新增課程",
            log_detail,
            current_user.get("username", ""),
        )

        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {
            "message": "課程新增成功" + ("（候補）" if status == "waitlist" else ""),
            "status": status,
            "total_amount": total_amount,
            "paid_amount": paid_amount,
            "outstanding_amount": outstanding_amount,
            "payment_status": _derive_payment_status(paid_amount, total_amount),
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/registrations/{registration_id}/supplies", status_code=201)
async def add_registration_supply(
    registration_id: int,
    body: AddSupplyRequest,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台為既有報名追加一筆用品。

    併發保護：鎖 reg 行，與 remove_registration_supply 對稱。
    """
    session = get_session()
    try:
        reg = _lock_registration(session, registration_id)
        if not reg:
            raise _not_found("報名資料")

        supply = (
            session.query(ActivitySupply)
            .filter(
                ActivitySupply.id == body.supply_id,
                ActivitySupply.is_active.is_(True),
            )
            .first()
        )
        if not supply:
            raise _not_found("用品")

        if supply.school_year != reg.school_year or supply.semester != reg.semester:
            raise HTTPException(status_code=400, detail="用品學期與報名學期不一致")

        paid_amount = reg.paid_amount or 0
        before_total = _calc_total_amount(session, registration_id)

        rs = RegistrationSupply(
            registration_id=registration_id,
            supply_id=supply.id,
            price_snapshot=supply.price,
        )
        session.add(rs)
        session.flush()

        total_amount = _calc_total_amount(session, registration_id)
        reg.is_paid = _compute_is_paid(paid_amount, total_amount)
        outstanding_amount = max(0, total_amount - paid_amount)
        debt_delta = max(0, (total_amount - paid_amount) - (before_total - paid_amount))

        log_detail = f"用品「{supply.name}」（價 ${supply.price}）"
        if debt_delta > 0:
            log_detail += (
                f"，產生欠款 NT${debt_delta}（累計欠款 NT${outstanding_amount}）"
            )
        activity_service.log_change(
            session,
            registration_id,
            reg.student_name,
            "新增用品",
            log_detail,
            current_user.get("username", ""),
        )

        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {
            "message": "用品新增成功",
            "id": rs.id,
            "total_amount": total_amount,
            "paid_amount": paid_amount,
            "outstanding_amount": outstanding_amount,
            "payment_status": _derive_payment_status(paid_amount, total_amount),
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/registrations/{registration_id}/supplies/{supply_record_id}")
async def remove_registration_supply(
    registration_id: int,
    supply_record_id: int,
    force_refund: bool = Query(
        False,
        description="移除用品後若出現超繳，需顯式帶 true 才允許移除並自動寫退費沖帳紀錄",
    ),
    refund_reason: Optional[str] = Query(
        None,
        description="當 force_refund 觸發實際退費時必填（≥5 字），原因會寫入 notes 供稽核",
    ),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台移除已報名的單筆用品。

    併發保護：鎖住 reg 行，避免與其他端點（結帳、退課）同時改 is_paid。
    與 withdraw_course 對稱：若移除後 paid_amount > new_total，須顯式 force_refund。
    """
    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not reg:
            raise _not_found("報名資料")

        rs = (
            session.query(RegistrationSupply)
            .filter(
                RegistrationSupply.id == supply_record_id,
                RegistrationSupply.registration_id == registration_id,
            )
            .first()
        )
        if not rs:
            raise _not_found("用品記錄")

        supply = (
            session.query(ActivitySupply)
            .filter(ActivitySupply.id == rs.supply_id)
            .first()
        )
        supply_name = supply.name if supply else str(rs.supply_id)

        paid_amount = reg.paid_amount or 0
        before_total = _calc_total_amount(session, registration_id)
        # 估算移除後的應繳；真正的 refund_needed 在 flush 後用 new_total 重算
        estimated_after_total = before_total - int(rs.price_snapshot or 0)
        preview_refund = max(0, paid_amount - estimated_after_total)

        if preview_refund > 0 and not force_refund:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"移除用品後將產生超繳 NT${preview_refund}（已繳 NT${paid_amount}、"
                    f"移除後應繳 NT${estimated_after_total}），請先處理退費或於移除時"
                    f"指定 force_refund=true 自動沖帳"
                ),
            )

        # 與正式退費端點同套守衛：fail-fast，避免 ACTIVITY_WRITE 經自動沖帳繞過
        # require_refund_reason / require_approve_for_large_refund 簽核閾值
        cleaned_reason: Optional[str] = None
        if preview_refund > 0 and force_refund:
            cleaned_reason = require_refund_reason(refund_reason)
            require_approve_for_cumulative_refund(
                session,
                registration_id,
                preview_refund,
                current_user,
                label="移除用品自動沖帳累積退費總額",
            )

        session.delete(rs)
        session.flush()

        new_total = _calc_total_amount(session, registration_id)
        refund_needed = max(0, paid_amount - new_total)

        if refund_needed > 0 and force_refund:
            today = today_taipei()
            _require_daily_close_unlocked(session, today)
            session.add(
                ActivityPaymentRecord(
                    registration_id=registration_id,
                    type="refund",
                    amount=refund_needed,
                    payment_date=today,
                    payment_method=SYSTEM_RECONCILE_METHOD,
                    notes=(
                        f"（移除用品「{supply_name}」自動沖帳）原因：{cleaned_reason}"
                        if cleaned_reason
                        else f"（移除用品「{supply_name}」自動沖帳）"
                    ),
                    operator=current_user.get("username", ""),
                )
            )
            reg.paid_amount = max(0, paid_amount - refund_needed)

        reg.is_paid = _compute_is_paid(reg.paid_amount or 0, new_total)

        log_detail = f"用品「{supply_name}」已移除"
        if refund_needed > 0 and force_refund:
            log_detail += f"（自動沖帳退費 NT${refund_needed}）"
        activity_service.log_change(
            session,
            registration_id,
            reg.student_name,
            "移除用品",
            log_detail,
            current_user.get("username", ""),
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        final_paid = reg.paid_amount or 0
        return {
            "message": f"已移除用品「{supply_name}」",
            "total_amount": new_total,
            "paid_amount": final_paid,
            "refunded_amount": refund_needed if force_refund else 0,
            "payment_status": _derive_payment_status(final_paid, new_total),
        }
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


def _lock_registration(session, registration_id: int):
    """對單筆 registration 取得行級鎖；SQLite（單元測試）自動降級為無鎖。"""
    query = session.query(ActivityRegistration).filter(
        ActivityRegistration.id == registration_id,
        ActivityRegistration.is_active.is_(True),
    )
    try:
        return query.with_for_update().first()
    except (CompileError, OperationalError, NotImplementedError):
        return query.first()


@router.put("/registrations/{registration_id}/waitlist")
async def promote_waitlist(
    registration_id: int,
    background_tasks: BackgroundTasks,
    course_id: int = Query(...),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """管理員手動將候補或 promoted_pending 直接升為正式報名（跳過 24h 確認窗）。"""
    session = get_session()
    try:
        student_name, course_name = activity_service.promote_waitlist(
            session, registration_id, course_id
        )

        activity_service.log_change(
            session,
            registration_id,
            student_name,
            "候補升正式",
            f"課程「{course_name}」由管理員手動升為正式",
            current_user.get("username", ""),
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        line_svc = get_line_service()
        if line_svc is not None:
            # 管理員手動升正式：通知不帶 deadline（已確認）
            background_tasks.add_task(
                line_svc.notify_activity_waitlist_promoted,
                student_name,
                course_name,
                None,
            )
        return {"message": "成功升為正式報名"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/waitlist/sweep-expired")
async def sweep_expired_waitlist_promotions(
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """管理員手動觸發候補轉正過期掃描（排程異常時備援）。"""
    session = get_session()
    try:
        result = activity_service.sweep_expired_pending_promotions(session)
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        logger.info(
            "手動觸發候補過期掃描：operator=%s expired=%s reminded=%s",
            current_user.get("username", ""),
            result["expired"],
            result["reminded"],
        )
        return {"message": "候補過期掃描完成", **result}
    except Exception as e:
        session.rollback()
        logger.error("候補過期掃描失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/registrations/{registration_id}/courses/{course_id}")
async def withdraw_course(
    registration_id: int,
    course_id: int,
    request: Request,
    force_refund: bool = Query(
        False,
        description="退課後若出現超繳，需顯式帶 true 才允許退課並自動寫退費沖帳紀錄",
    ),
    refund_reason: Optional[str] = Query(
        None,
        description="當 force_refund 觸發實際退費時必填（≥5 字），原因會寫入 notes 供稽核",
    ),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """退出單一課程（含候補），若為正式報名則自動升位候補

    併發保護：鎖住 reg 行，避免兩個 DELETE 同時扣 paid_amount / 重複沖帳。
    第二個請求會在鎖釋放後發現 RC 已被刪而拿到 404。
    """
    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not reg:
            raise _not_found("報名資料")

        from models.database import RegistrationCourse as RC

        rc = (
            session.query(RC)
            .filter(
                RC.registration_id == registration_id,
                RC.course_id == course_id,
            )
            .first()
        )
        if not rc:
            raise _not_found("課程報名項目")

        from models.database import ActivityCourse as AC

        course = session.query(AC).filter(AC.id == course_id).first()
        course_name = course.name if course else str(course_id)
        was_enrolled = rc.status == "enrolled"
        # enrolled 與 promoted_pending 都佔容量，刪除後都應嘗試遞補下一位候補
        was_occupying = rc.status in ("enrolled", "promoted_pending")

        # 先估算退課後的 total 用於 409 預檢；實際退費金額在 flush 後以 new_total 重算
        paid_amount = reg.paid_amount or 0
        # 估算退課後的 new_total：從目前 total 扣掉該 enrolled 項目的 price_snapshot
        # （candidate 列在 RegistrationCourse，候補 status 非 enrolled 不計入 total）
        before_total = _calc_total_amount(session, registration_id)
        if was_enrolled:
            estimated_after_total = before_total - int(rc.price_snapshot or 0)
        else:
            estimated_after_total = before_total
        preview_refund = max(0, paid_amount - estimated_after_total)

        if preview_refund > 0 and not force_refund:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"退課後將產生超繳 NT${preview_refund}（已繳 NT${paid_amount}、"
                    f"退課後應繳 NT${estimated_after_total}），請先處理退費或於退課時"
                    f"指定 force_refund=true 自動沖帳"
                ),
            )

        # 與正式退費端點同套守衛：fail-fast，避免 ACTIVITY_WRITE 經自動沖帳繞過
        # require_refund_reason / require_approve_for_large_refund 簽核閾值
        cleaned_reason: Optional[str] = None
        if preview_refund > 0 and force_refund:
            cleaned_reason = require_refund_reason(refund_reason)
            require_approve_for_cumulative_refund(
                session,
                registration_id,
                preview_refund,
                current_user,
                label="退課自動沖帳累積退費總額",
            )

        session.delete(rc)
        session.flush()

        # 清除該生在此課程所有場次的點名紀錄，避免統計把退課者算入，
        # 並防止未來重新報名時撞到 uq_activity_attendance_session_reg。
        session_ids_subq = (
            session.query(ActivitySession.id)
            .filter(ActivitySession.course_id == course_id)
            .subquery()
        )
        removed_attendance = (
            session.query(ActivityAttendance)
            .filter(
                ActivityAttendance.registration_id == registration_id,
                ActivityAttendance.session_id.in_(session_ids_subq),
            )
            .delete(synchronize_session=False)
        )

        if was_occupying:
            activity_service._auto_promote_first_waitlist(session, course_id)

        new_total = _calc_total_amount(session, registration_id)
        # 以 new_total 重算 refund_needed：避免 estimated_after_total 與實際 DB 狀態漂移
        # （例如 auto_promote 連動、或未來在 delete/flush 之間插入其他邏輯時自我校驗）
        refund_needed = max(0, paid_amount - new_total)

        # 若需要自動沖帳，寫 refund 紀錄並扣 paid_amount
        if refund_needed > 0 and force_refund:
            today = today_taipei()
            _require_daily_close_unlocked(session, today)
            session.add(
                ActivityPaymentRecord(
                    registration_id=registration_id,
                    type="refund",
                    amount=refund_needed,
                    payment_date=today,
                    payment_method=SYSTEM_RECONCILE_METHOD,
                    notes=(
                        f"（退課「{course_name}」自動沖帳）原因：{cleaned_reason}"
                        if cleaned_reason
                        else f"（退課「{course_name}」自動沖帳）"
                    ),
                    operator=current_user.get("username", ""),
                )
            )
            reg.paid_amount = max(0, paid_amount - refund_needed)

        reg.is_paid = _compute_is_paid(reg.paid_amount or 0, new_total)

        log_detail = f"退出課程「{course_name}」"
        if removed_attendance:
            log_detail += f"（同步清除 {removed_attendance} 筆舊點名紀錄）"
        if refund_needed > 0 and force_refund:
            log_detail += f"（自動沖帳退費 NT${refund_needed}）"
        activity_service.log_change(
            session,
            registration_id,
            reg.student_name,
            "退課",
            log_detail,
            current_user.get("username", ""),
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        final_paid = reg.paid_amount or 0
        # URL 尾段為 course_id，覆寫為 registration_id 以便依報名 ID 彙整稽核事件
        request.state.audit_entity_id = str(registration_id)
        request.state.audit_summary = f"退課：{reg.student_name} 退出「{course_name}」"
        request.state.audit_changes = {
            "student_name": reg.student_name,
            "course_id": course_id,
            "course_name": course_name,
            "was_enrolled": was_enrolled,
            "refund_needed": refund_needed,
            "force_refund": force_refund,
            "paid_amount_after": final_paid,
            "total_amount_after": new_total,
            "removed_attendance_count": removed_attendance,
        }
        return {
            "message": f"已退出課程「{course_name}」",
            "total_amount": new_total,
            "paid_amount": final_paid,
            "refunded_amount": refund_needed if force_refund else 0,
            "payment_status": _derive_payment_status(final_paid, new_total),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/registrations/{registration_id}")
async def delete_registration(
    registration_id: int,
    request: Request,
    force_refund: bool = Query(
        False,
        description="若報名已有繳費金額，需顯式帶 true 才允許刪除並自動寫退費沖帳紀錄",
    ),
    refund_reason: Optional[str] = Query(
        None,
        description="當 force_refund 觸發實際退費時必填（≥5 字），原因會寫入 notes 供稽核",
    ),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """軟刪除報名"""
    session = get_session()
    try:
        reg_preview = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        student_name = reg_preview.student_name if reg_preview else None
        paid_before = (reg_preview.paid_amount or 0) if reg_preview else 0

        # 與正式退費端點同套守衛：fail-fast，避免 ACTIVITY_WRITE 經自動沖帳繞過
        # require_refund_reason / require_approve_for_large_refund 簽核閾值
        cleaned_reason: Optional[str] = None
        if paid_before > 0 and force_refund:
            cleaned_reason = require_refund_reason(refund_reason)
            require_approve_for_cumulative_refund(
                session,
                registration_id,
                paid_before,
                current_user,
                label="刪除報名自動沖帳累積退費總額",
            )

        activity_service.delete_registration(
            session,
            registration_id,
            current_user.get("username", ""),
            force_refund=force_refund,
            refund_reason=cleaned_reason,
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        logger.warning(
            "課後才藝報名已刪除：id=%s operator=%s force_refund=%s",
            registration_id,
            current_user.get("username"),
            force_refund,
        )
        request.state.audit_summary = (
            f"刪除才藝報名：{student_name}" if student_name else "刪除才藝報名"
        )
        request.state.audit_changes = {
            "student_name": student_name,
            "paid_amount_before": paid_before,
            "force_refund": force_refund,
        }
        return {"message": "報名已刪除"}
    except ValueError as e:
        msg = str(e)
        # 找不到報名 → 404；尚有已繳金額 → 409（需呼叫端確認）
        if "找不到" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=409, detail=msg)
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()

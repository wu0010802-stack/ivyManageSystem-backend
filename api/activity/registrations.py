"""
api/activity/registrations.py — 報名管理 CRUD core

含 8 個核心端點：admin_create / list / detail / update / remark / waitlist /
sweep-expired / delete。不含繳費／審核／靜態匯出／子項加減（已拆出子模組）。

已拆出之子模組：
- registrations_static.py    batch-payment / export / payment-report
- registrations_pending.py   pending / match / reject / rematch / force-accept / restore
- registrations_payments.py  payment ledger（單筆 PUT/payment、payments 明細）
- registrations_items.py     /{id}/courses 與 /{id}/supplies 的加減
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func

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
    User,
)
from services.activity_service import activity_service
from services.activity_refund_query import build_refund_suggestion
from utils.errors import raise_safe_500
from utils.auth import require_staff_permission
from utils.advisory_lock import acquire_activity_registration_lock
from utils.permissions import Permission, list_active_user_ids_with_permission
from utils.portfolio_access import can_view_guardian_pii
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
    resolve_student_pii_scope,
    student_pii_row_visible,
    has_payment_approve,
    require_refund_reason,
    require_approve_for_large_refund,
    require_approve_for_cumulative_refund,
    TAIPEI_TZ,
    today_taipei,
)
from utils.academic import resolve_academic_term_filters
from schemas._common import DeleteResultOut
from schemas.activity_admin import (
    RefundSuggestionResponse,
    RegistrationBasicUpdateResultOut,
    RegistrationCreateResultOut,
    RegistrationDetailOut,
    RegistrationListOut,
    WaitlistSweepResultOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _list_active_users_with_permission(session, perm: str) -> list[int]:
    """SQLite/PG 通用：列 permission_names 含 perm 的 active user_id。

    對齊 api/permissions_admin.py:136-145 / api/portal/leaves.py 同名 helper。
    """
    return list_active_user_ids_with_permission(session, perm)


# ── 靜態路由（batch-payment / export / payment-report）已拆至 registrations_static.py ──


# ── 後台手動新增報名 ─────────────────────────────────────────────────────────


@router.post(
    "/registrations", status_code=201, response_model=RegistrationCreateResultOut
)
def admin_create_registration(
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

        # P2-2：以報名身分取 advisory lock 序列化同學生同學期的並發建立。去重是
        # check-then-insert（純 SELECT 後 INSERT，無行鎖），且 DB partial unique
        # index 鍵含 parent_phone，admin 路徑建立的列 parent_phone 為 NULL，PG 預設
        # NULL 互不相等故不攔重複 → 兩名 admin 同時建立同學生會產生兩筆有效報名
        # （容量多佔、在籍人數灌水、對帳分裂）。SQLite 測試 no-op。
        acquire_activity_registration_lock(
            session,
            student_name=body.name,
            birthday=body.birthday,
            school_year=sy,
            semester=sem,
        )

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


@router.get("/registrations", response_model=RegistrationListOut)
def get_registrations(
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
        # S7：STUDENTS_READ:own_class 者對非管轄班級的列照樣遮罩（per-row）
        pii_visible, pii_allowed = resolve_student_pii_scope(session, current_user)
        can_see_guardian = can_view_guardian_pii(current_user)

        items = []
        for r in regs:
            can_see_student = student_pii_row_visible(
                pii_visible, pii_allowed, r.classroom_id
            )
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


@router.get("/registrations/{registration_id}", response_model=RegistrationDetailOut)
def get_registration_detail(
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
        # S7：STUDENTS_READ:own_class 者對非管轄班級的報名照樣遮罩
        pii_visible, pii_allowed = resolve_student_pii_scope(session, current_user)
        can_see_student = student_pii_row_visible(
            pii_visible, pii_allowed, reg.classroom_id
        )
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


@router.put("/registrations/{registration_id}/remark", response_model=DeleteResultOut)
def update_remark(
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


@router.put(
    "/registrations/{registration_id}", response_model=RegistrationBasicUpdateResultOut
)
def update_registration_basic(
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


@router.put("/registrations/{registration_id}/waitlist", response_model=DeleteResultOut)
def promote_waitlist(
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

        # 通知：commit 前對每位 ACTIVITY_WRITE staff 個人推送（in_app + LINE）
        # 原 background_tasks LINE 群組廣播改為 per-staff dispatch.enqueue。
        try:
            from services.notification import dispatch

            staff_user_ids = _list_active_users_with_permission(
                session, Permission.ACTIVITY_WRITE.value
            )
            for sid in staff_user_ids:
                dispatch.enqueue(
                    session=session,
                    event_type="activity.waitlist_promoted",
                    recipient_user_id=sid,
                    context={
                        "student_name": student_name,
                        "course_name": course_name,
                        "course_id": course_id,
                    },
                    sender_id=current_user.get("user_id"),
                    source_entity_type="registration_course",
                    source_entity_id=registration_id,
                )
        except Exception as exc:
            logger.warning("activity.waitlist_promoted enqueue 失敗（已吞）：%s", exc)

        session.commit()
        _invalidate_activity_dashboard_caches(session)
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


@router.post("/waitlist/sweep-expired", response_model=WaitlistSweepResultOut)
def sweep_expired_waitlist_promotions(
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """管理員手動觸發候補轉正過期掃描（排程異常時備援）。"""
    session = get_session()
    try:
        result = activity_service.sweep_expired_pending_promotions(session)
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        logger.info(
            "手動觸發候補過期掃描：operator=%s expired=%s reminded=%s final_reminded=%s",
            current_user.get("username", ""),
            result["expired"],
            result["reminded"],
            result.get("final_reminded", 0),
        )
        return {"message": "候補過期掃描完成", **result}
    except Exception as e:
        session.rollback()
        logger.error("候補過期掃描失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/registrations/{registration_id}", response_model=DeleteResultOut)
def delete_registration(
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


@router.get(
    "/registrations/{registration_id}/refund-suggestion",
    response_model=RefundSuggestionResponse,
)
def get_refund_suggestion(
    registration_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """取得 registration 的退費建議（每門 course / 每筆 supply 分開列出）。

    spec §7：前端 POS UI 在退費前 GET 此 endpoint 預載建議值。
    Server-side build_refund_suggestion 同套邏輯也用於 POS verify。

    Returns: RefundSuggestionResponse
    Raises:
        404: reg 不存在或 is_active=False
        403: 無 ACTIVITY_WRITE 權限
    """
    session = get_session()
    try:
        result = build_refund_suggestion(session, registration_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        session.close()

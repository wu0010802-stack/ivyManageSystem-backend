"""
api/activity/registrations_items.py — 才藝報名子項加減（課程／用品）

含 4 個端點，處理已建立的 registration 上動態加減課程或文具用品：
- POST   /registrations/{id}/courses             加課程（額滿自動候補）
- DELETE /registrations/{id}/courses/{course_id} 退課（含退費沖帳）
- POST   /registrations/{id}/supplies            加用品
- DELETE /registrations/{id}/supplies/{supply_id} 退用品

所有端點皆會異動 reg.paid_amount / total_amount，需透過 _lock_registration
取得行級鎖；該 helper 仍保留於 registrations.py（CRUD core），透過 sibling
import 取用。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func

from models.database import (
    get_session,
    ActivityRegistration,
    ActivityCourse,
    RegistrationCourse,
    ActivitySupply,
    RegistrationSupply,
    ActivityPaymentRecord,
    ActivitySession,
    ActivityAttendance,
)
from services.activity_service import activity_service
from utils.errors import raise_safe_500
from utils.auth import require_staff_permission
from utils.permissions import Permission

from ._shared import (
    AddCourseRequest,
    AddSupplyRequest,
    SYSTEM_RECONCILE_METHOD,
    _not_found,
    _calc_total_amount,
    _compute_is_paid,
    _derive_payment_status,
    _invalidate_activity_dashboard_caches,
    _lock_registration,
    _require_daily_close_unlocked,
    require_refund_reason,
    require_approve_for_large_refund,
    require_approve_for_cumulative_refund,
    today_taipei,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/registrations/{registration_id}/courses", status_code=201)
def add_registration_course(
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
def add_registration_supply(
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
def remove_registration_supply(
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


@router.delete("/registrations/{registration_id}/courses/{course_id}")
def withdraw_course(
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
        # 與本檔其他端點一致：HTTPException 也走 rollback，避免 except 之前的
        # session.delete / flush 殘留在事務中
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()

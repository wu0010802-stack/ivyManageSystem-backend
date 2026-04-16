"""
api/activity/registrations.py — 報名管理端點（含 batch-payment、export）

⚠️ 注意：batch-payment 和 export 為靜態路由，必須定義在 /{registration_id}/... 之前。
"""

import io
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func

from models.database import (
    get_session,
    ActivityCourse, ActivityRegistration,
    RegistrationCourse, RegistrationSupply,
    ActivityPaymentRecord, RegistrationChange,
    ActivitySupply,
)
from services.activity_service import activity_service
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.rate_limit import SlidingWindowLimiter

from ._shared import (
    PaymentUpdate, RemarkUpdate, BatchPaymentUpdate, AddPaymentRequest,
    _not_found, _derive_payment_status, _calc_total_amount,
    _invalidate_activity_dashboard_caches, _batch_calc_total_amounts,
    _build_registration_filter_query, _fetch_reg_course_names,
    TAIPEI_TZ,
    get_line_service,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_export_limiter = SlidingWindowLimiter(
    max_calls=5,
    window_seconds=60,
    name="activity_export",
    error_detail="匯出過於頻繁，請稍後再試",
).as_dependency()


# ── 靜態路由（必須優先定義，在 /{id}/... 動態路由之前）─────────────────────

@router.put("/registrations/batch-payment")
async def batch_update_payment(
    body: BatchPaymentUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """批次標記付款狀態（使用 GROUP BY 避免 N+1 查詢）"""
    session = get_session()
    try:
        regs = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id.in_(body.ids),
                ActivityRegistration.is_active.is_(True),
            )
            .all()
        )
        if not regs:
            raise HTTPException(status_code=404, detail="找不到指定報名資料")

        status_str = "已繳費" if body.is_paid else "未繳費"
        operator = current_user.get("username", "")
        today = datetime.now().date()

        if body.is_paid:
            # P3 N+1 修正：一次 GROUP BY 查詢所有應繳金額
            unpaid_reg_ids = [reg.id for reg in regs if not reg.is_paid]
            total_amount_map = _batch_calc_total_amounts(session, unpaid_reg_ids) if unpaid_reg_ids else {}

            for reg in regs:
                if not reg.is_paid:
                    total_amount = total_amount_map.get(reg.id, 0)
                    shortfall = total_amount - (reg.paid_amount or 0)
                    if shortfall > 0:
                        rec = ActivityPaymentRecord(
                            registration_id=reg.id,
                            type="payment",
                            amount=shortfall,
                            payment_date=today,
                            payment_method="現金",
                            notes="（批次標記已繳費自動補齊）",
                            operator=operator,
                        )
                        session.add(rec)
                        reg.paid_amount = total_amount
                    reg.is_paid = True
                activity_service.log_change(
                    session, reg.id, reg.student_name,
                    "批次更新付款狀態", f"付款狀態批次更新為：{status_str}",
                    operator,
                )
        else:
            reg_ids_to_reset = [reg.id for reg in regs]
            session.query(ActivityPaymentRecord).filter(
                ActivityPaymentRecord.registration_id.in_(reg_ids_to_reset)
            ).delete(synchronize_session=False)
            for reg in regs:
                reg.paid_amount = 0
                reg.is_paid = False
                activity_service.log_change(
                    session, reg.id, reg.student_name,
                    "批次更新付款狀態", f"付款狀態批次更新為：{status_str}",
                    operator,
                )

        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        logger.warning(
            "批次付款狀態更新：筆數=%d is_paid=%s operator=%s",
            len(regs), body.is_paid, operator,
        )
        return {"message": f"已更新 {len(regs)} 筆報名為{status_str}", "updated": len(regs)}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/registrations/export")
async def export_registrations(
    search: Optional[str] = None,
    payment_status: Optional[str] = None,
    course_id: Optional[int] = None,
    classroom_name: Optional[str] = None,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """匯出報名名單為 Excel"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill

    session = get_session()
    try:
        q = _build_registration_filter_query(
            session,
            search=search,
            payment_status=payment_status,
            course_id=course_id,
            classroom_name=classroom_name,
        )
        regs = q.order_by(ActivityRegistration.created_at.desc()).all()
        reg_ids = [r.id for r in regs]
        course_name_map = _fetch_reg_course_names(session, reg_ids)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "報名名單"

        header_font = Font(bold=True)
        header_fill = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
        center = Alignment(horizontal="center", vertical="center")

        headers = ["序號", "學生姓名", "班級", "課程", "付款狀態", "備註", "報名時間"]
        for col, h in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center

        for idx, reg in enumerate(regs, start=1):
            ws.append([
                idx,
                reg.student_name,
                reg.class_name or "",
                "、".join(course_name_map.get(reg.id, [])),
                "已繳費" if reg.is_paid else "未繳費",
                reg.remark or "",
                reg.created_at.strftime("%Y-%m-%d %H:%M") if reg.created_at else "",
            ])

        for col in ws.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=0)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        filename = f"activity_registrations_{datetime.now(TAIPEI_TZ).strftime('%Y%m%d_%H%M%S')}.xlsx"
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    finally:
        session.close()


@router.get("/registrations/payment-report")
async def export_payment_report(
    search: Optional[str] = None,
    payment_status: Optional[str] = None,
    course_id: Optional[int] = None,
    classroom_name: Optional[str] = None,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
    _: None = Depends(_export_limiter),
):
    """匯出繳費帳務報表（兩個工作表：繳費總覽 + 繳費明細）"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill

    _HEADER_FONT = Font(bold=True)
    _HEADER_FILL = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
    _CENTER = Alignment(horizontal="center", vertical="center")

    session = get_session()
    try:
        q = _build_registration_filter_query(
            session,
            search=search,
            payment_status=payment_status,
            course_id=course_id,
            classroom_name=classroom_name,
        )
        regs = q.order_by(ActivityRegistration.created_at.desc()).all()
        reg_ids = [r.id for r in regs]

        # 批次計算應繳金額
        total_amount_map = _batch_calc_total_amounts(session, reg_ids) if reg_ids else {}

        # 批次查詢課程名稱
        course_name_map = _fetch_reg_course_names(session, reg_ids)

        # 批次查詢繳費明細
        payment_records = []
        payment_map: dict[int, list] = defaultdict(list)
        last_payment_date_map: dict[int, str] = {}
        if reg_ids:
            payment_records = (
                session.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id.in_(reg_ids))
                .order_by(
                    ActivityPaymentRecord.registration_id,
                    ActivityPaymentRecord.payment_date.asc(),
                )
                .all()
            )
            for pr in payment_records:
                payment_map[pr.registration_id].append(pr)
                if pr.payment_date:
                    date_str = pr.payment_date.isoformat()
                    existing = last_payment_date_map.get(pr.registration_id, "")
                    if date_str > existing:
                        last_payment_date_map[pr.registration_id] = date_str

        wb = openpyxl.Workbook()

        # ── 工作表一：繳費總覽 ──────────────────────────────────────────
        ws1 = wb.active
        ws1.title = "繳費總覽"
        headers1 = ["序號", "學生", "班級", "報名課程", "應繳總額", "已繳金額", "差額", "狀態", "最後繳費日"]
        for col, h in enumerate(headers1, start=1):
            cell = ws1.cell(row=1, column=col, value=h)
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
            cell.alignment = _CENTER

        status_label_map = {
            "paid": "已繳清",
            "partial": "部分繳費",
            "unpaid": "未繳費",
            "overpaid": "超額繳費",
        }
        for idx, reg in enumerate(regs, start=1):
            total = total_amount_map.get(reg.id, 0)
            paid = reg.paid_amount or 0
            diff = paid - total
            status = _derive_payment_status(paid, total)
            ws1.append([
                idx,
                reg.student_name,
                reg.class_name or "",
                "、".join(course_name_map.get(reg.id, [])),
                total,
                paid,
                diff,
                status_label_map.get(status, status),
                last_payment_date_map.get(reg.id, ""),
            ])

        for col in ws1.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=0)
            ws1.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

        # ── 工作表二：繳費明細 ──────────────────────────────────────────
        ws2 = wb.create_sheet(title="繳費明細")
        headers2 = ["學生", "班級", "類型", "金額", "方式", "日期", "操作人員", "備註"]
        for col, h in enumerate(headers2, start=1):
            cell = ws2.cell(row=1, column=col, value=h)
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
            cell.alignment = _CENTER

        reg_meta = {r.id: r for r in regs}
        for pr in payment_records:
            reg = reg_meta.get(pr.registration_id)
            ws2.append([
                reg.student_name if reg else "",
                reg.class_name if reg else "",
                "繳費" if pr.type == "payment" else "退費",
                pr.amount,
                pr.payment_method or "",
                pr.payment_date.isoformat() if pr.payment_date else "",
                pr.operator or "",
                pr.notes or "",
            ])

        for col in ws2.columns:
            max_len = max((len(str(cell.value or "")) for cell in col), default=0)
            ws2.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        filename = f"payment_report_{datetime.now(TAIPEI_TZ).strftime('%Y%m%d_%H%M%S')}.xlsx"
        logger.warning(
            "繳費帳務報表匯出：operator=%s 筆數=%d",
            current_user.get("username"), len(regs),
        )
        return StreamingResponse(
            buf,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
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
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得報名列表（分頁、搜尋、付款狀態、課程、班級篩選）"""
    session = get_session()
    try:
        q = _build_registration_filter_query(
            session,
            search=search,
            payment_status=payment_status,
            course_id=course_id,
            classroom_name=classroom_name,
        )
        total = q.count()
        regs = q.order_by(ActivityRegistration.created_at.desc()).offset(skip).limit(limit).all()
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

        items = []
        for r in regs:
            paid_amount = r.paid_amount or 0
            total_amount = (course_amount_map.get(r.id, 0) or 0) + (supply_amount_map.get(r.id, 0) or 0)
            items.append({
                "id": r.id,
                "student_name": r.student_name,
                "birthday": r.birthday,
                "class_name": r.class_name,
                "email": r.email,
                "is_paid": r.is_paid,
                "paid_amount": paid_amount,
                "total_amount": total_amount,
                "payment_status": _derive_payment_status(paid_amount, total_amount),
                "remark": r.remark or "",
                "course_count": course_count_map.get(r.id, 0),
                "supply_count": supply_count_map.get(r.id, 0),
                "course_names": "、".join(course_name_map.get(r.id, [])),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            })
        return {"items": items, "total": total, "skip": skip, "limit": limit}
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
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
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
            {"id": rs.id, "supply_id": sp.id, "name": sp.name, "price": rs.price_snapshot}
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

        return {
            "id": reg.id,
            "student_name": reg.student_name,
            "birthday": reg.birthday,
            "class_name": reg.class_name,
            "email": reg.email,
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


@router.put("/registrations/{registration_id}/payment")
async def update_payment(
    registration_id: int,
    body: PaymentUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """更新付款狀態"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise _not_found("報名資料")

        total_amount = _calc_total_amount(session, registration_id)
        operator = current_user.get("username", "")

        if body.is_paid:
            if not reg.is_paid:
                shortfall = total_amount - (reg.paid_amount or 0)
                if shortfall > 0:
                    rec = ActivityPaymentRecord(
                        registration_id=registration_id,
                        type="payment",
                        amount=shortfall,
                        payment_date=datetime.now().date(),
                        payment_method="現金",
                        notes="（批次標記已繳費自動補齊）",
                        operator=operator,
                    )
                    session.add(rec)
                    reg.paid_amount = total_amount
                reg.is_paid = True
        else:
            session.query(ActivityPaymentRecord).filter(
                ActivityPaymentRecord.registration_id == registration_id
            ).delete()
            reg.paid_amount = 0
            reg.is_paid = False

        status_str = "已繳費" if body.is_paid else "未繳費"
        activity_service.log_change(
            session, registration_id, reg.student_name,
            "更新付款狀態", f"付款狀態更新為：{status_str}",
            operator,
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {"message": f"更新成功，狀態為：{status_str}"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
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
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise _not_found("報名資料")

        reg.remark = body.remark
        activity_service.log_change(
            session, registration_id, reg.student_name,
            "更新備註", f"備註更新為：{body.remark}",
            current_user.get("username", ""),
        )
        session.commit()
        return {"message": "備註更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/registrations/{registration_id}/payments")
async def get_registration_payments(
    registration_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得報名的繳費／退費明細記錄"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise _not_found("報名資料")

        records = (
            session.query(ActivityPaymentRecord)
            .filter(ActivityPaymentRecord.registration_id == registration_id)
            .order_by(ActivityPaymentRecord.created_at.asc())
            .all()
        )
        total_amount = _calc_total_amount(session, registration_id)
        paid_amount = reg.paid_amount or 0
        return {
            "total_amount": total_amount,
            "paid_amount": paid_amount,
            "payment_status": _derive_payment_status(paid_amount, total_amount),
            "records": [
                {
                    "id": r.id,
                    "type": r.type,
                    "amount": r.amount,
                    "payment_date": r.payment_date.isoformat() if r.payment_date else None,
                    "payment_method": r.payment_method or "",
                    "notes": r.notes or "",
                    "operator": r.operator or "",
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ],
        }
    finally:
        session.close()


@router.post("/registrations/{registration_id}/payments", status_code=201)
async def add_registration_payment(
    registration_id: int,
    body: AddPaymentRequest,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """新增繳費或退費記錄"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise _not_found("報名資料")

        operator = current_user.get("username", "")
        rec = ActivityPaymentRecord(
            registration_id=registration_id,
            type=body.type,
            amount=body.amount,
            payment_date=body.payment_date,
            payment_method=body.payment_method,
            notes=body.notes,
            operator=operator,
        )
        session.add(rec)

        if body.type == "payment":
            reg.paid_amount = (reg.paid_amount or 0) + body.amount
        else:
            reg.paid_amount = max(0, (reg.paid_amount or 0) - body.amount)

        total_amount = _calc_total_amount(session, registration_id)
        reg.is_paid = reg.paid_amount >= total_amount > 0

        type_label = "繳費" if body.type == "payment" else "退費"
        activity_service.log_change(
            session, registration_id, reg.student_name,
            f"新增{type_label}記錄",
            f"{type_label} NT${body.amount}，繳費方式：{body.payment_method}",
            operator,
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {
            "message": f"{type_label}記錄新增成功",
            "paid_amount": reg.paid_amount,
            "payment_status": _derive_payment_status(reg.paid_amount, total_amount),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/registrations/{registration_id}/payments/{payment_id}")
async def delete_registration_payment(
    registration_id: int,
    payment_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """刪除繳費記錄（更正用），自動重新計算已繳金額"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise _not_found("報名資料")

        payment = session.query(ActivityPaymentRecord).filter(
            ActivityPaymentRecord.id == payment_id,
            ActivityPaymentRecord.registration_id == registration_id,
        ).first()
        if not payment:
            raise _not_found("繳費記錄")

        session.delete(payment)
        session.flush()

        totals = (
            session.query(ActivityPaymentRecord.type, func.sum(ActivityPaymentRecord.amount))
            .filter(ActivityPaymentRecord.registration_id == registration_id)
            .group_by(ActivityPaymentRecord.type)
            .all()
        )
        amount_map = {t: s for t, s in totals}
        new_paid = (amount_map.get("payment") or 0) - (amount_map.get("refund") or 0)
        reg.paid_amount = max(0, new_paid)

        total_amount = _calc_total_amount(session, registration_id)
        reg.is_paid = reg.paid_amount >= total_amount > 0

        activity_service.log_change(
            session, registration_id, reg.student_name,
            "刪除繳費記錄", f"刪除記錄 id={payment_id}，重新計算已繳 NT${reg.paid_amount}",
            current_user.get("username", ""),
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session, summary_only=True)
        return {
            "message": "記錄已刪除",
            "paid_amount": reg.paid_amount,
            "payment_status": _derive_payment_status(reg.paid_amount, total_amount),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/registrations/{registration_id}/waitlist")
async def promote_waitlist(
    registration_id: int,
    background_tasks: BackgroundTasks,
    course_id: int = Query(...),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """將候補升為正式報名"""
    session = get_session()
    try:
        student_name, course_name = activity_service.promote_waitlist(session, registration_id, course_id)

        activity_service.log_change(
            session, registration_id, student_name,
            "候補升正式", f"課程「{course_name}」候補升為正式",
            current_user.get("username", ""),
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        line_svc = get_line_service()
        if line_svc is not None:
            background_tasks.add_task(
                line_svc.notify_activity_waitlist_promoted, student_name, course_name
            )
        return {"message": "成功升為正式報名"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/registrations/{registration_id}/courses/{course_id}")
async def withdraw_course(
    registration_id: int,
    course_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """退出單一課程（含候補），若為正式報名則自動升位候補"""
    session = get_session()
    try:
        reg = session.query(ActivityRegistration).filter(
            ActivityRegistration.id == registration_id,
            ActivityRegistration.is_active.is_(True),
        ).first()
        if not reg:
            raise _not_found("報名資料")

        from models.database import RegistrationCourse as RC
        rc = session.query(RC).filter(
            RC.registration_id == registration_id,
            RC.course_id == course_id,
        ).first()
        if not rc:
            raise _not_found("課程報名項目")

        from models.database import ActivityCourse as AC
        course = session.query(AC).filter(AC.id == course_id).first()
        course_name = course.name if course else str(course_id)
        was_enrolled = rc.status == "enrolled"

        session.delete(rc)
        session.flush()

        if was_enrolled:
            activity_service._auto_promote_first_waitlist(session, course_id)

        new_total = _calc_total_amount(session, registration_id)
        paid_amount = reg.paid_amount or 0
        reg.is_paid = paid_amount >= new_total > 0

        activity_service.log_change(
            session, registration_id, reg.student_name,
            "退課", f"退出課程「{course_name}」",
            current_user.get("username", ""),
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        return {
            "message": f"已退出課程「{course_name}」",
            "total_amount": new_total,
            "payment_status": _derive_payment_status(paid_amount, new_total),
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/registrations/{registration_id}")
async def delete_registration(
    registration_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """軟刪除報名"""
    session = get_session()
    try:
        activity_service.delete_registration(
            session, registration_id,
            current_user.get("username", ""),
        )
        session.commit()
        _invalidate_activity_dashboard_caches(session)
        logger.warning(
            "課後才藝報名已刪除：id=%s operator=%s",
            registration_id, current_user.get("username"),
        )
        return {"message": "報名已刪除"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()

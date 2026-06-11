"""
Portal - overtime management endpoints
"""

import logging
import calendar as cal_module
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from utils.errors import raise_safe_500

from models.approval import ApprovalStatus
from models.database import get_session, OvertimeRecord, User
from utils.auth import get_current_user
from utils.approval_helpers import _get_finalized_salary_record
from utils.permissions import Permission, list_active_user_ids_with_permission
from ._shared import _get_employee, OvertimeCreatePortal, OVERTIME_TYPE_LABELS

logger = logging.getLogger(__name__)

router = APIRouter()


def _list_active_users_with_permission(session, perm: str) -> list[int]:
    """SQLite/PG 通用：列出 permission_names 含 perm 的 active user_id。

    對齊 api/permissions_admin.py:136-145 與 api/portal/leaves.py 同名 helper。
    """
    return list_active_user_ids_with_permission(session, perm)


@router.get("/my-overtimes")
def get_my_overtimes(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    """取得個人加班記錄"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        records = (
            session.query(OvertimeRecord)
            .filter(
                OvertimeRecord.employee_id == emp.id,
                OvertimeRecord.overtime_date >= start,
                OvertimeRecord.overtime_date <= end,
            )
            .order_by(OvertimeRecord.overtime_date.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        return [
            {
                "id": ot.id,
                "overtime_date": ot.overtime_date.isoformat(),
                "overtime_type": ot.overtime_type,
                "overtime_type_label": OVERTIME_TYPE_LABELS.get(
                    ot.overtime_type, ot.overtime_type
                ),
                "start_time": (
                    ot.start_time.strftime("%H:%M") if ot.start_time else None
                ),
                "end_time": ot.end_time.strftime("%H:%M") if ot.end_time else None,
                "hours": ot.hours,
                "overtime_pay": ot.overtime_pay,
                "use_comp_leave": ot.use_comp_leave,
                "comp_leave_granted": ot.comp_leave_granted,
                "reason": ot.reason,
                "status": ot.status,
                "approved_by": ot.approved_by,
                "created_at": ot.created_at.isoformat() if ot.created_at else None,
            }
            for ot in records
        ]
    finally:
        session.close()


@router.post("/my-overtimes", status_code=201)
def create_my_overtime(
    data: OvertimeCreatePortal,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """提交加班申請"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        if data.overtime_type not in OVERTIME_TYPE_LABELS:
            raise HTTPException(
                status_code=400, detail=f"無效的加班類型: {data.overtime_type}"
            )

        # F1 第二/三波：calculate_overtime_pay + 4 衝突檢查 helper 全部抽到 services；
        # 不再 lazy import admin router 私有 helper。
        from services.overtime_pay_calculator import calculate_overtime_pay
        from services.overtime_conflict_service import (
            check_employee_has_conflicting_leave as _check_employee_has_conflicting_leave,
            check_overtime_overlap as _check_overtime_overlap,
            check_monthly_overtime_cap as _check_monthly_overtime_cap,
            check_quarterly_overtime_cap as _check_quarterly_overtime_cap,
            check_overtime_type_calendar as _check_overtime_type_calendar,
        )

        pay = (
            0.0
            if data.use_comp_leave
            else calculate_overtime_pay(emp.base_salary, data.hours, data.overtime_type)
        )

        start_dt = None
        end_dt = None
        if data.start_time:
            h, m = map(int, data.start_time.split(":"))
            start_dt = datetime.combine(
                data.overtime_date, datetime.min.time().replace(hour=h, minute=m)
            )
        if data.end_time:
            h, m = map(int, data.end_time.split(":"))
            end_dt = datetime.combine(
                data.overtime_date, datetime.min.time().replace(hour=h, minute=m)
            )

        overlap = _check_overtime_overlap(
            session, emp.id, data.overtime_date, start_dt, end_dt
        )
        if overlap:
            st = (
                overlap.start_time.strftime("%H:%M") if overlap.start_time else "未指定"
            )
            et = overlap.end_time.strftime("%H:%M") if overlap.end_time else "未指定"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"您在 {overlap.overtime_date} 已有時間重疊的加班申請"
                    f"（ID: {overlap.id}，{st}～{et}），請勿重複送出"
                ),
            )

        # 修補 2026-05-11 P1-5：跨類重疊（同日請假 vs 加班）
        _check_employee_has_conflicting_leave(
            session, emp.id, data.overtime_date, start_dt, end_dt
        )

        # 與管理端 create_overtime 一致：46h/月上限 + 138h/季上限 + 國定假日類型驗證。
        # 否則教師可從 portal 繞過上限,或在國定假日用 weekday/weekend 短付加班費。
        _check_monthly_overtime_cap(session, emp.id, data.overtime_date, data.hours)
        _check_quarterly_overtime_cap(session, emp.id, data.overtime_date, data.hours)
        _check_overtime_type_calendar(session, data.overtime_date, data.overtime_type)

        ot = OvertimeRecord(
            employee_id=emp.id,
            overtime_date=data.overtime_date,
            overtime_type=data.overtime_type,
            start_time=start_dt,
            end_time=end_dt,
            hours=data.hours,
            overtime_pay=pay,
            use_comp_leave=data.use_comp_leave,
            reason=data.reason,
            status=ApprovalStatus.PENDING.value,
        )
        session.add(ot)
        session.flush()

        ot_type_label = OVERTIME_TYPE_LABELS.get(data.overtime_type, data.overtime_type)

        # 通知：commit 前 enqueue；dispatch after_commit hook 對每位 OVERTIME_WRITE
        # reviewer 個人推送（in_app + LINE）。原 _push 群組廣播改為 per-reviewer，
        # 行為變更與 portal/leaves.py 對齊。
        try:
            from services.notification import dispatch

            reviewer_user_ids = _list_active_users_with_permission(
                session, Permission.OVERTIME_WRITE.value
            )
            for rid in reviewer_user_ids:
                dispatch.enqueue(
                    session=session,
                    event_type="overtime.submitted",
                    recipient_user_id=rid,
                    context={
                        "submitter_name": emp.name,
                        "ot_date": data.overtime_date.isoformat(),
                        "ot_type": ot_type_label,
                        "overtime_id": ot.id,
                    },
                    sender_id=current_user.get("user_id"),
                    source_entity_type="overtime_record",
                    source_entity_id=ot.id,
                )
        except Exception as exc:
            logger.warning("overtime.submitted enqueue 失敗（已吞）：%s", exc)

        session.commit()

        request.state.audit_entity_id = str(ot.id)
        request.state.audit_summary = (
            f"教師送出加班申請：{emp.name} {ot_type_label} "
            f"{data.overtime_date}（{data.hours}h，"
            f"{'補休' if data.use_comp_leave else '加班費'}）"
        )
        request.state.audit_changes = {
            "action": "portal_create_overtime",
            "employee_id": emp.id,
            "employee_name": emp.name,
            "overtime_id": ot.id,
            "overtime_date": data.overtime_date.isoformat(),
            "overtime_type": data.overtime_type,
            "overtime_type_label": ot_type_label,
            "start_time": data.start_time,
            "end_time": data.end_time,
            "hours": data.hours,
            "use_comp_leave": data.use_comp_leave,
            "overtime_pay": pay,
        }

        msg = "加班申請已送出，待主管核准"
        if data.use_comp_leave:
            msg = (
                f"補休申請已送出（{data.hours}h），核准後計入當年度補休配額，待主管核准"
            )
        return {
            "message": msg,
            "id": ot.id,
            "overtime_pay": pay,
            "use_comp_leave": data.use_comp_leave,
        }
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/my-overtimes/{overtime_id}", status_code=200)
def delete_my_overtime(
    overtime_id: int,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """撤回待審中的加班申請（已核准或已駁回者不可撤回）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        ot = (
            session.query(OvertimeRecord)
            .filter(
                OvertimeRecord.id == overtime_id,
                OvertimeRecord.employee_id == emp.id,
            )
            .first()
        )
        if not ot:
            raise HTTPException(status_code=404, detail="找不到加班記錄")
        if ot.status != ApprovalStatus.PENDING.value:
            _status_msg = "已核准" if ot.status == ApprovalStatus.APPROVED.value else "已駁回"
            raise HTTPException(status_code=400, detail=f"此申請已{_status_msg}，無法撤回")
        # NV5：薪資已封存時不可撤回（避免薪資記錄與加班記錄不一致）
        year = ot.overtime_date.year
        month = ot.overtime_date.month
        finalized = _get_finalized_salary_record(session, emp.id, year, month)
        if finalized:
            by = finalized.finalized_by or "系統"
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{year} 年 {month} 月薪資已封存（結算人：{by}），"
                    "無法撤回加班申請。請先至薪資管理頁面解除封存後再操作。"
                ),
            )
        logger.warning(
            "加班申請撤回：operator=%s employee_id=%d overtime_id=%d overtime_date=%s",
            current_user.get("username"),
            emp.id,
            overtime_id,
            ot.overtime_date,
        )

        ot_type_label = OVERTIME_TYPE_LABELS.get(ot.overtime_type, ot.overtime_type)
        request.state.audit_entity_id = str(overtime_id)
        request.state.audit_summary = (
            f"教師撤回加班申請：{emp.name} {ot_type_label} "
            f"{ot.overtime_date}（{ot.hours}h）"
        )
        request.state.audit_changes = {
            "action": "portal_withdraw_overtime",
            "employee_id": emp.id,
            "overtime_id": overtime_id,
            "overtime_date": ot.overtime_date.isoformat(),
            "overtime_type": ot.overtime_type,
            "hours": ot.hours,
            "use_comp_leave": ot.use_comp_leave,
            "overtime_pay": ot.overtime_pay,
        }

        session.delete(ot)
        session.commit()
        return {"message": "加班申請已撤回"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()

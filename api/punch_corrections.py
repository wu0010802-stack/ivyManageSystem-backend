"""
Punch correction management router（管理員審核補打卡申請）
"""

import logging
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel
from models.database import (
    get_session,
    Employee,
    Attendance,
    PunchCorrectionRequest,
    User,
)
from models.approval import ApprovalStatus
from schemas._common import DeleteResultOut
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.approval_helpers import (
    _check_approval_eligibility,
    _get_submitter_role,
    _write_approval_log,
)
from services.salary.finalize_guard import (
    assert_months_not_finalized,
    collect_months_from_dates,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["punch-corrections"])


CORRECTION_TYPE_LABELS = {
    "punch_in": "補上班打卡",
    "punch_out": "補下班打卡",
    "both": "補全天打卡",
}


class ApproveRequest(BaseModel):
    approved: bool
    rejection_reason: Optional[str] = None


def _format_correction(c: PunchCorrectionRequest, employee_name: str = "") -> dict:
    return {
        "id": c.id,
        "employee_id": c.employee_id,
        "employee_name": employee_name,
        "attendance_date": c.attendance_date.isoformat(),
        "correction_type": c.correction_type,
        "correction_type_label": CORRECTION_TYPE_LABELS.get(
            c.correction_type, c.correction_type
        ),
        "requested_punch_in": (
            c.requested_punch_in.isoformat() if c.requested_punch_in else None
        ),
        "requested_punch_out": (
            c.requested_punch_out.isoformat() if c.requested_punch_out else None
        ),
        "reason": c.reason,
        "approval_status": c.approval_status,
        "approved_by": c.approved_by,
        "rejection_reason": c.rejection_reason,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/punch-corrections")
def list_punch_corrections(
    status: Optional[str] = Query(None, description="pending / approved / rejected"),
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    employee_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_staff_permission(Permission.APPROVALS)),
):
    """查詢補打卡申請（管理員用）"""
    session = get_session()
    try:
        query = session.query(PunchCorrectionRequest, Employee).join(
            Employee, PunchCorrectionRequest.employee_id == Employee.id
        )

        if status == "pending":
            query = query.filter(
                PunchCorrectionRequest.status == ApprovalStatus.PENDING.value
            )
        elif status == "approved":
            query = query.filter(
                PunchCorrectionRequest.status == ApprovalStatus.APPROVED.value
            )
        elif status == "rejected":
            query = query.filter(
                PunchCorrectionRequest.status == ApprovalStatus.REJECTED.value
            )

        if year and month:
            import calendar as cal_module

            _, last_day = cal_module.monthrange(year, month)
            start = date(year, month, 1)
            end = date(year, month, last_day)
            query = query.filter(
                PunchCorrectionRequest.attendance_date >= start,
                PunchCorrectionRequest.attendance_date <= end,
            )

        if employee_id:
            query = query.filter(PunchCorrectionRequest.employee_id == employee_id)

        rows = (
            query.order_by(PunchCorrectionRequest.created_at.desc()).limit(5000).all()
        )
        return [_format_correction(c, emp.name) for c, emp in rows]
    finally:
        session.close()


@router.put(
    "/punch-corrections/{correction_id}/approve", response_model=DeleteResultOut
)
def approve_punch_correction(
    correction_id: int,
    body: ApproveRequest,
    current_user: dict = Depends(require_staff_permission(Permission.APPROVALS)),
):
    """核准或駁回補打卡申請"""
    session = get_session()
    try:
        # 列鎖 serialize 兩位 approver 並發審核同一筆。bug sweep round 5
        # (2026-05-14) P1：line 218 advisory salary lock 是月度級（同員工同月
        # serialize），無法防止「兩位 approver 同時核准 / 一核准一駁回」造成
        # 雙寫 ApprovalLog / 雙推 LINE / attendance 雙寫的問題。與 leaves.py:1190
        # / overtimes approve（已加 with_for_update）對齊。
        correction = (
            session.query(PunchCorrectionRequest)
            .filter(PunchCorrectionRequest.id == correction_id)
            .with_for_update()
            .first()
        )
        if not correction:
            raise HTTPException(status_code=404, detail="找不到此補打卡申請")

        if correction.status != ApprovalStatus.PENDING.value:
            status_label = (
                "已核准"
                if correction.status == ApprovalStatus.APPROVED.value
                else "已駁回"
            )
            raise HTTPException(
                status_code=400, detail=f"此申請已{status_label}，無法再次審核"
            )

        # ── 自我核准防護（F-015）────────────────────────────────────────────
        # 僅在 approver 確實擁有 employee_id 且與申請人相同時才拒絕。
        # 無 employee_id 的帳號（如純管理員）本身無法提出補打卡申請，
        # 不構成自我核准風險。對齊 leaves.py:1014 / overtimes.py:1078 idiom。
        approver_eid = current_user.get("employee_id")
        if approver_eid and correction.employee_id == approver_eid:
            raise HTTPException(status_code=403, detail="不可自我核准補打卡申請")

        # ── 角色資格檢查 ──────────────────────────────────────────────────────
        submitter_role = _get_submitter_role(correction.employee_id, session)
        approver_role = current_user.get("role", "")
        if not _check_approval_eligibility(
            "punch_correction", submitter_role, approver_role, session
        ):
            raise HTTPException(
                status_code=403,
                detail=f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的補打卡申請",
            )

        if not body.approved:
            # 駁回
            if not body.rejection_reason or not body.rejection_reason.strip():
                raise HTTPException(status_code=422, detail="駁回時必須填寫駁回原因")
            correction.status = ApprovalStatus.REJECTED.value
            correction.rejection_reason = body.rejection_reason.strip()
            correction.approved_by = current_user.get("username", "")
            _write_approval_log(
                session=session,
                doc_type="punch_correction",
                doc_id=correction_id,
                action="rejected",
                approver=current_user,
                comment=body.rejection_reason,
            )

            # 個人推播（審核結果）— commit 前 enqueue 才會在 after_commit hook
            # 被 fan-out；PR-A 原版誤放 commit 後，session.close 時 _clear_on_rollback
            # 把 queue 抹掉，推播永遠不會送出（PR-D bug fix）。
            from services.notification import dispatch

            _owner_user = (
                session.query(User)
                .filter(User.employee_id == correction.employee_id)
                .first()
            )
            if _owner_user is not None:
                dispatch.enqueue(
                    session=session,
                    event_type="punch_correction.rejected",
                    recipient_user_id=_owner_user.id,
                    context={
                        "reviewer_name": current_user.get("name")
                        or current_user.get("username", ""),
                        "target_date": (
                            correction.attendance_date.isoformat()
                            if hasattr(correction.attendance_date, "isoformat")
                            else str(correction.attendance_date)
                        ),
                        "correction_id": correction.id,
                        "rejection_reason": correction.rejection_reason,
                    },
                    sender_id=current_user.get("user_id"),
                    source_entity_type="punch_correction",
                    source_entity_id=correction.id,
                )
            session.commit()
            logger.warning(
                "補打卡申請 #%d（員工 %d，日期 %s）已由 %s 駁回",
                correction_id,
                correction.employee_id,
                correction.attendance_date,
                current_user.get("username"),
            )
            return {"message": "補打卡申請已駁回"}

        # 提早取得薪資鎖,讓「封存守衛 → 改 attendance → mark_stale → commit」
        # 在同一鎖窗內完成,避免 finalize 在 commit 與 mark_stale 之間搶先封存。
        from utils.advisory_lock import acquire_salary_lock as _acquire_salary_lock

        _acquire_salary_lock(
            session,
            employee_id=correction.employee_id,
            year=correction.attendance_date.year,
            month=correction.attendance_date.month,
        )

        # 核准前檢查該月薪資是否已封存（避免改動已結算月份的考勤來源資料）
        assert_months_not_finalized(
            session,
            employee_id=correction.employee_id,
            months=collect_months_from_dates([correction.attendance_date]),
        )

        # 核准：取得或建立 Attendance 記錄
        att = (
            session.query(Attendance)
            .filter(
                Attendance.employee_id == correction.employee_id,
                Attendance.attendance_date == correction.attendance_date,
            )
            .first()
        )
        if not att:
            att = Attendance(
                employee_id=correction.employee_id,
                attendance_date=correction.attendance_date,
            )
            session.add(att)

        if correction.correction_type in ("punch_in", "both"):
            att.punch_in_time = correction.requested_punch_in
        if correction.correction_type in ("punch_out", "both"):
            att.punch_out_time = correction.requested_punch_out

        # 跨夜班修正：下班時間早於上班時間表示隔日下班（如 22:00→04:00）。
        # 前端 PortalPunchCorrectionForm 用 `${attendance_date}T${time}:00` 把兩個
        # datetime 都鎖在同日，跨夜情境會讓 recompute_attendance_status 算出
        # work_hours 變負 / early_leave_minutes 約 14h / 加班費歸零 → 扣全薪。
        # 與 api/attendance/records.py:266-273 + api/attendance/upload.py:340-356
        # 既有 normalize pattern 對齊；helper docstring（utils/attendance_calc.py:54）
        # 明列 caller 負責跨夜修正，本路徑原為唯一漏寫的 caller。
        if (
            att.punch_in_time
            and att.punch_out_time
            and att.punch_out_time < att.punch_in_time
        ):
            att.punch_out_time += timedelta(days=1)
        elif (
            att.punch_in_time
            and att.punch_out_time
            and att.punch_out_time == att.punch_in_time
        ):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"時間錯誤：上下班時間相同 "
                    f"{att.punch_in_time.isoformat()}，請確認資料"
                ),
            )

        # 依新 punch 時間整套重算 is_late/is_early_leave/late_minutes/
        # early_leave_minutes/status/is_missing_*。否則舊的遲到/早退欄位殘留會被
        # 薪資 engine（services/salary/engine.py:2099+）讀到，造成補卡通過卻仍
        # 扣遲到金的真實漏帳（audit 2026-05-07 P0 #6）。
        from utils.attendance_calc import apply_attendance_status

        # 取員工排班時間（caller 已驗 employee 存在於 correction）
        emp = (
            session.query(Employee)
            .filter(Employee.id == correction.employee_id)
            .first()
        )
        apply_attendance_status(
            att,
            work_start_str=emp.work_start_time if emp else None,
            work_end_str=emp.work_end_time if emp else None,
            session=session,
        )

        # 更新申請狀態
        correction.status = ApprovalStatus.APPROVED.value
        correction.approved_by = current_user.get("username", "")
        _write_approval_log(
            session=session,
            doc_type="punch_correction",
            doc_id=correction_id,
            action="approved",
            approver=current_user,
        )

        # 補打卡修改 punch_in/out 與缺卡旗標 → 影響遲到/早退/缺打卡扣款。
        # 若該月薪資已計算但尚未封存（finalize 那關才會擋封存），需標 stale 讓
        # finalize 完整性檢查擋下；避免會計在薪資計算後核准補卡造成考勤源與
        # 薪資結果分叉，最後仍可能 finalize 通過。
        from services.salary.utils import mark_salary_stale

        mark_salary_stale(
            session,
            correction.employee_id,
            correction.attendance_date.year,
            correction.attendance_date.month,
        )

        # 個人推播（審核結果）— commit 前 enqueue（PR-D bug fix；同 reject path）
        from services.notification import dispatch

        _owner_user = (
            session.query(User)
            .filter(User.employee_id == correction.employee_id)
            .first()
        )
        if _owner_user is not None:
            dispatch.enqueue(
                session=session,
                event_type="punch_correction.approved",
                recipient_user_id=_owner_user.id,
                context={
                    "reviewer_name": current_user.get("name")
                    or current_user.get("username", ""),
                    "target_date": (
                        correction.attendance_date.isoformat()
                        if hasattr(correction.attendance_date, "isoformat")
                        else str(correction.attendance_date)
                    ),
                    "correction_id": correction.id,
                },
                sender_id=current_user.get("user_id"),
                source_entity_type="punch_correction",
                source_entity_id=correction.id,
            )
        session.commit()

        logger.warning(
            "補打卡申請 #%d（員工 %d，日期 %s）已由 %s 核准",
            correction_id,
            correction.employee_id,
            correction.attendance_date,
            current_user.get("username"),
        )
        return {"message": "補打卡申請已核准，考勤記錄已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        logger.error("核准補打卡申請 #%d 時發生錯誤：%s", correction_id, e)
        raise_safe_500(e)
    finally:
        session.close()

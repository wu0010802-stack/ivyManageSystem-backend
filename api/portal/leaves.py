"""
Portal - leave management endpoints
"""

import calendar as cal_module
import json
import logging
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from utils.errors import raise_safe_500
from utils.rate_limit import SlidingWindowLimiter

# 附件上傳：每 IP 每 10 分鐘最多 30 次（5個檔 × 6次 = 30次）
_attach_upload_limiter = SlidingWindowLimiter(
    max_calls=30,
    window_seconds=600,
    name="leave_attachment_upload",
    error_detail="附件上傳過於頻繁，請稍後再試",
)
from fastapi.responses import FileResponse
from sqlalchemy import func, case


from models.database import (
    get_session, LeaveRecord, LeaveQuota,
    ShiftAssignment, ShiftType, DailyShift, Holiday,
    AttendancePolicy, Employee,
)
from utils.auth import get_current_user
from utils.error_messages import LEAVE_RECORD_NOT_FOUND
from ._shared import (
    _get_employee, _calculate_annual_leave_quota,
    LeaveCreatePortal, LEAVE_TYPE_LABELS, SubstituteRespond,
)
from api.leaves import _check_overlap, _check_substitute_leave_conflict
from utils.file_upload import validate_file_signature
from api.leaves_workday import _calc_shift_hours, validate_leave_hours_against_schedule, _build_workday_hours_payload
from api.leaves_quota import (
    _check_quota, _check_leave_limits, QUOTA_LEAVE_TYPES,
    STATUTORY_QUOTA_HOURS, LEAVE_DEDUCTION_RULES,
)
from services.leave_policy import validate_portal_leave_rules

router = APIRouter()

logger = logging.getLogger(__name__)

_line_service = None


def init_leave_notify(line_service):
    global _line_service
    _line_service = line_service

# ── 職務代理人工具函式 ──────────────────────────────────────────────────────

def _validate_substitute(session, emp_id: int, substitute_id: int) -> "Employee":
    """驗證代理人合法性：不能指定自己、員工必須存在且在職"""
    if emp_id == substitute_id:
        raise HTTPException(status_code=400, detail="代理人不能是自己")
    sub_emp = session.query(Employee).filter(
        Employee.id == substitute_id,
        Employee.is_active == True,
    ).first()
    if not sub_emp:
        raise HTTPException(status_code=404, detail="代理人員工不存在或已離職")
    return sub_emp


# 使用 __file__ 建立絕對路徑，避免 CWD 變動導致檔案寫入位置不正確
_UPLOAD_BASE = Path(__file__).resolve().parent.parent.parent / "uploads" / "leave_attachments"
_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".pdf"}
_MAX_FILE_SIZE = 5 * 1024 * 1024   # 5 MB
_MAX_FILES = 5
# 副檔名只允許英數字，防止特殊字元（null byte、路徑符號等）進入檔案系統
_EXT_RE = re.compile(r'^\.[a-z0-9]+$')


def _parse_paths(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


def _safe_attach_path(leave_id: int, filename: str) -> Path:
    """解析附件路徑並確認落在 _UPLOAD_BASE 之內（路徑穿越防護）。"""
    resolved = (_UPLOAD_BASE / str(leave_id) / filename).resolve()
    try:
        resolved.relative_to(_UPLOAD_BASE.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="無效的附件路徑")
    return resolved


@router.get("/my-leaves")
def get_my_leaves(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    """取得個人請假記錄"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.start_date <= end,
            LeaveRecord.end_date >= start,
        ).order_by(LeaveRecord.start_date.desc()).offset(skip).limit(limit).all()

        return [{
            "id": lv.id,
            "leave_type": lv.leave_type,
            "leave_type_label": LEAVE_TYPE_LABELS.get(lv.leave_type, lv.leave_type),
            "start_date": lv.start_date.isoformat(),
            "end_date": lv.end_date.isoformat(),
            "start_time": lv.start_time,
            "end_time": lv.end_time,
            "leave_hours": lv.leave_hours,
            "reason": lv.reason,
            "is_approved": lv.is_approved,
            "approved_by": lv.approved_by,
            "rejection_reason": lv.rejection_reason,
            "attachment_paths": _parse_paths(lv.attachment_paths),
            "substitute_employee_id": lv.substitute_employee_id,
            "substitute_status": lv.substitute_status or "not_required",
            "substitute_remark": lv.substitute_remark,
            "source_overtime_id": lv.source_overtime_id,
            "created_at": lv.created_at.isoformat() if lv.created_at else None,
        } for lv in leaves]
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="取得假單失敗")
    finally:
        session.close()


@router.post("/my-leaves", status_code=201)
def create_my_leave(
    data: LeaveCreatePortal,
    current_user: dict = Depends(get_current_user),
):
    """提交請假申請"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        if data.leave_type not in LEAVE_TYPE_LABELS:
            raise HTTPException(status_code=400, detail=f"無效的假別: {data.leave_type}")
        if data.end_date < data.start_date:
            raise HTTPException(status_code=400, detail="結束日期不可早於開始日期")
        if data.leave_hours < 0.5:
            raise HTTPException(status_code=400, detail="請假時數至少 0.5 小時")
        if round(data.leave_hours * 2) != data.leave_hours * 2:
            raise HTTPException(status_code=400, detail="請假時數必須為 0.5 小時的倍數（如 0.5、1、1.5、2…）")

        # 代理人衝突檢查提前：代理人有假單衝突是影響第三方的硬性條件，
        # 需優先於請假規則（如事假提前 2 日）等 400 錯誤回傳給用戶。
        substitute_status = "not_required"
        if data.substitute_employee_id is not None:
            _validate_substitute(session, emp.id, data.substitute_employee_id)
            _check_substitute_leave_conflict(
                session,
                data.substitute_employee_id,
                data.start_date,
                data.end_date,
                data.start_time,
                data.end_time,
            )
            substitute_status = "pending"

        try:
            validate_portal_leave_rules(
                data.leave_type,
                data.start_date,
                data.end_date,
                data.leave_hours,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # 重疊偵測（含時段精確比對，僅封鎖已核准的假單，待審核可並存）
        overlap = _check_overlap(
            session, emp.id, data.start_date, data.end_date,
            data.start_time, data.end_time,
        )
        if overlap:
            raise HTTPException(
                status_code=409,
                detail=f"您在 {overlap.start_date} ~ {overlap.end_date} 已有已核准的請假記錄，無法重複請假",
            )

        validate_leave_hours_against_schedule(
            session,
            emp.id,
            data.start_date,
            data.end_date,
            data.leave_hours,
            data.start_time,
            data.end_time,
        )

        # 配額檢查（已核准 + 待審合計不得超出年度上限，防止併發刷假）
        _check_leave_limits(
            session, emp.id, data.leave_type,
            data.start_date, data.leave_hours,
        )
        _check_quota(
            session, emp.id, data.leave_type,
            data.start_date.year, data.leave_hours,
        )

        effective_ratio = LEAVE_DEDUCTION_RULES[data.leave_type]
        leave = LeaveRecord(
            employee_id=emp.id,
            leave_type=data.leave_type,
            start_date=data.start_date,
            end_date=data.end_date,
            start_time=data.start_time,
            end_time=data.end_time,
            leave_hours=data.leave_hours,
            is_deductible=effective_ratio > 0,
            deduction_ratio=effective_ratio,
            reason=data.reason,
            is_approved=None,
            substitute_employee_id=data.substitute_employee_id,
            substitute_status=substitute_status,
            source_overtime_id=data.source_overtime_id if data.leave_type == "compensatory" else None,
        )
        session.add(leave)
        session.commit()

        # LINE 通知（fire-and-forget，失敗不影響申請）
        if _line_service:
            try:
                _line_service.notify_leave_submitted(
                    emp.name, data.leave_type,
                    data.start_date, data.end_date, data.leave_hours,
                )
            except Exception as e:
                logger.warning("LINE 通知發送失敗: %s", e)

        msg = "請假申請已送出，待主管核准"
        if substitute_status == "pending":
            msg = "請假申請已送出，請等待代理人接受後主管才能核准"
        return {"message": msg, "id": leave.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/my-leaves/{leave_id}/attachments")
async def upload_leave_attachments(
    leave_id: int,
    files: List[UploadFile] = File(...),
    current_user: dict = Depends(get_current_user),
    _rl: None = Depends(_attach_upload_limiter.as_dependency()),
):
    """上傳假單附件（如診斷證明、喜帖）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        leave = session.query(LeaveRecord).filter(
            LeaveRecord.id == leave_id,
            LeaveRecord.employee_id == emp.id,
        ).first()
        if not leave:
            raise HTTPException(status_code=404, detail="找不到請假記錄")

        existing = _parse_paths(leave.attachment_paths)
        if len(existing) + len(files) > _MAX_FILES:
            raise HTTPException(status_code=400, detail=f"附件總數不可超過 {_MAX_FILES} 個")

        dir_path = _UPLOAD_BASE / str(leave_id)
        dir_path.mkdir(parents=True, exist_ok=True)

        saved = []
        for f in files:
            raw_ext = Path(f.filename or "").suffix.lower()
            if not raw_ext or not _EXT_RE.match(raw_ext) or raw_ext not in _ALLOWED_EXT:
                raise HTTPException(status_code=400, detail=f"不支援的檔案格式：{raw_ext or '(無副檔名)'}，僅接受圖片與 PDF")

            content = await f.read()
            if len(content) > _MAX_FILE_SIZE:
                raise HTTPException(status_code=400, detail=f"檔案 {f.filename} 超過 5 MB 限制")
            validate_file_signature(content, raw_ext)

            safe_name = f"{uuid.uuid4().hex}{raw_ext}"
            with open(dir_path / safe_name, "wb") as fp:
                fp.write(content)
            saved.append(safe_name)

        all_paths = existing + saved
        leave.attachment_paths = json.dumps(all_paths)
        session.commit()
        return {"message": f"已上傳 {len(saved)} 個附件", "attachments": all_paths}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/my-leaves/{leave_id}/attachments/{filename}")
def delete_leave_attachment(
    leave_id: int,
    filename: str,
    current_user: dict = Depends(get_current_user),
):
    """刪除個人假單附件"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        leave = session.query(LeaveRecord).filter(
            LeaveRecord.id == leave_id,
            LeaveRecord.employee_id == emp.id,
        ).first()
        if not leave:
            raise HTTPException(status_code=404, detail="找不到請假記錄")
        if leave.is_approved is not None:
            raise HTTPException(status_code=400, detail="已審核的假單不可刪除附件")

        paths = _parse_paths(leave.attachment_paths)
        if filename not in paths:
            raise HTTPException(status_code=404, detail="找不到附件")

        file_path = _safe_attach_path(leave_id, filename)
        if file_path.exists():
            file_path.unlink()

        paths.remove(filename)
        leave.attachment_paths = json.dumps(paths) if paths else None
        session.commit()
        return {"message": "附件已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/my-leaves/{leave_id}/attachments/{filename}")
def get_leave_attachment(
    leave_id: int,
    filename: str,
    current_user: dict = Depends(get_current_user),
):
    """取得個人假單附件（僅限本人）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        leave = session.query(LeaveRecord).filter(
            LeaveRecord.id == leave_id,
            LeaveRecord.employee_id == emp.id,
        ).first()
        if not leave:
            raise HTTPException(status_code=404, detail="找不到請假記錄")

        paths = _parse_paths(leave.attachment_paths)
        if filename not in paths:
            raise HTTPException(status_code=404, detail="找不到附件")

        file_path = _safe_attach_path(leave_id, filename)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="檔案不存在")

        return FileResponse(str(file_path))
    finally:
        session.close()


@router.get("/my-leave-stats")
def get_my_leave_stats(
    current_user: dict = Depends(get_current_user),
):
    """取得個人特休統計 (年資、特休天數、已休天數)"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        hire_date = emp.hire_date
        seniority_years = 0
        seniority_months = 0
        annual_leave_quota = 0

        if hire_date:
            today = date.today()
            months_diff = (today.year - hire_date.year) * 12 + today.month - hire_date.month
            if today.day < hire_date.day:
                months_diff -= 1

            seniority_years = months_diff // 12
            seniority_months = months_diff % 12
            annual_leave_quota = _calculate_annual_leave_quota(hire_date)

        current_year = date.today().year
        start_of_year = date(current_year, 1, 1)
        end_of_year = date(current_year, 12, 31)

        used_hours = session.query(
            func.coalesce(func.sum(LeaveRecord.leave_hours), 0)
        ).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.leave_type == "annual",
            LeaveRecord.start_date >= start_of_year,
            LeaveRecord.start_date <= end_of_year,
            LeaveRecord.is_approved == True,
        ).scalar()

        used_days = float(used_hours or 0) / 8.0

        return {
            "hire_date": hire_date.isoformat() if hire_date else None,
            "seniority_years": seniority_years,
            "seniority_months": seniority_months,
            "annual_leave_quota": annual_leave_quota,
            "annual_leave_used_days": round(used_days, 1),
            "start_of_calculation": start_of_year.isoformat(),
            "end_of_calculation": end_of_year.isoformat()
        }
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────
# 工作日時數計算（整合排班與假日，供前端申請表使用）
# ─────────────────────────────────────────────────────────────

@router.get("/my-workday-hours")
def get_my_workday_hours(
    start_date: date,
    end_date: date,
    current_user: dict = Depends(get_current_user),
):
    """計算本人在指定區間的每日工時明細（整合排班 + 國定假日）"""
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="結束日期不得早於開始日期")
    if (end_date - start_date).days > 90:
        raise HTTPException(status_code=400, detail="查詢區間不得超過 90 天")

    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        employee_id = emp.id

        return _build_workday_hours_payload(session, employee_id, start_date, end_date)
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────
# 個人配額查詢
# ─────────────────────────────────────────────────────────────

@router.get("/my-quotas")
def get_my_quotas(
    year: int = None,
    current_user: dict = Depends(get_current_user),
):
    """查詢本人各假別年度配額（含動態計算的已使用、待審、剩餘時數）"""
    if year is None:
        year = date.today().year
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        quotas = session.query(LeaveQuota).filter(
            LeaveQuota.employee_id == emp.id,
            LeaveQuota.year == year,
        ).all()

        year_start = date(year, 1, 1)
        year_end = date(year + 1, 1, 1)

        # 單次查詢同時取得已核准與待審核時數（CASE WHEN 合併，減少一次 DB 往返）
        combined_rows = session.query(
            LeaveRecord.leave_type,
            func.coalesce(
                func.sum(case((LeaveRecord.is_approved.is_(True), LeaveRecord.leave_hours), else_=0)), 0
            ).label("used_hours"),
            func.coalesce(
                func.sum(case((LeaveRecord.is_approved.is_(None), LeaveRecord.leave_hours), else_=0)), 0
            ).label("pending_hours"),
        ).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.start_date >= year_start,
            LeaveRecord.start_date < year_end,
        ).group_by(LeaveRecord.leave_type).all()
        used_map = {row.leave_type: float(row.used_hours) for row in combined_rows}
        pending_map = {row.leave_type: float(row.pending_hours) for row in combined_rows}

        result = []
        for q in quotas:
            u = used_map.get(q.leave_type, 0.0)
            p = pending_map.get(q.leave_type, 0.0)
            result.append({
                "leave_type": q.leave_type,
                "leave_type_label": LEAVE_TYPE_LABELS.get(q.leave_type, q.leave_type),
                "total_hours": q.total_hours,
                "used_hours": u,
                "pending_hours": p,
                "remaining_hours": max(0.0, q.total_hours - u - p),
                "note": q.note,
            })
        return result
    finally:
        session.close()


# ─────────────────────────────────────────────────────────────
# 職務代理人：回應 & 查詢
# ─────────────────────────────────────────────────────────────

@router.post("/my-leaves/{leave_id}/substitute-respond")
def substitute_respond(
    leave_id: int,
    data: SubstituteRespond,
    current_user: dict = Depends(get_current_user),
):
    """代理人接受或拒絕代理請求（僅被指定人可操作）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        leave = session.query(LeaveRecord).filter(
            LeaveRecord.id == leave_id,
            LeaveRecord.substitute_employee_id == emp.id,
        ).first()
        if not leave:
            raise HTTPException(status_code=404, detail=LEAVE_RECORD_NOT_FOUND)
        if leave.substitute_status != "pending":
            raise HTTPException(status_code=409, detail="此代理請求已回應過，無法重複操作")

        leave.substitute_status = "accepted" if data.action == "accept" else "rejected"
        leave.substitute_responded_at = datetime.now()
        leave.substitute_remark = data.remark
        session.commit()

        action_label = "接受" if data.action == "accept" else "拒絕"
        return {"message": f"已{action_label}代理請求"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/my-substitute-requests")
def get_my_substitute_requests(
    status: Optional[str] = Query(None, description="過濾狀態：pending/accepted/rejected"),
    current_user: dict = Depends(get_current_user),
):
    """查詢被指定為代理人的假單列表"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        q = session.query(LeaveRecord, Employee).join(
            Employee, LeaveRecord.employee_id == Employee.id
        ).filter(
            LeaveRecord.substitute_employee_id == emp.id,
            LeaveRecord.substitute_status != "waived",
        )

        if status in ("pending", "accepted", "rejected"):
            q = q.filter(LeaveRecord.substitute_status == status)

        records = q.order_by(LeaveRecord.created_at.desc()).all()

        return [{
            "id": lv.id,
            "leave_type": lv.leave_type,
            "leave_type_label": LEAVE_TYPE_LABELS.get(lv.leave_type, lv.leave_type),
            "requester_name": requester.name,
            "requester_employee_id": requester.employee_id,
            "start_date": lv.start_date.isoformat(),
            "end_date": lv.end_date.isoformat(),
            "leave_hours": lv.leave_hours,
            "reason": lv.reason,
            "substitute_status": lv.substitute_status or "pending",
            "substitute_responded_at": lv.substitute_responded_at.isoformat() if lv.substitute_responded_at else None,
            "is_approved": lv.is_approved,
            "created_at": lv.created_at.isoformat() if lv.created_at else None,
        } for lv, requester in records]
    finally:
        session.close()


@router.get("/substitute-pending-count")
def get_substitute_pending_count(
    current_user: dict = Depends(get_current_user),
):
    """取得待回應代理請求數量（用於 badge）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        count = session.query(LeaveRecord).filter(
            LeaveRecord.substitute_employee_id == emp.id,
            LeaveRecord.substitute_status == "pending",
        ).count()
        return {"pending_count": count}
    finally:
        session.close()

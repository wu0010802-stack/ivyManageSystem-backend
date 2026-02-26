"""
Portal - leave management endpoints
"""

import calendar as cal_module
import json
import os
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from models.database import (
    get_session, LeaveRecord, LeaveQuota,
    ShiftAssignment, ShiftType, DailyShift, Holiday,
    AttendancePolicy,
)
from utils.auth import get_current_user
from ._shared import (
    _get_employee, _calculate_annual_leave_quota,
    LeaveCreatePortal, LEAVE_TYPE_LABELS,
)
from api.leaves import _check_overlap, _check_quota, _check_leave_limits

# ── 重用 leaves.py 的配額常數 ──
QUOTA_LEAVE_TYPES = {"annual", "sick", "menstrual", "personal", "family_care"}
STATUTORY_QUOTA_HOURS = {"sick": 240.0, "menstrual": 96.0, "personal": 112.0, "family_care": 56.0}

router = APIRouter()

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
    year: int = Query(...),
    month: int = Query(...),
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
        ).order_by(LeaveRecord.start_date.desc()).all()

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
            "created_at": lv.created_at.isoformat() if lv.created_at else None,
        } for lv in leaves]
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

        # 配額檢查（已核准 + 待審合計不得超出年度上限，防止併發刷假）
        _check_leave_limits(
            session, emp.id, data.leave_type,
            data.start_date, data.leave_hours,
        )
        _check_quota(
            session, emp.id, data.leave_type,
            data.start_date.year, data.leave_hours,
        )

        leave = LeaveRecord(
            employee_id=emp.id,
            leave_type=data.leave_type,
            start_date=data.start_date,
            end_date=data.end_date,
            start_time=data.start_time,
            end_time=data.end_time,
            leave_hours=data.leave_hours,
            reason=data.reason,
            is_approved=None,
        )
        session.add(leave)
        session.commit()
        return {"message": "請假申請已送出，待主管核准", "id": leave.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/my-leaves/{leave_id}/attachments")
async def upload_leave_attachments(
    leave_id: int,
    files: List[UploadFile] = File(...),
    current_user: dict = Depends(get_current_user),
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
        raise HTTPException(status_code=500, detail=str(e))
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
        raise HTTPException(status_code=500, detail=str(e))
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

        used_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.employee_id == emp.id,
            LeaveRecord.leave_type == "annual",
            LeaveRecord.start_date >= start_of_year,
            LeaveRecord.start_date <= end_of_year,
            LeaveRecord.is_approved == True,
        ).all()

        used_days = sum(lv.leave_hours for lv in used_leaves) / 8.0

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

def _calc_shift_hours(work_start: str, work_end: str) -> float:
    sh, sm = map(int, work_start.split(":"))
    eh, em = map(int, work_end.split(":"))
    total_minutes = (eh * 60 + em) - (sh * 60 + sm)
    if total_minutes <= 0:
        total_minutes += 24 * 60
    total_hours = total_minutes / 60
    if total_hours > 5:
        total_hours -= 1
    return round(total_hours * 2) / 2


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

        holidays = {
            h.date: h.name
            for h in session.query(Holiday).filter(
                Holiday.date >= start_date,
                Holiday.date <= end_date,
                Holiday.is_active.is_(True),
            ).all()
        }
        daily_shifts = {
            ds.date: ds.shift_type
            for ds in session.query(DailyShift)
            .filter(DailyShift.employee_id == employee_id, DailyShift.date >= start_date, DailyShift.date <= end_date)
            .options(joinedload(DailyShift.shift_type)).all()
        }
        monday_start = start_date - timedelta(days=start_date.weekday())
        monday_end = end_date - timedelta(days=end_date.weekday())
        weekly_shifts = {
            a.week_start_date: a.shift_type
            for a in session.query(ShiftAssignment)
            .filter(ShiftAssignment.employee_id == employee_id,
                    ShiftAssignment.week_start_date >= monday_start,
                    ShiftAssignment.week_start_date <= monday_end)
            .options(joinedload(ShiftAssignment.shift_type)).all()
        }

        # 系統預設上下班時間（當員工無排班時使用）
        policy = session.query(AttendancePolicy).first()
        default_ws = policy.default_work_start if policy and policy.default_work_start else "08:00"
        default_we = policy.default_work_end if policy and policy.default_work_end else "17:00"

        breakdown, total_hours, cur = [], 0.0, start_date
        while cur <= end_date:
            wd = cur.weekday()
            if wd >= 5:
                breakdown.append({"date": cur.isoformat(), "weekday": wd, "type": "weekend", "hours": 0, "shift": None, "work_start": None, "work_end": None, "holiday_name": None})
            elif cur in holidays:
                breakdown.append({"date": cur.isoformat(), "weekday": wd, "type": "holiday", "hours": 0, "shift": None, "work_start": None, "work_end": None, "holiday_name": holidays[cur]})
            else:
                st = daily_shifts.get(cur) or weekly_shifts.get(cur - timedelta(days=wd))
                if st:
                    hours = _calc_shift_hours(st.work_start, st.work_end)
                    breakdown.append({"date": cur.isoformat(), "weekday": wd, "type": "workday", "hours": hours, "shift": st.name, "work_start": st.work_start, "work_end": st.work_end, "holiday_name": None})
                else:
                    hours = _calc_shift_hours(default_ws, default_we)
                    breakdown.append({"date": cur.isoformat(), "weekday": wd, "type": "workday", "hours": hours, "shift": None, "work_start": default_ws, "work_end": default_we, "holiday_name": None})
                total_hours += hours
            cur += timedelta(days=1)

        return {"total_hours": round(total_hours * 2) / 2, "breakdown": breakdown}
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

        def used_hours(lt):
            r = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
                LeaveRecord.employee_id == emp.id,
                LeaveRecord.leave_type == lt,
                LeaveRecord.is_approved == True,
                func.extract('year', LeaveRecord.start_date) == year,
            ).scalar()
            return float(r)

        def pending_hours(lt):
            r = session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0)).filter(
                LeaveRecord.employee_id == emp.id,
                LeaveRecord.leave_type == lt,
                LeaveRecord.is_approved.is_(None),
                func.extract('year', LeaveRecord.start_date) == year,
            ).scalar()
            return float(r)

        result = []
        for q in quotas:
            u = used_hours(q.leave_type)
            p = pending_hours(q.leave_type)
            result.append({
                "leave_type": q.leave_type,
                "leave_type_label": LEAVE_TYPE_LABELS.get(q.leave_type, q.leave_type),
                "total_hours": q.total_hours,
                "used_hours": u,
                "pending_hours": p,
                "remaining_hours": max(0.0, q.total_hours - u),
                "note": q.note,
            })
        return result
    finally:
        session.close()

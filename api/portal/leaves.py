"""
Portal - leave management endpoints
"""

import calendar as cal_module
import json
import os
import uuid
from datetime import date
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from models.database import get_session, LeaveRecord
from utils.auth import get_current_user
from ._shared import (
    _get_employee, _calculate_annual_leave_quota,
    LeaveCreatePortal, LEAVE_TYPE_LABELS,
)

router = APIRouter()

_UPLOAD_DIR = "uploads/leave_attachments"
_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".pdf"}
_MAX_FILE_SIZE = 5 * 1024 * 1024   # 5 MB
_MAX_FILES = 5


def _parse_paths(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


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
            "leave_hours": lv.leave_hours,
            "reason": lv.reason,
            "is_approved": lv.is_approved,
            "approved_by": lv.approved_by,
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

        leave = LeaveRecord(
            employee_id=emp.id,
            leave_type=data.leave_type,
            start_date=data.start_date,
            end_date=data.end_date,
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

        dir_path = os.path.join(_UPLOAD_DIR, str(leave_id))
        os.makedirs(dir_path, exist_ok=True)

        saved = []
        for f in files:
            ext = os.path.splitext(f.filename or "")[1].lower()
            if ext not in _ALLOWED_EXT:
                raise HTTPException(status_code=400, detail=f"不支援的檔案格式：{ext or '(無副檔名)'}，僅接受圖片與 PDF")

            content = await f.read()
            if len(content) > _MAX_FILE_SIZE:
                raise HTTPException(status_code=400, detail=f"檔案 {f.filename} 超過 5 MB 限制")

            safe_name = f"{uuid.uuid4().hex}{ext}"
            with open(os.path.join(dir_path, safe_name), "wb") as fp:
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

        file_path = os.path.join(_UPLOAD_DIR, str(leave_id), filename)
        if os.path.exists(file_path):
            os.remove(file_path)

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

        file_path = os.path.join(_UPLOAD_DIR, str(leave_id), filename)
        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="檔案不存在")

        return FileResponse(file_path)
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

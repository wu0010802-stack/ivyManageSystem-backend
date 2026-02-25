"""
排班管理 API
- 班別模板 CRUD
- 每週排班指派
"""

import logging
from datetime import date, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from sqlalchemy.orm import joinedload

from models.database import get_session, ShiftType, ShiftAssignment, Employee, DailyShift, ShiftSwapRequest
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/shifts", tags=["shifts"])


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class ShiftTypeCreate(BaseModel):
    name: str
    work_start: str
    work_end: str
    sort_order: int = 0

class ShiftTypeUpdate(BaseModel):
    name: Optional[str] = None
    work_start: Optional[str] = None
    work_end: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None

class AssignmentItem(BaseModel):
    employee_id: int
    shift_type_id: Optional[int] = None
    notes: Optional[str] = None

class BulkAssignmentRequest(BaseModel):
    week_start_date: str  # YYYY-MM-DD (must be a Monday)
    assignments: List[AssignmentItem]


class DailyShiftCreate(BaseModel):
    """每日排班（調班）請求"""
    employee_id: int
    shift_type_id: int
    date: str  # YYYY-MM-DD
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# 班別模板 CRUD
# ---------------------------------------------------------------------------

@router.get("/types")
def list_shift_types(current_user: dict = Depends(require_permission(Permission.SCHEDULE))):
    session = get_session()
    try:
        types = session.query(ShiftType).order_by(ShiftType.sort_order).all()
        return [
            {
                "id": t.id,
                "name": t.name,
                "work_start": t.work_start,
                "work_end": t.work_end,
                "sort_order": t.sort_order,
                "is_active": t.is_active,
            }
            for t in types
        ]
    finally:
        session.close()


@router.post("/types", status_code=201)
def create_shift_type(data: ShiftTypeCreate, current_user: dict = Depends(require_permission(Permission.SCHEDULE))):
    session = get_session()
    try:
        st = ShiftType(
            name=data.name,
            work_start=data.work_start,
            work_end=data.work_end,
            sort_order=data.sort_order,
        )
        session.add(st)
        session.commit()
        session.refresh(st)
        logger.info(f"Created shift type: {st.name}")
        return {"id": st.id, "name": st.name, "work_start": st.work_start, "work_end": st.work_end, "sort_order": st.sort_order, "is_active": st.is_active}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        session.close()


@router.put("/types/{type_id}")
def update_shift_type(type_id: int, data: ShiftTypeUpdate, current_user: dict = Depends(require_permission(Permission.SCHEDULE))):
    session = get_session()
    try:
        st = session.query(ShiftType).get(type_id)
        if not st:
            raise HTTPException(status_code=404, detail="班別不存在")
        for field, value in data.dict(exclude_unset=True).items():
            setattr(st, field, value)
        session.commit()
        logger.info(f"Updated shift type: {st.name}")
        return {"id": st.id, "name": st.name, "work_start": st.work_start, "work_end": st.work_end, "sort_order": st.sort_order, "is_active": st.is_active}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        session.close()


@router.delete("/types/{type_id}")
def delete_shift_type(type_id: int, current_user: dict = Depends(require_permission(Permission.SCHEDULE))):
    session = get_session()
    try:
        st = session.query(ShiftType).get(type_id)
        if not st:
            raise HTTPException(status_code=404, detail="班別不存在")
        # Check if any assignments reference this type
        count = session.query(ShiftAssignment).filter(ShiftAssignment.shift_type_id == type_id).count()
        if count > 0:
            raise HTTPException(status_code=400, detail=f"此班別已被 {count} 筆排班使用，無法刪除")
        session.delete(st)
        session.commit()
        logger.info(f"Deleted shift type: {st.name}")
        return {"message": "已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 每週排班
# ---------------------------------------------------------------------------

@router.get("/assignments")
def get_assignments(week_start: str, current_user: dict = Depends(require_permission(Permission.SCHEDULE))):
    """查詢某週排班。week_start 為該週週一日期 (YYYY-MM-DD)"""
    session = get_session()
    try:
        week_date = date.fromisoformat(week_start)
        # Align to Monday
        week_date = week_date - timedelta(days=week_date.weekday())

        assignments = (
            session.query(ShiftAssignment)
            .options(joinedload(ShiftAssignment.employee), joinedload(ShiftAssignment.shift_type))
            .filter(ShiftAssignment.week_start_date == week_date)
            .all()
        )
        result = []
        for a in assignments:
            emp = a.employee
            st = a.shift_type
            result.append({
                "id": a.id,
                "employee_id": a.employee_id,
                "employee_name": emp.name if emp else "",
                "shift_type_id": a.shift_type_id,
                "shift_type_name": st.name if st else "",
                "work_start": st.work_start if st else "",
                "work_end": st.work_end if st else "",
                "week_start_date": str(a.week_start_date),
                "notes": a.notes or "",
            })
        return result
    finally:
        session.close()


def _apply_employee_assignment_action(session, existing, item, week_date: str) -> str:
    """針對單一員工執行排班 upsert 或刪除。

    僅影響該員工自身的記錄，不動其他員工的資料。

    Returns: 'inserted' | 'updated' | 'deleted' | 'skipped'
    """
    if item.shift_type_id is None:
        if existing:
            session.delete(existing)
            return "deleted"
        return "skipped"

    if existing:
        existing.shift_type_id = item.shift_type_id
        existing.notes = item.notes
        return "updated"

    session.add(ShiftAssignment(
        employee_id=item.employee_id,
        shift_type_id=item.shift_type_id,
        week_start_date=week_date,
        notes=item.notes,
    ))
    return "inserted"


@router.post("/assignments", status_code=201)
def save_assignments(data: BulkAssignmentRequest, current_user: dict = Depends(require_permission(Permission.SCHEDULE))):
    """批次儲存某週排班（per-employee upsert，不影響清單外的員工）"""
    session = get_session()
    try:
        week_date = date.fromisoformat(data.week_start_date)
        # Align to Monday
        week_date = week_date - timedelta(days=week_date.weekday())

        saved = deleted = 0
        for item in data.assignments:
            existing = (
                session.query(ShiftAssignment)
                .filter(
                    ShiftAssignment.employee_id == item.employee_id,
                    ShiftAssignment.week_start_date == week_date,
                )
                .first()
            )
            action = _apply_employee_assignment_action(session, existing, item, str(week_date))
            if action in ("inserted", "updated"):
                saved += 1
            elif action == "deleted":
                deleted += 1

        session.commit()
        logger.info(f"Saved {saved} / deleted {deleted} shift assignments for week {week_date}")
        return {"message": f"已儲存 {saved} 筆、清除 {deleted} 筆排班", "week_start_date": str(week_date)}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 每日排班（調班/換班）
# ---------------------------------------------------------------------------

@router.get("/daily")
def get_daily_shifts(
    start_date: str,
    end_date: str,
    employee_id: Optional[int] = None,
    current_user: dict = Depends(require_permission(Permission.SCHEDULE)),
):
    """查詢日期範圍內的排班調動/每日排班"""
    session = get_session()
    try:
        s_date = date.fromisoformat(start_date)
        e_date = date.fromisoformat(end_date)
        
        query = session.query(DailyShift).filter(
            DailyShift.date >= s_date,
            DailyShift.date <= e_date
        )
        
        if employee_id:
            query = query.filter(DailyShift.employee_id == employee_id)
            
        daily_shifts = query.order_by(DailyShift.date).all()
        
        result = []
        for ds in daily_shifts:
            emp = session.query(Employee).get(ds.employee_id)
            st = session.query(ShiftType).get(ds.shift_type_id)
            result.append({
                "id": ds.id,
                "employee_id": ds.employee_id,
                "employee_name": emp.name if emp else "",
                "shift_type_id": ds.shift_type_id,
                "shift_type_name": st.name if st else "",
                "work_start": st.work_start if st else "",
                "work_end": st.work_end if st else "",
                "date": str(ds.date),
                "notes": ds.notes or ""
            })
        return result
    finally:
        session.close()


@router.post("/daily", status_code=201)
def upsert_daily_shift(data: DailyShiftCreate, current_user: dict = Depends(require_permission(Permission.SCHEDULE))):
    """新增或更新每日排班（支援 UPSERT）"""
    session = get_session()
    try:
        target_date = date.fromisoformat(data.date)
        
        # 檢查是否已存在
        existing = session.query(DailyShift).filter(
            DailyShift.employee_id == data.employee_id,
            DailyShift.date == target_date
        ).first()
        
        if existing:
            existing.shift_type_id = data.shift_type_id
            existing.notes = data.notes
            msg = "Updated daily shift"
        else:
            new_shift = DailyShift(
                employee_id=data.employee_id,
                shift_type_id=data.shift_type_id,
                date=target_date,
                notes=data.notes
            )
            session.add(new_shift)
            msg = "Created daily shift"
            
        session.commit()
        logger.info(f"{msg}: {data.employee_id} on {target_date}")
        return {"message": "已儲存"}
        
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        session.close()


@router.delete("/daily/{shift_id}")
def delete_daily_shift(shift_id: int, current_user: dict = Depends(require_permission(Permission.SCHEDULE))):
    """刪除每日排班（恢復為週排班或預設）"""
    session = get_session()
    try:
        ds = session.query(DailyShift).get(shift_id)
        if not ds:
            raise HTTPException(status_code=404, detail="找不到該排班記錄")
            
        session.delete(ds)
        session.commit()
        return {"message": "已刪除"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 換班歷史（管理端）
# ---------------------------------------------------------------------------

@router.get("/swap-history")
def get_swap_history(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(require_permission(Permission.SCHEDULE)),
):
    """查看換班歷史（管理端）"""
    session = get_session()
    try:
        query = session.query(ShiftSwapRequest).order_by(ShiftSwapRequest.created_at.desc())

        if start_date:
            query = query.filter(ShiftSwapRequest.swap_date >= date.fromisoformat(start_date))
        if end_date:
            query = query.filter(ShiftSwapRequest.swap_date <= date.fromisoformat(end_date))
        if status:
            query = query.filter(ShiftSwapRequest.status == status)

        swaps = query.limit(100).all()

        # Pre-fetch employees and shift types
        emp_ids = set()
        for s in swaps:
            emp_ids.add(s.requester_id)
            emp_ids.add(s.target_id)
        emps = {e.id: e.name for e in session.query(Employee).filter(Employee.id.in_(emp_ids)).all()} if emp_ids else {}
        sts = {st.id: st.name for st in session.query(ShiftType).all()}

        return [{
            "id": s.id,
            "requester_name": emps.get(s.requester_id, ""),
            "target_name": emps.get(s.target_id, ""),
            "swap_date": s.swap_date.isoformat(),
            "requester_shift": sts.get(s.requester_shift_type_id, "未排班"),
            "target_shift": sts.get(s.target_shift_type_id, "未排班"),
            "reason": s.reason,
            "status": s.status,
            "target_remark": s.target_remark,
            "target_responded_at": s.target_responded_at.isoformat() if s.target_responded_at else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        } for s in swaps]
    finally:
        session.close()

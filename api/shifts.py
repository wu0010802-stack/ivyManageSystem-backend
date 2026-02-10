"""
排班管理 API
- 班別模板 CRUD
- 每週排班指派
"""

import logging
from datetime import date, timedelta
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from models.database import get_session, ShiftType, ShiftAssignment, Employee

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


# ---------------------------------------------------------------------------
# 班別模板 CRUD
# ---------------------------------------------------------------------------

@router.get("/types")
def list_shift_types():
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


@router.post("/types")
def create_shift_type(data: ShiftTypeCreate):
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
def update_shift_type(type_id: int, data: ShiftTypeUpdate):
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
def delete_shift_type(type_id: int):
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
def get_assignments(week_start: str):
    """查詢某週排班。week_start 為該週週一日期 (YYYY-MM-DD)"""
    session = get_session()
    try:
        week_date = date.fromisoformat(week_start)
        # Align to Monday
        week_date = week_date - timedelta(days=week_date.weekday())

        assignments = (
            session.query(ShiftAssignment)
            .filter(ShiftAssignment.week_start_date == week_date)
            .all()
        )
        result = []
        for a in assignments:
            emp = session.query(Employee).get(a.employee_id)
            st = session.query(ShiftType).get(a.shift_type_id)
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


@router.post("/assignments")
def save_assignments(data: BulkAssignmentRequest):
    """批次儲存某週排班（覆蓋該週所有排班）"""
    session = get_session()
    try:
        week_date = date.fromisoformat(data.week_start_date)
        # Align to Monday
        week_date = week_date - timedelta(days=week_date.weekday())

        # Delete existing assignments for this week
        session.query(ShiftAssignment).filter(
            ShiftAssignment.week_start_date == week_date
        ).delete()

        # Insert new assignments (skip entries without shift_type_id)
        count = 0
        for item in data.assignments:
            if item.shift_type_id is None:
                continue
            assignment = ShiftAssignment(
                employee_id=item.employee_id,
                shift_type_id=item.shift_type_id,
                week_start_date=week_date,
                notes=item.notes,
            )
            session.add(assignment)
            count += 1

        session.commit()
        logger.info(f"Saved {count} shift assignments for week {week_date}")
        return {"message": f"已儲存 {count} 筆排班", "week_start_date": str(week_date)}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        session.close()

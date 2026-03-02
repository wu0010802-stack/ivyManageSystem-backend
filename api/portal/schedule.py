"""
Portal - schedule and shift swap endpoints
"""

import calendar as cal_module
import logging
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_

from models.database import (
    get_session, Employee, Classroom, ShiftAssignment,
    DailyShift, ShiftSwapRequest,
)
from utils.auth import get_current_user
from ._shared import (
    _get_employee, _get_employee_shift_for_date, _get_shift_type_map,
    SwapRequestCreate, SwapRequestRespond, WEEKDAY_NAMES,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _assert_swap_snapshot_fresh(session, swap) -> None:
    """TOCTOU 保護：在接受換班前，確認雙方班別快照未被主管修改。

    create_swap_request 建立申請時，會快照雙方當下的班別 ID。
    若主管在「申請後、對方同意前」修改了任一方的班表，
    快照就已過期；直接執行換班會悄悄還原主管的修改。

    此函式重新讀取 DB 當前班別，與快照比對：
    - 相符 → 正常返回（可安全執行換班）
    - 不符 → 拋出 409，通知對方重新申請

    Args:
        session: SQLAlchemy session
        swap:    ShiftSwapRequest 物件（帶有快照 requester/target_shift_type_id）

    Raises:
        HTTPException 409：快照過期
    """
    current_req = _get_employee_shift_for_date(session, swap.requester_id, swap.swap_date)
    current_tgt = _get_employee_shift_for_date(session, swap.target_id, swap.swap_date)

    if current_req != swap.requester_shift_type_id or current_tgt != swap.target_shift_type_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "班表已被主管修改，換班申請的班別快照已過期，此申請已自動取消。"
                "請重新查看最新班表後，重新發起換班申請。"
            ),
        )


@router.get("/my-schedule")
def get_my_schedule(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """取得自己當月排班"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        shift_types = _get_shift_type_map(session, active_only=True)

        daily_shifts = session.query(DailyShift).filter(
            DailyShift.employee_id == emp.id,
            DailyShift.date >= start,
            DailyShift.date <= end,
        ).all()
        # 用 dict 存 shift_type_id（可能為 None，代表換班後該日明確排休）
        daily_map = {ds.date: ds.shift_type_id for ds in daily_shifts}
        # 用 set 記錄「有 DailyShift 記錄」的日期，不論 shift_type_id 是否為 None
        daily_override_dates = {ds.date for ds in daily_shifts}

        first_monday = start - timedelta(days=start.weekday())
        last_monday = end - timedelta(days=end.weekday())
        assignments = session.query(ShiftAssignment).filter(
            ShiftAssignment.employee_id == emp.id,
            ShiftAssignment.week_start_date >= first_monday,
            ShiftAssignment.week_start_date <= last_monday,
        ).all()
        weekly_map = {a.week_start_date: a.shift_type_id for a in assignments}

        days = []
        for day_num in range(1, last_day + 1):
            d = date(year, month, day_num)
            weekday = d.weekday()
            is_weekend = weekday >= 5

            # 以 record 存在性（非 shift_type_id 非空）判斷是否為覆蓋日
            # DailyShift(shift_type_id=None) = 換班後明確排休，仍算 override
            is_override = d in daily_override_dates
            if is_override:
                shift_type_id = daily_map[d]  # 取出（可能為 None）
            else:
                week_monday = d - timedelta(days=weekday)
                shift_type_id = weekly_map.get(week_monday)

            st = shift_types.get(shift_type_id) if shift_type_id else None
            days.append({
                "date": d.isoformat(),
                "day": day_num,
                "weekday": WEEKDAY_NAMES[weekday],
                "is_weekend": is_weekend,
                "shift_type_id": shift_type_id,
                "shift_name": st.name if st else None,
                "work_start": st.work_start if st else None,
                "work_end": st.work_end if st else None,
                "is_override": is_override,
            })

        return {
            "employee_name": emp.name,
            "year": year,
            "month": month,
            "days": days,
        }
    finally:
        session.close()


@router.get("/swap-candidates")
def get_swap_candidates(
    swap_date: str = Query(..., alias="date"),
    current_user: dict = Depends(get_current_user),
):
    """取得指定日期其他老師及其班別"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        target_date = date.fromisoformat(swap_date)

        shift_types = _get_shift_type_map(session)

        classrooms = session.query(Classroom).filter(Classroom.is_active == True).all()
        teacher_ids = set()
        for c in classrooms:
            if c.head_teacher_id:
                teacher_ids.add(c.head_teacher_id)
            if c.assistant_teacher_id:
                teacher_ids.add(c.assistant_teacher_id)
        teacher_ids.discard(emp.id)

        if not teacher_ids:
            return []

        teachers = session.query(Employee).filter(
            Employee.id.in_(teacher_ids), Employee.is_active == True
        ).all()
        teacher_map = {t.id: t for t in teachers}
        active_ids = set(teacher_map.keys())

        daily_shifts = session.query(DailyShift).filter(
            DailyShift.employee_id.in_(active_ids),
            DailyShift.date == target_date,
        ).all()
        # 以 set 記錄「有 DailyShift 記錄」的員工 id，不論 shift_type_id 是否為 None
        daily_override_ids = {ds.employee_id for ds in daily_shifts}
        daily_shift_map = {ds.employee_id: ds.shift_type_id for ds in daily_shifts}

        week_monday = target_date - timedelta(days=target_date.weekday())
        weekly_assigns = session.query(ShiftAssignment).filter(
            ShiftAssignment.employee_id.in_(active_ids),
            ShiftAssignment.week_start_date == week_monday,
        ).all()
        weekly_map = {sa.employee_id: sa.shift_type_id for sa in weekly_assigns}

        pending_swaps = session.query(ShiftSwapRequest).filter(
            ShiftSwapRequest.swap_date == target_date,
            ShiftSwapRequest.status == "pending",
            or_(
                ShiftSwapRequest.requester_id.in_(active_ids),
                ShiftSwapRequest.target_id.in_(active_ids),
            ),
        ).all()
        pending_ids = set()
        for ps in pending_swaps:
            pending_ids.add(ps.requester_id)
            pending_ids.add(ps.target_id)

        candidates = []
        for tid in active_ids:
            teacher = teacher_map[tid]
            # 以 record 存在性判斷：DailyShift 存在則優先（含 shift_type_id=None 的明確排休）
            if tid in daily_override_ids:
                shift_type_id = daily_shift_map[tid]
            else:
                shift_type_id = weekly_map.get(tid)
            st = shift_types.get(shift_type_id) if shift_type_id else None

            candidates.append({
                "employee_id": tid,
                "name": teacher.name,
                "shift_type_id": shift_type_id,
                "shift_name": st.name if st else "未排班",
                "work_start": st.work_start if st else None,
                "work_end": st.work_end if st else None,
                "has_pending_swap": tid in pending_ids,
            })

        return candidates
    finally:
        session.close()


@router.get("/swap-requests")
def get_swap_requests(
    current_user: dict = Depends(get_current_user),
):
    """查詢自己的換班申請（發起+收到）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        requests = session.query(ShiftSwapRequest).filter(
            (ShiftSwapRequest.requester_id == emp.id) | (ShiftSwapRequest.target_id == emp.id),
        ).order_by(ShiftSwapRequest.created_at.desc()).limit(50).all()

        shift_types = _get_shift_type_map(session)

        emp_ids = set()
        for r in requests:
            emp_ids.add(r.requester_id)
            emp_ids.add(r.target_id)
        emp_rows = session.query(Employee.id, Employee.name).filter(Employee.id.in_(emp_ids)).all() if emp_ids else []
        emp_map = {e.id: e.name for e in emp_rows}

        result = []
        for r in requests:
            req_st = shift_types.get(r.requester_shift_type_id)
            tgt_st = shift_types.get(r.target_shift_type_id)

            result.append({
                "id": r.id,
                "requester_id": r.requester_id,
                "requester_name": emp_map.get(r.requester_id, ""),
                "target_id": r.target_id,
                "target_name": emp_map.get(r.target_id, ""),
                "swap_date": r.swap_date.isoformat(),
                "requester_shift": req_st.name if req_st else "未排班",
                "target_shift": tgt_st.name if tgt_st else "未排班",
                "reason": r.reason,
                "status": r.status,
                "target_remark": r.target_remark,
                "target_responded_at": r.target_responded_at.isoformat() if r.target_responded_at else None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "is_mine": r.requester_id == emp.id,
            })

        return result
    finally:
        session.close()


@router.post("/swap-requests", status_code=201)
def create_swap_request(
    data: SwapRequestCreate,
    current_user: dict = Depends(get_current_user),
):
    """發起換班申請"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        if data.target_id == emp.id:
            raise HTTPException(status_code=400, detail="不可與自己換班")
        if data.swap_date < date.today():
            raise HTTPException(status_code=400, detail="不可換過去的日期")

        target = session.query(Employee).filter(Employee.id == data.target_id, Employee.is_active == True).first()
        if not target:
            raise HTTPException(status_code=404, detail="找不到換班對象")

        existing = session.query(ShiftSwapRequest).filter(
            ShiftSwapRequest.requester_id == emp.id,
            ShiftSwapRequest.swap_date == data.swap_date,
            ShiftSwapRequest.status == "pending",
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="您在此日期已有一筆待處理的換班申請")

        target_pending = session.query(ShiftSwapRequest).filter(
            ShiftSwapRequest.swap_date == data.swap_date,
            ShiftSwapRequest.status == "pending",
            (ShiftSwapRequest.requester_id == data.target_id) | (ShiftSwapRequest.target_id == data.target_id),
        ).first()
        if target_pending:
            raise HTTPException(status_code=400, detail="對方在此日期已有待處理的換班申請")

        req_shift_id = _get_employee_shift_for_date(session, emp.id, data.swap_date)
        tgt_shift_id = _get_employee_shift_for_date(session, data.target_id, data.swap_date)

        swap = ShiftSwapRequest(
            requester_id=emp.id,
            target_id=data.target_id,
            swap_date=data.swap_date,
            requester_shift_type_id=req_shift_id,
            target_shift_type_id=tgt_shift_id,
            reason=data.reason,
            status="pending",
        )
        session.add(swap)
        session.commit()
        return {"message": "換班申請已送出", "id": swap.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/swap-requests/{request_id}/respond")
def respond_swap_request(
    request_id: int,
    data: SwapRequestRespond,
    current_user: dict = Depends(get_current_user),
):
    """接受或拒絕換班申請（對象操作）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        swap = session.query(ShiftSwapRequest).filter(ShiftSwapRequest.id == request_id).first()
        if not swap:
            raise HTTPException(status_code=404, detail="找不到該換班申請")
        if swap.target_id != emp.id:
            raise HTTPException(status_code=403, detail="您不是此申請的換班對象")
        if swap.status != "pending":
            raise HTTPException(status_code=400, detail="此申請已不是待處理狀態")

        swap.target_responded_at = datetime.now()
        swap.target_remark = data.remark

        if data.action == "accept":
            # ── TOCTOU 保護：commit 前驗證快照是否已過期 ──────────────────────
            # 若主管在申請後、B 同意前修改了班表，直接用快照執行換班
            # 會悄悄還原主管的修改，且完全不留痕跡。
            # 快照過期 → 自動將申請設為 cancelled，要求重新發起。
            try:
                _assert_swap_snapshot_fresh(session, swap)
            except HTTPException:
                swap.status = "cancelled"
                logger.warning(
                    "換班申請 #%d 快照已過期（req_snapshot=%s, tgt_snapshot=%s），"
                    "班表已被主管修改，申請已自動取消",
                    swap.id,
                    swap.requester_shift_type_id,
                    swap.target_shift_type_id,
                )
                session.commit()
                raise

            swap.status = "accepted"
            swap.executed_at = datetime.now()

            for emp_id, new_shift_type_id in [
                (swap.requester_id, swap.target_shift_type_id),
                (swap.target_id, swap.requester_shift_type_id),
            ]:
                # new_shift_type_id 可能為 None（對方原本無班），
                # 此時仍需寫入 DailyShift(shift_type_id=None) 來顯式覆蓋週排班，
                # 否則本人的週排班會繼續生效，造成「排班複製」的人事成本錯誤。
                existing_ds = session.query(DailyShift).filter(
                    DailyShift.employee_id == emp_id,
                    DailyShift.date == swap.swap_date,
                ).first()
                if existing_ds:
                    existing_ds.shift_type_id = new_shift_type_id
                    existing_ds.notes = f"換班 #{swap.id}"
                else:
                    ds = DailyShift(
                        employee_id=emp_id,
                        shift_type_id=new_shift_type_id,  # None 表示該日排休
                        date=swap.swap_date,
                        notes=f"換班 #{swap.id}",
                    )
                    session.add(ds)

            session.commit()
            return {"message": "已接受換班，班別已自動互換"}

        elif data.action == "reject":
            swap.status = "rejected"
            session.commit()
            return {"message": "已拒絕換班申請"}

        else:
            raise HTTPException(status_code=400, detail="無效的操作")

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/swap-requests/{request_id}/cancel")
def cancel_swap_request(
    request_id: int,
    current_user: dict = Depends(get_current_user),
):
    """撤銷換班申請（發起人操作）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        swap = session.query(ShiftSwapRequest).filter(ShiftSwapRequest.id == request_id).first()
        if not swap:
            raise HTTPException(status_code=404, detail="找不到該換班申請")
        if swap.requester_id != emp.id:
            raise HTTPException(status_code=403, detail="您不是此申請的發起人")
        if swap.status != "pending":
            raise HTTPException(status_code=400, detail="只能撤銷待處理的申請")

        swap.status = "cancelled"
        session.commit()
        return {"message": "已撤銷換班申請"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.get("/swap-pending-count")
def get_swap_pending_count(
    current_user: dict = Depends(get_current_user),
):
    """取得待回覆的換班申請數量（用於 badge）"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        count = session.query(ShiftSwapRequest).filter(
            ShiftSwapRequest.target_id == emp.id,
            ShiftSwapRequest.status == "pending",
        ).count()
        return {"pending_count": count}
    finally:
        session.close()

"""
園務會議記錄 API
"""

import logging
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from pydantic import BaseModel

from models.database import get_session, MeetingRecord, Employee, SalaryRecord
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.approval_helpers import _get_finalized_salary_record
from services.salary.constants import DEFAULT_MEETING_HOURS

logger = logging.getLogger(__name__)


def _mark_meeting_emps_stale(session, emp_ids, meeting_date: date) -> None:
    """會議異動後將相關員工該月薪資標 needs_recalc=True。

    Why: 會議出席會進入 meeting_overtime_pay,缺席會在發放月扣節慶獎金;
    薪資算完後再改會議紀錄,既有 SalaryRecord 必須標 stale 避免被 finalize
    封存到舊金額。
    """
    if not emp_ids:
        return
    from services.salary.utils import mark_salary_stale

    for eid in set(emp_ids):
        try:
            mark_salary_stale(session, eid, meeting_date.year, meeting_date.month)
        except Exception:
            logger.warning(
                "會議異動標記 SalaryRecord stale 失敗 emp=%d %d/%d",
                eid,
                meeting_date.year,
                meeting_date.month,
                exc_info=True,
            )


def _meeting_pay_for(base_salary: float, hours: float) -> float:
    """以勞基法平日加班費公式計算園務會議加班費。

    底薪 ≤ 0 時視為 0 元（避免 calculate_overtime_pay 拋錯阻斷批次建立）。
    """
    if not base_salary or base_salary <= 0 or hours <= 0:
        return 0.0
    from api.overtimes import calculate_overtime_pay

    return calculate_overtime_pay(base_salary, hours, "weekday")


router = APIRouter(prefix="/api", tags=["meetings"])


# ============ Pydantic Models ============


class MeetingRecordCreate(BaseModel):
    employee_id: int
    meeting_date: str  # YYYY-MM-DD
    meeting_type: str = "staff_meeting"
    attended: bool = True
    overtime_hours: float = DEFAULT_MEETING_HOURS
    # overtime_pay 一律由後端依勞基法平日加班費公式計算，不接受前端傳入
    # （拿掉 override 防止 MEETINGS 權限者直接寫入超額金額繞過薪資簽核）
    remark: Optional[str] = None


class MeetingRecordUpdate(BaseModel):
    attended: Optional[bool] = None
    overtime_hours: Optional[float] = None
    # overtime_pay 由後端依 overtime_hours + 員工底薪自動重算，不接受前端 override
    remark: Optional[str] = None


class MeetingBatchCreate(BaseModel):
    """批次建立園務會議記錄（同一天所有員工）"""

    meeting_date: str  # YYYY-MM-DD
    meeting_type: str = "staff_meeting"
    attendees: List[int]  # 出席的 employee IDs
    absentees: List[int] = []  # 缺席的 employee IDs
    remark: Optional[str] = None


# ============ Business Rules ============


def _enforce_absent_no_overtime(record) -> None:
    """業務規則：缺席者不得有加班費，強制歸零。

    呼叫時機：每次 create / update 後，只要 record.attended 為 False，
    即清空 overtime_hours 與 overtime_pay，防止「幽靈加班費」產生。
    """
    if not record.attended:
        record.overtime_hours = 0
        record.overtime_pay = 0


def _assert_meeting_month_not_finalized(
    session, employee_id: int, meeting_date: date
) -> None:
    """會議記錄寫入前檢查該員工該月薪資是否已封存。

    會議出席影響 meeting_overtime_pay、缺席會扣節慶獎金，封存後仍可覆蓋
    會讓薪資與會議原始資料分叉。
    """
    record = _get_finalized_salary_record(
        session, employee_id, meeting_date.year, meeting_date.month
    )
    if record:
        by = record.finalized_by or "系統"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{meeting_date.year} 年 {meeting_date.month} 月薪資已封存"
                f"（結算人：{by}），無法修改會議紀錄。請先至薪資管理頁面解除封存後再操作。"
            ),
        )


def _assert_meeting_batch_month_not_finalized(
    session, emp_ids: list, meeting_date: date
) -> None:
    """批次建立/覆蓋會議記錄前：任一員工該月已封存即整批拒絕。"""
    if not emp_ids:
        return
    record = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.employee_id.in_(emp_ids),
            SalaryRecord.salary_year == meeting_date.year,
            SalaryRecord.salary_month == meeting_date.month,
            SalaryRecord.is_finalized == True,
        )
        .first()
    )
    if record:
        by = record.finalized_by or "系統"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{meeting_date.year} 年 {meeting_date.month} 月薪資已封存"
                f"（員工 #{record.employee_id}，結算人：{by}），"
                "無法批次建立會議紀錄。請先至薪資管理頁面解除封存後再操作。"
            ),
        )


# ============ Routes ============


@router.get("/meetings")
def get_meetings(
    year: int = Query(...),
    month: int = Query(...),
    employee_id: Optional[int] = Query(None),
    current_user: dict = Depends(require_staff_permission(Permission.MEETINGS)),
):
    """查詢園務會議記錄"""
    session = get_session()
    try:
        import calendar

        _, last_day = calendar.monthrange(year, month)
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)

        query = (
            session.query(MeetingRecord, Employee)
            .join(Employee, MeetingRecord.employee_id == Employee.id)
            .filter(
                MeetingRecord.meeting_date >= start_date,
                MeetingRecord.meeting_date <= end_date,
            )
        )

        if employee_id:
            query = query.filter(MeetingRecord.employee_id == employee_id)

        records = query.order_by(MeetingRecord.meeting_date, Employee.name).all()

        results = []
        for record, emp in records:
            results.append(
                {
                    "id": record.id,
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "meeting_date": record.meeting_date.isoformat(),
                    "meeting_type": record.meeting_type,
                    "attended": record.attended,
                    "overtime_hours": record.overtime_hours,
                    "overtime_pay": record.overtime_pay,
                    "remark": record.remark,
                }
            )

        return results
    finally:
        session.close()


@router.post("/meetings", status_code=201)
def create_meeting(
    data: MeetingRecordCreate,
    current_user: dict = Depends(require_staff_permission(Permission.MEETINGS)),
):
    """建立單筆園務會議記錄"""
    session = get_session()
    try:
        meeting_date = datetime.strptime(data.meeting_date, "%Y-%m-%d").date()
        _assert_meeting_month_not_finalized(session, data.employee_id, meeting_date)

        # 檢查是否已存在
        existing = (
            session.query(MeetingRecord)
            .filter(
                MeetingRecord.employee_id == data.employee_id,
                MeetingRecord.meeting_date == meeting_date,
                MeetingRecord.meeting_type == data.meeting_type,
            )
            .first()
        )

        if existing:
            raise HTTPException(status_code=400, detail="該員工此日期已有記錄")

        emp = session.query(Employee).get(data.employee_id)
        base = emp.base_salary if emp else 0
        pay = _meeting_pay_for(base, data.overtime_hours)

        record = MeetingRecord(
            employee_id=data.employee_id,
            meeting_date=meeting_date,
            meeting_type=data.meeting_type,
            attended=data.attended,
            overtime_hours=data.overtime_hours,
            overtime_pay=pay,
            remark=data.remark,
        )
        # 業務規則：缺席者不得有加班費
        _enforce_absent_no_overtime(record)
        session.add(record)
        # 未封存月既有薪資需標 stale,避免後續 finalize 把舊薪資封存
        _mark_meeting_emps_stale(session, [data.employee_id], meeting_date)
        session.commit()

        return {"message": "建立成功", "id": record.id}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/meetings/batch", status_code=201)
def create_meetings_batch(
    data: MeetingBatchCreate,
    current_user: dict = Depends(require_staff_permission(Permission.MEETINGS)),
):
    """批次建立園務會議記錄（一次建立同日所有員工）"""
    session = get_session()
    try:
        meeting_date = datetime.strptime(data.meeting_date, "%Y-%m-%d").date()

        # 既有同日同類型紀錄的所有 employee_id 也要納入封存檢查；
        # 否則 payload 沒列到的舊員工會被下方 delete() 連帶清掉，繞過已封存月守衛。
        existing_emp_ids = [
            row[0]
            for row in session.query(MeetingRecord.employee_id)
            .filter(
                MeetingRecord.meeting_date == meeting_date,
                MeetingRecord.meeting_type == data.meeting_type,
            )
            .all()
        ]
        all_emp_ids = list({*data.attendees, *data.absentees, *existing_emp_ids})
        _assert_meeting_batch_month_not_finalized(session, all_emp_ids, meeting_date)

        # 先刪除該日同類型已有記錄（覆蓋模式）
        session.query(MeetingRecord).filter(
            MeetingRecord.meeting_date == meeting_date,
            MeetingRecord.meeting_type == data.meeting_type,
        ).delete()

        created = 0

        # 查詢員工底薪用於計算平日加班費
        employees = session.query(Employee).filter(Employee.id.in_(all_emp_ids)).all()
        emp_map = {e.id: e for e in employees}

        # 建立出席記錄：依勞基法平日加班費公式（底薪 ÷ 30 ÷ 8 × 1 小時 × 1.34）
        for emp_id in data.attendees:
            emp = emp_map.get(emp_id)
            base = emp.base_salary if emp else 0
            pay = _meeting_pay_for(base, DEFAULT_MEETING_HOURS)

            record = MeetingRecord(
                employee_id=emp_id,
                meeting_date=meeting_date,
                meeting_type=data.meeting_type,
                attended=True,
                overtime_hours=DEFAULT_MEETING_HOURS,
                overtime_pay=pay,
                remark=data.remark,
            )
            session.add(record)
            created += 1

        # 建立缺席記錄
        for emp_id in data.absentees:
            record = MeetingRecord(
                employee_id=emp_id,
                meeting_date=meeting_date,
                meeting_type=data.meeting_type,
                attended=False,
                overtime_hours=0,
                overtime_pay=0,
                remark=data.remark,
            )
            session.add(record)
            created += 1

        # 未封存月既有薪資需標 stale。覆蓋模式下被刪除的舊員工(existing_emp_ids
        # 中不在 attendees/absentees 內者)也算實質異動,須一併標。
        _mark_meeting_emps_stale(session, all_emp_ids, meeting_date)
        session.commit()
        return {"message": f"批次建立完成，共 {created} 筆", "count": created}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/meetings/{record_id}")
def update_meeting(
    record_id: int,
    data: MeetingRecordUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.MEETINGS)),
):
    """更新園務會議記錄"""
    session = get_session()
    try:
        record = session.query(MeetingRecord).get(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="記錄不存在")

        _assert_meeting_month_not_finalized(
            session, record.employee_id, record.meeting_date
        )

        if data.attended is not None:
            record.attended = data.attended
        if data.overtime_hours is not None:
            record.overtime_hours = data.overtime_hours
        if data.remark is not None:
            record.remark = data.remark

        # overtime_pay 一律以勞基法公式重算（依當前 attended/overtime_hours/員工底薪）
        # Why: 不接受前端 override，避免 MEETINGS 權限者塞高額金流進薪資
        if record.attended:
            emp = session.query(Employee).get(record.employee_id)
            base = emp.base_salary if emp else 0
            record.overtime_pay = _meeting_pay_for(base, record.overtime_hours or 0)

        # 業務規則：缺席者不得有加班費（與上方 attended 分支互補）
        _enforce_absent_no_overtime(record)

        # 未封存月既有薪資需標 stale,避免後續 finalize 把舊薪資封存
        _mark_meeting_emps_stale(session, [record.employee_id], record.meeting_date)
        session.commit()
        return {"message": "更新成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/meetings/{record_id}")
def delete_meeting(
    record_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.MEETINGS)),
):
    """刪除園務會議記錄"""
    session = get_session()
    try:
        record = session.query(MeetingRecord).get(record_id)
        if not record:
            raise HTTPException(status_code=404, detail="記錄不存在")

        _assert_meeting_month_not_finalized(
            session, record.employee_id, record.meeting_date
        )

        emp_id = record.employee_id
        meeting_date = record.meeting_date
        session.delete(record)
        # 未封存月既有薪資需標 stale,避免後續 finalize 把舊薪資封存
        _mark_meeting_emps_stale(session, [emp_id], meeting_date)
        session.commit()
        return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/meetings/summary")
def get_meeting_summary(
    year: int = Query(...),
    month: int = Query(...),
    current_user: dict = Depends(require_staff_permission(Permission.MEETINGS)),
):
    """查詢當月園務會議出勤統計"""
    session = get_session()
    try:
        import calendar

        _, last_day = calendar.monthrange(year, month)
        start_date = date(year, month, 1)
        end_date = date(year, month, last_day)

        records = (
            session.query(MeetingRecord, Employee)
            .join(Employee, MeetingRecord.employee_id == Employee.id)
            .filter(
                MeetingRecord.meeting_date >= start_date,
                MeetingRecord.meeting_date <= end_date,
                Employee.is_active == True,
            )
            .all()
        )

        # 彙總每位員工
        summary = {}
        for r, emp in records:
            if emp.id not in summary:
                summary[emp.id] = {
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "attended": 0,
                    "absent": 0,
                    "total_pay": 0,
                }
            if r.attended:
                summary[emp.id]["attended"] += 1
                summary[emp.id]["total_pay"] += r.overtime_pay or 0
            else:
                summary[emp.id]["absent"] += 1

        return list(summary.values())
    finally:
        session.close()

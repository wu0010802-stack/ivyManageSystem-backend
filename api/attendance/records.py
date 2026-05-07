"""
Attendance - CRUD endpoints for attendance records
"""

import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500

from models.database import get_session, Employee, Attendance, SalaryRecord
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.approval_helpers import _get_finalized_salary_record
from utils.attendance_guards import require_not_self_attendance
from services.salary.utils import lock_and_premark_stale
from ._shared import AttendanceRecordUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


# ── 出勤紀錄保存期（勞基法第 30 條第 5 項：保存 5 年）───────────────
ATTENDANCE_RETENTION_YEARS = 5


def _retention_cutoff(today: date) -> date:
    """回傳「N 年前的同一天」；若閏年 2/29 則退回 2/28。"""
    target_year = today.year - ATTENDANCE_RETENTION_YEARS
    try:
        return today.replace(year=target_year)
    except ValueError:
        return today.replace(year=target_year, day=28)


def _assert_attendance_within_retention(
    attendance_date: date, today: Optional[date] = None
) -> None:
    """5 年保存期內的出勤紀錄不得刪除（勞基法第 30 條第 5 項）。"""
    today = today or date.today()
    cutoff = _retention_cutoff(today)
    if attendance_date >= cutoff:
        raise HTTPException(
            status_code=400,
            detail=(
                f"考勤日期 {attendance_date} 在 5 年保存期內（≥ {cutoff}），"
                "依勞基法第 30 條第 5 項出勤紀錄須保存 5 年，不得刪除"
            ),
        )


def _assert_attendance_not_finalized(
    session, employee_id: int, attendance_date: date
) -> None:
    """考勤寫入/刪除前檢查該員工該月薪資是否已封存。

    封存月份若補寫或刪考勤紀錄，缺卡/遲到/曠職來源資料會變，
    但 salary_records 仍保留原封存結果，造成對帳不一致。
    """
    record = _get_finalized_salary_record(
        session, employee_id, attendance_date.year, attendance_date.month
    )
    if record:
        by = record.finalized_by or "系統"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{attendance_date.year} 年 {attendance_date.month} 月薪資已封存"
                f"（結算人：{by}），無法修改該月份考勤紀錄。請先至薪資管理頁面解除封存後再操作。"
            ),
        )


def _assert_month_no_finalized_salary(session, year: int, month: int) -> None:
    """整月考勤刪除前檢查該月份是否有任何員工薪資已封存。"""
    record = (
        session.query(SalaryRecord)
        .filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
            SalaryRecord.is_finalized == True,
        )
        .first()
    )
    if record:
        by = record.finalized_by or "系統"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{year} 年 {month} 月已有薪資封存（結算人：{by}），"
                "無法整月刪除考勤紀錄。請先至薪資管理頁面解除封存後再操作。"
            ),
        )


def _assert_upload_months_not_finalized(session, emp_ids: set, dates: set) -> None:
    """考勤批次匯入前檢查：涉及的 (員工, 月份) 是否有薪資已封存。

    邏輯對齊單筆守衛：任一將寫入的 (emp_id, year, month) 已封存 → 整批 409。
    """
    if not emp_ids or not dates:
        return
    from sqlalchemy import and_, or_

    months = {(d.year, d.month) for d in dates}
    rows = (
        session.query(
            SalaryRecord.employee_id,
            SalaryRecord.salary_year,
            SalaryRecord.salary_month,
            SalaryRecord.finalized_by,
        )
        .filter(
            SalaryRecord.employee_id.in_(emp_ids),
            SalaryRecord.is_finalized == True,
            or_(
                *(
                    and_(
                        SalaryRecord.salary_year == y,
                        SalaryRecord.salary_month == m,
                    )
                    for y, m in months
                )
            ),
        )
        .all()
    )
    if rows:
        detail_rows = ", ".join(
            f"員工#{r.employee_id} {r.salary_year}/{r.salary_month:02d}"
            f"（結算人：{r.finalized_by or '系統'}）"
            for r in rows
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"下列月份薪資已封存，無法批次匯入考勤：{detail_rows}。"
                "請先至薪資管理頁面解除封存後再操作。"
            ),
        )


@router.get("/records")
async def get_attendance_records(
    year: int = Query(...),
    month: int = Query(...),
    employee_id: Optional[int] = None,
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_READ)),
):
    """查詢考勤記錄"""
    session = get_session()
    try:
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)

        query = (
            session.query(Attendance, Employee)
            .join(Employee)
            .filter(
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date,
            )
        )

        if employee_id:
            query = query.filter(Attendance.employee_id == employee_id)

        query = query.order_by(Employee.name, Attendance.attendance_date)

        records = query.limit(5000).all()

        result = []
        for att, emp in records:
            result.append(
                {
                    "id": att.id,
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "employee_number": emp.employee_id,
                    "date": att.attendance_date.isoformat(),
                    "weekday": ["一", "二", "三", "四", "五", "六", "日"][
                        att.attendance_date.weekday()
                    ],
                    "punch_in": (
                        att.punch_in_time.strftime("%H:%M")
                        if att.punch_in_time
                        else None
                    ),
                    "punch_out": (
                        att.punch_out_time.strftime("%H:%M")
                        if att.punch_out_time
                        else None
                    ),
                    "status": att.status,
                    "is_late": att.is_late,
                    "is_early_leave": att.is_early_leave,
                    "is_missing_punch_in": att.is_missing_punch_in,
                    "is_missing_punch_out": att.is_missing_punch_out,
                    "late_minutes": att.late_minutes,
                    "early_leave_minutes": att.early_leave_minutes,
                    "remark": att.remark,
                }
            )

        return result
    finally:
        session.close()


@router.post("/record", status_code=201)
async def create_or_update_attendance_record(
    record: AttendanceRecordUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_WRITE)),
):
    """新增或更新單筆考勤記錄"""
    # 自我守衛（F-041）：caller 不可寫自己的考勤紀錄；對齊
    # api/overtimes.py:1078-1079、api/leaves.py:1014-1018 既有 idiom。
    require_not_self_attendance(current_user, record.employee_id)

    session = get_session()
    try:
        employee = (
            session.query(Employee).filter(Employee.id == record.employee_id).first()
        )
        if not employee:
            raise HTTPException(status_code=404, detail="找不到員工")

        try:
            attendance_date = datetime.strptime(record.date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(
                status_code=400, detail="日期格式錯誤，請使用 YYYY-MM-DD"
            )

        _assert_attendance_not_finalized(session, employee.id, attendance_date)

        punch_in_time = None
        if record.punch_in and record.punch_in.strip():
            try:
                punch_in_time = datetime.combine(
                    attendance_date,
                    datetime.strptime(record.punch_in.strip(), "%H:%M").time(),
                )
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="上班時間格式錯誤，請使用 HH:MM"
                )

        punch_out_time = None
        if record.punch_out and record.punch_out.strip():
            try:
                punch_out_time = datetime.combine(
                    attendance_date,
                    datetime.strptime(record.punch_out.strip(), "%H:%M").time(),
                )
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="下班時間格式錯誤，請使用 HH:MM"
                )

        # 跨夜班修正：下班時間早於上班時間表示隔日下班（如 18:00→02:00）
        if punch_in_time and punch_out_time and punch_out_time < punch_in_time:
            punch_out_time += timedelta(days=1)
        elif punch_in_time and punch_out_time and punch_out_time == punch_in_time:
            raise HTTPException(
                status_code=400,
                detail=f"時間錯誤：上下班時間相同 {record.punch_in}，請確認資料",
            )

        from utils.attendance_calc import recompute_attendance_status

        fields = recompute_attendance_status(
            attendance_date=attendance_date,
            punch_in_time=punch_in_time,
            punch_out_time=punch_out_time,
            work_start_str=employee.work_start_time,
            work_end_str=employee.work_end_time,
        )
        is_late = fields["is_late"]
        is_early_leave = fields["is_early_leave"]
        is_missing_punch_in = fields["is_missing_punch_in"]
        is_missing_punch_out = fields["is_missing_punch_out"]
        late_minutes = fields["late_minutes"]
        early_leave_minutes = fields["early_leave_minutes"]
        status = fields["status"]

        existing = (
            session.query(Attendance)
            .filter(
                Attendance.employee_id == employee.id,
                Attendance.attendance_date == attendance_date,
            )
            .first()
        )

        if existing:
            existing.punch_in_time = punch_in_time
            existing.punch_out_time = punch_out_time
            existing.status = status
            existing.is_late = is_late
            existing.is_early_leave = is_early_leave
            existing.is_missing_punch_in = is_missing_punch_in
            existing.is_missing_punch_out = is_missing_punch_out
            existing.late_minutes = late_minutes
            existing.early_leave_minutes = early_leave_minutes
            message = "考勤記錄已更新"
        else:
            attendance = Attendance(
                employee_id=employee.id,
                attendance_date=attendance_date,
                punch_in_time=punch_in_time,
                punch_out_time=punch_out_time,
                status=status,
                is_late=is_late,
                is_early_leave=is_early_leave,
                is_missing_punch_in=is_missing_punch_in,
                is_missing_punch_out=is_missing_punch_out,
                late_minutes=late_minutes,
                early_leave_minutes=early_leave_minutes,
            )
            session.add(attendance)
            message = "考勤記錄已新增"

        # 考勤異動會改變遲到/早退/缺打卡計數,進而影響薪資扣款計算;
        # 該月若有未封存薪資需標 stale,讓 finalize 守衛擋下舊薪資。
        # 已封存月份由 _assert_attendance_not_finalized 攔下,此處不會執行。
        # lock_and_premark_stale 同時取 advisory lock + 標 stale,避免 finalize 在
        # 「來源檢查通過 → mark_stale → caller commit」中間以舊 needs_recalc=False 搶先封存。

        lock_and_premark_stale(
            session,
            employee.id,
            {(attendance_date.year, attendance_date.month)},
        )

        session.commit()

        return {
            "message": message,
            "status": status,
            "is_late": is_late,
            "late_minutes": late_minutes,
            "is_early_leave": is_early_leave,
            "early_leave_minutes": early_leave_minutes,
        }

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/record/{employee_id}/{date}")
async def delete_single_attendance_record(
    employee_id: int,
    date: str,
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_WRITE)),
):
    """刪除單筆考勤記錄"""
    # 自我守衛（F-041）：caller 不可刪自己的考勤紀錄。
    require_not_self_attendance(current_user, employee_id)

    session = get_session()
    try:
        attendance_date = datetime.strptime(date, "%Y-%m-%d").date()
        _assert_attendance_within_retention(attendance_date)
        _assert_attendance_not_finalized(session, employee_id, attendance_date)

        deleted = (
            session.query(Attendance)
            .filter(
                Attendance.employee_id == employee_id,
                Attendance.attendance_date == attendance_date,
            )
            .delete()
        )

        if deleted:

            lock_and_premark_stale(
                session,
                employee_id,
                {(attendance_date.year, attendance_date.month)},
            )

        session.commit()

        if deleted:
            return {"message": "考勤記錄已刪除"}
        else:
            raise HTTPException(status_code=404, detail="找不到該考勤記錄")

    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/records/{employee_id}/{date_str}")
def delete_single_attendance(
    employee_id: int,
    date_str: str,
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_WRITE)),
):
    """刪除單筆考勤記錄"""
    # 自我守衛（F-041）：caller 不可刪自己的考勤紀錄。
    require_not_self_attendance(current_user, employee_id)

    session = get_session()
    try:
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            target_date = datetime.strptime(date_str, "%Y/%m/%d").date()
        _assert_attendance_within_retention(target_date)
        _assert_attendance_not_finalized(session, employee_id, target_date)

        record = (
            session.query(Attendance)
            .filter(
                Attendance.employee_id == employee_id,
                Attendance.attendance_date == target_date,
            )
            .first()
        )

        if not record:
            raise HTTPException(status_code=404, detail="找不到該筆考勤記錄")

        session.delete(record)

        lock_and_premark_stale(
            session, employee_id, {(target_date.year, target_date.month)}
        )

        session.commit()
        return {"message": "刪除成功"}

    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式錯誤")
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/records/{year}/{month}")
async def delete_attendance_records(
    year: int,
    month: int,
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_WRITE)),
):
    """刪除指定月份的所有考勤記錄"""
    session = get_session()
    try:
        start_date = date(year, month, 1)
        _, last_day = monthrange(year, month)
        end_date = date(year, month, last_day)
        # 整月任一天落在 5 年保存期內就拒絕（最嚴格保護）
        _assert_attendance_within_retention(end_date)
        _assert_month_no_finalized_salary(session, year, month)

        # 刪除前先撈出涉及的員工 id,以便整月刪除後標 stale
        affected_emp_ids = [
            row[0]
            for row in session.query(Attendance.employee_id)
            .filter(
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date,
            )
            .distinct()
            .all()
        ]

        deleted = (
            session.query(Attendance)
            .filter(
                Attendance.attendance_date >= start_date,
                Attendance.attendance_date <= end_date,
            )
            .delete()
        )

        if affected_emp_ids:

            for emp_id in affected_emp_ids:
                lock_and_premark_stale(session, emp_id, {(year, month)})

        session.commit()

        return {"message": f"已刪除 {deleted} 筆考勤記錄"}
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()

"""
Overtime management router
"""

import logging
import calendar as cal_module
from datetime import date, datetime, time as dt_time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import or_

from models.database import get_session, Employee, OvertimeRecord
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["overtimes"])

# ============ Service Injection ============

_salary_engine = None


def init_overtimes_services(salary_engine_instance):
    global _salary_engine
    _salary_engine = salary_engine_instance


# ============ Constants ============

OVERTIME_TYPE_LABELS = {
    "weekday": "平日",
    "weekend": "假日",
    "holiday": "國定假日",
}


# ============ 加班倍率常數（勞基法） ============
WEEKDAY_FIRST_2H_RATE = 1.34   # 平日前 2 小時
WEEKDAY_AFTER_2H_RATE = 1.67   # 平日第 3-4 小時
WEEKDAY_THRESHOLD_HOURS = 2     # 平日倍率分界時數
HOLIDAY_RATE = 2.0              # 假日 / 國定假日
DAILY_WORK_HOURS = 8            # 每日法定工時
MONTHLY_BASE_DAYS = 30          # 勞基法時薪計算基準日數（月薪 ÷ 30 ÷ 8）
# 單筆加班記錄的合法上限：勞基法假日最多可加班至 12 小時（正常 8H + 延長 4H）
MAX_OVERTIME_HOURS = 12.0


# ============ Helper Functions ============

def _to_time(val) -> dt_time:
    """str / datetime.time / datetime.datetime 統一正規化為 datetime.time。

    DB 欄位依設定不同可能回傳 datetime.time（Time 欄位）或 datetime.datetime（DateTime 欄位）；
    外部輸入則為 'HH:MM' 字串。直接混型比較（str < time、datetime < time 等）會
    觸發 TypeError，本函式確保任何輸入都能安全轉換為可比較的 datetime.time。
    """
    if isinstance(val, str):
        h, m = map(int, val.strip().split(':'))
        return dt_time(h, m)
    if isinstance(val, datetime):   # datetime 是 date 的子類別，必須在 date 之前檢查
        return val.time()
    if isinstance(val, dt_time):
        return val
    raise TypeError(f"無法將 {type(val).__name__!r} 轉為 datetime.time")


def _times_overlap(start1, end1, start2, end2) -> bool:
    """判斷兩個時間區間是否重疊（開放端點：端點相接不視為重疊）。

    接受 str ('HH:MM')、datetime.time 或 datetime.datetime，
    透過 _to_time() 統一轉換後再比較，不受傳入型別影響。

    公式：start1 < end2 AND start2 < end1
    """
    return _to_time(start1) < _to_time(end2) and _to_time(start2) < _to_time(end1)


def calculate_overtime_pay(base_salary: float, hours: float, overtime_type: str) -> float:
    """依勞基法計算加班費（時薪 = 月薪 ÷ 30 ÷ 8）"""
    # 防禦縱深：即使前端驗證被繞過，也不允許負數或零時數計算
    if hours <= 0:
        return 0.0
    hours = min(hours, MAX_OVERTIME_HOURS)
    hourly_base = base_salary / MONTHLY_BASE_DAYS / DAILY_WORK_HOURS

    if overtime_type == "weekday":
        if hours <= WEEKDAY_THRESHOLD_HOURS:
            return round(hourly_base * hours * WEEKDAY_FIRST_2H_RATE)
        else:
            return round(
                hourly_base * WEEKDAY_THRESHOLD_HOURS * WEEKDAY_FIRST_2H_RATE
                + hourly_base * (hours - WEEKDAY_THRESHOLD_HOURS) * WEEKDAY_AFTER_2H_RATE
            )
    else:
        return round(hourly_base * hours * HOLIDAY_RATE)


def _check_overtime_overlap(
    session,
    employee_id: int,
    overtime_date: date,
    start_time,
    end_time,
    exclude_id: int = None,
) -> "OvertimeRecord | None":
    """
    檢查員工在指定日期是否已有時間重疊的加班申請（待審核或已核准）。

    重疊規則：
    - 已駁回的申請不列入，允許重新申請
    - 若新申請或現有記錄缺少時間資訊，同日即視為重疊
    - 若雙方都有 start/end time，做時間區間重疊判斷（start1 < end2 AND start2 < end1）
    """
    q = session.query(OvertimeRecord).filter(
        OvertimeRecord.employee_id == employee_id,
        OvertimeRecord.overtime_date == overtime_date,
        or_(OvertimeRecord.is_approved.is_(None), OvertimeRecord.is_approved == True),
    )
    if exclude_id is not None:
        q = q.filter(OvertimeRecord.id != exclude_id)

    for record in q.all():
        if (
            start_time is None
            or end_time is None
            or record.start_time is None
            or record.end_time is None
        ):
            return record  # 缺乏時間資訊，同日即視為重疊
        if _times_overlap(start_time, end_time, record.start_time, record.end_time):
            return record  # 時間區間重疊

    return None


# ============ Pydantic Models ============

class OvertimeCreate(BaseModel):
    employee_id: int
    overtime_date: date
    overtime_type: str  # weekday / weekend / holiday
    start_time: Optional[str] = None  # HH:MM
    end_time: Optional[str] = None    # HH:MM
    hours: float
    reason: Optional[str] = None

    @field_validator("overtime_type")
    @classmethod
    def validate_overtime_type(cls, v):
        if v not in OVERTIME_TYPE_LABELS:
            allowed = ", ".join(OVERTIME_TYPE_LABELS.keys())
            raise ValueError(f"無效的加班類型，允許值：{allowed}")
        return v

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v):
        if v <= 0:
            raise ValueError("加班時數必須大於 0")
        if v > MAX_OVERTIME_HOURS:
            raise ValueError(f"單筆加班時數不得超過 {MAX_OVERTIME_HOURS} 小時")
        return v


class OvertimeUpdate(BaseModel):
    overtime_date: Optional[date] = None
    overtime_type: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    hours: Optional[float] = None
    reason: Optional[str] = None

    @field_validator("overtime_type")
    @classmethod
    def validate_overtime_type(cls, v):
        if v is not None and v not in OVERTIME_TYPE_LABELS:
            allowed = ", ".join(OVERTIME_TYPE_LABELS.keys())
            raise ValueError(f"無效的加班類型，允許值：{allowed}")
        return v

    @field_validator("hours")
    @classmethod
    def validate_hours(cls, v):
        if v is None:
            return v
        if v <= 0:
            raise ValueError("加班時數必須大於 0")
        if v > MAX_OVERTIME_HOURS:
            raise ValueError(f"單筆加班時數不得超過 {MAX_OVERTIME_HOURS} 小時")
        return v


# ============ Routes ============

@router.get("/overtimes")
def get_overtimes(
    employee_id: Optional[int] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    status: Optional[str] = None,  # pending, approved, rejected
    current_user: dict = Depends(require_permission(Permission.OVERTIME_READ)),
):
    """查詢加班記錄"""
    session = get_session()
    try:
        q = session.query(OvertimeRecord, Employee).join(
            Employee, OvertimeRecord.employee_id == Employee.id
        )
        if employee_id:
            q = q.filter(OvertimeRecord.employee_id == employee_id)
        if year and month:
            _, last_day = cal_module.monthrange(year, month)
            start = date(year, month, 1)
            end = date(year, month, last_day)
            q = q.filter(OvertimeRecord.overtime_date >= start, OvertimeRecord.overtime_date <= end)
        elif year:
            q = q.filter(OvertimeRecord.overtime_date >= date(year, 1, 1), OvertimeRecord.overtime_date <= date(year, 12, 31))

        if status == "pending":
            q = q.filter(OvertimeRecord.is_approved.is_(None))
        elif status == "approved":
            q = q.filter(OvertimeRecord.is_approved == True)
        elif status == "rejected":
            q = q.filter(OvertimeRecord.is_approved == False)

        records = q.order_by(OvertimeRecord.overtime_date.desc()).all()

        results = []
        for ot, emp in records:
            results.append({
                "id": ot.id,
                "employee_id": ot.employee_id,
                "employee_name": emp.name,
                "overtime_date": ot.overtime_date.isoformat(),
                "overtime_type": ot.overtime_type,
                "overtime_type_label": OVERTIME_TYPE_LABELS.get(ot.overtime_type, ot.overtime_type),
                "start_time": ot.start_time.strftime("%H:%M") if ot.start_time else None,
                "end_time": ot.end_time.strftime("%H:%M") if ot.end_time else None,
                "hours": ot.hours,
                "overtime_pay": ot.overtime_pay,
                "is_approved": ot.is_approved,
                "approved_by": ot.approved_by,
                "reason": ot.reason,
                "created_at": ot.created_at.isoformat() if ot.created_at else None,
            })
        return results
    finally:
        session.close()


@router.post("/overtimes", status_code=201)
def create_overtime(data: OvertimeCreate, current_user: dict = Depends(require_permission(Permission.OVERTIME_WRITE))):
    """新增加班記錄（自動計算加班費）"""
    session = get_session()
    try:
        if data.overtime_type not in OVERTIME_TYPE_LABELS:
            raise HTTPException(status_code=400, detail=f"無效的加班類型: {data.overtime_type}")

        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail="員工不存在")

        pay = calculate_overtime_pay(emp.base_salary, data.hours, data.overtime_type)

        start_dt = None
        end_dt = None
        if data.start_time:
            h, m = map(int, data.start_time.split(":"))
            start_dt = datetime.combine(data.overtime_date, datetime.min.time().replace(hour=h, minute=m))
        if data.end_time:
            h, m = map(int, data.end_time.split(":"))
            end_dt = datetime.combine(data.overtime_date, datetime.min.time().replace(hour=h, minute=m))

        overlap = _check_overtime_overlap(session, data.employee_id, data.overtime_date, start_dt, end_dt)
        if overlap:
            st = overlap.start_time.strftime("%H:%M") if overlap.start_time else "未指定"
            et = overlap.end_time.strftime("%H:%M") if overlap.end_time else "未指定"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"該員工在 {overlap.overtime_date} 已有時間重疊的加班申請"
                    f"（ID: {overlap.id}，{st}～{et}），請勿重複申請"
                ),
            )

        ot = OvertimeRecord(
            employee_id=data.employee_id,
            overtime_date=data.overtime_date,
            overtime_type=data.overtime_type,
            start_time=start_dt,
            end_time=end_dt,
            hours=data.hours,
            overtime_pay=pay,
            reason=data.reason,
            is_approved=None,  # Explicitly set to Pending
        )
        session.add(ot)
        session.commit()
        return {"message": "加班記錄已新增", "id": ot.id, "overtime_pay": pay}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.put("/overtimes/{overtime_id}")
def update_overtime(overtime_id: int, data: OvertimeUpdate, current_user: dict = Depends(require_permission(Permission.OVERTIME_WRITE))):
    """更新加班記錄。若記錄已核准，修改後自動退回「待審核」狀態以符合稽核要求。"""
    session = get_session()
    try:
        ot = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).first()
        if not ot:
            raise HTTPException(status_code=404, detail="加班記錄不存在")

        # 記錄修改前的核准狀態（供後續稽核退審判斷）
        was_approved = ot.is_approved == True

        # 先計算更新後的日期與時間（供重疊檢查使用）
        check_date = data.overtime_date or ot.overtime_date
        if data.start_time:
            h, m = map(int, data.start_time.split(":"))
            new_start_dt = datetime.combine(check_date, datetime.min.time().replace(hour=h, minute=m))
        else:
            new_start_dt = ot.start_time
        if data.end_time:
            h, m = map(int, data.end_time.split(":"))
            new_end_dt = datetime.combine(check_date, datetime.min.time().replace(hour=h, minute=m))
        else:
            new_end_dt = ot.end_time

        overlap = _check_overtime_overlap(session, ot.employee_id, check_date, new_start_dt, new_end_dt, exclude_id=overtime_id)
        if overlap:
            st = overlap.start_time.strftime("%H:%M") if overlap.start_time else "未指定"
            et = overlap.end_time.strftime("%H:%M") if overlap.end_time else "未指定"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"修改後的時段與已存在的加班申請重疊"
                    f"（ID: {overlap.id}，{overlap.overtime_date} {st}～{et}），請調整時段"
                ),
            )

        update_data = data.dict(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None and key not in ('start_time', 'end_time'):
                setattr(ot, key, value)

        if data.start_time:
            ot.start_time = new_start_dt
        if data.end_time:
            ot.end_time = new_end_dt

        # Recalculate pay
        emp = session.query(Employee).filter(Employee.id == ot.employee_id).first()
        if emp:
            ot.overtime_pay = calculate_overtime_pay(emp.base_salary, ot.hours, ot.overtime_type)

        # ── 稽核退審：已核准的記錄被修改，自動退回待審核 ──────────────────────
        # 防止管理員靜默修改已核准加班時數，導致薪資異常（財務防呆）
        if was_approved:
            ot.is_approved = None
            ot.approved_by = None
            logger.warning(
                "稽核警告：已核准加班記錄 #%d（員工 ID=%d, %s）被管理員「%s」修改，"
                "已自動退回待審核狀態，需重新核准",
                overtime_id, ot.employee_id, ot.overtime_date,
                current_user.get("username", "unknown"),
            )

        session.commit()

        msg = "加班記錄已更新"
        if was_approved:
            msg += "；原核准狀態已自動退回「待審核」，請重新送審"
        return {"message": msg, "overtime_pay": ot.overtime_pay, "reset_to_pending": was_approved}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.delete("/overtimes/{overtime_id}")
def delete_overtime(overtime_id: int, current_user: dict = Depends(require_permission(Permission.OVERTIME_WRITE))):
    """刪除加班記錄"""
    session = get_session()
    try:
        ot = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).first()
        if not ot:
            raise HTTPException(status_code=404, detail="加班記錄不存在")
        session.delete(ot)
        session.commit()
        return {"message": "加班記錄已刪除"}
    finally:
        session.close()


@router.put("/overtimes/{overtime_id}/approve")
def approve_overtime(overtime_id: int, approved: bool = True, approved_by: str = "管理員", current_user: dict = Depends(require_permission(Permission.OVERTIME_WRITE))):
    """核准/駁回加班；核准後自動重算該員工當月薪資"""
    session = get_session()
    try:
        ot = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).first()
        if not ot:
            raise HTTPException(status_code=404, detail="加班記錄不存在")
        ot.is_approved = approved
        ot.approved_by = approved_by
        session.commit()

        result = {"message": "已核准" if approved else "已駁回"}

        # 核准後自動重算該員工當月薪資
        if approved and _salary_engine is not None:
            try:
                year = ot.overtime_date.year
                month = ot.overtime_date.month
                emp_id = ot.employee_id
                _salary_engine.process_salary_calculation(emp_id, year, month)
                result["salary_recalculated"] = True
                result["message"] = "已核准，薪資已自動重算"
                logger.info(f"加班核准後自動重算薪資：emp_id={emp_id}, {year}/{month}")
            except Exception as e:
                result["salary_recalculated"] = False
                result["warning"] = "已核准，但薪資重算失敗，請手動前往薪資頁面重新計算"
                logger.error(f"加班核准後薪資重算失敗：{e}")

        return result
    finally:
        session.close()

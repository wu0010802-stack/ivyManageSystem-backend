"""
Overtime management router
"""

import logging
import calendar as cal_module
from datetime import date, datetime, time as dt_time
from io import BytesIO
from typing import Optional, List

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from utils.errors import raise_safe_500
from utils.excel_utils import SafeWorksheet
from utils.rate_limit import SlidingWindowLimiter

# 批次核准為重 DB 操作（多筆 FOR UPDATE + 狀態變更 + LINE 推播），防內部濫用或帳號被盜後的放大
_batch_approve_limiter = SlidingWindowLimiter(
    max_calls=10,
    window_seconds=60,
    name="overtime_batch_approve",
    error_detail="批次審核操作過於頻繁，請稍後再試",
).as_dependency()
from utils.file_upload import validate_file_signature
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import func, or_, and_

from models.database import (
    get_session,
    Employee,
    OvertimeRecord,
    LeaveQuota,
    User,
    LeaveRecord,
    SalaryRecord,
)
from models.event import Holiday
from utils.auth import require_staff_permission
from utils.constants import (
    OVERTIME_TYPE_LABELS,
    MAX_OVERTIME_HOURS,
    MAX_MONTHLY_OVERTIME_HOURS,
    DAILY_WORK_HOURS,
    WEEKDAY_FIRST_2H_RATE,
    WEEKDAY_AFTER_2H_RATE,
    WEEKDAY_THRESHOLD_HOURS,
    HOLIDAY_RATE,
    RESTDAY_FIRST_2H_RATE,
    RESTDAY_MID_RATE,
    RESTDAY_AFTER_8H_RATE,
    RESTDAY_FIRST_SEGMENT,
    RESTDAY_SECOND_SEGMENT,
    RESTDAY_MIN_HOURS,
)
from utils.validators import validate_hhmm_format
from utils.error_messages import EMPLOYEE_DOES_NOT_EXIST, OVERTIME_RECORD_NOT_FOUND
from utils.permissions import Permission
from services.salary.utils import mark_salary_stale as _mark_salary_stale
from utils.approval_helpers import (
    _get_submitter_role,
    _check_approval_eligibility,
    _write_approval_log,
    _get_finalized_salary_record,
)
from utils.excel_utils import xlsx_streaming_response
from utils.import_utils import build_employee_lookup, resolve_employee_from_row

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["overtimes"])

# ============ Service Injection ============

_salary_engine = None
_line_service = None


def init_overtimes_services(salary_engine_instance):
    global _salary_engine
    _salary_engine = salary_engine_instance


def init_overtimes_line_service(line_service):
    global _line_service
    _line_service = line_service


MONTHLY_BASE_DAYS = 30  # 勞基法時薪計算基準日數（月薪 ÷ 30 ÷ 8）


# ============ Helper Functions ============


def _check_salary_month_not_finalized(
    session, employee_id: int, overtime_date: date
) -> None:
    """避免修改已封存月份的已核准加班，造成薪資與原始資料不一致。"""
    record = _get_finalized_salary_record(
        session, employee_id, overtime_date.year, overtime_date.month
    )
    if record:
        by = record.finalized_by or "系統"
        raise HTTPException(
            status_code=409,
            detail=(
                f"{overtime_date.year} 年 {overtime_date.month} 月薪資已封存（結算人：{by}），"
                "無法修改該月份的已核准加班。請先解除封存後再操作。"
            ),
        )


def _revoke_comp_leave_grant(session, ot: OvertimeRecord) -> None:
    """撤銷已發放的補休配額。

    1. 若有與此加班記錄明確關聯（source_overtime_id）的補休假單：
       - 已核准（已使用）→ 拋出 409，必須人工撤銷
       - 待審核 → 自動駁回，釋放配額占用
    2. 全域檢查：確認撤銷後配額不低於仍在使用/申請中的補休總量（含無關聯舊資料）
    """
    if not ot.use_comp_leave or not ot.comp_leave_granted:
        return

    year = ot.overtime_date.year
    quota = (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == ot.employee_id,
            LeaveQuota.year == year,
            LeaveQuota.leave_type == "compensatory",
        )
        .first()
    )

    if not quota:
        ot.comp_leave_granted = False
        logger.warning(
            "補休回滾時找不到配額紀錄：員工 ID=%d, %d 年（加班 #%d）",
            ot.employee_id,
            year,
            ot.id,
        )
        return

    # ── 步驟 1：處理有明確關聯的補休假單 ──────────────────────────────────
    linked_leaves = (
        session.query(LeaveRecord)
        .filter(
            LeaveRecord.source_overtime_id == ot.id,
        )
        .all()
    )

    linked_approved = [lv for lv in linked_leaves if lv.is_approved is True]
    linked_pending = [lv for lv in linked_leaves if lv.is_approved is None]

    if linked_approved:
        approved_h = sum(lv.leave_hours for lv in linked_approved)
        ids = ", ".join(str(lv.id) for lv in linked_approved)
        raise HTTPException(
            status_code=409,
            detail=(
                f"此筆加班已發放的補休有 {approved_h:.1f} 小時已核准使用"
                f"（假單 ID：{ids}），請先撤銷相關補休假單後再操作"
            ),
        )

    # 自動駁回待審核的關聯補休假單
    for lv in linked_pending:
        lv.is_approved = False
        lv.rejection_reason = (
            f"來源加班申請（#{ot.id}，{ot.overtime_date}）已被撤銷，補休資格取消"
        )
        logger.info(
            "補休假單 #%d 因來源加班 #%d 被撤銷而自動駁回（員工 ID=%d）",
            lv.id,
            ot.id,
            ot.employee_id,
        )

    # ── 步驟 2：全域 committed 檢查（含舊資料或其他來源的補休） ─────────────
    # autoflush 確保上方的狀態變更在下方查詢前已寫入 session，
    # 避免自動駁回的假單仍被計入 pending。
    new_total = float(quota.total_hours or 0) - float(ot.hours or 0)

    approved_committed = float(
        session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0))
        .filter(
            LeaveRecord.employee_id == ot.employee_id,
            LeaveRecord.leave_type == "compensatory",
            LeaveRecord.is_approved == True,
            LeaveRecord.start_date >= date(year, 1, 1),
            LeaveRecord.start_date < date(year + 1, 1, 1),
        )
        .scalar()
    )
    pending_committed = float(
        session.query(func.coalesce(func.sum(LeaveRecord.leave_hours), 0))
        .filter(
            LeaveRecord.employee_id == ot.employee_id,
            LeaveRecord.leave_type == "compensatory",
            LeaveRecord.is_approved.is_(None),
            LeaveRecord.start_date >= date(year, 1, 1),
            LeaveRecord.start_date < date(year + 1, 1, 1),
        )
        .scalar()
    )
    committed = approved_committed + pending_committed

    if new_total + 1e-9 < committed:
        raise HTTPException(
            status_code=409,
            detail=(
                f"此筆加班已發放的補休 {ot.hours:.1f} 小時已有 {committed:.1f} 小時被使用或申請，"
                "請先撤銷相關補休假單後再修改、刪除或駁回此加班申請"
            ),
        )

    quota.total_hours = max(0.0, new_total)
    ot.comp_leave_granted = False


def _recalculate_salary_for_overtime_months(
    employee_id: int, months: set[tuple[int, int]]
) -> None:
    """重算受影響月份的薪資。"""
    if _salary_engine is None:
        return
    for year, month in sorted(months):
        _salary_engine.process_salary_calculation(employee_id, year, month)


def _grant_comp_leave_quota(session, ot: OvertimeRecord, result: dict) -> None:
    """核准補休模式加班時，upsert 補休配額並標記 comp_leave_granted。

    防止重複發放：只在 ot.comp_leave_granted 為 False 時才執行。
    發放後將小時數寫入 result['comp_leave_hours_granted'] 供 API 回傳。
    """
    if not (ot.use_comp_leave and not ot.comp_leave_granted):
        return
    year = ot.overtime_date.year
    quota = (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == ot.employee_id,
            LeaveQuota.year == year,
            LeaveQuota.leave_type == "compensatory",
        )
        .with_for_update()
        .first()
    )
    if quota:
        quota.total_hours += ot.hours
    else:
        quota = LeaveQuota(
            employee_id=ot.employee_id,
            year=year,
            leave_type="compensatory",
            total_hours=ot.hours,
            note="由加班補休累積",
        )
        session.add(quota)
    ot.comp_leave_granted = True
    result["comp_leave_hours_granted"] = ot.hours
    logger.info(
        "補休配額已發放：員工 ID=%d, %d 年度 +%.1f 小時（加班記錄 #%d）",
        ot.employee_id,
        year,
        ot.hours,
        ot.id,
    )


def _notify_and_recalc_overtime(
    session, ot: OvertimeRecord, approved: bool, was_approved: bool, result: dict
) -> None:
    """核准/駁回後的 LINE 推播與薪資重算。

    LINE 通知失敗不影響主流程（僅記錄 warning）。
    薪資重算失敗則在 result 中寫入 warning 訊息，由 API 回傳給前端。
    """
    # 個人 LINE 推播（審核結果）
    if _line_service is not None:
        try:
            emp_user = (
                session.query(User).filter(User.employee_id == ot.employee_id).first()
            )
            if emp_user and emp_user.line_user_id:
                emp = (
                    session.query(Employee)
                    .filter(Employee.id == ot.employee_id)
                    .first()
                )
                emp_name = emp.name if emp else "員工"
                ot_type_label = OVERTIME_TYPE_LABELS.get(
                    ot.overtime_type, ot.overtime_type
                )
                _line_service.notify_overtime_result(
                    emp_user.line_user_id,
                    emp_name,
                    ot.overtime_date,
                    ot_type_label,
                    approved,
                )
        except Exception as _le:
            logger.warning("加班審核 LINE 推播失敗: %s", _le)

    # 核准或撤銷已核准狀態後都需要重算薪資
    if (approved or was_approved) and _salary_engine is not None:
        try:
            _salary_engine.process_salary_calculation(
                ot.employee_id, ot.overtime_date.year, ot.overtime_date.month
            )
            result["salary_recalculated"] = True
            result["message"] = (
                "已核准，薪資已自動重算" if approved else "已駁回，薪資已自動重算"
            )
            logger.info(
                "加班審核後自動重算薪資：emp_id=%d, %d/%d",
                ot.employee_id,
                ot.overtime_date.year,
                ot.overtime_date.month,
            )
        except Exception as e:
            result["salary_recalculated"] = False
            result["warning"] = (
                "已核准，但薪資重算失敗，請手動前往薪資頁面重新計算"
                if approved
                else "已駁回，但薪資重算失敗，請手動前往薪資頁面重新計算"
            )
            logger.error("加班審核後薪資重算失敗：%s", e)
            try:
                _mark_salary_stale(
                    session,
                    ot.employee_id,
                    ot.overtime_date.year,
                    ot.overtime_date.month,
                )
                session.commit()
            except Exception:
                logger.warning(
                    "加班審核降級時標記 SalaryRecord stale 失敗", exc_info=True
                )
                session.rollback()


def _to_time(val) -> dt_time:
    """str / datetime.time / datetime.datetime 統一正規化為 datetime.time。

    DB 欄位依設定不同可能回傳 datetime.time（Time 欄位）或 datetime.datetime（DateTime 欄位）；
    外部輸入則為 'HH:MM' 字串。直接混型比較（str < time、datetime < time 等）會
    觸發 TypeError，本函式確保任何輸入都能安全轉換為可比較的 datetime.time。
    """
    if isinstance(val, str):
        h, m = map(int, val.strip().split(":")[:2])
        return dt_time(h, m)
    if isinstance(val, datetime):  # datetime 是 date 的子類別，必須在 date 之前檢查
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


def calculate_overtime_pay(
    base_salary: float, hours: float, overtime_type: str
) -> float:
    """依勞基法計算加班費（時薪 = 月薪 ÷ 30 ÷ 8）"""
    if not base_salary or base_salary <= 0:
        raise HTTPException(
            status_code=400,
            detail="該員工底薪未設定或為 0，無法計算加班費，請先完成薪資設定。",
        )
    # 防禦縱深：即使前端驗證被繞過，也不允許負數或零時數計算
    if hours <= 0:
        return 0.0
    hours = min(hours, MAX_OVERTIME_HOURS)
    hourly_base = base_salary / MONTHLY_BASE_DAYS / DAILY_WORK_HOURS

    if overtime_type == "weekday":
        # 平日：前2h × 1.34，超過 × 1.67
        if hours <= WEEKDAY_THRESHOLD_HOURS:
            return round(hourly_base * hours * WEEKDAY_FIRST_2H_RATE)
        return round(
            hourly_base * WEEKDAY_THRESHOLD_HOURS * WEEKDAY_FIRST_2H_RATE
            + hourly_base * (hours - WEEKDAY_THRESHOLD_HOURS) * WEEKDAY_AFTER_2H_RATE
        )
    elif overtime_type == "weekend":
        # 休息日：最低計 2h，前2h × 1.33，3~8h × 1.67，超8h × 2.67
        billable = max(hours, RESTDAY_MIN_HOURS)
        if billable <= RESTDAY_FIRST_SEGMENT:
            return round(hourly_base * billable * RESTDAY_FIRST_2H_RATE)
        elif billable <= RESTDAY_SECOND_SEGMENT:
            return round(
                hourly_base * RESTDAY_FIRST_SEGMENT * RESTDAY_FIRST_2H_RATE
                + hourly_base * (billable - RESTDAY_FIRST_SEGMENT) * RESTDAY_MID_RATE
            )
        return round(
            hourly_base * RESTDAY_FIRST_SEGMENT * RESTDAY_FIRST_2H_RATE
            + hourly_base
            * (RESTDAY_SECOND_SEGMENT - RESTDAY_FIRST_SEGMENT)
            * RESTDAY_MID_RATE
            + hourly_base * (billable - RESTDAY_SECOND_SEGMENT) * RESTDAY_AFTER_8H_RATE
        )
    else:
        # 例假日 / 國定假日：全部 × 2.0
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

    # 若新申請缺少時間，同日任何記錄均視為重疊（維持原邏輯）
    if start_time is None or end_time is None:
        return q.first()

    # 有明確時間：DB 端排除「確定不重疊」的記錄
    # 保留：既有記錄缺少時間（無法比對，視為重疊），或時間區間重疊
    q = q.filter(
        or_(
            OvertimeRecord.start_time.is_(None),
            OvertimeRecord.end_time.is_(None),
            and_(
                OvertimeRecord.start_time < end_time,
                OvertimeRecord.end_time > start_time,
            ),
        )
    )
    return q.first()


def _assert_within_monthly_cap(
    existing_hours: float, new_hours: float, year: int, month: int
) -> None:
    """純函式：驗證既存 + 新加班時數不超過勞基法第 32 條第 2 項 46h/月上限。"""
    existing = float(existing_hours or 0)
    new = float(new_hours or 0)
    total = existing + new
    if total > MAX_MONTHLY_OVERTIME_HOURS + 1e-9:
        raise HTTPException(
            status_code=400,
            detail=(
                f"該員工 {year}/{month} 已申請加班 {existing:.1f} 小時，"
                f"加上此筆 {new:.1f} 小時合計 {total:.1f} 小時，"
                f"超過勞基法第 32 條每月延長工時上限 {MAX_MONTHLY_OVERTIME_HOURS:.0f} 小時。"
            ),
        )


def _validate_overtime_type_matches_calendar(
    overtime_type: str, is_statutory_holiday: bool
) -> None:
    """純函式：overtime_type 與該日是否為國定假日需一致（勞基法第 37 條）。

    - "holiday" 但日期非國定假日 → 400（防止溢付）
    - "weekday"/"weekend" 但日期為國定假日 → 400（防止短付違反第 37 條）
    """
    if overtime_type == "holiday" and not is_statutory_holiday:
        raise HTTPException(
            status_code=400,
            detail="該日期不在國定假日清單，請改用 weekday 或 weekend",
        )
    if overtime_type in ("weekday", "weekend") and is_statutory_holiday:
        raise HTTPException(
            status_code=400,
            detail=(
                "該日期為國定假日，加班類型須為 holiday 以加倍發給工資"
                "（勞基法第 37 條）"
            ),
        )


def _check_overtime_type_calendar(
    session, target_date: date, overtime_type: str
) -> None:
    """查詢 Holiday 表後呼叫純函式驗證。"""
    is_holiday = (
        session.query(Holiday)
        .filter(Holiday.date == target_date, Holiday.is_active == True)
        .first()
        is not None
    )
    _validate_overtime_type_matches_calendar(overtime_type, is_holiday)


def _check_monthly_overtime_cap(
    session,
    employee_id: int,
    target_date: date,
    new_hours: float,
    exclude_id: int = None,
) -> None:
    """查詢員工指定月份已申請（待審+已核准）加班時數，加上新時數後驗證不超過月上限。

    已駁回的申請不計入（釋放時數額度）。
    """
    year, month = target_date.year, target_date.month
    _, last_day = cal_module.monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)
    q = session.query(func.coalesce(func.sum(OvertimeRecord.hours), 0)).filter(
        OvertimeRecord.employee_id == employee_id,
        OvertimeRecord.overtime_date >= start,
        OvertimeRecord.overtime_date <= end,
        or_(
            OvertimeRecord.is_approved.is_(None),
            OvertimeRecord.is_approved == True,
        ),
    )
    if exclude_id is not None:
        q = q.filter(OvertimeRecord.id != exclude_id)
    existing = float(q.scalar() or 0)
    _assert_within_monthly_cap(existing, new_hours, year, month)


# ============ Pydantic Models ============


class OvertimeCreate(BaseModel):
    employee_id: int
    overtime_date: date
    overtime_type: str  # weekday / weekend / holiday
    start_time: Optional[str] = None  # HH:MM
    end_time: Optional[str] = None  # HH:MM
    hours: float
    reason: Optional[str] = None
    use_comp_leave: bool = False  # 以補休代替加班費

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

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_time_format(cls, v):
        return validate_hhmm_format(v)

    @model_validator(mode="after")
    def validate_time_order(self):
        if self.start_time and self.end_time:
            if self.start_time >= self.end_time:
                raise ValueError("start_time 必須早於 end_time（不支援跨日加班）")
        return self


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

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_time_format(cls, v):
        return validate_hhmm_format(v)

    @model_validator(mode="after")
    def validate_time_order(self):
        if self.start_time and self.end_time:
            if self.start_time >= self.end_time:
                raise ValueError("start_time 必須早於 end_time（不支援跨日加班）")
        return self


# ============ Batch Approve Request Model ============


class OvertimeBatchApproveRequest(BaseModel):
    ids: List[int]
    approved: bool
    rejection_reason: Optional[str] = None


# ============ Excel Helpers (local) ============


_OT_HEADER_FONT = Font(bold=True, size=11, color="FFFFFF")
_OT_HEADER_FILL = PatternFill(
    start_color="4472C4", end_color="4472C4", fill_type="solid"
)
_OT_THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)
_OT_CENTER_ALIGN = Alignment(horizontal="center")


def _ot_write_header(ws, row, headers):
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=col, value=h)
        cell.font = _OT_HEADER_FONT
        cell.fill = _OT_HEADER_FILL
        cell.border = _OT_THIN_BORDER
        cell.alignment = _OT_CENTER_ALIGN


# ============ Routes ============


@router.get("/overtimes")
def get_overtimes(
    employee_id: Optional[int] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    status: Optional[str] = None,  # pending, approved, rejected
    current_user: dict = Depends(require_staff_permission(Permission.OVERTIME_READ)),
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
            q = q.filter(
                OvertimeRecord.overtime_date >= start,
                OvertimeRecord.overtime_date <= end,
            )
        elif year:
            q = q.filter(
                OvertimeRecord.overtime_date >= date(year, 1, 1),
                OvertimeRecord.overtime_date <= date(year, 12, 31),
            )

        if status == "pending":
            q = q.filter(OvertimeRecord.is_approved.is_(None))
        elif status == "approved":
            q = q.filter(OvertimeRecord.is_approved == True)
        elif status == "rejected":
            q = q.filter(OvertimeRecord.is_approved == False)

        records = q.order_by(OvertimeRecord.overtime_date.desc()).all()

        # 預先載入員工角色映射
        employee_ids = list({ot.employee_id for ot, _ in records})
        user_roles = {}
        if employee_ids:
            users = (
                session.query(User)
                .filter(
                    User.employee_id.in_(employee_ids),
                    User.is_active == True,
                )
                .all()
            )
            user_roles = {u.employee_id: u.role for u in users}

        results = []
        for ot, emp in records:
            results.append(
                {
                    "id": ot.id,
                    "employee_id": ot.employee_id,
                    "employee_name": emp.name,
                    "submitter_role": user_roles.get(ot.employee_id, "teacher"),
                    "overtime_date": ot.overtime_date.isoformat(),
                    "overtime_type": ot.overtime_type,
                    "overtime_type_label": OVERTIME_TYPE_LABELS.get(
                        ot.overtime_type, ot.overtime_type
                    ),
                    "start_time": (
                        ot.start_time.strftime("%H:%M") if ot.start_time else None
                    ),
                    "end_time": ot.end_time.strftime("%H:%M") if ot.end_time else None,
                    "hours": ot.hours,
                    "overtime_pay": ot.overtime_pay,
                    "use_comp_leave": ot.use_comp_leave,
                    "comp_leave_granted": ot.comp_leave_granted,
                    "is_approved": ot.is_approved,
                    "approved_by": ot.approved_by,
                    "reason": ot.reason,
                    "created_at": ot.created_at.isoformat() if ot.created_at else None,
                }
            )
        return results
    finally:
        session.close()


@router.post("/overtimes", status_code=201)
def create_overtime(
    data: OvertimeCreate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.OVERTIME_WRITE)),
):
    """新增加班記錄（自動計算加班費）"""
    session = get_session()
    try:
        emp = session.query(Employee).filter(Employee.id == data.employee_id).first()
        if not emp:
            raise HTTPException(status_code=404, detail=EMPLOYEE_DOES_NOT_EXIST)

        pay = (
            0.0
            if data.use_comp_leave
            else calculate_overtime_pay(emp.base_salary, data.hours, data.overtime_type)
        )

        start_dt = None
        end_dt = None
        if data.start_time:
            h, m = map(int, data.start_time.split(":"))
            start_dt = datetime.combine(
                data.overtime_date, datetime.min.time().replace(hour=h, minute=m)
            )
        if data.end_time:
            h, m = map(int, data.end_time.split(":"))
            end_dt = datetime.combine(
                data.overtime_date, datetime.min.time().replace(hour=h, minute=m)
            )

        overlap = _check_overtime_overlap(
            session, data.employee_id, data.overtime_date, start_dt, end_dt
        )
        if overlap:
            st = (
                overlap.start_time.strftime("%H:%M") if overlap.start_time else "未指定"
            )
            et = overlap.end_time.strftime("%H:%M") if overlap.end_time else "未指定"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"該員工在 {overlap.overtime_date} 已有時間重疊的加班申請"
                    f"（ID: {overlap.id}，{st}～{et}），請勿重複申請"
                ),
            )

        _check_monthly_overtime_cap(
            session, data.employee_id, data.overtime_date, data.hours
        )

        _check_overtime_type_calendar(session, data.overtime_date, data.overtime_type)

        ot = OvertimeRecord(
            employee_id=data.employee_id,
            overtime_date=data.overtime_date,
            overtime_type=data.overtime_type,
            start_time=start_dt,
            end_time=end_dt,
            hours=data.hours,
            overtime_pay=pay,
            use_comp_leave=data.use_comp_leave,
            reason=data.reason,
            is_approved=None,  # Explicitly set to Pending
        )
        session.add(ot)
        session.commit()

        request.state.audit_entity_id = str(ot.id)
        request.state.audit_summary = (
            f"管理端建立加班：employee_id={data.employee_id} "
            f"{data.overtime_type} {data.overtime_date}（{data.hours}h）"
        )
        request.state.audit_changes = {
            "action": "overtime_create",
            "overtime_id": ot.id,
            "employee_id": data.employee_id,
            "overtime_date": data.overtime_date.isoformat(),
            "overtime_type": data.overtime_type,
            "hours": data.hours,
            "start_time": data.start_time,
            "end_time": data.end_time,
            "use_comp_leave": data.use_comp_leave,
            "overtime_pay": pay,
            "reason": data.reason,
        }
        return {"message": "加班記錄已新增", "id": ot.id, "overtime_pay": pay}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.put("/overtimes/{overtime_id}")
def update_overtime(
    overtime_id: int,
    data: OvertimeUpdate,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.OVERTIME_WRITE)),
):
    """更新加班記錄。若記錄已核准，修改後自動退回「待審核」狀態以符合稽核要求。"""
    session = get_session()
    try:
        ot = (
            session.query(OvertimeRecord)
            .filter(OvertimeRecord.id == overtime_id)
            .first()
        )
        if not ot:
            raise HTTPException(status_code=404, detail=OVERTIME_RECORD_NOT_FOUND)

        # 記錄修改前的核准狀態（供後續稽核退審判斷）+ before snapshot 供 audit_changes
        was_approved = ot.is_approved == True
        original_month = (ot.overtime_date.year, ot.overtime_date.month)
        before_snapshot = {
            "overtime_date": ot.overtime_date.isoformat(),
            "overtime_type": getattr(ot, "overtime_type", None),
            "hours": getattr(ot, "hours", None),
            "start_time": (
                str(ot.start_time) if getattr(ot, "start_time", None) else None
            ),
            "end_time": str(ot.end_time) if getattr(ot, "end_time", None) else None,
            "use_comp_leave": getattr(ot, "use_comp_leave", None),
            "overtime_pay": getattr(ot, "overtime_pay", None),
            "is_approved": ot.is_approved,
            "reason": getattr(ot, "reason", None),
        }

        # 先計算更新後的日期與時間（供重疊檢查使用）
        check_date = data.overtime_date or ot.overtime_date
        date_changed = (
            data.overtime_date is not None and data.overtime_date != ot.overtime_date
        )

        # 改日期但沒重新指定時間時，須以「新日期 + 舊時間」重組 datetime，
        # 否則 datetime 會留在舊日期，造成 overtime_date 與 start/end 日期欄不一致，
        # 且 _check_overtime_overlap 在 SQL 端會用舊日期 datetime 做比較而誤判。
        if data.start_time:
            h, m = map(int, data.start_time.split(":"))
            new_start_dt = datetime.combine(
                check_date, datetime.min.time().replace(hour=h, minute=m)
            )
        elif date_changed and ot.start_time is not None:
            new_start_dt = datetime.combine(check_date, ot.start_time.time())
        else:
            new_start_dt = ot.start_time
        if data.end_time:
            h, m = map(int, data.end_time.split(":"))
            new_end_dt = datetime.combine(
                check_date, datetime.min.time().replace(hour=h, minute=m)
            )
        elif date_changed and ot.end_time is not None:
            new_end_dt = datetime.combine(check_date, ot.end_time.time())
        else:
            new_end_dt = ot.end_time

        if (
            new_start_dt is not None
            and new_end_dt is not None
            and new_start_dt >= new_end_dt
        ):
            raise HTTPException(
                status_code=400,
                detail="start_time 必須早於 end_time（不支援跨日加班）",
            )

        overlap = _check_overtime_overlap(
            session,
            ot.employee_id,
            check_date,
            new_start_dt,
            new_end_dt,
            exclude_id=overtime_id,
        )
        if overlap:
            st = (
                overlap.start_time.strftime("%H:%M") if overlap.start_time else "未指定"
            )
            et = overlap.end_time.strftime("%H:%M") if overlap.end_time else "未指定"
            raise HTTPException(
                status_code=409,
                detail=(
                    f"修改後的時段與已存在的加班申請重疊"
                    f"（ID: {overlap.id}，{overlap.overtime_date} {st}～{et}），請調整時段"
                ),
            )

        # 修改後的時數驗證月上限（排除自己）
        new_hours_val = data.hours if data.hours is not None else ot.hours
        _check_monthly_overtime_cap(
            session,
            ot.employee_id,
            check_date,
            new_hours_val,
            exclude_id=overtime_id,
        )

        new_type = (
            data.overtime_type if data.overtime_type is not None else ot.overtime_type
        )
        _check_overtime_type_calendar(session, check_date, new_type)

        recalculation_months = {original_month, (check_date.year, check_date.month)}
        if was_approved:
            for year, month in recalculation_months:
                _check_salary_month_not_finalized(
                    session, ot.employee_id, date(year, month, 1)
                )
            _revoke_comp_leave_grant(session, ot)

        update_data = data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            if value is not None and key not in ("start_time", "end_time"):
                setattr(ot, key, value)

        # 改日期或改時間都需同步寫回 ORM，避免 overtime_date 與 start/end 日期欄
        # 形成不一致狀態。
        if data.start_time or (date_changed and ot.start_time is not None):
            ot.start_time = new_start_dt
        if data.end_time or (date_changed and ot.end_time is not None):
            ot.end_time = new_end_dt

        # Recalculate pay（補休模式加班費固定為 0）
        emp = session.query(Employee).filter(Employee.id == ot.employee_id).first()
        if emp:
            ot.overtime_pay = (
                0.0
                if ot.use_comp_leave
                else calculate_overtime_pay(emp.base_salary, ot.hours, ot.overtime_type)
            )

        # ── 稽核退審：已核准的記錄被修改，自動退回待審核 ──────────────────────
        # 防止管理員靜默修改已核准加班時數，導致薪資異常（財務防呆）
        if was_approved:
            ot.is_approved = None
            ot.approved_by = None
            logger.warning(
                "稽核警告：已核准加班記錄 #%d（員工 ID=%d, %s）被管理員「%s」修改，"
                "已自動退回待審核狀態，需重新核准",
                overtime_id,
                ot.employee_id,
                ot.overtime_date,
                current_user.get("username", "unknown"),
            )

        session.commit()

        after_snapshot = {
            "overtime_date": ot.overtime_date.isoformat(),
            "overtime_type": getattr(ot, "overtime_type", None),
            "hours": getattr(ot, "hours", None),
            "start_time": (
                str(ot.start_time) if getattr(ot, "start_time", None) else None
            ),
            "end_time": str(ot.end_time) if getattr(ot, "end_time", None) else None,
            "use_comp_leave": getattr(ot, "use_comp_leave", None),
            "overtime_pay": getattr(ot, "overtime_pay", None),
            "is_approved": ot.is_approved,
            "reason": getattr(ot, "reason", None),
        }
        diff = {
            k: {"before": before_snapshot[k], "after": after_snapshot[k]}
            for k in before_snapshot
            if before_snapshot[k] != after_snapshot[k]
        }
        request.state.audit_entity_id = str(overtime_id)
        request.state.audit_summary = (
            f"管理端修改加班 #{overtime_id}（employee_id={ot.employee_id}）"
            + ("；自動退回待審" if was_approved else "")
        )
        request.state.audit_changes = {
            "action": "overtime_update",
            "overtime_id": overtime_id,
            "employee_id": ot.employee_id,
            "was_approved": was_approved,
            "reset_to_pending": was_approved,
            "diff": diff,
            "recalculation_months": [
                f"{y}-{m:02d}" for (y, m) in sorted(recalculation_months)
            ],
        }

        msg = "加班記錄已更新"
        if was_approved:
            msg += "；原核准狀態已自動退回「待審核」，請重新送審"
        result = {
            "message": msg,
            "overtime_pay": ot.overtime_pay,
            "reset_to_pending": was_approved,
        }
        if was_approved:
            try:
                _recalculate_salary_for_overtime_months(
                    ot.employee_id, recalculation_months
                )
                result["salary_recalculated"] = True
            except Exception as e:
                result["salary_recalculated"] = False
                result["warning"] = (
                    "加班記錄已更新，但薪資重算失敗，請手動前往薪資頁面重新計算"
                )
                logger.error("加班修改退審後薪資重算失敗：%s", e)
                try:
                    for year, month in sorted(recalculation_months):
                        _mark_salary_stale(session, ot.employee_id, year, month)
                    session.commit()
                except Exception:
                    logger.warning("加班修改退審降級時標記 stale 失敗", exc_info=True)
                    session.rollback()
        return result
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.delete("/overtimes/{overtime_id}")
def delete_overtime(
    overtime_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.OVERTIME_WRITE)),
):
    """刪除加班記錄"""
    session = get_session()
    try:
        ot = (
            session.query(OvertimeRecord)
            .filter(OvertimeRecord.id == overtime_id)
            .first()
        )
        if not ot:
            raise HTTPException(status_code=404, detail=OVERTIME_RECORD_NOT_FOUND)
        was_approved = ot.is_approved is True
        overtime_month = (ot.overtime_date.year, ot.overtime_date.month)
        employee_id = ot.employee_id
        # 預先 snapshot：刪除後 ot 物件被 expunge，audit 必須在這裡備份
        deleted_snapshot = {
            "overtime_id": overtime_id,
            "employee_id": employee_id,
            "overtime_date": ot.overtime_date.isoformat(),
            "overtime_type": getattr(ot, "overtime_type", None),
            "hours": getattr(ot, "hours", None),
            "use_comp_leave": getattr(ot, "use_comp_leave", None),
            "overtime_pay": getattr(ot, "overtime_pay", None),
            "is_approved": ot.is_approved,
            "reason": getattr(ot, "reason", None),
        }
        if was_approved:
            _check_salary_month_not_finalized(session, employee_id, ot.overtime_date)
            _revoke_comp_leave_grant(session, ot)
        session.delete(ot)
        session.commit()

        request.state.audit_entity_id = str(overtime_id)
        request.state.audit_summary = (
            f"管理端刪除加班 #{overtime_id}（employee_id={employee_id}）"
            + ("；已核准 → 觸發補休/薪資重算" if was_approved else "")
        )
        request.state.audit_changes = {
            "action": "overtime_delete",
            "deleted": deleted_snapshot,
            "was_approved": was_approved,
            "overtime_month": f"{overtime_month[0]}-{overtime_month[1]:02d}",
            "triggered_salary_recalc": was_approved,
        }

        result = {"message": "加班記錄已刪除"}
        if was_approved:
            try:
                _recalculate_salary_for_overtime_months(employee_id, {overtime_month})
                result["salary_recalculated"] = True
            except Exception as e:
                result["salary_recalculated"] = False
                result["warning"] = (
                    "加班記錄已刪除，但薪資重算失敗，請手動前往薪資頁面重新計算"
                )
                logger.error("刪除加班後薪資重算失敗：%s", e)
                try:
                    _mark_salary_stale(
                        session, employee_id, overtime_month[0], overtime_month[1]
                    )
                    session.commit()
                except Exception:
                    logger.warning("刪除加班降級時標記 stale 失敗", exc_info=True)
                    session.rollback()
        return result
    finally:
        session.close()


@router.put("/overtimes/{overtime_id}/approve")
def approve_overtime(
    overtime_id: int,
    request: Request,
    approved: bool = True,
    approved_by: str = "管理員",
    current_user: dict = Depends(require_staff_permission(Permission.OVERTIME_WRITE)),
):
    """核准/駁回加班；核准後自動重算該員工當月薪資，補休模式核准後自動累積配額"""
    session = get_session()
    try:
        # with_for_update() 鎖定加班記錄，防止並發核准觸發補休配額重複發放（Race Condition）
        ot = (
            session.query(OvertimeRecord)
            .filter(OvertimeRecord.id == overtime_id)
            .with_for_update()
            .first()
        )
        if not ot:
            raise HTTPException(status_code=404, detail=OVERTIME_RECORD_NOT_FOUND)
        was_approved = ot.is_approved is True

        # ── 自我核准防護 ─────────────────────────────────────────────────────────
        # 僅在 approver 確實擁有 employee_id 且與申請人相同時才拒絕。
        # 無 employee_id 的帳號（如純管理員）本身無法提出加班單，不構成自我核准風險。
        approver_eid = current_user.get("employee_id")
        if approver_eid and ot.employee_id == approver_eid:
            raise HTTPException(status_code=403, detail="不可自我核准加班單")

        # ── 角色資格檢查 ──────────────────────────────────────────────────────
        submitter_role = _get_submitter_role(ot.employee_id, session)
        approver_role = current_user.get("role", "")
        if not _check_approval_eligibility(
            "overtime", submitter_role, approver_role, session
        ):
            raise HTTPException(
                status_code=403,
                detail=f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的加班申請",
            )

        if approved or was_approved:
            # 提早取得薪資鎖,讓「封存守衛 → commit overtime → recalc」三步在
            # 同一鎖窗內完成,避免 finalize 在 commit 與 recalc 之間搶先封存舊薪資。
            # 與 leaves approve 同樣模式。
            from utils.advisory_lock import (
                acquire_salary_lock as _acquire_salary_lock,
            )

            _acquire_salary_lock(
                session,
                employee_id=ot.employee_id,
                year=ot.overtime_date.year,
                month=ot.overtime_date.month,
            )
            _check_salary_month_not_finalized(session, ot.employee_id, ot.overtime_date)
        if not approved and was_approved:
            _revoke_comp_leave_grant(session, ot)

        # ── 核准最後一致性驗證 ───────────────────────────────────────────────
        # 建立路徑（管理端 create、portal、import）都有這些檢查，但舊資料、import
        # 過去版本、或直接改 DB 的紀錄可能違規。核准會進薪資，必須在最後守門。
        if approved and not was_approved:
            if ot.start_time and ot.end_time and ot.start_time >= ot.end_time:
                raise HTTPException(
                    status_code=400,
                    detail="start_time 必須早於 end_time（不支援跨日加班）",
                )
            overlap = _check_overtime_overlap(
                session,
                ot.employee_id,
                ot.overtime_date,
                ot.start_time,
                ot.end_time,
                exclude_id=overtime_id,
            )
            if overlap:
                st = (
                    overlap.start_time.strftime("%H:%M")
                    if overlap.start_time
                    else "未指定"
                )
                et = (
                    overlap.end_time.strftime("%H:%M") if overlap.end_time else "未指定"
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"該員工在 {overlap.overtime_date} 已有時間重疊的加班申請"
                        f"（ID: {overlap.id}，{st}～{et}），請先處理衝突記錄再核准"
                    ),
                )
            _check_monthly_overtime_cap(
                session,
                ot.employee_id,
                ot.overtime_date,
                ot.hours,
                exclude_id=overtime_id,
            )
            _check_overtime_type_calendar(session, ot.overtime_date, ot.overtime_type)

        ot.is_approved = approved
        ot.approved_by = current_user.get("username", approved_by) if approved else None

        result = {"message": "已核准" if approved else "已駁回"}

        # 補休配額發放（核准時才執行，且防止重複發放）
        if approved:
            _grant_comp_leave_quota(session, ot, result)

        action = "approved" if approved else "rejected"
        approval_log_row = _write_approval_log(
            "overtime", overtime_id, action, current_user, None, session
        )
        session.commit()

        # 高風險：已核准 → 駁回 顯式標記，AuditLogView 可篩
        is_reject_of_approved = was_approved and not approved
        risk_tags = []
        if is_reject_of_approved:
            risk_tags.append("reject_of_approved")
        request.state.audit_entity_id = str(overtime_id)
        request.state.audit_summary = (
            f"{'核准' if approved else '駁回'}加班 #{overtime_id}"
            f"（employee_id={ot.employee_id}）"
            + (f"｜⚠ {','.join(risk_tags)}" if risk_tags else "")
        )
        request.state.audit_changes = {
            "action": "overtime_approve",
            "overtime_id": overtime_id,
            "employee_id": ot.employee_id,
            "decision": "approved" if approved else "rejected",
            "before": {"is_approved": (True if was_approved else None)},
            "after": {
                "is_approved": ot.is_approved,
                "approved_by": ot.approved_by,
            },
            "approval_log_id": approval_log_row.id if approval_log_row else None,
            "is_reject_of_approved": is_reject_of_approved,
            "use_comp_leave": getattr(ot, "use_comp_leave", None),
            "hours": getattr(ot, "hours", None),
            "overtime_pay": getattr(ot, "overtime_pay", None),
            "comp_leave_granted": getattr(ot, "comp_leave_granted", None),
            "risk_tags": risk_tags,
        }

        _notify_and_recalc_overtime(session, ot, approved, was_approved, result)

        return result
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/overtimes/batch-approve")
def batch_approve_overtimes(
    data: OvertimeBatchApproveRequest,
    request: Request,
    _rl=Depends(_batch_approve_limiter),
    current_user: dict = Depends(require_staff_permission(Permission.OVERTIME_WRITE)),
):
    """批次核准/駁回加班。兩階段原子提交：先全部驗證，再統一 commit。"""
    succeeded = []
    failed = []

    session = get_session()
    try:
        # ── 預先批次載入，避免 N+1 ────────────────────────────────────────
        ot_map = {
            ot.id: ot
            for ot in session.query(OvertimeRecord)
            .filter(OvertimeRecord.id.in_(data.ids))
            .with_for_update()
            .all()
        }
        emp_ids = {ot.employee_id for ot in ot_map.values()}
        submitter_role_map: dict[int, str] = (
            {
                u.employee_id: u.role
                for u in session.query(User.employee_id, User.role)
                .filter(User.employee_id.in_(emp_ids), User.is_active == True)
                .all()
            }
            if emp_ids
            else {}
        )
        approver_role = current_user.get("role", "")
        _eligibility_cache: dict[str, bool] = {}

        # ── Phase 1：驗證 + 準備所有異動（不 commit）────────────────────────
        changes = []  # list of (ot_id, ot, was_approved)
        for ot_id in data.ids:
            try:
                ot = ot_map.get(ot_id)
                if not ot:
                    failed.append({"id": ot_id, "reason": OVERTIME_RECORD_NOT_FOUND})
                    continue
                was_approved = ot.is_approved is True

                # 防止自我核准
                approver_eid = current_user.get("employee_id")
                if approver_eid and ot.employee_id == approver_eid:
                    failed.append({"id": ot_id, "reason": "不可自我核准"})
                    continue

                submitter_role = submitter_role_map.get(ot.employee_id, "teacher")
                if submitter_role not in _eligibility_cache:
                    _eligibility_cache[submitter_role] = _check_approval_eligibility(
                        "overtime", submitter_role, approver_role, session
                    )
                if not _eligibility_cache[submitter_role]:
                    failed.append(
                        {
                            "id": ot_id,
                            "reason": f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的加班申請",
                        }
                    )
                    continue

                if data.approved or was_approved:
                    _check_salary_month_not_finalized(
                        session, ot.employee_id, ot.overtime_date
                    )
                if not data.approved and was_approved:
                    _revoke_comp_leave_grant(session, ot)

                # 核准最後一致性驗證，防止舊資料 / import 舊版遺留壞紀錄進入薪資
                if data.approved and not was_approved:
                    if ot.start_time and ot.end_time and ot.start_time >= ot.end_time:
                        raise HTTPException(
                            status_code=400,
                            detail="start_time 必須早於 end_time（不支援跨日加班）",
                        )
                    overlap = _check_overtime_overlap(
                        session,
                        ot.employee_id,
                        ot.overtime_date,
                        ot.start_time,
                        ot.end_time,
                        exclude_id=ot_id,
                    )
                    if overlap:
                        st = (
                            overlap.start_time.strftime("%H:%M")
                            if overlap.start_time
                            else "未指定"
                        )
                        et = (
                            overlap.end_time.strftime("%H:%M")
                            if overlap.end_time
                            else "未指定"
                        )
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"該員工在 {overlap.overtime_date} 已有時間重疊的加班申請"
                                f"（ID: {overlap.id}，{st}～{et}）"
                            ),
                        )
                    _check_monthly_overtime_cap(
                        session,
                        ot.employee_id,
                        ot.overtime_date,
                        ot.hours,
                        exclude_id=ot_id,
                    )
                    _check_overtime_type_calendar(
                        session, ot.overtime_date, ot.overtime_type
                    )

                ot.is_approved = data.approved
                ot.approved_by = (
                    current_user.get("username", "管理員") if data.approved else None
                )

                if data.approved:
                    # 使用共用 helper 統一配額發放邏輯（含 with_for_update() 列鎖），
                    # 避免批次與單筆核准在補休配額累積的 race condition。
                    _grant_comp_leave_quota(session, ot, {})

                action = "approved" if data.approved else "rejected"
                approval_log_row = _write_approval_log(
                    "overtime", ot_id, action, current_user, None, session
                )
                is_reject_of_approved = was_approved and not data.approved
                changes.append(
                    (
                        ot_id,
                        ot,
                        was_approved,
                        is_reject_of_approved,
                        approval_log_row.id if approval_log_row else None,
                    )
                )
            except Exception as e:
                session.rollback()
                failed.append({"id": ot_id, "reason": str(e)})
                # 驗證階段失敗需清除 session 狀態，重新載入後續記錄
                session.expire_all()

        # ── Phase 2：所有驗證通過後統一 commit ──────────────────────────────
        if changes:
            try:
                session.commit()
                for ot_id, ot, was_approved, _ir, _alog in changes:
                    succeeded.append(ot_id)
                    if (data.approved or was_approved) and _salary_engine is not None:
                        try:
                            _salary_engine.process_salary_calculation(
                                ot.employee_id,
                                ot.overtime_date.year,
                                ot.overtime_date.month,
                            )
                        except Exception as se:
                            logger.error(
                                "批次審核後薪資重算失敗（加班 #%d）：%s", ot_id, se
                            )
                            try:
                                _mark_salary_stale(
                                    session,
                                    ot.employee_id,
                                    ot.overtime_date.year,
                                    ot.overtime_date.month,
                                )
                                session.commit()
                            except Exception:
                                logger.warning(
                                    "批次審核降級時標記 stale 失敗（加班 #%d）",
                                    ot_id,
                                    exc_info=True,
                                )
                                session.rollback()
            except Exception as e:
                session.rollback()
                for ot_id, *_ in changes:
                    failed.append({"id": ot_id, "reason": f"統一提交失敗：{e}"})

        # AuditLog changes：批次操作彙整成單筆 audit 摘要
        request.state.audit_summary = (
            f"批次{'核准' if data.approved else '駁回'}加班 "
            f"成功 {len(succeeded)}/失敗 {len(failed)}（請求 {len(data.ids)} 筆）"
        )
        request.state.audit_changes = {
            "action": "overtime_batch_approve",
            "decision": "approved" if data.approved else "rejected",
            "requested_ids": data.ids,
            "succeeded_ids": list(succeeded),
            "failed": failed,
            "approval_log_ids": [
                {
                    "overtime_id": _id,
                    "employee_id": _ot.employee_id,
                    "is_reject_of_approved": _ir,
                    "approval_log_id": _alog,
                }
                for _id, _ot, _wa, _ir, _alog in changes
            ],
            "high_risk_count": sum(1 for _id, _ot, _wa, _ir, _alog in changes if _ir),
        }
    finally:
        session.close()

    return {"succeeded": succeeded, "failed": failed}


@router.get("/overtimes/import-template")
def get_overtime_import_template(
    current_user: dict = Depends(require_staff_permission(Permission.OVERTIME_WRITE)),
):
    """下載加班批次匯入 Excel 範本"""
    wb = Workbook()
    ws = SafeWorksheet(wb.active)
    ws.title = "加班匯入範本"

    headers = [
        "員工編號",
        "員工姓名",
        "加班日期",
        "加班類型",
        "時數",
        "開始時間(可空)",
        "結束時間(可空)",
        "原因(可空)",
        "補休(是/否,可空)",
    ]
    _ot_write_header(ws, 1, headers)

    ws.cell(row=2, column=1, value="E001")
    ws.cell(row=2, column=2, value="王小明")
    ws.cell(row=2, column=3, value="2026-03-15")
    ws.cell(row=2, column=4, value="weekday")
    ws.cell(row=2, column=5, value=2)
    ws.cell(row=2, column=6, value="18:00")
    ws.cell(row=2, column=7, value="20:00")
    ws.cell(row=2, column=8, value="開學準備")
    ws.cell(row=2, column=9, value="否")

    ws2 = SafeWorksheet(wb.create_sheet("加班類型說明"))
    ws2.cell(row=1, column=1, value="類型代碼")
    ws2.cell(row=1, column=2, value="說明")
    ws2.cell(row=1, column=3, value="加班費倍率")
    ws2.cell(row=2, column=1, value="weekday")
    ws2.cell(row=2, column=2, value="平日加班")
    ws2.cell(row=2, column=3, value="前2h×1.34，後2h×1.67")
    ws2.cell(row=3, column=1, value="weekend")
    ws2.cell(row=3, column=2, value="假日加班")
    ws2.cell(row=3, column=3, value="×2.0")
    ws2.cell(row=4, column=1, value="holiday")
    ws2.cell(row=4, column=2, value="國定假日加班")
    ws2.cell(row=4, column=3, value="×2.0")

    return xlsx_streaming_response(wb, "加班匯入範本.xlsx")


@router.post("/overtimes/import")
async def import_overtimes(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_staff_permission(Permission.OVERTIME_WRITE)),
):
    """批次匯入加班申請（建立草稿加班單，is_approved=None，需後續人工審核）"""
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="檔案超過 10MB 限制")
    validate_file_signature(content, ".xlsx")
    try:
        df = pd.read_excel(BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"無法解析 Excel 檔案：{e}")

    results: dict = {"total": 0, "created": 0, "failed": 0, "errors": []}
    session = get_session()
    try:
        emp_by_id, emp_by_name = build_employee_lookup(session)

        for idx, row in df.iterrows():
            results["total"] += 1
            row_num = int(idx) + 2
            try:
                emp = resolve_employee_from_row(row, emp_by_id, emp_by_name)

                ot_date_raw = row.get("加班日期")
                if ot_date_raw is None or pd.isna(ot_date_raw):
                    raise ValueError("加班日期不得為空")
                try:
                    overtime_date = pd.to_datetime(ot_date_raw).date()
                except Exception:
                    raise ValueError("加班日期格式錯誤，建議使用 YYYY-MM-DD")

                ot_type_raw = str(row.get("加班類型", "")).strip()
                if ot_type_raw not in OVERTIME_TYPE_LABELS:
                    raise ValueError(
                        f"無效的加班類型：{ot_type_raw}（可用：weekday/weekend/holiday）"
                    )

                hours_raw = row.get("時數")
                if hours_raw is None or pd.isna(hours_raw):
                    raise ValueError("時數不得為空")
                hours = float(hours_raw)
                if hours <= 0:
                    raise ValueError("時數必須大於 0")
                if hours > MAX_OVERTIME_HOURS:
                    raise ValueError(f"時數不得超過 {MAX_OVERTIME_HOURS} 小時")

                start_dt = None
                end_dt = None
                for col_name, is_start in [
                    ("開始時間(可空)", True),
                    ("結束時間(可空)", False),
                ]:
                    raw_val = row.get(col_name)
                    if raw_val is not None and not pd.isna(raw_val):
                        val_str = str(raw_val).strip()
                        if val_str and val_str not in ("nan", ""):
                            try:
                                h, m = map(int, val_str.split(":")[:2])
                                if not (0 <= h <= 23 and 0 <= m <= 59):
                                    raise ValueError(
                                        f"{col_name} 時間值超出範圍（小時 0-23，分鐘 0-59）"
                                    )
                                dt = datetime.combine(
                                    overtime_date,
                                    datetime.min.time().replace(hour=h, minute=m),
                                )
                                if is_start:
                                    start_dt = dt
                                else:
                                    end_dt = dt
                            except ValueError:
                                raise
                            except Exception:
                                raise ValueError(f"{col_name} 格式錯誤，應為 HH:MM")

                if start_dt is not None and end_dt is not None and start_dt >= end_dt:
                    raise ValueError("開始時間必須早於結束時間（不支援跨日加班）")

                comp_raw = row.get("補休(是/否,可空)")
                use_comp_leave = False
                if comp_raw is not None and not pd.isna(comp_raw):
                    use_comp_leave = str(comp_raw).strip() in (
                        "是",
                        "yes",
                        "Yes",
                        "YES",
                        "true",
                        "True",
                        "1",
                    )

                pay = (
                    0.0
                    if use_comp_leave
                    else calculate_overtime_pay(emp.base_salary, hours, ot_type_raw)
                )

                overlap = _check_overtime_overlap(
                    session, emp.id, overtime_date, start_dt, end_dt
                )
                if overlap:
                    st = (
                        overlap.start_time.strftime("%H:%M")
                        if overlap.start_time
                        else "未指定"
                    )
                    et = (
                        overlap.end_time.strftime("%H:%M")
                        if overlap.end_time
                        else "未指定"
                    )
                    raise ValueError(
                        f"該員工在 {overlap.overtime_date} 已有時間重疊的加班申請"
                        f"（ID: {overlap.id}，{st}～{et}），請勿重複匯入"
                    )

                _check_monthly_overtime_cap(session, emp.id, overtime_date, hours)
                _check_overtime_type_calendar(session, overtime_date, ot_type_raw)

                reason_raw = row.get("原因(可空)")
                reason = (
                    str(reason_raw).strip()
                    if reason_raw is not None and not pd.isna(reason_raw)
                    else None
                )

                ot = OvertimeRecord(
                    employee_id=emp.id,
                    overtime_date=overtime_date,
                    overtime_type=ot_type_raw,
                    start_time=start_dt,
                    end_time=end_dt,
                    hours=hours,
                    overtime_pay=pay,
                    use_comp_leave=use_comp_leave,
                    reason=reason,
                    is_approved=None,
                )
                session.add(ot)
                session.flush()
                results["created"] += 1
            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"第 {row_num} 行: {str(e)}")

        session.commit()
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e, context="匯入失敗")
    finally:
        session.close()

    return results

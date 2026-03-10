"""
Overtime management router
"""

import logging
import calendar as cal_module
from datetime import date, datetime, time as dt_time
from io import BytesIO
from typing import Optional, List
from urllib.parse import quote

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import or_

from models.database import get_session, Employee, OvertimeRecord, LeaveQuota, User, ApprovalPolicy, ApprovalLog
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

def _get_submitter_role(employee_id: int, session) -> str:
    """查詢員工對應 User 帳號的角色，找不到預設 teacher"""
    user = session.query(User).filter(
        User.employee_id == employee_id,
        User.is_active == True,
    ).first()
    return user.role if user else "teacher"


def _check_approval_eligibility(doc_type: str, submitter_role: str, approver_role: str, session) -> bool:
    """查詢 ApprovalPolicy，確認 approver_role 是否有資格審核"""
    policy = session.query(ApprovalPolicy).filter(
        ApprovalPolicy.is_active == True,
        ApprovalPolicy.submitter_role == submitter_role,
        ApprovalPolicy.doc_type.in_([doc_type, "all"]),
    ).first()
    if not policy:
        return approver_role == "admin"
    return approver_role in [r.strip() for r in policy.approver_roles.split(",")]


def _write_approval_log(doc_type: str, doc_id: int, action: str, approver: dict, comment: str | None, session):
    """寫入簽核記錄"""
    session.add(ApprovalLog(
        doc_type=doc_type,
        doc_id=doc_id,
        action=action,
        approver_id=approver.get("id"),
        approver_username=approver.get("username", ""),
        approver_role=approver.get("role", ""),
        comment=comment,
    ))


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


# ============ Batch Approve Request Model ============

class OvertimeBatchApproveRequest(BaseModel):
    ids: List[int]
    approved: bool
    rejection_reason: Optional[str] = None


# ============ Excel Helpers (local) ============

def _ot_xlsx_response(wb, filename: str):
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    encoded = quote(filename)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


_OT_HEADER_FONT = Font(bold=True, size=11, color="FFFFFF")
_OT_HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
_OT_THIN_BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
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

        # 預先載入員工角色映射
        employee_ids = list({ot.employee_id for ot, _ in records})
        user_roles = {}
        if employee_ids:
            users = session.query(User).filter(
                User.employee_id.in_(employee_ids),
                User.is_active == True,
            ).all()
            user_roles = {u.employee_id: u.role for u in users}

        results = []
        for ot, emp in records:
            results.append({
                "id": ot.id,
                "employee_id": ot.employee_id,
                "employee_name": emp.name,
                "submitter_role": user_roles.get(ot.employee_id, "teacher"),
                "overtime_date": ot.overtime_date.isoformat(),
                "overtime_type": ot.overtime_type,
                "overtime_type_label": OVERTIME_TYPE_LABELS.get(ot.overtime_type, ot.overtime_type),
                "start_time": ot.start_time.strftime("%H:%M") if ot.start_time else None,
                "end_time": ot.end_time.strftime("%H:%M") if ot.end_time else None,
                "hours": ot.hours,
                "overtime_pay": ot.overtime_pay,
                "use_comp_leave": ot.use_comp_leave,
                "comp_leave_granted": ot.comp_leave_granted,
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

        pay = 0.0 if data.use_comp_leave else calculate_overtime_pay(emp.base_salary, data.hours, data.overtime_type)

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
            use_comp_leave=data.use_comp_leave,
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

        # Recalculate pay（補休模式加班費固定為 0）
        emp = session.query(Employee).filter(Employee.id == ot.employee_id).first()
        if emp:
            ot.overtime_pay = 0.0 if ot.use_comp_leave else calculate_overtime_pay(emp.base_salary, ot.hours, ot.overtime_type)

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
    """核准/駁回加班；核准後自動重算該員工當月薪資，補休模式核准後自動累積配額"""
    session = get_session()
    try:
        ot = session.query(OvertimeRecord).filter(OvertimeRecord.id == overtime_id).first()
        if not ot:
            raise HTTPException(status_code=404, detail="加班記錄不存在")

        # ── 角色資格檢查 ──────────────────────────────────────────────────────
        submitter_role = _get_submitter_role(ot.employee_id, session)
        approver_role = current_user.get("role", "")
        if not _check_approval_eligibility("overtime", submitter_role, approver_role, session):
            raise HTTPException(
                status_code=403,
                detail=f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的加班申請",
            )

        ot.is_approved = approved
        ot.approved_by = current_user.get("username", approved_by)

        result = {"message": "已核准" if approved else "已駁回"}

        # 補休配額發放（核准時才執行，且防止重複發放）
        if approved and ot.use_comp_leave and not ot.comp_leave_granted:
            year = ot.overtime_date.year
            quota = session.query(LeaveQuota).filter(
                LeaveQuota.employee_id == ot.employee_id,
                LeaveQuota.year == year,
                LeaveQuota.leave_type == "compensatory",
            ).first()
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
                ot.employee_id, year, ot.hours, overtime_id,
            )

        action = "approved" if approved else "rejected"
        _write_approval_log("overtime", overtime_id, action, current_user, None, session)
        session.commit()

        # 核准後自動重算該員工當月薪資（補休模式加班費為 0，仍可重算確保一致性）
        if approved and _salary_engine is not None:
            try:
                year = ot.overtime_date.year
                month = ot.overtime_date.month
                emp_id = ot.employee_id
                _salary_engine.process_salary_calculation(emp_id, year, month)
                result["salary_recalculated"] = True
                result["message"] = "已核准，薪資已自動重算"
                logger.info("加班核准後自動重算薪資：emp_id=%d, %d/%d", emp_id, year, month)
            except Exception as e:
                result["salary_recalculated"] = False
                result["warning"] = "已核准，但薪資重算失敗，請手動前往薪資頁面重新計算"
                logger.error("加班核准後薪資重算失敗：%s", e)

        return result
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


@router.post("/overtimes/batch-approve")
def batch_approve_overtimes(
    data: OvertimeBatchApproveRequest,
    current_user: dict = Depends(require_permission(Permission.OVERTIME_WRITE)),
):
    """批次核准/駁回加班。每筆獨立處理，補休配額只在逐筆成功後發放。"""
    succeeded = []
    failed = []

    for ot_id in data.ids:
        session = get_session()
        try:
            ot = session.query(OvertimeRecord).filter(OvertimeRecord.id == ot_id).first()
            if not ot:
                failed.append({"id": ot_id, "reason": "加班記錄不存在"})
                continue

            # 角色資格檢查（403 視為失敗條目，不中斷批次）
            submitter_role = _get_submitter_role(ot.employee_id, session)
            approver_role = current_user.get("role", "")
            if not _check_approval_eligibility("overtime", submitter_role, approver_role, session):
                failed.append({
                    "id": ot_id,
                    "reason": f"您的角色（{approver_role}）無權審核此員工（{submitter_role}）的加班申請",
                })
                continue

            ot.is_approved = data.approved
            ot.approved_by = current_user.get("username", "管理員") if data.approved else None

            if data.approved and ot.use_comp_leave and not ot.comp_leave_granted:
                year = ot.overtime_date.year
                quota = session.query(LeaveQuota).filter(
                    LeaveQuota.employee_id == ot.employee_id,
                    LeaveQuota.year == year,
                    LeaveQuota.leave_type == "compensatory",
                ).first()
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

            action = "approved" if data.approved else "rejected"
            _write_approval_log("overtime", ot_id, action, current_user, None, session)
            session.commit()

            if data.approved and _salary_engine is not None:
                try:
                    _salary_engine.process_salary_calculation(
                        ot.employee_id, ot.overtime_date.year, ot.overtime_date.month
                    )
                except Exception as se:
                    logger.error("批次審核後薪資重算失敗（加班 #%d）：%s", ot_id, se)

            succeeded.append(ot_id)
        except Exception as e:
            session.rollback()
            failed.append({"id": ot_id, "reason": str(e)})
        finally:
            session.close()

    return {"succeeded": succeeded, "failed": failed}


@router.get("/overtimes/import-template")
def get_overtime_import_template(
    current_user: dict = Depends(require_permission(Permission.OVERTIME_WRITE)),
):
    """下載加班批次匯入 Excel 範本"""
    wb = Workbook()
    ws = wb.active
    ws.title = "加班匯入範本"

    headers = [
        "員工編號", "員工姓名", "加班日期", "加班類型",
        "時數", "開始時間(可空)", "結束時間(可空)", "原因(可空)", "補休(是/否,可空)",
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

    ws2 = wb.create_sheet("加班類型說明")
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

    return _ot_xlsx_response(wb, "加班匯入範本.xlsx")


@router.post("/overtimes/import")
async def import_overtimes(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_permission(Permission.OVERTIME_WRITE)),
):
    """批次匯入加班申請（建立草稿加班單，is_approved=None，需後續人工審核）"""
    content = await file.read()
    try:
        df = pd.read_excel(BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"無法解析 Excel 檔案：{e}")

    results: dict = {"total": 0, "created": 0, "failed": 0, "errors": []}
    session = get_session()
    try:
        employees = session.query(Employee).filter(Employee.is_active == True).all()
        emp_by_id = {str(e.employee_id): e for e in employees}
        emp_by_name = {e.name: e for e in employees}

        for idx, row in df.iterrows():
            results["total"] += 1
            row_num = int(idx) + 2
            try:
                emp_id_str = str(row.get("員工編號", "")).strip()
                emp_name_str = str(row.get("員工姓名", "")).strip()
                emp = None
                if emp_id_str and emp_id_str not in ("nan", ""):
                    emp = emp_by_id.get(emp_id_str)
                if emp is None and emp_name_str and emp_name_str not in ("nan", ""):
                    emp = emp_by_name.get(emp_name_str)
                if emp is None:
                    raise ValueError(f"找不到員工（編號:{emp_id_str}，姓名:{emp_name_str}）")

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
                for col_name, is_start in [("開始時間(可空)", True), ("結束時間(可空)", False)]:
                    raw_val = row.get(col_name)
                    if raw_val is not None and not pd.isna(raw_val):
                        val_str = str(raw_val).strip()
                        if val_str and val_str not in ("nan", ""):
                            try:
                                h, m = map(int, val_str.split(":")[:2])
                                dt = datetime.combine(
                                    overtime_date,
                                    datetime.min.time().replace(hour=h, minute=m),
                                )
                                if is_start:
                                    start_dt = dt
                                else:
                                    end_dt = dt
                            except Exception:
                                pass

                comp_raw = row.get("補休(是/否,可空)")
                use_comp_leave = False
                if comp_raw is not None and not pd.isna(comp_raw):
                    use_comp_leave = str(comp_raw).strip() in ("是", "yes", "Yes", "YES", "true", "True", "1")

                pay = 0.0 if use_comp_leave else calculate_overtime_pay(
                    emp.base_salary, hours, ot_type_raw
                )

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
        raise HTTPException(status_code=500, detail=f"匯入失敗：{e}")
    finally:
        session.close()

    return results

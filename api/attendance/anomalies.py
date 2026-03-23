"""
Attendance anomalies - admin batch view, batch confirm, and export endpoints.
"""

import io
import calendar as cal_module
import logging
from datetime import date, datetime
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from utils.errors import raise_safe_500
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from pydantic import BaseModel

from sqlalchemy import or_

from models.database import get_session, Employee, Attendance
from services.salary.utils import calc_daily_salary
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter()

# ============ Excel 樣式（複用 exports.py 的定義） ============

HEADER_FONT = Font(bold=True, size=11, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)
CENTER_ALIGN = Alignment(horizontal="center")

_FORMULA_PREFIXES = ('=', '+', '-', '@', '|')


def _sanitize_excel_value(value):
    if not isinstance(value, str):
        return value
    clean = value.lstrip('\t\r\n')
    if clean.startswith(_FORMULA_PREFIXES):
        return "'" + clean
    return clean


class SafeWorksheet:
    def __init__(self, ws):
        object.__setattr__(self, '_ws', ws)

    def cell(self, row, column, value=None):
        return self._ws.cell(row=row, column=column, value=_sanitize_excel_value(value))

    def __setitem__(self, key, value):
        self._ws[key].value = _sanitize_excel_value(value)

    def __getitem__(self, key):
        return self._ws[key]

    def __getattr__(self, name):
        return getattr(self._ws, name)

    def __setattr__(self, name, value):
        if name == '_ws':
            object.__setattr__(self, name, value)
        else:
            setattr(self._ws, name, value)


# ============ 確認動作標籤映射 ============

ACTION_LABELS = {
    "accept":       "接受扣款",
    "admin_accept": "接受扣款",
    "use_pto":      "特休抵銷",
    "dispute":      "申訴中",
    "admin_waive":  "管理員豁免",
}

WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]

# ============ Pydantic Models ============


class BatchConfirmRequest(BaseModel):
    attendance_ids: List[int]
    action: str  # "admin_accept" | "admin_waive"
    remark: Optional[str] = None


# ============ 共用輔助：建立異常列表 ============

def _build_anomaly_rows(session, year: int, month: int, status_filter: str):
    """查詢指定月份所有員工的異常記錄，回傳 list[dict]"""
    _, last_day = cal_module.monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)

    # 在 SQL 層過濾：只取有異常的記錄，大幅減少資料傳輸
    query = (
        session.query(Attendance, Employee)
        .join(Employee, Attendance.employee_id == Employee.id)
        .filter(
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
            Employee.is_active == True,
            or_(
                Attendance.is_late == True,
                Attendance.is_early_leave == True,
                Attendance.is_missing_punch_in == True,
                Attendance.is_missing_punch_out == True,
            ),
        )
    )

    # 狀態篩選也在 SQL 層完成
    if status_filter == "pending":
        query = query.filter(Attendance.confirmed_action == None)
    elif status_filter == "confirmed":
        query = query.filter(Attendance.confirmed_action != None)

    records = query.all()

    rows = []
    for att, emp in records:
        daily_salary = calc_daily_salary(emp.base_salary)

        items = []
        if att.is_late and att.late_minutes and att.late_minutes > 0:
            deduction = round(daily_salary / 8 / 60 * att.late_minutes)
            items.append({
                "type": "late",
                "type_label": "遲到",
                "detail": f"遲到 {att.late_minutes} 分鐘",
                "estimated_deduction": deduction,
            })
        if att.is_early_leave:
            items.append({
                "type": "early_leave",
                "type_label": "早退",
                "detail": "早退",
                "estimated_deduction": 50,
            })
        if att.is_missing_punch_in:
            items.append({
                "type": "missing_punch",
                "type_label": "未打卡(上班)",
                "detail": "上班未打卡（不扣款，僅記錄）",
                "estimated_deduction": 0,
            })
        if att.is_missing_punch_out:
            items.append({
                "type": "missing_punch",
                "type_label": "未打卡(下班)",
                "detail": "下班未打卡（不扣款，僅記錄）",
                "estimated_deduction": 0,
            })

        for item in items:
            rows.append({
                "id": att.id,
                "employee_name": emp.name,
                "employee_number": emp.employee_number or "",
                "date": att.attendance_date.isoformat(),
                "weekday": WEEKDAY_NAMES[att.attendance_date.weekday()],
                "confirmed_action": att.confirmed_action,
                "confirmed_by": att.confirmed_by,
                "confirmed_at": att.confirmed_at.isoformat() if att.confirmed_at else None,
                **item,
            })

    return rows


# ============ Endpoints ============

@router.get("/anomalies")
def get_attendance_anomalies(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    status: str = Query("all", pattern="^(all|pending|confirmed)$"),
    current_user: dict = Depends(require_permission(Permission.ATTENDANCE_READ)),
):
    """查詢月份異常清單（所有員工）"""
    session = get_session()
    try:
        rows = _build_anomaly_rows(session, year, month, status)
        total = len(rows)
        pending = sum(1 for r in rows if r["confirmed_action"] is None)
        return {
            "total": total,
            "pending": pending,
            "confirmed": total - pending,
            "items": rows,
        }
    finally:
        session.close()


@router.post("/anomalies/batch-confirm")
def batch_confirm_anomalies(
    data: BatchConfirmRequest,
    current_user: dict = Depends(require_permission(Permission.ATTENDANCE_WRITE)),
):
    """批次確認異常處理方式（管理員代確認）"""
    if data.action not in ("admin_accept", "admin_waive"):
        raise HTTPException(status_code=400, detail="無效的批次確認動作，允許值：admin_accept、admin_waive")

    operator = current_user.get("username", "admin")
    session = get_session()
    try:
        processed = 0
        now = datetime.now()
        att_map = {a.id: a for a in session.query(Attendance).filter(
            Attendance.id.in_(data.attendance_ids)
        ).all()}
        for att_id in data.attendance_ids:
            att = att_map.get(att_id)
            if not att:
                continue
            att.confirmed_action = data.action
            att.confirmed_by = operator
            att.confirmed_at = now
            if data.remark:
                att.remark = (att.remark or "") + f" [批次{ACTION_LABELS[data.action]}: {data.remark}]"
            processed += 1

        session.commit()
        logger.warning(
            "批次確認考勤異常：操作者=%s action=%s 筆數=%d",
            operator, data.action, processed,
        )
        return {"processed": processed}
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.get("/anomalies/export")
def export_attendance_anomalies(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    status: str = Query("all", pattern="^(all|pending|confirmed)$"),
    current_user: dict = Depends(require_permission(Permission.ATTENDANCE_READ)),
):
    """匯出考勤異常 Excel"""
    session = get_session()
    try:
        rows = _build_anomaly_rows(session, year, month, status)
    finally:
        session.close()

    wb = Workbook()
    ws_raw = wb.active
    ws = SafeWorksheet(ws_raw)
    ws.title = f"{year}年{month}月考勤異常"

    headers = [
        "員工編號", "姓名", "日期", "星期", "異常類型", "明細",
        "預估扣款", "確認狀態", "確認動作", "確認人員", "確認時間",
    ]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = THIN_BORDER
        cell.alignment = CENTER_ALIGN

    for row_idx, item in enumerate(rows, 2):
        is_confirmed = item["confirmed_action"] is not None
        action_label = ACTION_LABELS.get(item["confirmed_action"] or "", "") if is_confirmed else ""
        ws.cell(row=row_idx, column=1,  value=item["employee_number"])
        ws.cell(row=row_idx, column=2,  value=item["employee_name"])
        ws.cell(row=row_idx, column=3,  value=item["date"])
        ws.cell(row=row_idx, column=4,  value=item["weekday"])
        ws.cell(row=row_idx, column=5,  value=item["type_label"])
        ws.cell(row=row_idx, column=6,  value=item["detail"])
        ws.cell(row=row_idx, column=7,  value=item["estimated_deduction"])
        ws.cell(row=row_idx, column=8,  value="已處理" if is_confirmed else "待處理")
        ws.cell(row=row_idx, column=9,  value=action_label)
        ws.cell(row=row_idx, column=10, value=item["confirmed_by"] or "")
        ws.cell(row=row_idx, column=11, value=item["confirmed_at"] or "")

    # 自動欄寬
    for col in ws_raw.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=0)
        ws_raw.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"{year}年{month}月考勤異常.xlsx"
    encoded = quote(filename)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )

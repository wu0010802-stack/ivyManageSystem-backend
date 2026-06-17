"""考勤匯入預覽端點。

POST /attendance/upload/preview
- 解析 raw_text（TSV/CSV 含標題列）或直接接收 records
- 逐列分類：importable / employee_not_found / invalid_date / month_finalized / overwrite
- shift-aware 狀態估算（不依賴 DB 排班快取時走預設值）
- 唯讀：不寫入 Attendance 表
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from api.attendance._shared import MAX_IMPORT_ROWS, AttendanceCSVRow
from models.database import Attendance, Employee, get_session
from schemas.attendance_preview import (
    AttendancePreviewRequest,
    AttendancePreviewResult,
    PreviewRow,
    PreviewSummary,
)
from utils.approval_helpers import _get_finalized_salary_record
from utils.attendance_shift_window import compute_status_for_employee_date
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)
router = APIRouter()

# Header 別名對應到 AttendanceCSVRow 欄位
_HEADER_ALIASES: dict[str, str] = {
    "部門": "department",
    "編號": "employee_number",
    "員工編號": "employee_number",
    "姓名": "name",
    "日期": "date",
    "星期": "weekday",
    "上班時間": "punch_in",
    "上班": "punch_in",
    "下班時間": "punch_out",
    "下班": "punch_out",
}


def _parse_raw_text(raw: str) -> list[dict]:
    """將 TSV/CSV 原始字串解析為 dict list（第一列視為 header）。"""
    lines = [ln for ln in raw.replace("\r\n", "\n").split("\n") if ln.strip()]
    if len(lines) < 2:
        return []
    sep = "\t" if "\t" in lines[0] else ","
    headers = [_HEADER_ALIASES.get(h.strip(), h.strip()) for h in lines[0].split(sep)]
    out: list[dict] = []
    for ln in lines[1:]:
        cols = [c.strip() for c in ln.split(sep)]
        out.append({headers[i]: cols[i] for i in range(min(len(headers), len(cols)))})
    return out


def _norm_date(raw: str | None) -> str | None:
    """嘗試多種日期格式，成功回傳 ISO 字串，失敗回傳 None。"""
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).date().isoformat()
        except (ValueError, AttributeError):
            continue
    return None


@router.post("/upload/preview", response_model=AttendancePreviewResult)
def preview_attendance_upload(
    body: AttendancePreviewRequest,
    current_user: dict = Depends(require_staff_permission(Permission.ATTENDANCE_READ)),
):
    """考勤匯入預覽（唯讀）。

    接受 raw_text（貼上 CSV/TSV）或 records 二擇一；
    逐列分類並回傳 check 結果，不寫入任何考勤記錄。
    """
    if body.raw_text:
        raw_rows = _parse_raw_text(body.raw_text)
        if len(raw_rows) > MAX_IMPORT_ROWS:
            raise HTTPException(status_code=400, detail="資料列數超過上限")
    elif body.records:
        raw_rows = [r.model_dump() for r in body.records]
    else:
        raise HTTPException(status_code=400, detail="需提供 raw_text 或 records")

    session = get_session()
    try:
        # 以 employee_id（工號字串）為 key 建查詢 dict
        emps: dict[str, Employee] = {
            e.employee_id: e
            for e in session.query(Employee).filter(Employee.is_active.is_(True)).all()
        }

        rows: list[PreviewRow] = []
        normalized: list[AttendanceCSVRow] = []
        importable = problems = overwrites = 0

        for i, r in enumerate(raw_rows, start=1):
            num = (r.get("employee_number") or "").strip()
            name = (r.get("name") or "").strip()
            emp = emps.get(num)

            # 1. 找不到員工
            if emp is None:
                rows.append(
                    PreviewRow(
                        row_num=i,
                        employee_number=num,
                        employee_name=name,
                        check="employee_not_found",
                    )
                )
                problems += 1
                continue

            iso = _norm_date(r.get("date"))

            # 2. 日期無效
            if iso is None:
                rows.append(
                    PreviewRow(
                        row_num=i,
                        employee_number=num,
                        employee_name=emp.name,
                        date=r.get("date") or None,
                        check="invalid_date",
                    )
                )
                problems += 1
                continue

            d = datetime.fromisoformat(iso).date()
            pin_raw = (r.get("punch_in") or "").strip() or None
            pout_raw = (r.get("punch_out") or "").strip() or None

            # 3. 該月已封存
            if _get_finalized_salary_record(session, emp.id, d.year, d.month):
                rows.append(
                    PreviewRow(
                        row_num=i,
                        employee_number=num,
                        employee_name=emp.name,
                        date=iso,
                        punch_in=pin_raw,
                        punch_out=pout_raw,
                        check="month_finalized",
                    )
                )
                problems += 1
                continue

            # 解析打卡時間（格式 HH:MM；解析失敗保留 None，狀態估算走預設）
            pin_dt: datetime | None = None
            pout_dt: datetime | None = None
            try:
                if pin_raw:
                    pin_dt = datetime.combine(
                        d, datetime.strptime(pin_raw, "%H:%M").time()
                    )
            except ValueError:
                pass
            try:
                if pout_raw:
                    pout_dt = datetime.combine(
                        d, datetime.strptime(pout_raw, "%H:%M").time()
                    )
            except ValueError:
                pass

            # 跨夜班修正
            if pout_dt and pin_dt and pout_dt < pin_dt:
                pout_dt += timedelta(days=1)

            # shift-aware 狀態估算（空班別/排班 map 時走預設）
            _, _, _, _, status = compute_status_for_employee_date(
                emp,
                d,
                pin_dt,
                pout_dt,
                {},  # daily_shift_map（不查 DB，預估用）
                {},  # shift_schedule_map
                is_head_teacher=getattr(emp, "is_head_teacher", False),
                is_assistant=getattr(emp, "is_assistant", False),
            )

            # 4. 檢查是否已有記錄（將覆蓋）
            exists = (
                session.query(Attendance.id)
                .filter_by(employee_id=emp.id, attendance_date=d)
                .first()
                is not None
            )
            check = "overwrite" if exists else "importable"
            if check == "overwrite":
                overwrites += 1
            else:
                importable += 1

            rows.append(
                PreviewRow(
                    row_num=i,
                    employee_number=num,
                    employee_name=emp.name,
                    matched_employee_id=emp.id,
                    date=iso,
                    punch_in=pin_raw,
                    punch_out=pout_raw,
                    status=status,
                    check=check,
                )
            )
            normalized.append(
                AttendanceCSVRow(
                    department=r.get("department") or "",
                    employee_number=num,
                    name=emp.name,
                    date=iso,
                    weekday=r.get("weekday") or "",
                    punch_in=pin_raw,
                    punch_out=pout_raw,
                )
            )

        return AttendancePreviewResult(
            summary=PreviewSummary(
                importable=importable,
                problems=problems,
                overwrites=overwrites,
            ),
            rows=rows,
            normalized=normalized,
        )
    finally:
        session.close()

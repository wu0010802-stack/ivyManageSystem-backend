"""
Data export router - Excel downloads for employees, students, attendance, calendar
"""

import io
import logging
import calendar as cal_module
from datetime import date, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from utils.auth import require_permission
from utils.permissions import Permission
from utils.rate_limit import SlidingWindowLimiter
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment

from models.database import (
    get_session, Employee, Student, Attendance, Classroom, SchoolEvent, JobTitle,
    LeaveRecord, OvertimeRecord, Holiday,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/exports", tags=["exports"])

# 匯出端點限流：同一 IP 每分鐘最多 5 次（匯出屬於重資源操作，防 DoS 消耗）
_export_rate_limit = SlidingWindowLimiter(
    max_calls=5,
    window_seconds=60,
    name="export",
    error_detail="匯出過於頻繁，請稍後再試",
).as_dependency()

# ============ Shared Styles ============

HEADER_FONT = Font(bold=True, size=11, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
TITLE_FONT = Font(bold=True, size=14)
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)
CENTER_ALIGN = Alignment(horizontal="center")


def _write_header_row(ws, row, headers):
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=row, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = THIN_BORDER
        cell.alignment = CENTER_ALIGN


# Excel 公式觸發前綴：= + - @（試算表公式），| 可觸發 DDE 攻擊
_FORMULA_PREFIXES = ('=', '+', '-', '@', '|')


def _sanitize_excel_value(value):
    """防止 Excel 公式注入（Excel Injection / DDE 攻擊）。

    策略：
    1. 先去除開頭 Tab / CR / LF — 這些字元常被用來繞過前綴偵測
       （例如 '\\t=cmd...' 不以 '=' 開頭，可繞過只檢查第一字元的邏輯）
    2. 若清理後仍以危險前綴開頭，則在最前面加上單引號
       openpyxl 會將其儲存為純字串，Excel 開啟時不會執行公式
    """
    if not isinstance(value, str):
        return value
    clean = value.lstrip('\t\r\n')
    if clean.startswith(_FORMULA_PREFIXES):
        return "'" + clean
    return clean


def _write_data_row(ws, row, values):
    for col, value in enumerate(values, 1):
        sanitized_value = _sanitize_excel_value(value)
        cell = ws.cell(row=row, column=col, value=sanitized_value)
        cell.border = THIN_BORDER


def _auto_width(ws):
    from openpyxl.cell.cell import MergedCell
    for col in ws.columns:
        max_len = 0
        col_letter = None
        for cell in col:
            if isinstance(cell, MergedCell):
                continue
            if col_letter is None:
                col_letter = cell.column_letter
            if cell.value:
                length = len(str(cell.value))
                if length > max_len:
                    max_len = length
        if col_letter:
            ws.column_dimensions[col_letter].width = min(max(max_len + 4, 8), 40)


def _to_response(wb, filename):
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    encoded = quote(filename)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


def _id_name_map(session, model):
    """建立 {id: name} 對照表"""
    return {obj.id: obj.name for obj in session.query(model).all()}


# ============ Employees ============

@router.get("/employees")
def export_employees(
    _rl=Depends(_export_rate_limit),
    current_user: dict = Depends(require_permission(Permission.EMPLOYEES_READ)),
):
    """匯出員工名冊 Excel"""
    session = get_session()
    try:
        employees = session.query(Employee).order_by(Employee.employee_id).all()
        classrooms = _id_name_map(session, Classroom)
        job_titles = _id_name_map(session, JobTitle)

        wb = Workbook()
        ws = wb.active
        ws.title = "員工名冊"

        ws.merge_cells("A1:O1")
        ws["A1"] = "員工名冊"
        ws["A1"].font = TITLE_FONT
        ws["A1"].alignment = CENTER_ALIGN

        headers = [
            "工號", "姓名", "職稱", "職務", "員工類型", "所屬班級",
            "到職日期", "聯絡電話", "地址", "緊急聯絡人", "緊急聯絡人電話",
            "銀行代碼", "銀行帳號", "戶名", "在職狀態",
        ]
        _write_header_row(ws, 3, headers)

        for idx, emp in enumerate(employees, 4):
            jt = job_titles.get(emp.job_title_id, emp.title or "")
            cr = classrooms.get(emp.classroom_id, "")
            emp_type = "正職" if emp.employee_type == "regular" else "時薪"
            _write_data_row(ws, idx, [
                emp.employee_id, emp.name, jt, emp.position or "", emp_type, cr,
                emp.hire_date.isoformat() if emp.hire_date else "",
                emp.phone or "", emp.address or "",
                emp.emergency_contact_name or "", emp.emergency_contact_phone or "",
                emp.bank_code or "", emp.bank_account or "", emp.bank_account_name or "",
                "在職" if emp.is_active else "離職",
            ])

        _auto_width(ws)
        return _to_response(wb, "員工名冊.xlsx")
    finally:
        session.close()


# ============ Students ============

@router.get("/students")
def export_students(
    _rl=Depends(_export_rate_limit),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """匯出學生名冊 Excel"""
    session = get_session()
    try:
        students = session.query(Student).order_by(Student.student_id).all()
        classrooms = _id_name_map(session, Classroom)

        wb = Workbook()
        ws = wb.active
        ws.title = "學生名冊"

        ws.merge_cells("A1:K1")
        ws["A1"] = "學生名冊"
        ws["A1"].font = TITLE_FONT
        ws["A1"].alignment = CENTER_ALIGN

        headers = [
            "學號", "姓名", "性別", "生日", "班級", "入學日期",
            "家長姓名", "家長電話", "地址", "狀態標籤", "在籍狀態",
        ]
        _write_header_row(ws, 3, headers)

        gender_map = {"M": "男", "F": "女"}
        for idx, stu in enumerate(students, 4):
            cr = classrooms.get(stu.classroom_id, "")
            _write_data_row(ws, idx, [
                stu.student_id, stu.name,
                gender_map.get(stu.gender, stu.gender or ""),
                stu.birthday.isoformat() if stu.birthday else "",
                cr,
                stu.enrollment_date.isoformat() if stu.enrollment_date else "",
                stu.parent_name or "", stu.parent_phone or "",
                stu.address or "", stu.status_tag or "",
                "在籍" if stu.is_active else "離校",
            ])

        _auto_width(ws)
        return _to_response(wb, "學生名冊.xlsx")
    finally:
        session.close()


# ============ Attendance ============

@router.get("/attendance")
def export_attendance(
    _rl=Depends(_export_rate_limit),
    current_user: dict = Depends(require_permission(Permission.ATTENDANCE_READ)),
    year: int = Query(...),
    month: int = Query(...),
):
    """匯出出勤月報 Excel"""
    session = get_session()
    try:
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        employees = session.query(Employee).filter(Employee.is_active == True).order_by(Employee.employee_id).all()
        emp_map = {e.id: e for e in employees}

        # 1. 計算當月應出勤天數（排除週末與國定假日）
        holiday_dates = {
            h.date for h in session.query(Holiday).filter(
                Holiday.date >= start,
                Holiday.date <= end,
                Holiday.is_active.is_(True),
            ).all()
        }
        expected_workdays: set = set()
        cur = start
        while cur <= end:
            if cur.weekday() < 5 and cur not in holiday_dates:
                expected_workdays.add(cur)
            cur += timedelta(days=1)

        # 2. 已核准請假日期，用來區分「合法缺席」與「曠職」
        approved_leaves = session.query(LeaveRecord).filter(
            LeaveRecord.start_date <= end,
            LeaveRecord.end_date >= start,
            LeaveRecord.is_approved == True,
        ).all()
        leave_dates_by_emp: dict = {}
        for lv in approved_leaves:
            if lv.employee_id not in emp_map:
                continue
            d = max(lv.start_date, start)
            lv_end = min(lv.end_date, end)
            while d <= lv_end:
                leave_dates_by_emp.setdefault(lv.employee_id, set()).add(d)
                d += timedelta(days=1)

        # 3. 從打卡記錄彙整統計，同時記錄每位員工的實際出勤日期集合
        records = session.query(Attendance).filter(
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
        ).all()

        stats: dict = {}
        att_dates_by_emp: dict = {}
        for att in records:
            if att.employee_id not in emp_map:
                continue
            if att.employee_id not in stats:
                stats[att.employee_id] = {
                    "total": 0, "normal": 0, "late": 0, "early": 0,
                    "missing_in": 0, "missing_out": 0, "late_min": 0,
                }
                att_dates_by_emp[att.employee_id] = set()
            s = stats[att.employee_id]
            s["total"] += 1
            att_dates_by_emp[att.employee_id].add(att.attendance_date)
            if not att.is_late and not att.is_early_leave and not att.is_missing_punch_in and not att.is_missing_punch_out:
                s["normal"] += 1
            if att.is_late:
                s["late"] += 1
                s["late_min"] += att.late_minutes or 0
            if att.is_early_leave:
                s["early"] += 1
            if att.is_missing_punch_in:
                s["missing_in"] += 1
            if att.is_missing_punch_out:
                s["missing_out"] += 1

        wb = Workbook()
        ws = wb.active
        ws.title = f"{year}年{month}月出勤月報"

        ws.merge_cells("A1:L1")
        ws["A1"] = f"{year}年{month}月 出勤月報"
        ws["A1"].font = TITLE_FONT
        ws["A1"].alignment = CENTER_ALIGN

        headers = [
            "工號", "姓名", "應出勤天數", "實際出勤天數", "正常天數",
            "遲到次數", "早退次數", "缺卡(上班)", "缺卡(下班)", "遲到總分鐘",
            "請假天數", "曠職天數",
        ]
        _write_header_row(ws, 3, headers)

        row_idx = 4
        for emp in employees:
            s = stats.get(emp.id, {
                "total": 0, "normal": 0, "late": 0, "early": 0,
                "missing_in": 0, "missing_out": 0, "late_min": 0,
            })
            att_dates = att_dates_by_emp.get(emp.id, set())
            leave_dates = leave_dates_by_emp.get(emp.id, set())
            # 請假天數：已核准請假日 ∩ 應出勤日（排除本來就是假日的天）
            leave_days = len(leave_dates & expected_workdays)
            # 曠職天數：應出勤日 - 有打卡日 - 已核准請假日
            absent_days = len(expected_workdays - att_dates - leave_dates)
            _write_data_row(ws, row_idx, [
                emp.employee_id, emp.name,
                len(expected_workdays),
                s["total"], s["normal"], s["late"], s["early"],
                s["missing_in"], s["missing_out"], s["late_min"],
                leave_days, absent_days,
            ])
            row_idx += 1

        _auto_width(ws)
        return _to_response(wb, f"{year}年{month}月出勤月報.xlsx")
    finally:
        session.close()


# ============ Calendar ============

EVENT_TYPE_LABELS = {
    "meeting": "會議",
    "activity": "活動",
    "holiday": "假日",
    "general": "一般",
}


@router.get("/calendar")
def export_calendar(
    _rl=Depends(_export_rate_limit),
    current_user: dict = Depends(require_permission(Permission.CALENDAR)),
    year: int = Query(...),
    month: int = Query(...),
):
    """匯出行事曆 Excel"""
    session = get_session()
    try:
        from sqlalchemy import or_

        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        events = session.query(SchoolEvent).filter(
            SchoolEvent.is_active == True,
            SchoolEvent.event_date <= end,
            or_(
                SchoolEvent.end_date >= start,
                (SchoolEvent.end_date.is_(None)) & (SchoolEvent.event_date >= start),
            ),
        ).order_by(SchoolEvent.event_date).all()

        wb = Workbook()
        ws = wb.active
        ws.title = f"{year}年{month}月行事曆"

        ws.merge_cells("A1:G1")
        ws["A1"] = f"{year}年{month}月 學校行事曆"
        ws["A1"].font = TITLE_FONT
        ws["A1"].alignment = CENTER_ALIGN

        headers = ["日期", "結束日期", "類型", "標題", "說明", "時間", "地點"]
        _write_header_row(ws, 3, headers)

        for idx, ev in enumerate(events, 4):
            time_str = ""
            if ev.is_all_day:
                time_str = "全天"
            elif ev.start_time and ev.end_time:
                time_str = f"{ev.start_time} - {ev.end_time}"

            _write_data_row(ws, idx, [
                ev.event_date.isoformat(),
                ev.end_date.isoformat() if ev.end_date else "",
                EVENT_TYPE_LABELS.get(ev.event_type, ev.event_type),
                ev.title,
                ev.description or "",
                time_str,
                ev.location or "",
            ])

        _auto_width(ws)
        return _to_response(wb, f"{year}年{month}月行事曆.xlsx")
    finally:
        session.close()


# ============ Leaves ============

LEAVE_TYPE_LABELS = {
    "personal": "事假",
    "sick": "病假",
    "menstrual": "生理假",
    "annual": "特休",
    "maternity": "產假",
    "paternity": "陪產假",
    "official": "公假",
    "marriage": "婚假",
    "bereavement": "喪假",
    "prenatal": "產檢假",
    "paternity_new": "陪產檢及陪產假",
    "miscarriage": "流產假",
    "family_care": "家庭照顧假",
    "parental_unpaid": "育嬰留職停薪",
}


def _approval_label(is_approved):
    if is_approved is True:
        return "已核准"
    if is_approved is False:
        return "已駁回"
    return "待審核"


@router.get("/leaves")
def export_leaves(
    _rl=Depends(_export_rate_limit),
    current_user: dict = Depends(require_permission(Permission.LEAVES_READ)),
    year: int = Query(...),
    month: int = Query(...),
):
    """匯出請假記錄 Excel"""
    session = get_session()
    try:
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        leaves = (
            session.query(LeaveRecord)
            .filter(LeaveRecord.start_date <= end, LeaveRecord.end_date >= start)
            .order_by(LeaveRecord.start_date)
            .all()
        )
        emp_map = _id_name_map(session, Employee)

        wb = Workbook()
        ws = wb.active
        ws.title = f"{year}年{month}月請假記錄"

        ws.merge_cells("A1:H1")
        ws["A1"] = f"{year}年{month}月 請假記錄"
        ws["A1"].font = TITLE_FONT
        ws["A1"].alignment = CENTER_ALIGN

        headers = ["員工姓名", "假別", "開始日期", "結束日期", "時數", "扣薪比例", "原因", "審核狀態"]
        _write_header_row(ws, 3, headers)

        for idx, lv in enumerate(leaves, 4):
            _write_data_row(ws, idx, [
                emp_map.get(lv.employee_id, ""),
                LEAVE_TYPE_LABELS.get(lv.leave_type, lv.leave_type),
                lv.start_date.isoformat(),
                lv.end_date.isoformat(),
                lv.leave_hours or 8,
                lv.deduction_ratio,
                lv.reason or "",
                _approval_label(lv.is_approved),
            ])

        _auto_width(ws)
        return _to_response(wb, f"{year}年{month}月請假記錄.xlsx")
    finally:
        session.close()


# ============ Overtimes ============

OVERTIME_TYPE_LABELS = {
    "weekday": "平日",
    "weekend": "假日",
    "holiday": "國定假日",
}


@router.get("/overtimes")
def export_overtimes(
    _rl=Depends(_export_rate_limit),
    current_user: dict = Depends(require_permission(Permission.OVERTIME_READ)),
    year: int = Query(...),
    month: int = Query(...),
):
    """匯出加班記錄 Excel"""
    session = get_session()
    try:
        _, last_day = cal_module.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)

        overtimes = (
            session.query(OvertimeRecord)
            .filter(OvertimeRecord.overtime_date >= start, OvertimeRecord.overtime_date <= end)
            .order_by(OvertimeRecord.overtime_date)
            .all()
        )
        emp_map = _id_name_map(session, Employee)

        wb = Workbook()
        ws = wb.active
        ws.title = f"{year}年{month}月加班記錄"

        ws.merge_cells("A1:I1")
        ws["A1"] = f"{year}年{month}月 加班記錄"
        ws["A1"].font = TITLE_FONT
        ws["A1"].alignment = CENTER_ALIGN

        headers = ["員工姓名", "日期", "類型", "開始時間", "結束時間", "時數", "加班費", "原因", "審核狀態"]
        _write_header_row(ws, 3, headers)

        for idx, ot in enumerate(overtimes, 4):
            start_t = ot.start_time.strftime("%H:%M") if ot.start_time else ""
            end_t = ot.end_time.strftime("%H:%M") if ot.end_time else ""
            _write_data_row(ws, idx, [
                emp_map.get(ot.employee_id, ""),
                ot.overtime_date.isoformat(),
                OVERTIME_TYPE_LABELS.get(ot.overtime_type, ot.overtime_type),
                start_t,
                end_t,
                ot.hours or 0,
                round(ot.overtime_pay or 0),
                ot.reason or "",
                _approval_label(ot.is_approved),
            ])

        _auto_width(ws)
        return _to_response(wb, f"{year}年{month}月加班記錄.xlsx")
    finally:
        session.close()

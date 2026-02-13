"""
Data export router - Excel downloads for employees, students, attendance, calendar
"""

import io
import logging
import calendar as cal_module
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment

from models.database import (
    get_session, Employee, Student, Attendance, Classroom, SchoolEvent, JobTitle,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/exports", tags=["exports"])

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


def _write_data_row(ws, row, values):
    for col, value in enumerate(values, 1):
        cell = ws.cell(row=row, column=col, value=value)
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


# ============ Employees ============

@router.get("/employees")
def export_employees():
    """匯出員工名冊 Excel"""
    session = get_session()
    try:
        employees = session.query(Employee).order_by(Employee.employee_id).all()
        classrooms = {c.id: c.name for c in session.query(Classroom).all()}
        job_titles = {j.id: j.name for j in session.query(JobTitle).all()}

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
def export_students():
    """匯出學生名冊 Excel"""
    session = get_session()
    try:
        students = session.query(Student).order_by(Student.student_id).all()
        classrooms = {c.id: c.name for c in session.query(Classroom).all()}

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

        records = session.query(Attendance).filter(
            Attendance.attendance_date >= start,
            Attendance.attendance_date <= end,
        ).all()

        # Aggregate per employee
        stats = {}
        for att in records:
            if att.employee_id not in emp_map:
                continue
            if att.employee_id not in stats:
                stats[att.employee_id] = {
                    "total": 0, "normal": 0, "late": 0, "early": 0,
                    "missing_in": 0, "missing_out": 0, "late_min": 0,
                }
            s = stats[att.employee_id]
            s["total"] += 1
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

        ws.merge_cells("A1:I1")
        ws["A1"] = f"{year}年{month}月 出勤月報"
        ws["A1"].font = TITLE_FONT
        ws["A1"].alignment = CENTER_ALIGN

        headers = [
            "工號", "姓名", "出勤天數", "正常天數",
            "遲到次數", "早退次數", "缺卡(上班)", "缺卡(下班)", "遲到總分鐘",
        ]
        _write_header_row(ws, 3, headers)

        row_idx = 4
        for emp in employees:
            s = stats.get(emp.id, {
                "total": 0, "normal": 0, "late": 0, "early": 0,
                "missing_in": 0, "missing_out": 0, "late_min": 0,
            })
            _write_data_row(ws, row_idx, [
                emp.employee_id, emp.name,
                s["total"], s["normal"], s["late"], s["early"],
                s["missing_in"], s["missing_out"], s["late_min"],
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

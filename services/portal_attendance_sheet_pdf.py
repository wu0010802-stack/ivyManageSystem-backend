"""教師端月考勤表 PDF 產生器（A4 橫向，上下兩個半月表格）。

對應前端 views/portal/components/attendance/AttendanceTableView.vue。
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from utils.pdf_fonts import CJK_FONT_NAME, register_cjk_font


def _status_short(day: dict[str, Any]) -> str:
    """單格內顯示的狀態縮寫（請假類別 / 遲到分鐘 / 缺卡 / 加班）。"""
    if day.get("is_holiday") and day.get("holiday_name"):
        return day["holiday_name"][:3]
    if day.get("is_weekend"):
        return ""
    if day.get("leave_type_label"):
        return day["leave_type_label"]
    if day.get("is_late") and day.get("late_minutes"):
        return f"遲{day['late_minutes']}"
    if day.get("is_missing_punch_in") or day.get("is_missing_punch_out"):
        return "缺卡"
    if day.get("is_early_leave"):
        return "早退"
    if day.get("punch_in"):
        return "正常"
    return "—"


def _overtime_short(day: dict[str, Any]) -> str:
    ot_list = day.get("overtime_requests") or []
    if not ot_list:
        return ""
    ot = ot_list[0]
    hours = ot.get("hours")
    if hours is None:
        return "加"
    return f"{hours}h"


def _draw_half_month_table(
    c: canvas.Canvas,
    *,
    days: list[dict[str, Any]],
    top_y: float,
    width: float,
    margin_x: float,
    uses_shift: bool,
) -> float:
    """畫一張上半月或下半月表格；回傳底部 y 座標（供下一張表續用）。"""
    n_days = len(days)
    if n_days == 0:
        return top_y
    label_col_w = 18 * mm
    day_col_w = (width - label_col_w) / n_days

    # 列順序 (label, extractor)
    rows: list[tuple[str, Any]] = [
        ("日期", lambda d: str(d.get("day") or "")),
        ("星期", lambda d: d.get("weekday") or ""),
    ]
    if uses_shift:
        rows.append(("班表", lambda d: d.get("shift_name") or ""))
    rows.extend(
        [
            ("上班", lambda d: d.get("punch_in") or ""),
            ("下班", lambda d: d.get("punch_out") or ""),
            (
                "工時",
                lambda d: (
                    f"{d['work_hours']:.1f}" if d.get("work_hours") is not None else ""
                ),
            ),
            ("狀態", _status_short),
            ("加班", _overtime_short),
        ]
    )
    row_h = 6 * mm
    total_h = row_h * len(rows)

    # 外框
    c.setLineWidth(0.4)
    c.rect(margin_x, top_y - total_h, width, total_h, stroke=1, fill=0)

    # 直線
    c.line(margin_x + label_col_w, top_y - total_h, margin_x + label_col_w, top_y)
    for i in range(1, n_days + 1):
        x = margin_x + label_col_w + i * day_col_w
        c.line(x, top_y - total_h, x, top_y)

    # 橫線 + 內容
    c.setFont(CJK_FONT_NAME, 8)
    y = top_y
    for ri, (label, extractor) in enumerate(rows):
        y -= row_h
        if ri > 0:
            c.line(margin_x, y + row_h, margin_x + width, y + row_h)
        # label
        c.drawString(margin_x + 1.5 * mm, y + row_h / 2 - 1.3 * mm, label)
        # 第一列（日期）是 highlight
        if label == "日期":
            c.setFont(CJK_FONT_NAME, 9)
            for ci, d in enumerate(days):
                # 假日 / 週末標灰
                if d.get("is_weekend") or d.get("is_holiday"):
                    c.setFillColor(colors.HexColor("#eeeeee"))
                    c.rect(
                        margin_x + label_col_w + ci * day_col_w,
                        y,
                        day_col_w,
                        row_h,
                        stroke=0,
                        fill=1,
                    )
                    c.setFillColor(colors.black)
                txt = extractor(d)
                c.drawCentredString(
                    margin_x + label_col_w + ci * day_col_w + day_col_w / 2,
                    y + row_h / 2 - 1.3 * mm,
                    txt,
                )
            c.setFont(CJK_FONT_NAME, 8)
            continue

        for ci, d in enumerate(days):
            txt = extractor(d) if callable(extractor) else ""
            if not txt:
                continue
            cx = margin_x + label_col_w + ci * day_col_w + day_col_w / 2
            cy = y + row_h / 2 - 1.3 * mm
            # 太長則 truncate
            max_chars = max(2, int(day_col_w / mm / 1.6))
            if len(txt) > max_chars:
                txt = txt[: max_chars - 1] + "…"
            c.drawCentredString(cx, cy, txt)

    return top_y - total_h


def generate_portal_attendance_sheet_pdf(*, sheet: dict[str, Any]) -> bytes:
    """產生教師個人月考勤表 PDF；回傳 bytes。

    sheet 結構同 api/portal/attendance.py get_attendance_sheet 回傳。
    """
    register_cjk_font()

    buffer = io.BytesIO()
    page_size = landscape(A4)
    c = canvas.Canvas(buffer, pagesize=page_size)
    width, height = page_size

    margin_x = 10 * mm
    margin_top = 10 * mm
    content_w = width - 2 * margin_x

    employee_name = sheet.get("employee_name") or ""
    year = sheet.get("year") or ""
    month = sheet.get("month") or ""
    uses_shift = bool(sheet.get("uses_shift"))
    days = sheet.get("days") or []
    summary = sheet.get("summary") or {}

    # 標題
    title_y = height - margin_top - 6 * mm
    c.setFont(CJK_FONT_NAME, 16)
    c.drawString(margin_x, title_y, f"{employee_name}　{year} 年 {month} 月　月考勤表")
    c.setLineWidth(0.8)
    c.line(margin_x, title_y - 3 * mm, margin_x + content_w, title_y - 3 * mm)

    # 摘要列
    summary_y = title_y - 9 * mm
    c.setFont(CJK_FONT_NAME, 9)
    c.setFillColor(colors.HexColor("#555555"))
    stats = (
        f"出勤 {summary.get('total_work_days', 0)} 日　"
        f"請假 {summary.get('leave_count', 0)} 日　"
        f"遲到 {summary.get('late_count', 0)} 次　"
        f"早退 {summary.get('early_leave_count', 0)} 次　"
        f"缺卡 {summary.get('missing_punch_count', 0)} 次　"
        f"日均工時 {summary.get('avg_work_hours', 0)} 小時"
    )
    c.drawString(margin_x, summary_y, stats)
    c.setFillColor(colors.black)

    # 上半月 (1-15)
    upper_days = days[:15]
    lower_days = days[15:]

    top1 = summary_y - 7 * mm
    bottom1 = _draw_half_month_table(
        c,
        days=upper_days,
        top_y=top1,
        width=content_w,
        margin_x=margin_x,
        uses_shift=uses_shift,
    )

    if lower_days:
        top2 = bottom1 - 8 * mm
        _draw_half_month_table(
            c,
            days=lower_days,
            top_y=top2,
            width=content_w,
            margin_x=margin_x,
            uses_shift=uses_shift,
        )

    c.showPage()
    c.save()
    return buffer.getvalue()

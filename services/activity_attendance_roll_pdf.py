"""課後才藝點名單 PDF 產生器。

A4 直式：
- 標題列「才藝點名單」+ 園所名稱
- 場次資料（課程／日期／總人數／授課老師）
- 表格：#／班級／姓名／出席／缺席／請假／備註，至少 20 列
- 頁尾：統計欄、授課老師簽名、主管覆核、日期、列印時間
"""

from __future__ import annotations

import io
from datetime import datetime
from utils.taipei_time import now_taipei_naive
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from utils.pdf_fonts import CJK_FONT_NAME, register_cjk_font

_CJK_FONT = CJK_FONT_NAME

MIN_ROWS = 20


def _ensure_font() -> None:
    register_cjk_font()


def _fmt_session_date(iso: str | None) -> str:
    if not iso:
        return ""
    parts = iso.split("-")
    if len(parts) != 3:
        return iso
    y, m, d = parts
    try:
        return f"{int(y)} 年 {int(m)} 月 {int(d)} 日"
    except ValueError:
        return iso


def _sort_students(students: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        students or [],
        key=lambda s: ((s.get("class_name") or ""), (s.get("student_name") or "")),
    )


def _draw_checkbox(c: canvas.Canvas, x: float, y: float, *, filled: bool) -> None:
    """於座標 (x, y) 畫一個 4mm 見方的勾選框；filled=True 則填黑。"""
    size = 4 * mm
    c.setLineWidth(0.6)
    c.rect(x, y, size, size, stroke=1, fill=1 if filled else 0)


def generate_attendance_roll_pdf(
    *,
    session_data: dict[str, Any],
    institution_name: str = "常春藤幼兒園",
) -> bytes:
    """生成才藝點名單 PDF；回傳 bytes。

    session_data 期望結構同 api/activity/_shared.py 的 _build_session_detail_response：
      - course_name, session_date, notes, total
      - students: list[{student_name, class_name, is_present, attendance_notes}]
    """
    _ensure_font()

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    margin_x = 14 * mm
    content_w = width - 2 * margin_x

    course_name = session_data.get("course_name") or ""
    session_date_iso = session_data.get("session_date") or ""
    notes = session_data.get("notes") or ""
    total = int(session_data.get("total") or 0)
    students = _sort_students(session_data.get("students") or [])

    # ── 標題列 ────────────────────────────────────────────────────────────
    title_y = height - 18 * mm
    c.setFont(_CJK_FONT, 18)
    c.drawString(margin_x, title_y, "才藝點名單")
    c.setFont(_CJK_FONT, 11)
    c.drawRightString(margin_x + content_w, title_y, institution_name)
    c.setLineWidth(1.2)
    c.line(margin_x, title_y - 3 * mm, margin_x + content_w, title_y - 3 * mm)

    # ── Meta：課程／日期／總人數／授課老師 ──────────────────────────────
    meta_y = title_y - 11 * mm
    label_w = 18 * mm
    col_w = content_w / 2
    c.setFont(_CJK_FONT, 11)

    def _draw_meta(label: str, value: str, col_idx: int, row_idx: int) -> None:
        x = margin_x + col_idx * col_w
        y = meta_y - row_idx * 7 * mm
        c.setFillColor(colors.HexColor("#555555"))
        c.drawString(x, y, f"{label}：")
        c.setFillColor(colors.black)
        c.drawString(x + label_w, y, value)

    _draw_meta("課程", course_name, 0, 0)
    _draw_meta("日期", _fmt_session_date(session_date_iso), 1, 0)
    _draw_meta("總人數", f"{total} 人", 0, 1)
    # 授課老師留空白欄供手寫
    x = margin_x + 1 * col_w
    y = meta_y - 1 * 7 * mm
    c.setFillColor(colors.HexColor("#555555"))
    c.drawString(x, y, "授課老師：")
    c.setFillColor(colors.black)
    c.setLineWidth(0.5)
    c.line(x + label_w, y - 1, x + col_w - 5 * mm, y - 1)

    # ── 備註 ──────────────────────────────────────────────────────────────
    notes_y = meta_y - 2 * 7 * mm - 2 * mm
    if notes:
        c.setFont(_CJK_FONT, 10)
        c.setFillColor(colors.HexColor("#333333"))
        c.drawString(margin_x, notes_y, f"備註：{notes}")
        c.setFillColor(colors.black)
        notes_y -= 6 * mm

    # ── 表格 ──────────────────────────────────────────────────────────────
    table_top = notes_y - 4 * mm
    # 欄寬（單位 mm，總和需 = content_w）
    col_widths_mm = [
        10,
        22,
        35,
        14,
        14,
        14,
        content_w / mm - (10 + 22 + 35 + 14 + 14 + 14),
    ]
    headers = ["#", "班級", "姓名", "出席", "缺席", "請假", "備註"]
    row_h = 8 * mm
    header_h = 8 * mm

    # 計算各欄 x 起點
    col_x = [margin_x]
    for w_mm in col_widths_mm[:-1]:
        col_x.append(col_x[-1] + w_mm * mm)

    # 表頭背景灰底
    c.setFillColor(colors.HexColor("#e8e8e8"))
    c.rect(margin_x, table_top - header_h, content_w, header_h, stroke=0, fill=1)
    c.setFillColor(colors.black)

    # 畫表頭文字 + 直框線
    c.setFont(_CJK_FONT, 10)
    for i, label in enumerate(headers):
        cx = col_x[i] + (col_widths_mm[i] * mm) / 2
        cy = table_top - header_h / 2 - 1.5 * mm
        c.drawCentredString(cx, cy, label)

    # 計算 row 數：至少 MIN_ROWS
    row_count = max(MIN_ROWS, len(students))

    # 總表格框
    total_table_h = header_h + row_count * row_h
    c.setLineWidth(0.8)
    c.rect(
        margin_x, table_top - total_table_h, content_w, total_table_h, stroke=1, fill=0
    )

    # 橫線（表頭下 + 每列下緣）
    c.setLineWidth(0.4)
    y_line = table_top - header_h
    c.line(margin_x, y_line, margin_x + content_w, y_line)
    for r in range(1, row_count + 1):
        y_line = table_top - header_h - r * row_h
        c.line(margin_x, y_line, margin_x + content_w, y_line)

    # 直線（欄分隔）
    for i in range(1, len(col_widths_mm)):
        x_line = col_x[i]
        c.line(x_line, table_top - total_table_h, x_line, table_top)

    # 學生列內容
    c.setFont(_CJK_FONT, 10)
    for idx in range(row_count):
        row_top = table_top - header_h - idx * row_h
        row_center_y = row_top - row_h / 2 - 1.5 * mm
        is_pad = idx >= len(students)

        # # 序號
        c.setFillColor(colors.HexColor("#aaaaaa") if is_pad else colors.black)
        c.drawCentredString(
            col_x[0] + col_widths_mm[0] * mm / 2,
            row_center_y,
            str(idx + 1),
        )

        if is_pad:
            # 空白列只畫 checkbox（讓老師手寫加人）
            for ci in (3, 4, 5):
                box_x = col_x[ci] + col_widths_mm[ci] * mm / 2 - 2 * mm
                box_y = row_top - row_h / 2 - 2 * mm
                _draw_checkbox(c, box_x, box_y, filled=False)
            c.setFillColor(colors.black)
            continue

        c.setFillColor(colors.black)
        s = students[idx]
        # 班級
        c.drawCentredString(
            col_x[1] + col_widths_mm[1] * mm / 2,
            row_center_y,
            s.get("class_name") or "—",
        )
        # 姓名
        c.drawString(col_x[2] + 2 * mm, row_center_y, s.get("student_name") or "")

        # 出席 / 缺席 / 請假 三個 checkbox
        present_filled = s.get("is_present") is True
        absent_filled = s.get("is_present") is False
        for ci, filled in ((3, present_filled), (4, absent_filled), (5, False)):
            box_x = col_x[ci] + col_widths_mm[ci] * mm / 2 - 2 * mm
            box_y = row_top - row_h / 2 - 2 * mm
            _draw_checkbox(c, box_x, box_y, filled=filled)

        # 備註
        remark = (s.get("attendance_notes") or "").strip()
        if remark:
            # 過長截斷
            max_chars = 18
            if len(remark) > max_chars:
                remark = remark[: max_chars - 1] + "…"
            c.drawString(col_x[6] + 2 * mm, row_center_y, remark)

    # ── 頁尾統計 + 簽名 ───────────────────────────────────────────────────
    footer_y = table_top - total_table_h - 10 * mm
    c.setFont(_CJK_FONT, 10)
    c.drawString(margin_x, footer_y, "出席：____ 人")
    c.drawString(margin_x + 50 * mm, footer_y, "缺席：____ 人")
    c.drawString(margin_x + 100 * mm, footer_y, "請假：____ 人")
    c.setStrokeColor(colors.HexColor("#888888"))
    c.setLineWidth(0.3)
    c.setDash(2, 2)
    c.line(margin_x, footer_y - 3 * mm, margin_x + content_w, footer_y - 3 * mm)
    c.setDash()  # reset
    c.setStrokeColor(colors.black)

    sign_y = footer_y - 18 * mm
    block_w = content_w / 3
    c.setFillColor(colors.HexColor("#555555"))
    c.setFont(_CJK_FONT, 9)
    c.drawString(margin_x, sign_y + 7 * mm, "授課老師簽名")
    c.drawString(margin_x + block_w, sign_y + 7 * mm, "主管覆核")
    c.drawString(margin_x + 2 * block_w, sign_y + 7 * mm, "日期")
    c.setFillColor(colors.black)
    c.setLineWidth(0.6)
    c.line(margin_x, sign_y, margin_x + block_w - 8 * mm, sign_y)
    c.line(margin_x + block_w, sign_y, margin_x + 2 * block_w - 8 * mm, sign_y)
    c.line(margin_x + 2 * block_w, sign_y, margin_x + content_w, sign_y)

    # 列印時間（右下）
    c.setFont(_CJK_FONT, 8)
    c.setFillColor(colors.HexColor("#888888"))
    c.drawRightString(
        margin_x + content_w,
        15 * mm,
        f"列印時間：{now_taipei_naive().strftime('%Y-%m-%d %H:%M')}",
    )
    c.setFillColor(colors.black)

    c.showPage()
    c.save()
    return buffer.getvalue()

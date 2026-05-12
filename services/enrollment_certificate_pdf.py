"""在學證明書 PDF 產生器（Phase 4 sub-system C）。

A4 直式：
- 抬頭（園所名稱）
- 標題「在學證明書」
- 學生資料（姓名、學號、身分證、入園日期、目前班級）
- 申請用途、開立日期、序號、份數
- 章戳預留位置（園長章、園所章）
"""

from __future__ import annotations

import io
from datetime import date

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

_CJK_FONT = "STSong-Light"


def _ensure_font() -> None:
    try:
        pdfmetrics.getFont(_CJK_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))


def _fmt_roc(d: date) -> str:
    """民國年格式：YYYY-MM-DD → 民國 ROC.MM.DD"""
    return f"中華民國 {d.year - 1911} 年 {d.month} 月 {d.day} 日"


def generate_enrollment_cert_pdf(
    *,
    student_name: str,
    student_no: str,
    id_number: str | None,
    admit_date: date,
    classroom_name: str,
    purpose: str,
    issue_date: date,
    serial: str,
    copies: int = 1,
    institution_name: str = "義華幼兒園",
) -> bytes:
    """產生在學證明 PDF；回傳 bytes。"""
    _ensure_font()

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    c.setFont(_CJK_FONT, 16)
    c.drawCentredString(width / 2, height - 25 * mm, institution_name)

    c.setFont(_CJK_FONT, 22)
    c.drawCentredString(width / 2, height - 45 * mm, "在學證明書")

    c.setFont(_CJK_FONT, 11)
    c.drawString(150 * mm, height - 55 * mm, f"字號：{serial}")

    body_y = height - 80 * mm
    line_gap = 9 * mm
    c.setFont(_CJK_FONT, 12)
    rows = [
        ("學生姓名", student_name),
        ("學號", student_no),
        ("身分證字號", id_number or "（未提供）"),
        ("入園日期", _fmt_roc(admit_date)),
        ("目前班級", classroom_name),
        ("申請用途", purpose),
        ("開立日期", _fmt_roc(issue_date)),
        ("份數", f"{copies} 份"),
    ]
    for label, value in rows:
        c.drawString(35 * mm, body_y, f"{label}：")
        c.drawString(75 * mm, body_y, str(value))
        body_y -= line_gap

    body_y -= 15 * mm
    c.drawString(35 * mm, body_y, "茲證明上列學生確實於本園就讀，特此證明。")

    seal_y = 60 * mm
    c.setFont(_CJK_FONT, 10)
    c.drawString(40 * mm, seal_y, "（園長章）")
    c.drawString(120 * mm, seal_y, "（園所章）")
    c.rect(35 * mm, seal_y - 30 * mm, 35 * mm, 30 * mm)
    c.rect(115 * mm, seal_y - 30 * mm, 35 * mm, 30 * mm)

    c.showPage()
    c.save()
    return buffer.getvalue()

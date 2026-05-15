"""年終獎金列印 PDF 模板（M6）— reportlab，繁體中文。

提供三個 PDF 產出函式：
  generate_personal_bonus_slip_pdf  — 個人年終獎金條 (對應「New年終獎金條」)
  generate_transfer_roster_pdf      — 轉帳名冊 PDF (對應「轉帳名冊~P6」)
  generate_summary_table_pdf        — 年終獎金總表 PDF (對應「年終獎金總表」)

中文字型：使用 reportlab 內建 STSong-Light（PDF 預設 CJK），無需外部字型檔。
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from models.year_end import SpecialBonusType

logger = logging.getLogger(__name__)


CJK_FONT = "STSong-Light"


def _register_font_once() -> None:
    """註冊 CJK 字型（idempotent）。"""
    if CJK_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(CJK_FONT))


def _build_styles() -> dict[str, ParagraphStyle]:
    _register_font_once()
    base = getSampleStyleSheet()
    title = ParagraphStyle(
        "ZhTitle",
        parent=base["Title"],
        fontName=CJK_FONT,
        fontSize=18,
        leading=22,
        alignment=1,
    )
    h2 = ParagraphStyle(
        "ZhH2",
        parent=base["Heading2"],
        fontName=CJK_FONT,
        fontSize=14,
        leading=18,
    )
    body = ParagraphStyle(
        "ZhBody",
        parent=base["Normal"],
        fontName=CJK_FONT,
        fontSize=11,
        leading=15,
    )
    small = ParagraphStyle(
        "ZhSmall",
        parent=base["Normal"],
        fontName=CJK_FONT,
        fontSize=9,
        leading=12,
    )
    return {"title": title, "h2": h2, "body": body, "small": small}


# ---------------------------------------------------------------------------
# 1. 個人年終獎金條
# ---------------------------------------------------------------------------


@dataclass
class PersonalBonusSlipData:
    employee_name: str
    academic_year: int
    print_date: str  # YYYY.MM.DD
    year_end_amount: Decimal
    bonus_by_type: dict[SpecialBonusType, Decimal]
    school_name: str = "高雄市私立常春藤幼兒園"


BONUS_TYPE_LABELS: dict[SpecialBonusType, str] = {
    SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST: "特別獎金-113上考核獎金",
    SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND: "特別獎金-113下考核獎金",
    SpecialBonusType.SEMESTER_DIVIDEND_FIRST: "特別獎金-113上學期紅利獎金",
    SpecialBonusType.SEMESTER_DIVIDEND_SECOND: "特別獎金-113下學期紅利獎金",
    SpecialBonusType.AFTER_CLASS_AWARD: "114上鼓勵推動才藝班獎金",
    SpecialBonusType.TEACHING_EXTRA: "114上教課教師獎勵金",
    SpecialBonusType.EXCESS_ENROLLMENT: "114上超額獎金",
    SpecialBonusType.FESTIVAL_DIFF: "114.8-115.01節慶獎金差額",
    SpecialBonusType.CUSTOM: "其他特別獎金",
}


def generate_personal_bonus_slip_pdf(data: PersonalBonusSlipData) -> bytes:
    """產生一張個人年終獎金條 PDF（A4，對應 Excel「New年終獎金條」格式）。"""
    styles = _build_styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )
    flow = []
    flow.append(Paragraph(data.school_name, styles["title"]))
    flow.append(Paragraph(f"{data.academic_year}年終分紅發放明細表", styles["h2"]))
    flow.append(
        Paragraph(
            f"姓名：{data.employee_name}　　　　　列印日期：{data.print_date}",
            styles["body"],
        )
    )
    flow.append(Spacer(1, 0.4 * cm))

    # 左欄：年終獎金（固定）／右欄：特別獎金（多項）
    bonus_rows = [["項目", "金額"]]
    bonus_rows.append(["年終獎金", f"{data.year_end_amount:,.2f}"])
    subtotal_a = data.year_end_amount

    special_rows = [["項目", "金額"]]
    subtotal_b = Decimal("0")
    for btype, label in BONUS_TYPE_LABELS.items():
        amount = data.bonus_by_type.get(btype, Decimal("0"))
        special_rows.append([label, f"{amount:,.2f}"])
        subtotal_b += amount
    special_rows.append(["小計 (B)", f"{subtotal_b:,.2f}"])

    bonus_rows.append(["小計 (A)", f"{subtotal_a:,.2f}"])
    while len(bonus_rows) < len(special_rows):
        bonus_rows.append(["", ""])

    actual_amount = subtotal_a + subtotal_b
    combined = []
    for left, right in zip(bonus_rows, special_rows):
        combined.append([left[0], left[1], right[0], right[1]])

    table = Table(
        combined,
        colWidths=[3.8 * cm, 3 * cm, 6.5 * cm, 3 * cm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), CJK_FONT, 10),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("ALIGN", (3, 0), (3, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    flow.append(table)
    flow.append(Spacer(1, 0.5 * cm))
    flow.append(
        Paragraph(
            f"<b>實際金額 (A)+(B) = {actual_amount:,.2f} 元</b>", styles["h2"]
        )
    )

    doc.build(flow)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 2. 轉帳名冊 PDF
# ---------------------------------------------------------------------------


@dataclass
class TransferEntry:
    bank_account: str
    name: str
    amount: Decimal


def generate_transfer_roster_pdf(
    *,
    entries: Iterable[TransferEntry],
    academic_year: int,
    school_name: str = "高雄市私立常春藤幼兒園",
    title: str = "年終獎金 轉帳名冊",
    org_bank_account: str = "0727-940-008106",
) -> bytes:
    """產出年終獎金轉帳名冊 PDF。"""
    styles = _build_styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )
    flow = []
    flow.append(Paragraph(school_name, styles["title"]))
    flow.append(Paragraph(f"{academic_year}年 {title}", styles["h2"]))
    flow.append(Paragraph(f"轉出帳號：{org_bank_account}", styles["body"]))
    flow.append(Spacer(1, 0.3 * cm))

    rows: list[list[str]] = [["帳號", "戶名", "金額"]]
    total = Decimal("0")
    for entry in entries:
        if entry.amount <= 0:
            continue
        rows.append([entry.bank_account, entry.name, f"{entry.amount:,.2f}"])
        total += entry.amount
    rows.append(["", "合計", f"{total:,.2f}"])

    table = Table(rows, colWidths=[5.5 * cm, 4 * cm, 4 * cm], hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), CJK_FONT, 10),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("BACKGROUND", (0, -1), (-1, -1), colors.beige),
                ("FONTNAME", (0, -1), (-1, -1), CJK_FONT),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("ALIGN", (2, 0), (2, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    flow.append(table)
    flow.append(Spacer(1, 1 * cm))
    flow.append(Paragraph("經辦人：　　　　　　　　　　　主管：", styles["body"]))

    doc.build(flow)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 3. 年終獎金總表 PDF
# ---------------------------------------------------------------------------


@dataclass
class SummaryTableRow:
    name: str
    year_end_amount: Decimal
    bonus_by_type: dict[SpecialBonusType, Decimal]
    total: Decimal


def generate_summary_table_pdf(
    *,
    rows: Iterable[SummaryTableRow],
    academic_year: int,
    school_name: str = "高雄市私立常春藤幼兒園",
) -> bytes:
    """產出年終獎金總表 PDF（橫向 A4，多欄）。"""
    styles = _build_styles()
    buf = io.BytesIO()
    # 橫向 A4
    from reportlab.lib.pagesizes import landscape

    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )
    flow = []
    flow.append(Paragraph(school_name, styles["title"]))
    flow.append(Paragraph(f"{academic_year}年度年終分紅獎金", styles["h2"]))
    flow.append(Spacer(1, 0.3 * cm))

    type_order = [
        (SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST, "113上考核"),
        (SpecialBonusType.APPRAISAL_HALF_BONUS_SECOND, "113下考核"),
        (SpecialBonusType.SEMESTER_DIVIDEND_FIRST, "113上紅利"),
        (SpecialBonusType.SEMESTER_DIVIDEND_SECOND, "113下紅利"),
        (SpecialBonusType.AFTER_CLASS_AWARD, "114上鼓勵才藝"),
        (SpecialBonusType.TEACHING_EXTRA, "114上教課"),
        (SpecialBonusType.EXCESS_ENROLLMENT, "114上超額"),
        (SpecialBonusType.FESTIVAL_DIFF, "114上節慶差額"),
    ]
    header = ["姓名", "年終獎金"] + [label for _, label in type_order] + ["合計"]
    data: list[list[str]] = [header]
    grand_total = Decimal("0")
    for row in rows:
        line = [
            row.name,
            f"{row.year_end_amount:,.0f}",
        ]
        for btype, _ in type_order:
            v = row.bonus_by_type.get(btype, Decimal("0"))
            line.append(f"{v:,.0f}" if v != 0 else "")
        line.append(f"{row.total:,.0f}")
        data.append(line)
        grand_total += row.total
    data.append([""] + [""] * (len(type_order) + 1) + [f"總計 {grand_total:,.0f}"])

    col_widths = [2.4 * cm, 2.4 * cm] + [2 * cm] * len(type_order) + [2.6 * cm]
    table = Table(data, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("FONT", (0, 0), (-1, -1), CJK_FONT, 8),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("BACKGROUND", (0, -1), (-1, -1), colors.beige),
                ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    flow.append(table)
    doc.build(flow)
    return buf.getvalue()

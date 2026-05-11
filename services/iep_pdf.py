"""IEP PDF — A4 制式格式 (Phase 4A)."""

import io
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.lib import colors

_CJK_FONT = "STSong-Light"


def _ensure_font():
    try:
        pdfmetrics.getFont(_CJK_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))


def generate_iep_pdf(
    *,
    student_name: str,
    school_year: int,
    semester: int,
    current_status: str,
    long_term_goals: str,
    short_term_goals: list,
    mid_term_evaluation: str,
    final_evaluation: str,
    iep_team_members: list,
    meeting_dates: dict,
    institution_name: str = "義華幼兒園",
) -> bytes:
    _ensure_font()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontName=_CJK_FONT, fontSize=16)
    body = ParagraphStyle(
        "body", parent=styles["Normal"], fontName=_CJK_FONT, fontSize=11, leading=15
    )

    flow = [
        Paragraph(institution_name, h1),
        Paragraph(
            f"個別化教育計畫 IEP — {student_name}（{school_year} 學年度第 {semester} 學期）",
            h1,
        ),
        Spacer(1, 6 * mm),
        Paragraph(f"<b>目前發展狀況：</b><br/>{current_status or ''}", body),
        Spacer(1, 4 * mm),
        Paragraph(f"<b>長期目標：</b><br/>{long_term_goals or ''}", body),
        Spacer(1, 4 * mm),
        Paragraph("<b>短期目標：</b>", body),
    ]
    if short_term_goals:
        rows = [["目標", "達成標準", "到期日", "狀態"]]
        for g in short_term_goals:
            rows.append(
                [
                    g.get("goal", ""),
                    g.get("criteria", ""),
                    g.get("due_date", ""),
                    g.get("status", ""),
                ]
            )
        t = Table(rows, hAlign="LEFT")
        t.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, -1), _CJK_FONT, 10),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ]
            )
        )
        flow.append(t)
    flow += [
        Spacer(1, 4 * mm),
        Paragraph(f"<b>期中評估：</b><br/>{mid_term_evaluation or '（待填）'}", body),
        Paragraph(f"<b>期末評估：</b><br/>{final_evaluation or '（待填）'}", body),
        Spacer(1, 4 * mm),
        Paragraph(
            f"<b>IEP 團隊：</b> {', '.join(m.get('name', '') for m in (iep_team_members or []))}",
            body,
        ),
        Paragraph(f"<b>會議日期：</b> {meeting_dates or '（無）'}", body),
    ]
    doc.build(flow)
    return buf.getvalue()

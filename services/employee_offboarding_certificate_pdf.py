"""離職證明 PDF service（勞基法 §19）。

§19 規定：勞工於離職時，得請求發給服務證明書，雇主不得拒絕。
證明書不得記載對受僱人不利之事項（如離職原因、評核分數等）。

PDF 內容：公司資訊 + 員工姓名/身分證/到職日/離職日/職務 + 證明文字 + 日期。
"""

from __future__ import annotations

from datetime import date
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from sqlalchemy.orm import Session

from models.employee import Employee
from utils.pdf_fonts import CJK_FONT_NAME, register_cjk_font


def _fmt_roc(d: date) -> str:
    """西元 → 民國年 (YYY.MM.DD)"""
    return f"{d.year - 1911}.{d.month:02d}.{d.day:02d}"


def generate_certificate_pdf(
    session: Session, employee_id: int, resign_date: date
) -> bytes:
    """產生離職證明 PDF bytes。

    Args:
        session: SQLAlchemy session
        employee_id: 員工 ID
        resign_date: 離職日

    Returns:
        PDF bytes（可直接寫檔或串流）

    Raises:
        ValueError: 員工不存在
    """
    emp = session.query(Employee).filter_by(id=employee_id).first()
    if emp is None:
        raise ValueError(f"員工不存在: id={employee_id}")

    register_cjk_font()

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=2.5 * cm,
        rightMargin=2.5 * cm,
        topMargin=2.5 * cm,
        bottomMargin=2.5 * cm,
    )

    base = getSampleStyleSheet()["Normal"]
    title_style = ParagraphStyle(
        "Title",
        parent=base,
        fontName=CJK_FONT_NAME,
        fontSize=22,
        alignment=1,
        spaceAfter=24,  # alignment=1 為置中
    )
    body_style = ParagraphStyle(
        "Body",
        parent=base,
        fontName=CJK_FONT_NAME,
        fontSize=12,
        leading=20,
        spaceAfter=8,
    )
    sign_style = ParagraphStyle(
        "Sign",
        parent=base,
        fontName=CJK_FONT_NAME,
        fontSize=12,
        alignment=2,
        leading=22,
        spaceBefore=36,  # alignment=2 為靠右
    )

    elements = []
    elements.append(Paragraph("離職證明書", title_style))
    elements.append(Spacer(1, 0.5 * cm))

    # 公司資訊（hardcode；未來可從 config table 讀）
    elements.append(Paragraph("扣繳義務人：常春藤幼兒園", body_style))
    elements.append(Paragraph("統一編號：（請填入）", body_style))
    elements.append(Paragraph("公司地址：（請填入）", body_style))
    elements.append(Spacer(1, 0.8 * cm))

    elements.append(Paragraph("茲證明：", body_style))
    elements.append(Spacer(1, 0.3 * cm))

    elements.append(Paragraph(f"姓名：{emp.name}", body_style))
    elements.append(Paragraph(f"身分證字號：{emp.id_number or '（未填）'}", body_style))
    hire_str = _fmt_roc(emp.hire_date) if emp.hire_date else "（未填）"
    elements.append(Paragraph(f"到職日期：{hire_str}", body_style))
    elements.append(Paragraph(f"離職日期：{_fmt_roc(resign_date)}", body_style))
    elements.append(Paragraph(f"擔任職務：{emp.position or '（未填）'}", body_style))
    elements.append(Spacer(1, 0.5 * cm))

    elements.append(Paragraph("特此證明。", body_style))

    today = date.today()
    elements.append(Paragraph("負責人簽章：______________", sign_style))
    elements.append(Paragraph(f"證明日期：{_fmt_roc(today)}", sign_style))

    doc.build(elements)
    return buf.getvalue()

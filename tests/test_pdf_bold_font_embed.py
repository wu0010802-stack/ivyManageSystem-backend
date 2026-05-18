"""PDF bold 中文字型 family 對映驗證 — 防回歸到 Helvetica-Bold fallback（P0-C）。

修補 bug sweep 2026-05-18 P0-C 的防禦性保護。

背景：services/iep_pdf.py 與 year_end/print_pdf.py 使用 Paragraph('<b>中文</b>') 時，
若 reportlab 找不到 family 的 bold variant，會退回 Helvetica-Bold（無 CJK glyph）→
bold 中文渲染為空白/方塊。

實測：reportlab 4.5.0 的 `registerFont(TTFont(...))` 已會自動把 family map 的
(b,i) 四種組合都指向同一個 font，因此在當前環境下 bug 不會發生。本 fix 加
`registerFontFamily` 顯式鎖定 mapping，避免 reportlab 版本變更或 ParagraphStyle
parent 繼承的 corner case 翻車。

本測試用 positive invariant：`register_cjk_font()` 後 family map 對 (b,i) 四種
組合都必須回到 NotoSansTC（不是 fallback 到 Helvetica-Bold 或拋錯）。
"""

from __future__ import annotations

from io import BytesIO

import pytest
from reportlab.lib.fonts import tt2ps
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate

from utils.pdf_fonts import CJK_FONT_NAME, register_cjk_font


@pytest.mark.parametrize(
    "bold,italic",
    [(0, 0), (1, 0), (0, 1), (1, 1)],
)
def test_family_map_resolves_all_variants_to_cjk_font(bold, italic):
    """family map 對 normal/bold/italic/boldItalic 四種組合都應回 NotoSansTC。"""
    register_cjk_font()
    assert tt2ps(CJK_FONT_NAME, bold, italic) == CJK_FONT_NAME


def test_iep_style_chain_does_not_fallback_to_helvetica_bold():
    """iep_pdf 風格 (parent=Title) 的 Paragraph('<b>中文</b>') 不應 fallback 到 Helvetica-Bold。"""
    register_cjk_font()
    styles = getSampleStyleSheet()
    # 模擬 services/iep_pdf.py:44-49 的 ParagraphStyle chain（parent=styles['Title']）
    h1 = ParagraphStyle(
        "h1", parent=styles["Title"], fontName=CJK_FONT_NAME, fontSize=16
    )
    body = ParagraphStyle(
        "body",
        parent=styles["Normal"],
        fontName=CJK_FONT_NAME,
        fontSize=11,
        leading=18,
        wordWrap="CJK",
    )

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    doc.build(
        [
            Paragraph("特殊教育服務", h1),
            Paragraph("<b>目前發展狀況：</b><br/>正常", body),
            Paragraph("<b>長期目標：</b><br/>進步", body),
        ]
    )
    pdf = buf.getvalue()

    # PDF 必須 embed NotoSansTC（subset 名稱可能含 prefix 如 AAAAAA+NotoSansTC-Thin）
    assert b"NotoSansTC" in pdf, "bold 中文未 embed NotoSansTC"
    # 不應有 Helvetica-Bold 出現於 BaseFont（reportlab 內建 fallback、無 CJK glyph）
    assert (
        b"/BaseFont /Helvetica-Bold" not in pdf
    ), "fallback 到 Helvetica-Bold（無 CJK glyph 會空白/方塊）"

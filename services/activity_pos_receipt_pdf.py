"""POS 收據 PDF 產生器（80mm 寬窄條，模擬熱感應收據版面）。

對應前端 components/activity/POSReceipt.vue。
熱感應 80mm 紙寬，列印機驅動會 auto-cut 結尾空白。雷射列印時可印在 A4 一角。
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from utils.pdf_fonts import CJK_FONT_NAME, register_cjk_font

PAGE_WIDTH = 80 * mm
PAGE_HEIGHT = 200 * mm  # 一張收據通常 < 100mm，多留兜底
MARGIN_X = 4 * mm


def _fmt_amount(n: Any) -> str:
    if n is None or n == "":
        return ""
    try:
        return f"${int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _draw_text(
    c: canvas.Canvas, x: float, y: float, text: str, *, font_size: int = 9
) -> None:
    c.setFont(CJK_FONT_NAME, font_size)
    c.drawString(x, y, text)


def _draw_right(
    c: canvas.Canvas, x: float, y: float, text: str, *, font_size: int = 9
) -> None:
    c.setFont(CJK_FONT_NAME, font_size)
    c.drawRightString(x, y, text)


def _draw_dashed_line(c: canvas.Canvas, y: float) -> None:
    c.setDash(2, 2)
    c.setLineWidth(0.4)
    c.line(MARGIN_X, y, PAGE_WIDTH - MARGIN_X, y)
    c.setDash()


def generate_pos_receipt_pdf(
    *,
    receipt: dict[str, Any],
    institution_name: str = "常春藤幼兒園",
    institution_subtitle: str = "課後才藝繳費收據",
) -> bytes:
    """產生 POS 收據 PDF；回傳 bytes。

    receipt 結構同 api/activity/pos.py `_parse_receipt_response_from_record` 的回傳。
    """
    register_cjk_font()

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT))

    is_refund = receipt.get("type") == "refund"
    is_reprint = bool(receipt.get("is_reprint"))
    right_x = PAGE_WIDTH - MARGIN_X
    center_x = PAGE_WIDTH / 2

    y = PAGE_HEIGHT - 8 * mm

    # ── 抬頭 ──────────────────────────────────────────────────────────────
    c.setFont(CJK_FONT_NAME, 12)
    c.drawCentredString(center_x, y, institution_name)
    y -= 5 * mm
    c.setFont(CJK_FONT_NAME, 9)
    subtitle = "退費收據" if is_refund else institution_subtitle
    if is_reprint:
        subtitle += "（補印）"
    c.drawCentredString(center_x, y, subtitle)
    y -= 5 * mm

    # Finding 2（2026-06-23 audit）：補印且明細係即時重建（舊收據無 snapshot）時，
    # 標註明細可能與開立當下不符，避免被當成正式原始收據。新收據走凍結 snapshot
    # （items_rebuilt_live=False）則不顯示此警語。
    if is_reprint and receipt.get("items_rebuilt_live"):
        c.setFont(CJK_FONT_NAME, 7)
        c.setFillColor(colors.HexColor("#999999"))
        c.drawCentredString(
            center_x, y, "※明細依目前報名狀態重建，金額以收據開立時為準"
        )
        c.setFillColor(colors.black)
        y -= 4 * mm

    # ── meta ──────────────────────────────────────────────────────────────
    c.setFont(CJK_FONT_NAME, 8)
    _draw_text(c, MARGIN_X, y, f"編號：{receipt.get('receipt_no') or ''}", font_size=8)
    y -= 3.5 * mm
    _draw_text(c, MARGIN_X, y, f"時間：{receipt.get('created_at') or ''}", font_size=8)
    y -= 3.5 * mm
    payment_method = receipt.get("payment_method") or "現金"
    _draw_text(c, MARGIN_X, y, f"方式：{payment_method}", font_size=8)
    y -= 3.5 * mm
    if receipt.get("operator"):
        _draw_text(c, MARGIN_X, y, f"經手：{receipt['operator']}", font_size=8)
        y -= 3.5 * mm

    y -= 1 * mm
    _draw_dashed_line(c, y)
    y -= 3 * mm

    # ── 明細 ──────────────────────────────────────────────────────────────
    for item in receipt.get("items") or []:
        # 學生姓名 + 班級
        c.setFont(CJK_FONT_NAME, 9)
        student_line = item.get("student_name") or ""
        cls = item.get("class_name") or ""
        if cls:
            _draw_text(c, MARGIN_X, y, student_line, font_size=9)
            _draw_text(
                c,
                MARGIN_X + c.stringWidth(student_line, CJK_FONT_NAME, 9) + 2 * mm,
                y,
                cls,
                font_size=7,
            )
        else:
            _draw_text(c, MARGIN_X, y, student_line, font_size=9)
        y -= 4 * mm

        # 課程 / 用品行
        for line in item.get("courses") or []:
            name = line.get("name") or ""
            price = _fmt_amount(line.get("price"))
            _draw_text(c, MARGIN_X + 1 * mm, y, name, font_size=8)
            _draw_right(c, right_x, y, price, font_size=8)
            y -= 3.5 * mm
        for line in item.get("supplies") or []:
            name = line.get("name") or ""
            price = _fmt_amount(line.get("price"))
            _draw_text(c, MARGIN_X + 1 * mm, y, name, font_size=8)
            _draw_right(c, right_x, y, price, font_size=8)
            y -= 3.5 * mm

        # 本次收取/退費
        label = "本次退費" if is_refund else "本次收取"
        applied = _fmt_amount(item.get("amount_applied"))
        c.setFont(CJK_FONT_NAME, 8)
        c.setFillColor(colors.HexColor("#555555"))
        _draw_text(c, MARGIN_X + 1 * mm, y, label, font_size=8)
        _draw_right(c, right_x, y, applied, font_size=8)
        c.setFillColor(colors.black)
        y -= 4 * mm

    _draw_dashed_line(c, y)
    y -= 4 * mm

    # ── 合計 ──────────────────────────────────────────────────────────────
    c.setFont(CJK_FONT_NAME, 11)
    total_label = "退款合計" if is_refund else "應收合計"
    _draw_text(c, MARGIN_X, y, total_label, font_size=11)
    _draw_right(c, right_x, y, _fmt_amount(receipt.get("total")), font_size=11)
    y -= 5 * mm

    # P2-6（2026-06-23 audit）：部分作廢標註。重印時若該收據有 item 已作廢，
    # 合計只計有效金額；補一行說明原始開立金額，避免同收據編號兩次列印金額不同
    # 卻無說明造成客訴困惑。
    if receipt.get("has_voided_items"):
        c.setFont(CJK_FONT_NAME, 8)
        _draw_text(
            c,
            MARGIN_X,
            y,
            f"※部分項目已作廢，原始開立 {_fmt_amount(receipt.get('original_total'))}",
            font_size=8,
        )
        y -= 4 * mm

    if receipt.get("tendered") is not None:
        c.setFont(CJK_FONT_NAME, 9)
        _draw_text(c, MARGIN_X, y, "實收", font_size=9)
        _draw_right(c, right_x, y, _fmt_amount(receipt.get("tendered")), font_size=9)
        y -= 4 * mm
    if receipt.get("change") is not None:
        c.setFont(CJK_FONT_NAME, 9)
        _draw_text(c, MARGIN_X, y, "找零", font_size=9)
        _draw_right(c, right_x, y, _fmt_amount(receipt.get("change")), font_size=9)
        y -= 4 * mm

    if receipt.get("notes"):
        y -= 1 * mm
        c.setFont(CJK_FONT_NAME, 8)
        _draw_text(c, MARGIN_X, y, f"備註：{receipt['notes']}", font_size=8)
        y -= 4 * mm

    # ── 頁尾 ──────────────────────────────────────────────────────────────
    y -= 2 * mm
    c.setFont(CJK_FONT_NAME, 9)
    footer = "—— 已辦理退費 ——" if is_refund else "—— 謝謝惠顧 ——"
    c.drawCentredString(center_x, y, footer)

    c.showPage()
    c.save()
    return buffer.getvalue()

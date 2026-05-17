"""幼生在籍花名冊 PDF 產生器（A4 橫向）。

對應前端 components/enrollment/EnrollmentRosterTable.vue 的列印版本。

版面：
- 標題列：<民國年> 學年度 <上/下學期> 全園在籍花名冊
- 主表：每班一欄
  - 表頭：班序號 / 年級 / 班名 / 班導師 / 副班導 / 美師
  - 身體：學生姓名（按各班最多人數展開），新生/原住民/特教/不足齡以後綴標示
  - 表尾：合計 / 舊生 / 新生 / 年級合計（colspan）/ 舊・新（colspan）/ 全園總計
- 員工名單：表格下方按職稱分組列出
"""

from __future__ import annotations

import io
from typing import Any

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from utils.pdf_fonts import CJK_FONT_NAME, register_cjk_font

_TAG_SUFFIX = {
    "新生": "新",
    "不足齡": "齡",
    "特教生": "特",
    "原住民": "原",
}


def _fmt_generated_date(roc_compact: str) -> str:
    """`1150517` → `115/05/17`，其他 fallback 原樣。"""
    if not roc_compact or len(roc_compact) < 7 or not roc_compact.isdigit():
        return roc_compact or ""
    return f"{roc_compact[:-4]}/{roc_compact[-4:-2]}/{roc_compact[-2:]}"


def _name_with_tag(student: dict[str, Any]) -> str:
    """姓名 + 狀態標記後綴：`王小明 原`（原住民）等。"""
    name = student.get("name") or ""
    tag = student.get("status_tag")
    suffix = _TAG_SUFFIX.get(tag, "")
    if suffix:
        return f"{name} {suffix}"
    return name


def generate_enrollment_roster_pdf(*, roster: dict[str, Any]) -> bytes:
    """產生在籍花名冊 PDF；回傳 bytes。

    roster 結構同 api/student_enrollment.py RosterResponse 的 model_dump()。
    """
    register_cjk_font()
    font = CJK_FONT_NAME

    buffer = io.BytesIO()
    page_size = landscape(A4)
    c = canvas.Canvas(buffer, pagesize=page_size)
    width, height = page_size

    margin_x = 10 * mm
    margin_top = 10 * mm
    margin_bottom = 10 * mm

    classes = roster.get("classes") or []
    grade_summaries = roster.get("grade_summaries") or []
    class_count = len(classes)

    # ── 標題列 ─────────────────────────────────────────────────────────────
    school_year = roster.get("school_year") or 0
    roc_year = school_year - 1911 if school_year > 1911 else school_year
    semester_label = roster.get("semester_label") or ""
    gen_date = _fmt_generated_date(roster.get("generated_date") or "")

    c.setFont(font, 16)
    title_y = height - margin_top - 6 * mm
    c.drawString(
        margin_x,
        title_y,
        f"{roc_year} 學年度 {semester_label}・全園在籍花名冊",
    )
    c.setFont(font, 10)
    c.setFillColor(colors.HexColor("#555555"))
    c.drawRightString(width - margin_x, title_y, f"列印日期：{gen_date}")
    c.setFillColor(colors.black)

    # 標題下方分隔線
    c.setLineWidth(0.8)
    c.line(margin_x, title_y - 3 * mm, width - margin_x, title_y - 3 * mm)

    # ── 主表寬度規劃 ───────────────────────────────────────────────────────
    table_top = title_y - 8 * mm
    content_w = width - 2 * margin_x
    label_col_w = 18 * mm
    if class_count == 0:
        c.setFont(font, 12)
        c.drawString(
            margin_x,
            table_top - 10 * mm,
            "此學期尚無在籍班級。",
        )
        c.showPage()
        c.save()
        return buffer.getvalue()

    class_col_w = (content_w - label_col_w) / class_count
    # 動態字級：欄太窄時縮小
    if class_col_w >= 22 * mm:
        body_font_size = 10
    elif class_col_w >= 16 * mm:
        body_font_size = 9
    else:
        body_font_size = 8
    header_font_size = max(body_font_size, 9)

    # ── 表頭：班序號 / 年級 / 班名 / 班導師 / 副班導 / 美師 ──────────────
    header_labels = ["班序", "年級", "班名", "班導師", "副班導", "美師"]
    header_extract = [
        lambda cls, idx: str(cls.get("class_number") or idx + 1),
        lambda cls, _: cls.get("grade_name") or "",
        lambda cls, _: cls.get("class_name") or "",
        lambda cls, _: cls.get("head_teacher_name") or "",
        lambda cls, _: cls.get("assistant_teacher_name") or "",
        lambda cls, _: cls.get("art_teacher_name") or "",
    ]
    header_row_h = 7 * mm

    # ── 學生身體列高 / row 數 ──────────────────────────────────────────────
    max_students = max((len(c.get("students") or []) for c in classes), default=0)
    body_row_h = 5.5 * mm

    # ── 表尾：合計 / 舊生 / 新生 / 年級合計 / 舊・新 / 總計 ────────────────
    footer_simple_labels = ["合計", "舊生", "新生"]
    footer_simple_extract = [
        lambda cls: str(cls.get("total") or 0),
        lambda cls: str(cls.get("old_count") or 0) if cls.get("old_count") else "",
        lambda cls: str(cls.get("new_count") or 0) if cls.get("new_count") else "",
    ]
    footer_row_h = 6 * mm
    footer_grade_h = 6 * mm
    grand_total_h = 8 * mm

    # ── 計算總表格高度 + 是否要縮 body row 高（一頁裝下） ───────────────
    total_header_h = len(header_labels) * header_row_h
    total_footer_h = (
        len(footer_simple_labels) * footer_row_h
        + 2 * footer_grade_h  # 年級合計 + 舊/新
        + grand_total_h
    )
    staff_block_min_h = 30 * mm  # 預留員工名單區
    available_body_h = (
        table_top
        - margin_bottom
        - total_header_h
        - total_footer_h
        - staff_block_min_h
        - 4 * mm
    )
    if max_students > 0:
        body_row_h = min(body_row_h, max(3.2 * mm, available_body_h / max_students))

    total_table_h = total_header_h + max_students * body_row_h + total_footer_h

    # ── 開始畫表 ────────────────────────────────────────────────────────────
    c.setLineWidth(0.4)

    # 整張表外框
    c.rect(
        margin_x, table_top - total_table_h, content_w, total_table_h, stroke=1, fill=0
    )

    # 內部直線（label col + class cols 間）
    x_pos = margin_x + label_col_w
    for i in range(class_count + 1):
        c.line(x_pos, table_top - total_table_h, x_pos, table_top)
        x_pos += class_col_w

    # 表頭：橫線 + 內容
    y = table_top
    c.setFont(font, header_font_size)
    for ri, label in enumerate(header_labels):
        # 該列底部橫線
        y -= header_row_h
        c.line(margin_x, y, margin_x + content_w, y)
        # label col
        c.drawString(
            margin_x + 1.5 * mm,
            y + header_row_h / 2 - 1.5 * mm,
            label,
        )
        # 各班 column
        for ci, cls in enumerate(classes):
            text = header_extract[ri](cls, ci)
            cx = margin_x + label_col_w + ci * class_col_w + class_col_w / 2
            cy = y + header_row_h / 2 - 1.5 * mm
            c.drawCentredString(cx, cy, text)

    # 身體區
    c.setFont(font, body_font_size)
    body_top = y  # 此時 y 在表頭下緣
    for ri in range(max_students):
        y -= body_row_h
        c.setLineWidth(0.2)
        c.line(margin_x, y, margin_x + content_w, y)
        c.setLineWidth(0.4)
        # 左側序號（label col 內）
        c.drawCentredString(
            margin_x + label_col_w / 2,
            y + body_row_h / 2 - 1.2 * mm,
            str(ri + 1),
        )
        for ci, cls in enumerate(classes):
            stus = cls.get("students") or []
            if ri >= len(stus):
                continue
            text = _name_with_tag(stus[ri])
            cx = margin_x + label_col_w + ci * class_col_w + class_col_w / 2
            cy = y + body_row_h / 2 - 1.2 * mm
            # 過長截斷
            max_chars = max(2, int(class_col_w / mm / 2.5))
            if len(text) > max_chars:
                text = text[: max_chars - 1] + "…"
            c.drawCentredString(cx, cy, text)

    # 表尾
    c.setFont(font, header_font_size)
    # 1) 合計/舊生/新生（每班獨立 column）
    for ri, label in enumerate(footer_simple_labels):
        y -= footer_row_h
        c.line(margin_x, y, margin_x + content_w, y)
        c.drawString(
            margin_x + 1.5 * mm,
            y + footer_row_h / 2 - 1.5 * mm,
            label,
        )
        for ci, cls in enumerate(classes):
            text = footer_simple_extract[ri](cls)
            cx = margin_x + label_col_w + ci * class_col_w + class_col_w / 2
            cy = y + footer_row_h / 2 - 1.5 * mm
            c.drawCentredString(cx, cy, text)

    # 2) 年級合計（按年級 colspan）
    y -= footer_grade_h
    c.line(margin_x, y, margin_x + content_w, y)
    c.drawString(
        margin_x + 1.5 * mm,
        y + footer_grade_h / 2 - 1.5 * mm,
        "年級合計",
    )
    grade_spans = _compute_grade_spans(classes)
    for span in grade_spans:
        cx = margin_x + label_col_w + (span["start"] + span["count"] / 2) * class_col_w
        cy = y + footer_grade_h / 2 - 1.5 * mm
        c.drawCentredString(cx, cy, f"{span['grade_name']} {span['total']}人")
        # 年級邊界垂直線（畫粗一點以區隔）
        end_x = margin_x + label_col_w + (span["start"] + span["count"]) * class_col_w
        c.setLineWidth(0.6)
        c.line(end_x, y, end_x, y + footer_grade_h)
        c.setLineWidth(0.4)

    # 3) 年級 舊／新
    y -= footer_grade_h
    c.line(margin_x, y, margin_x + content_w, y)
    c.drawString(
        margin_x + 1.5 * mm,
        y + footer_grade_h / 2 - 1.5 * mm,
        "舊／新",
    )
    for span in grade_spans:
        cx = margin_x + label_col_w + (span["start"] + span["count"] / 2) * class_col_w
        cy = y + footer_grade_h / 2 - 1.5 * mm
        c.drawCentredString(
            cx,
            cy,
            f"舊{span['old_count']} ／ 新{span['new_count']}",
        )

    # 4) 全園總計（橫跨整張）
    y -= grand_total_h
    c.line(margin_x, y, margin_x + content_w, y)
    grand_total = roster.get("grand_total") or 0
    new_grand = roster.get("new_grand_total") or 0
    old_grand = roster.get("old_grand_total") or 0
    c.setFont(font, 12)
    c.drawString(
        margin_x + 2 * mm,
        y + grand_total_h / 2 - 1.8 * mm,
        f"全園總計：{grand_total} 人　（舊生 {old_grand} ／ 新生 {new_grand}）",
    )

    # ── 員工名單（表格下方） ──────────────────────────────────────────────
    staff_top = y - 5 * mm
    c.setFont(font, header_font_size)
    c.setFillColor(colors.HexColor("#444444"))
    c.drawString(margin_x, staff_top, "員工名單")
    c.setFillColor(colors.black)
    c.setLineWidth(0.3)
    c.line(margin_x, staff_top - 1.5 * mm, margin_x + 30 * mm, staff_top - 1.5 * mm)

    c.setFont(font, body_font_size)
    sy = staff_top - 6 * mm
    staff_by_role = roster.get("staff_by_role") or {}
    for role, entries in staff_by_role.items():
        names = "、".join((e.get("name") or "") for e in (entries or []))
        line = f"【{role}】 {names}"
        # 簡易斷行：每行 ~70 char
        max_chars = max(20, int(content_w / mm / 2.2))
        while len(line) > max_chars:
            chunk = line[:max_chars]
            c.drawString(margin_x, sy, chunk)
            sy -= 4.5 * mm
            line = line[max_chars:]
            if sy < margin_bottom + 8 * mm:
                c.showPage()
                sy = height - margin_top - 6 * mm
                c.setFont(font, body_font_size)
        c.drawString(margin_x, sy, line)
        sy -= 4.5 * mm
        if sy < margin_bottom + 8 * mm:
            c.showPage()
            sy = height - margin_top - 6 * mm
            c.setFont(font, body_font_size)

    # 圖例（員工名單下方右側）
    if sy > margin_bottom + 12 * mm:
        c.setFont(font, 8)
        c.setFillColor(colors.HexColor("#555555"))
        legend = "標記說明：新=新生　齡=不足齡　特=特教生　原=原住民"
        c.drawString(margin_x, margin_bottom + 4 * mm, legend)
        c.setFillColor(colors.black)

    c.showPage()
    c.save()
    return buffer.getvalue()


def _compute_grade_spans(classes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把連續同年級的班合併成 span，回傳 list[{grade_name, start, count, total, new, old}]."""
    spans: list[dict[str, Any]] = []
    for idx, cls in enumerate(classes):
        gname = cls.get("grade_name") or ""
        if spans and spans[-1]["grade_name"] == gname:
            spans[-1]["count"] += 1
            spans[-1]["total"] += cls.get("total") or 0
            spans[-1]["new_count"] += cls.get("new_count") or 0
            spans[-1]["old_count"] += cls.get("old_count") or 0
        else:
            spans.append(
                {
                    "grade_name": gname,
                    "start": idx,
                    "count": 1,
                    "total": cls.get("total") or 0,
                    "new_count": cls.get("new_count") or 0,
                    "old_count": cls.get("old_count") or 0,
                }
            )
    return spans

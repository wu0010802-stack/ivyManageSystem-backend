"""Growth report PDF generator using reportlab + reportlab.graphics.

Pattern reference: .worktrees/moe-phase4/services/enrollment_certificate_pdf.py
- 用 utils/pdf_fonts.register_cjk_font() 註冊嵌入式 TTF（Noto Sans TC）
- 直接寫到 BytesIO 後 caller 寫檔
"""

from __future__ import annotations

import io
import unicodedata
from datetime import date
from typing import Any

from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from utils.pdf_fonts import CJK_FONT_NAME, register_cjk_font

_CJK_FONT = CJK_FONT_NAME


def _ensure_font() -> None:
    register_cjk_font()


def _fmt_roc(d: date) -> str:
    return f"中華民國 {d.year - 1911} 年 {d.month} 月 {d.day} 日"


def _draw_section_title(c, text: str, y: float) -> float:
    c.setFont(_CJK_FONT, 14)
    c.setFillColor(colors.HexColor("#0d9053"))
    c.drawString(20 * mm, y, text)
    c.setFillColor(colors.black)
    c.setFont(_CJK_FONT, 11)
    return y - 8 * mm


def _draw_kv_lines(
    c, pairs: list[tuple[str, str]], y: float, *, label_w: float = 28 * mm
) -> float:
    for label, value in pairs:
        c.drawString(20 * mm, y, label + "：")
        c.drawString(20 * mm + label_w, y, str(value))
        y -= 6 * mm
    return y


def _draw_paragraph(c, text: str, y: float, max_chars_per_line: int = 38) -> float:
    """簡易斷行 — reportlab 沒有 native paragraph，這裡按字數切。

    處理：剝除控制字元（保留 \\n \\t）、尊重明確換行、再對長行按字寬切。
    """
    if not text:
        return y
    cleaned = "".join(
        ch
        for ch in str(text)
        if ch in ("\n", "\t") or unicodedata.category(ch)[0] != "C"
    ).strip()
    if not cleaned:
        return y
    for raw_line in cleaned.split("\n"):
        line = raw_line.rstrip()
        if not line:
            y -= 3 * mm
            continue
        for start in range(0, len(line), max_chars_per_line):
            chunk = line[start : start + max_chars_per_line]
            c.drawString(20 * mm, y, chunk)
            y -= 5 * mm
    return y - 2 * mm


def _measurement_chart(
    series: dict[str, list[tuple]], width_mm: float, height_mm: float
) -> Drawing:
    """產生身高/體重折線圖；series = {"height": [(date_iso, value)], "weight": [...]}."""
    drawing = Drawing(width_mm * mm, height_mm * mm)
    chart = HorizontalLineChart()
    chart.x = 20
    chart.y = 20
    chart.width = width_mm * mm - 40
    chart.height = height_mm * mm - 40

    h = series.get("height") or []
    w = series.get("weight") or []
    all_dates = sorted({d for d, _ in h} | {d for d, _ in w})
    if not all_dates:
        return drawing

    chart.categoryAxis.categoryNames = all_dates
    height_dict = dict(h)
    weight_dict = dict(w)
    chart.data = [
        [height_dict.get(d, 0) for d in all_dates],
        [weight_dict.get(d, 0) for d in all_dates],
    ]
    chart.lines[0].strokeColor = colors.HexColor("#0d9053")
    chart.lines[1].strokeColor = colors.HexColor("#3b82f6")
    chart.lines[0].strokeWidth = 1.5
    chart.lines[1].strokeWidth = 1.5
    chart.categoryAxis.labels.fontName = _CJK_FONT
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.angle = 30
    chart.valueAxis.labels.fontName = _CJK_FONT
    chart.valueAxis.labels.fontSize = 7
    drawing.add(chart)
    return drawing


def generate_growth_report_pdf(*, report_data: dict[str, Any]) -> bytes:
    """生成 PDF；回傳 bytes.

    report_data 結構（由 services/growth_report_collector + endpoint 組合）：
    {
      "student": {"name": str, "student_no": str, "classroom_name": str, "birthday": date},
      "report": {"period_label": str, "period_start": date, "period_end": date,
                 "report_id": int, "teacher_narrative": str, "generated_on": date},
      "attendance_summary": {...},
      "highlight_observations": [{...}],
      "milestones": [{"title": str, "achieved_on": date, "icon": str}],
      "measurement_series": {"height": [...], "weight": [...]},
      "assessments": [{"domain": str, "rating": int, "comment": str}],
      "activities": [{"name": str, "registered_at": date}],
      "institution_name": str,
    }
    """
    _ensure_font()
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    student = report_data["student"]
    report = report_data["report"]
    inst = report_data.get("institution_name", "義華幼兒園")

    # ===== 封面 =====
    c.setFont(_CJK_FONT, 16)
    c.drawCentredString(width / 2, height - 30 * mm, inst)
    c.setFont(_CJK_FONT, 24)
    c.setFillColor(colors.HexColor("#0d9053"))
    c.drawCentredString(width / 2, height - 60 * mm, "成長報告")
    c.setFillColor(colors.black)
    c.setFont(_CJK_FONT, 14)
    c.drawCentredString(width / 2, height - 80 * mm, student["name"])
    c.setFont(_CJK_FONT, 11)
    c.drawCentredString(
        width / 2,
        height - 90 * mm,
        f"班級：{student.get('classroom_name', '—')}",
    )
    c.drawCentredString(
        width / 2,
        height - 100 * mm,
        f"期間：{report['period_label']}"
        f"（{report['period_start']} ~ {report['period_end']}）",
    )
    c.setFont(_CJK_FONT, 9)
    c.setFillColor(colors.gray)
    c.drawCentredString(
        width / 2,
        height - 110 * mm,
        f"報告編號 #{report['report_id']}　|　開立日期 {_fmt_roc(report['generated_on'])}",
    )
    c.setFillColor(colors.black)
    c.showPage()

    # ===== 第二頁起：內容 =====
    y = height - 25 * mm

    # § 學生資料
    y = _draw_section_title(c, "§ 學生資料", y)
    y = _draw_kv_lines(
        c,
        [
            ("姓名", student["name"]),
            ("學號", student.get("student_no", "—")),
            ("班級", student.get("classroom_name", "—")),
        ],
        y,
    )
    y -= 5 * mm

    # § 出勤統計
    y = _draw_section_title(c, "§ 出勤統計", y)
    att = report_data.get("attendance_summary", {})
    y = _draw_kv_lines(
        c,
        [
            ("出勤天數", f"{att.get('present_days', 0)} 天"),
            ("請假天數", f"{att.get('leave_days', 0)} 天"),
            ("病假天數", f"{att.get('sick_days', 0)} 天"),
            ("缺席天數", f"{att.get('absent_days', 0)} 天"),
            ("出席率", f"{att.get('present_rate', 0) * 100:.1f}%"),
        ],
        y,
    )
    y -= 5 * mm

    # § 教師觀察精選
    y = _draw_section_title(c, "§ 教師觀察精選", y)
    obs_list = report_data.get("highlight_observations", [])
    if obs_list:
        for o in obs_list:
            mark = "★ " if o.get("is_highlight") else "・"
            header = f"{mark}{o.get('observation_date', '')}"
            if o.get("domain"):
                header += f"（{o['domain']}）"
            c.setFont(_CJK_FONT, 10)
            c.drawString(20 * mm, y, header)
            y -= 5 * mm
            y = _draw_paragraph(c, o.get("narrative", ""), y)
            if y < 30 * mm:
                c.showPage()
                y = height - 25 * mm
    else:
        c.setFont(_CJK_FONT, 10)
        c.drawString(20 * mm, y, "（本期間無教師觀察紀錄）")
        y -= 6 * mm
    y -= 3 * mm

    if y < 80 * mm:
        c.showPage()
        y = height - 25 * mm

    # § 里程碑
    y = _draw_section_title(c, "§ 里程碑", y)
    ms_list = report_data.get("milestones", [])
    if ms_list:
        c.setFont(_CJK_FONT, 11)
        for m in ms_list:
            icon = m.get("icon", "")
            title = m.get("title", "")
            date_str = m.get("achieved_on", "")
            c.drawString(20 * mm, y, f"{icon} {date_str}　{title}")
            y -= 6 * mm
            if y < 30 * mm:
                c.showPage()
                y = height - 25 * mm
    else:
        c.setFont(_CJK_FONT, 10)
        c.drawString(20 * mm, y, "（本期間無里程碑）")
        y -= 6 * mm
    y -= 3 * mm

    # § 量測曲線
    if y < 90 * mm:
        c.showPage()
        y = height - 25 * mm
    y = _draw_section_title(c, "§ 量測曲線", y)
    series = report_data.get("measurement_series", {})
    if series.get("height") or series.get("weight"):
        drawing = _measurement_chart(series, width_mm=170, height_mm=70)
        drawing.drawOn(c, 20 * mm, y - 75 * mm)
        y -= 80 * mm
    else:
        c.setFont(_CJK_FONT, 10)
        c.drawString(20 * mm, y, "（本期間無量測資料）")
        y -= 6 * mm

    # § 學期評量
    if y < 60 * mm:
        c.showPage()
        y = height - 25 * mm
    y = _draw_section_title(c, "§ 學期評量", y)
    assessments = report_data.get("assessments", [])
    if assessments:
        c.setFont(_CJK_FONT, 10)
        for a in assessments:
            domain = a.get("domain", "—")
            rating = a.get("rating", "—")
            comment = a.get("comment", "") or ""
            c.drawString(20 * mm, y, f"{domain}　評分：{rating}")
            y -= 5 * mm
            if comment:
                y = _draw_paragraph(c, comment, y)
            if y < 30 * mm:
                c.showPage()
                y = height - 25 * mm
    else:
        c.setFont(_CJK_FONT, 10)
        c.drawString(20 * mm, y, "（本期間無學期評量）")
        y -= 6 * mm
    y -= 3 * mm

    # § 才藝參與
    if y < 40 * mm:
        c.showPage()
        y = height - 25 * mm
    y = _draw_section_title(c, "§ 才藝參與", y)
    activities = report_data.get("activities", [])
    if activities:
        c.setFont(_CJK_FONT, 10)
        for act in activities:
            c.drawString(
                20 * mm,
                y,
                f"・{act.get('name', '—')}（{act.get('registered_at', '')}）",
            )
            y -= 5 * mm
            if y < 30 * mm:
                c.showPage()
                y = height - 25 * mm
    else:
        c.setFont(_CJK_FONT, 10)
        c.drawString(20 * mm, y, "（本期間無才藝報名）")
        y -= 6 * mm
    y -= 3 * mm

    # § 教師敘述
    if y < 60 * mm:
        c.showPage()
        y = height - 25 * mm
    y = _draw_section_title(c, "§ 教師敘述", y)
    narrative = report.get("teacher_narrative") or "（本份報告教師未填寫敘述）"
    c.setFont(_CJK_FONT, 10)
    y = _draw_paragraph(c, narrative, y, max_chars_per_line=40)

    # 封底
    c.setFont(_CJK_FONT, 9)
    c.setFillColor(colors.gray)
    c.drawString(20 * mm, 25 * mm, "教師簽名：__________________")
    c.drawString(20 * mm, 18 * mm, f"開立日期：{_fmt_roc(report['generated_on'])}")
    c.drawString(20 * mm, 11 * mm, f"報告編號：#{report['report_id']}")

    c.save()
    buffer.seek(0)
    return buffer.getvalue()

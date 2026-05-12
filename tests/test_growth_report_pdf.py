"""Tests for growth_report_pdf generator (smoke + key sections)."""

from __future__ import annotations

from datetime import date

import pytest


def _minimal_report_data():
    return {
        "student": {
            "name": "王小明",
            "student_no": "S001",
            "classroom_name": "兔兔班",
            "birthday": date(2022, 3, 5),
        },
        "report": {
            "period_label": "2026 春季",
            "period_start": date(2026, 2, 1),
            "period_end": date(2026, 5, 31),
            "report_id": 42,
            "teacher_narrative": "小明本學期表現穩定，社交互動有明顯進步。",
            "generated_on": date(2026, 5, 14),
        },
        "attendance_summary": {
            "total_days": 60,
            "present_days": 55,
            "leave_days": 3,
            "sick_days": 2,
            "absent_days": 0,
            "late_days": 0,
            "present_rate": 0.9167,
        },
        "highlight_observations": [
            {
                "id": 1,
                "observation_date": "2026-04-10",
                "narrative": "今天小明主動分享玩具",
                "domain": "社會",
                "is_highlight": True,
            },
        ],
        "milestones": [
            {"title": "5 歲生日", "achieved_on": "2026-03-05", "icon": "🎂"},
            {"title": "2026/04 滿月全勤", "achieved_on": "2026-04-01", "icon": "🏆"},
        ],
        "measurement_series": {
            "height": [("2026-02-01", 110.0), ("2026-05-01", 111.5)],
            "weight": [("2026-02-01", 18.0), ("2026-05-01", 18.5)],
        },
        "assessments": [
            {"domain": "認知", "rating": 4, "comment": "持續進步"},
        ],
        "activities": [
            {"name": "兒童繪畫班", "registered_at": "2026-03-01"},
        ],
        "institution_name": "義華幼兒園",
    }


def test_generate_pdf_returns_bytes():
    from services.growth_report_pdf import generate_growth_report_pdf

    pdf_bytes = generate_growth_report_pdf(report_data=_minimal_report_data())
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes[:4] == b"%PDF"
    assert len(pdf_bytes) > 1000


def test_generate_pdf_with_empty_sections():
    """空資料也不能 crash."""
    from services.growth_report_pdf import generate_growth_report_pdf

    data = _minimal_report_data()
    data["highlight_observations"] = []
    data["milestones"] = []
    data["measurement_series"] = {"height": [], "weight": []}
    data["assessments"] = []
    data["activities"] = []
    pdf_bytes = generate_growth_report_pdf(report_data=data)
    assert pdf_bytes[:4] == b"%PDF"


def test_generate_pdf_long_narrative_wraps():
    """長字串應自動換行不爆。"""
    from services.growth_report_pdf import generate_growth_report_pdf

    data = _minimal_report_data()
    data["report"]["teacher_narrative"] = "小明是個非常活潑的孩子。" * 30
    pdf_bytes = generate_growth_report_pdf(report_data=data)
    assert pdf_bytes[:4] == b"%PDF"

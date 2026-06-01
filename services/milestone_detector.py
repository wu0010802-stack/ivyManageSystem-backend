"""Pure milestone detection functions.

每個 detect_* 函式：
- 輸入：學生物件（或必要資料）+ optional 參考日期
- 輸出：list of milestone payload dict（可直接傳給 StudentMilestone() 建構）
- 無 DB / FastAPI 依賴，純可測試函式

合規 status 字串：以中文 "出席" / "請假" / "病假" / "遲到" 等為主。
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date as _date
from typing import Iterable

# === 規則常數 ===
PRESENT_STATUS = "出席"  # 與 StudentAttendance default 一致


def detect_first_day(student) -> list[dict]:
    """First day = enrollment_date."""
    enrollment_date = getattr(student, "enrollment_date", None)
    if not enrollment_date:
        return []
    return [
        {
            "student_id": student.id,
            "milestone_type": "first_day",
            "achieved_on": enrollment_date,
            "title": "入學首日",
            "description": "歡迎來到我們的大家庭！",
            "icon": "🌱",
            "source_type": "auto_enrollment",
            "source_ref_type": "student",
            "source_ref_id": student.id,
        }
    ]


def detect_birthdays(student, reference_date: _date) -> list[dict]:
    """為學生產生所有已過的歲數生日 milestone。

    1 歲、2 歲... 至 reference_date 為止。
    處理 2/29 → 平年用 2/28。
    """
    birthday = getattr(student, "birthday", None)
    if not birthday:
        return []
    out: list[dict] = []
    age = 1
    while age <= 20:
        try:
            next_bday = _date(birthday.year + age, birthday.month, birthday.day)
        except ValueError:
            # 2/29 in non-leap year → 2/28
            next_bday = _date(birthday.year + age, 2, 28)
        if next_bday > reference_date:
            break
        out.append(
            {
                "student_id": student.id,
                "milestone_type": "birthday",
                "achieved_on": next_bday,
                "title": f"{age} 歲生日",
                "description": None,
                "icon": "🎂",
                "source_type": "auto_enrollment",
                "source_ref_type": "student_birthday",
                "source_ref_id": student.id,
            }
        )
        age += 1
    return out


def detect_graduation(student) -> list[dict]:
    if getattr(student, "lifecycle_status", None) != "graduated":
        return []
    g_date = getattr(student, "graduation_date", None)
    if not g_date:
        return []
    return [
        {
            "student_id": student.id,
            "milestone_type": "graduation",
            "achieved_on": g_date,
            "title": "畢業典禮",
            "description": "完成幼兒園學業，邁向新階段。",
            "icon": "🎓",
            "source_type": "auto_enrollment",
            "source_ref_type": "student_graduation",
            "source_ref_id": student.id,
        }
    ]


def detect_perfect_attendance_months(
    student_id: int,
    records: Iterable[dict],
    reference_date: _date,
    official_workdays: Iterable[_date],
) -> list[dict]:
    """Records: iterable of {"date": date, "status": str}.

    全勤月 = 已結束的月份中，該月每個官方工作日（``official_workdays``，由 caller
    以 workday_rules 算好傳入）學生都有 status=="出席" 記錄。缺任一天記錄、或有
    任何非「出席」狀態（含遲到 / 缺席 / 請假 / 病假 / 事假）→ 不發章。

    只發「已結束」的月份（月底 < reference_date），未結束的當月不發；非工作日
    （週末 / 假日）無記錄不影響判定。修前以「≥3 筆且全出席」判定，會在記錄稀疏
    或缺席未建檔時誤發。
    """
    present_dates = {r["date"] for r in records if r.get("status") == PRESENT_STATUS}

    workdays_by_month: dict[tuple[int, int], set[_date]] = defaultdict(set)
    for d in official_workdays:
        workdays_by_month[(d.year, d.month)].add(d)

    out: list[dict] = []
    for (yr, mo), wdays in sorted(workdays_by_month.items()):
        if not wdays:
            continue
        # 只發已結束的月份（最後一個日曆日 < reference_date）
        last_day = _date(yr, mo, calendar.monthrange(yr, mo)[1])
        if last_day >= reference_date:
            continue
        # 該月每個官方工作日都必須有「出席」記錄
        if not wdays.issubset(present_dates):
            continue
        out.append(
            {
                "student_id": student_id,
                "milestone_type": "perfect_attendance_month",
                "achieved_on": _date(yr, mo, 1),
                "title": f"{yr}/{mo:02d} 滿月全勤",
                "description": None,
                "icon": "🏆",
                "source_type": "auto_attendance",
                "source_ref_type": "attendance_month",
                "source_ref_id": yr * 100 + mo,
            }
        )
    return out

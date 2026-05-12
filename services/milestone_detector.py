"""Pure milestone detection functions.

每個 detect_* 函式：
- 輸入：學生物件（或必要資料）+ optional 參考日期
- 輸出：list of milestone payload dict（可直接傳給 StudentMilestone() 建構）
- 無 DB / FastAPI 依賴，純可測試函式

合規 status 字串：以中文 "出席" / "請假" / "病假" / "遲到" 等為主。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date as _date
from typing import Iterable

# === 規則常數 ===
PERFECT_ATTENDANCE_MIN_DAYS = 3  # 一個月至少要有 N 筆出勤紀錄才視為全勤候選
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
    student_id: int, records: Iterable[dict], reference_date: _date
) -> list[dict]:
    """Records: iterable of {"date": date, "status": str}.

    Rule: 一個月有 ≥ PERFECT_ATTENDANCE_MIN_DAYS 筆且全 "出席" → 全勤 milestone。
    """
    by_month: dict[tuple[int, int], list[str]] = defaultdict(list)
    for r in records:
        d = r["date"]
        if d > reference_date:
            continue
        by_month[(d.year, d.month)].append(r["status"])

    out: list[dict] = []
    for (yr, mo), statuses in by_month.items():
        if len(statuses) < PERFECT_ATTENDANCE_MIN_DAYS:
            continue
        if all(s == PRESENT_STATUS for s in statuses):
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

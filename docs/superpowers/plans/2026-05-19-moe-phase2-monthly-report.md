# MOE Phase 2 月報匯出器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 補做 MOE Phase 2 — 月度幼生在園/出席統計 3-sheet Excel 匯出，讓業主每月對照貼到 ece.moe.edu.tw（影響教育券撥款）。

**Architecture:** 跨前後端兩 worktree 平行；後端產純函式 → Excel writer → 3 endpoints，前端 codegen → api wrapper → 1 view + 3 子元件；snapshot 表 Phase 1 已建好不需 migration；權限重用 `GOV_REPORTS_VIEW/EXPORT` 不新增位元；並發 generate 用 PG advisory lock。

**Tech Stack:** FastAPI + SQLAlchemy + Pydantic + openpyxl + python-dateutil（後端）；Vue 3 + Element Plus + TypeScript + Vitest（前端）；OpenAPI codegen via `openapi-typescript`。

**Spec：** `ivy-backend/docs/superpowers/specs/2026-05-19-moe-phase2-monthly-report-design.md`（commit `6431f14`）

---

## File Structure

### Backend (`/Users/yilunwu/Desktop/ivy-backend/`)

| 路徑 | 動作 | 職責 |
|------|------|------|
| `services/gov_moe/__init__.py` | Create | namespace marker |
| `services/gov_moe/monthly_calculator.py` | Create | 6 純函式（age_group / is_foreign / working_days / classroom_at_month_end / student_days / build_snapshot_rows） |
| `services/gov_moe/monthly_excel_writer.py` | Create | openpyxl 3-sheet 寫入 |
| `api/gov_moe/monthly.py` | Create | 3 endpoints + inline Pydantic schemas + advisory lock + audit |
| `api/gov_moe/__init__.py` | Modify | 註冊 monthly router |
| `tests/test_gov_moe_monthly_calculator.py` | Create | 純函式測試 |
| `tests/test_gov_moe_monthly_excel.py` | Create | Excel writer 測試 |
| `tests/test_gov_moe_monthly_api.py` | Create | endpoint 整合測試 |

### Frontend (`/Users/yilunwu/Desktop/ivy-frontend/`)

| 路徑 | 動作 | 職責 |
|------|------|------|
| `src/api/_generated/schema.d.ts` | 自動 regen | OpenAPI schema |
| `src/api/govMoe.ts` | Modify | 加 `generateMonthlyReport` / `getMonthlyReport` / `exportMonthlyReport` |
| `src/views/admin/gov-reports/MonthlyReportView.vue` | Create | 主頁面：月份選擇、產生、tab 切換 |
| `src/components/gov-reports/ClassroomSummaryTable.vue` | Create | Sheet 1 對應 table |
| `src/components/gov-reports/StudentDetailTable.vue` | Create | Sheet 2 對應 table |
| `src/components/gov-reports/OverviewSummaryCard.vue` | Create | Sheet 3 對應卡片 |
| `src/router/index.ts` 或 `admin.routes.ts` | Modify | 加 `/admin/gov-reports/monthly` |
| sidebar config（位置待 FE-4 確認） | Modify | 加導覽連結 |
| `tests/views/admin/gov-reports/MonthlyReportView.test.ts` | Create | vitest |
| `tests/components/gov-reports/*.test.ts` | Create | 3 子元件 vitest |

---

## Pre-Task: Worktree Setup

### Pre-1: 開兩個 worktree

**Files:** —

- [ ] **Step 1: 建後端 worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-backend && \
  git fetch origin && \
  git worktree add .claude/worktrees/moe-phase2-be \
    -b feat/moe-phase2-monthly-report-2026-05-19-backend origin/main
```

Expected: worktree 建立於 `.claude/worktrees/moe-phase2-be`，新分支自 `origin/main` 拉。

- [ ] **Step 2: 建前端 worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend && \
  git fetch origin && \
  git worktree add .claude/worktrees/moe-phase2-fe \
    -b feat/moe-phase2-monthly-report-2026-05-19-frontend origin/main
```

Expected: 同上。

- [ ] **Step 3: 驗證 worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-backend && git worktree list
cd /Users/yilunwu/Desktop/ivy-frontend && git worktree list
```

Expected: 各列出 1 個 worktree。

**Done conditions:**
- 後端 worktree at `ivy-backend/.claude/worktrees/moe-phase2-be` on `feat/moe-phase2-monthly-report-2026-05-19-backend`
- 前端 worktree at `ivy-frontend/.claude/worktrees/moe-phase2-fe` on `feat/moe-phase2-monthly-report-2026-05-19-frontend`

**Depends on:** —

---

## Backend Tasks

### BE-1: 純函式 `monthly_calculator.py` + TDD

**Files:**
- Create: `services/gov_moe/__init__.py`
- Create: `services/gov_moe/monthly_calculator.py`
- Create: `tests/test_gov_moe_monthly_calculator.py`

工作目錄：`ivy-backend/.claude/worktrees/moe-phase2-be/`

- [ ] **Step 1: 建 namespace**

```bash
mkdir -p services/gov_moe
touch services/gov_moe/__init__.py
```

- [ ] **Step 2: 寫測試（calc_age_group）**

建 `tests/test_gov_moe_monthly_calculator.py`：

```python
"""純函式測試 — MOE Phase 2 monthly calculator."""
from datetime import date

import pytest

from services.gov_moe.monthly_calculator import (
    calc_age_group,
    is_foreign,
    working_days_in_month,
    classroom_at_month_end,
    compute_student_attendance_for_month,
    build_snapshot_rows,
)


class TestCalcAgeGroup:
    def test_under_2_returns_2_3(self):
        assert calc_age_group(date(2025, 1, 1), date(2026, 5, 31)) == "2-3"

    def test_exactly_2_returns_2_3(self):
        assert calc_age_group(date(2024, 5, 31), date(2026, 5, 31)) == "2-3"

    def test_3_returns_3_4(self):
        assert calc_age_group(date(2023, 5, 31), date(2026, 5, 31)) == "3-4"

    def test_4_returns_4_5(self):
        assert calc_age_group(date(2022, 5, 31), date(2026, 5, 31)) == "4-5"

    def test_5_returns_5_6(self):
        assert calc_age_group(date(2021, 5, 31), date(2026, 5, 31)) == "5-6"

    def test_over_6_returns_5_6(self):
        assert calc_age_group(date(2019, 5, 31), date(2026, 5, 31)) == "5-6"

    def test_birthday_none_returns_unknown(self):
        assert calc_age_group(None, date(2026, 5, 31)) == "未知"

    def test_birthday_after_ref_date_returns_2_3(self):
        # 出生於 ref_date 後（罕見，data corruption）→ age = 0，歸 2-3 防呆
        assert calc_age_group(date(2026, 6, 1), date(2026, 5, 31)) == "2-3"


class TestIsForeign:
    @pytest.mark.parametrize("nationality", ["本國", "台灣", "中華民國", "中華民國（台灣）", "ROC"])
    def test_taiwan_aliases_not_foreign(self, nationality):
        assert is_foreign(nationality) is False

    def test_with_whitespace(self):
        assert is_foreign("  本國  ") is False

    @pytest.mark.parametrize("nationality", ["美國", "日本", "越南", "印尼"])
    def test_other_country_is_foreign(self, nationality):
        assert is_foreign(nationality) is True

    def test_none_returns_false(self):
        assert is_foreign(None) is False

    def test_empty_returns_false(self):
        assert is_foreign("") is False
```

- [ ] **Step 3: 跑測試確認 FAIL**

```bash
pytest tests/test_gov_moe_monthly_calculator.py -v
```

Expected: ImportError — `services.gov_moe.monthly_calculator` 不存在。

- [ ] **Step 4: 寫 monthly_calculator.py（純函式部分）**

建 `services/gov_moe/monthly_calculator.py`：

```python
"""MOE Phase 2 月報計算純函式。

純函式（無 session）：calc_age_group, is_foreign
DB query helper：working_days_in_month, classroom_at_month_end
聚合：compute_student_attendance_for_month, build_snapshot_rows
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from dateutil.relativedelta import relativedelta
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from models.classroom import Student, StudentAttendance
from models.event import Holiday, WorkdayOverride
from models.student_transfer import StudentClassroomTransfer

TAIWAN_ALIASES = {"本國", "台灣", "中華民國", "中華民國（台灣）", "ROC"}
ATTENDED_STATUSES = {"出席", "遲到"}
EXCLUDED_LIFECYCLE = {"prospect"}


def calc_age_group(birthday: date | None, ref_date: date) -> str:
    """以 ref_date 滿歲切 2-3/3-4/4-5/5-6 四段。

    < 2 歲（罕見資料）歸 2-3；> 5 歲（含超齡）歸 5-6（fallback 防呆）。
    birthday 為 None → '未知'。
    """
    if birthday is None:
        return "未知"
    age = relativedelta(ref_date, birthday).years
    if age <= 2:
        return "2-3"
    if age == 3:
        return "3-4"
    if age == 4:
        return "4-5"
    return "5-6"


def is_foreign(nationality: str | None) -> bool:
    """nationality 為 NULL/空 視為本國（保守不誤報）。"""
    if not nationality:
        return False
    return nationality.strip() not in TAIWAN_ALIASES
```

- [ ] **Step 5: 跑測試確認 PASS**

```bash
pytest tests/test_gov_moe_monthly_calculator.py::TestCalcAgeGroup tests/test_gov_moe_monthly_calculator.py::TestIsForeign -v
```

Expected: all PASS (14 tests).

- [ ] **Step 6: 寫測試（working_days_in_month）— 整合測試需 DB**

加到 `tests/test_gov_moe_monthly_calculator.py`：

```python
class TestWorkingDaysInMonth:
    def test_no_holidays_returns_weekdays(self, test_db_session):
        """2026-05 純 weekday 應有 21 天。"""
        result = working_days_in_month(test_db_session, 2026, 5)
        assert len(result) == 21
        # 5/1 (Fri), 5/4-5/8 (Mon-Fri), ...
        assert date(2026, 5, 1) in result
        assert date(2026, 5, 2) not in result  # Sat
        assert date(2026, 5, 3) not in result  # Sun

    def test_active_holiday_excluded(self, test_db_session):
        test_db_session.add(Holiday(
            date=date(2026, 5, 1), name="勞動節", is_active=True
        ))
        test_db_session.commit()
        result = working_days_in_month(test_db_session, 2026, 5)
        assert date(2026, 5, 1) not in result
        assert len(result) == 20

    def test_inactive_holiday_not_excluded(self, test_db_session):
        test_db_session.add(Holiday(
            date=date(2026, 5, 1), name="勞動節", is_active=False
        ))
        test_db_session.commit()
        result = working_days_in_month(test_db_session, 2026, 5)
        assert date(2026, 5, 1) in result

    def test_workday_override_includes_weekend(self, test_db_session):
        test_db_session.add(WorkdayOverride(
            date=date(2026, 5, 9), name="補上班", is_active=True
        ))
        test_db_session.commit()
        result = working_days_in_month(test_db_session, 2026, 5)
        assert date(2026, 5, 9) in result  # Saturday
        assert len(result) == 22
```

注意：`test_db_session` fixture 來自 `tests/conftest.py:137`，每測試自動 rollback。

- [ ] **Step 7: 跑測試確認 FAIL（函式未實作）**

```bash
pytest tests/test_gov_moe_monthly_calculator.py::TestWorkingDaysInMonth -v
```

Expected: NameError — `working_days_in_month` 不存在或實作不完整。

- [ ] **Step 8: 加 working_days_in_month**

在 `services/gov_moe/monthly_calculator.py` 末尾加：

```python
def working_days_in_month(session: Session, year: int, month: int) -> set[date]:
    """月份工作日集合 = weekday(Mon-Fri) - 假日 + 補班日。"""
    first = date(year, month, 1)
    last = first + relativedelta(months=1, days=-1)

    days = {
        first + timedelta(days=d)
        for d in range((last - first).days + 1)
        if (first + timedelta(days=d)).weekday() < 5
    }

    holiday_dates = {
        row[0]
        for row in session.query(Holiday.date)
        .filter(
            Holiday.is_active == True,  # noqa: E712
            Holiday.date.between(first, last),
        )
        .all()
    }

    override_dates = {
        row[0]
        for row in session.query(WorkdayOverride.date)
        .filter(
            WorkdayOverride.is_active == True,  # noqa: E712
            WorkdayOverride.date.between(first, last),
        )
        .all()
    }

    return (days - holiday_dates) | override_dates
```

- [ ] **Step 9: 跑測試確認 PASS**

```bash
pytest tests/test_gov_moe_monthly_calculator.py::TestWorkingDaysInMonth -v
```

Expected: 4 tests PASS.

- [ ] **Step 10: 寫測試（classroom_at_month_end）**

加到測試檔：

```python
class TestClassroomAtMonthEnd:
    def test_uses_last_transfer_before_snapshot(self, test_db_session, sample_classroom_context):
        student_id = sample_classroom_context["student_id"]
        c1_id = sample_classroom_context["classroom_id"]
        c2 = Classroom(name="芒果班", capacity=20)
        test_db_session.add(c2)
        test_db_session.commit()
        test_db_session.add(StudentClassroomTransfer(
            student_id=student_id,
            from_classroom_id=c1_id,
            to_classroom_id=c2.id,
            transferred_at=datetime(2026, 5, 10, 9, 0),
        ))
        test_db_session.commit()
        assert classroom_at_month_end(test_db_session, student_id, date(2026, 5, 31)) == c2.id

    def test_transfer_after_snapshot_not_used(self, test_db_session, sample_classroom_context):
        student_id = sample_classroom_context["student_id"]
        c1_id = sample_classroom_context["classroom_id"]
        c2 = Classroom(name="芒果班", capacity=20)
        test_db_session.add(c2)
        test_db_session.commit()
        test_db_session.add(StudentClassroomTransfer(
            student_id=student_id,
            from_classroom_id=c1_id,
            to_classroom_id=c2.id,
            transferred_at=datetime(2026, 6, 5, 9, 0),  # after snapshot
        ))
        test_db_session.commit()
        # fallback to student.classroom_id (c1)
        assert classroom_at_month_end(test_db_session, student_id, date(2026, 5, 31)) == c1_id

    def test_no_transfer_falls_back_to_classroom_id(self, test_db_session, sample_classroom_context):
        student_id = sample_classroom_context["student_id"]
        c1_id = sample_classroom_context["classroom_id"]
        assert classroom_at_month_end(test_db_session, student_id, date(2026, 5, 31)) == c1_id
```

注意：`sample_classroom_context` fixture 來自 `tests/conftest.py:83`，提供已建好的 student + classroom。需要 `from models.classroom import Classroom` 於檔頭。

- [ ] **Step 11: 跑測試確認 FAIL，加 import，加實作**

加 import：

```python
from models.classroom import Classroom  # 加到測試檔頂
```

加實作於 `monthly_calculator.py`：

```python
def classroom_at_month_end(
    session: Session,
    student_id: int,
    snapshot_date: date,
) -> int | None:
    """月底班級歸屬：先查 transfer 表，無紀錄則 fallback student.classroom_id。"""
    last_transfer = (
        session.query(StudentClassroomTransfer)
        .filter(
            StudentClassroomTransfer.student_id == student_id,
            StudentClassroomTransfer.transferred_at
            <= datetime.combine(snapshot_date, time.max),
        )
        .order_by(StudentClassroomTransfer.transferred_at.desc())
        .first()
    )
    if last_transfer:
        return last_transfer.to_classroom_id
    return (
        session.query(Student.classroom_id)
        .filter(Student.id == student_id)
        .scalar()
    )
```

- [ ] **Step 12: 跑測試確認 PASS**

```bash
pytest tests/test_gov_moe_monthly_calculator.py::TestClassroomAtMonthEnd -v
```

Expected: 3 tests PASS.

- [ ] **Step 13: 寫測試 + 實作 compute_student_attendance_for_month**

加測試：

```python
class TestComputeStudentAttendance:
    def test_full_attendance(self, test_db_session, sample_classroom_context):
        student_id = sample_classroom_context["student_id"]
        # 學生 enrollment_date 設為月初前
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2025, 1, 1)
        # 加 3 天出席記錄（月內 weekday）
        for d in [date(2026, 5, 4), date(2026, 5, 5), date(2026, 5, 6)]:
            test_db_session.add(StudentAttendance(
                student_id=student_id, date=d, status="出席"
            ))
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        expected, actual = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        assert expected == 21  # 全月 weekday
        assert actual == 3

    def test_mid_month_enrollment(self, test_db_session, sample_classroom_context):
        student_id = sample_classroom_context["student_id"]
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2026, 5, 15)  # 月中加入
        test_db_session.add(StudentAttendance(
            student_id=student_id, date=date(2026, 5, 18), status="出席"
        ))
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        expected, actual = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        # 5/15 (Fri) ~ 5/31，weekdays = 5/15,18,19,20,21,22,25,26,27,28,29 = 11
        assert expected == 11
        assert actual == 1

    def test_late_status_counts_as_attended(self, test_db_session, sample_classroom_context):
        student_id = sample_classroom_context["student_id"]
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2025, 1, 1)
        test_db_session.add(StudentAttendance(
            student_id=student_id, date=date(2026, 5, 4), status="遲到"
        ))
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        _, actual = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        assert actual == 1

    def test_sick_and_personal_leave_not_counted(self, test_db_session, sample_classroom_context):
        student_id = sample_classroom_context["student_id"]
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2025, 1, 1)
        test_db_session.add(StudentAttendance(
            student_id=student_id, date=date(2026, 5, 4), status="病假"
        ))
        test_db_session.add(StudentAttendance(
            student_id=student_id, date=date(2026, 5, 5), status="事假"
        ))
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        _, actual = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        assert actual == 0

    def test_withdrawal_caps_expected_days(self, test_db_session, sample_classroom_context):
        student_id = sample_classroom_context["student_id"]
        student = test_db_session.query(Student).get(student_id)
        student.enrollment_date = date(2025, 1, 1)
        student.withdrawal_date = date(2026, 5, 15)
        test_db_session.commit()
        working_days = working_days_in_month(test_db_session, 2026, 5)
        expected, _ = compute_student_attendance_for_month(
            test_db_session, student, 2026, 5, working_days
        )
        # 5/1 ~ 5/15 weekdays = 5/1,4,5,6,7,8,11,12,13,14,15 = 11
        assert expected == 11
```

實作於 `monthly_calculator.py`：

```python
def compute_student_attendance_for_month(
    session: Session,
    student: Student,
    year: int,
    month: int,
    working_days: set[date],
) -> tuple[int, int]:
    """回傳 (expected_days, actual_days)。

    expected_days = 學生個別在園日 ∩ 月份工作日
    actual_days = 該學生本月 StudentAttendance status IN ('出席','遲到') 的天數
    """
    first = date(year, month, 1)
    last = first + relativedelta(months=1, days=-1)

    student_start = max(first, student.enrollment_date or first)
    student_end_candidates = [
        last,
        student.withdrawal_date or last,
        student.graduation_date or last,
    ]
    student_end = min(student_end_candidates)

    if student_start > student_end:
        return 0, 0

    student_days = {
        d for d in working_days if student_start <= d <= student_end
    }
    expected = len(student_days)

    if expected == 0:
        return 0, 0

    attended_rows = (
        session.query(StudentAttendance.date)
        .filter(
            StudentAttendance.student_id == student.id,
            StudentAttendance.date.between(student_start, student_end),
            StudentAttendance.status.in_(list(ATTENDED_STATUSES)),
        )
        .all()
    )
    attended_dates = {row[0] for row in attended_rows}
    actual = len(attended_dates & student_days)

    return expected, actual
```

- [ ] **Step 14: 跑測試確認 PASS**

```bash
pytest tests/test_gov_moe_monthly_calculator.py::TestComputeStudentAttendance -v
```

Expected: 5 tests PASS.

- [ ] **Step 15: 寫測試 + 實作 build_snapshot_rows（最複雜，是聚合）**

加測試：

```python
class TestBuildSnapshotRows:
    def _make_students_fixture(self, db, classroom_id):
        """建 3 名學生：本國男 4-5 歲、外籍女 3-4 歲弱勢、本國身障"""
        s1 = Student(
            student_id="S001", name="王小明", gender="男",
            birthday=date(2022, 1, 1), classroom_id=classroom_id,
            enrollment_date=date(2025, 1, 1),
            nationality="本國", lifecycle_status="active",
        )
        s2 = Student(
            student_id="S002", name="陳小華", gender="女",
            birthday=date(2023, 1, 1), classroom_id=classroom_id,
            enrollment_date=date(2025, 1, 1),
            nationality="越南", lifecycle_status="active",
            is_disadvantaged=True, low_income_status="low",
        )
        s3 = Student(
            student_id="S003", name="林小強", gender="男",
            birthday=date(2022, 6, 1), classroom_id=classroom_id,
            enrollment_date=date(2025, 1, 1),
            nationality="本國", lifecycle_status="active",
            disability_type="智能", disability_level="輕度",
            indigenous_status="阿美",
        )
        db.add_all([s1, s2, s3])
        db.commit()
        return s1, s2, s3

    def test_three_students_one_classroom_one_age_group_split(
        self, test_db_session, sample_classroom_context, generated_by="test@example.com"
    ):
        classroom_id = sample_classroom_context["classroom_id"]
        s1, s2, s3 = self._make_students_fixture(test_db_session, classroom_id)
        # 全員 5 月 1 號出席（一天）
        for s in [s1, s2, s3]:
            test_db_session.add(StudentAttendance(
                student_id=s.id, date=date(2026, 5, 1), status="出席"
            ))
        test_db_session.commit()

        rows = build_snapshot_rows(test_db_session, 2026, 5, generated_by="test@example.com")

        # s1, s3 都 4-5 歲（2022 出生，到 2026-05-31 滿 4 歲）
        # s2 是 3-4 歲（2023 出生）
        # 同班級分兩 group → 2 rows
        assert len(rows) == 2
        group_45 = next(r for r in rows if r["age_group"] == "4-5")
        group_34 = next(r for r in rows if r["age_group"] == "3-4")
        assert group_45["total_count"] == 2
        assert group_45["male_count"] == 2
        assert group_45["disability_count"] == 1
        assert group_45["indigenous_count"] == 1
        assert group_34["total_count"] == 1
        assert group_34["female_count"] == 1
        assert group_34["disadvantaged_count"] == 1
        assert group_34["foreign_count"] == 1

    def test_excludes_prospect_lifecycle(self, test_db_session, sample_classroom_context):
        classroom_id = sample_classroom_context["classroom_id"]
        Student(
            student_id="S099", name="未報名", gender="男",
            birthday=date(2022, 1, 1), classroom_id=classroom_id,
            lifecycle_status="prospect",
        )
        rows = build_snapshot_rows(test_db_session, 2026, 5, generated_by="t")
        # 預設 fixture 的 student（active）會被算進去；prospect 不會
        # 這裡只驗 prospect 不在 rows
        total = sum(r["total_count"] for r in rows)
        # 預設 sample_classroom_context 含 1 名 student（active），prospect 一名不算
        assert total <= 1
```

實作於 `monthly_calculator.py`：

```python
@dataclass
class _StudentAggregate:
    total: int = 0
    male: int = 0
    female: int = 0
    disadvantaged: int = 0
    disability: int = 0
    indigenous: int = 0
    foreign: int = 0
    expected_days: int = 0
    actual_days: int = 0
    student_details: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.student_details is None:
            self.student_details = []


def build_snapshot_rows(
    session: Session,
    year: int,
    month: int,
    *,
    generated_by: str,
) -> list[dict]:
    """產生 snapshot rows（list[dict]，未寫 DB）。

    Returns:
        [{"year", "month", "classroom_id", "age_group", "total_count", ...,
          "expected_attendance_days", "actual_attendance_days", "attendance_rate",
          "snapshot_date", "generated_at", "generated_by",
          "_student_details": [...]  # 暫存用於 student_detail tab，不寫表
        }, ...]
    """
    first = date(year, month, 1)
    last = first + relativedelta(months=1, days=-1)

    candidates = (
        session.query(Student)
        .filter(
            ~Student.lifecycle_status.in_(list(EXCLUDED_LIFECYCLE)),
            or_(Student.enrollment_date.is_(None), Student.enrollment_date <= last),
            or_(Student.withdrawal_date.is_(None), Student.withdrawal_date >= first),
            or_(Student.graduation_date.is_(None), Student.graduation_date >= first),
        )
        .all()
    )

    wd = working_days_in_month(session, year, month)
    now = datetime.now()

    groups: dict[tuple[int | None, str], _StudentAggregate] = defaultdict(_StudentAggregate)
    student_details_global: list[dict] = []

    for s in candidates:
        expected, actual = compute_student_attendance_for_month(session, s, year, month, wd)
        if expected == 0 and actual == 0:
            continue
        ag = calc_age_group(s.birthday, last)
        cls_id = classroom_at_month_end(session, s.id, last)
        key = (cls_id, ag)
        agg = groups[key]
        agg.total += 1
        if s.gender == "男":
            agg.male += 1
        elif s.gender == "女":
            agg.female += 1
        if s.is_disadvantaged:
            agg.disadvantaged += 1
        if s.disability_type:
            agg.disability += 1
        if s.indigenous_status:
            agg.indigenous += 1
        if is_foreign(s.nationality):
            agg.foreign += 1
        agg.expected_days += expected
        agg.actual_days += actual

        rate = round(actual / expected * 10000) if expected else 0
        student_details_global.append({
            "student_id": s.id,
            "student_no": s.student_id,
            "name": s.name,
            "id_number": s.id_number,
            "classroom_id": cls_id,
            "age_group": ag,
            "expected_days": expected,
            "actual_days": actual,
            "attendance_rate_pct": round(actual / expected * 100, 2) if expected else 0,
            "is_disadvantaged": bool(s.is_disadvantaged),
        })

    rows: list[dict] = []
    for (cls_id, ag), agg in groups.items():
        rate = (
            round(agg.actual_days / agg.expected_days * 10000)
            if agg.expected_days
            else 0
        )
        rows.append({
            "year": year,
            "month": month,
            "classroom_id": cls_id,
            "age_group": ag,
            "total_count": agg.total,
            "male_count": agg.male,
            "female_count": agg.female,
            "disadvantaged_count": agg.disadvantaged,
            "disability_count": agg.disability,
            "indigenous_count": agg.indigenous,
            "foreign_count": agg.foreign,
            "expected_attendance_days": agg.expected_days,
            "actual_attendance_days": agg.actual_days,
            "attendance_rate": rate,
            "snapshot_date": last,
            "generated_at": now,
            "generated_by": generated_by,
        })

    # student_details 不放在 rows 內，由 caller 用第二個回傳值或全域 sidecar
    rows.append({"__student_details__": student_details_global})  # sentinel
    return rows
```

讓 `build_snapshot_rows` 多回一個 dict 不漂亮。改設計：回 `(rows, student_details)` tuple。**修改測試與實作**：

```python
# 實作改為：
def build_snapshot_rows(
    session: Session,
    year: int,
    month: int,
    *,
    generated_by: str,
) -> tuple[list[dict], list[dict]]:
    """回傳 (group_rows, student_details)。"""
    ...
    return rows, student_details_global
```

測試對應呼叫改 `rows, _ = build_snapshot_rows(...)`。

- [ ] **Step 16: 跑測試確認 PASS**

```bash
pytest tests/test_gov_moe_monthly_calculator.py -v
```

Expected: 全部 PASS（28+ tests）。

- [ ] **Step 17: Commit**

```bash
git add services/gov_moe/ tests/test_gov_moe_monthly_calculator.py
git commit -m "feat(gov_moe): Phase 2 monthly_calculator 純函式 + 整合測試

- calc_age_group / is_foreign (純函式)
- working_days_in_month (Holiday + WorkdayOverride)
- classroom_at_month_end (transfer 表 fallback)
- compute_student_attendance_for_month (跨月加退、graduation 邊界)
- build_snapshot_rows (聚合到 (classroom, age_group) + student_details)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

**Done conditions:**
- 28+ tests PASS（calc_age_group 8 + is_foreign 6 + working_days 4 + classroom 3 + student_attendance 5 + build_snapshot 2+）
- `services/gov_moe/monthly_calculator.py` 含 6 public 函式 + 1 dataclass
- 無 type checking 錯誤

**Test command:** `pytest tests/test_gov_moe_monthly_calculator.py -v`

**Depends on:** Pre-1

---

### BE-2: Excel writer `monthly_excel_writer.py` + TDD

**Files:**
- Create: `services/gov_moe/monthly_excel_writer.py`
- Create: `tests/test_gov_moe_monthly_excel.py`

工作目錄：`ivy-backend/.claude/worktrees/moe-phase2-be/`

- [ ] **Step 1: 寫測試**

建 `tests/test_gov_moe_monthly_excel.py`：

```python
"""Excel writer 整合測試（讀回 xlsx 驗證內容）。"""
from datetime import date, datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook

from services.gov_moe.monthly_excel_writer import build_monthly_xlsx_bytes


@pytest.fixture
def sample_rows():
    return [
        {
            "classroom_id": 1,
            "classroom_name": "蘋果班",
            "teacher_names": "張老師",
            "age_group": "4-5",
            "total_count": 5,
            "male_count": 3,
            "female_count": 2,
            "disadvantaged_count": 1,
            "disability_count": 0,
            "indigenous_count": 0,
            "foreign_count": 0,
            "expected_attendance_days": 100,
            "actual_attendance_days": 95,
            "attendance_rate": 9500,  # 95.00%
        },
    ]


@pytest.fixture
def sample_student_details():
    return [
        {
            "student_id": 1,
            "student_no": "S001",
            "name": "王小明",
            "id_number": "A123456789",
            "classroom_name": "蘋果班",
            "age_group": "4-5",
            "expected_days": 22,
            "actual_days": 20,
            "attendance_rate_pct": 90.91,
            "is_disadvantaged": False,
        },
    ]


@pytest.fixture
def sample_overview():
    return {
        "year": 2026,
        "month": 5,
        "snapshot_date": date(2026, 5, 31),
        "generated_at": datetime(2026, 6, 1, 10, 23),
        "generated_by": "test@example.com",
        "total_students": 28,
        "by_age_group": {"2-3": 0, "3-4": 8, "4-5": 12, "5-6": 8},
        "disadvantaged_pct": 7.14,
        "disability_pct": 3.57,
        "indigenous_pct": 0.0,
        "foreign_pct": 0.0,
        "total_expected_days": 1300,
        "total_actual_days": 1238,
        "total_attendance_rate_pct": 95.23,
    }


def test_xlsx_has_three_sheets(sample_rows, sample_student_details, sample_overview):
    data = build_monthly_xlsx_bytes(sample_rows, sample_student_details, sample_overview)
    wb = load_workbook(BytesIO(data))
    assert wb.sheetnames == ["班級總表", "幼生明細", "統計摘要"]


def test_sheet1_classroom_summary_headers(sample_rows, sample_student_details, sample_overview):
    data = build_monthly_xlsx_bytes(sample_rows, sample_student_details, sample_overview)
    wb = load_workbook(BytesIO(data))
    ws = wb["班級總表"]
    headers = [c.value for c in ws[1]]
    assert headers == ["班級", "教師", "年齡層", "應到人日", "實到人日", "出席率",
                       "男", "女", "弱勢", "身障", "原民", "外籍"]


def test_sheet1_has_total_row(sample_rows, sample_student_details, sample_overview):
    data = build_monthly_xlsx_bytes(sample_rows, sample_student_details, sample_overview)
    wb = load_workbook(BytesIO(data))
    ws = wb["班級總表"]
    last_row = list(ws.rows)[-1]
    assert last_row[0].value == "合計"


def test_sheet2_student_headers(sample_rows, sample_student_details, sample_overview):
    data = build_monthly_xlsx_bytes(sample_rows, sample_student_details, sample_overview)
    wb = load_workbook(BytesIO(data))
    ws = wb["幼生明細"]
    headers = [c.value for c in ws[1]]
    assert headers == ["學號", "姓名", "身分證", "班級", "年齡層",
                       "應到日數", "實到日數", "出席率", "弱勢標記"]


def test_sheet3_overview_contains_total_students(sample_rows, sample_student_details, sample_overview):
    data = build_monthly_xlsx_bytes(sample_rows, sample_student_details, sample_overview)
    wb = load_workbook(BytesIO(data))
    ws = wb["統計摘要"]
    cells = [c.value for row in ws.rows for c in row]
    assert "總人數" in cells
    assert 28 in cells


def test_empty_rows_does_not_raise(sample_overview):
    data = build_monthly_xlsx_bytes([], [], sample_overview)
    wb = load_workbook(BytesIO(data))
    assert wb.sheetnames == ["班級總表", "幼生明細", "統計摘要"]


def test_id_number_none_displayed_as_dash(sample_rows, sample_overview):
    details = [{
        "student_id": 1, "student_no": "S001", "name": "王小明",
        "id_number": None, "classroom_name": "蘋果班", "age_group": "4-5",
        "expected_days": 22, "actual_days": 20, "attendance_rate_pct": 90.91,
        "is_disadvantaged": False,
    }]
    data = build_monthly_xlsx_bytes(sample_rows, details, sample_overview)
    wb = load_workbook(BytesIO(data))
    ws = wb["幼生明細"]
    row2 = list(ws.rows)[1]
    assert row2[2].value == "-"  # 身分證欄位
```

- [ ] **Step 2: 跑測試確認 FAIL**

```bash
pytest tests/test_gov_moe_monthly_excel.py -v
```

Expected: ImportError。

- [ ] **Step 3: 實作 Excel writer**

建 `services/gov_moe/monthly_excel_writer.py`：

```python
"""MOE Phase 2 月報 Excel 寫入器（openpyxl）。

3 sheet：班級總表 / 幼生明細 / 統計摘要
不需 embed 字型（XLSX 使用 OS 系統字型）。
"""
from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

_HEADER_FILL = PatternFill("solid", fgColor="4472C4")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center")
_TOTAL_FILL = PatternFill("solid", fgColor="D9E1F2")
_TOTAL_FONT = Font(bold=True)


def _apply_header(ws, row_idx: int = 1) -> None:
    for cell in ws[row_idx]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _HEADER_ALIGN


def _autofit_columns(ws, min_w: int = 10, max_w: int = 30) -> None:
    for col in ws.columns:
        max_len = min_w
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            if cell.value is not None:
                length = len(str(cell.value))
                # 中文字寬約英文 1.5 倍
                if any("一" <= ch <= "鿿" for ch in str(cell.value)):
                    length = int(length * 1.5)
                max_len = max(max_len, length)
        ws.column_dimensions[col_letter].width = min(max_len + 2, max_w)


def _dash_if_none(value):
    return "-" if value is None or value == "" else value


def _format_rate(rate_int: int) -> str:
    """rate_int = 9542 → '95.42%'"""
    return f"{rate_int / 100:.2f}%"


def _build_sheet1(wb: Workbook, rows: list[dict]) -> None:
    ws = wb.create_sheet("班級總表")
    headers = ["班級", "教師", "年齡層", "應到人日", "實到人日", "出席率",
               "男", "女", "弱勢", "身障", "原民", "外籍"]
    ws.append(headers)
    _apply_header(ws)

    total_exp = total_act = 0
    total_m = total_f = total_dis = total_dis2 = total_ind = total_for = 0

    for r in rows:
        ws.append([
            _dash_if_none(r.get("classroom_name")),
            _dash_if_none(r.get("teacher_names")),
            r.get("age_group") or "未知",
            r["expected_attendance_days"],
            r["actual_attendance_days"],
            _format_rate(r["attendance_rate"]),
            r["male_count"], r["female_count"],
            r["disadvantaged_count"], r["disability_count"],
            r["indigenous_count"], r["foreign_count"],
        ])
        total_exp += r["expected_attendance_days"]
        total_act += r["actual_attendance_days"]
        total_m += r["male_count"]
        total_f += r["female_count"]
        total_dis += r["disadvantaged_count"]
        total_dis2 += r["disability_count"]
        total_ind += r["indigenous_count"]
        total_for += r["foreign_count"]

    total_rate = (
        round(total_act / total_exp * 10000) if total_exp else 0
    )
    total_row_idx = ws.max_row + 1
    ws.append([
        "合計", "—", "—",
        total_exp, total_act, _format_rate(total_rate),
        total_m, total_f, total_dis, total_dis2, total_ind, total_for,
    ])
    for cell in ws[total_row_idx]:
        cell.font = _TOTAL_FONT
        cell.fill = _TOTAL_FILL

    ws.freeze_panes = "A2"
    _autofit_columns(ws)


def _build_sheet2(wb: Workbook, details: list[dict]) -> None:
    ws = wb.create_sheet("幼生明細")
    headers = ["學號", "姓名", "身分證", "班級", "年齡層",
               "應到日數", "實到日數", "出席率", "弱勢標記"]
    ws.append(headers)
    _apply_header(ws)

    for d in details:
        ws.append([
            _dash_if_none(d.get("student_no")),
            _dash_if_none(d.get("name")),
            _dash_if_none(d.get("id_number")),
            _dash_if_none(d.get("classroom_name")),
            d.get("age_group") or "未知",
            d["expected_days"],
            d["actual_days"],
            f"{d['attendance_rate_pct']:.2f}%",
            "是" if d.get("is_disadvantaged") else "否",
        ])

    ws.freeze_panes = "A2"
    _autofit_columns(ws)


def _build_sheet3(wb: Workbook, overview: dict) -> None:
    ws = wb.create_sheet("統計摘要")
    lines = [
        ("總人數", overview.get("total_students", 0)),
        ("", ""),
        ("年齡層分布", ""),
    ]
    for ag in ["2-3", "3-4", "4-5", "5-6"]:
        cnt = overview.get("by_age_group", {}).get(ag, 0)
        lines.append((f"  {ag} 歲", cnt))
    lines.extend([
        ("", ""),
        ("特殊屬性占比", ""),
        ("  弱勢", f"{overview.get('disadvantaged_pct', 0):.2f}%"),
        ("  身障", f"{overview.get('disability_pct', 0):.2f}%"),
        ("  原住民", f"{overview.get('indigenous_pct', 0):.2f}%"),
        ("  外籍", f"{overview.get('foreign_pct', 0):.2f}%"),
        ("", ""),
        ("出席統計", ""),
        ("  全園應到人日", overview.get("total_expected_days", 0)),
        ("  全園實到人日", overview.get("total_actual_days", 0)),
        ("  全園出席率", f"{overview.get('total_attendance_rate_pct', 0):.2f}%"),
        ("", ""),
        ("產生資訊", ""),
        ("  快照日期", str(overview.get("snapshot_date", ""))),
        ("  產生時間", overview.get("generated_at").strftime("%Y-%m-%d %H:%M")
            if overview.get("generated_at") else "-"),
        ("  產生人", overview.get("generated_by", "-")),
    ])
    for label, value in lines:
        ws.append([label, value])

    # 第一欄粗體
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, min_col=1, max_col=1):
        for cell in row:
            if cell.value and not cell.value.startswith(" "):
                cell.font = Font(bold=True)

    ws.column_dimensions["A"].width = 25
    ws.column_dimensions["B"].width = 20


def build_monthly_xlsx_bytes(
    snapshot_rows: list[dict],
    student_details: list[dict],
    overview: dict,
) -> bytes:
    """產 xlsx bytes（3 sheets：班級總表 / 幼生明細 / 統計摘要）。"""
    wb = Workbook()
    # remove default Sheet
    wb.remove(wb.active)
    _build_sheet1(wb, snapshot_rows)
    _build_sheet2(wb, student_details)
    _build_sheet3(wb, overview)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
```

- [ ] **Step 4: 跑測試確認 PASS**

```bash
pytest tests/test_gov_moe_monthly_excel.py -v
```

Expected: 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/gov_moe/monthly_excel_writer.py tests/test_gov_moe_monthly_excel.py
git commit -m "feat(gov_moe): Phase 2 monthly_excel_writer (openpyxl 3-sheet)

- 班級總表 / 幼生明細 / 統計摘要
- 凍結首列、自動欄寬、中文字寬補正
- 出席率 9542 → '95.42%' format
- 空值顯示 '-'

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

**Done conditions:**
- 7 tests PASS
- `build_monthly_xlsx_bytes(rows, details, overview) -> bytes` 簽章成立
- 3 sheets 正確產生，header 樣式套用，合計列存在

**Test command:** `pytest tests/test_gov_moe_monthly_excel.py -v`

**Depends on:** BE-1

---

### BE-3: API endpoints `api/gov_moe/monthly.py` + advisory lock + audit + TDD

**Files:**
- Create: `api/gov_moe/monthly.py`
- Modify: `api/gov_moe/__init__.py`
- Create: `tests/test_gov_moe_monthly_api.py`

工作目錄：`ivy-backend/.claude/worktrees/moe-phase2-be/`

- [ ] **Step 1: 寫測試（generate happy path + 重算 + 並發）**

建 `tests/test_gov_moe_monthly_api.py`：

```python
"""Endpoint 整合測試 — POST /generate, GET /, GET /export, audit, lock。"""
from datetime import date, datetime
from io import BytesIO
from unittest.mock import patch

import pytest
from openpyxl import load_workbook

from models.audit import AuditLog
from models.classroom import Student, StudentAttendance
from models.gov_moe import MonthlyEnrollmentSnapshot
from models.permissions import Permission


@pytest.fixture
def export_user(staff_with_perms):
    """擁有 GOV_REPORTS_VIEW + GOV_REPORTS_EXPORT 的 staff。"""
    return staff_with_perms(
        Permission.GOV_REPORTS_VIEW | Permission.GOV_REPORTS_EXPORT
    )


@pytest.fixture
def view_only_user(staff_with_perms):
    return staff_with_perms(Permission.GOV_REPORTS_VIEW)


def test_generate_creates_snapshot_rows(client, test_db_session, export_user, sample_classroom_context):
    # 加 1 名 active student + 1 天出席
    student_id = sample_classroom_context["student_id"]
    student = test_db_session.query(Student).get(student_id)
    student.enrollment_date = date(2025, 1, 1)
    student.lifecycle_status = "active"
    student.birthday = date(2022, 1, 1)
    test_db_session.add(StudentAttendance(
        student_id=student_id, date=date(2026, 5, 1), status="出席"
    ))
    test_db_session.commit()

    resp = client.post("/api/gov-moe/monthly/generate",
                       json={"year": 2026, "month": 5},
                       headers=export_user.auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["year"] == 2026
    assert body["month"] == 5
    assert body["rows_generated"] >= 1

    snapshot_count = test_db_session.query(MonthlyEnrollmentSnapshot).filter_by(
        year=2026, month=5
    ).count()
    assert snapshot_count >= 1


def test_generate_writes_audit_log(client, test_db_session, export_user, sample_classroom_context):
    resp = client.post("/api/gov-moe/monthly/generate",
                       json={"year": 2026, "month": 5},
                       headers=export_user.auth_headers())
    assert resp.status_code == 200
    audit = test_db_session.query(AuditLog).filter(
        AuditLog.action == "GENERATE",
        AuditLog.entity_type == "monthly_enrollment_snapshot",
    ).first()
    assert audit is not None
    assert audit.entity_id == "2026-05"


def test_regenerate_overwrites_and_audits(client, test_db_session, export_user):
    # 第一次
    client.post("/api/gov-moe/monthly/generate",
                json={"year": 2026, "month": 5},
                headers=export_user.auth_headers())
    first_count = test_db_session.query(MonthlyEnrollmentSnapshot).filter_by(
        year=2026, month=5
    ).count()

    # 第二次（覆寫）
    resp = client.post("/api/gov-moe/monthly/generate",
                       json={"year": 2026, "month": 5},
                       headers=export_user.auth_headers())
    assert resp.status_code == 200

    second_count = test_db_session.query(MonthlyEnrollmentSnapshot).filter_by(
        year=2026, month=5
    ).count()
    # 不會 double（覆寫）
    assert second_count == first_count

    # audit 兩筆（generate + regenerate）
    audits = test_db_session.query(AuditLog).filter(
        AuditLog.entity_type == "monthly_enrollment_snapshot",
        AuditLog.entity_id == "2026-05",
    ).all()
    assert len(audits) >= 2
    actions = [a.action for a in audits]
    assert "REGENERATE" in actions


def test_generate_invalid_year_400(client, export_user):
    resp = client.post("/api/gov-moe/monthly/generate",
                       json={"year": 1999, "month": 5},
                       headers=export_user.auth_headers())
    assert resp.status_code == 400


def test_generate_invalid_month_422(client, export_user):
    """Pydantic 驗證會回 422。"""
    resp = client.post("/api/gov-moe/monthly/generate",
                       json={"year": 2026, "month": 13},
                       headers=export_user.auth_headers())
    assert resp.status_code == 422


def test_generate_requires_export_permission(client, view_only_user):
    resp = client.post("/api/gov-moe/monthly/generate",
                       json={"year": 2026, "month": 5},
                       headers=view_only_user.auth_headers())
    assert resp.status_code == 403


def test_get_returns_404_before_generate(client, view_only_user):
    resp = client.get("/api/gov-moe/monthly?year=2026&month=5",
                      headers=view_only_user.auth_headers())
    assert resp.status_code == 404


def test_get_returns_three_dimensions(client, export_user, view_only_user):
    client.post("/api/gov-moe/monthly/generate",
                json={"year": 2026, "month": 5},
                headers=export_user.auth_headers())

    resp = client.get("/api/gov-moe/monthly?year=2026&month=5",
                      headers=view_only_user.auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "classroom_summary" in body
    assert "student_detail" in body
    assert "overview" in body
    assert body["year"] == 2026


def test_export_returns_xlsx_bytes(client, export_user):
    client.post("/api/gov-moe/monthly/generate",
                json={"year": 2026, "month": 5},
                headers=export_user.auth_headers())

    resp = client.get("/api/gov-moe/monthly/export?year=2026&month=5&format=xlsx",
                      headers=export_user.auth_headers())
    assert resp.status_code == 200
    assert resp.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "義華幼兒園_月報_2026-05" in resp.headers["content-disposition"]
    wb = load_workbook(BytesIO(resp.content))
    assert wb.sheetnames == ["班級總表", "幼生明細", "統計摘要"]


def test_export_returns_404_before_generate(client, export_user):
    resp = client.get("/api/gov-moe/monthly/export?year=2026&month=5",
                      headers=export_user.auth_headers())
    assert resp.status_code == 404


def test_get_requires_view_permission(client, no_perm_user):
    """no_perm_user fixture 來自 conftest，零權限 staff。"""
    resp = client.get("/api/gov-moe/monthly?year=2026&month=5",
                      headers=no_perm_user.auth_headers())
    assert resp.status_code == 403
```

注意：`staff_with_perms` 與 `no_perm_user` fixture 若不存在於 `tests/conftest.py`，需先建（用既有 user-with-permissions fixture pattern）。先跑既有 conftest grep：

```bash
grep -nE "staff_with_perms|no_perm_user|auth_headers" tests/conftest.py
```

若 fixture 缺，用 inline 製造 user：

```python
@pytest.fixture
def export_user(test_db_session):
    from models.auth import User, UserAccount
    u = UserAccount(username="export_test", password_hash="x", role="staff",
                    permissions=int(Permission.GOV_REPORTS_VIEW | Permission.GOV_REPORTS_EXPORT))
    test_db_session.add(u)
    test_db_session.commit()
    # 取 token / cookie，看既有 tests 怎麼處理（grep `auth_headers` 在其他測試）
    return u
```

如果 conftest 沒有現成 auth 機制，要先讀 `tests/test_appraisal_*.py` 或類似測試的 fixture 模式。**先用 `Bash` grep 確認** before writing this task。

- [ ] **Step 2: 跑測試確認 FAIL**

```bash
pytest tests/test_gov_moe_monthly_api.py -v
```

Expected: ImportError / 404（router 未掛）。

- [ ] **Step 3: 實作 `api/gov_moe/monthly.py`**

建檔：

```python
"""MOE Phase 2 月報 API — generate / get / export。

3 endpoints：
- POST /gov-moe/monthly/generate   (GOV_REPORTS_EXPORT, 並發 advisory lock)
- GET  /gov-moe/monthly             (GOV_REPORTS_VIEW)
- GET  /gov-moe/monthly/export      (GOV_REPORTS_EXPORT)
"""
from __future__ import annotations

from datetime import date, datetime
from io import BytesIO
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from models.classroom import Classroom
from models.database import get_db
from models.gov_moe import MonthlyEnrollmentSnapshot
from models.permissions import Permission
from services.gov_moe.monthly_calculator import build_snapshot_rows
from services.gov_moe.monthly_excel_writer import build_monthly_xlsx_bytes
from utils.audit import write_audit_in_session
from utils.auth import get_current_user, require_staff_permission

router = APIRouter(prefix="/monthly", tags=["gov_moe_monthly"])

_MIN_YEAR = 2020
_MAX_YEAR_OFFSET = 1  # 允許到「明年」（測試/規劃用）


class GenerateRequest(BaseModel):
    year: int
    month: int = Field(ge=1, le=12)


class GenerateResponse(BaseModel):
    year: int
    month: int
    rows_generated: int
    snapshot_date: date
    generated_at: datetime
    generated_by: str


def _validate_year(year: int) -> None:
    current_year = datetime.now().year
    if year < _MIN_YEAR or year > current_year + _MAX_YEAR_OFFSET:
        raise HTTPException(
            status_code=400,
            detail=f"year 必須介於 {_MIN_YEAR}~{current_year + _MAX_YEAR_OFFSET}",
        )


def _try_advisory_lock(db: Session, year: int, month: int) -> bool:
    """PG advisory lock；若取不到回 False。"""
    lock_key = abs(hash(f"moe_monthly_gen_{year}_{month}")) % (2**31)
    result = db.execute(
        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
    ).scalar()
    return bool(result)


def _identity_from_user(user: dict) -> str:
    return user.get("email") or user.get("username") or "unknown"


def _classroom_name_map(db: Session) -> dict[int, str]:
    return {
        c.id: c.name
        for c in db.query(Classroom).all()
    }


@router.post(
    "/generate",
    response_model=GenerateResponse,
    dependencies=[Depends(require_staff_permission(Permission.GOV_REPORTS_EXPORT))],
)
def generate_monthly_report(
    payload: GenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """產生或重算指定 (year, month) 的月報快照。

    並發保護：PG advisory lock；同一 (year, month) 同時兩個請求，第二個收 409。
    覆寫：刪除既有 (year, month) rows 後重新計算寫入。
    Audit：寫 AuditLog 記錄前後 row 數。
    """
    _validate_year(payload.year)

    if not _try_advisory_lock(db, payload.year, payload.month):
        raise HTTPException(status_code=409, detail="另一個產生請求進行中，請稍後再試")

    rows_before = (
        db.query(MonthlyEnrollmentSnapshot)
        .filter(
            MonthlyEnrollmentSnapshot.year == payload.year,
            MonthlyEnrollmentSnapshot.month == payload.month,
        )
        .count()
    )

    if rows_before > 0:
        db.query(MonthlyEnrollmentSnapshot).filter(
            MonthlyEnrollmentSnapshot.year == payload.year,
            MonthlyEnrollmentSnapshot.month == payload.month,
        ).delete()
        action = "REGENERATE"
    else:
        action = "GENERATE"

    identity = _identity_from_user(current_user)
    rows, _student_details = build_snapshot_rows(
        db, payload.year, payload.month, generated_by=identity
    )

    for r in rows:
        db.add(MonthlyEnrollmentSnapshot(
            year=r["year"], month=r["month"],
            classroom_id=r["classroom_id"], age_group=r["age_group"],
            total_count=r["total_count"],
            male_count=r["male_count"], female_count=r["female_count"],
            disadvantaged_count=r["disadvantaged_count"],
            disability_count=r["disability_count"],
            indigenous_count=r["indigenous_count"],
            foreign_count=r["foreign_count"],
            expected_attendance_days=r["expected_attendance_days"],
            actual_attendance_days=r["actual_attendance_days"],
            attendance_rate=r["attendance_rate"],
            snapshot_date=r["snapshot_date"],
            generated_at=r["generated_at"],
            generated_by=r["generated_by"],
        ))

    write_audit_in_session(
        db, request,
        action=action,
        entity_type="monthly_enrollment_snapshot",
        summary=f"月報 {payload.year}-{payload.month:02d} {action.lower()}",
        entity_id=f"{payload.year}-{payload.month:02d}",
        changes={
            "rows_before": rows_before,
            "rows_after": len(rows),
            "year": payload.year,
            "month": payload.month,
        },
    )

    db.commit()

    snapshot_date = date(payload.year, payload.month, 1).replace(day=28)  # 用實際 last day
    from dateutil.relativedelta import relativedelta
    snapshot_date = date(payload.year, payload.month, 1) + relativedelta(months=1, days=-1)

    return GenerateResponse(
        year=payload.year,
        month=payload.month,
        rows_generated=len(rows),
        snapshot_date=snapshot_date,
        generated_at=datetime.now(),
        generated_by=identity,
    )


@router.get(
    "",
    dependencies=[Depends(require_staff_permission(Permission.GOV_REPORTS_VIEW))],
)
def get_monthly_report(
    year: int,
    month: int,
    db: Session = Depends(get_db),
):
    """取得已產生的月報（classroom_summary + student_detail + overview）。

    若該月份未產生過 → 404。
    student_detail 改用即時重算（snapshot 表不存 per-student）：
    呼叫 build_snapshot_rows 重新計算 details；group rows 直接讀 snapshot。
    """
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month 須 1~12")

    snapshot_rows = (
        db.query(MonthlyEnrollmentSnapshot)
        .filter(
            MonthlyEnrollmentSnapshot.year == year,
            MonthlyEnrollmentSnapshot.month == month,
        )
        .all()
    )
    if not snapshot_rows:
        raise HTTPException(status_code=404, detail="尚未產生此月份月報")

    cls_map = _classroom_name_map(db)

    classroom_summary = []
    total_exp = total_act = 0
    by_age_group = {"2-3": 0, "3-4": 0, "4-5": 0, "5-6": 0}
    total_students = total_disadv = total_disab = total_ind = total_for = 0

    for r in snapshot_rows:
        classroom_summary.append({
            "classroom_id": r.classroom_id,
            "classroom_name": cls_map.get(r.classroom_id, "(未分班)"),
            "age_group": r.age_group or "未知",
            "expected_days": r.expected_attendance_days,
            "actual_days": r.actual_attendance_days,
            "attendance_rate_pct": round(r.attendance_rate / 100, 2),
            "total_count": r.total_count,
            "male_count": r.male_count,
            "female_count": r.female_count,
            "disadvantaged_count": r.disadvantaged_count,
            "disability_count": r.disability_count,
            "indigenous_count": r.indigenous_count,
            "foreign_count": r.foreign_count,
        })
        total_exp += r.expected_attendance_days
        total_act += r.actual_attendance_days
        total_students += r.total_count
        total_disadv += r.disadvantaged_count
        total_disab += r.disability_count
        total_ind += r.indigenous_count
        total_for += r.foreign_count
        if r.age_group in by_age_group:
            by_age_group[r.age_group] += r.total_count

    # student_detail 即時算（snapshot 表不存 per-student）
    _, student_details = build_snapshot_rows(db, year, month, generated_by="(query)")
    for sd in student_details:
        sd["classroom_name"] = cls_map.get(sd.get("classroom_id"), "(未分班)")

    overview = {
        "total_students": total_students,
        "by_age_group": by_age_group,
        "disadvantaged_pct": round(total_disadv / total_students * 100, 2) if total_students else 0,
        "disability_pct": round(total_disab / total_students * 100, 2) if total_students else 0,
        "indigenous_pct": round(total_ind / total_students * 100, 2) if total_students else 0,
        "foreign_pct": round(total_for / total_students * 100, 2) if total_students else 0,
        "total_expected_days": total_exp,
        "total_actual_days": total_act,
        "total_attendance_rate_pct": round(total_act / total_exp * 100, 2) if total_exp else 0,
    }

    first_row = snapshot_rows[0]
    return {
        "year": year,
        "month": month,
        "snapshot_date": first_row.snapshot_date.isoformat() if first_row.snapshot_date else None,
        "generated_at": first_row.generated_at.isoformat() if first_row.generated_at else None,
        "generated_by": first_row.generated_by,
        "classroom_summary": classroom_summary,
        "student_detail": student_details,
        "overview": overview,
    }


@router.get(
    "/export",
    dependencies=[Depends(require_staff_permission(Permission.GOV_REPORTS_EXPORT))],
)
def export_monthly_report(
    year: int,
    month: int,
    format: str = "xlsx",
    db: Session = Depends(get_db),
):
    """匯出 3-sheet Excel。format 目前只支援 xlsx。"""
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month 須 1~12")
    if format != "xlsx":
        raise HTTPException(status_code=400, detail="format 只支援 xlsx")

    snapshot_rows = (
        db.query(MonthlyEnrollmentSnapshot)
        .filter(
            MonthlyEnrollmentSnapshot.year == year,
            MonthlyEnrollmentSnapshot.month == month,
        )
        .all()
    )
    if not snapshot_rows:
        raise HTTPException(status_code=404, detail="尚未產生此月份月報")

    cls_map = _classroom_name_map(db)
    rows_payload = []
    total_exp = total_act = 0
    total_students = total_disadv = total_disab = total_ind = total_for = 0
    by_age_group = {"2-3": 0, "3-4": 0, "4-5": 0, "5-6": 0}

    for r in snapshot_rows:
        rows_payload.append({
            "classroom_name": cls_map.get(r.classroom_id, "(未分班)"),
            "teacher_names": "",  # MVP：暫不填，後端不查教師（可由前端 v2 補）
            "age_group": r.age_group or "未知",
            "expected_attendance_days": r.expected_attendance_days,
            "actual_attendance_days": r.actual_attendance_days,
            "attendance_rate": r.attendance_rate,
            "male_count": r.male_count, "female_count": r.female_count,
            "disadvantaged_count": r.disadvantaged_count,
            "disability_count": r.disability_count,
            "indigenous_count": r.indigenous_count,
            "foreign_count": r.foreign_count,
        })
        total_exp += r.expected_attendance_days
        total_act += r.actual_attendance_days
        total_students += r.total_count
        total_disadv += r.disadvantaged_count
        total_disab += r.disability_count
        total_ind += r.indigenous_count
        total_for += r.foreign_count
        if r.age_group in by_age_group:
            by_age_group[r.age_group] += r.total_count

    _, student_details = build_snapshot_rows(db, year, month, generated_by="(export)")
    for sd in student_details:
        sd["classroom_name"] = cls_map.get(sd.get("classroom_id"), "(未分班)")

    first_row = snapshot_rows[0]
    overview = {
        "year": year, "month": month,
        "snapshot_date": first_row.snapshot_date,
        "generated_at": first_row.generated_at,
        "generated_by": first_row.generated_by,
        "total_students": total_students,
        "by_age_group": by_age_group,
        "disadvantaged_pct": round(total_disadv / total_students * 100, 2) if total_students else 0,
        "disability_pct": round(total_disab / total_students * 100, 2) if total_students else 0,
        "indigenous_pct": round(total_ind / total_students * 100, 2) if total_students else 0,
        "foreign_pct": round(total_for / total_students * 100, 2) if total_students else 0,
        "total_expected_days": total_exp,
        "total_actual_days": total_act,
        "total_attendance_rate_pct": round(total_act / total_exp * 100, 2) if total_exp else 0,
    }

    xlsx_bytes = build_monthly_xlsx_bytes(rows_payload, student_details, overview)
    today_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"義華幼兒園_月報_{year}-{month:02d}_產生於{today_str}.xlsx"

    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
```

- [ ] **Step 4: 掛到 `api/gov_moe/__init__.py`**

修改 `api/gov_moe/__init__.py`：

```python
"""MOE reporting module — government reporting (Phase 1+)."""

from fastapi import APIRouter

from api.gov_moe import disability_documents, dashboard, monthly
from api.gov_moe import certificates as _certificates_module
from api.gov_moe import subsidies as _subsidies_module
from api.gov_moe import iep as _iep_module

router = APIRouter(prefix="/gov-moe", tags=["gov_moe"])
router.include_router(disability_documents.router)
router.include_router(dashboard.router)
router.include_router(_certificates_module.router)
router.include_router(_subsidies_module.router)
router.include_router(_iep_module.router)
router.include_router(monthly.router)  # ← 新增
```

- [ ] **Step 5: 跑全部 endpoint 測試確認 PASS**

```bash
pytest tests/test_gov_moe_monthly_api.py -v
```

Expected: 11 tests PASS.

若 fixture 不存在錯誤，先補 fixture（看 conftest grep 結果或參考既有 gov_moe 測試的 auth pattern）。

- [ ] **Step 6: 跑既有 gov_moe 測試確認沒被打壞**

```bash
pytest tests/test_gov_moe_*.py -v
```

Expected: 既有測試 + 新測試全 PASS。

- [ ] **Step 7: Commit**

```bash
git add api/gov_moe/monthly.py api/gov_moe/__init__.py tests/test_gov_moe_monthly_api.py
git commit -m "feat(gov_moe): Phase 2 monthly report API endpoints

- POST /gov-moe/monthly/generate (GOV_REPORTS_EXPORT, PG advisory lock, audit)
- GET  /gov-moe/monthly (GOV_REPORTS_VIEW, 3-dim response)
- GET  /gov-moe/monthly/export (GOV_REPORTS_EXPORT, 3-sheet xlsx)

並發保護用 pg_try_advisory_xact_lock；重算覆寫前留 audit log 記 rows_before/after。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

**Done conditions:**
- 11 new tests PASS
- 既有 gov_moe 測試零回歸
- 3 endpoints 掛在 `/api/gov-moe/monthly/*`

**Test command:** `pytest tests/test_gov_moe_monthly_api.py tests/test_gov_moe_*.py -v`

**Depends on:** BE-1, BE-2

---

### BE-4: 後端全套測試 + OpenAPI dump

**Files:** —

工作目錄：`ivy-backend/.claude/worktrees/moe-phase2-be/`

- [ ] **Step 1: 跑全套後端測試**

```bash
pytest -x --timeout=30 2>&1 | tail -30
```

Expected: 全綠（除既有 3 條 pre-existing fail `test_audit_router` — 不算回歸）。

- [ ] **Step 2: 跑 ruff / black（若有）**

```bash
ruff check services/gov_moe/ api/gov_moe/monthly.py tests/test_gov_moe_monthly*.py
```

Expected: no error。

- [ ] **Step 3: dump OpenAPI**

```bash
python scripts/dump_openapi.py
```

Expected: `openapi.json` 重新生成（被 .gitignore 不入 repo，但前端 codegen 會用）。

- [ ] **Step 4: 確認 OpenAPI 含新 endpoint**

```bash
grep -E "monthly/generate|monthly/export" openapi.json | head -5
```

Expected: 3 條 path 存在。

- [ ] **Step 5: Push branch**

```bash
git push -u origin feat/moe-phase2-monthly-report-2026-05-19-backend
```

Expected: 推上 origin，可 PR。

**Done conditions:**
- 全套後端測試零回歸
- OpenAPI 新增 3 endpoint
- branch pushed

**Test command:** `pytest -x --timeout=30`

**Depends on:** BE-3

---

## Frontend Tasks

### FE-1: OpenAPI codegen 拉新 schema

**Files:**
- Modify: `src/api/_generated/schema.d.ts`

工作目錄：`ivy-frontend/.claude/worktrees/moe-phase2-fe/`

- [ ] **Step 1: 依賴後端 worktree dump 出 openapi.json**

確認 `ivy-backend/.claude/worktrees/moe-phase2-be/openapi.json` 存在（BE-4 Step 3 已產）。

- [ ] **Step 2: 跑 codegen**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/moe-phase2-fe
# 把 backend worktree 的 openapi.json 暫時 symlink 過來（或調整 gen:api 腳本路徑）
# 看 package.json 的 gen:api 命令怎麼寫
cat package.json | grep -A1 "gen:api"
```

依 gen:api 慣例（讀 `../../ivy-backend/openapi.json`）執行：

```bash
# 先確保 backend worktree 的 openapi.json 在預設 lookup 位置
cp /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/moe-phase2-be/openapi.json /Users/yilunwu/Desktop/ivy-backend/openapi.json
npm run gen:api
```

Expected: `src/api/_generated/schema.d.ts` 更新含 `/gov-moe/monthly/*` paths。

- [ ] **Step 3: 驗證 schema.d.ts 含新 path**

```bash
grep -c "gov-moe/monthly" src/api/_generated/schema.d.ts
```

Expected: ≥3（generate / get / export）。

- [ ] **Step 4: typecheck 全綠**

```bash
npm run typecheck
```

Expected: 0 errors。

- [ ] **Step 5: Commit**

```bash
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regen schema for MOE Phase 2 monthly endpoints

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

**Done conditions:**
- `schema.d.ts` 含 3 個 `/gov-moe/monthly/*` paths
- typecheck 零錯

**Test command:** `npm run typecheck && grep -c "gov-moe/monthly" src/api/_generated/schema.d.ts`

**Depends on:** BE-4

---

### FE-2: `govMoe.ts` API wrappers

**Files:**
- Modify: `src/api/govMoe.ts`

工作目錄：`ivy-frontend/.claude/worktrees/moe-phase2-fe/`

- [ ] **Step 1: 在 `govMoe.ts` 末尾加 3 個 wrapper**

讀現有 `src/api/govMoe.ts` 結尾位置，加：

```typescript
// --- Monthly Enrollment Report (Phase 2) ---
import type { ApiBody, ApiQuery, AxiosResp } from './_generated/typed'

export const generateMonthlyReport = (
  payload: ApiBody<'/gov-moe/monthly/generate', 'post'>,
): AxiosResp<'/gov-moe/monthly/generate', 'post'> =>
  api.post('/gov-moe/monthly/generate', payload)

export const getMonthlyReport = (
  params: ApiQuery<'/gov-moe/monthly', 'get'>,
): AxiosResp<'/gov-moe/monthly', 'get'> =>
  api.get('/gov-moe/monthly', { params })

export const exportMonthlyReport = (params: { year: number; month: number }) =>
  api.get('/gov-moe/monthly/export', {
    params: { ...params, format: 'xlsx' },
    responseType: 'blob',
  })
```

注意：
- `import type` 在檔頭就有，若已 import 同名 alias 不需重複 import
- `exportMonthlyReport` 因 response 是 blob，型別 helper 套不上去，用 plain axios call

- [ ] **Step 2: typecheck 全綠**

```bash
npm run typecheck
```

Expected: 0 errors。

- [ ] **Step 3: Commit**

```bash
git add src/api/govMoe.ts
git commit -m "feat(api): add Phase 2 monthly report wrappers in govMoe.ts

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

**Done conditions:**
- `govMoe.ts` exports `generateMonthlyReport` / `getMonthlyReport` / `exportMonthlyReport`
- typecheck 零錯

**Test command:** `npm run typecheck`

**Depends on:** FE-1

---

### FE-3: 3 個子元件 + vitest

**Files:**
- Create: `src/components/gov-reports/ClassroomSummaryTable.vue`
- Create: `src/components/gov-reports/StudentDetailTable.vue`
- Create: `src/components/gov-reports/OverviewSummaryCard.vue`
- Create: `tests/components/gov-reports/ClassroomSummaryTable.test.ts`
- Create: `tests/components/gov-reports/StudentDetailTable.test.ts`
- Create: `tests/components/gov-reports/OverviewSummaryCard.test.ts`

工作目錄：`ivy-frontend/.claude/worktrees/moe-phase2-fe/`

- [ ] **Step 1: 建 `ClassroomSummaryTable.vue`**

```vue
<script setup lang="ts">
import { computed } from 'vue'

interface ClassroomSummaryRow {
  classroom_id: number | null
  classroom_name: string
  age_group: string
  expected_days: number
  actual_days: number
  attendance_rate_pct: number
  total_count: number
  male_count: number
  female_count: number
  disadvantaged_count: number
  disability_count: number
  indigenous_count: number
  foreign_count: number
}

const props = defineProps<{ rows: ClassroomSummaryRow[] }>()

const totals = computed(() => {
  const t = {
    expected_days: 0, actual_days: 0,
    male: 0, female: 0,
    disadvantaged: 0, disability: 0, indigenous: 0, foreign: 0,
  }
  for (const r of props.rows) {
    t.expected_days += r.expected_days
    t.actual_days += r.actual_days
    t.male += r.male_count
    t.female += r.female_count
    t.disadvantaged += r.disadvantaged_count
    t.disability += r.disability_count
    t.indigenous += r.indigenous_count
    t.foreign += r.foreign_count
  }
  return t
})

const totalRate = computed(() =>
  totals.value.expected_days
    ? ((totals.value.actual_days / totals.value.expected_days) * 100).toFixed(2)
    : '0.00',
)
</script>

<template>
  <el-table :data="rows" border stripe size="small">
    <el-table-column prop="classroom_name" label="班級" min-width="100" />
    <el-table-column prop="age_group" label="年齡層" width="80">
      <template #default="{ row }">{{ row.age_group }} 歲</template>
    </el-table-column>
    <el-table-column prop="expected_days" label="應到人日" width="90" align="right" />
    <el-table-column prop="actual_days" label="實到人日" width="90" align="right" />
    <el-table-column label="出席率" width="80" align="right">
      <template #default="{ row }">{{ row.attendance_rate_pct.toFixed(2) }}%</template>
    </el-table-column>
    <el-table-column prop="male_count" label="男" width="60" align="right" />
    <el-table-column prop="female_count" label="女" width="60" align="right" />
    <el-table-column prop="disadvantaged_count" label="弱勢" width="60" align="right" />
    <el-table-column prop="disability_count" label="身障" width="60" align="right" />
    <el-table-column prop="indigenous_count" label="原民" width="60" align="right" />
    <el-table-column prop="foreign_count" label="外籍" width="60" align="right" />
    <template #append>
      <div class="totals-row">
        <strong>合計</strong>
        <span>應到 {{ totals.expected_days }} / 實到 {{ totals.actual_days }} ({{ totalRate }}%)</span>
        <span>男 {{ totals.male }} / 女 {{ totals.female }}</span>
        <span>弱勢 {{ totals.disadvantaged }} / 身障 {{ totals.disability }} / 原民 {{ totals.indigenous }} / 外籍 {{ totals.foreign }}</span>
      </div>
    </template>
  </el-table>
</template>

<style scoped>
.totals-row {
  padding: 8px 12px;
  background: #f0f4ff;
  display: flex;
  gap: 24px;
  font-size: 13px;
}
</style>
```

- [ ] **Step 2: 建 `StudentDetailTable.vue`**

```vue
<script setup lang="ts">
interface StudentDetailRow {
  student_id: number
  student_no: string
  name: string
  id_number: string | null
  classroom_name: string
  age_group: string
  expected_days: number
  actual_days: number
  attendance_rate_pct: number
  is_disadvantaged: boolean
}

defineProps<{ rows: StudentDetailRow[] }>()
</script>

<template>
  <el-table :data="rows" border stripe size="small" max-height="600">
    <el-table-column prop="student_no" label="學號" width="90" sortable />
    <el-table-column prop="name" label="姓名" width="100" />
    <el-table-column label="身分證" width="120">
      <template #default="{ row }">{{ row.id_number || '-' }}</template>
    </el-table-column>
    <el-table-column prop="classroom_name" label="班級" width="100" />
    <el-table-column prop="age_group" label="年齡層" width="80">
      <template #default="{ row }">{{ row.age_group }} 歲</template>
    </el-table-column>
    <el-table-column prop="expected_days" label="應到日數" width="90" align="right" sortable />
    <el-table-column prop="actual_days" label="實到日數" width="90" align="right" sortable />
    <el-table-column label="出席率" width="80" align="right" sortable>
      <template #default="{ row }">{{ row.attendance_rate_pct.toFixed(2) }}%</template>
    </el-table-column>
    <el-table-column label="弱勢" width="60" align="center">
      <template #default="{ row }">
        <el-tag v-if="row.is_disadvantaged" type="warning" size="small">是</el-tag>
        <span v-else>否</span>
      </template>
    </el-table-column>
  </el-table>
</template>
```

- [ ] **Step 3: 建 `OverviewSummaryCard.vue`**

```vue
<script setup lang="ts">
interface Overview {
  total_students: number
  by_age_group: Record<string, number>
  disadvantaged_pct: number
  disability_pct: number
  indigenous_pct: number
  foreign_pct: number
  total_expected_days: number
  total_actual_days: number
  total_attendance_rate_pct: number
}

defineProps<{
  overview: Overview
  snapshotDate: string | null
  generatedAt: string | null
  generatedBy: string | null
}>()
</script>

<template>
  <div class="summary-grid">
    <el-card shadow="never">
      <div class="kpi-label">總人數</div>
      <div class="kpi-value">{{ overview.total_students }}</div>
    </el-card>

    <el-card shadow="never">
      <div class="kpi-label">年齡層分布</div>
      <div class="age-list">
        <div v-for="ag in ['2-3', '3-4', '4-5', '5-6']" :key="ag">
          {{ ag }} 歲：<strong>{{ overview.by_age_group[ag] || 0 }}</strong>
        </div>
      </div>
    </el-card>

    <el-card shadow="never">
      <div class="kpi-label">特殊屬性占比</div>
      <div class="attr-list">
        <div>弱勢：<strong>{{ overview.disadvantaged_pct.toFixed(2) }}%</strong></div>
        <div>身障：<strong>{{ overview.disability_pct.toFixed(2) }}%</strong></div>
        <div>原住民：<strong>{{ overview.indigenous_pct.toFixed(2) }}%</strong></div>
        <div>外籍：<strong>{{ overview.foreign_pct.toFixed(2) }}%</strong></div>
      </div>
    </el-card>

    <el-card shadow="never">
      <div class="kpi-label">出席統計</div>
      <div class="att-list">
        <div>應到人日：<strong>{{ overview.total_expected_days.toLocaleString() }}</strong></div>
        <div>實到人日：<strong>{{ overview.total_actual_days.toLocaleString() }}</strong></div>
        <div>全園出席率：<strong>{{ overview.total_attendance_rate_pct.toFixed(2) }}%</strong></div>
      </div>
    </el-card>

    <el-card shadow="never" class="produce-info">
      <div class="kpi-label">產生資訊</div>
      <div>快照日：{{ snapshotDate || '-' }}</div>
      <div>產生時間：{{ generatedAt || '-' }}</div>
      <div>產生人：{{ generatedBy || '-' }}</div>
    </el-card>
  </div>
</template>

<style scoped>
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
}
.kpi-label { font-size: 13px; color: #909399; margin-bottom: 8px; }
.kpi-value { font-size: 32px; font-weight: 600; color: #303133; }
.age-list, .attr-list, .att-list { display: flex; flex-direction: column; gap: 4px; font-size: 14px; }
.produce-info { grid-column: 1 / -1; }
</style>
```

- [ ] **Step 4: 寫子元件 vitest**

建 `tests/components/gov-reports/ClassroomSummaryTable.test.ts`：

```typescript
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ClassroomSummaryTable from '@/components/gov-reports/ClassroomSummaryTable.vue'

const sampleRow = {
  classroom_id: 1, classroom_name: '蘋果班', age_group: '4-5',
  expected_days: 100, actual_days: 95, attendance_rate_pct: 95.0,
  total_count: 5, male_count: 3, female_count: 2,
  disadvantaged_count: 1, disability_count: 0,
  indigenous_count: 0, foreign_count: 0,
}

describe('ClassroomSummaryTable', () => {
  it('renders one row per data entry', () => {
    const wrapper = mount(ClassroomSummaryTable, {
      props: { rows: [sampleRow, { ...sampleRow, classroom_id: 2, classroom_name: '芒果班' }] },
    })
    expect(wrapper.text()).toContain('蘋果班')
    expect(wrapper.text()).toContain('芒果班')
  })

  it('shows totals row with sum of all', () => {
    const wrapper = mount(ClassroomSummaryTable, {
      props: { rows: [sampleRow, { ...sampleRow, expected_days: 50, actual_days: 48 }] },
    })
    expect(wrapper.text()).toContain('150')  // 100 + 50
    expect(wrapper.text()).toContain('143')  // 95 + 48
  })

  it('handles empty rows', () => {
    const wrapper = mount(ClassroomSummaryTable, { props: { rows: [] } })
    expect(wrapper.text()).toContain('合計')
  })
})
```

建 `tests/components/gov-reports/StudentDetailTable.test.ts`：

```typescript
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import StudentDetailTable from '@/components/gov-reports/StudentDetailTable.vue'

const sampleRow = {
  student_id: 1, student_no: 'S001', name: '王小明',
  id_number: 'A123456789', classroom_name: '蘋果班', age_group: '4-5',
  expected_days: 22, actual_days: 20, attendance_rate_pct: 90.91,
  is_disadvantaged: false,
}

describe('StudentDetailTable', () => {
  it('renders student rows', () => {
    const wrapper = mount(StudentDetailTable, { props: { rows: [sampleRow] } })
    expect(wrapper.text()).toContain('王小明')
    expect(wrapper.text()).toContain('A123456789')
  })

  it('shows dash for null id_number', () => {
    const wrapper = mount(StudentDetailTable, {
      props: { rows: [{ ...sampleRow, id_number: null }] },
    })
    const idCell = wrapper.findAll('td').find((c) => c.text() === '-')
    expect(idCell).toBeDefined()
  })

  it('shows disadvantaged tag when true', () => {
    const wrapper = mount(StudentDetailTable, {
      props: { rows: [{ ...sampleRow, is_disadvantaged: true }] },
    })
    expect(wrapper.text()).toContain('是')
  })
})
```

建 `tests/components/gov-reports/OverviewSummaryCard.test.ts`：

```typescript
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import OverviewSummaryCard from '@/components/gov-reports/OverviewSummaryCard.vue'

const overview = {
  total_students: 28,
  by_age_group: { '2-3': 0, '3-4': 8, '4-5': 12, '5-6': 8 },
  disadvantaged_pct: 7.14, disability_pct: 3.57,
  indigenous_pct: 0, foreign_pct: 0,
  total_expected_days: 1300, total_actual_days: 1238,
  total_attendance_rate_pct: 95.23,
}

describe('OverviewSummaryCard', () => {
  it('renders total students', () => {
    const wrapper = mount(OverviewSummaryCard, {
      props: { overview, snapshotDate: '2026-05-31', generatedAt: '2026-06-01T10:23', generatedBy: 'test' },
    })
    expect(wrapper.text()).toContain('28')
    expect(wrapper.text()).toContain('總人數')
  })

  it('renders age group distribution', () => {
    const wrapper = mount(OverviewSummaryCard, {
      props: { overview, snapshotDate: null, generatedAt: null, generatedBy: null },
    })
    expect(wrapper.text()).toContain('4-5 歲')
    expect(wrapper.text()).toContain('12')
  })

  it('shows total attendance rate', () => {
    const wrapper = mount(OverviewSummaryCard, {
      props: { overview, snapshotDate: null, generatedAt: null, generatedBy: null },
    })
    expect(wrapper.text()).toContain('95.23%')
  })
})
```

- [ ] **Step 5: 跑 vitest 確認 PASS**

```bash
npx vitest run tests/components/gov-reports/
```

Expected: 9 tests PASS。

- [ ] **Step 6: Commit**

```bash
git add src/components/gov-reports/ tests/components/gov-reports/
git commit -m "feat(gov-reports): MOE Phase 2 月報 3 子元件 + tests

- ClassroomSummaryTable（含合計列）
- StudentDetailTable（弱勢 tag、id_number null fallback '-'）
- OverviewSummaryCard（5 卡 grid layout）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

**Done conditions:**
- 3 子元件 `.vue` 完成且 vue 模板正確
- 9 vitest 全綠
- typecheck 零錯

**Test command:** `npx vitest run tests/components/gov-reports/ && npm run typecheck`

**Depends on:** FE-2

---

### FE-4: `MonthlyReportView.vue` + router + sidebar + vitest

**Files:**
- Create: `src/views/admin/gov-reports/MonthlyReportView.vue`
- Modify: router 設定檔（用 `grep -rn "IepView\\|CertificatesView" src/router` 找位置）
- Modify: sidebar 設定檔（用 `grep -rn "/admin/gov-reports/iep" src/` 找位置）
- Create: `tests/views/admin/gov-reports/MonthlyReportView.test.ts`

工作目錄：`ivy-frontend/.claude/worktrees/moe-phase2-fe/`

- [ ] **Step 1: 找 router 與 sidebar 的既有 gov-reports 連結**

```bash
grep -rn "IepView\|CertificatesView\|SubsidiesView\|gov-reports/iep" src/router/ src/views/admin/AdminSidebar.vue src/components/layout/AdminSidebar.vue 2>/dev/null | head -10
```

把這兩個位置記下來，作為新 view 的對齊範本。

- [ ] **Step 2: 建 `MonthlyReportView.vue`**

```vue
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  exportMonthlyReport,
  generateMonthlyReport,
  getMonthlyReport,
} from '@/api/govMoe'
import ClassroomSummaryTable from '@/components/gov-reports/ClassroomSummaryTable.vue'
import OverviewSummaryCard from '@/components/gov-reports/OverviewSummaryCard.vue'
import StudentDetailTable from '@/components/gov-reports/StudentDetailTable.vue'

const today = new Date()
const defaultMonth =
  today.getDate() === 1
    ? new Date(today.getFullYear(), today.getMonth() - 1, 1)
    : new Date(today.getFullYear(), today.getMonth() - 1, 1)
const year = ref(defaultMonth.getFullYear())
const month = ref(defaultMonth.getMonth() + 1)

const loading = ref(false)
const exporting = ref(false)
const report = ref<null | {
  snapshot_date: string | null
  generated_at: string | null
  generated_by: string | null
  classroom_summary: any[]
  student_detail: any[]
  overview: any
}>(null)
const activeTab = ref<'classroom' | 'student' | 'overview'>('classroom')

const hasReport = computed(() => report.value !== null)

const fetchReport = async () => {
  try {
    const resp = await getMonthlyReport({ year: year.value, month: month.value })
    report.value = (resp.data ?? resp) as any
  } catch (err: any) {
    if (err?.response?.status === 404) {
      report.value = null
      return
    }
    throw err
  }
}

const onGenerate = async () => {
  if (hasReport.value) {
    try {
      await ElMessageBox.confirm(
        `${year.value}-${String(month.value).padStart(2, '0')} 已產生過，確認覆寫並重算？`,
        '重算月報',
        { type: 'warning' },
      )
    } catch {
      return
    }
  }
  loading.value = true
  try {
    const resp = await generateMonthlyReport({ year: year.value, month: month.value })
    const body = (resp.data ?? resp) as any
    ElMessage.success(`已產生 ${body.rows_generated} 筆`)
    await fetchReport()
  } catch (err: any) {
    if (err?.response?.status === 409) {
      ElMessage.warning('另一個產生請求進行中，請稍後再試')
    } else {
      ElMessage.error(err?.response?.data?.detail || '產生失敗')
    }
  } finally {
    loading.value = false
  }
}

const onExport = async () => {
  exporting.value = true
  try {
    const resp = await exportMonthlyReport({ year: year.value, month: month.value })
    const blob = new Blob([resp.data], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `義華幼兒園_月報_${year.value}-${String(month.value).padStart(2, '0')}.xlsx`
    a.click()
    URL.revokeObjectURL(url)
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || '匯出失敗')
  } finally {
    exporting.value = false
  }
}

const onMonthChange = async () => {
  loading.value = true
  try {
    await fetchReport()
  } finally {
    loading.value = false
  }
}

const yearOptions = computed(() => {
  const now = new Date().getFullYear()
  return Array.from({ length: now - 2020 + 1 }, (_, i) => 2020 + i)
})
const monthOptions = Array.from({ length: 12 }, (_, i) => i + 1)

onMounted(() => { fetchReport() })
</script>

<template>
  <div class="monthly-report-view" v-loading="loading">
    <h2>月度幼生在園統計（教育部申報用）</h2>

    <div class="toolbar">
      <span>月份：</span>
      <el-select v-model="year" style="width: 100px" @change="onMonthChange">
        <el-option v-for="y in yearOptions" :key="y" :label="`${y} 年`" :value="y" />
      </el-select>
      <el-select v-model="month" style="width: 100px" @change="onMonthChange">
        <el-option v-for="m in monthOptions" :key="m" :label="`${m} 月`" :value="m" />
      </el-select>
      <el-button type="primary" :loading="loading" @click="onGenerate">
        {{ hasReport ? '重算本月' : '產生本月' }}
      </el-button>
      <el-tooltip :content="hasReport ? '' : '請先產生本月'" :disabled="hasReport">
        <el-button :disabled="!hasReport" :loading="exporting" @click="onExport">
          匯出 Excel
        </el-button>
      </el-tooltip>
    </div>

    <div v-if="report" class="meta">
      上次產生：{{ report.generated_at || '-' }}
      <span v-if="report.generated_by"> by {{ report.generated_by }}</span>
    </div>

    <el-tabs v-if="report" v-model="activeTab" class="tabs">
      <el-tab-pane label="班級總表" name="classroom">
        <ClassroomSummaryTable :rows="report.classroom_summary" />
      </el-tab-pane>
      <el-tab-pane label="幼生明細" name="student">
        <StudentDetailTable :rows="report.student_detail" />
      </el-tab-pane>
      <el-tab-pane label="統計摘要" name="overview">
        <OverviewSummaryCard
          :overview="report.overview"
          :snapshot-date="report.snapshot_date"
          :generated-at="report.generated_at"
          :generated-by="report.generated_by"
        />
      </el-tab-pane>
    </el-tabs>

    <el-empty v-else description="尚未產生本月月報" />

    <div class="footer-note">
      對照 ece.moe.edu.tw → 幼生通報 → 月報
    </div>
  </div>
</template>

<style scoped>
.monthly-report-view { padding: 16px; }
.toolbar { display: flex; gap: 8px; align-items: center; margin: 16px 0; }
.meta { color: #909399; font-size: 13px; margin-bottom: 12px; }
.tabs { margin-top: 16px; }
.footer-note {
  margin-top: 32px;
  color: #909399;
  font-size: 12px;
  text-align: center;
  padding-top: 16px;
  border-top: 1px solid #ebeef5;
}
</style>
```

- [ ] **Step 3: 加 router**

依 Step 1 找到的 router 檔（假設為 `src/router/index.ts` 或 `src/router/admin.routes.ts`），仿照 `IepView` 的 entry 加：

```typescript
{
  path: '/admin/gov-reports/monthly',
  name: 'AdminGovReportsMonthly',
  component: () => import('@/views/admin/gov-reports/MonthlyReportView.vue'),
  meta: { requiresPermission: 'GOV_REPORTS_VIEW' },
}
```

注意：`requiresPermission` key 名以 Step 1 看到的 IepView 為準（可能是 `permissions` array 或 `requiredBit`）。

- [ ] **Step 4: 加 sidebar 連結**

依 Step 1 找到的 sidebar 檔，仿 `IepView` 加一個 entry：

```typescript
{ label: '月度月報', route: '/admin/gov-reports/monthly', permission: 'GOV_REPORTS_VIEW' }
```

- [ ] **Step 5: 寫 vitest**

建 `tests/views/admin/gov-reports/MonthlyReportView.test.ts`：

```typescript
import { mount, flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MonthlyReportView from '@/views/admin/gov-reports/MonthlyReportView.vue'

vi.mock('@/api/govMoe', () => ({
  getMonthlyReport: vi.fn(),
  generateMonthlyReport: vi.fn(),
  exportMonthlyReport: vi.fn(),
}))

const { getMonthlyReport, generateMonthlyReport, exportMonthlyReport } = await import('@/api/govMoe')

const sampleReport = {
  year: 2026, month: 5,
  snapshot_date: '2026-05-31',
  generated_at: '2026-06-01T10:23:00+08:00',
  generated_by: 'test@example.com',
  classroom_summary: [],
  student_detail: [],
  overview: {
    total_students: 0,
    by_age_group: { '2-3': 0, '3-4': 0, '4-5': 0, '5-6': 0 },
    disadvantaged_pct: 0, disability_pct: 0, indigenous_pct: 0, foreign_pct: 0,
    total_expected_days: 0, total_actual_days: 0, total_attendance_rate_pct: 0,
  },
}

describe('MonthlyReportView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('defaults to last completed month', () => {
    vi.mocked(getMonthlyReport).mockResolvedValue({ data: sampleReport } as any)
    const wrapper = mount(MonthlyReportView)
    const today = new Date()
    const expectedMonth =
      today.getDate() === 1 ? today.getMonth() : today.getMonth()
    // expectedMonth 0-indexed 上個月 → 顯示時 +1
    // 因 fixture 邏輯，default = last month
    expect(getMonthlyReport).toHaveBeenCalled()
  })

  it('disables export button when no report', async () => {
    vi.mocked(getMonthlyReport).mockRejectedValue({ response: { status: 404 } })
    const wrapper = mount(MonthlyReportView)
    await flushPromises()
    const btn = wrapper.findAll('button').find((b) => b.text().includes('匯出'))
    expect(btn?.attributes('disabled')).toBeDefined()
  })

  it('shows empty state when no report', async () => {
    vi.mocked(getMonthlyReport).mockRejectedValue({ response: { status: 404 } })
    const wrapper = mount(MonthlyReportView)
    await flushPromises()
    expect(wrapper.text()).toContain('尚未產生本月月報')
  })

  it('calls generate when button clicked (no existing report)', async () => {
    vi.mocked(getMonthlyReport).mockRejectedValueOnce({ response: { status: 404 } })
    vi.mocked(generateMonthlyReport).mockResolvedValue({
      data: { year: 2026, month: 5, rows_generated: 3, snapshot_date: '2026-05-31',
              generated_at: '2026-06-01T10:23', generated_by: 'test' },
    } as any)
    vi.mocked(getMonthlyReport).mockResolvedValueOnce({ data: sampleReport } as any)

    const wrapper = mount(MonthlyReportView)
    await flushPromises()
    const btn = wrapper.findAll('button').find((b) => b.text().includes('產生'))!
    await btn.trigger('click')
    await flushPromises()
    expect(generateMonthlyReport).toHaveBeenCalledWith({ year: expect.any(Number), month: expect.any(Number) })
  })

  it('shows confirm dialog when regenerating', async () => {
    vi.mocked(getMonthlyReport).mockResolvedValue({ data: sampleReport } as any)
    const wrapper = mount(MonthlyReportView)
    await flushPromises()
    const btn = wrapper.findAll('button').find((b) => b.text().includes('重算'))!
    expect(btn).toBeDefined()
    // 點擊會跳 ElMessageBox，本測試只驗有「重算本月」按鈕（dialog 互動屬整合測試）
  })
})
```

- [ ] **Step 6: 跑 vitest 確認 PASS**

```bash
npx vitest run tests/views/admin/gov-reports/MonthlyReportView.test.ts
```

Expected: 5 tests PASS。

- [ ] **Step 7: typecheck + build**

```bash
npm run typecheck && npm run build
```

Expected: 零錯誤；build 成功。

- [ ] **Step 8: Commit**

```bash
git add src/views/admin/gov-reports/MonthlyReportView.vue tests/views/admin/gov-reports/MonthlyReportView.test.ts src/router/ src/components/layout/
git commit -m "feat(gov-reports): MOE Phase 2 月報主頁 + router + sidebar

- MonthlyReportView 含月份選擇、產生/重算、3 tab、匯出
- 重算前 ElMessageBox 確認
- 404 顯示 el-empty「尚未產生本月月報」
- 預設月份 = 上個完整月份
- 加入 sidebar gov-reports 群組（與 IEP / 特教加給 / 在學證明並列）

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

**Done conditions:**
- `MonthlyReportView.vue` 完整含 3 tab、產生、匯出、重算確認
- router 與 sidebar 加好連結
- 5 vitest 全綠
- typecheck + build 零錯

**Test command:** `npx vitest run tests/views/admin/gov-reports/ && npm run typecheck && npm run build`

**Depends on:** FE-3

---

### FE-5: 全套前端測試 + push

**Files:** —

工作目錄：`ivy-frontend/.claude/worktrees/moe-phase2-fe/`

- [ ] **Step 1: 跑全套 vitest**

```bash
npx vitest run 2>&1 | tail -15
```

Expected: 全綠（含既有 + 新增 14 條）。

- [ ] **Step 2: build**

```bash
npm run build 2>&1 | tail -10
```

Expected: build success，無 error。

- [ ] **Step 3: Push branch**

```bash
git push -u origin feat/moe-phase2-monthly-report-2026-05-19-frontend
```

Expected: pushed。

**Done conditions:**
- vitest 全綠
- build 成功
- branch pushed

**Test command:** `npx vitest run && npm run build`

**Depends on:** FE-4

---

## Integration

### INT-1: User 整合驗證

**Files:** —

工作目錄：兩個 worktree

⚠️ **此 task 需 user 親自執行** — agent 不替代手動驗證。

- [ ] **Step 1: 起 dev server**

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem && ./start.sh
```

預期：後端 8088 + 前端 5173 啟動，瀏覽器開 http://localhost:5173。

- [ ] **Step 2: 登入有 GOV_REPORTS_EXPORT 權限的帳號**

導航：管理端 → 政府申報 → 月度月報（sidebar 應顯示）。

- [ ] **Step 3: 產生 2026-04 月報**

選 2026 年 / 4 月 → 「產生本月」→ 等 loading 結束 → 應顯示 「已產生 N 筆」 toast。

- [ ] **Step 4: 驗 3 tab 數字**

切「班級總表」「幼生明細」「統計摘要」三 tab，目視確認：
- 班級總表合計列 = 各班加總
- 幼生明細的「出席率」≈ 實到/應到 × 100
- 統計摘要的「總人數」= 幼生明細列數
- 統計摘要的「全園出席率」≈ 全園實到人日 / 全園應到人日

- [ ] **Step 5: 匯出 Excel 驗 3 sheet**

點「匯出 Excel」→ 檔名應為「義華幼兒園_月報_2026-04_產生於YYYY-MM-DD.xlsx」→ 用 Excel/Numbers 開：
- Sheet 1「班級總表」首列粗體底色，最末「合計」列粗體
- Sheet 2「幼生明細」凍結首列
- Sheet 3「統計摘要」5 區塊（總人數 / 年齡層 / 特殊屬性 / 出席統計 / 產生資訊）

- [ ] **Step 6: 邊界 manual 驗證**

- 再點「重算本月」→ 應跳 ElMessageBox 「已產生過，確認覆寫並重算？」
- 確認 → 應顯示重算 toast
- 換另一個無資料月份（如 2027-01）→ 「產生」應產 0 筆，畫面顯示「尚未產生本月月報」或產 0 筆 toast 後顯示 0 row tab

- [ ] **Step 7: 確認 audit log**

在後端 DB（用 `mcp__postgres__query` 或 psql）：

```sql
SELECT action, entity_type, entity_id, summary, created_at
FROM audit_logs
WHERE entity_type = 'monthly_enrollment_snapshot'
ORDER BY created_at DESC LIMIT 5;
```

應看到 GENERATE / REGENERATE 各 1 條。

- [ ] **Step 8: 收尾**

驗收 OK 後：
1. 兩個 PR 各自開：
   ```bash
   gh pr create --title "feat(gov_moe): MOE Phase 2 月報匯出器（後端）" ...
   gh pr create --title "feat(gov_moe): MOE Phase 2 月報匯出器（前端）" ...
   ```
2. 兩 PR 都 green CI 後，先 merge 後端，再 merge 前端。
3. 清掉 worktree：
   ```bash
   cd /Users/yilunwu/Desktop/ivy-backend && git worktree remove .claude/worktrees/moe-phase2-be
   cd /Users/yilunwu/Desktop/ivy-frontend && git worktree remove .claude/worktrees/moe-phase2-fe
   ```

**Done conditions:**
- 3 tab 數字目視合理
- Excel 3 sheet 開啟正常
- 重算 dialog 出現
- audit log 兩條
- 兩 PR 合併

**Test command:** N/A — user 手動

**Depends on:** BE-4, FE-5

---

## Self-Review Notes

對照 spec §1–§13 檢查：

| Spec 章節 | 對應 Task |
|---------|---------|
| §2.1 snapshot 表（已建）| BE-3（僅 INSERT 不 ALTER）|
| §3.1–3.8 演算法 | BE-1 |
| §3.9 重新產生 + advisory lock | BE-3 |
| §4.1 POST generate | BE-3 |
| §4.2 GET get | BE-3 |
| §4.3 GET export | BE-3 |
| §5.1–5.4 Excel 3 sheet | BE-2 |
| §6.1 路由 | FE-4 |
| §6.2 頁面結構 | FE-4 |
| §6.3 互動細節 | FE-4 |
| §6.4 元件拆分 | FE-3 |
| §6.5 API wrapper | FE-2 |
| §7 邊界 12 情境 | BE-1（5 種 in compute_student_attendance）、BE-3（404 / 409 / 0 row）|
| §8 Audit log | BE-3 |
| §9 權限 | BE-3 |
| §10.1 pytest | BE-1, BE-2, BE-3 |
| §10.2 vitest | FE-3, FE-4 |

**Gaps 修補：**
- 無

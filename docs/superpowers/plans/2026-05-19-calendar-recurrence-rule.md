# Calendar Recurrence Rule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 為 `school_events` 加 `recurrence_rule JSONB` 欄位，後端純函式 query-time 展開三種規則（weekly / monthly_day / monthly_nth），admin_feed + 家長端 calendar 都吃同一 expander；前端事件編輯 dialog 嵌「重複設定」section。

**Architecture:** 純函式 expander 在 `utils/recurrence.py`（無 DB 依賴、好測），三處消費者（`api/events.py` 寫入驗證 / `api/calendar_admin.py:_fetch_event` 讀展開 / `api/parent_portal/calendar.py` 同樣展開）只 import 同一 helper。occurrence id 用 `{event_pk}@{date}` 字串，原 PK 用於 deep-link。前端新 `RecurrenceEditor.vue` 元件嵌進既有 CalendarView dialog。

**Tech Stack:** FastAPI / SQLAlchemy + Alembic / Pydantic v2 / PostgreSQL JSONB / Vue 3 Composition API / Element Plus / Vitest / pytest

**Spec:** [`docs/superpowers/specs/2026-05-19-calendar-recurrence-rule-design.md`](../specs/2026-05-19-calendar-recurrence-rule-design.md)

**Predecessor:** Phase A admin_feed merged `2c1629e` (BE) / `94c0ae0f` (FE)

---

## 檔案結構

| 檔案 | 動作 | 用途 |
|---|---|---|
| `ivy-backend/utils/recurrence.py` | Create | `expand_event` + `validate_rule` 純函式 |
| `ivy-backend/tests/test_recurrence.py` | Create | 14 unit test（11 expand + 3 validate） |
| `ivy-backend/alembic/versions/<id>_school_events_recurrence_rule.py` | Create | add nullable JSONB column |
| `ivy-backend/models/event.py` | Modify | `SchoolEvent` add `recurrence_rule` Mapped column |
| `ivy-backend/api/events.py` | Modify | `EventCreate/EventUpdate` 加欄位、`_event_to_dict` 加欄位、create/update validate |
| `ivy-backend/api/calendar_admin.py` | Modify | `_fetch_event` 改 SQL + 用 expander 展開 |
| `ivy-backend/api/parent_portal/calendar.py` | Modify | 同 admin expand 邏輯 |
| `ivy-backend/tests/test_calendar_admin.py` | Modify | +2 recurrence integration test |
| `ivy-backend/tests/test_events_api.py`（若存在）/ `tests/test_events_recurrence.py` | Create/Modify | 4 個 api-level test |
| `ivy-frontend/src/components/calendar/RecurrenceEditor.vue` | Create | 3 規則型別 UI |
| `ivy-frontend/src/components/calendar/__tests__/RecurrenceEditor.test.ts` | Create | 5 vitest |
| `ivy-frontend/src/views/CalendarView.vue` | Modify | 編輯 dialog 嵌入 + form.recurrence_rule 連動 |

---

## Phase 1：純函式 expander（後端）

### Task 1: expander 純函式 + 11 單元測試

**Files:**
- Create: `ivy-backend/utils/recurrence.py`
- Create: `ivy-backend/tests/test_recurrence.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_recurrence.py
"""recurrence.expand_event 純函式測試。

純函式無 DB 依賴；只測「給定 event_date/end_date/rule/window，
回傳 (start, end) tuple list」的純邏輯。
"""

from datetime import date

import pytest

from utils.recurrence import expand_event


# ----- 無 rule（向後相容）-----

def test_recurrence_null_returns_single_occurrence():
    """rule=None 回 [(event_date, end_date or event_date)] 一筆。"""
    result = expand_event(
        event_date=date(2026, 5, 20),
        end_date=None,
        rule=None,
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    assert result == [(date(2026, 5, 20), date(2026, 5, 20))]


def test_recurrence_null_outside_window_returns_empty():
    """rule=None 且 event 在 window 外 → []"""
    result = expand_event(
        event_date=date(2026, 1, 5),
        end_date=None,
        rule=None,
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    assert result == []


# ----- weekly -----

def test_weekly_expand_4_weeks():
    """每週週二、起 5/5、until 5/26 → 4 occurrence (5/5, 5/12, 5/19, 5/26)。"""
    result = expand_event(
        event_date=date(2026, 5, 5),   # 週二
        end_date=None,
        rule={"type": "weekly", "weekday": 1, "until": "2026-05-26"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    starts = [s for s, _ in result]
    assert starts == [date(2026, 5, 5), date(2026, 5, 12), date(2026, 5, 19), date(2026, 5, 26)]


def test_weekly_until_inclusive():
    """until 當天若 match weekday，本身要產 occurrence。"""
    result = expand_event(
        event_date=date(2026, 5, 5),
        end_date=None,
        rule={"type": "weekly", "weekday": 1, "until": "2026-05-12"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    assert [s for s, _ in result] == [date(2026, 5, 5), date(2026, 5, 12)]


def test_window_clipping():
    """window 在 rule 範圍中段，只回中段 occurrence。"""
    result = expand_event(
        event_date=date(2026, 1, 6),   # 週二
        end_date=None,
        rule={"type": "weekly", "weekday": 1, "until": "2026-12-29"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    starts = [s for s, _ in result]
    # 5/5, 5/12, 5/19, 5/26 — window 內 4 個週二
    assert len(starts) == 4
    assert min(starts) >= date(2026, 5, 1)
    assert max(starts) <= date(2026, 5, 31)


# ----- monthly_day -----

def test_monthly_day_15_full_year():
    """每月 15 號 × 1 整年 = 12 occurrence。"""
    result = expand_event(
        event_date=date(2026, 1, 15),
        end_date=None,
        rule={"type": "monthly_day", "day": 15, "until": "2026-12-15"},
        window_from=date(2026, 1, 1),
        window_to=date(2026, 12, 31),
    )
    assert len(result) == 12
    assert [s.month for s, _ in result] == list(range(1, 13))


def test_monthly_day_31_skips_short_months():
    """每月 31 號在 2/4/6/9/11 月跳過（28/30 號月份不產）。"""
    result = expand_event(
        event_date=date(2026, 1, 31),
        end_date=None,
        rule={"type": "monthly_day", "day": 31, "until": "2026-12-31"},
        window_from=date(2026, 1, 1),
        window_to=date(2026, 12, 31),
    )
    months = [s.month for s, _ in result]
    # 1/3/5/7/8/10/12 月有 31 號 = 7 個
    assert months == [1, 3, 5, 7, 8, 10, 12]


# ----- monthly_nth -----

def test_monthly_nth_first_monday():
    """每月第一個週一 × 6 月。"""
    result = expand_event(
        event_date=date(2026, 5, 4),   # 5 月第一個週一
        end_date=None,
        rule={"type": "monthly_nth", "nth": 1, "weekday": 0, "until": "2026-10-31"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 10, 31),
    )
    starts = [s for s, _ in result]
    # 5/4, 6/1, 7/6, 8/3, 9/7, 10/5
    assert starts == [
        date(2026, 5, 4), date(2026, 6, 1), date(2026, 7, 6),
        date(2026, 8, 3), date(2026, 9, 7), date(2026, 10, 5),
    ]


def test_monthly_nth_last_friday():
    """nth=-1 表最後一個週五。"""
    result = expand_event(
        event_date=date(2026, 5, 29),  # 5 月最後一個週五
        end_date=None,
        rule={"type": "monthly_nth", "nth": -1, "weekday": 4, "until": "2026-07-31"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 7, 31),
    )
    starts = [s for s, _ in result]
    assert starts == [date(2026, 5, 29), date(2026, 6, 26), date(2026, 7, 31)]


def test_monthly_nth_fifth_skipped_when_absent():
    """nth=5 在不存在第 5 個的月份跳過。"""
    result = expand_event(
        event_date=date(2026, 1, 29),  # 2026-01 第 5 個週四是 1/29 — 存在
        end_date=None,
        rule={"type": "monthly_nth", "nth": 5, "weekday": 3, "until": "2026-04-30"},
        window_from=date(2026, 1, 1),
        window_to=date(2026, 4, 30),
    )
    starts = [s for s, _ in result]
    # 2026 第 5 週四：1/29 ✓ 4/30 ✓；2/3 月不存在第 5 週四
    assert starts == [date(2026, 1, 29), date(2026, 4, 30)]


# ----- multi-day recurring -----

def test_multi_day_recurring():
    """event_date+end_date 跨日 + weekly rule → 每週都跨日。"""
    result = expand_event(
        event_date=date(2026, 5, 5),
        end_date=date(2026, 5, 6),
        rule={"type": "weekly", "weekday": 1, "until": "2026-05-19"},
        window_from=date(2026, 5, 1),
        window_to=date(2026, 5, 31),
    )
    # 5/5-5/6, 5/12-5/13, 5/19-5/20
    assert result == [
        (date(2026, 5, 5), date(2026, 5, 6)),
        (date(2026, 5, 12), date(2026, 5, 13)),
        (date(2026, 5, 19), date(2026, 5, 20)),
    ]
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_recurrence.py -v
```

Expected: ImportError，11 case 全 fail。

- [ ] **Step 3: 寫實作**

```python
# utils/recurrence.py
"""行事曆重複事件純函式 expander。

設計：
- 無 DB 依賴；輸入 event_date/end_date/rule/window，輸出 (start, end) tuple list
- 三種 rule type：weekly / monthly_day / monthly_nth
- weekday 用 Python 約定：0=Mon..6=Sun（matches `date.weekday()`）
- until inclusive；event_date 必為第一個 occurrence
- 多日事件 (end_date != None) 展開時每 occurrence 保持 `end_date - event_date` 長度
"""

from datetime import date, timedelta
from typing import TypedDict, Literal

WeeklyRule = TypedDict(
    "WeeklyRule",
    {"type": Literal["weekly"], "weekday": int, "until": str},
)
MonthlyDayRule = TypedDict(
    "MonthlyDayRule",
    {"type": Literal["monthly_day"], "day": int, "until": str},
)
MonthlyNthRule = TypedDict(
    "MonthlyNthRule",
    {"type": Literal["monthly_nth"], "nth": int, "weekday": int, "until": str},
)


def expand_event(
    event_date: date,
    end_date: date | None,
    rule: dict | None,
    window_from: date,
    window_to: date,
) -> list[tuple[date, date]]:
    """展開事件成 [(start, end), ...]，只回 window 內 occurrence。"""
    if rule is None:
        single_end = end_date or event_date
        if event_date > window_to or single_end < window_from:
            return []
        return [(event_date, single_end)]

    until = date.fromisoformat(rule["until"])
    duration = (end_date - event_date) if end_date else timedelta(0)
    rtype = rule["type"]

    if rtype == "weekly":
        return _expand_weekly(event_date, duration, rule["weekday"], until, window_from, window_to)
    if rtype == "monthly_day":
        return _expand_monthly_day(event_date, duration, rule["day"], until, window_from, window_to)
    if rtype == "monthly_nth":
        return _expand_monthly_nth(
            event_date, duration, rule["nth"], rule["weekday"], until, window_from, window_to
        )
    raise ValueError(f"unknown rule type: {rtype}")


def _expand_weekly(
    event_date: date, duration: timedelta, weekday: int,
    until: date, wf: date, wt: date,
) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cur = event_date
    while cur <= until:
        end = cur + duration
        if cur <= wt and end >= wf:
            out.append((cur, end))
        cur = cur + timedelta(days=7)
    return out


def _expand_monthly_day(
    event_date: date, duration: timedelta, day: int,
    until: date, wf: date, wt: date,
) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    y, m = event_date.year, event_date.month
    while True:
        try:
            cur = date(y, m, day)
        except ValueError:
            cur = None  # 該月無此日
        if cur is not None:
            if cur > until:
                break
            end = cur + duration
            if cur <= wt and end >= wf:
                out.append((cur, end))
        # 跳下個月
        m += 1
        if m > 12:
            m = 1
            y += 1
        if date(y, m, 1) > until:
            break
    return out


def _expand_monthly_nth(
    event_date: date, duration: timedelta, nth: int, weekday: int,
    until: date, wf: date, wt: date,
) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    y, m = event_date.year, event_date.month
    while True:
        cur = _nth_weekday_of_month(y, m, nth, weekday)
        if cur is not None:
            if cur > until:
                break
            end = cur + duration
            if cur <= wt and end >= wf:
                out.append((cur, end))
        m += 1
        if m > 12:
            m = 1
            y += 1
        if date(y, m, 1) > until:
            break
    return out


def _nth_weekday_of_month(year: int, month: int, nth: int, weekday: int) -> date | None:
    """月份內第 nth 個 weekday（0=Mon..6=Sun）；nth=-1 表最後一個；不存在回 None。"""
    import calendar as _cal
    _, last_day = _cal.monthrange(year, month)

    matches = [
        date(year, month, d)
        for d in range(1, last_day + 1)
        if date(year, month, d).weekday() == weekday
    ]
    if not matches:
        return None
    if nth == -1:
        return matches[-1]
    idx = nth - 1
    if 0 <= idx < len(matches):
        return matches[idx]
    return None
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_recurrence.py -v
```

Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/recurrence.py tests/test_recurrence.py
git commit -m "feat(calendar): add recurrence expander pure functions

支援 weekly / monthly_day / monthly_nth 三種規則展開。
無 DB 依賴的純函式，admin_feed 與家長端 calendar 共用。
11 unit test 覆蓋 single occurrence / window clipping / multi-day /
short-month skip / 5th-week skip / last-of-month。"
```

---

### Task 2: validate_rule 函式 + 3 個失敗 case 測試

**Files:**
- Modify: `ivy-backend/utils/recurrence.py`
- Modify: `ivy-backend/tests/test_recurrence.py`

- [ ] **Step 1: 加 3 個失敗測試**

接續寫進 `tests/test_recurrence.py`：

```python
# ----- validate_rule -----

from utils.recurrence import validate_rule


def test_validate_weekday_mismatch_rejected():
    """rule weekday=1（週二）但 event_date 是週三 → ValueError。"""
    with pytest.raises(ValueError, match="weekday"):
        validate_rule(
            event_date=date(2026, 5, 6),  # 週三
            rule={"type": "weekly", "weekday": 1, "until": "2026-05-26"},
        )


def test_validate_until_over_730_days_rejected():
    """until - event_date > 730 → ValueError runaway。"""
    with pytest.raises(ValueError, match="730"):
        validate_rule(
            event_date=date(2026, 1, 1),
            rule={"type": "weekly", "weekday": 3, "until": "2029-01-01"},  # ~1096 days
        )


def test_validate_unknown_type_rejected():
    """unknown rule type → ValueError。"""
    with pytest.raises(ValueError, match="rule type"):
        validate_rule(
            event_date=date(2026, 5, 5),
            rule={"type": "yearly", "until": "2030-01-01"},
        )


def test_validate_monthly_day_31_for_jan_31_event_passes():
    """event_date 是 1/31 + monthly_day 31 → 通過（不會 weekday check）。"""
    validate_rule(
        event_date=date(2026, 1, 31),
        rule={"type": "monthly_day", "day": 31, "until": "2026-06-30"},
    )  # 不 raise
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_recurrence.py -v -k "validate"
```

Expected: 4 fail (ImportError).

- [ ] **Step 3: 加 `validate_rule` 到 `utils/recurrence.py`**

加在 `expand_event` 上方：

```python
MAX_DURATION_DAYS = 730


def validate_rule(event_date: date, rule: dict) -> None:
    """驗證 rule 結構 + 業務規則；違反 raise ValueError。

    呼叫方應在 API 入口（events.py create/update）catch 並回 422。
    """
    rtype = rule.get("type")
    if rtype not in ("weekly", "monthly_day", "monthly_nth"):
        raise ValueError(f"unknown rule type: {rtype}")

    until_str = rule.get("until")
    if not until_str:
        raise ValueError("rule.until is required")
    try:
        until = date.fromisoformat(until_str)
    except ValueError as e:
        raise ValueError(f"rule.until invalid date: {until_str}") from e

    if until <= event_date:
        raise ValueError("rule.until must be after event_date")

    if (until - event_date).days > MAX_DURATION_DAYS:
        raise ValueError(f"rule.until - event_date exceeds {MAX_DURATION_DAYS} days (runaway 防護)")

    if rtype == "weekly":
        wd = rule.get("weekday")
        if not isinstance(wd, int) or not (0 <= wd <= 6):
            raise ValueError("weekly.weekday must be int 0..6")
        if event_date.weekday() != wd:
            raise ValueError(f"event_date weekday ({event_date.weekday()}) does not match rule.weekday ({wd})")
    elif rtype == "monthly_day":
        d = rule.get("day")
        if not isinstance(d, int) or not (1 <= d <= 31):
            raise ValueError("monthly_day.day must be int 1..31")
        if event_date.day != d:
            raise ValueError(f"event_date.day ({event_date.day}) does not match rule.day ({d})")
    elif rtype == "monthly_nth":
        nth = rule.get("nth")
        if not isinstance(nth, int) or not (nth in (-1, 1, 2, 3, 4, 5)):
            raise ValueError("monthly_nth.nth must be int in {-1, 1..5}")
        wd = rule.get("weekday")
        if not isinstance(wd, int) or not (0 <= wd <= 6):
            raise ValueError("monthly_nth.weekday must be int 0..6")
        if event_date.weekday() != wd:
            raise ValueError(f"event_date weekday ({event_date.weekday()}) does not match rule.weekday ({wd})")
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_recurrence.py -v
```

Expected: 15 passed (11 expand + 4 validate).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/recurrence.py tests/test_recurrence.py
git commit -m "feat(calendar): add recurrence rule validator (validate_rule)

3 種規則的 schema + 業務驗證：weekday match event_date / day match /
nth in {-1,1..5} / until ≤ +730 天 runaway 防護。"
```

---

## Phase 2：資料模型 + Migration

### Task 3: Alembic migration

**Files:**
- Create: `ivy-backend/alembic/versions/<new-id>_school_events_recurrence_rule.py`

- [ ] **Step 1: 看現有 migration 命名慣例**

```bash
cd ~/Desktop/ivy-backend && ls alembic/versions/ | tail -10
```

Pick 一個 4-letter prefix（例如 `recurr01` 或既有風格如 `mfc00001`）。

- [ ] **Step 2: 看上一個 migration head**

```bash
cd ~/Desktop/ivy-backend && alembic heads
```

複製 head 作為 `down_revision`。

- [ ] **Step 3: 寫 migration**

```python
# alembic/versions/<id>_school_events_recurrence_rule.py
"""Add recurrence_rule JSONB column to school_events

Revision ID: recurr01
Revises: <CURRENT_HEAD>
Create Date: 2026-05-19
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "recurr01"
down_revision = "<CURRENT_HEAD>"  # ← 改成 step 2 抓到的值
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "school_events",
        sa.Column(
            "recurrence_rule",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="重複規則 JSONB；null 表單次事件",
        ),
    )


def downgrade():
    op.drop_column("school_events", "recurrence_rule")
```

- [ ] **Step 4: 驗證 migration 可上下**

```bash
cd ~/Desktop/ivy-backend
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Expected: 三步均 0 error。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add alembic/versions/recurr01_school_events_recurrence_rule.py
git commit -m "feat(db): add recurrence_rule JSONB column to school_events (recurr01)

Nullable，舊資料保持 null；無 backfill 無索引（JSONB 內 until
不適合 BTREE，本 endpoint 用 JSONB cast 拉、量小不需 GIN）。"
```

---

### Task 4: Model 加欄位

**Files:**
- Modify: `ivy-backend/models/event.py`

- [ ] **Step 1: 看現有 SchoolEvent column 寫法**

```bash
cd ~/Desktop/ivy-backend && grep -nA1 "class SchoolEvent" models/event.py
```

確認用 `Column(...)` 還是 `Mapped[...]`。

- [ ] **Step 2: 加欄位**

於 `SchoolEvent` class 內、現有 `requires_acknowledgment` 附近加：

```python
# 若 class 用 Column(...) 風格：
recurrence_rule = Column(
    JSONB,
    nullable=True,
    comment="重複規則 JSONB；null 表單次事件",
)
```

import：
```python
from sqlalchemy.dialects.postgresql import JSONB
```

（若 SchoolEvent 是 `Mapped[...]` 風格，用 `Mapped[dict | None] = mapped_column(JSONB, nullable=True)`）

- [ ] **Step 3: smoke test — model load**

```bash
cd ~/Desktop/ivy-backend && python -c "from models.event import SchoolEvent; print(SchoolEvent.recurrence_rule)"
```

Expected: 印出 column 物件（不 raise）。

- [ ] **Step 4: 跑 Phase A test 確認沒 break**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -q
```

Expected: 23 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/event.py
git commit -m "feat(model): SchoolEvent add recurrence_rule column"
```

---

## Phase 3：API 整合

### Task 5: events.py CRUD 接 recurrence_rule

**Files:**
- Modify: `ivy-backend/api/events.py`
- Create: `ivy-backend/tests/test_events_recurrence.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_events_recurrence.py
"""events.py CRUD 整合 recurrence_rule 測試。"""

from datetime import date

import pytest


def test_create_event_with_weekly_rule(events_client, admin_token):
    """POST 含 recurrence_rule 寫進 DB。"""
    client, _ = events_client
    r = client.post(
        "/api/events",
        json={
            "title": "週會",
            "event_date": "2026-05-05",
            "event_type": "meeting",
            "recurrence_rule": {"type": "weekly", "weekday": 1, "until": "2026-12-29"},
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["recurrence_rule"]["type"] == "weekly"


def test_create_event_with_invalid_rule_returns_422(events_client, admin_token):
    """weekday mismatch → 422。"""
    client, _ = events_client
    r = client.post(
        "/api/events",
        json={
            "title": "週會",
            "event_date": "2026-05-06",  # 週三
            "event_type": "meeting",
            "recurrence_rule": {"type": "weekly", "weekday": 1, "until": "2026-12-29"},  # 週二
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422
    assert "weekday" in r.json()["detail"].lower()


def test_create_event_runaway_until_rejected(events_client, admin_token):
    """until > 730 天 → 422。"""
    client, _ = events_client
    r = client.post(
        "/api/events",
        json={
            "title": "永遠",
            "event_date": "2026-01-01",
            "event_type": "meeting",
            "recurrence_rule": {"type": "weekly", "weekday": 3, "until": "2030-01-01"},
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422
    assert "730" in r.json()["detail"]


def test_update_event_clear_recurrence(events_client, admin_token):
    """PUT recurrence_rule=null 清空規則回單次事件。"""
    client, _ = events_client
    r = client.post(
        "/api/events",
        json={
            "title": "x", "event_date": "2026-05-05", "event_type": "meeting",
            "recurrence_rule": {"type": "weekly", "weekday": 1, "until": "2026-06-30"},
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    eid = r.json()["id"]

    r = client.put(
        f"/api/events/{eid}",
        json={"recurrence_rule": None},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["recurrence_rule"] is None
```

**Fixture `events_client` + `admin_token`**: 沿用 Phase A `tests/test_calendar_admin.py` 既有 pattern（per-file TestClient + session_factory + permissions=-1 admin）。先讀 `tests/test_calendar_admin.py` 開頭 fixture 區段，把它複製進本檔開頭，把 `calendar_admin_router` 換成 `events_router`，函式名稱改 `events_client`。

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_events_recurrence.py -v
```

Expected: 4 fail（Pydantic 不接 recurrence_rule 欄位）。

- [ ] **Step 3: 改 `api/events.py`**

`EventCreate` 加：
```python
recurrence_rule: Optional[dict] = None
```

`EventUpdate` 加：
```python
recurrence_rule: Optional[dict] = None
```

`_event_to_dict` 加：
```python
"recurrence_rule": ev.recurrence_rule,
```

`create_event` 內，在 `session.add(ev)` 前加：
```python
if payload.recurrence_rule is not None:
    try:
        validate_rule(payload.event_date, payload.recurrence_rule)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
```

`update_event` 同樣，在 `if payload.event_date is not None: ev.event_date = ...` 區塊後加：
```python
if "recurrence_rule" in payload.model_fields_set:
    if payload.recurrence_rule is not None:
        ev_date = payload.event_date or ev.event_date
        try:
            validate_rule(ev_date, payload.recurrence_rule)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    ev.recurrence_rule = payload.recurrence_rule
```

import：
```python
from utils.recurrence import validate_rule
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_events_recurrence.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/events.py tests/test_events_recurrence.py
git commit -m "feat(events): CRUD 接 recurrence_rule + validate_rule guard

create/update 接 recurrence_rule（Optional dict），寫入前過
validate_rule；違反規則回 422 + 中文 detail。
_event_to_dict 加回傳欄位。"
```

---

### Task 6: admin_feed 用 expander 展開

**Files:**
- Modify: `ivy-backend/api/calendar_admin.py`
- Modify: `ivy-backend/tests/test_calendar_admin.py`

- [ ] **Step 1: 加 2 個 integration test**

接續 `tests/test_calendar_admin.py` 末尾：

```python
# ---------------------------------------------------------------------------
# recurrence layer (Phase C)
# ---------------------------------------------------------------------------


def test_admin_feed_expands_weekly_event(calendar_admin_client):
    """source event 含 weekly rule → admin_feed 展開成多筆 occurrence。"""
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        ev = SchoolEvent(
            title="園務週會", event_date=date(2026, 5, 5), is_active=True,
            recurrence_rule={"type": "weekly", "weekday": 1, "until": "2026-05-26"},
        )
        s.add(ev)
        s.commit()
        eid = ev.id

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = r.json()["items"]
    assert len(items) == 4
    starts = sorted(it["start"] for it in items)
    assert starts == ["2026-05-05", "2026-05-12", "2026-05-19", "2026-05-26"]
    # occurrence id 格式 `{pk}@{iso}`
    for it in items:
        assert it["id"].startswith(f"{eid}@")


def test_admin_feed_source_before_window_returns_in_window_occurrences(calendar_admin_client):
    """source event_date 在 window 前，但 rule 展開的 occurrence 在 window 內 → 應拉到。"""
    client, sf = calendar_admin_client
    tok = _login_admin(client, sf)
    with sf() as s:
        s.add(SchoolEvent(
            title="跨季週會", event_date=date(2026, 1, 6),  # 週二
            is_active=True,
            recurrence_rule={"type": "weekly", "weekday": 1, "until": "2026-12-29"},
        ))
        s.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    items = r.json()["items"]
    assert len(items) == 4  # 5 月內的 4 個週二
    starts = sorted(it["start"] for it in items)
    assert starts == ["2026-05-05", "2026-05-12", "2026-05-19", "2026-05-26"]
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v -k "expands_weekly or source_before"
```

Expected: 2 fail (current `_fetch_event` 不展開、source 在 window 前看不到)。

- [ ] **Step 3: 改 `_fetch_event` in `api/calendar_admin.py`**

加 import：
```python
from sqlalchemy import cast, Date
from utils.recurrence import expand_event
```

把 `_fetch_event` 整支替換為：

```python
def _fetch_event(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permissions", 0), Permission.CALENDAR):
        return []
    # 查詢條件：
    # - 非重複事件：window 內按既有 overlap clause
    # - 重複事件：source event_date <= to AND rule.until >= from_
    until_cast = cast(SchoolEvent.recurrence_rule["until"].astext, Date)
    stmt = (
        select(
            SchoolEvent.id,
            SchoolEvent.title,
            SchoolEvent.event_date,
            SchoolEvent.end_date,
            SchoolEvent.requires_acknowledgment,
            SchoolEvent.event_type,
            SchoolEvent.recurrence_rule,
        )
        .where(SchoolEvent.is_active.is_(True))
        .where(
            or_(
                # 非重複：原 overlap
                SchoolEvent.recurrence_rule.is_(None)
                & (SchoolEvent.event_date <= to)
                & or_(
                    SchoolEvent.end_date.is_(None) & (SchoolEvent.event_date >= from_),
                    SchoolEvent.end_date.is_not(None) & (SchoolEvent.end_date >= from_),
                ),
                # 重複：source 在 to 之前且 until 在 from 之後
                SchoolEvent.recurrence_rule.is_not(None)
                & (SchoolEvent.event_date <= to)
                & (until_cast >= from_),
            )
        )
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        color = (
            LAYER_COLORS["event"]["ack"]
            if r.requires_acknowledgment
            else LAYER_COLORS["event"]["default"]
        )
        occurrences = expand_event(
            r.event_date, r.end_date, r.recurrence_rule, from_, to,
        )
        for occ_start, occ_end in occurrences:
            item_id = (
                f"{r.id}@{occ_start.isoformat()}" if r.recurrence_rule else r.id
            )
            out.append(CalendarFeedItem(
                layer="event",
                id=item_id,
                title=r.title,
                start=occ_start,
                end=occ_end,
                all_day=True,
                color=color,
                link=f"/calendar?eventId={r.id}",
                meta={
                    "event_type": r.event_type,
                    "requires_acknowledgment": r.requires_acknowledgment,
                    "is_recurring": r.recurrence_rule is not None,
                },
            ))
    return out
```

> **SQLite 注意**：test 用 SQLite，`cast(JSON["until"].astext, Date)` 在 SQLite 上行為與 PostgreSQL 不同。若 SQLite 跑 fail，改 SQL 條件為 Python-side filter（後 fetch 後在 expander 內過濾），或在 test 環境用 SQLite-friendly 寫法（query 拉全部 active+ 重複 event，由 `expand_event` 的 window clipping 過濾）。

- [ ] **Step 4: 跑全 calendar_admin test**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v
```

Expected: 25 passed (23 + 2 new)。

如果原本 23 個有任何 break（特別是 `test_event_layer_basic` 等假設 id 是 int），這代表 id 格式變化需要更新斷言。先檢查是否本 task 引入 regression：若有，**修舊 test 把 id 斷言改為 `assert it["id"] == eid_for_nonrecurring` 或 split('@')**。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/calendar_admin.py tests/test_calendar_admin.py
git commit -m "feat(calendar): admin_feed expand recurring events query-time

_fetch_event 用 utils.recurrence.expand_event 展開；
occurrence id 格式 \`{pk}@{iso}\` 區分原 PK；
SQL WHERE 拉重複事件條件加 cast(rule.until, Date) >= from。"
```

---

### Task 7: parent_portal/calendar.py 同步展開

**Files:**
- Modify: `ivy-backend/api/parent_portal/calendar.py`

- [ ] **Step 1: 看現有 SchoolEvent 用法**

```bash
cd ~/Desktop/ivy-backend && sed -n '125,160p' api/parent_portal/calendar.py
```

確認既有 query + 後處理結構。

- [ ] **Step 2: 加 expansion 邏輯**

把既有：
```python
events = session.query(SchoolEvent).filter(
    SchoolEvent.is_active == True,
    SchoolEvent.event_date < end,
    # ... 其他既有 filter
).all()
for ev in events:
    # ... build output
```

改為（保留既有 filter 框架）：

```python
from utils.recurrence import expand_event

events = session.query(SchoolEvent).filter(
    SchoolEvent.is_active == True,
    # 同 admin_feed：放寬條件涵蓋重複事件
    or_(
        and_(
            SchoolEvent.recurrence_rule.is_(None),
            SchoolEvent.event_date < end,
            or_(
                SchoolEvent.end_date.is_(None),
                SchoolEvent.end_date >= start,
            ),
        ),
        and_(
            SchoolEvent.recurrence_rule.is_not(None),
            SchoolEvent.event_date <= end,
            # ...（若 SQLite 環境，避免 cast；改用 Python-side window clipping）
        ),
    ),
).all()

for ev in events:
    for occ_start, occ_end in expand_event(
        ev.event_date, ev.end_date, ev.recurrence_rule, start, end,
    ):
        # 既有 build output 邏輯，但用 occ_start/occ_end 取代 ev.event_date/end_date
        ...
```

**注意**：parent_portal 的回傳結構不一定有 id 欄位（可能直接 dict）。若有，同 admin_feed 用 `{pk}@{iso}` 格式；若沒有，直接用 occurrence date 作為 list key。

實作前先讀完整個 calendar.py 確認 build output 區段（約 130-170 行），把改動限制在「展開」即可。

- [ ] **Step 3: 補 1 個 integration test**

於 `tests/test_parent_portal_calendar.py`（若不存在則 create + sibling fixture）加：

```python
def test_parent_calendar_expands_recurring_event(parent_calendar_client):
    """家長端週行程聚合展開重複事件。"""
    client, sf, parent_user = parent_calendar_client
    with sf() as s:
        s.add(SchoolEvent(
            title="家長週會",
            event_date=date(2026, 5, 5),
            is_active=True,
            recurrence_rule={"type": "weekly", "weekday": 1, "until": "2026-05-26"},
        ))
        s.commit()

    r = client.get(
        "/api/parent_portal/calendar/week?days=30",
        # ... 沿用既有 auth + endpoint pattern；先讀現有 test_parent_portal 系列文件
    )
    assert r.status_code == 200
    # 驗證 response 中含 ≥ 2 個此事件的 occurrence（5/5, 5/12 在 30 天 window 內）
    ...
```

> **若 `tests/test_parent_portal_calendar.py` 不存在或測試骨架不夠成熟**：本 task 可降級為 manual smoke test — 用 `pytest --collect-only -q tests/test_parent_portal*` 確認測試檔狀態，若無就只更動 production code、補 1 個小 unit test 驗 expander wired（用 mock session）。不要為了單一 test 重新建立整套 parent_portal test infra。

- [ ] **Step 4: 跑測試確認**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_parent_portal_calendar.py -v 2>&1 | tail -10
```

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/parent_portal/calendar.py tests/test_parent_portal_calendar.py
git commit -m "feat(parent_calendar): expand recurring SchoolEvent occurrences

家長端週行程聚合與 admin_feed 共用 utils.recurrence.expand_event；
回傳同 source event 多個 occurrence。"
```

---

## Phase 4：前端

### Task 8: RecurrenceEditor.vue + 5 vitest

**Files:**
- Create: `ivy-frontend/src/components/calendar/RecurrenceEditor.vue`
- Create: `ivy-frontend/src/components/calendar/__tests__/RecurrenceEditor.test.ts`

- [ ] **Step 1: 寫失敗測試**

```ts
// src/components/calendar/__tests__/RecurrenceEditor.test.ts
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RecurrenceEditor from '../RecurrenceEditor.vue'

describe('RecurrenceEditor', () => {
  it('defaults to disabled, emits null when first mounted', () => {
    const w = mount(RecurrenceEditor, { props: { modelValue: null } })
    expect(w.find('input[type="checkbox"]').element.checked).toBe(false)
  })

  it('emits weekly rule when user enables + selects weekday + until', async () => {
    const w = mount(RecurrenceEditor, { props: { modelValue: null } })
    await w.find('input[type="checkbox"]').setValue(true)
    // 預設 weekly + weekday=0 + 預設 until 今天+1個月（後續測 emit）
    expect(w.emitted('update:modelValue')).toBeTruthy()
    const lastEmit = w.emitted('update:modelValue')!.at(-1)![0] as any
    expect(lastEmit.type).toBe('weekly')
    expect(typeof lastEmit.weekday).toBe('number')
    expect(typeof lastEmit.until).toBe('string')
  })

  it('switches rule type emits correct shape', async () => {
    const w = mount(RecurrenceEditor, {
      props: { modelValue: { type: 'weekly', weekday: 1, until: '2026-12-29' } },
    })
    const radios = w.findAll('input[type="radio"]')
    // 第二個 radio = monthly_day
    await radios[1].setValue(true)
    const lastEmit = w.emitted('update:modelValue')!.at(-1)![0] as any
    expect(lastEmit.type).toBe('monthly_day')
    expect('day' in lastEmit).toBe(true)
  })

  it('emits null when user unchecks enabled', async () => {
    const w = mount(RecurrenceEditor, {
      props: { modelValue: { type: 'weekly', weekday: 1, until: '2026-12-29' } },
    })
    await w.find('input[type="checkbox"]').setValue(false)
    const lastEmit = w.emitted('update:modelValue')!.at(-1)![0]
    expect(lastEmit).toBe(null)
  })

  it('renders Chinese label for each weekday option', () => {
    const w = mount(RecurrenceEditor, {
      props: { modelValue: { type: 'weekly', weekday: 0, until: '2026-12-29' } },
    })
    // 至少要看到一/二/三 等中文
    const text = w.text()
    expect(text).toMatch(/[一二三四五六日]/)
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/components/calendar/__tests__/RecurrenceEditor.test.ts
```

Expected: import error.

- [ ] **Step 3: 寫元件**

```vue
<!-- src/components/calendar/RecurrenceEditor.vue -->
<template>
  <div class="recurrence-editor">
    <el-checkbox v-model="enabled">每週/每月重複</el-checkbox>

    <template v-if="enabled">
      <el-radio-group v-model="ruleType" class="rule-type-group">
        <el-radio value="weekly">每週 X</el-radio>
        <el-radio value="monthly_day">每月 N 號</el-radio>
        <el-radio value="monthly_nth">每月第 N 個星期 X</el-radio>
      </el-radio-group>

      <div v-if="ruleType === 'weekly'" class="rule-field">
        <span>星期</span>
        <el-select v-model="weekday" style="width: 100px">
          <el-option v-for="(label, idx) in WEEKDAYS" :key="idx" :value="idx" :label="label" />
        </el-select>
      </div>

      <div v-if="ruleType === 'monthly_day'" class="rule-field">
        <span>每月</span>
        <el-input-number v-model="day" :min="1" :max="31" />
        <span>號</span>
      </div>

      <div v-if="ruleType === 'monthly_nth'" class="rule-field">
        <span>每月第</span>
        <el-select v-model="nth" style="width: 100px">
          <el-option v-for="n in [1,2,3,4,5,-1]" :key="n" :value="n" :label="nthLabel(n)" />
        </el-select>
        <span>個</span>
        <el-select v-model="weekday" style="width: 100px">
          <el-option v-for="(label, idx) in WEEKDAYS" :key="idx" :value="idx" :label="label" />
        </el-select>
      </div>

      <div class="rule-field">
        <span>結束日</span>
        <el-date-picker v-model="until" type="date" value-format="YYYY-MM-DD" />
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import {
  ElCheckbox,
  ElRadioGroup,
  ElRadio,
  ElSelect,
  ElOption,
  ElInputNumber,
  ElDatePicker,
} from 'element-plus'

interface WeeklyRule { type: 'weekly'; weekday: number; until: string }
interface MonthlyDayRule { type: 'monthly_day'; day: number; until: string }
interface MonthlyNthRule { type: 'monthly_nth'; nth: number; weekday: number; until: string }
type RecurrenceRule = WeeklyRule | MonthlyDayRule | MonthlyNthRule

const props = defineProps<{ modelValue: RecurrenceRule | null }>()
const emit = defineEmits<{ 'update:modelValue': [RecurrenceRule | null] }>()

const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日']

const enabled = ref(props.modelValue !== null)
const ruleType = ref<RecurrenceRule['type']>(props.modelValue?.type ?? 'weekly')
const weekday = ref<number>((props.modelValue as any)?.weekday ?? 0)
const day = ref<number>((props.modelValue as any)?.day ?? 1)
const nth = ref<number>((props.modelValue as any)?.nth ?? 1)
const until = ref<string>(props.modelValue?.until ?? defaultUntil())

function defaultUntil(): string {
  const d = new Date()
  d.setMonth(d.getMonth() + 1)
  return d.toISOString().slice(0, 10)
}

function nthLabel(n: number): string {
  return n === -1 ? '最後一個' : String(n)
}

const buildRule = computed<RecurrenceRule | null>(() => {
  if (!enabled.value) return null
  if (ruleType.value === 'weekly') {
    return { type: 'weekly', weekday: weekday.value, until: until.value }
  }
  if (ruleType.value === 'monthly_day') {
    return { type: 'monthly_day', day: day.value, until: until.value }
  }
  return {
    type: 'monthly_nth',
    nth: nth.value,
    weekday: weekday.value,
    until: until.value,
  }
})

watch(buildRule, (v) => emit('update:modelValue', v))
</script>

<style scoped>
.recurrence-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px 0;
}
.rule-type-group {
  margin-left: 24px;
}
.rule-field {
  margin-left: 24px;
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
```

- [ ] **Step 4: 跑測試**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/components/calendar/__tests__/RecurrenceEditor.test.ts
```

Expected: 5 passed.

- [ ] **Step 5: typecheck**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck 2>&1 | tail -5
```

Expected: 0 errors.

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/components/calendar/RecurrenceEditor.vue src/components/calendar/__tests__/RecurrenceEditor.test.ts
git commit -m "feat(calendar): RecurrenceEditor component (3 rule types + Chinese labels)

v-model 雙向綁 RecurrenceRule | null；
checkbox 關閉 emit null；切 type 重新 build payload；
WEEKDAYS 一..日 中文 label。"
```

---

### Task 9: CalendarView 嵌入 RecurrenceEditor

**Files:**
- Modify: `ivy-frontend/src/views/CalendarView.vue`

- [ ] **Step 1: 找事件編輯 dialog**

```bash
cd ~/Desktop/ivy-frontend && grep -nE "dialogVisible|form.event_date|saveEvent" src/views/CalendarView.vue | head -10
```

確認既有 `form` ref 結構（lines ~476-517）。

- [ ] **Step 2: 改 `<script setup>`**

加 import：
```ts
import RecurrenceEditor from '@/components/calendar/RecurrenceEditor.vue'

// 在既有 form ref 上加 recurrence_rule（若已是 reactive 物件，加 field）：
const form = reactive<{
  title: string
  description: string
  event_date: string
  end_date: string | null
  // ... 其他既有欄位
  recurrence_rule: any  // RecurrenceRule | null
}>({
  // 既有欄位...
  recurrence_rule: null,
})
```

`saveEvent`（PUT/POST request body）加：
```ts
recurrence_rule: form.recurrence_rule,
```

當編輯既有事件 load form 時，從 response 拿 `recurrence_rule` 並塞回：
```ts
form.recurrence_rule = event.recurrence_rule ?? null
```

- [ ] **Step 3: 改 template 加入 editor**

於 dialog form 內、「說明」item 上方插：

```vue
<el-form-item label="重複">
  <RecurrenceEditor v-model="form.recurrence_rule" />
</el-form-item>
```

- [ ] **Step 4: typecheck + build**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck 2>&1 | tail -5 && npm run build 2>&1 | tail -5
```

Expected: 0 errors, build exit 0.

- [ ] **Step 5: 既有 CalendarView 測試（若有）跑一輪**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/views/__tests__/CalendarView 2>/dev/null | tail -10 || echo "no existing tests"
```

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/views/CalendarView.vue
git commit -m "feat(calendar): wire RecurrenceEditor into event edit dialog

form.recurrence_rule 雙向綁；load event 時從 response 帶入；
saveEvent POST/PUT body 帶 recurrence_rule。"
```

---

## Phase 5：收尾

### Task 10: 手測 + 全套 regression

- [ ] **Step 1: 套 migration（dev DB）**

```bash
cd ~/Desktop/ivy-backend && alembic upgrade head
```

- [ ] **Step 2: 啟 dev server**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

- [ ] **Step 3: 手測 checklist**

`http://localhost:5173/calendar` 登入管理員：

- [ ] 新增事件，「重複」欄勾選 weekly weekday=1，until 一個月後 → 儲存
- [ ] 月曆連續 4 週週二都看到該事件
- [ ] 點任一週的事件 → 開 dialog，看到 recurrence info（不是只有單筆）
- [ ] 編輯 weekly → 改 weekday=3 + until 延後 → 儲存後重整，所有 occurrence 移到週四
- [ ] 取消「重複」勾選 → 儲存後只剩第一筆（單次事件）
- [ ] 422 case：新增事件 event_date 是週三 + rule weekday=1 → 後端拒絕，UI 顯示錯誤訊息
- [ ] 切到下個月看 monthly_day 規則展開正確
- [ ] 家長端 `/parent` 登入，看週行程是否含展開後的事件

- [ ] **Step 4: 跑兩端全套 regression**

```bash
cd ~/Desktop/ivy-backend && pytest --ignore=tests/test_mcp_activity_crud_tools.py --ignore=tests/test_sentry_scrubber.py -q 2>&1 | tail -5
cd ~/Desktop/ivy-frontend && npx vitest run 2>&1 | tail -10
```

Expected:
- 後端：4320+ passed（+15 recurrence + 4 events + 2 calendar_admin + 1 parent_calendar = ~22 新 test），3 pre-existing fail (`test_audit_router`)
- 前端：2326+ passed（+5 RecurrenceEditor）

- [ ] **Step 5: 無 commit；若手測修了 UX 細節，獨立 commit**

---

## Self-Review

**1. Spec coverage：**

| Spec 章節 | Task |
|---|---|
| `recurrence_rule` JSONB column | Task 3 (migration) + Task 4 (model) |
| 3 rule types | Task 1 (expander) |
| Query-time expansion | Task 6 (admin) + Task 7 (parent) |
| Edit-all-only v1 | Task 5 (events.py update) |
| `until ≤ +730 天` runaway | Task 2 (validate_rule) |
| `event_date` 必 match weekday | Task 2 (validate_rule) |
| 多日 + 重複 | Task 1 (`test_multi_day_recurring`) |
| occurrence id `{pk}@{date}` | Task 6 (`_fetch_event`) |
| Front-end `RecurrenceEditor.vue` | Task 8 |
| CalendarView dialog 整合 | Task 9 |

**2. Placeholder scan**：無 TBD / TODO；Task 3 step 2 / Task 4 step 1 有「先 grep 看格式」屬必要的環境檢查、不是空洞 placeholder。

**3. Type consistency**：`expand_event` 簽章在 Task 1 定，後續 Task 6/7 同樣呼叫；`validate_rule` 簽章在 Task 2 定，Task 5 呼叫；`RecurrenceRule` TS interface 在 Task 8 定，Task 9 使用 `form.recurrence_rule: any` 過 type（已標註，可日後收緊）。

---

## Out of Scope（不在本 plan）

- Edit-this-only / exception 表 — v2 spec
- iCal/ics 輸出 — Phase D 候選
- 重複事件搬入 Phase B 拖拉時的 422 處理 — 在 Phase B plan 內處理
- weekday 多選（每週一三五）— v2
- 節日跳過 — v2

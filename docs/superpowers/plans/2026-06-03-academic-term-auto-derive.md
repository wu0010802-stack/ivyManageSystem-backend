# 學期改為系統全自動判斷 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除「學年/學期」手動設定，改由系統依今天日期全自動判斷當前學期，並在學期跨界（8/1、2/1）時自動觸發既有結轉副作用。

**Architecture:** 保留 `academic_terms` 表（改由系統維護）；`resolve_current_academic_term()` 改為日期推導為主、`is_current` 降級為排程器的結轉標記；新增每日「自動切換排程器」用日期 vs `is_current` 偵測跨界、重用既有 `fire_term_changed()` 觸發班級延續/假別結轉；一次性 migration 在部署時靜默對齊避免上線誤觸發；前端移除設定分頁與 API。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（後端）、Vue 3 + Vite + TypeScript（前端）、pytest / vitest。

**Spec:** `docs/superpowers/specs/2026-06-03-academic-term-auto-derive-design.md`

**全程規範：** 繁體中文 commit message（Conventional Commits，一個 commit 一件事）；後端 .py Edit 後有 PostToolUse black hook（surgical edit 用 `python3` 的 `str.replace` 繞過避免 cosmetic creep）；前後端分開 commit；不擅自 push、不切換 user 既有分支。本計畫在一支 feature 分支上做後端、另一支做前端。

---

## File Structure

**後端（ivy-backend）新增/修改：**
- `utils/academic.py` — 改 `resolve_current_academic_term()` 為日期推導；新增純函式 `term_bounds()`。
- `services/academic_term_turnover_scheduler.py` — **新檔**。`reconcile_academic_term()` 核心 + async loop。
- `config/scheduler.py` — 新增兩個 setting。
- `main.py` — 註冊新排程器 + 移除 academic_terms router。
- `services/term_subscribers/classroom_carry_over.py` — 補防重複守衛。
- `services/dashboard_query_service.py` — 加學期切換 reminder。
- `api/academic_terms.py` — **刪除整檔**。
- `alembic/versions/acadterm01_*.py` — **新 migration**（資料正規化 + 靜默對齊）。
- `tests/` — 新增 scheduler 測試；刪 smoke 測試；改寫 term_change 整合測試。

**前端（ivy-frontend）修改/刪除：**
- `src/views/SettingsView.vue` — 移除分頁。
- `src/components/settings/SettingsAcademicTermsTab.vue` — **刪除**。
- `src/components/settings/__tests__/SettingsAcademicTermsTab.test.ts` — **刪除**。
- `src/api/academicTerms.ts` — **刪除**。
- `src/api/_generated/schema.d.ts` — codegen regen。

---

# 後端（ivy-backend）

> 建議先確認在後端 feature 分支上：`git -C /Users/yilunwu/Desktop/ivy-backend branch --show-current`。
> 測試指令在 `ivy-backend/` 下跑。

## Task 1: `term_bounds()` 純函式（固定學期起訖日）

**Files:**
- Modify: `utils/academic.py`（在 `_resolve_by_date` 之後新增）
- Test: `tests/test_academic_term_bounds.py`（新檔）

- [ ] **Step 1: 寫失敗測試**

新檔 `tests/test_academic_term_bounds.py`：

```python
"""term_bounds 純函式：(學年, 學期) → 固定起訖日。"""
from datetime import date

import pytest

from utils.academic import term_bounds, _resolve_by_date


def test_first_semester_bounds():
    # 114 學年上學期：2025/8/1 ~ 2026/1/31
    assert term_bounds(114, 1) == (date(2025, 8, 1), date(2026, 1, 31))


def test_second_semester_bounds():
    # 114 學年下學期：2026/2/1 ~ 2026/7/31
    assert term_bounds(114, 2) == (date(2026, 2, 1), date(2026, 7, 31))


def test_invalid_semester_raises():
    with pytest.raises(ValueError):
        term_bounds(114, 3)


@pytest.mark.parametrize(
    "d",
    [
        date(2025, 8, 1),
        date(2025, 12, 31),
        date(2026, 1, 31),
        date(2026, 2, 1),
        date(2026, 7, 31),
    ],
)
def test_round_trip_resolve_matches_bounds(d):
    """_resolve_by_date(d) 算出的學期，其 term_bounds 必含 d。"""
    sy, sem = _resolve_by_date(d)
    start, end = term_bounds(sy, sem)
    assert start <= d <= end
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_academic_term_bounds.py -v`
Expected: FAIL（`ImportError: cannot import name 'term_bounds'`）

- [ ] **Step 3: 實作 `term_bounds`**

在 `utils/academic.py` 的 `_resolve_by_date` 函式之後（約 `:37` 空行處）插入：

```python
def term_bounds(school_year: int, semester: int) -> tuple[date, date]:
    """由民國學年 + 學期回推固定起訖日（學期日期是日期的純函數，無需設定）。

    - 上學期（semester=1）：8/1 ~ 隔年 1/31
    - 下學期（semester=2）：2/1 ~ 同年 7/31
    西元 = 民國學年 + 1911。
    """
    base = school_year + 1911
    if semester == 1:
        return date(base, 8, 1), date(base + 1, 1, 31)
    if semester == 2:
        return date(base + 1, 2, 1), date(base + 1, 7, 31)
    raise ValueError(f"semester must be 1 or 2, got {semester}")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_academic_term_bounds.py -v`
Expected: PASS（5 項）

- [ ] **Step 5: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add utils/academic.py tests/test_academic_term_bounds.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(academic): 新增 term_bounds 純函式回推固定學期起訖日"
```

---

## Task 2: `resolve_current_academic_term()` 改為日期推導為主

**Files:**
- Modify: `utils/academic.py:39-75`
- Test: `tests/test_resolve_current_term_date_driven.py`（新檔）

- [ ] **Step 1: 寫失敗測試**

新檔 `tests/test_resolve_current_term_date_driven.py`：

```python
"""resolve_current_academic_term() 改為日期推導：不再讀 is_current。"""
from datetime import date
from unittest.mock import patch

from utils.academic import resolve_current_academic_term


def test_no_target_date_uses_today_not_db():
    """無 target_date 時走 today_taipei 日期推導，完全不碰 DB。

    若還在查 DB，patch session 工廠會被呼叫；這裡斷言 _resolve_by_date 被用。
    """
    with patch("utils.academic.today_taipei", return_value=date(2026, 3, 15)):
        sy, sem = resolve_current_academic_term()
    assert (sy, sem) == (114, 2)


def test_explicit_target_date_still_pure():
    assert resolve_current_academic_term(target_date=date(2025, 9, 1)) == (114, 1)


def test_does_not_query_db_session(monkeypatch):
    """傳入一個會在被 query 時爆炸的假 session，確認根本沒被 query。"""

    class BoomSession:
        def query(self, *a, **k):
            raise AssertionError("不應再查 DB")

    with patch("utils.academic.today_taipei", return_value=date(2026, 1, 10)):
        # 1 月 → 前一年上學期：2025 → 114, sem 1
        assert resolve_current_academic_term(session=BoomSession()) == (114, 1)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_resolve_current_term_date_driven.py -v`
Expected: FAIL（`test_does_not_query_db_session` 觸發 `AssertionError: 不應再查 DB`，因現碼仍 query `is_current`）

- [ ] **Step 3: 改 `resolve_current_academic_term`**

把 `utils/academic.py` 的 `resolve_current_academic_term`（現 `:39-75`）整個函式替換為：

```python
def resolve_current_academic_term(
    target_date: Optional[date] = None,
    session: Optional[Session] = None,
) -> tuple[int, int]:
    """決定當前學年/學期（民國年）。

    學期是「今天日期」的純函數（上學期 8/1–隔年1/31、下學期 2/1–7/31），
    不再讀 AcademicTerm.is_current；is_current 僅供 turnover 排程器當結轉標記。
    session 參數保留以維持既有呼叫相容，但不再使用。
    """
    return _resolve_by_date(target_date if target_date is not None else today_taipei())
```

> `session` 參數刻意保留（多處 caller 仍傳），函式體不再用它。`get_session` import 若變未使用，移除以免 lint。

- [ ] **Step 4: 跑測試確認通過 + 回歸**

Run: `python -m pytest tests/test_resolve_current_term_date_driven.py tests/test_academic_term_bounds.py -v`
Expected: PASS

Run（回歸：依賴此函式的模組）: `python -m pytest tests/ -k "academic or term or classroom or leaves_quota" -q`
Expected: 既有測試綠（若有測試 stub 了 is_current 行為而失敗，於該測試補 `patch("utils.academic.today_taipei", ...)` 對齊日期；不改業務碼）

- [ ] **Step 5: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add utils/academic.py tests/test_resolve_current_term_date_driven.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "refactor(academic): resolve_current_academic_term 改為日期推導，is_current 降為結轉標記"
```

---

## Task 3: `classroom_carry_over` 補防重複守衛

**Files:**
- Modify: `services/term_subscribers/classroom_carry_over.py:51-66`
- Test: `tests/test_classroom_carry_over_idempotent.py`（新檔）

- [ ] **Step 1: 寫失敗測試**

新檔 `tests/test_classroom_carry_over_idempotent.py`：

```python
"""classroom_carry_over 重跑不 double-create 目標學期班級。"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Classroom, Student
from models.academic_term import AcademicTerm
from services.term_subscribers.classroom_carry_over import handle


def _mk_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_carry_over_twice_no_double_create():
    s = _mk_session()
    old = AcademicTerm(school_year=114, semester=1,
                       start_date=date(2025, 8, 1), end_date=date(2026, 1, 31))
    new = AcademicTerm(school_year=114, semester=2,
                       start_date=date(2026, 2, 1), end_date=date(2026, 7, 31))
    s.add_all([old, new])
    s.flush()
    cls = Classroom(name="星星班", school_year=114, semester=1, capacity=30)
    s.add(cls)
    s.flush()

    handle(old=old, new=new, session=s)
    s.flush()
    first = s.query(Classroom).filter(
        Classroom.school_year == 114, Classroom.semester == 2).count()

    handle(old=old, new=new, session=s)
    s.flush()
    second = s.query(Classroom).filter(
        Classroom.school_year == 114, Classroom.semester == 2).count()

    assert first == 1
    assert second == 1  # 第二次跑：目標學期已有班級 → 跳過
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_classroom_carry_over_idempotent.py -v`
Expected: FAIL（`second == 2`，因無防重複）

- [ ] **Step 3: 加守衛**

用 `python3` 繞 black hook（surgical），在 `services/term_subscribers/classroom_carry_over.py` 的 `_carry_over_same_year` 內、`old_classrooms` 查詢「之前」插入防重複檢查：

```bash
python3 - <<'PY'
p = "/Users/yilunwu/Desktop/ivy-backend/services/term_subscribers/classroom_carry_over.py"
src = open(p, encoding="utf-8").read()
anchor = '    """同學年 1→2：每個 old classroom 生新 row（複製欄位、新 id），\n    再把該 classroom 名下 active student.classroom_id 重新指向新 row。"""\n'
guard = (
    '    # 防重複：目標學期已存在班級 → 視為已 carry-over，跳過（排程器冪等保險）\n'
    '    already = (\n'
    '        session.query(Classroom)\n'
    '        .filter(\n'
    '            Classroom.school_year == new.school_year,\n'
    '            Classroom.semester == new.semester,\n'
    '        )\n'
    '        .first()\n'
    '    )\n'
    '    if already is not None:\n'
    '        logger.info(\n'
    '            "classroom_carry_over: 目標學期 %s-%s 已有班級，跳過 carry-over",\n'
    '            new.school_year,\n'
    '            new.semester,\n'
    '        )\n'
    '        return\n'
)
assert anchor in src, "anchor not found"
src = src.replace(anchor, anchor + guard, 1)
open(p, "w", encoding="utf-8").write(src)
print("done")
PY
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_classroom_carry_over_idempotent.py -v`
Expected: PASS

Run（語法）: `python -m py_compile services/term_subscribers/classroom_carry_over.py`
Expected: 無輸出

- [ ] **Step 5: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add services/term_subscribers/classroom_carry_over.py tests/test_classroom_carry_over_idempotent.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "fix(term): classroom_carry_over 補防重複守衛，目標學期已有班級則跳過"
```

---

## Task 4: 學期自動切換排程器（核心）

**Files:**
- Create: `services/academic_term_turnover_scheduler.py`
- Modify: `config/scheduler.py:79-82`（在 announcement 設定後新增）
- Test: `tests/test_academic_term_turnover_scheduler.py`（新檔）

- [ ] **Step 1: 新增 config 設定**

用 `python3` 在 `config/scheduler.py` 的 announcement 區塊後追加（檔尾 class 內最後一行 `announcement_publish_check_interval` 之後）：

```bash
python3 - <<'PY'
p = "/Users/yilunwu/Desktop/ivy-backend/config/scheduler.py"
src = open(p, encoding="utf-8").read()
anchor = "    announcement_publish_check_interval: int = 60  # 60 秒輪詢，足以讓「8:00 排程」最遲 8:01 推播\n"
add = (
    "\n"
    "    # Academic term turnover（學期自動切換：唯一的學期切換驅動器）\n"
    "    # 預設 True：關閉等於學期永不換（resolve_current 仍走日期推導，但結轉不觸發）。\n"
    "    # 上線誤觸發已由 acadterm01 migration 靜默對齊防護。\n"
    "    academic_term_turnover_enabled: BoolEnv = True\n"
    "    academic_term_turnover_check_interval: int = 3600  # 1 小時輪詢\n"
)
assert anchor in src, "anchor not found"
src = src.replace(anchor, anchor + add, 1)
open(p, "w", encoding="utf-8").write(src)
print("done")
PY
```

- [ ] **Step 2: 寫失敗測試**

新檔 `tests/test_academic_term_turnover_scheduler.py`：

```python
"""學期自動切換排程器核心 reconcile_academic_term 的三分支 + 冪等 + rollback。"""
from datetime import date
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.database import Base, Employee
from models.academic_term import AcademicTerm
from services.academic_term_turnover_scheduler import reconcile_academic_term


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def _current(session):
    return session.query(AcademicTerm).filter(AcademicTerm.is_current.is_(True)).first()


def test_seed_when_absent_no_events(session):
    """全新 DB（無 is_current）→ 靜默建立當前學期 row，不觸發事件。"""
    with patch(
        "services.academic_term_turnover_scheduler.fire_term_changed"
    ) as fired:
        out = reconcile_academic_term(session, today=date(2026, 3, 15))
        session.flush()
    assert out["action"] == "seed"
    fired.assert_not_called()
    cur = _current(session)
    assert (cur.school_year, cur.semester) == (114, 2)
    assert cur.start_date == date(2026, 2, 1)
    assert cur.end_date == date(2026, 7, 31)


def test_noop_when_aligned(session):
    """is_current 已等於日期推導學期 → 不動、不觸發。"""
    t = AcademicTerm(school_year=114, semester=2, is_current=True,
                     start_date=date(2026, 2, 1), end_date=date(2026, 7, 31))
    session.add(t)
    session.flush()
    with patch(
        "services.academic_term_turnover_scheduler.fire_term_changed"
    ) as fired:
        out = reconcile_academic_term(session, today=date(2026, 3, 15))
        session.flush()
    assert out["action"] == "noop"
    fired.assert_not_called()


def test_turnover_fires_events_and_flips(session):
    """is_current=114-1，今天落在 114-2 → 翻牌 + 觸發事件一次 + 寫 audit。"""
    old = AcademicTerm(school_year=114, semester=1, is_current=True,
                       start_date=date(2025, 8, 1), end_date=date(2026, 1, 31))
    session.add(old)
    session.flush()
    with patch(
        "services.academic_term_turnover_scheduler.fire_term_changed"
    ) as fired:
        out = reconcile_academic_term(session, today=date(2026, 3, 1))
        session.flush()
    assert out["action"] == "turnover"
    assert fired.call_count == 1
    _, kwargs = fired.call_args
    assert (kwargs["old"].school_year, kwargs["old"].semester) == (114, 1)
    assert (kwargs["new"].school_year, kwargs["new"].semester) == (114, 2)
    cur = _current(session)
    assert (cur.school_year, cur.semester) == (114, 2)
    # 舊 row is_current 已清
    assert old.is_current is False
    # 寫了一筆 audit
    from models.audit import AuditLog
    logs = session.query(AuditLog).filter(
        AuditLog.entity_type == "academic_term").all()
    assert len(logs) == 1
    assert logs[0].username == "academic_term_turnover"


def test_turnover_idempotent_second_run_noop(session):
    """翻牌後同日再跑 → 已對齊 → noop、不再觸發。"""
    old = AcademicTerm(school_year=114, semester=1, is_current=True,
                       start_date=date(2025, 8, 1), end_date=date(2026, 1, 31))
    session.add(old)
    session.flush()
    reconcile_academic_term(session, today=date(2026, 3, 1))
    session.flush()
    with patch(
        "services.academic_term_turnover_scheduler.fire_term_changed"
    ) as fired:
        out = reconcile_academic_term(session, today=date(2026, 3, 1))
        session.flush()
    assert out["action"] == "noop"
    fired.assert_not_called()


def test_reuses_existing_target_row(session):
    """目標學期 row 已存在（非 current）→ 不重建，翻它的 is_current。"""
    old = AcademicTerm(school_year=114, semester=1, is_current=True,
                       start_date=date(2025, 8, 1), end_date=date(2026, 1, 31))
    existing_new = AcademicTerm(school_year=114, semester=2, is_current=False,
                                start_date=date(2026, 2, 1), end_date=date(2026, 7, 31))
    session.add_all([old, existing_new])
    session.flush()
    with patch("services.academic_term_turnover_scheduler.fire_term_changed"):
        reconcile_academic_term(session, today=date(2026, 3, 1))
        session.flush()
    assert session.query(AcademicTerm).filter(
        AcademicTerm.school_year == 114, AcademicTerm.semester == 2).count() == 1
    assert _current(session).id == existing_new.id
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `python -m pytest tests/test_academic_term_turnover_scheduler.py -v`
Expected: FAIL（`ModuleNotFoundError: services.academic_term_turnover_scheduler`）

- [ ] **Step 4: 實作排程器**

新檔 `services/academic_term_turnover_scheduler.py`：

```python
"""services/academic_term_turnover_scheduler.py — 學期自動切換驅動器。

唯一的學期切換來源（取代手動 set-current）。每個週期 reconcile：
- 用 _resolve_by_date(today) 算出當前學期 T
- 比對 is_current row C：缺→靜默 seed（無事件）；C≠T→翻牌 + fire_term_changed（含事件）；
  C==T→no-op（用 is_current 當標記，天然冪等）
首次部署誤觸發由 acadterm01 migration 靜默對齊防護。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import get_settings
from models.academic_term import AcademicTerm
from models.audit import AuditLog
from utils.academic import _resolve_by_date, term_bounds
from utils.taipei_time import now_taipei_naive
from utils.scheduler_observability import record_rows, scheduler_iteration
from utils.term_events import fire_term_changed

logger = logging.getLogger(__name__)


def _today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.academic_term_turnover_enabled)


def _get_or_create_term(session, school_year: int, semester: int) -> AcademicTerm:
    row = (
        session.query(AcademicTerm)
        .filter(
            AcademicTerm.school_year == school_year,
            AcademicTerm.semester == semester,
        )
        .first()
    )
    if row is not None:
        return row
    start, end = term_bounds(school_year, semester)
    row = AcademicTerm(
        school_year=school_year,
        semester=semester,
        start_date=start,
        end_date=end,
        is_current=False,
    )
    session.add(row)
    session.flush()
    return row


def reconcile_academic_term(session, *, today: date) -> dict:
    """核心 reconcile（同步、在 caller 的 session/transaction 內）。

    回傳 {"action": "seed"|"turnover"|"noop", "term": "<sy>-<sem>"}。
    caller 負責 commit / rollback（handler raise 會 propagate）。
    """
    sy, sem = _resolve_by_date(today)
    current = (
        session.query(AcademicTerm)
        .filter(AcademicTerm.is_current.is_(True))
        .first()
    )

    if current is None:
        # 缺：全新 DB / 從未種子化 → 靜默基準，不觸發事件
        target = _get_or_create_term(session, sy, sem)
        target.is_current = True
        session.flush()
        logger.info("academic_term seed（靜默）：%s-%s", sy, sem)
        return {"action": "seed", "term": f"{sy}-{sem}"}

    if current.school_year == sy and current.semester == sem:
        return {"action": "noop", "term": f"{sy}-{sem}"}

    # 真的跨界 → 翻牌 + 觸發結轉事件
    old = current
    # 先清舊 is_current 並 flush，再設新的；避免兩列同時 true 撞 partial unique singleton index
    old.is_current = False
    session.flush()
    target = _get_or_create_term(session, sy, sem)
    target.is_current = True
    session.flush()
    fire_term_changed(old=old, new=target, session=session)
    session.add(
        AuditLog(
            user_id=None,
            username="academic_term_turnover",
            action="UPDATE",
            entity_type="academic_term",
            entity_id=str(target.id),
            summary=(
                f"學期自動切換：{old.school_year}-{old.semester} → {sy}-{sem}"
            ),
            changes=json.dumps(
                {
                    "from": f"{old.school_year}-{old.semester}",
                    "to": f"{sy}-{sem}",
                },
                ensure_ascii=False,
            ),
            ip_address=None,
            created_at=now_taipei_naive(),
        )
    )
    logger.info(
        "學期自動切換：%s-%s → %s-%s",
        old.school_year,
        old.semester,
        sy,
        sem,
    )
    return {"action": "turnover", "term": f"{sy}-{sem}"}


async def run_academic_term_turnover_scheduler(stop_event: asyncio.Event) -> None:
    """每日輪詢；loop 第一圈即時跑（涵蓋啟動時補抓停機期間跨界）。"""
    from models.base import session_scope

    check_interval = get_settings().scheduler.academic_term_turnover_check_interval
    logger.info(
        "academic term turnover scheduler 啟動 (interval=%ss)", check_interval
    )
    while not stop_event.is_set():
        with scheduler_iteration(
            "academic_term_turnover", expected_interval_seconds=check_interval
        ):
            with session_scope() as session:
                out = reconcile_academic_term(session, today=_today_taipei())
            record_rows(
                "academic_term_turnover",
                1 if out["action"] == "turnover" else 0,
            )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
```

- [ ] **Step 5: 跑測試確認通過**

Run: `python -m pytest tests/test_academic_term_turnover_scheduler.py -v`
Expected: PASS（5 項）

- [ ] **Step 6: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add services/academic_term_turnover_scheduler.py config/scheduler.py tests/test_academic_term_turnover_scheduler.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(term): 新增學期自動切換排程器，跨界自動觸發結轉並寫稽核"
```

---

## Task 5: 在 main.py 註冊排程器

**Files:**
- Modify: `main.py`（lifespan，約 `:357-367` recruitment 區塊後 + `:725-733` shutdown 區塊後）

- [ ] **Step 1: 加啟動註冊（Read 後 Edit）**

先 Read `main.py:355-370`（recruitment_term_advance 啟動區塊），取得實際縮排與文字。然後用 Edit tool，在 recruitment 的 `create_task(...)` 區塊「之後」插入下列內容（縮排對齊 recruitment——同在 `app_lifespan` 的 `try:` 區塊內）：

```python

        academic_term_turnover_task = None
        academic_term_turnover_stop_event: asyncio.Event | None = None
        try:
            from services import academic_term_turnover_scheduler as _at_sched

            if _at_sched.scheduler_enabled():
                academic_term_turnover_stop_event = asyncio.Event()
                academic_term_turnover_task = asyncio.create_task(
                    _at_sched.run_academic_term_turnover_scheduler(
                        academic_term_turnover_stop_event
                    )
                )
                logger.info("academic term turnover scheduler 已啟用")
        except Exception as e:
            logger.warning("學期自動切換排程啟動失敗: %s", e)
```

> Edit 的 `old_string` 用你從 Read 取得的 recruitment `create_task(...)` 結尾那幾行（含正確縮排），`new_string` = 同樣那幾行 + 上面這段。

- [ ] **Step 2: 加關閉清理（Read 後 Edit）**

先 Read `main.py:720-740`（recruitment_term_advance 的 shutdown／cancel 區塊），取得實際文字。然後用 Edit tool，在該 recruitment 清理段「之後」插入鏡像的清理段（縮排與 recruitment 清理段一致）：

```python

        if academic_term_turnover_task is not None:
            if academic_term_turnover_stop_event is not None:
                academic_term_turnover_stop_event.set()
            try:
                await asyncio.wait_for(academic_term_turnover_task, timeout=5)
            except Exception:
                academic_term_turnover_task.cancel()
                try:
                    await academic_term_turnover_task
                except Exception:
                    pass
```

> `old_string` = recruitment 清理段結尾幾行（從 Read 取得），`new_string` = 那幾行 + 上面這段。務必比照 recruitment 既有的 try/except/cancel 結構縮排。

- [ ] **Step 3: 驗證可載入**

Run: `python -c "import main"`（在 `ivy-backend/` 下）
Expected: 無 ImportError / SyntaxError（DB 相關 warning 可忽略）

Run: `python -m py_compile main.py`
Expected: 無輸出

- [ ] **Step 4: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add main.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(term): main.py lifespan 註冊學期自動切換排程器（含啟動即跑與優雅關閉）"
```

---

## Task 6: 移除 `academic_terms` router + 改寫相關測試

**Files:**
- Delete: `api/academic_terms.py`
- Modify: `main.py:71`（import）、`main.py:976`（include_router）
- Delete: `tests/test_academic_terms_api_smoke.py`
- Modify: `tests/test_term_change_integration.py`（改走 `fire_term_changed` 驅動，移除 HTTP/endpoint 依賴）

- [ ] **Step 1: 移除 router 註冊與檔案**

```bash
python3 - <<'PY'
p = "/Users/yilunwu/Desktop/ivy-backend/main.py"
src = open(p, encoding="utf-8").read()
src = src.replace("from api.academic_terms import router as academic_terms_router\n", "", 1)
src = src.replace("app.include_router(academic_terms_router)\n", "", 1)
open(p, "w", encoding="utf-8").write(src)
print("main.py cleaned")
PY
git -C /Users/yilunwu/Desktop/ivy-backend rm api/academic_terms.py tests/test_academic_terms_api_smoke.py
```

- [ ] **Step 2: 改寫 `test_term_change_integration.py`**

把整合測試從「POST set-current 端點」改成「直接呼叫 `fire_term_changed` 驅動 subscriber」。**整檔覆寫**為下列內容（移除 auth/TestClient/academic_terms_router；保留所有 subscriber 行為覆蓋；丟棄 HTTP-only 的 409/404 案例；turnover-trigger 與 rollback 覆蓋已移至 Task 4 scheduler 測試）：

```python
"""term.changed subscriber 整合測試（改由 fire_term_changed 直接驅動）。

set-current 端點已移除，學期切換改由 academic_term_turnover_scheduler 驅動。
本檔聚焦 subscriber（classroom_carry_over / leave_quota_cutover）在一次
term.changed 中的行為；turnover 觸發/rollback 的 reconcile 層覆蓋見
tests/test_academic_term_turnover_scheduler.py。
"""
import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (  # noqa: E402
    Base,
    Classroom,
    Employee,
    LeaveQuota,
    Student,
)
from models.academic_term import AcademicTerm  # noqa: E402
from models.overtime import OvertimeRecord  # noqa: E402,F401
from models.overtime_comp_leave_grant import OvertimeCompLeaveGrant  # noqa: E402,F401
from models.unused_leave_payout_log import UnusedLeavePayoutLog  # noqa: E402,F401
from utils.term_events import (  # noqa: E402
    fire_term_changed,
    register_handler,
    reset_handlers_for_tests,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()

    from services.term_subscribers.classroom_carry_over import handle as cco
    from services.term_subscribers.leave_quota_cutover import handle as lqc
    from services.term_subscribers.activity_semester_tag import handle as ast

    reset_handlers_for_tests()
    register_handler("classroom_carry_over", cco)
    register_handler("leave_quota_cutover", lqc)
    register_handler("activity_semester_tag_reset", ast)

    yield s

    reset_handlers_for_tests()
    s.close()


def _seed_term(session, *, school_year, semester, is_current=False):
    from utils.academic import term_bounds

    start, end = term_bounds(school_year, semester)
    t = AcademicTerm(
        school_year=school_year,
        semester=semester,
        start_date=start,
        end_date=end,
        is_current=is_current,
    )
    session.add(t)
    session.flush()
    return t


def _seed_classroom(session, sy, sem, name="ABC"):
    cls = Classroom(name=name, school_year=sy, semester=sem, capacity=30)
    session.add(cls)
    session.flush()
    return cls


def _seed_student(session, classroom_id, student_id):
    s = Student(
        student_id=student_id,
        name=f"S{student_id}",
        gender="M",
        birthday=date(2020, 1, 1),
        classroom_id=classroom_id,
        is_active=True,
    )
    session.add(s)
    session.flush()
    return s


_emp_counter = 0


def _seed_emp(session, hire_date=date(2020, 9, 1)):
    global _emp_counter
    _emp_counter += 1
    e = Employee(
        employee_id=f"E{_emp_counter:03d}",
        name="員工",
        hire_date=hire_date,
        is_active=True,
    )
    session.add(e)
    session.flush()
    return e


def _fire(session, old, new):
    """模擬 reconcile 翻牌後呼叫 subscriber：先 toggle is_current 再 fire。"""
    if old is not None:
        old.is_current = False
    new.is_current = True
    session.flush()
    fire_term_changed(old=old, new=new, session=session)
    session.flush()


class TestTermChangeSubscribers:
    def test_same_year_1_to_2_classroom_carry_over(self, db_session):
        """114-1 → 114-2：classroom 複製、學生遷移、quota 不動。"""
        old_t = _seed_term(db_session, school_year=114, semester=1, is_current=True)
        new_t = _seed_term(db_session, school_year=114, semester=2)
        old_cls = _seed_classroom(db_session, 114, 1, name="星星班")
        s = _seed_student(db_session, old_cls.id, "114-A-01")

        _fire(db_session, old_t, new_t)

        new_cls = (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 114, Classroom.semester == 2)
            .first()
        )
        assert new_cls is not None
        assert new_cls.name == "星星班"
        db_session.refresh(s)
        assert s.classroom_id == new_cls.id
        assert db_session.query(LeaveQuota).count() == 0

    def test_cross_year_2_to_1_leave_quota_cutover(self, db_session):
        """114-2 → 115-1：classroom 不動、每員工生 5 筆 quota。"""
        old_t = _seed_term(db_session, school_year=114, semester=2, is_current=True)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        emp = _seed_emp(db_session)

        _fire(db_session, old_t, new_t)

        rows = (
            db_session.query(LeaveQuota)
            .filter(LeaveQuota.employee_id == emp.id, LeaveQuota.school_year == 115)
            .all()
        )
        assert len(rows) == 5  # 4 QUOTA_LEAVE_TYPES + compensatory

    def test_cross_year_quota_compensatory_balance_carry_over(self, db_session):
        """補休結餘 carry-over：granted 8h、consumed 2h → 新 row 6h。"""
        old_t = _seed_term(db_session, school_year=114, semester=2, is_current=True)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        emp = _seed_emp(db_session)
        ot = OvertimeRecord(
            employee_id=emp.id,
            overtime_date=date(2026, 3, 1),
            overtime_type="weekday",
            hours=8.0,
            use_comp_leave=True,
            comp_leave_granted=True,
            status="approved",
        )
        db_session.add(ot)
        db_session.flush()
        db_session.add(
            OvertimeCompLeaveGrant(
                overtime_record_id=ot.id,
                employee_id=emp.id,
                granted_hours=8.0,
                granted_at=date(2026, 3, 1),
                expires_at=date(2027, 3, 1),
                consumed_hours=2.0,
                status="active",
            )
        )
        db_session.flush()

        _fire(db_session, old_t, new_t)

        new_comp = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
        assert new_comp.total_hours == pytest.approx(6.0)

    def test_cross_year_does_not_create_annual_quota(self, db_session):
        """特休週年制後 cutover 不建 annual row。"""
        old_t = _seed_term(db_session, school_year=114, semester=2, is_current=True)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        emp = _seed_emp(db_session, hire_date=date(2020, 9, 1))

        _fire(db_session, old_t, new_t)

        annual = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "annual",
            )
            .first()
        )
        assert annual is None

    def test_idempotent_handler_does_not_double_insert_quotas(self, db_session):
        """raw 連呼叫兩次 leave_quota_cutover → quota 只一份。"""
        from services.term_subscribers.leave_quota_cutover import handle as lqc_handle

        old_t = _seed_term(db_session, school_year=114, semester=2)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        _seed_emp(db_session)

        lqc_handle(old=old_t, new=new_t, session=db_session)
        db_session.flush()
        first = db_session.query(LeaveQuota).filter(
            LeaveQuota.school_year == 115).count()
        lqc_handle(old=old_t, new=new_t, session=db_session)
        db_session.flush()
        second = db_session.query(LeaveQuota).filter(
            LeaveQuota.school_year == 115).count()
        assert first == second == 5

    def test_atypical_jump_113_2_to_115_1_no_op(self, db_session, caplog):
        """跳級 113-2 → 115-1：classroom no-op + warning（直接呼叫驗 handler 分支）。"""
        import logging

        old_t = _seed_term(db_session, school_year=113, semester=2, is_current=True)
        new_t = _seed_term(db_session, school_year=115, semester=1)
        _seed_classroom(db_session, 113, 2)

        with caplog.at_level(logging.WARNING):
            _fire(db_session, old_t, new_t)

        assert (
            db_session.query(Classroom).filter(Classroom.school_year == 115).count()
            == 0
        )
        assert db_session.query(LeaveQuota).count() == 0
        assert any("非典型切換" in r.message for r in caplog.records)

    def test_read_path_prefers_school_year_falls_back_to_year(self, db_session):
        """_resolve_quota_row：school_year row 優先、缺則 fallback 西元年。"""
        from api.leaves_quota import _resolve_quota_row

        _seed_term(db_session, school_year=115, semester=1, is_current=True)
        emp = _seed_emp(db_session)
        new_row = LeaveQuota(
            employee_id=emp.id, year=2026, school_year=115,
            leave_type="annual", total_hours=120.0,
        )
        legacy_row = LeaveQuota(
            employee_id=emp.id, year=2026, school_year=None,
            leave_type="annual", total_hours=100.0,
        )
        db_session.add_all([new_row, legacy_row])
        db_session.flush()

        found = _resolve_quota_row(db_session, emp.id, "annual")
        assert found.id == new_row.id

        db_session.delete(new_row)
        db_session.flush()
        fallback = _resolve_quota_row(db_session, emp.id, "annual")
        assert fallback.id == legacy_row.id
```

- [ ] **Step 3: 跑測試確認通過**

Run: `python -m pytest tests/test_term_change_integration.py -v`
Expected: PASS（7 項）

Run: `python -c "import main"`
Expected: 無 ImportError（router 已移除）

- [ ] **Step 4: 確認無殘留引用**

Run: `grep -rn "academic_terms_router\|from api.academic_terms\|api\.academic_terms" /Users/yilunwu/Desktop/ivy-backend --include="*.py"`
Expected: 無輸出（全清）

- [ ] **Step 5: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add -A
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(term)!: 移除 academic_terms router（含 set-current），學期切換改全自動排程驅動"
```

---

## Task 7: 後台首頁學期切換提醒（純日期、無狀態）

**Files:**
- Modify: `services/dashboard_query_service.py:387-388`（`reminders` 區塊內新增）
- Test: `tests/test_dashboard_term_turnover_reminder.py`（新檔）

- [ ] **Step 1: 寫失敗測試**

新檔 `tests/test_dashboard_term_turnover_reminder.py`：

```python
"""dashboard 學期切換 reminder：term start_date 起 7 天內顯示。"""
from datetime import date
from unittest.mock import patch

from services.dashboard_query_service import build_term_turnover_reminder


def test_reminder_present_on_start_day():
    with patch(
        "services.dashboard_query_service._today_taipei", return_value=date(2026, 2, 1)
    ):
        r = build_term_turnover_reminder()
    assert r is not None
    assert r["type"] == "academic_term_turnover"
    assert "下學期" in r["title"]


def test_reminder_present_within_7_days():
    with patch(
        "services.dashboard_query_service._today_taipei", return_value=date(2026, 2, 7)
    ):
        assert build_term_turnover_reminder() is not None


def test_reminder_absent_after_7_days():
    with patch(
        "services.dashboard_query_service._today_taipei", return_value=date(2026, 2, 9)
    ):
        assert build_term_turnover_reminder() is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_dashboard_term_turnover_reminder.py -v`
Expected: FAIL（`ImportError: build_term_turnover_reminder`）

- [ ] **Step 3: 實作**

先確認 `services/dashboard_query_service.py` 頂部 import 區是否已有 `date` / `ZoneInfo`；缺則補。用 Read 檢視檔頭後，新增 module-level helper（放在 `DashboardQueryService` class 定義「之前」的 module scope，或作為 module function；測試以 module function 匯入）：

在 `services/dashboard_query_service.py` 適當的 import 後新增：

```python
from datetime import date, datetime
from zoneinfo import ZoneInfo


def _today_taipei() -> date:
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def build_term_turnover_reminder() -> dict | None:
    """學期 start_date 起 7 天內顯示「已自動切換」提醒；純日期、無狀態、自動消失。"""
    from utils.academic import _resolve_by_date, term_bounds

    today = _today_taipei()
    sy, sem = _resolve_by_date(today)
    start, _ = term_bounds(sy, sem)
    delta = (today - start).days
    if 0 <= delta <= 7:
        label = "上學期" if sem == 1 else "下學期"
        return {
            "type": "academic_term_turnover",
            "title": f"本學期已自動切換為 {sy} 學年{label}",
            "route": "/",
            "priority": "low",
            "items": [
                {
                    "id": f"term-{sy}-{sem}",
                    "label": "已完成班級延續與假別額度結轉",
                    "date": start.isoformat(),
                    "meta": f"{sy} 學年{label}",
                }
            ],
        }
    return None
```

> 若檔頭已 import `date`/`datetime`/`ZoneInfo`，勿重複 import（會 lint 失敗）；只加 `_today_taipei` 與 `build_term_turnover_reminder`。

接著在 `build_notification_summary`（`:387-388` 初始化 `action_items=[]`、`reminders=[]` 之後）把 reminder 併入。用 Edit 在 `reminders = []` 之後插入：

```python
        term_reminder = build_term_turnover_reminder()
        if term_reminder is not None:
            reminders.append(term_reminder)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_dashboard_term_turnover_reminder.py -v`
Expected: PASS（3 項）

Run（語法 + import）: `python -c "import services.dashboard_query_service"`
Expected: 無錯

- [ ] **Step 5: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add services/dashboard_query_service.py tests/test_dashboard_term_turnover_reminder.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(dashboard): 學期自動切換後 7 天內顯示後台提醒（純日期無狀態）"
```

---

## Task 8: Migration — 資料正規化 + 靜默對齊

**Files:**
- Create: `alembic/versions/acadterm01_normalize_academic_terms.py`

- [ ] **Step 1: 確認當前 head**

Run: `python -m alembic heads`
記下你這支分支的 head revision（本 workspace 有大量未合併分支；務必用你分支實際的 head，不要硬抄）。若出現多 head，本 migration 的 `down_revision` 設為你分支的單一 head；如整體多 head 需另開 merge migration（見 §部署）。

- [ ] **Step 2: 撰寫 migration**

新檔 `alembic/versions/acadterm01_normalize_academic_terms.py`（把 `down_revision` 換成 Step 1 取得的 head）：

```python
"""normalize academic_terms dates + silent is_current reconcile

學期改為日期自動推導後：把所有 academic_terms 的 start_date/end_date 正規化成固定值
（上學期 8/1–隔年1/31、下學期 2/1–7/31），並把 is_current 靜默對齊到「今天日期推導」
的學期（缺則建立）。**不觸發任何 term.changed 事件**——純資料遷移，避免上線誤觸發批次結轉。

Revision ID: acadterm01
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa

revision = "acadterm01"
down_revision = "REPLACE_WITH_BRANCH_HEAD"  # ← Step 1 取得
branch_labels = None
depends_on = None


def _bounds(school_year: int, semester: int):
    base = school_year + 1911
    if semester == 1:
        return date(base, 8, 1), date(base + 1, 1, 31)
    return date(base + 1, 2, 1), date(base + 1, 7, 31)


def _resolve_by_date(d: date):
    if d.month >= 8:
        return d.year - 1911, 1
    if d.month >= 2:
        return d.year - 1 - 1911, 2
    return d.year - 1 - 1911, 1


def upgrade():
    bind = op.get_bind()
    terms = sa.table(
        "academic_terms",
        sa.column("id", sa.Integer),
        sa.column("school_year", sa.Integer),
        sa.column("semester", sa.Integer),
        sa.column("start_date", sa.Date),
        sa.column("end_date", sa.Date),
        sa.column("is_current", sa.Boolean),
    )

    # 1) 正規化所有 row 的起訖日
    rows = bind.execute(
        sa.select(terms.c.id, terms.c.school_year, terms.c.semester)
    ).fetchall()
    for rid, sy, sem in rows:
        start, end = _bounds(sy, sem)
        bind.execute(
            sa.update(terms)
            .where(terms.c.id == rid)
            .values(start_date=start, end_date=end)
        )

    # 2) 靜默對齊 is_current 到今天日期推導的學期（缺則建立）；【不觸發事件】
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    tsy, tsem = _resolve_by_date(today)

    # 先全部清 is_current（避免 partial unique 衝突）
    bind.execute(sa.update(terms).values(is_current=False))

    target = bind.execute(
        sa.select(terms.c.id).where(
            terms.c.school_year == tsy, terms.c.semester == tsem
        )
    ).first()
    if target is None:
        start, end = _bounds(tsy, tsem)
        bind.execute(
            sa.insert(terms).values(
                school_year=tsy,
                semester=tsem,
                start_date=start,
                end_date=end,
                is_current=True,
            )
        )
    else:
        bind.execute(
            sa.update(terms).where(terms.c.id == target[0]).values(is_current=True)
        )


def downgrade():
    # 純資料正規化，無法精確還原使用者原本自訂的任意起訖日；no-op。
    pass
```

- [ ] **Step 3: 在 dev DB 實跑驗證**

Run: `python -m alembic upgrade heads`
Expected: 成功；無「Can't locate revision」。若報多 head，依 §部署 先 merge。

驗證資料（postgres MCP 或 psql）：`SELECT school_year, semester, start_date, end_date, is_current FROM academic_terms ORDER BY school_year, semester;`
Expected: 每筆 start/end 為固定值；恰一筆 is_current=true，且對應今天日期推導的學期。

- [ ] **Step 4: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-backend add alembic/versions/acadterm01_normalize_academic_terms.py
git -C /Users/yilunwu/Desktop/ivy-backend commit -m "feat(term): migration 正規化學期起訖日並靜默對齊 is_current（不觸發事件）"
```

---

## Task 9: 後端整體回歸

- [ ] **Step 1: 跑相關測試群**

Run: `python -m pytest tests/ -k "academic or term or classroom or quota or dashboard" -q`
Expected: 全綠

- [ ] **Step 2: 跑全套件（時間長，CPU 飢餓非真 hang）**

Run: `python -m pytest tests/ -q`
Expected: 相對 main 無「新增」失敗（既有 tz/flaky baseline 失敗不算回歸；以 main 基線比對）

- [ ] **Step 3: 若有測試因 resolve_current 改動而失敗**

逐一檢視：多半是測試先前假設「is_current row 決定當前學期」。修法是在該測試 `patch("utils.academic.today_taipei", return_value=<對齊日期>)` 或改斷言；**不改業務碼**。修完重跑該檔。

---

# 前端（ivy-frontend）

> 在前端 feature 分支上做：`git -C /Users/yilunwu/Desktop/ivy-frontend branch --show-current`。
> ⚠ 前端 worktree 的 node_modules 是 tracked symlink，勿 `git add -A` 誤納；只 add 具名檔。

## Task 10: 移除設定分頁與元件

**Files:**
- Modify: `src/views/SettingsView.vue:8,43-45`
- Delete: `src/components/settings/SettingsAcademicTermsTab.vue`
- Delete: `src/components/settings/__tests__/SettingsAcademicTermsTab.test.ts`

- [ ] **Step 1: 移除 import（SettingsView.vue:8）**

Edit `src/views/SettingsView.vue`，刪除這行：

```
import SettingsAcademicTermsTab from '@/components/settings/SettingsAcademicTermsTab.vue'
```

- [ ] **Step 2: 移除 tab-pane（SettingsView.vue:43-45）**

Edit `src/views/SettingsView.vue`，刪除這段：

```
      <el-tab-pane label="學年/學期" name="academic-terms" lazy>
        <SettingsAcademicTermsTab />
      </el-tab-pane>
```

- [ ] **Step 3: 刪除元件與測試**

```bash
git -C /Users/yilunwu/Desktop/ivy-frontend rm \
  src/components/settings/SettingsAcademicTermsTab.vue \
  src/components/settings/__tests__/SettingsAcademicTermsTab.test.ts
```

- [ ] **Step 4: 確認 SettingsView 無殘留引用 + typecheck**

Run: `grep -rn "AcademicTermsTab" /Users/yilunwu/Desktop/ivy-frontend/src`
Expected: 無輸出

Run（在 ivy-frontend/）: `npm run typecheck`
Expected: 通過（SettingsView 不再引用已刪元件）

- [ ] **Step 5: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-frontend add src/views/SettingsView.vue
git -C /Users/yilunwu/Desktop/ivy-frontend commit -m "feat(settings): 移除「學年/學期」設定分頁（學期改系統自動判斷）"
```

---

## Task 11: 刪除 `academicTerms.ts` API

**Files:**
- Delete: `src/api/academicTerms.ts`

- [ ] **Step 1: 再次確認無消費端**

Run: `grep -rn "academicTerms" /Users/yilunwu/Desktop/ivy-frontend/src`
Expected: 無輸出（Task 10 已刪 tab 與測試）

- [ ] **Step 2: 刪除檔案**

```bash
git -C /Users/yilunwu/Desktop/ivy-frontend rm src/api/academicTerms.ts
```

- [ ] **Step 3: typecheck**

Run: `npm run typecheck`
Expected: 通過

- [ ] **Step 4: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-frontend commit -m "chore(api): 刪除 academicTerms.ts（端點已移除、無消費端）"
```

---

## Task 12: OpenAPI codegen 重生 schema.d.ts

> 前置：後端 Task 6（移除 router）已合併且本機後端碼為最新。

**Files:**
- Modify: `src/api/_generated/schema.d.ts`

- [ ] **Step 1: 重生 OpenAPI 與型別**

```bash
cd /Users/yilunwu/Desktop/ivy-backend && python scripts/dump_openapi.py
cd /Users/yilunwu/Desktop/ivy-frontend && npm run gen:api
```

- [ ] **Step 2: 確認 schema.d.ts 僅移除 academic-terms 路徑**

Run: `git -C /Users/yilunwu/Desktop/ivy-frontend diff src/api/_generated/schema.d.ts | grep -i "academic"`
Expected: 只有 `-`（移除）行，涉及 `/academic-terms`；無其他端點被動到。

Run: `npm run typecheck`
Expected: 通過

- [ ] **Step 3: Commit**

```bash
git -C /Users/yilunwu/Desktop/ivy-frontend add src/api/_generated/schema.d.ts
git -C /Users/yilunwu/Desktop/ivy-frontend commit -m "chore(api): regen schema.d.ts 移除 academic-terms 端點型別"
```

---

## 部署與收尾（USER 手動 gate）

1. **多 head 處理**：本 workspace migration tree 有大量未合併分支。合併後若 `python -m alembic heads` 出現多 head，補一支 merge migration 再 `alembic upgrade heads`（見 feedback：含 backfill 的 migration 合併前必手動 upgrade heads 驗證）。
2. **上線順序**：後端先（migration + 端點移除 + 排程器）→ 前端後（移除 tab + codegen）。migration 在 App 啟動前跑完，保證 is_current 已對齊、排程器首圈不誤觸發。
3. **env**：`ACADEMIC_TERM_TURNOVER_ENABLED` 預設 True，正常不需設；如需暫停自動切換可設 False。
4. **整合驗證**：`start.sh` 起兩端，登入後台確認「系統設定」無「學年/學期」分頁、首頁正常、報表/班級學期下拉正常（走純函式）。
5. **更新 workspace CLAUDE.md**：學期改日期自動推導、設定頁移除、`academic_terms` 由排程器維護、新增排程器 flag。
6. **push**：兩 repo 各自 push（後端先），不擅自進行。

---

## Self-Review（plan 對 spec 覆蓋檢查）

- spec §3 決策 A（保留表）→ 全程不拆表 ✓；決策 B（日期為真相）→ Task 2 ✓；決策 C（排程器）→ Task 4+5 ✓；決策 D（首次部署防護）→ Task 8 migration 靜默對齊 + Task 4 seed 分支 ✓；決策 E（防重複）→ Task 3 ✓。
- spec §4 行為變更（8/1 轉在學）→ 不需改碼（recruitment scheduler 既有行為），已於 spec 記錄 ✓。
- spec §5.1 B1–B8 → Task 1,2,4,5,6,3,7,8 ✓；§5.2 F1–F5 → Task 10,10,10,11,12 ✓。
- spec §6 測試 → 各 Task TDD + Task 9 回歸 ✓。
- spec「稽核 is_current 消費端」→ 已驗證唯一處在 utils/academic.py（Task 2）✓，無額外任務。
- 通知（稽核 + 後台提醒）→ Task 4 audit + Task 7 dashboard reminder ✓。

# 招生入學「分學期」實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓招生入學以「入學學期」(target_school_year/target_semester) 為一級維度貫穿登記、篩選、漏斗看板、統計、轉成學生與學生詳情。

**Architecture:** 重用 `RecruitmentVisit` 既有的 `target_school_year`/`target_semester`（原僅保留座位流程使用）作為「入學學期」單一事實來源；漏斗看板與統計改以此欄位 scope（取代「訪視月份推算」）；所有寫入路徑（API create 預設當前學期、Excel import 由月份推導、Alembic 一次性 backfill）保證此欄位有值，故看板純依 target 過濾、不需 fallback。學生本人新增對稱欄位 `Student.enrollment_semester`。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + PostgreSQL（測試 SQLite）；Vue 3 `<script setup lang="ts">` + Element Plus + Pinia + Vitest。

## Global Constraints

- 語言一律繁體中文（對話、commit、docstring、UI 文案）。
- 學期格式：`{ school_year: 民國年(int), semester: 1=上 | 2=下 }`。學年邊界：上學期 8/1~隔年1/31、下學期 2/1~同年7/31（`utils/academic.term_bounds` / `_resolve_by_date`）。
- 前端 TS-only：禁 `: any`/`as any`，用 `: unknown` + narrow；新 SFC 一律 `<script setup lang="ts">`。
- BE 針對性 pytest 一律加 `-o addopts=""` 關 coverage（否則 120s timeout 假卡）；單元測試必掛 `test_db_session` fixture 避免打到 dev PG。
- BE `.py` 經 Edit 後 PostToolUse black hook 會自動格式化（屬正常）。
- 前後端**分開 commit**（不同 repo）；建議各開 worktree（FE worktree 需 `ln -s ../../ivy-frontend/node_modules node_modules`）。共用 main 上有平行 session WIP，commit 用精確 pathspec、勿 `git add -A`。
- Alembic 目前 head = `mscrcptp01`；新 migration `down_revision = "mscrcptp01"`。
- 既有 `RecruitmentRecordOut`（`schemas/recruitment_records.py:30-60`）**已含** `target_school_year`/`target_semester`，明細回傳不需改此 schema。
- BE schema 異動後必跑 codegen（Task 10）保持前端 `schema.d.ts` 不漂移，否則兩 repo CI `openapi-drift` job 會紅。

---

## Phase A — 後端（ivy-backend）

### Task 1: `roc_month_to_school_term` 純函式（月份標籤 → 入學學期）

**Files:**
- Modify: `services/recruitment_funnel.py`（在 `school_term_to_roc_months` 之後新增反函式）
- Test: `tests/test_recruitment_funnel_term.py`（新檔）

**Interfaces:**
- Produces: `roc_month_to_school_term(month: str) -> tuple[int, int]` — "115.03" → (114, 2)。Task 2 migration 與 Task 6 import 會比照此邏輯。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_recruitment_funnel_term.py`：

```python
from services.recruitment_funnel import roc_month_to_school_term


def test_roc_month_to_school_term_basic():
    assert roc_month_to_school_term("114.09") == (114, 1)  # 9月 → 上學期
    assert roc_month_to_school_term("115.01") == (114, 1)  # 1月 → 前一年上學期
    assert roc_month_to_school_term("115.02") == (114, 2)  # 2月 → 下學期
    assert roc_month_to_school_term("115.03") == (114, 2)  # 3月 → 下學期
    assert roc_month_to_school_term("115.08") == (115, 1)  # 8月 → 下個學年上學期


def test_roc_month_to_school_term_invalid():
    import pytest

    for bad in ("", "115", "115.13", "abc.0a"):
        with pytest.raises(ValueError):
            roc_month_to_school_term(bad)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_funnel_term.py -o addopts="" -q`
Expected: FAIL（`ImportError: cannot import name 'roc_month_to_school_term'`）

- [ ] **Step 3: 實作**

在 `services/recruitment_funnel.py` 檔頂確認有 `from datetime import date`（無則加），並 import `resolve_current_academic_term`（與 `term_bounds` 同來源 `utils.academic`）。在 `school_term_to_roc_months` 函式之後新增：

```python
def roc_month_to_school_term(month: str) -> tuple[int, int]:
    """民國月份標籤（"115.03"）→ 所屬學年/學期（民國）。

    school_term_to_roc_months 的反函式：依 utils.academic 學年邊界
    （上學期 8/1~隔年1/31、下學期 2/1~同年7/31）判定 month 落在哪個學年/學期。
    用於招生「入學學期」的 backfill（Alembic）與 Excel 匯入時由訪視月份推導預設值。
    """
    parts = (month or "").strip().split(".")
    if len(parts) < 2:
        raise ValueError(f"invalid roc month label: {month!r}")
    try:
        roc_year = int(parts[0])
        mm = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"invalid roc month label: {month!r}") from exc
    if not 1 <= mm <= 12:
        raise ValueError(f"invalid month in label: {month!r}")
    return resolve_current_academic_term(target_date=date(roc_year + 1911, mm, 1))
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_funnel_term.py -o addopts="" -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add services/recruitment_funnel.py tests/test_recruitment_funnel_term.py
git commit -m "feat(recruitment): 新增 roc_month_to_school_term 反函式（月份標籤→入學學期）"
```

---

### Task 2: `Student.enrollment_semester` 欄位 + Alembic migration（建欄 + 雙向 backfill）

**Files:**
- Modify: `models/classroom.py:166-168`（`enrollment_school_year` 之後插入 `enrollment_semester`）
- Create: `alembic/versions/enrterm01_add_enrollment_semester_and_backfill.py`
- Test: `tests/test_migration_enrollment_semester.py`（新檔，測 migration 內的月份推導 helper）

**Interfaces:**
- Produces: `Student.enrollment_semester`（nullable Integer，1/2，永久不變）；DB 既有資料經 backfill 補值。

- [ ] **Step 1: 加 model 欄位**

`models/classroom.py`，在第 166-168 行 `enrollment_school_year = Column(...)` 之後、`enrollment_seq = Column(...)` 之前插入：

```python
    enrollment_semester = Column(
        Integer, nullable=True, comment="入學學期 1=上/2=下；入學配發一次、終身不變"
    )
```

- [ ] **Step 2: 寫 migration（含 backfill）**

建立 `alembic/versions/enrterm01_add_enrollment_semester_and_backfill.py`：

```python
"""add students.enrollment_semester + backfill visit target term & student enroll semester

Revision ID: enrterm01
Revises: mscrcptp01
Create Date: 2026-06-30
"""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from alembic import op

revision = "enrterm01"
down_revision = "mscrcptp01"
branch_labels = None
depends_on = None


def _date_to_term(d: date) -> tuple[int, int]:
    """純日期 → (民國學年, 學期)。mirror of utils.academic._resolve_by_date。"""
    if d.month >= 8:
        return d.year - 1911, 1
    if d.month >= 2:
        return d.year - 1 - 1911, 2
    return d.year - 1 - 1911, 1


def _roc_month_to_term(month: str | None) -> tuple[int, int] | tuple[None, None]:
    """民國月份標籤（"115.03"）→ (學年, 學期)。mirror of roc_month_to_school_term。"""
    if not month:
        return None, None
    parts = str(month).strip().split(".")
    if len(parts) < 2:
        return None, None
    try:
        roc_year = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return None, None
    if not 1 <= mm <= 12:
        return None, None
    return _date_to_term(date(roc_year + 1911, mm, 1))


def _coerce_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    s = str(value)[:10]
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. 新增欄位
    with op.batch_alter_table("students") as batch_op:
        batch_op.add_column(
            sa.Column("enrollment_semester", sa.Integer(), nullable=True)
        )

    # 2. backfill 招生訪視 target_school_year/target_semester（NULL 者用訪視月份推導）
    visit_rows = bind.execute(
        sa.text(
            "SELECT id, month FROM recruitment_visits "
            "WHERE target_school_year IS NULL OR target_semester IS NULL"
        )
    ).fetchall()
    for vid, month in visit_rows:
        sy, sem = _roc_month_to_term(month)
        if sy is None:
            continue
        bind.execute(
            sa.text(
                "UPDATE recruitment_visits "
                "SET target_school_year = COALESCE(target_school_year, :sy), "
                "    target_semester = COALESCE(target_semester, :sem) "
                "WHERE id = :id"
            ),
            {"sy": sy, "sem": sem, "id": vid},
        )

    # 3a. backfill 學生入學學期：優先取「入學」異動紀錄最早一筆的 semester
    log_rows = bind.execute(
        sa.text(
            "SELECT student_id, semester, event_date "
            "FROM student_change_logs WHERE event_type = '入學'"
        )
    ).fetchall()
    earliest: dict[int, tuple[int, date | None]] = {}
    for sid, sem, event_date in log_rows:
        d = _coerce_date(event_date)
        cur = earliest.get(sid)
        if cur is None or (d is not None and (cur[1] is None or d < cur[1])):
            earliest[sid] = (sem, d)
    for sid, (sem, _d) in earliest.items():
        if sem is None:
            continue
        bind.execute(
            sa.text(
                "UPDATE students SET enrollment_semester = :sem "
                "WHERE id = :id AND enrollment_semester IS NULL"
            ),
            {"sem": sem, "id": sid},
        )

    # 3b. 仍為 NULL 者，由 enrollment_date 推導
    rest = bind.execute(
        sa.text(
            "SELECT id, enrollment_date FROM students "
            "WHERE enrollment_semester IS NULL AND enrollment_date IS NOT NULL"
        )
    ).fetchall()
    for sid, enroll_date in rest:
        d = _coerce_date(enroll_date)
        if d is None:
            continue
        _sy, sem = _date_to_term(d)
        bind.execute(
            sa.text(
                "UPDATE students SET enrollment_semester = :sem WHERE id = :id"
            ),
            {"sem": sem, "id": sid},
        )


def downgrade() -> None:
    with op.batch_alter_table("students") as batch_op:
        batch_op.drop_column("enrollment_semester")
    # 註：recruitment_visits target_* 的 backfill 為資料補值，不可逆（downgrade 不還原）。
```

- [ ] **Step 3: 寫 migration helper 測試**

建立 `tests/test_migration_enrollment_semester.py`：

```python
import importlib.util
from datetime import date
from pathlib import Path

_MIG = (
    Path(__file__).resolve().parent.parent
    / "alembic/versions/enrterm01_add_enrollment_semester_and_backfill.py"
)
_spec = importlib.util.spec_from_file_location("enrterm01_mig", _MIG)
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def test_roc_month_to_term_matches_service():
    from services.recruitment_funnel import roc_month_to_school_term

    for label in ("114.09", "115.01", "115.02", "115.03", "115.08"):
        assert mig._roc_month_to_term(label) == roc_month_to_school_term(label)


def test_roc_month_to_term_invalid_returns_none():
    assert mig._roc_month_to_term("") == (None, None)
    assert mig._roc_month_to_term("115") == (None, None)
    assert mig._roc_month_to_term("115.13") == (None, None)


def test_date_to_term_boundaries():
    assert mig._date_to_term(date(2025, 8, 1)) == (114, 1)
    assert mig._date_to_term(date(2026, 1, 31)) == (114, 1)
    assert mig._date_to_term(date(2026, 2, 1)) == (114, 2)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_migration_enrollment_semester.py -o addopts="" -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 套用 migration（dev DB）並驗證**

Run:
```bash
cd ~/Desktop/ivy-backend && alembic upgrade head
psql "postgresql://yilunwu@localhost:5432/ivymanagement" -c "\d students" | grep enrollment_semester
psql "postgresql://yilunwu@localhost:5432/ivymanagement" -c "SELECT count(*) FILTER (WHERE enrollment_semester IS NULL) AS null_sem, count(*) AS total FROM students;"
psql "postgresql://yilunwu@localhost:5432/ivymanagement" -c "SELECT count(*) FILTER (WHERE target_school_year IS NULL) AS null_tsy FROM recruitment_visits;"
```
Expected: `enrollment_semester` 欄位存在；學生 null_sem 遠小於 total（多數已 backfill）；訪視 null_tsy = 0 或僅剩月份格式異常者。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/classroom.py alembic/versions/enrterm01_add_enrollment_semester_and_backfill.py tests/test_migration_enrollment_semester.py
git commit -m "feat(students): 新增 enrollment_semester 欄位 + 招生訪視入學學期/學生入學學期 backfill migration"
```

---

### Task 3: `RecruitmentVisitCreate/Update` 加入學學期欄位 + create 預設當前學期

**Files:**
- Modify: `api/recruitment/shared.py:622-651`（Create）、`654-685`（Update）
- Modify: `api/recruitment/records.py:178-194`（POST handler 補預設）
- Test: `tests/test_recruitment_records_term.py`（新檔）

**Interfaces:**
- Consumes: `resolve_current_academic_term`（`utils.academic`）。
- Produces: `RecruitmentVisitCreate.target_school_year/target_semester`（Optional；POST 缺值時填當前學期）；`RecruitmentVisitUpdate` 同欄位 Optional。

> 設計取捨：schema 層設為 Optional（不丟 422），POST handler 缺值時填「當前學期」，前端表單以「必填、預設當前學期」強制 UX。這比 schema 強制必填更穩（不破壞既有以 API 建立訪視的測試），同時滿足業主「預設當前學期」決策。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_recruitment_records_term.py`：

```python
def test_create_record_persists_target_term(client, test_db_session, admin_headers):
    resp = client.post(
        "/api/recruitment/records",
        json={
            "month": "114.09",
            "child_name": "測試童甲",
            "target_school_year": 115,
            "target_semester": 1,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target_school_year"] == 115
    assert body["target_semester"] == 1


def test_create_record_defaults_to_current_term_when_missing(
    client, test_db_session, admin_headers
):
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    resp = client.post(
        "/api/recruitment/records",
        json={"month": "114.09", "child_name": "測試童乙"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["target_school_year"] == sy
    assert body["target_semester"] == sem


def test_update_record_changes_target_term(client, test_db_session, admin_headers):
    created = client.post(
        "/api/recruitment/records",
        json={"month": "114.09", "child_name": "測試童丙",
              "target_school_year": 114, "target_semester": 1},
        headers=admin_headers,
    ).json()
    rid = created["id"]
    resp = client.put(
        f"/api/recruitment/records/{rid}",
        json={"target_school_year": 115, "target_semester": 2},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["target_school_year"] == 115
    assert resp.json()["target_semester"] == 2


def test_create_record_rejects_bad_semester(client, test_db_session, admin_headers):
    resp = client.post(
        "/api/recruitment/records",
        json={"month": "114.09", "child_name": "測試童丁",
              "target_school_year": 115, "target_semester": 3},
        headers=admin_headers,
    )
    assert resp.status_code == 422
```

> 註：`client` / `admin_headers` / `test_db_session` 用既有 conftest fixture（參考其他 `tests/test_recruitment_*.py` 的 fixture 名；若 admin header fixture 名稱不同請對齊）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_records_term.py -o addopts="" -q`
Expected: FAIL（target_* 未持久化 / 預設未填）

- [ ] **Step 3: 改 schema**

`api/recruitment/shared.py`，在 `RecruitmentVisitCreate`（622-651）的 `transfer_term: bool = False`（641 行）之後、`geocoding_consent` 之前插入：

```python
    target_school_year: Optional[int] = None
    target_semester: Optional[int] = None

    @field_validator("target_semester")
    @classmethod
    def validate_target_semester(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v not in (1, 2):
            raise ValueError("target_semester must be 1 or 2")
        return v
```

在 `RecruitmentVisitUpdate`（654-685）的 `transfer_term: Optional[bool] = None`（673 行）之後插入相同的兩個欄位與同名 validator（簽名相同，Optional）。

- [ ] **Step 4: POST handler 補預設當前學期**

`api/recruitment/records.py` 的 `create_recruitment_record`（178-194），在 `record = RecruitmentVisit(**data)`（185 行附近）之後、`session.add(record)` 之前插入：

```python
        if record.target_school_year is None or record.target_semester is None:
            cur_year, cur_sem = resolve_current_academic_term()
            if record.target_school_year is None:
                record.target_school_year = cur_year
            if record.target_semester is None:
                record.target_semester = cur_sem
```

確認檔頂有 `from utils.academic import resolve_current_academic_term`（無則加入 import 區）。

- [ ] **Step 5: 跑測試確認通過 + 既有招生測試不破**

Run:
```bash
cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_records_term.py -o addopts="" -q
cd ~/Desktop/ivy-backend && python -m pytest tests/ -k recruitment -o addopts="" -q
```
Expected: 新測試 PASS；既有 recruitment 測試全綠（POST /records 仍相容，因 target 為 Optional）。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/recruitment/shared.py api/recruitment/records.py tests/test_recruitment_records_term.py
git commit -m "feat(recruitment): 訪視建立/更新支援入學學期，create 缺值預設當前學期"
```

---

### Task 4: `GET /records` 入學學期篩選

**Files:**
- Modify: `api/recruitment/records.py:59-111`（list handler）
- Test: 追加於 `tests/test_recruitment_records_term.py`

**Interfaces:**
- Produces: `GET /recruitment/records?school_year=&semester=` 依 `target_school_year`/`target_semester` 篩選；semester 省略＝整學年。

- [ ] **Step 1: 寫失敗測試（追加）**

於 `tests/test_recruitment_records_term.py` 追加：

```python
def test_list_records_filter_by_term(client, test_db_session, admin_headers):
    for name, sy, sem in [("甲", 114, 1), ("乙", 114, 2), ("丙", 115, 1)]:
        client.post(
            "/api/recruitment/records",
            json={"month": "114.09", "child_name": f"濾測{name}",
                  "target_school_year": sy, "target_semester": sem},
            headers=admin_headers,
        )
    # 指定學年+學期 → 只回該期
    r = client.get(
        "/api/recruitment/records",
        params={"school_year": 114, "semester": 2},
        headers=admin_headers,
    ).json()
    names = {rec["child_name"] for rec in r["records"]}
    assert "濾測乙" in names
    assert "濾測甲" not in names and "濾測丙" not in names
    # 只指定學年 → 涵蓋整學年（上+下）
    r2 = client.get(
        "/api/recruitment/records",
        params={"school_year": 114},
        headers=admin_headers,
    ).json()
    names2 = {rec["child_name"] for rec in r2["records"]}
    assert {"濾測甲", "濾測乙"} <= names2
    assert "濾測丙" not in names2
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_records_term.py::test_list_records_filter_by_term -o addopts="" -q`
Expected: FAIL（參數未被接受/未過濾）

- [ ] **Step 3: 實作篩選**

`api/recruitment/records.py` 的 `list_recruitment_records`：
1. 在參數區（`keyword: Optional[str] = Query(None),` 之後，`dataset_scope` 之前）加：

```python
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None),
```

2. 在篩選 query 組裝區（`if month:` 之前或之後皆可，建議緊接 `if month:` 區塊後）加：

```python
        if school_year is not None:
            q = q.filter(RecruitmentVisit.target_school_year == school_year)
        if semester is not None:
            q = q.filter(RecruitmentVisit.target_semester == semester)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_records_term.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/recruitment/records.py tests/test_recruitment_records_term.py
git commit -m "feat(recruitment): GET /records 支援依入學學期篩選"
```

---

### Task 5: 漏斗看板改依「入學學期」分組

**Files:**
- Modify: `api/recruitment/funnel.py:59-119`（`get_board`）
- Test: `tests/test_recruitment_funnel_board_term.py`（新檔）
- Modify: 既有看板測試 fixture（grep 後補 target，見 Step 5）

**Interfaces:**
- Consumes: `RecruitmentVisit.target_school_year/target_semester`（Task 2/3 已保證有值）。
- Produces: `GET /recruitment/funnel/board?school_year=&semester=` 依 target 過濾（取代 month.in_）。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_recruitment_funnel_board_term.py`：

```python
from models.recruitment import RecruitmentVisit


def test_board_groups_by_enrollment_term_not_visit_month(
    client, test_db_session, admin_headers
):
    # 訪視月份屬 114 上學期(114.09)，但入學學期設為 115 上學期
    test_db_session.add(
        RecruitmentVisit(
            month="114.09", child_name="跨期童", has_deposit=False,
            target_school_year=115, target_semester=1,
        )
    )
    test_db_session.commit()
    # 查 115 上學期 → 應出現（依 target，而非依 visit month 的 114 上）
    r = client.get(
        "/api/recruitment/funnel/board",
        params={"school_year": 115, "semester": 1},
        headers=admin_headers,
    ).json()
    all_names = [c["child_name"] for stage in r["stages"].values() for c in stage]
    assert "跨期童" in all_names
    # 查 114 上學期 → 不應出現
    r2 = client.get(
        "/api/recruitment/funnel/board",
        params={"school_year": 114, "semester": 1},
        headers=admin_headers,
    ).json()
    all_names2 = [c["child_name"] for stage in r2["stages"].values() for c in stage]
    assert "跨期童" not in all_names2
```

> 註：`client`/`test_db_session` fixture 對齊既有 funnel 測試（參考 `tests/` 下既有 funnel board 測試檔的 fixture 取得 session 與建立 visit 的方式）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_funnel_board_term.py -o addopts="" -q`
Expected: FAIL（看板仍依 month，跨期童被歸到 114 上而非 115 上）

- [ ] **Step 3: 改 board 過濾**

`api/recruitment/funnel.py` 的 `get_board`，把 month-based 過濾（79-84 行）：

```python
    month_labels = school_term_to_roc_months(school_year, semester)
    visits = (
        session.query(RecruitmentVisit)
        .filter(RecruitmentVisit.month.in_(month_labels))
        .all()
    )
```

改為依入學學期過濾：

```python
    visit_q = session.query(RecruitmentVisit).filter(
        RecruitmentVisit.target_school_year == school_year
    )
    if semester is not None:
        visit_q = visit_q.filter(RecruitmentVisit.target_semester == semester)
    visits = visit_q.all()
```

並更新 `get_board` docstring：把「依訪視月份所屬學年圈定」「target_school_year 多為 NULL 不能過濾」等敘述，改為「依入學學期（target_school_year/target_semester）圈定；所有寫入路徑保證 target 有值（create 預設當前學期、import 由月份推導、enrterm01 backfill）」。若 `school_term_to_roc_months` 在本檔已無其他 caller，移除其 import（保留 `services/recruitment_funnel.py` 中的函式本體，stats 等他處仍可能用）。

- [ ] **Step 4: 跑新測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_funnel_board_term.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: 修既有看板測試（行為變更掃尾）**

既有看板測試多半建立只帶 `month` 的 visit、預期依月份出現在看板。改用 target 過濾後，未帶 target 的 fixture 會消失。執行：

```bash
cd ~/Desktop/ivy-backend && grep -rln "funnel/board\|get_board\|funnel_board" tests/
```
逐檔檢視建立 `RecruitmentVisit(...)` 的 fixture，凡未設 `target_school_year`/`target_semester` 且測試斷言其出現在特定學年看板者，補上對應 target（學年/學期對齊該測試查詢的 school_year/semester；若測試原意是「visit 月份所屬學期」，用 `roc_month_to_school_term(month)` 推導後填）。

Run（修完）: `cd ~/Desktop/ivy-backend && python -m pytest tests/ -k "funnel" -o addopts="" -q`
Expected: 全綠。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/recruitment/funnel.py tests/test_recruitment_funnel_board_term.py tests/  # 僅限本任務改動的 funnel 測試檔
git commit -m "feat(recruitment): 漏斗看板改依入學學期分組（取代訪視月份推算）"
```

> 提交前用 `git status` 確認只納入本任務檔案，勿掃入平行 session 的暫存改動。

---

### Task 6: Excel 匯入由訪視月份推導入學學期

**Files:**
- Modify: `api/recruitment/records.py`（`POST /import` handler，建立每筆 `RecruitmentVisit` 之處）
- Test: `tests/test_recruitment_import_term.py`（新檔）

**Interfaces:**
- Consumes: `roc_month_to_school_term`（Task 1）。
- Produces: import 產生的訪視，其 `target_school_year/target_semester` 由 `month` 推導（確保不從看板消失）。

- [ ] **Step 1: 讀 import handler**

Run: `cd ~/Desktop/ivy-backend && grep -n "import" api/recruitment/records.py | head -40`
找到 `@router.post("/import"` handler 與其建立 `RecruitmentVisit(...)`（或 `model_dump` → model）的迴圈。

- [ ] **Step 2: 寫失敗測試**

建立 `tests/test_recruitment_import_term.py`：

```python
def test_import_derives_target_term_from_month(client, test_db_session, admin_headers):
    resp = client.post(
        "/api/recruitment/import",
        json=[{"month": "115.03", "child_name": "匯入童"}],
        headers=admin_headers,
    )
    assert resp.status_code in (200, 201), resp.text
    # 115.03 → 入學學期 (114, 2)
    r = client.get(
        "/api/recruitment/records",
        params={"school_year": 114, "semester": 2},
        headers=admin_headers,
    ).json()
    assert "匯入童" in {rec["child_name"] for rec in r["records"]}
```

> 註：import 的 request body 形狀以實際 `ImportRecord` schema 為準，必要欄位對齊 handler；若最少欄位不同請調整。

- [ ] **Step 3: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_import_term.py -o addopts="" -q`
Expected: FAIL（匯入童 target 為 NULL，不在 114下篩選結果）

- [ ] **Step 4: 實作**

在 import handler 建立每筆 `RecruitmentVisit` 後（設定欄位的同一區塊），加入：

```python
        if record.target_school_year is None or record.target_semester is None:
            try:
                sy, sem = roc_month_to_school_term(record.month)
            except ValueError:
                sy = sem = None
            if record.target_school_year is None:
                record.target_school_year = sy
            if record.target_semester is None:
                record.target_semester = sem
```

於檔頂 import：`from services.recruitment_funnel import roc_month_to_school_term`（若該檔已 import 其他 recruitment_funnel 函式則併入）。

- [ ] **Step 5: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_import_term.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/recruitment/records.py tests/test_recruitment_import_term.py
git commit -m "feat(recruitment): Excel 匯入依訪視月份推導入學學期"
```

---

### Task 7: 轉成學生時寫入 `enrollment_semester`

**Files:**
- Modify: `services/recruitment_conversion.py:100-115`（Student 建立區塊）
- Test: `tests/test_recruitment_conversion_term.py`（新檔）

**Interfaces:**
- Consumes: `enroll_sem`（既有，88-97 行已決定）、`Student.enrollment_semester`（Task 2）。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_recruitment_conversion_term.py`：

```python
from models.recruitment import RecruitmentVisit
from models.classroom import Student
from services.recruitment_conversion import convert_visit_to_student  # 對齊實際函式名


def test_conversion_sets_enrollment_semester(test_db_session):
    visit = RecruitmentVisit(
        month="114.09", child_name="轉化童", has_deposit=True,
        target_school_year=115, target_semester=1,
    )
    test_db_session.add(visit)
    test_db_session.commit()

    student = convert_visit_to_student(  # 參數對齊實際簽名
        test_db_session, visit, classroom_id=None, recorded_by=None
    )
    test_db_session.commit()

    fetched = test_db_session.query(Student).get(student.id)
    assert fetched.enrollment_school_year == 115
    assert fetched.enrollment_semester == 1
```

> 註：`convert_visit_to_student` 的實際函式名與簽名請對齊 `services/recruitment_conversion.py`（讀檔確認；88-97 行決定 enroll_year/enroll_sem，100-115 建立 Student）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_conversion_term.py -o addopts="" -q`
Expected: FAIL（`enrollment_semester` 為 None）

- [ ] **Step 3: 實作**

`services/recruitment_conversion.py` 的 `Student(...)` 建立（100-115 行），在 `enrollment_school_year=enroll_year,` 之後加：

```python
        enrollment_semester=enroll_sem,
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_conversion_term.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add services/recruitment_conversion.py tests/test_recruitment_conversion_term.py
git commit -m "feat(recruitment): 轉成學生時寫入 enrollment_semester"
```

---

### Task 8: 學生詳情暴露 `enrollment_semester`

**Files:**
- Modify: `services/student_profile.py:291-305`（`_serialize_lifecycle`）
- Modify: `schemas/students.py`（`StudentDetailOut` 加欄位）+ `api/students.py:768-789`（payload）
- Test: `tests/test_student_profile_term.py`（新檔）

**Interfaces:**
- Produces: `GET /students/{id}/profile` 回應 `lifecycle.enrollment_school_year` + `lifecycle.enrollment_semester`；`GET /students/{id}`（StudentDetailOut）加 `enrollment_semester`。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_student_profile_term.py`：

```python
def test_profile_lifecycle_exposes_enrollment_semester(test_db_session):
    from models.classroom import Student
    from services.student_profile import assemble_profile

    s = Student(
        name="檔案童", lifecycle_status="active",
        enrollment_school_year=115, enrollment_semester=1,
    )
    test_db_session.add(s)
    test_db_session.commit()

    profile = assemble_profile(test_db_session, s.id)
    assert profile["lifecycle"]["enrollment_school_year"] == 115
    assert profile["lifecycle"]["enrollment_semester"] == 1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_student_profile_term.py -o addopts="" -q`
Expected: FAIL（`KeyError: 'enrollment_semester'`）

- [ ] **Step 3: 改 `_serialize_lifecycle`**

`services/student_profile.py` 的 `_serialize_lifecycle`（291-305），在 `"recruitment_visit_id": student.recruitment_visit_id,`（304 行）之前加：

```python
        "enrollment_school_year": student.enrollment_school_year,
        "enrollment_semester": student.enrollment_semester,
```

- [ ] **Step 4: 同步 StudentDetailOut（GET /students/{id}）**

`schemas/students.py` 的 `StudentDetailOut`，在 `enrollment_date` 欄位旁加 `enrollment_semester: Optional[int] = None`（型別風格對齊該檔；若有 `enrollment_school_year` 則一併補）。
`api/students.py` 的 `get_student` payload（768-789），在 `"enrollment_date": ...` 之後加：

```python
        "enrollment_semester": student.enrollment_semester,
```

- [ ] **Step 5: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_student_profile_term.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add services/student_profile.py schemas/students.py api/students.py tests/test_student_profile_term.py
git commit -m "feat(students): 學生詳情/檔案暴露 enrollment_semester"
```

---

### Task 9: `/stats` 與 `/stats/export` 加入學學期 scope

**Files:**
- Modify: `api/recruitment/stats.py:58-63`（`_query_stats` 簽名）、`68`（base_filters）、`639-649`（GET /stats）、`669-680`（export）
- Test: `tests/test_recruitment_stats_term.py`（新檔）

**Interfaces:**
- Produces: `GET /recruitment/stats?school_year=&semester=` 與 `/stats/export` 同參；以 `target_*` scope 整份統計。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_recruitment_stats_term.py`：

```python
from models.recruitment import RecruitmentVisit


def test_stats_scoped_by_enrollment_term(client, test_db_session, admin_headers):
    test_db_session.add_all([
        RecruitmentVisit(month="114.09", child_name="統甲", has_deposit=True,
                         target_school_year=115, target_semester=1),
        RecruitmentVisit(month="114.09", child_name="統乙", has_deposit=False,
                         target_school_year=114, target_semester=2),
    ])
    test_db_session.commit()
    r = client.get(
        "/api/recruitment/stats",
        params={"school_year": 115, "semester": 1},
        headers=admin_headers,
    ).json()
    assert r["total_visit"] == 1  # 只算入學 115 上學期者
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_stats_term.py -o addopts="" -q`
Expected: FAIL（未 scope，total_visit != 1）

- [ ] **Step 3: 實作**

`api/recruitment/stats.py`：
1. `_query_stats` 簽名加參數：

```python
def _query_stats(
    session,
    reference_month: Optional[str] = None,
    dataset_scope: Optional[str] = None,
    school_year: Optional[int] = None,
    semester: Optional[int] = None,
) -> dict:
```

2. base_filters 後追加 term 過濾（68 行 `base_filters = _dataset_scope_filters(dataset_scope)` 之後）：

```python
    base_filters = list(_dataset_scope_filters(dataset_scope))
    if school_year is not None:
        base_filters.append(RecruitmentVisit.target_school_year == school_year)
    if semester is not None:
        base_filters.append(RecruitmentVisit.target_semester == semester)
```

3. `get_recruitment_stats`（639-649）加 `school_year`/`semester` query 並轉傳：

```python
@router.get("/stats")
def get_recruitment_stats(
    reference_month: Optional[str] = None,
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None),
    dataset_scope: str = Query(DATASET_SCOPE_ALL, pattern="^(all)$"),
    _=Depends(require_staff_permission(Permission.RECRUITMENT_READ)),
):
    """完整統計匯總（全 SQL GROUP BY，效能最佳化版）；可依入學學期 scope。"""
    with session_scope() as session:
        return _query_stats(
            session,
            reference_month=reference_month,
            dataset_scope=dataset_scope,
            school_year=school_year,
            semester=semester,
        )
```

4. `export_recruitment_stats`（669-680）同樣加參並轉傳給 `_query_stats`。

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_recruitment_stats_term.py -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/recruitment/stats.py tests/test_recruitment_stats_term.py
git commit -m "feat(recruitment): /stats 與匯出支援依入學學期 scope"
```

---

## Phase B — OpenAPI codegen（防漂移）

### Task 10: 重新產生前端 `schema.d.ts`

**Files:**
- Modify: `ivy-frontend/src/api/_generated/schema.d.ts`（產生物）

- [ ] **Step 1: 產 openapi + 前端型別**

Run:
```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
cd ~/Desktop/ivy-frontend && npm run gen:api
```

- [ ] **Step 2: 確認 typecheck 綠**

Run: `cd ~/Desktop/ivy-frontend && npm run type-check`
Expected: 無新錯誤（新欄位/參數已併入 schema.d.ts）

- [ ] **Step 3: Commit（僅 schema.d.ts）**

```bash
cd ~/Desktop/ivy-frontend
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regenerate schema.d.ts（招生入學學期欄位/參數）"
```

> `openapi.json` 為 dev-time artifact，受 .gitignore 擋，勿提交。

---

## Phase C — 前端（ivy-frontend）

### Task 11: `VisitFormState` 加入學學期欄位（預設當前學期）

**Files:**
- Modify: `src/constants/recruitment.ts:15-49`
- Test: `src/constants/__tests__/recruitment.spec.ts`（新檔，若已有則追加）

**Interfaces:**
- Produces: `VisitFormState.target_school_year: number`、`target_semester: 1 | 2`；`emptyVisitForm()` 預設帶當前學期。

- [ ] **Step 1: 寫失敗測試**

建立/追加 `src/constants/__tests__/recruitment.spec.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { emptyVisitForm } from '@/constants/recruitment'
import { getCurrentAcademicTerm } from '@/utils/academic'

describe('emptyVisitForm', () => {
  it('預設帶當前學年/學期', () => {
    const term = getCurrentAcademicTerm()
    const f = emptyVisitForm()
    expect(f.target_school_year).toBe(term.school_year)
    expect(f.target_semester).toBe(term.semester)
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/constants/__tests__/recruitment.spec.ts`
Expected: FAIL（欄位不存在）

- [ ] **Step 3: 實作**

`src/constants/recruitment.ts`：
1. 檔頂加 `import { getCurrentAcademicTerm } from '@/utils/academic'`。
2. `VisitFormState` interface（15-37）在 `transfer_term: boolean`（31）後加：

```ts
  target_school_year: number
  target_semester: 1 | 2
```

3. `emptyVisitForm()`（39-49）改為先取當前學期再回傳：

```ts
export function emptyVisitForm(): VisitFormState {
  const term = getCurrentAcademicTerm()
  return {
    month: '', month_raw: null, seq_no: '', visit_date: '', child_name: '',
    birthday: null, grade: null, phone: '', address: '',
    district: '', source: '', referrer: '', deposit_collector: '',
    has_deposit: false, enrolled: false, transfer_term: false,
    target_school_year: term.school_year,
    target_semester: term.semester as 1 | 2,
    no_deposit_reason: null, no_deposit_reason_detail: '',
    notes: '', parent_response: '',
    geocoding_consent: false,
  }
}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/constants/__tests__/recruitment.spec.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/constants/recruitment.ts src/constants/__tests__/recruitment.spec.ts
git commit -m "feat(recruitment): VisitFormState 加入學學期欄位並預設當前學期"
```

---

### Task 12: 訪視表單加「入學學期」選擇器

**Files:**
- Modify: `src/components/recruitment/RecruitmentRecordDialog.vue`
- Test: `src/components/recruitment/__tests__/RecruitmentRecordDialog.spec.ts`（新檔，若已有則追加）

**Interfaces:**
- Consumes: `form.target_school_year` / `form.target_semester`（Task 11）。
- Produces: 表單可選入學學期（學年下拉 + 學期切鈕），選項範圍當前學年 −1~+3。

- [ ] **Step 1: 寫失敗測試**

建立 `src/components/recruitment/__tests__/RecruitmentRecordDialog.spec.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import RecruitmentRecordDialog from '@/components/recruitment/RecruitmentRecordDialog.vue'
import { emptyVisitForm } from '@/constants/recruitment'

describe('RecruitmentRecordDialog 入學學期', () => {
  it('渲染入學學期選擇器並綁定 form', () => {
    const wrapper = mount(RecruitmentRecordDialog, {
      props: { visible: true, mode: 'add', form: emptyVisitForm() },
      global: { stubs: { teleport: true } },
    })
    expect(wrapper.html()).toContain('入學學期')
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/components/recruitment/__tests__/RecruitmentRecordDialog.spec.ts`
Expected: FAIL（找不到「入學學期」）

- [ ] **Step 3: 實作**

`RecruitmentRecordDialog.vue`：
1. template 在「基本資料」FormSection 內（「適讀班級」之後）加一個入學學期區塊：

```vue
        <el-form-item label="入學學期" required>
          <div class="enroll-term">
            <el-select v-model="form.target_school_year" size="small" style="width:130px">
              <el-option v-for="y in enrollYearOptions" :key="y" :value="y" :label="`${y} 學年`" />
            </el-select>
            <el-radio-group v-model="form.target_semester" size="small">
              <el-radio-button :value="1">上學期</el-radio-button>
              <el-radio-button :value="2">下學期</el-radio-button>
            </el-radio-group>
          </div>
          <div class="form-hint">小孩預計入學的學期（預設當前學期，可改）。</div>
        </el-form-item>
```

2. script 區：`import { currentRocYear } from '@/utils/academic'`（與既有 `toRocYear` import 併）；加：

```ts
const enrollYearOptions = computed(() => {
  const y = currentRocYear()
  return [y + 3, y + 2, y + 1, y, y - 1]
})
```

（檔頂 `import { ref, watch, reactive, nextTick } from 'vue'` 補上 `computed`。）

3. 本地 `VisitForm` interface（124-146）加：

```ts
  target_school_year?: number
  target_semester?: number
```

4. style scoped 加：

```css
.enroll-term { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
```

- [ ] **Step 4: 跑測試確認通過 + 既有對話框測試**

Run:
```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/components/recruitment/__tests__/RecruitmentRecordDialog.spec.ts
```
Expected: PASS。若 repo 已有此對話框的既有測試，一併跑確認不破。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/components/recruitment/RecruitmentRecordDialog.vue src/components/recruitment/__tests__/RecruitmentRecordDialog.spec.ts
git commit -m "feat(recruitment): 訪視表單加入學學期選擇器（預設當前學期）"
```

---

### Task 13: 訪視明細加學期篩選與「入學學期」欄；編輯帶回 target

**Files:**
- Modify: `src/components/recruitment/AdmissionsRecordsPanel.vue`（filter / fetchDetail / openEditDialog / clearFilter / watch / onMounted）
- Modify: `src/components/recruitment/RecruitmentDetailTab.vue`（filter-bar / DetailFilters / 表格欄）
- Test: `src/components/recruitment/__tests__/AdmissionsRecordsPanel.term.spec.ts`（新檔）

**Interfaces:**
- Consumes: `getRecruitmentRecords`（params 透傳 school_year/semester）。
- Produces: 明細可按入學學期篩選；表格顯示入學學期；編輯訪視保留 target。

- [ ] **Step 1: 寫失敗測試**

建立 `src/components/recruitment/__tests__/AdmissionsRecordsPanel.term.spec.ts`（聚焦 fetchDetail 帶 term 參數）：

```ts
import { describe, it, expect, vi } from 'vitest'

vi.mock('@/api/recruitment', () => ({
  getRecruitmentRecords: vi.fn(() => Promise.resolve({ data: { records: [], total: 0 } })),
  createRecruitmentRecord: vi.fn(),
  updateRecruitmentRecord: vi.fn(),
  deleteRecruitmentRecord: vi.fn(),
}))

import { getRecruitmentRecords } from '@/api/recruitment'
import { mount } from '@vue/test-utils'
import AdmissionsRecordsPanel from '@/components/recruitment/AdmissionsRecordsPanel.vue'

function makeDashboard() {
  return {
    options: { value: {} },
    stats: { value: { by_district: [] } },
    invalidateOptions: vi.fn(),
    fetchOptions: vi.fn(() => Promise.resolve()),
  } as unknown as never
}

describe('AdmissionsRecordsPanel 學期篩選', () => {
  it('帶 school_year/semester 進 getRecruitmentRecords', async () => {
    const wrapper = mount(AdmissionsRecordsPanel, {
      props: { dashboard: makeDashboard() },
      global: { stubs: { RecruitmentDetailTab: true, RecruitmentMonthDialog: true,
        RecruitmentRecordDialog: true, RecruitmentConvertDialog: true,
        ReserveSeatDialog: true, JourneyTimeline: true } },
    })
    ;(wrapper.vm as unknown as { filter: { value?: unknown } }).filter // 觸發 setup
    // 透過 update-filter 模擬選學期
    const vm = wrapper.vm as unknown as {
      updateDetailFilter: (p: Record<string, unknown>) => void
      fetchDetail: () => Promise<boolean>
    }
    vm.updateDetailFilter({ school_year: 115, semester: 1 })
    await vm.fetchDetail()
    const calls = (getRecruitmentRecords as unknown as { mock: { calls: unknown[][] } }).mock.calls
    const lastParams = calls[calls.length - 1][0] as Record<string, unknown>
    expect(lastParams.school_year).toBe(115)
    expect(lastParams.semester).toBe(1)
  })
})
```

> 註：`updateDetailFilter` / `fetchDetail` 已在 `defineExpose` 或元件實例可達；若不可達，改以掛載後操作 stub 的 `@update-filter`/`@filter-change` 事件達成。fixture 形狀對齊 `useRecruitmentDashboard` 回傳。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/components/recruitment/__tests__/AdmissionsRecordsPanel.term.spec.ts`
Expected: FAIL（params 不含 school_year）

- [ ] **Step 3: 改 AdmissionsRecordsPanel.vue**

1. filter ref（176-191）的初始物件加 `school_year: null, semester: null`，型別加 `school_year: number | null; semester: number | null`。
2. `fetchDetail`（252-275）在組 params 處加：

```ts
    if (filter.value.school_year != null) params.school_year = filter.value.school_year
    if (filter.value.semester != null) params.semester = filter.value.semester
```

3. `openEditDialog`（310-340）的 `form.value = { ... }` 物件加：

```ts
    target_school_year: (row.target_school_year as number | null) ?? emptyVisitForm().target_school_year,
    target_semester: (row.target_semester as number | null) ?? emptyVisitForm().target_semester,
```

4. `clearFilter`（278-285）reset 物件加 `school_year: null, semester: null`。
5. `filterPatch` watch（386-398）reset 物件加 `school_year: null, semester: null`。

- [ ] **Step 4: 改 RecruitmentDetailTab.vue**

1. `DetailFilters` interface（164-175）加：

```ts
  school_year?: number | null
  semester?: number | null
```

2. filter-bar（3-80）在「月份」select 之後加學年/學期下拉：

```vue
      <el-select
        :model-value="filters.school_year ?? null"
        placeholder="入學學年" clearable size="small" style="width:120px"
        @update:model-value="updateFilter('school_year', $event)"
        @change="$emit('filter-change')"
      >
        <el-option v-for="y in termYearOptions" :key="y" :label="`${y} 學年`" :value="y" />
      </el-select>
      <el-select
        :model-value="filters.semester ?? null"
        placeholder="學期" clearable size="small" style="width:100px"
        @update:model-value="updateFilter('semester', $event)"
        @change="$emit('filter-change')"
      >
        <el-option :label="'上學期'" :value="1" />
        <el-option :label="'下學期'" :value="2" />
      </el-select>
```

3. script 加（import + computed + formatter）：

```ts
import { currentRocYear } from '@/utils/academic'
import { formatSemester } from '@/utils/classHistory'

const termYearOptions = computed(() => {
  const y = currentRocYear()
  return [y + 1, y, y - 1, y - 2]
})

const enrollTermText = (row: Record<string, unknown>): string => {
  const sy = row.target_school_year as number | null
  const sem = row.target_semester as number | null
  return sy != null && sem != null ? formatSemester(sy, sem) : '—'
}
```

（若 `computed` 尚未 import 於本檔，補上。`updateFilter` 為既有 emit helper。）

4. 表格（84-149）在「班別」欄之後加：

```vue
      <el-table-column label="入學學期" width="110" :formatter="(row) => enrollTermText(row as Record<string, unknown>)" />
```

- [ ] **Step 5: 跑測試 + 家長/學生三測試樹掃尾**

Run:
```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/components/recruitment/__tests__/AdmissionsRecordsPanel.term.spec.ts
cd ~/Desktop/ivy-frontend && npm run type-check
```
Expected: PASS、typecheck 綠。

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/components/recruitment/AdmissionsRecordsPanel.vue src/components/recruitment/RecruitmentDetailTab.vue src/components/recruitment/__tests__/AdmissionsRecordsPanel.term.spec.ts
git commit -m "feat(recruitment): 訪視明細加入學學期篩選與欄位，編輯保留 target"
```

---

### Task 14: 漏斗看板選擇器文案與年份範圍

**Files:**
- Modify: `src/components/recruitment/funnel/FunnelBoard.vue`

**Interfaces:**
- 後端 board 已改依入學學期（Task 5）；本任務只調 UI 文案/年份範圍，無契約改動。

- [ ] **Step 1: 改文案與年份範圍**

`FunnelBoard.vue`：
1. 學年 select placeholder 由 `"學年"` 改 `"入學學年"`；學期 select placeholder 由 `"學期"` 改 `"入學學期"`（template 1-40）。
2. `yearOptions`（94 行）由 `[currentYear, currentYear - 1, currentYear - 2]` 改為涵蓋未來梯次：

```ts
const yearOptions = computed(() => [currentYear + 1, currentYear, currentYear - 1, currentYear - 2])
```

- [ ] **Step 2: 驗證**

Run: `cd ~/Desktop/ivy-frontend && npm run type-check`
Expected: 綠。（如有 FunnelBoard 既有測試一併跑。）

- [ ] **Step 3: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/components/recruitment/funnel/FunnelBoard.vue
git commit -m "feat(recruitment): 漏斗看板選擇器標示入學學期並涵蓋未來梯次"
```

---

### Task 15: 學生詳情顯示「入學學期」

**Files:**
- Modify: `src/components/student/tabs/OverviewTab.vue:126-144`（學籍狀態卡）
- Test: `src/components/student/tabs/__tests__/OverviewTab.term.spec.ts`（新檔）

**Interfaces:**
- Consumes: `lifecycle.enrollment_school_year` + `lifecycle.enrollment_semester`（Task 8）、`formatSemester`（`@/utils/classHistory`）。

- [ ] **Step 1: 寫失敗測試**

建立 `src/components/student/tabs/__tests__/OverviewTab.term.spec.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import OverviewTab from '@/components/student/tabs/OverviewTab.vue'

describe('OverviewTab 入學學期', () => {
  it('顯示入學學期文字', () => {
    const wrapper = mount(OverviewTab, {
      props: { profile: { lifecycle: {
        enrollment_date: '2025-09-01',
        enrollment_school_year: 114, enrollment_semester: 1,
      }, basic: {} } },
      global: { stubs: { teleport: true } },
    })
    expect(wrapper.text()).toContain('入學學期')
    expect(wrapper.text()).toContain('114 上學期')
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/components/student/tabs/__tests__/OverviewTab.term.spec.ts`
Expected: FAIL

- [ ] **Step 3: 實作**

`OverviewTab.vue`：
1. script 加 `import { formatSemester } from '@/utils/classHistory'` 與 computed：

```ts
const enrollTermText = computed(() => {
  const sy = lifecycle.value.enrollment_school_year as number | null
  const sem = lifecycle.value.enrollment_semester as number | null
  return sy != null && sem != null ? formatSemester(sy, sem) : '—'
})
```

2. template 在「入學日」descriptions-item（135 行）之後加：

```vue
          <el-descriptions-item label="入學學期">{{ enrollTermText }}</el-descriptions-item>
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/components/student/tabs/__tests__/OverviewTab.term.spec.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/components/student/tabs/OverviewTab.vue src/components/student/tabs/__tests__/OverviewTab.term.spec.ts
git commit -m "feat(students): 學生詳情顯示入學學期"
```

---

### Task 16: 統計面板加入學學期篩選

**Files:**
- Modify: `src/components/recruitment/RecruitmentStatsPanel.vue`（toolbar + refs + stats fetch/export params）

**Interfaces:**
- Consumes: `getRecruitmentStats` / `exportRecruitmentStats`（params 透傳 school_year/semester）。

- [ ] **Step 1: 讀現有 stats 載入函式**

Run: `cd ~/Desktop/ivy-frontend && grep -n "getRecruitmentStats\|exportRecruitmentStats\|referenceMonth" src/components/recruitment/RecruitmentStatsPanel.vue`
找到主統計 fetch（呼叫 `getRecruitmentStats(...)`）與匯出（`exportRecruitmentStats(...)`）組 params 之處。

- [ ] **Step 2: 加 toolbar 選擇器與 refs**

template toolbar（1-25）在「參考月份」select 之前加：

```vue
      <el-select v-model="termYear" size="small" placeholder="入學學年" clearable
        style="width: 120px" @change="fetchStats">
        <el-option v-for="y in termYearOptions" :key="y" :label="`${y} 學年`" :value="y" />
      </el-select>
      <el-select v-model="termSemester" size="small" placeholder="學期" clearable
        style="width: 100px" @change="fetchStats">
        <el-option :label="'上學期'" :value="1" />
        <el-option :label="'下學期'" :value="2" />
      </el-select>
```

script 加（與既有 `currentRocYear` import 併；`fetchStats` 對齊實際主 fetch 函式名）：

```ts
import { currentRocYear } from '@/utils/academic'
const termYear = ref<number | null>(null)
const termSemester = ref<1 | 2 | null>(null)
const termYearOptions = computed(() => {
  const y = currentRocYear()
  return [y + 1, y, y - 1, y - 2]
})
```

- [ ] **Step 3: 透傳 params**

在主統計 fetch 組 params 物件處（仿 `referenceMonth` 寫法）加：

```ts
    if (termYear.value != null) params.school_year = termYear.value
    if (termSemester.value != null) params.semester = termSemester.value
```

於 `exportRecruitmentStats` 的 params 同樣加上述兩行。

- [ ] **Step 4: 驗證**

Run: `cd ~/Desktop/ivy-frontend && npm run type-check`
Expected: 綠。（如有 stats 面板既有測試一併跑。）

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/components/recruitment/RecruitmentStatsPanel.vue
git commit -m "feat(recruitment): 統計面板加入學學期篩選"
```

---

## 整合驗證（全部完成後）

- [ ] 啟動兩端：`cd ~/Desktop/ivyManageSystem && ./start.sh`
- [ ] 手動走一次：① 新增訪視（入學學期預設當前學期、可改 115 上）② 明細用學期下拉篩出該生 ③ 漏斗看板選 115 上學期看到該生（即使訪視月份屬其他學期）④ 轉成學生 ⑤ 學生詳情顯示「入學學期 = 115 上學期」⑥ 統計面板選 115 上學期數字正確。
- [ ] 後端全測試 sanity：`cd ~/Desktop/ivy-backend && python -m pytest tests/ -k "recruitment or student_profile" -o addopts="" -q`
- [ ] 前端：`cd ~/Desktop/ivy-frontend && npm run type-check && npx vitest run src/components/recruitment src/components/student src/constants`

---

## 收尾（Definition of Done）

- 後端 commit 一串、前端 commit 一串，分屬各自 repo。
- 跑 `cd ~/Desktop/ivyManageSystem && ./scripts/finish-check.sh` 前，注意：push 後端會在正式 DB 跑 `enrterm01` migration（含 backfill）—— 確認 prod 前置後再 push。
- CI 綠需自行到 GitHub Actions 確認（含兩 repo `openapi-drift` job）。

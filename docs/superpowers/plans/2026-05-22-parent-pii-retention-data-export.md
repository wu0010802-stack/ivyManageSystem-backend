# 家長端 PII Retention + Data-Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 為畢業/轉出/退學學生的家長端 PII 落地 365 天 retention（個資法 §11），同時加 data-export endpoint 滿足當事人查閱權（§10）。

**Architecture:**
1. 後端：加 `Student.terminal_entered_at` + `Guardian.pii_redacted_at` 兩欄位 + `set_lifecycle_status` helper + 新 `pii_retention_scheduler.py`（日級 GC，dry-run 預設開）+ `GET /api/parent/me/data-export` 同步 JSON endpoint。
2. 前端：`MeView.vue` 加「下載我的個人資料」row + dialog + `useDataExport` composable。
3. 部署：migration backfill 現有終態學生 → backend deploy（GC ENV 預設 disabled）→ codegen → frontend deploy → user 開啟 dry-run review → 正式啟用。

**Tech Stack:** FastAPI / SQLAlchemy / Alembic / pytest / Vue 3 (`<script setup lang="ts">`) / Pinia 不涉 / Vitest

**Spec:** `ivy-backend/docs/superpowers/specs/2026-05-22-parent-pii-retention-data-export-design.md`

**Worktrees（兩個並行）:**
- BE: `ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend` branch `feat/parent-pii-retention-2026-05-22-backend` 起自 `origin/main`
- FE: `ivy-frontend/.claude/worktrees/parent-pii-retention-2026-05-22-frontend` branch `feat/parent-pii-retention-2026-05-22-frontend` 起自 `origin/main`

**Current Alembic head:** `3be2e40aaa42`（`20260521_3be2e40aaa42_merge_parlsr_recurr.py`）—— 本 plan migration 接於此後。

---

## File Structure

### 後端新增
- `alembic/versions/20260522_pretent001_pii_retention_columns.py`
- `utils/student_lifecycle.py`
- `services/pii_retention_scheduler.py`
- `api/parent_portal/data_export.py`
- `tests/test_student_lifecycle_helper.py`
- `tests/test_pii_retention_gc.py`
- `tests/test_parent_data_export.py`
- `tests/test_alembic_pretent001.py`

### 後端修改
- `config/scheduler.py`（加 3 個 env field）
- `main.py`（啟動 pii_retention_scheduler）
- `api/parent_portal/__init__.py`（register data_export router）
- 約 3-5 處直接寫 `student.lifecycle_status = X` 的 caller → 改走 `set_lifecycle_status`
- `CLAUDE.md`（ivy-backend）加「PII Retention 政策」章節
- `CLAUDE.md`（workspace）加跨端注意

### 前端新增
- `src/parent/composables/useDataExport.ts`
- `src/parent/composables/__tests__/useDataExport.test.ts`

### 前端修改
- `src/parent/views/MeView.vue` 加 export row + dialog
- `src/parent/views/__tests__/MeView.test.ts`
- `src/api/_generated/schema.d.ts`（codegen 自動）

---

## Task 1: 建立兩個 worktree

**Files:**
- 建立 BE / FE worktree

- [ ] **Step 1: 建 BE worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git worktree add -b feat/parent-pii-retention-2026-05-22-backend .claude/worktrees/parent-pii-retention-2026-05-22-backend origin/main
```

- [ ] **Step 2: 建 FE worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git worktree add -b feat/parent-pii-retention-2026-05-22-frontend .claude/worktrees/parent-pii-retention-2026-05-22-frontend origin/main
```

- [ ] **Step 3: 確認兩個 worktree 都拿到 main HEAD**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend && git log -1 --oneline
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/parent-pii-retention-2026-05-22-frontend && git log -1 --oneline
```

Expected：兩邊都顯示 origin/main HEAD commit。

---

## Task 2: Alembic Migration `pretent001`（schema + backfill）

**Files:**
- Create: `alembic/versions/20260522_pretent001_pii_retention_columns.py`
- Test: `tests/test_alembic_pretent001.py`

**Working dir:** BE worktree

- [ ] **Step 1: 寫 migration test（會失敗，因 migration 還沒寫）**

```python
# tests/test_alembic_pretent001.py
"""Migration pretent001 test：up 加欄位+index+backfill / down 完全乾淨。"""

from datetime import datetime, timedelta
from sqlalchemy import inspect, text
from alembic import command
from alembic.config import Config

from tests.conftest import alembic_test_engine  # 既有 fixture


def _get_cols(engine, table):
    return {c['name'] for c in inspect(engine).get_columns(table)}


def test_pretent001_upgrade_adds_columns(alembic_test_engine):
    engine = alembic_test_engine
    cfg = Config("alembic.ini")
    cfg.attributes["connection"] = engine.connect()
    command.upgrade(cfg, "pretent001")

    students_cols = _get_cols(engine, "students")
    guardians_cols = _get_cols(engine, "guardians")
    assert "terminal_entered_at" in students_cols
    assert "pii_redacted_at" in guardians_cols


def test_pretent001_downgrade_drops_columns(alembic_test_engine):
    engine = alembic_test_engine
    cfg = Config("alembic.ini")
    cfg.attributes["connection"] = engine.connect()
    command.upgrade(cfg, "pretent001")
    command.downgrade(cfg, "-1")

    students_cols = _get_cols(engine, "students")
    guardians_cols = _get_cols(engine, "guardians")
    assert "terminal_entered_at" not in students_cols
    assert "pii_redacted_at" not in guardians_cols


def test_pretent001_backfill_uses_updated_at(alembic_test_engine):
    """無 audit_log 記錄時，backfill 用 students.updated_at。"""
    engine = alembic_test_engine
    cfg = Config("alembic.ini")
    cfg.attributes["connection"] = engine.connect()

    # 升到上一版（pretent001 之前）
    command.upgrade(cfg, "3be2e40aaa42")

    with engine.begin() as conn:
        # 建一筆畢業學生（updated_at 設特定時間）
        ten_months_ago = datetime.now() - timedelta(days=300)
        conn.execute(text("""
            INSERT INTO students (id, name, lifecycle_status, updated_at, created_at, birth_date)
            VALUES (:id, '已畢業學生', 'graduated', :ts, :ts, '2018-01-01')
        """), {"id": 99001, "ts": ten_months_ago})

    command.upgrade(cfg, "pretent001")

    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT terminal_entered_at FROM students WHERE id = 99001"
        )).fetchone()
        assert row[0] is not None
        # 應該等於 updated_at（fallback）
        assert abs((row[0] - ten_months_ago).total_seconds()) < 5
```

- [ ] **Step 2: 確認測試 fail（migration 還沒寫）**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend
pytest tests/test_alembic_pretent001.py -v
```

Expected: FAIL with "Can't locate revision identified by 'pretent001'"

- [ ] **Step 3: 寫 migration 檔**

```python
# alembic/versions/20260522_pretent001_pii_retention_columns.py
"""pretent001 — 加 students.terminal_entered_at + guardians.pii_redacted_at + backfill

Revision ID: pretent001
Revises: 3be2e40aaa42
Create Date: 2026-05-22

加兩個 nullable 欄位 + 兩個 partial index，並 backfill 現有 終態學生的
terminal_entered_at（從 audit_logs 反推，找不到 fallback updated_at）。
"""
from alembic import op
import sqlalchemy as sa

revision = "pretent001"
down_revision = "3be2e40aaa42"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "students",
        sa.Column(
            "terminal_entered_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "進入終態（graduated/transferred/withdrawn）的 UTC 時間戳；"
                "復學回 active 時 NULL；PII retention GC 計算用"
            ),
        ),
    )
    op.add_column(
        "guardians",
        sa.Column(
            "pii_redacted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Guardian PII 被 retention GC 抹除的時間戳；"
                "NOT NULL 即已抹過避免重複 GC"
            ),
        ),
    )

    # Partial index for GC scan
    op.create_index(
        "ix_student_terminal_retention",
        "students",
        ["terminal_entered_at", "lifecycle_status"],
        postgresql_where=sa.text("terminal_entered_at IS NOT NULL"),
    )
    op.create_index(
        "ix_guardians_pii_redacted_null",
        "guardians",
        ["student_id"],
        postgresql_where=sa.text("pii_redacted_at IS NULL"),
    )

    # Backfill：現有終態學生從 audit_logs 找 lifecycle 變更時間
    # audit_logs.entity_id 是 String(50)：用 regex 過濾純數字後 cast
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("""
            WITH lifecycle_changes AS (
                SELECT
                    CAST(entity_id AS INTEGER) AS student_id,
                    MAX(created_at) AS last_change_at
                FROM audit_logs
                WHERE entity_type = 'student'
                  AND action IN ('UPDATE', 'CREATE')
                  AND (changes LIKE '%lifecycle_status%' OR summary LIKE '%lifecycle%')
                  AND entity_id ~ '^\\d+$'
                GROUP BY entity_id
            )
            UPDATE students s
            SET terminal_entered_at = COALESCE(lc.last_change_at, s.updated_at)
            FROM lifecycle_changes lc
            WHERE s.id = lc.student_id
              AND s.lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
              AND s.terminal_entered_at IS NULL;

            UPDATE students
            SET terminal_entered_at = updated_at
            WHERE lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
              AND terminal_entered_at IS NULL;
        """)
    else:
        # SQLite test fallback：直接用 updated_at
        op.execute("""
            UPDATE students
            SET terminal_entered_at = updated_at
            WHERE lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
              AND terminal_entered_at IS NULL;
        """)


def downgrade():
    op.drop_index("ix_guardians_pii_redacted_null", "guardians")
    op.drop_index("ix_student_terminal_retention", "students")
    op.drop_column("guardians", "pii_redacted_at")
    op.drop_column("students", "terminal_entered_at")
```

- [ ] **Step 4: 確認測試通過**

```bash
pytest tests/test_alembic_pretent001.py -v
```

Expected: 3 tests PASS

- [ ] **Step 5: 套用到 local DB 驗證 backfill**

```bash
alembic upgrade heads
psql ivymanagement -c "SELECT COUNT(*) FROM students WHERE lifecycle_status IN ('graduated','transferred','withdrawn') AND terminal_entered_at IS NULL;"
```

Expected: 第二行回 `0`（所有終態學生都有戳記了）

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/20260522_pretent001_pii_retention_columns.py tests/test_alembic_pretent001.py
git commit -m "feat(db): add terminal_entered_at + pii_redacted_at for PII retention (pretent001)"
```

---

## Task 3: Config Scheduler env fields

**Files:**
- Modify: `config/scheduler.py`
- Test: `tests/test_config_scheduler.py`（既有檔，擴）

**Working dir:** BE worktree

- [ ] **Step 1: 寫測試**

```python
# tests/test_config_scheduler.py — 新增以下測試
def test_pii_retention_defaults():
    """PII retention 預設安全：disabled + dry-run + 365 天。"""
    from config.scheduler import SchedulerSettings
    s = SchedulerSettings()
    assert s.pii_retention_gc_disabled is True
    assert s.pii_retention_gc_dry_run is True
    assert s.pii_retention_terminal_days == 365


def test_pii_retention_env_override(monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DISABLED", "0")
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    monkeypatch.setenv("PII_RETENTION_TERMINAL_DAYS", "180")
    from config.scheduler import SchedulerSettings
    s = SchedulerSettings()
    assert s.pii_retention_gc_disabled is False
    assert s.pii_retention_gc_dry_run is False
    assert s.pii_retention_terminal_days == 180
```

- [ ] **Step 2: 確認測試 fail**

```bash
pytest tests/test_config_scheduler.py::test_pii_retention_defaults tests/test_config_scheduler.py::test_pii_retention_env_override -v
```

Expected: FAIL with `AttributeError: 'SchedulerSettings' object has no attribute 'pii_retention_gc_disabled'`

- [ ] **Step 3: 加 fields 到 SchedulerSettings**

在 `config/scheduler.py` `SchedulerSettings` class 內最後一段加：

```python
    # PII Retention GC（spec 2026-05-22-parent-pii-retention-data-export-design.md）
    # 預設全 OFF + dry-run：上線後 user 手動 review log 才開正式抹
    pii_retention_gc_disabled: BoolEnv = True
    pii_retention_gc_dry_run: BoolEnv = True
    pii_retention_terminal_days: int = 365
```

- [ ] **Step 4: 確認測試通過**

```bash
pytest tests/test_config_scheduler.py::test_pii_retention_defaults tests/test_config_scheduler.py::test_pii_retention_env_override -v
```

Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add config/scheduler.py tests/test_config_scheduler.py
git commit -m "feat(config): add PII_RETENTION_GC_DISABLED/DRY_RUN/TERMINAL_DAYS env"
```

---

## Task 4: `set_lifecycle_status` Helper

**Files:**
- Create: `utils/student_lifecycle.py`
- Test: `tests/test_student_lifecycle_helper.py`

**Working dir:** BE worktree

- [ ] **Step 1: 寫測試**

```python
# tests/test_student_lifecycle_helper.py
"""set_lifecycle_status：原子化 lifecycle 變更 + terminal_entered_at + audit_log。"""

from datetime import datetime, timezone
import pytest

from models.classroom import (
    Student,
    LIFECYCLE_ACTIVE, LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED,
    LIFECYCLE_ON_LEAVE,
)
from models.audit import AuditLog
from utils.student_lifecycle import set_lifecycle_status


def _make_student(session, *, lifecycle=LIFECYCLE_ACTIVE, terminal_at=None):
    s = Student(
        name="測試",
        birth_date="2018-01-01",
        lifecycle_status=lifecycle,
        terminal_entered_at=terminal_at,
    )
    session.add(s)
    session.flush()
    return s


def test_set_active_to_graduated_sets_terminal_entered_at(db_session):
    s = _make_student(db_session, lifecycle=LIFECYCLE_ACTIVE)
    before = datetime.now(timezone.utc)
    set_lifecycle_status(db_session, s, LIFECYCLE_GRADUATED, actor_user_id=1)
    assert s.lifecycle_status == LIFECYCLE_GRADUATED
    assert s.terminal_entered_at is not None
    assert s.terminal_entered_at >= before


def test_set_graduated_back_to_active_clears_terminal_entered_at(db_session):
    s = _make_student(
        db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        terminal_at=datetime.now(timezone.utc),
    )
    set_lifecycle_status(db_session, s, LIFECYCLE_ACTIVE, actor_user_id=1)
    assert s.lifecycle_status == LIFECYCLE_ACTIVE
    assert s.terminal_entered_at is None


def test_set_terminal_to_terminal_keeps_timestamp(db_session):
    """從一個終態換到另一個終態，原戳記不動（避免 retention timer reset）。"""
    fixed = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = _make_student(db_session, lifecycle=LIFECYCLE_TRANSFERRED, terminal_at=fixed)
    set_lifecycle_status(db_session, s, LIFECYCLE_GRADUATED, actor_user_id=1)
    assert s.lifecycle_status == LIFECYCLE_GRADUATED
    assert s.terminal_entered_at == fixed


def test_same_status_no_op(db_session):
    s = _make_student(db_session, lifecycle=LIFECYCLE_ACTIVE)
    audit_count_before = db_session.query(AuditLog).count()
    set_lifecycle_status(db_session, s, LIFECYCLE_ACTIVE, actor_user_id=1)
    assert db_session.query(AuditLog).count() == audit_count_before


def test_audit_log_written(db_session):
    s = _make_student(db_session, lifecycle=LIFECYCLE_ACTIVE)
    set_lifecycle_status(db_session, s, LIFECYCLE_ON_LEAVE, actor_user_id=42, reason="家長申請")
    db_session.flush()

    log = (
        db_session.query(AuditLog)
        .filter(AuditLog.entity_type == "student", AuditLog.entity_id == str(s.id))
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert log is not None
    assert log.action == "UPDATE"
    assert log.user_id == 42
    assert "on_leave" in log.changes
    assert "家長申請" in log.changes


def test_audit_disabled_when_audit_false(db_session):
    s = _make_student(db_session, lifecycle=LIFECYCLE_ACTIVE)
    audit_count_before = db_session.query(AuditLog).count()
    set_lifecycle_status(db_session, s, LIFECYCLE_GRADUATED, actor_user_id=1, audit=False)
    db_session.flush()
    assert db_session.query(AuditLog).count() == audit_count_before
```

- [ ] **Step 2: 確認測試 fail**

```bash
pytest tests/test_student_lifecycle_helper.py -v
```

Expected: FAIL with `ImportError: cannot import name 'set_lifecycle_status'`

- [ ] **Step 3: 實作 helper**

```python
# utils/student_lifecycle.py
"""Student lifecycle 變更原子化 helper。

所有 `Student.lifecycle_status` 變更必須走 set_lifecycle_status，不可直接
.lifecycle_status =。理由：(1) 維護 terminal_entered_at 戳記給 PII retention
GC 算 365 天 (2) 統一寫 audit_log (3) 復學自動取消 retention timer。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from models.audit import AuditLog
from models.classroom import (
    Student,
    LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN,
)


_TERMINAL_LIFECYCLE = frozenset(
    {LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN}
)


def set_lifecycle_status(
    session,
    student: Student,
    new_status: str,
    *,
    actor_user_id: int | None = None,
    audit: bool = True,
    reason: str | None = None,
) -> None:
    """原子化變更 lifecycle_status + 維護 terminal_entered_at + 寫 audit_log。

    - 非終態 → 終態：terminal_entered_at = NOW(utc)
    - 終態 → 非終態（罕見復學）：terminal_entered_at = NULL（取消 retention）
    - 終態 → 終態 / 非終態 → 非終態：戳記不動
    - 同狀態：no-op（不寫 audit_log）
    """
    old_status = student.lifecycle_status
    if old_status == new_status:
        return

    was_terminal = old_status in _TERMINAL_LIFECYCLE
    is_terminal = new_status in _TERMINAL_LIFECYCLE

    student.lifecycle_status = new_status
    if not was_terminal and is_terminal:
        student.terminal_entered_at = datetime.now(timezone.utc)
    elif was_terminal and not is_terminal:
        student.terminal_entered_at = None

    if audit:
        session.add(AuditLog(
            user_id=actor_user_id,
            username="scheduler" if actor_user_id is None else None,
            action="UPDATE",
            entity_type="student",
            entity_id=str(student.id),
            summary=f"lifecycle: {old_status} → {new_status}",
            changes=json.dumps({
                "old_status": old_status,
                "new_status": new_status,
                "reason": reason,
            }, ensure_ascii=False),
            ip_address=None,
            created_at=datetime.now(),
        ))
```

- [ ] **Step 4: 確認測試通過**

```bash
pytest tests/test_student_lifecycle_helper.py -v
```

Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add utils/student_lifecycle.py tests/test_student_lifecycle_helper.py
git commit -m "feat(utils): set_lifecycle_status helper with terminal_entered_at maintenance"
```

---

## Task 5: Refactor lifecycle_status callers

**Files:**
- Modify: 約 3-5 處（grep 後逐處改）

**Working dir:** BE worktree

- [ ] **Step 1: Grep 所有直接寫 lifecycle_status 的 caller**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend
grep -rn "\.lifecycle_status\s*=" --include="*.py" . | grep -v "tests/" | grep -v ".claude/" | grep -v "test_student_lifecycle_helper.py"
```

預期會看到的位置（依代碼 grep 結果為準）：
- `services/graduation_scheduler.py`（自動畢業）
- `api/student_enrollment.py` 或 `api/students.py`（admin 改 lifecycle）
- `api/recruitment/*.py`（招生 funnel 進終態）

- [ ] **Step 2: 對每處 caller 改寫**

範例（`services/graduation_scheduler.py` 內）：

```python
# ❌ 改前
for student in graduates:
    student.lifecycle_status = LIFECYCLE_GRADUATED

# ✅ 改後
from utils.student_lifecycle import set_lifecycle_status

for student in graduates:
    set_lifecycle_status(
        session, student, LIFECYCLE_GRADUATED,
        actor_user_id=None,  # 自動排程
        reason="auto_graduation",
    )
```

對招生轉終態的 caller（recruitment 退場）：

```python
# api/recruitment/transitions.py（或實際檔名）
from utils.student_lifecycle import set_lifecycle_status

set_lifecycle_status(
    session, student, LIFECYCLE_WITHDRAWN,
    actor_user_id=current_user.user_id,
    reason="recruitment_revert",
)
```

- [ ] **Step 3: 全測試跑一遍確認沒打破現有 caller 的 audit 語意**

```bash
pytest tests/ -x -q --timeout=60
```

Expected: 全綠（注意：先前直接寫 `.lifecycle_status =` 的測試可能因 audit_log 多寫一筆而需更新）

- [ ] **Step 4: 補一條回歸測試確認 graduation_scheduler 透過 helper 寫戳記**

```python
# tests/test_graduation_scheduler.py 加（既有檔擴）
def test_auto_graduation_sets_terminal_entered_at(db_session):
    # 建畢業班 + 在學學生
    student = _make_student_in_graduation_grade(db_session)
    from services.graduation_scheduler import run_auto_graduation
    run_auto_graduation(effective_date=date.today())
    db_session.refresh(student)
    assert student.lifecycle_status == "graduated"
    assert student.terminal_entered_at is not None
```

- [ ] **Step 5: 跑回歸測試**

```bash
pytest tests/test_graduation_scheduler.py -v
```

Expected: PASS（含新測試）

- [ ] **Step 6: Commit**

```bash
git add services/ api/ tests/
git commit -m "refactor: route all Student.lifecycle_status writes through set_lifecycle_status"
```

---

## Task 6: PII Retention Scheduler

**Files:**
- Create: `services/pii_retention_scheduler.py`
- Test: `tests/test_pii_retention_gc.py`

**Working dir:** BE worktree

- [ ] **Step 1: 寫測試（先列關鍵 case）**

```python
# tests/test_pii_retention_gc.py
"""PII Retention GC 測試：retention 邊界、dry-run、SKIP LOCKED、idempotency。"""

from datetime import datetime, timedelta, timezone

import pytest

from models.audit import AuditLog
from models.classroom import (
    Student, LIFECYCLE_ACTIVE, LIFECYCLE_GRADUATED,
)
from models.guardian import Guardian
from services.pii_retention_scheduler import _run_pii_retention_gc


def _make_guardian_pair(session, *, lifecycle, days_ago, user_id=None,
                       pii_redacted=False):
    student = Student(
        name="畢業生",
        birth_date="2018-01-01",
        lifecycle_status=lifecycle,
        terminal_entered_at=(
            datetime.now(timezone.utc) - timedelta(days=days_ago)
            if lifecycle != LIFECYCLE_ACTIVE else None
        ),
    )
    session.add(student)
    session.flush()
    g = Guardian(
        student_id=student.id,
        user_id=user_id,
        name="王媽媽",
        phone="0912345678",
        email="mom@example.com",
        relation="母親",
        custody_note="探視週末",
        pii_redacted_at=(datetime.now(timezone.utc) if pii_redacted else None),
    )
    session.add(g)
    session.flush()
    return student, g


def test_gc_redacts_after_365_days(db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        db_session, lifecycle=LIFECYCLE_GRADUATED, days_ago=400, user_id=7,
    )
    db_session.commit()

    _run_pii_retention_gc()

    db_session.refresh(g)
    assert g.name == "[已離校家長]"
    assert g.phone is None
    assert g.email is None
    assert g.relation is None
    assert g.custody_note is None
    assert g.user_id is None
    assert g.pii_redacted_at is not None


def test_gc_skips_within_retention_window(db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        db_session, lifecycle=LIFECYCLE_GRADUATED, days_ago=300, user_id=7,
    )
    db_session.commit()

    _run_pii_retention_gc()

    db_session.refresh(g)
    assert g.name == "王媽媽"
    assert g.phone == "0912345678"
    assert g.pii_redacted_at is None


def test_gc_skips_active_students(db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        db_session, lifecycle=LIFECYCLE_ACTIVE, days_ago=400, user_id=7,
    )
    db_session.commit()

    _run_pii_retention_gc()

    db_session.refresh(g)
    assert g.phone == "0912345678"


def test_dry_run_does_not_modify(db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "1")
    student, g = _make_guardian_pair(
        db_session, lifecycle=LIFECYCLE_GRADUATED, days_ago=400, user_id=7,
    )
    db_session.commit()

    _run_pii_retention_gc()

    db_session.refresh(g)
    assert g.phone == "0912345678"
    assert g.pii_redacted_at is None


def test_gc_idempotent_skips_already_redacted(db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        db_session, lifecycle=LIFECYCLE_GRADUATED, days_ago=400,
        user_id=7, pii_redacted=True,
    )
    initial_redacted_at = g.pii_redacted_at
    db_session.commit()

    _run_pii_retention_gc()

    db_session.refresh(g)
    assert g.pii_redacted_at == initial_redacted_at


def test_gc_writes_audit_log(db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        db_session, lifecycle=LIFECYCLE_GRADUATED, days_ago=400, user_id=7,
    )
    db_session.commit()

    _run_pii_retention_gc()

    log = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.entity_type == "guardian",
            AuditLog.entity_id == str(g.id),
            AuditLog.username == "pii_retention_gc",
        )
        .first()
    )
    assert log is not None
    # 不含 PII
    assert "0912345678" not in (log.changes or "")
    assert "mom@example.com" not in (log.changes or "")
    assert "王媽媽" not in (log.changes or "")
    assert str(student.id) in log.changes


def test_gc_redact_unlinks_user_so_parent_portal_returns_empty(db_session, monkeypatch):
    """user_id 解綁後 _get_parent_student_ids 回空 list。"""
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        db_session, lifecycle=LIFECYCLE_GRADUATED, days_ago=400, user_id=99,
    )
    db_session.commit()

    _run_pii_retention_gc()

    from api.parent_portal._shared import _get_parent_student_ids
    db_session.refresh(g)
    guardian_ids, student_ids = _get_parent_student_ids(db_session, 99)
    assert guardian_ids == []
    assert student_ids == []
```

- [ ] **Step 2: 確認測試 fail**

```bash
pytest tests/test_pii_retention_gc.py -v
```

Expected: FAIL with `ImportError: cannot import name '_run_pii_retention_gc'`

- [ ] **Step 3: 實作 scheduler**

```python
# services/pii_retention_scheduler.py
"""PII Retention GC：定期清除已超過 retention 期的家長 PII。

驅動：個資法第 11 條「特定目的消失應主動刪除」。

- 對象：Guardian 表中 student 已進終態且 terminal_entered_at < NOW - 365 天
- 動作：抹 phone/email/relation/custody_note，name 改 '[已離校家長]'，user_id 解綁
- 不刪 Guardian row、不動 Student PII、不刪 User row
- ENV：PII_RETENTION_GC_DISABLED=1（關閉）/ PII_RETENTION_GC_DRY_RUN=1（只 log）
       / PII_RETENTION_TERMINAL_DAYS=365（可調）

設計選擇：開新檔不擴 security_gc_scheduler（PII GC 是日級且邏輯複雜）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from config import get_settings
from models.audit import AuditLog
from models.database import get_session

logger = logging.getLogger(__name__)

_GC_INTERVAL_SEC = 24 * 60 * 60  # 每日
_INITIAL_DELAY_SEC = 60
_BATCH_LIMIT = 500


def scheduler_enabled() -> bool:
    return not bool(get_settings().scheduler.pii_retention_gc_disabled)


def dry_run_enabled() -> bool:
    return bool(get_settings().scheduler.pii_retention_gc_dry_run)


def retention_days() -> int:
    return int(get_settings().scheduler.pii_retention_terminal_days or 365)


async def run_pii_retention_scheduler(stop_event: asyncio.Event) -> None:
    """主迴圈：每 24 小時跑一次 PII retention GC。"""
    logger.info(
        "pii_retention_scheduler started (dry_run=%s, days=%s)",
        dry_run_enabled(), retention_days(),
    )
    try:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_INITIAL_DELAY_SEC)
            return
        except asyncio.TimeoutError:
            pass

        while not stop_event.is_set():
            _run_pii_retention_gc()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_GC_INTERVAL_SEC)
            except asyncio.TimeoutError:
                continue
    finally:
        logger.info("pii_retention_scheduler stopped")


def _run_pii_retention_gc() -> None:
    """單次 GC：找到期 Guardian → 抹 PII → 寫 audit_log。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days())
    dry = dry_run_enabled()

    session = get_session()
    try:
        # SQLite 不支援 FOR UPDATE SKIP LOCKED；測試環境簡化
        dialect = session.bind.dialect.name
        lock_clause = "FOR UPDATE SKIP LOCKED" if dialect == "postgresql" else ""
        rows = session.execute(text(f"""
            SELECT g.id, g.student_id, s.lifecycle_status, s.terminal_entered_at
            FROM guardians g
            JOIN students s ON s.id = g.student_id
            WHERE s.lifecycle_status IN ('graduated', 'transferred', 'withdrawn')
              AND s.terminal_entered_at IS NOT NULL
              AND s.terminal_entered_at < :cutoff
              AND g.pii_redacted_at IS NULL
              AND g.deleted_at IS NULL
            ORDER BY g.id
            LIMIT :limit
            {lock_clause}
        """), {"cutoff": cutoff, "limit": _BATCH_LIMIT}).fetchall()

        if not rows:
            logger.info("pii_retention GC: 無到期 Guardian")
            return

        guardian_ids = [r[0] for r in rows]
        logger.info(
            "pii_retention GC: %s 筆%s",
            len(guardian_ids), " (dry-run)" if dry else "",
        )
        for r in rows:
            logger.info(
                "  - guardian_id=%s student_id=%s lifecycle=%s terminal_at=%s",
                r[0], r[1], r[2], r[3],
            )

        if dry:
            session.rollback()
            return

        # 抹 PII（單一 UPDATE atomic）
        session.execute(text("""
            UPDATE guardians
            SET name = '[已離校家長]',
                phone = NULL,
                email = NULL,
                relation = NULL,
                custody_note = NULL,
                user_id = NULL,
                pii_redacted_at = :now,
                updated_at = :now
            WHERE id IN :ids
        """).bindparams(
            __import__("sqlalchemy").bindparam("ids", expanding=True)
        ), {"ids": tuple(guardian_ids), "now": datetime.now(timezone.utc)})

        # 寫 audit_log（每筆一條，changes 不含 PII）
        days = retention_days()
        for r in rows:
            session.add(AuditLog(
                user_id=None,
                username="pii_retention_gc",
                action="UPDATE",
                entity_type="guardian",
                entity_id=str(r[0]),
                summary=f"PII retention redact (>{days}d after terminal)",
                changes=json.dumps({
                    "reason": f"retention_{days}d",
                    "student_id": r[1],
                    "lifecycle_status": r[2],
                }, ensure_ascii=False),
                ip_address=None,
                created_at=datetime.now(),
            ))

        session.commit()
        logger.info("pii_retention GC: 已抹 %s 筆 Guardian PII", len(guardian_ids))
    except Exception as e:
        logger.error("pii_retention GC 失敗: %s", e, exc_info=True)
        session.rollback()
    finally:
        session.close()
```

- [ ] **Step 4: 確認測試通過**

```bash
pytest tests/test_pii_retention_gc.py -v
```

Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add services/pii_retention_scheduler.py tests/test_pii_retention_gc.py
git commit -m "feat(scheduler): pii_retention_scheduler with dry-run + SKIP LOCKED"
```

---

## Task 7: Wire scheduler into `main.py`

**Files:**
- Modify: `main.py` 在 `security_gc_scheduler` 啟動段下方加 `pii_retention_scheduler`

**Working dir:** BE worktree

- [ ] **Step 1: 找 security_gc_scheduler 啟動段確定插入位置**

```bash
grep -n "security_gc_scheduler" /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend/main.py
```

預期：line 341-350 範圍。

- [ ] **Step 2: 在 security_gc 之後加 pii_retention 啟動 block**

在 `main.py` security_gc 啟動段（`security_gc_task = asyncio.create_task(...)` 那段 try/except 結尾）之後加：

```python
    # PII Retention GC（spec 2026-05-22-parent-pii-retention-data-export-design.md）
    # 預設 disabled + dry-run，user 手動 review log 後再啟用
    pii_retention_task = None
    pii_retention_stop_event: asyncio.Event | None = None
    try:
        from services import pii_retention_scheduler as _pii_gc

        if _pii_gc.scheduler_enabled():
            pii_retention_stop_event = asyncio.Event()
            pii_retention_task = asyncio.create_task(
                _pii_gc.run_pii_retention_scheduler(pii_retention_stop_event)
            )
    except Exception as e:
        logger.warning("PII retention GC 排程啟動失敗: %s", e)
        capture_exception(e, level="warning")
```

並在 shutdown 區段（search `security_gc_stop_event` 對應的 shutdown 段）加：

```python
    if pii_retention_stop_event is not None:
        pii_retention_stop_event.set()
    if pii_retention_task is not None:
        try:
            await asyncio.wait_for(pii_retention_task, timeout=5)
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("pii_retention_scheduler 停止異常: %s", e)
```

- [ ] **Step 3: 寫 smoke test 確認 main.py 不爆**

```python
# tests/test_main_starts_pii_scheduler.py
def test_main_imports_and_wires_pii_scheduler():
    """smoke：main 模組 import 不爆、pii_retention_scheduler 模組能找到。"""
    import importlib
    main_mod = importlib.import_module("main")
    from services import pii_retention_scheduler as pii
    assert hasattr(pii, "run_pii_retention_scheduler")
    assert hasattr(pii, "scheduler_enabled")
```

- [ ] **Step 4: 跑測試**

```bash
pytest tests/test_main_starts_pii_scheduler.py -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main_starts_pii_scheduler.py
git commit -m "feat(main): wire pii_retention_scheduler with graceful shutdown"
```

---

## Task 8: Data-Export Endpoint

**Files:**
- Create: `api/parent_portal/data_export.py`
- Test: `tests/test_parent_data_export.py`
- Modify: `api/parent_portal/__init__.py`

**Working dir:** BE worktree

- [ ] **Step 1: 寫測試**

```python
# tests/test_parent_data_export.py
"""家長 data-export endpoint：JSON shape / rate-limit / 413 / IDOR / redacted 家長空回應。"""

import json
import pytest

from models.guardian import Guardian
from models.classroom import Student


def test_export_returns_json_with_attachment_header(parent_client, parent_user, db_session):
    """正常路徑：回 JSON + Content-Disposition。"""
    student = Student(
        name="王大寶", birth_date="2018-03-15",
        lifecycle_status="active",
    )
    db_session.add(student)
    db_session.flush()
    db_session.add(Guardian(
        student_id=student.id, user_id=parent_user.id,
        name="王媽媽", phone="0911", relation="母親",
    ))
    db_session.commit()

    resp = parent_client.get("/api/parent/me/data-export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    assert "ivy_data_export" in resp.headers["content-disposition"]

    body = resp.json()
    assert body["exported_by_user_id"] == parent_user.id
    assert body["schema_version"] == 1
    assert len(body["students"]) == 1
    assert body["students"][0]["name"] == "王大寶"


def test_export_includes_terminal_students_within_retention(
    parent_client, parent_user, db_session,
):
    """已畢業但未過 retention 的 student 仍應出現在 export。"""
    from datetime import datetime, timezone, timedelta
    student = Student(
        name="畢業寶", birth_date="2017-03-15",
        lifecycle_status="graduated",
        terminal_entered_at=datetime.now(timezone.utc) - timedelta(days=100),
    )
    db_session.add(student)
    db_session.flush()
    db_session.add(Guardian(
        student_id=student.id, user_id=parent_user.id,
        name="畢業媽", phone="0922", relation="母親",
    ))
    db_session.commit()

    resp = parent_client.get("/api/parent/me/data-export")
    body = resp.json()
    names = [s["name"] for s in body["students"]]
    assert "畢業寶" in names


def test_export_rate_limit_429(parent_client, parent_user, db_session):
    """第二次 1 小時內呼叫 429。"""
    resp1 = parent_client.get("/api/parent/me/data-export")
    assert resp1.status_code == 200
    resp2 = parent_client.get("/api/parent/me/data-export")
    assert resp2.status_code == 429


def test_export_returns_empty_for_redacted_parent(
    parent_client, parent_user, db_session,
):
    """Guardian.user_id 被 PII GC 解綁的家長登入 → students 空 list。"""
    # 不建任何 Guardian
    resp = parent_client.get("/api/parent/me/data-export")
    assert resp.status_code == 200
    body = resp.json()
    assert body["students"] == []


def test_export_403_for_non_parent_role(admin_client):
    """admin role 走錯 endpoint → 403。"""
    resp = admin_client.get("/api/parent/me/data-export")
    assert resp.status_code == 403


def test_export_includes_all_modules(parent_client, parent_user, db_session):
    """students[].應該包含 contact_book/attendance/leaves/fees/medications/messages/photos/growth_reports keys。"""
    student = Student(
        name="完整寶", birth_date="2018-03-15",
        lifecycle_status="active",
    )
    db_session.add(student)
    db_session.flush()
    db_session.add(Guardian(
        student_id=student.id, user_id=parent_user.id,
        name="完整媽", phone="0933", relation="母親",
    ))
    db_session.commit()

    resp = parent_client.get("/api/parent/me/data-export")
    body = resp.json()
    s = body["students"][0]
    for key in [
        "contact_book", "attendance", "leaves", "fees",
        "medications", "messages", "photos", "growth_reports",
    ]:
        assert key in s, f"missing key: {key}"
        assert isinstance(s[key], list)
```

> 測試假設 conftest 已有 `parent_client` / `parent_user` / `admin_client` / `db_session` fixtures（既有 parent_portal 測試應該都用到，沿用）。若沒有，從 `tests/test_parent_portal_*.py` 找最近的 fixture pattern。

- [ ] **Step 2: 確認測試 fail**

```bash
pytest tests/test_parent_data_export.py -v
```

Expected: FAIL with 404（router 未註冊）

- [ ] **Step 3: 實作 endpoint**

```python
# api/parent_portal/data_export.py
"""家長端個資查閱權（個資法 §10）：GET /me/data-export。

回當前家長綁的所有 student 的全資料 JSON（contact_book/attendance/leaves/
fees/medications/photos/messages/growth_reports）。同步生成，rate-limit
1 次/小時/user，容量 50MB 上限。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from utils.auth import require_parent_role
from utils.rate_limit import create_limiter

from ._dependencies import get_parent_db
from ._shared import _get_parent_user, _get_parent_student_ids, resolve_parent_display_name

logger = logging.getLogger(__name__)

router = APIRouter(tags=["parent-data-export"])

_MAX_BYTES = 50 * 1024 * 1024  # 50MB

_export_limiter = create_limiter(
    max_calls=1,
    window_seconds=3600,
    name="parent_data_export",
    error_detail="每小時限下載 1 次，請稍後再試",
)


@router.get("/me/data-export")
def get_data_export(
    request: Request,
    current_user: dict = Depends(require_parent_role),
    session=Depends(get_parent_db),
):
    user = _get_parent_user(session, current_user)
    _export_limiter.check(f"user:{user.id}")

    guardian_ids, student_ids = _get_parent_student_ids(session, user.id)

    students_payload = []
    for sid in student_ids:
        students_payload.append(_collect_student_export(session, sid, user.id))

    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exported_by_user_id": user.id,
        "schema_version": 1,
        "parent": {
            "display_name": resolve_parent_display_name(session, user),
            "line_user_id": user.username if user.username.startswith("parent_line_") else None,
        },
        "students": students_payload,
    }

    body = json.dumps(payload, ensure_ascii=False, default=str)
    if len(body.encode("utf-8")) > _MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail="資料量超過 50MB，請聯絡園所協助匯出",
        )

    filename = f"ivy_data_export_{user.id}_{datetime.now().strftime('%Y%m%d')}.json"
    # 顯式 audit（GET 但讀 PII，須留稽核軌跡）
    from utils.audit import write_explicit_audit
    write_explicit_audit(
        request,
        action="READ",
        entity_type="parent_data_export",
        summary=f"家長下載個人資料 ({len(students_payload)} 學生)",
        entity_id=str(user.id),
        changes={"student_count": len(students_payload), "size_bytes": len(body)},
    )

    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _collect_student_export(session, student_id: int, user_id: int) -> dict:
    """收集單一 student 的所有可讀資料（與家長端各 router 回傳對齊）。"""
    from models.classroom import Student
    from models.guardian import Guardian

    student = session.query(Student).filter(Student.id == student_id).first()
    if student is None:
        return {}

    guardian = (
        session.query(Guardian)
        .filter(Guardian.student_id == student_id, Guardian.user_id == user_id,
                Guardian.deleted_at.is_(None))
        .first()
    )

    return {
        "id": student.id,
        "name": student.name,
        "birth_date": student.birth_date.isoformat() if student.birth_date else None,
        "lifecycle_status": student.lifecycle_status,
        "guardian_role": ({
            "name": guardian.name,
            "relation": guardian.relation,
            "is_primary": guardian.is_primary,
        } if guardian else None),
        "contact_book": _list_contact_book(session, student_id),
        "attendance": _list_attendance(session, student_id),
        "leaves": _list_leaves(session, student_id),
        "fees": _list_fees(session, student_id),
        "medications": _list_medications(session, student_id),
        "photos": _list_photos(session, student_id),
        "messages": _list_messages(session, student_id, user_id),
        "growth_reports": _list_growth_reports(session, student_id),
    }


# --- 子模組收集（每個 helper 對齊既有 parent_portal/<module>.py 的 query） ---

def _list_contact_book(session, student_id: int) -> list[dict]:
    from models.contact_book import ContactBookEntry
    rows = (
        session.query(ContactBookEntry)
        .filter(ContactBookEntry.student_id == student_id)
        .order_by(ContactBookEntry.entry_date.desc())
        .all()
    )
    return [{
        "id": r.id,
        "entry_date": r.entry_date.isoformat() if r.entry_date else None,
        "content": r.content,
        "teacher_name": r.teacher_name,
        "parent_acknowledged_at": (
            r.parent_acknowledged_at.isoformat() if r.parent_acknowledged_at else None
        ),
    } for r in rows]


def _list_attendance(session, student_id: int) -> list[dict]:
    from models.attendance import StudentAttendance
    rows = (
        session.query(StudentAttendance)
        .filter(StudentAttendance.student_id == student_id)
        .order_by(StudentAttendance.attendance_date.desc())
        .all()
    )
    return [{
        "date": r.attendance_date.isoformat() if r.attendance_date else None,
        "status": r.status,
        "check_in_time": r.check_in_time.isoformat() if r.check_in_time else None,
        "check_out_time": r.check_out_time.isoformat() if r.check_out_time else None,
    } for r in rows]


def _list_leaves(session, student_id: int) -> list[dict]:
    from models.leave_request import StudentLeaveRequest
    rows = (
        session.query(StudentLeaveRequest)
        .filter(StudentLeaveRequest.student_id == student_id)
        .order_by(StudentLeaveRequest.start_date.desc())
        .all()
    )
    return [{
        "id": r.id,
        "leave_type": r.leave_type,
        "start_date": r.start_date.isoformat() if r.start_date else None,
        "end_date": r.end_date.isoformat() if r.end_date else None,
        "reason": r.reason,
        "status": r.status,
    } for r in rows]


def _list_fees(session, student_id: int) -> list[dict]:
    from models.fee import FeeRecord
    rows = (
        session.query(FeeRecord)
        .filter(FeeRecord.student_id == student_id)
        .order_by(FeeRecord.billing_period.desc())
        .all()
    )
    return [{
        "id": r.id,
        "billing_period": r.billing_period,
        "amount": float(r.amount) if r.amount is not None else None,
        "status": r.status,
        "paid_at": r.paid_at.isoformat() if r.paid_at else None,
    } for r in rows]


def _list_medications(session, student_id: int) -> list[dict]:
    from models.medication import MedicationRequest
    rows = (
        session.query(MedicationRequest)
        .filter(MedicationRequest.student_id == student_id)
        .order_by(MedicationRequest.requested_at.desc())
        .all()
    )
    return [{
        "id": r.id,
        "drug_name": r.drug_name,
        "dosage": r.dosage,
        "requested_at": r.requested_at.isoformat() if r.requested_at else None,
        "status": r.status,
        "notes": r.notes,
    } for r in rows]


def _list_photos(session, student_id: int) -> list[dict]:
    from models.portfolio import StudentPhoto
    rows = (
        session.query(StudentPhoto)
        .filter(StudentPhoto.student_id == student_id)
        .order_by(StudentPhoto.taken_at.desc())
        .all()
    )
    return [{
        "id": r.id,
        "url": r.url,
        "caption": r.caption,
        "taken_at": r.taken_at.isoformat() if r.taken_at else None,
    } for r in rows]


def _list_messages(session, student_id: int, user_id: int) -> list[dict]:
    from models.message import Message
    rows = (
        session.query(Message)
        .filter(
            Message.student_id == student_id,
            (Message.sender_user_id == user_id) | (Message.recipient_user_id == user_id),
        )
        .order_by(Message.sent_at.desc())
        .all()
    )
    return [{
        "id": r.id,
        "sender_user_id": r.sender_user_id,
        "recipient_user_id": r.recipient_user_id,
        "subject": r.subject,
        "body": r.body,
        "sent_at": r.sent_at.isoformat() if r.sent_at else None,
    } for r in rows]


def _list_growth_reports(session, student_id: int) -> list[dict]:
    from models.portfolio import StudentGrowthReport
    rows = (
        session.query(StudentGrowthReport)
        .filter(StudentGrowthReport.student_id == student_id)
        .order_by(StudentGrowthReport.report_period.desc())
        .all()
    )
    return [{
        "id": r.id,
        "report_period": r.report_period,
        "content": r.content,
        "issued_at": r.issued_at.isoformat() if r.issued_at else None,
    } for r in rows]
```

> 上面 model import 路徑（如 `models.contact_book.ContactBookEntry`）為對齊既有 parent_portal/*.py 的 import 來推測。實作時先 grep `class.*Entry|class.*Record|class.*Request` 找實際 class 名再確認。若某 model 不存在或 column 名不同，**對齊既有 parent_portal/<module>.py 的 query 寫法**。

- [ ] **Step 4: Register router**

修改 `api/parent_portal/__init__.py` 加 import + include：

```python
# 在其他 from .xxx import xxx_router 那一區加：
from .data_export import router as data_export_router

# 在 parent_router.include_router(...) 那一區加（位置任意）：
parent_router.include_router(data_export_router)
```

- [ ] **Step 5: 跑測試**

```bash
pytest tests/test_parent_data_export.py -v
```

Expected: 6 PASS

> 若某 model import 失敗：先 grep `class.*<猜的名字>` 找真實名稱，調整 `_list_*` helper。**邏輯目標**：每個 student 7-8 個模組的歷史紀錄各回一個 list，欄位對齊既有家長端 router 的回傳即可。

- [ ] **Step 6: Commit**

```bash
git add api/parent_portal/data_export.py api/parent_portal/__init__.py tests/test_parent_data_export.py
git commit -m "feat(parent): GET /me/data-export endpoint for PDPA Art.10 access right"
```

---

## Task 9: 後端整體跑一遍

**Working dir:** BE worktree

- [ ] **Step 1: 全套 pytest**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend
pytest tests/ -x -q --timeout=120
```

Expected: 全綠（或只有 pre-existing fail 如 `test_audit_router` / `test_supabase_storage`，不是本 plan 引入）

- [ ] **Step 2: 驗 dev server 能起**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend
uvicorn main:app --reload --port 8089 &
sleep 5
curl -s http://localhost:8089/docs | grep -q "data-export" && echo "PASS" || echo "FAIL"
kill %1
```

Expected: `PASS`（OpenAPI 看得到 /me/data-export endpoint）

---

## Task 10: OpenAPI codegen → 前端 schema 更新

**Files:**
- 跨 repo

- [ ] **Step 1: 從 BE worktree dump OpenAPI**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend
python scripts/dump_openapi.py
ls -lh openapi.json
```

Expected: openapi.json 存在，size 比上次大一點（多了 /me/data-export）

- [ ] **Step 2: 把 openapi.json copy 到 FE worktree**

```bash
cp /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend/openapi.json \
   /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/parent-pii-retention-2026-05-22-frontend/openapi.json
```

- [ ] **Step 3: 在 FE worktree regen schema**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/parent-pii-retention-2026-05-22-frontend
npm run gen:api
git status -s src/api/_generated/
```

Expected: `src/api/_generated/schema.d.ts` modified

- [ ] **Step 4: 確認 /me/data-export 出現在 schema**

```bash
grep -A 3 "me/data-export" src/api/_generated/schema.d.ts | head -10
```

Expected: 出現 path entry 與對應 response shape

- [ ] **Step 5: Commit schema only（FE worktree）**

```bash
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regen schema.d.ts with /me/data-export"
```

> 別 commit `openapi.json`（被 .gitignore 擋）

---

## Task 11: 前端 `useDataExport` composable

**Files:**
- Create: `src/parent/composables/useDataExport.ts`
- Test: `src/parent/composables/__tests__/useDataExport.test.ts`

**Working dir:** FE worktree

- [ ] **Step 1: 寫測試**

```typescript
// src/parent/composables/__tests__/useDataExport.test.ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useDataExport } from '../useDataExport'

vi.mock('@/parent/api', () => ({
  apiParent: { get: vi.fn() },
}))

describe('useDataExport', () => {
  let apiParent: { get: ReturnType<typeof vi.fn> }

  beforeEach(async () => {
    apiParent = (await import('@/parent/api')).apiParent as any
    apiParent.get.mockReset()
    // jsdom global URL 與 a.click() stub
    ;(global as any).URL.createObjectURL = vi.fn(() => 'blob:fake')
    ;(global as any).URL.revokeObjectURL = vi.fn()
  })

  it('downloads blob and triggers anchor click', async () => {
    apiParent.get.mockResolvedValue({
      data: new Blob(['{"a":1}'], { type: 'application/json' }),
      headers: { 'content-disposition': 'attachment; filename="ivy_data_export_42_20260522.json"' },
    })

    const { downloading, downloadExport } = useDataExport()
    expect(downloading.value).toBe(false)
    await downloadExport()
    expect(apiParent.get).toHaveBeenCalledWith('/me/data-export', { responseType: 'blob' })
    expect((global as any).URL.createObjectURL).toHaveBeenCalled()
    expect(downloading.value).toBe(false)
  })

  it('returns rate-limited=true on 429', async () => {
    apiParent.get.mockRejectedValue({ response: { status: 429 } })
    const { downloadExport } = useDataExport()
    const result = await downloadExport()
    expect(result).toEqual({ ok: false, reason: 'rate_limited' })
  })

  it('returns too_large=true on 413', async () => {
    apiParent.get.mockRejectedValue({ response: { status: 413 } })
    const { downloadExport } = useDataExport()
    const result = await downloadExport()
    expect(result).toEqual({ ok: false, reason: 'too_large' })
  })

  it('rethrows other errors', async () => {
    apiParent.get.mockRejectedValue({ response: { status: 500 } })
    const { downloadExport } = useDataExport()
    await expect(downloadExport()).rejects.toBeTruthy()
  })
})
```

- [ ] **Step 2: 確認測試 fail**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/parent-pii-retention-2026-05-22-frontend
npx vitest run src/parent/composables/__tests__/useDataExport.test.ts
```

Expected: FAIL with "Cannot find module"

- [ ] **Step 3: 實作 composable**

```typescript
// src/parent/composables/useDataExport.ts
import { ref } from 'vue'
import { apiParent } from '@/parent/api'

export type ExportResult =
  | { ok: true }
  | { ok: false; reason: 'rate_limited' | 'too_large' }

export function useDataExport() {
  const downloading = ref(false)

  async function downloadExport(): Promise<ExportResult> {
    downloading.value = true
    try {
      const resp = await apiParent.get('/me/data-export', { responseType: 'blob' })
      const blob = resp.data as Blob
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      const cd = resp.headers?.['content-disposition'] as string | undefined
      const match = cd?.match(/filename="?([^";]+)"?/)
      a.download = match?.[1] ?? 'ivy_data_export.json'
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      return { ok: true }
    } catch (err: unknown) {
      const status = (err as { response?: { status?: number } })?.response?.status
      if (status === 429) return { ok: false, reason: 'rate_limited' }
      if (status === 413) return { ok: false, reason: 'too_large' }
      throw err
    } finally {
      downloading.value = false
    }
  }

  return { downloading, downloadExport }
}
```

- [ ] **Step 4: 跑測試**

```bash
npx vitest run src/parent/composables/__tests__/useDataExport.test.ts
```

Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/parent/composables/useDataExport.ts src/parent/composables/__tests__/useDataExport.test.ts
git commit -m "feat(parent): useDataExport composable for PDPA data export"
```

---

## Task 12: 前端 `MeView.vue` 加 export UI

**Files:**
- Modify: `src/parent/views/MeView.vue`
- Test: `src/parent/views/__tests__/MeView.test.ts`（擴）

**Working dir:** FE worktree

- [ ] **Step 1: Locate MeView 結構**

```bash
cat src/parent/views/MeView.vue | head -80
```

確認位置（NotificationPrefs row 在哪），找到合適的插入點。

- [ ] **Step 2: 寫測試（先擴既有 MeView.test.ts）**

```typescript
// src/parent/views/__tests__/MeView.test.ts 加：
import { mount, flushPromises } from '@vue/test-utils'
import { describe, it, expect, vi } from 'vitest'
import MeView from '../MeView.vue'

vi.mock('@/parent/composables/useDataExport', () => ({
  useDataExport: () => ({
    downloading: { value: false },
    downloadExport: vi.fn().mockResolvedValue({ ok: true }),
  }),
}))

describe('MeView data export', () => {
  it('renders 下載我的個人資料 button', () => {
    const w = mount(MeView, { global: { /* 既有 stubs */ } })
    expect(w.text()).toContain('下載我的個人資料')
  })

  it('opens dialog on button click', async () => {
    const w = mount(MeView, { global: { /* 既有 stubs */ } })
    await w.find('[data-testid="open-export-dialog"]').trigger('click')
    expect(w.text()).toContain('每小時限下載 1 次')
  })

  it('calls downloadExport on confirm', async () => {
    const downloadExport = vi.fn().mockResolvedValue({ ok: true })
    vi.doMock('@/parent/composables/useDataExport', () => ({
      useDataExport: () => ({ downloading: { value: false }, downloadExport }),
    }))
    const fresh = await import('../MeView.vue')
    const w = mount(fresh.default, { global: { /* 既有 stubs */ } })
    await w.find('[data-testid="open-export-dialog"]').trigger('click')
    await w.find('[data-testid="confirm-export"]').trigger('click')
    await flushPromises()
    expect(downloadExport).toHaveBeenCalled()
  })
})
```

- [ ] **Step 3: 確認測試 fail**

```bash
npx vitest run src/parent/views/__tests__/MeView.test.ts -t "data export"
```

Expected: FAIL with "Cannot find element"

- [ ] **Step 4: 修改 `MeView.vue`**

在 NotificationPrefs row 下方加：

```vue
<!-- 加入 template 段 -->
<ContactBookCard>
  <button
    class="row-button"
    data-testid="open-export-dialog"
    @click="showExportDialog = true"
  >
    <span class="row-label">下載我的個人資料</span>
    <span class="row-chevron">›</span>
  </button>
</ContactBookCard>

<AppModal v-model="showExportDialog" title="下載個人資料">
  <div class="export-info">
    <p>將下載您與孩子在園所的所有紀錄（JSON 格式）。</p>
    <ul class="muted small">
      <li>包含聯絡簿、出席、請假、繳費、投藥、相片連結、訊息、成長報告</li>
      <li>每小時限下載 1 次</li>
      <li>檔案上限 50MB</li>
    </ul>
    <p v-if="exportError === 'rate_limited'" class="error">
      請於稍後再試（每小時限 1 次）
    </p>
    <p v-if="exportError === 'too_large'" class="error">
      資料量超過 50MB，請聯絡園所協助匯出
    </p>
  </div>
  <template #actions>
    <button
      data-testid="confirm-export"
      :disabled="downloading"
      @click="handleExport"
    >
      {{ downloading ? '下載中…' : '確認下載' }}
    </button>
  </template>
</AppModal>
```

`<script setup lang="ts">` 段加：

```typescript
import { ref } from 'vue'
import { useDataExport } from '@/parent/composables/useDataExport'
// ...既有 import...

const showExportDialog = ref(false)
const exportError = ref<'rate_limited' | 'too_large' | null>(null)
const { downloading, downloadExport } = useDataExport()

async function handleExport() {
  exportError.value = null
  try {
    const result = await downloadExport()
    if (result.ok) {
      showExportDialog.value = false
    } else {
      exportError.value = result.reason
    }
  } catch {
    exportError.value = null
    // 既有錯誤已由 axios interceptor displayMessage 處理
  }
}
```

- [ ] **Step 5: 跑測試**

```bash
npx vitest run src/parent/views/__tests__/MeView.test.ts
```

Expected: 全 PASS

- [ ] **Step 6: 跑型別檢查**

```bash
npm run typecheck
```

Expected: 0 errors

- [ ] **Step 7: Commit**

```bash
git add src/parent/views/MeView.vue src/parent/views/__tests__/MeView.test.ts
git commit -m "feat(parent): MeView download personal data button + dialog"
```

---

## Task 13: CLAUDE.md 更新

**Files:**
- Modify: `ivy-backend/CLAUDE.md`（BE worktree）
- Modify: `CLAUDE.md`（workspace 根，**不在 worktree 內**，需切回主目錄）

**Working dir:** 兩處分別改

- [ ] **Step 1: 改 ivy-backend/CLAUDE.md**

在某個合適位置（建議「Schedulers」章節之後或新增「PII Retention 政策」章節）：

```markdown
## PII Retention 政策（spec 2026-05-22-parent-pii-retention-data-export-design.md）

- **觸發**：學生 `lifecycle_status` 進入終態（graduated/transferred/withdrawn）寫 `terminal_entered_at` 戳記
- **Retention 期**：預設 365 天（ENV `PII_RETENTION_TERMINAL_DAYS` 可調）
- **抹除範圍**：Guardian.phone/email/relation/custody_note 設 NULL、name 改 `[已離校家長]`、user_id 解綁；不刪 Guardian row、不動 Student PII、不刪 User row
- **復學自動取消**：`set_lifecycle_status` 從終態回非終態時 `terminal_entered_at=NULL`
- **GC scheduler**：`services/pii_retention_scheduler.py` 每日跑；ENV `PII_RETENTION_GC_DISABLED=1` 關閉、`PII_RETENTION_GC_DRY_RUN=1` 只 log 不寫
- **上線啟用流程**：dry-run 看 log → 人工確認清單 → 改 `PII_RETENTION_GC_DRY_RUN=0` 正式抹
- **資料查閱權**（個資法 §10）：`GET /api/parent/me/data-export`，rate-limit 1/hr/user、50MB 上限
- **lifecycle 變更規則**：所有 `Student.lifecycle_status` 變更**必經** `utils/student_lifecycle.set_lifecycle_status`，不可直接 `.lifecycle_status =`
```

- [ ] **Step 2: 改 workspace `CLAUDE.md`**

切到主目錄（不是 worktree）：

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
```

在「跨端常見陷阱」或合適位置加一條：

```markdown
9. **家長端 PII Retention（2026-05-22 起）**：學生進入終態 365 天後家長端 PII（Guardian.phone/email/name/user_id）會被 GC scheduler 抹除。LIFF login 仍能登入但 `_get_parent_student_ids` 回空 → portal 空白。data-export endpoint `GET /api/parent/me/data-export` 滿足個資法 §10 查閱權。所有 `Student.lifecycle_status` 變更必經 `utils/student_lifecycle.set_lifecycle_status`。
```

> 注意：workspace CLAUDE.md 不在 worktree 內，這次修改不會跟 BE/FE commit 一起；user 後續在 workspace 主目錄手動 git add / commit（**workspace 不是 git repo**，依 CLAUDE.md 描述「workspace 內的 CLAUDE.md 直接 edit save 即可，無需 commit」）。

- [ ] **Step 3: Commit BE CLAUDE.md**（FE 不動 CLAUDE.md）

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend
git add CLAUDE.md
git commit -m "docs(claude): PII retention policy + lifecycle helper rule"
```

---

## Task 14: 整合驗證

**Working dir:** workspace root

- [ ] **Step 1: 啟兩個 dev server**

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
./start.sh &
sleep 8
```

> 注意：start.sh 是用兩個 repo 主目錄（不是 worktree）。要驗證 worktree 內變更：BE worktree 內 `uvicorn main:app --reload --port 8089`、FE worktree 內 `npm run dev -- --port 5174`，並改 FE worktree 的 `.env.development` `VITE_API_BASE_URL=http://localhost:8089/api`。

- [ ] **Step 2: 用真實家長帳號驗 export endpoint**

開瀏覽器到 `http://localhost:5174/parent`（或 LIFF dev mock url），登入既有家長測試帳號 → Me 頁 → 點「下載我的個人資料」→ 確認 dialog 出現 → 確認下載按鈕觸發 download → 檢查 JSON 內容 keys / 比對學生數正確 / 第二次點立刻 429

- [ ] **Step 3: GC dry-run 驗證**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend
# 暫時關 GC sleep，立即跑一次
PII_RETENTION_GC_DISABLED=0 PII_RETENTION_GC_DRY_RUN=1 python -c "
from services.pii_retention_scheduler import _run_pii_retention_gc
_run_pii_retention_gc()
"
```

Expected: log 列出可能要 redact 的 Guardian 清單（dry-run 不寫 DB）

```bash
# 確認 DB 沒被改
psql ivymanagement -c "SELECT COUNT(*) FROM guardians WHERE pii_redacted_at IS NOT NULL;"
```

Expected: `0`

- [ ] **Step 4: 關閉 dev server**

```bash
kill %1
```

---

## Task 15: PR 準備

**Working dir:** 兩個 worktree 分別處理

- [ ] **Step 1: BE worktree push**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/parent-pii-retention-2026-05-22-backend
git log --oneline origin/main..HEAD
git push -u origin feat/parent-pii-retention-2026-05-22-backend
```

預期 commit 數：約 8-10 個

- [ ] **Step 2: FE worktree push**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend/.claude/worktrees/parent-pii-retention-2026-05-22-frontend
git log --oneline origin/main..HEAD
git push -u origin feat/parent-pii-retention-2026-05-22-frontend
```

預期 commit 數：約 3-4 個

- [ ] **Step 3: 開 PR（user 手動）**

User 進 GitHub 兩 repo 開 PR。**互相 link** 在 description。

- [ ] **Step 4: 告知 user 後續部署步驟**

依 spec §11：
1. BE merge → `alembic upgrade heads` 在 prod
2. FE merge
3. ENV `PII_RETENTION_GC_DRY_RUN=1`、`PII_RETENTION_GC_DISABLED=0` 啟用 dry-run
4. 看 log 一週確認沒誤判
5. `PII_RETENTION_GC_DRY_RUN=0` 正式啟用

---

## Self-Review（plan 自查）

**1. Spec coverage**
- §3 架構 → Task 4, 6, 8, 11, 12
- §4 Schema migration → Task 2
- §5 Lifecycle helper → Task 4 + caller refactor Task 5
- §6 GC scheduler → Task 6 + main.py wire Task 7
- §7 Data-export endpoint → Task 8
- §8 前端 UI → Task 11, 12
- §9 測試覆蓋 → 各 task 內 TDD step
- §10 CLAUDE.md → Task 13
- §11 部署順序 → Task 14, 15

✅ All spec sections covered.

**2. Placeholder scan**
- No "TBD/TODO/implement later" in code blocks
- `_collect_student_export` 子 helper 的 model 路徑用「對齊既有 parent_portal/<module>.py 的 query」這個指引——這是執行階段必須 grep 出 actual model 名的指示，但避免我在 plan 寫死可能不存在的 import；給了 reviewer 明確的回退規則

**3. Type consistency**
- `set_lifecycle_status(session, student, new_status, *, actor_user_id, audit, reason)` 簽章在 Task 4 定義，Task 5 caller 用一致
- `useDataExport` return type `{ downloading, downloadExport }` Task 11 定義，Task 12 用一致
- `ExportResult` union `{ ok: true } | { ok: false; reason: 'rate_limited' | 'too_large' }` Task 11 定義，Task 12 `exportError` ref 對齊

✅ 一致。

---

## Execution choice

Plan complete and saved to `ivy-backend/docs/superpowers/plans/2026-05-22-parent-pii-retention-data-export.md`.

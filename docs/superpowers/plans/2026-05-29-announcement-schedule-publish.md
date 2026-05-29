# PR #1：公告排程發佈 + 到期下架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 公告加 `publish_at` / `expires_at`，到時自動上下架；publish_at 到達時 scheduler 自動推播家長 LINE。

**Architecture:** 兩個 nullable timestamp column（NULL 即立即發佈/永久）；可見性 helper 純函式被 3 端 visible_filter 共用；scheduler 沿用 `leave_quota_expiry_scheduler.py` asyncio polling pattern，每 60 秒掃 publish_at 命中區間的公告呼叫既有 `_fire_announcement_push`。員工端不發 LINE。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、Vue 3 (`<script setup lang="ts">`) + Element Plus、pytest + vitest。

**Spec:** `docs/superpowers/specs/2026-05-29-announcement-improvements-design.md` §「PR #1」

**前置確認**：實作前先跑 `alembic heads`，確認當前 head（spec 撰寫時為 `eb0d4cf88f26`）；若已變動，alembic migration 的 `down_revision` 改為新 head。

---

## 檔案結構

**Create:**
- `alembic/versions/annsched01_announcement_schedule_publish.py` — 加 publish_at / expires_at
- `services/announcements/__init__.py` — 空 package marker
- `services/announcements/visibility.py` — `visibility_time_predicate()` + `derive_status()` 純函式
- `services/announcement_publish_scheduler.py` — asyncio polling + tick 邏輯
- `tests/services/announcements/test_visibility.py` — helper 純函式測試
- `tests/services/test_announcement_publish_scheduler.py` — scheduler tick 測試
- `tests/api/test_announcements_schedule.py` — admin endpoint 排程欄位測試

**Modify:**
- `models/event.py:153-182` — Announcement 加 publish_at / expires_at column
- `config/scheduler.py` — 加 `announcement_publish_scheduler_enabled` / `announcement_publish_check_interval`
- `api/announcements.py` — list/create/update 加排程欄位；create flow guard
- `api/portal/announcements.py` — visible_filter + mark_read 套 time predicate；ETag formula 加 max publish_at/expires_at
- `api/parent_portal/announcements.py` — visible_subquery + mark_read 套 time predicate
- `schemas/announcements.py` — list/create/update response/request schema 加欄位
- `main.py:355` 附近 — 註冊新 scheduler
- `tests/api/test_announcements.py` — 既有測試補 publish_at/expires_at default behavior
- `tests/api/test_portal_announcements.py` — 補 time predicate case
- `tests/api/test_parent_announcements.py` — 補 time predicate case

**前端 Modify:**
- `ivy-frontend/src/api/_generated/schema.d.ts` — `npm run gen:api` 自動 regen
- `ivy-frontend/src/views/AnnouncementView.vue` — form 加 datetime picker / table 加 status column
- `ivy-frontend/tests/unit/views/AnnouncementView.test.js`（或 `.ts`，依現況）— vitest

---

## Task 1: Schema migration

**Files:**
- Create: `alembic/versions/annsched01_announcement_schedule_publish.py`

- [ ] **Step 1: 寫 migration**

```python
"""announcements: add publish_at and expires_at columns

Revision ID: annsched01
Revises: eb0d4cf88f26
Create Date: 2026-05-29
"""

from alembic import op
import sqlalchemy as sa

revision = "annsched01"
down_revision = "eb0d4cf88f26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "announcements",
        sa.Column("publish_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "announcements",
        sa.Column("expires_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_announcements_publish_at",
        "announcements",
        ["publish_at"],
    )
    op.create_index(
        "ix_announcements_expires_at",
        "announcements",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_announcements_expires_at", table_name="announcements")
    op.drop_index("ix_announcements_publish_at", table_name="announcements")
    op.drop_column("announcements", "expires_at")
    op.drop_column("announcements", "publish_at")
```

- [ ] **Step 2: 驗 single head**

Run: `cd ivy-backend && alembic heads`
Expected: 單一 head `annsched01`（若回多 head 表示 down_revision 抓錯，改為當下實際 head）

- [ ] **Step 3: 跑 upgrade 與 downgrade 雙向**

Run:
```bash
cd ivy-backend && alembic upgrade head && alembic downgrade -1 && alembic upgrade head
```
Expected: 全綠，無錯誤。

- [ ] **Step 4: Commit**

```bash
cd ivy-backend && git add alembic/versions/annsched01_announcement_schedule_publish.py
git commit -m "feat(db): add publish_at / expires_at to announcements (annsched01)"
```

---

## Task 2: Announcement model 加 column

**Files:**
- Modify: `models/event.py:162-174`

- [ ] **Step 1: Read 既有 Announcement model**

Read `models/event.py:153-182`，確認當前 column 順序。

- [ ] **Step 2: 加兩個 column（插在 `is_pinned` 之後）**

`models/event.py` `Announcement` class 在 `created_by` column **之前**插入：

```python
    publish_at = Column(
        DateTime,
        nullable=True,
        comment="排程發佈時間；NULL 視為立即發佈",
    )
    expires_at = Column(
        DateTime,
        nullable=True,
        comment="到期下架時間；NULL 視為永久",
    )
```

- [ ] **Step 3: 跑 sanity import test**

Run: `cd ivy-backend && python3 -c "from models.database import Announcement; print(Announcement.publish_at, Announcement.expires_at)"`
Expected: 印出兩個 `InstrumentedAttribute`，無錯。

- [ ] **Step 4: Commit**

```bash
git add models/event.py
git commit -m "feat(model): Announcement.publish_at / expires_at"
```

---

## Task 3: Visibility helper 純函式

**Files:**
- Create: `services/announcements/__init__.py`
- Create: `services/announcements/visibility.py`
- Create: `tests/services/announcements/__init__.py`
- Create: `tests/services/announcements/test_visibility.py`

- [ ] **Step 1: 寫失敗的測試**

Create `tests/services/announcements/__init__.py`（空檔）。

Create `tests/services/announcements/test_visibility.py`：

```python
"""Tests for services.announcements.visibility helpers."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import and_, or_

from models.database import Announcement
from services.announcements.visibility import (
    derive_status,
    visibility_time_predicate,
)


def test_visibility_predicate_returns_sqlalchemy_clause():
    """Predicate must compose with SQLAlchemy filter()."""
    now = datetime(2026, 5, 29, 8, 0, 0)
    pred = visibility_time_predicate(now)
    # Smoke: 能放進 filter() 不拋例外，且 generative SQL 含 now timestamp
    compiled = str(pred.compile(compile_kwargs={"literal_binds": True}))
    assert "publish_at" in compiled
    assert "expires_at" in compiled
    assert "2026-05-29" in compiled


@pytest.mark.parametrize(
    "publish_at_delta,expires_at_delta,expected",
    [
        (None, None, "active"),
        (-timedelta(hours=1), None, "active"),
        (timedelta(hours=1), None, "scheduled"),
        (None, timedelta(hours=1), "active"),
        (None, -timedelta(hours=1), "expired"),
        (-timedelta(hours=2), -timedelta(hours=1), "expired"),
        (timedelta(hours=1), timedelta(hours=2), "scheduled"),
    ],
)
def test_derive_status_combinations(publish_at_delta, expires_at_delta, expected):
    now = datetime(2026, 5, 29, 8, 0, 0)
    ann = Announcement(
        title="T",
        content="C",
        created_by=1,
        publish_at=(now + publish_at_delta) if publish_at_delta is not None else None,
        expires_at=(now + expires_at_delta) if expires_at_delta is not None else None,
    )
    assert derive_status(ann, now) == expected
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/services/announcements/test_visibility.py -v`
Expected: FAIL with `ModuleNotFoundError: services.announcements.visibility`

- [ ] **Step 3: 寫 helper 實作**

Create `services/announcements/__init__.py`（空檔）。

Create `services/announcements/visibility.py`：

```python
"""Announcement visibility helpers — time-window predicate + derived status."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_

from models.database import Announcement


def visibility_time_predicate(now: datetime):
    """SQL filter clause: publish_at 已到 AND 尚未 expires_at。

    NULL 語意：
    - publish_at IS NULL → 立即發佈
    - expires_at IS NULL → 永不過期
    """
    return and_(
        or_(Announcement.publish_at.is_(None), Announcement.publish_at <= now),
        or_(Announcement.expires_at.is_(None), Announcement.expires_at > now),
    )


def derive_status(ann: Announcement, now: datetime) -> str:
    """Derived status for admin UI: scheduled / active / expired."""
    if ann.publish_at is not None and ann.publish_at > now:
        return "scheduled"
    if ann.expires_at is not None and ann.expires_at <= now:
        return "expired"
    return "active"
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/services/announcements/test_visibility.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add services/announcements/ tests/services/announcements/
git commit -m "feat(announcements): visibility_time_predicate + derive_status helpers"
```

---

## Task 4: Scheduler config

**Files:**
- Modify: `config/scheduler.py:75-77` 附近

- [ ] **Step 1: 加 setting**

`config/scheduler.py` 在 `# Leave quota expiry` 區塊**之後**新增：

```python
    # Announcement publish scheduler（spec 2026-05-29-announcement-improvements-design.md）
    announcement_publish_scheduler_enabled: BoolEnv = True
    announcement_publish_check_interval: int = 60  # 60 秒輪詢，足以讓「8:00 排程」最遲 8:01 推播
```

- [ ] **Step 2: 跑 sanity test**

Run: `cd ivy-backend && python3 -c "from config import get_settings; s = get_settings(); print(s.scheduler.announcement_publish_scheduler_enabled, s.scheduler.announcement_publish_check_interval)"`
Expected: `True 60`

- [ ] **Step 3: Commit**

```bash
git add config/scheduler.py
git commit -m "feat(config): announcement_publish_scheduler_enabled / check_interval"
```

---

## Task 5: Scheduler tick 純函式 + 測試

**Files:**
- Create: `services/announcement_publish_scheduler.py`
- Create: `tests/services/test_announcement_publish_scheduler.py`

- [ ] **Step 1: 寫失敗的測試**

Create `tests/services/test_announcement_publish_scheduler.py`：

```python
"""Tests for services.announcement_publish_scheduler.tick."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    Employee,
    get_session,
)
from services.announcement_publish_scheduler import tick
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def admin_emp(db_session):
    emp = Employee(name="admin", email="admin@test.local", hire_date=None)
    db_session.add(emp)
    db_session.commit()
    return emp


def _mk_ann(session, emp_id, publish_at, has_parent=True):
    a = Announcement(
        title="T", content="C", created_by=emp_id, publish_at=publish_at
    )
    session.add(a)
    session.flush()
    if has_parent:
        session.add(
            AnnouncementParentRecipient(announcement_id=a.id, scope="all")
        )
        session.flush()
    return a


def test_tick_dispatches_window_only(db_session, admin_emp):
    """Only announcements with publish_at in (last_dispatched_at, now] should fire."""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)
    # in window
    a_in = _mk_ann(db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
    # before window (already dispatched)
    a_before = _mk_ann(db_session, admin_emp.id, datetime(2026, 5, 29, 7, 58, 0))
    # future (still scheduled)
    a_future = _mk_ann(db_session, admin_emp.id, datetime(2026, 5, 29, 8, 1, 0))
    # no parent recipients → skip
    a_no_parent = _mk_ann(
        db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 45), has_parent=False
    )
    db_session.commit()

    with patch(
        "services.announcement_publish_scheduler._fire_announcement_push"
    ) as mock_fire:
        count = tick(db_session, now=now, last_dispatched_at=last)

    assert count == 1
    assert mock_fire.call_count == 1
    fired_ann = mock_fire.call_args.args[1]
    assert fired_ann.id == a_in.id


def test_tick_idempotent_within_same_window(db_session, admin_emp):
    """Re-running tick with same (now, last_dispatched_at) must not double-dispatch."""
    now = datetime(2026, 5, 29, 8, 0, 0)
    last = datetime(2026, 5, 29, 7, 59, 0)
    _mk_ann(db_session, admin_emp.id, datetime(2026, 5, 29, 7, 59, 30))
    db_session.commit()

    with patch(
        "services.announcement_publish_scheduler._fire_announcement_push"
    ) as mock_fire:
        c1 = tick(db_session, now=now, last_dispatched_at=last)
        # Caller advances last_dispatched_at = now; second tick same now → empty
        c2 = tick(db_session, now=now, last_dispatched_at=now)

    assert c1 == 1
    assert c2 == 0
    assert mock_fire.call_count == 1
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/services/test_announcement_publish_scheduler.py -v`
Expected: `ModuleNotFoundError: services.announcement_publish_scheduler`

- [ ] **Step 3: 寫 tick 實作**

Create `services/announcement_publish_scheduler.py`：

```python
"""services/announcement_publish_scheduler.py — 排程公告自動推播家長 LINE。

Pattern 對齊 services/leave_quota_expiry_scheduler.py asyncio polling。

- Tick 純函式 tick(session, now, last_dispatched_at) 易測
- run_announcement_publish_scheduler(stop_event) 為 wiring 入口
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import and_, exists

from config import get_settings
from models.database import (
    Announcement,
    AnnouncementParentRecipient,
)
from utils.scheduler_observability import scheduler_iteration

# 既有家長推播 helper（不重新定義，遵守 DRY）
from api.announcements import _fire_announcement_push

logger = logging.getLogger(__name__)


def scheduler_enabled() -> bool:
    return bool(get_settings().scheduler.announcement_publish_scheduler_enabled)


def tick(session, now: datetime, last_dispatched_at: datetime) -> int:
    """掃 publish_at in (last_dispatched_at, now] 且有家長 recipients 的公告，逐筆推播。

    Returns dispatched announcement count.
    Caller 負責持久化 last_dispatched_at = now 給下一次 tick。
    """
    parent_exists = exists().where(
        AnnouncementParentRecipient.announcement_id == Announcement.id
    )
    rows = (
        session.query(Announcement)
        .filter(
            and_(
                Announcement.publish_at.isnot(None),
                Announcement.publish_at > last_dispatched_at,
                Announcement.publish_at <= now,
                parent_exists,
            )
        )
        .all()
    )
    if not rows:
        return 0

    for ann in rows:
        recipients = (
            session.query(AnnouncementParentRecipient)
            .filter(AnnouncementParentRecipient.announcement_id == ann.id)
            .all()
        )
        try:
            _fire_announcement_push(
                session,
                ann,
                recipients,
                sender_user_id=None,  # 系統觸發；audit 走 sender=None
            )
        except Exception:
            logger.exception(
                "announcement publish scheduler 對 announcement_id=%s push 失敗",
                ann.id,
            )

    session.commit()
    logger.info(
        "announcement publish tick: dispatched=%d (last=%s now=%s)",
        len(rows),
        last_dispatched_at.isoformat(),
        now.isoformat(),
    )
    return len(rows)


async def run_announcement_publish_scheduler(stop_event: asyncio.Event) -> None:
    """Main loop: 每 check_interval 秒跑一次 tick，持久化 last_dispatched_at。"""
    from models.base import session_scope
    from utils.taipei_time import now_taipei_naive

    check_interval = get_settings().scheduler.announcement_publish_check_interval
    logger.info(
        "announcement publish scheduler 啟動 (interval=%ss)", check_interval
    )

    # 初始化 last_dispatched_at = process start time（避免啟動瞬間補發舊公告）
    last_dispatched_at = now_taipei_naive()

    while not stop_event.is_set():
        with scheduler_iteration(
            "announcement_publish",
            expected_interval_seconds=check_interval,
        ):
            now = now_taipei_naive()
            try:
                with session_scope() as session:
                    tick(session, now=now, last_dispatched_at=last_dispatched_at)
                last_dispatched_at = now
            except Exception:
                logger.exception("announcement publish scheduler tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            pass
```

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/services/test_announcement_publish_scheduler.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add services/announcement_publish_scheduler.py tests/services/test_announcement_publish_scheduler.py
git commit -m "feat(announcements): publish scheduler tick + asyncio polling loop"
```

---

## Task 6: Scheduler 註冊到 main.py

**Files:**
- Modify: `main.py` (around line 373 after leave_quota_expiry registration)

- [ ] **Step 1: Read 既有 leave_quota_expiry 註冊段**

Read `main.py:356-373`，仿照 pattern 加新 scheduler 註冊。

- [ ] **Step 2: 加註冊段**

在 leave_quota_expiry 註冊段（約 line 373）之後加：

```python
    # Announcement publish scheduler（排程發佈到時自動推播家長 LINE）
    announcement_publish_task = None
    announcement_publish_stop_event: asyncio.Event | None = None
    try:
        from services import announcement_publish_scheduler as _ann_pub_sched

        if _ann_pub_sched.scheduler_enabled():
            announcement_publish_stop_event = asyncio.Event()
            announcement_publish_task = asyncio.create_task(
                _ann_pub_sched.run_announcement_publish_scheduler(
                    announcement_publish_stop_event
                )
            )
            logger.info("announcement publish scheduler 已啟用")
    except Exception as e:
        logger.warning("公告排程發佈 scheduler 啟動失敗: %s", e)
        capture_exception(e, level="warning")
```

- [ ] **Step 3: 找 shutdown 區塊加 stop event**

在 main.py shutdown 階段 leave_quota_expiry 對應 stop event 之後加：

```python
    if announcement_publish_stop_event:
        announcement_publish_stop_event.set()
    if announcement_publish_task:
        try:
            await asyncio.wait_for(announcement_publish_task, timeout=5.0)
        except asyncio.TimeoutError:
            announcement_publish_task.cancel()
```

（依 main.py 既有 shutdown pattern 微調；若無 shutdown 段 leave_quota_expiry 同樣沒處理，本 task 亦可跳過 step 3。）

- [ ] **Step 4: Sanity test**

Run: `cd ivy-backend && python3 -c "import main"`
Expected: 不拋例外。

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat(main): register announcement publish scheduler"
```

---

## Task 7: Pydantic schema 加欄位

**Files:**
- Modify: `schemas/announcements.py`

- [ ] **Step 1: Read 既有 schemas**

Read `schemas/announcements.py` 全檔，確認 `AnnouncementListOut` / Item 結構。

- [ ] **Step 2: Item schema 加欄位**

在 List item Pydantic model 加：

```python
    publish_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    status: Literal["scheduled", "active", "expired"]
```

`Literal` / `Optional` / `datetime` 視 import 補上。

- [ ] **Step 3: 確認 OpenAPI dump 不拋例外**

Run: `cd ivy-backend && python3 scripts/dump_openapi.py > /tmp/openapi.json && jq '.components.schemas | keys | length' /tmp/openapi.json`
Expected: 正整數，無 stack trace。

- [ ] **Step 4: Commit**

```bash
git add schemas/announcements.py
git commit -m "feat(schema): announcement list item adds publish_at/expires_at/status"
```

---

## Task 8: Admin endpoints 改寫（list / create / update）

**Files:**
- Modify: `api/announcements.py`
- Test: `tests/api/test_announcements_schedule.py` (new)

- [ ] **Step 1: 寫失敗的測試**

Create `tests/api/test_announcements_schedule.py`：

```python
"""Tests for admin announcement schedule fields."""

from datetime import datetime, timedelta

import pytest

from utils.taipei_time import now_taipei_naive


def test_list_returns_status_active_for_unscheduled(admin_client, db_session, admin_emp):
    """No publish_at + no expires_at → active."""
    from models.database import Announcement

    a = Announcement(title="T", content="C", created_by=admin_emp.id)
    db_session.add(a)
    db_session.commit()

    res = admin_client.get("/api/announcements")
    assert res.status_code == 200
    items = res.json()["items"]
    me = next(i for i in items if i["id"] == a.id)
    assert me["status"] == "active"
    assert me["publish_at"] is None
    assert me["expires_at"] is None


def test_list_returns_scheduled_for_future_publish(admin_client, db_session, admin_emp):
    from models.database import Announcement

    future = now_taipei_naive() + timedelta(hours=2)
    a = Announcement(
        title="T", content="C", created_by=admin_emp.id, publish_at=future
    )
    db_session.add(a)
    db_session.commit()
    res = admin_client.get("/api/announcements")
    me = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert me["status"] == "scheduled"


def test_list_returns_expired_for_past_expires(admin_client, db_session, admin_emp):
    from models.database import Announcement

    past = now_taipei_naive() - timedelta(hours=1)
    a = Announcement(
        title="T", content="C", created_by=admin_emp.id, expires_at=past
    )
    db_session.add(a)
    db_session.commit()
    res = admin_client.get("/api/announcements")
    me = next(i for i in res.json()["items"] if i["id"] == a.id)
    assert me["status"] == "expired"


def test_create_with_publish_at_future_skips_immediate_push(
    admin_client, db_session, admin_emp, monkeypatch
):
    """publish_at 在未來時 replace_parent_recipients 不應立即觸發 LINE enqueue。"""
    from api import announcements as ann_module

    calls = []
    monkeypatch.setattr(
        ann_module,
        "_fire_announcement_push",
        lambda *a, **kw: calls.append((a, kw)),
    )

    future = (now_taipei_naive() + timedelta(hours=2)).isoformat()
    create_res = admin_client.post(
        "/api/announcements",
        json={
            "title": "排程公告",
            "content": "C",
            "priority": "normal",
            "publish_at": future,
        },
    )
    ann_id = create_res.json()["id"]
    # 補上家長 recipients
    admin_client.put(
        f"/api/announcements/{ann_id}/parent-recipients",
        json={"recipients": [{"scope": "all"}]},
    )
    assert calls == [], "publish_at 在未來時不應立即推播"


def test_create_with_publish_at_past_immediate_push(
    admin_client, db_session, admin_emp, monkeypatch
):
    from api import announcements as ann_module

    calls = []
    monkeypatch.setattr(
        ann_module,
        "_fire_announcement_push",
        lambda *a, **kw: calls.append((a, kw)),
    )

    past = (now_taipei_naive() - timedelta(hours=1)).isoformat()
    create_res = admin_client.post(
        "/api/announcements",
        json={"title": "T", "content": "C", "priority": "normal", "publish_at": past},
    )
    ann_id = create_res.json()["id"]
    admin_client.put(
        f"/api/announcements/{ann_id}/parent-recipients",
        json={"recipients": [{"scope": "all"}]},
    )
    assert len(calls) == 1


def test_create_rejects_expires_before_publish(admin_client):
    base = now_taipei_naive()
    res = admin_client.post(
        "/api/announcements",
        json={
            "title": "T",
            "content": "C",
            "priority": "normal",
            "publish_at": (base + timedelta(hours=2)).isoformat(),
            "expires_at": (base + timedelta(hours=1)).isoformat(),
        },
    )
    assert res.status_code == 400
    assert "expires" in res.json()["detail"].lower() or "到期" in res.json()["detail"]
```

`admin_client` / `admin_emp` / `db_session` fixture 沿用 repo 既有 conftest pattern；若 fixture 名不同，依 `tests/conftest.py` 實況微調。

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_announcements_schedule.py -v`
Expected: 全部 FAIL（status field 不存在、validation 沒擋）。

- [ ] **Step 3: 改 Pydantic create/update body 加欄位**

Edit `api/announcements.py` `AnnouncementCreate` 與 `AnnouncementUpdate`：

```python
class AnnouncementCreate(BaseModel):
    title: str
    content: str
    priority: str = "normal"
    is_pinned: bool = False
    target_employee_ids: Optional[List[int]] = None
    publish_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class AnnouncementUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[str] = None
    is_pinned: Optional[bool] = None
    target_employee_ids: Optional[List[int]] = None
    publish_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
```

於檔首補 `from datetime import datetime`（若已有則跳過）。

- [ ] **Step 4: 抽 validation helper**

於 `api/announcements.py` `_strip_html` 之後加：

```python
def _validate_schedule(publish_at, expires_at):
    """publish_at < expires_at；publish_at 不可在 now - 5min 之前 reject。"""
    from utils.taipei_time import now_taipei_naive
    from datetime import timedelta

    if publish_at is not None and expires_at is not None:
        if expires_at <= publish_at:
            raise HTTPException(
                status_code=400, detail="到期時間必須晚於發佈時間"
            )
    if publish_at is not None:
        threshold = now_taipei_naive() - timedelta(minutes=5)
        if publish_at < threshold:
            raise HTTPException(
                status_code=400, detail="排程發佈時間不可早於目前時間"
            )
```

- [ ] **Step 5: create_announcement 套 validation + 落欄位**

Edit `create_announcement`：

```python
    if data.priority not in ("normal", "important", "urgent"):
        raise HTTPException(status_code=400, detail="無效的優先級")
    _validate_schedule(data.publish_at, data.expires_at)

    session = get_session()
    try:
        ann = Announcement(
            title=_strip_html(data.title),
            content=_strip_html(data.content),
            priority=data.priority,
            is_pinned=data.is_pinned,
            publish_at=data.publish_at,
            expires_at=data.expires_at,
            created_by=current_user["employee_id"],
        )
```

- [ ] **Step 6: update_announcement 同樣套**

在 `update_announcement` 既有 `if data.is_pinned is not None: ann.is_pinned = data.is_pinned` 之後補：

```python
        # 用 model 上現有值與 incoming 合併後做 validation
        new_publish = data.publish_at if data.publish_at is not None else ann.publish_at
        new_expires = data.expires_at if data.expires_at is not None else ann.expires_at
        _validate_schedule(new_publish, new_expires)
        ann.publish_at = new_publish
        ann.expires_at = new_expires
```

- [ ] **Step 7: list 加 status + publish_at/expires_at 欄位**

於 `list_announcements` 序列化處（results.append 區塊）的 dict 加：

```python
from services.announcements.visibility import derive_status
from utils.taipei_time import now_taipei_naive
# … 後續
now = now_taipei_naive()
# results.append(...) 內加：
                    "publish_at": ann.publish_at.isoformat() if ann.publish_at else None,
                    "expires_at": ann.expires_at.isoformat() if ann.expires_at else None,
                    "status": derive_status(ann, now),
```

- [ ] **Step 8: replace_parent_recipients 加 future guard**

於 `_replace_recipients_impl` 既有 `if rows:` enqueue 段加上：

```python
        if rows:
            from utils.taipei_time import now_taipei_naive
            if ann.publish_at is not None and ann.publish_at > now_taipei_naive():
                logger.info(
                    "announcement %s publish_at 未到（%s），跳過立即推播；scheduler 接手",
                    ann.id,
                    ann.publish_at.isoformat(),
                )
            else:
                try:
                    _fire_announcement_push(...)
                except Exception ...
```

- [ ] **Step 9: 跑全部新測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_announcements_schedule.py -v`
Expected: 6 passed.

- [ ] **Step 10: 跑既有 announcements 測試確認無 regression**

Run: `cd ivy-backend && pytest tests/api/test_announcements.py -v`
Expected: 既有 case 全綠（新 key 不影響 dict 取舊欄位）。

- [ ] **Step 11: Commit**

```bash
git add api/announcements.py tests/api/test_announcements_schedule.py
git commit -m "feat(api): admin announcements support publish_at/expires_at + status field + future-push guard"
```

---

## Task 9: Portal endpoint 套 time predicate

**Files:**
- Modify: `api/portal/announcements.py`
- Test: `tests/api/test_portal_announcements.py`（補測試）

- [ ] **Step 1: 寫失敗的測試**

於 `tests/api/test_portal_announcements.py` 加：

```python
from datetime import timedelta

from utils.taipei_time import now_taipei_naive


def test_portal_hides_scheduled_announcement(portal_client, db_session, admin_emp):
    from models.database import Announcement

    future = now_taipei_naive() + timedelta(hours=2)
    a = Announcement(
        title="未來", content="C", created_by=admin_emp.id, publish_at=future
    )
    db_session.add(a)
    db_session.commit()
    res = portal_client.get("/api/portal/announcements")
    ids = [i["id"] for i in res.json()["items"]]
    assert a.id not in ids


def test_portal_hides_expired_announcement(portal_client, db_session, admin_emp):
    from models.database import Announcement

    past = now_taipei_naive() - timedelta(hours=1)
    a = Announcement(
        title="過期", content="C", created_by=admin_emp.id, expires_at=past
    )
    db_session.add(a)
    db_session.commit()
    res = portal_client.get("/api/portal/announcements")
    ids = [i["id"] for i in res.json()["items"]]
    assert a.id not in ids


def test_portal_mark_read_rejects_unpublished(portal_client, db_session, admin_emp):
    from models.database import Announcement

    future = now_taipei_naive() + timedelta(hours=2)
    a = Announcement(
        title="T", content="C", created_by=admin_emp.id, publish_at=future
    )
    db_session.add(a)
    db_session.commit()
    res = portal_client.post(f"/api/portal/announcements/{a.id}/read")
    assert res.status_code == 403
```

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_portal_announcements.py -v -k "scheduled or expired or unpublished"`
Expected: 3 FAIL。

- [ ] **Step 3: 修 visible_filter**

於 `api/portal/announcements.py` 三處（list / mark_read / unread_count）`visible_filter = no_recipients_subq | targeted_to_me_subq` 之後追加：

```python
        from services.announcements.visibility import visibility_time_predicate
        from utils.taipei_time import now_taipei_naive
        time_pred = visibility_time_predicate(now_taipei_naive())
        visible_filter = and_(or_(no_recipients_subq, targeted_to_me_subq), time_pred)
```

於檔首補 `from sqlalchemy import and_, or_`（若已有 `or_` 則只補 `and_`）。

注意：`visible_filter` 原為 `|`（SQLAlchemy or operator）；改寫後用 `and_(or_(...), time_pred)` 明確化。

- [ ] **Step 4: 補 ETag formula 包含 max publish_at/expires_at**

在 list endpoint 算 max_changed 段：

```python
        max_changed = session.query(
            func.max(Announcement.updated_at),
            func.max(Announcement.created_at),
            func.max(Announcement.publish_at),
            func.max(Announcement.expires_at),
        ).first()
```

避免「公告未變更但 publish_at 跨過 now() 點」造成 ETag 命中而看不到剛上架的公告。

- [ ] **Step 5: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_portal_announcements.py -v`
Expected: 全綠（新 3 case + 既有 case）。

- [ ] **Step 6: Commit**

```bash
git add api/portal/announcements.py tests/api/test_portal_announcements.py
git commit -m "feat(portal): announcements visible_filter + mark_read enforce publish/expires window"
```

---

## Task 10: Parent portal endpoint 套 time predicate

**Files:**
- Modify: `api/parent_portal/announcements.py`
- Test: `tests/api/test_parent_announcements.py`

- [ ] **Step 1: 寫失敗的測試（依既有 fixture pattern）**

在 `tests/api/test_parent_announcements.py` 加 2 test：
- `test_parent_hides_scheduled` — publish_at 在未來、scope=all → 不在 list
- `test_parent_hides_expired` — expires_at 過去、scope=all → 不在 list
- `test_parent_mark_read_rejects_unpublished` — 不可見公告 mark_read 403

（如 fixture 名不同，沿用 repo 既有 parent test fixture）。

- [ ] **Step 2: 跑測試確認 FAIL**

Run: `cd ivy-backend && pytest tests/api/test_parent_announcements.py -v -k "scheduled or expired or unpublished"`
Expected: 全 FAIL。

- [ ] **Step 3: 修 _build_visibility_subquery / mark_read**

於 `api/parent_portal/announcements.py` 三處（list / count_unread_for_user / mark_read）將 `visible_subq` 條件改為：

```python
from services.announcements.visibility import visibility_time_predicate
from utils.taipei_time import now_taipei_naive

# Within list()/count_unread_for_user()/mark_read():
time_pred = visibility_time_predicate(now_taipei_naive())
visible_subq = exists().where(and_(apr.announcement_id == Announcement.id, cond))
q = (
    session.query(Announcement)
    .filter(visible_subq, time_pred)
    ...
)
```

`mark_read` 的 `.first()` query 同樣 .filter `time_pred`。

- [ ] **Step 4: 跑測試確認 PASS**

Run: `cd ivy-backend && pytest tests/api/test_parent_announcements.py -v`
Expected: 全綠。

- [ ] **Step 5: Commit**

```bash
git add api/parent_portal/announcements.py tests/api/test_parent_announcements.py
git commit -m "feat(parent): announcements list/mark_read enforce publish/expires window"
```

---

## Task 11: Backend 全套 regression

- [ ] **Step 1: 跑 announcement 相關全 pytest**

Run:
```bash
cd ivy-backend && pytest tests/ -v -k "announcement" 2>&1 | tail -30
```
Expected: 全綠；若任何既有 case 失敗，需查是不是 visible_filter 改寫造成 — 修到全綠。

- [ ] **Step 2: 跑全 pytest 快速 smoke（focused suite）**

Run:
```bash
cd ivy-backend && pytest tests/ -q --ignore=tests/integration 2>&1 | tail -10
```
Expected: 與 main 相同的綠/紅平衡（無**新**失敗）。

- [ ] **Step 3: 若有意外 regression，git bisect 自己改的 commit 修到綠**

無 commit 步驟（純驗證）。

---

## Task 12: Frontend — OpenAPI codegen regen

**Files:**
- Modify: `ivy-frontend/src/api/_generated/schema.d.ts`

- [ ] **Step 1: Dump backend OpenAPI**

Run:
```bash
cd ivy-backend && python3 scripts/dump_openapi.py > openapi.json
```

- [ ] **Step 2: 前端 regen**

Run:
```bash
cd ivy-frontend && npm run gen:api
```

Expected: `src/api/_generated/schema.d.ts` 變動，include `publish_at` / `expires_at` / `status`。

- [ ] **Step 3: typecheck**

Run: `cd ivy-frontend && npm run typecheck`
Expected: 0 error。

- [ ] **Step 4: Commit（前端 repo）**

```bash
cd ivy-frontend && git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regen schema for announcement schedule fields"
```

---

## Task 13: Frontend — AnnouncementView.vue datetime picker + status

**Files:**
- Modify: `ivy-frontend/src/views/AnnouncementView.vue`
- Test: `ivy-frontend/tests/unit/views/AnnouncementView.test.js`（或 `.ts`）

- [ ] **Step 1: Form reactive 加欄位**

在 `const form = reactive<{...}>({...})` 加：

```typescript
  publish_at: null as string | null,   // ISO 字串或 null
  expires_at: null as string | null,
```

`resetForm` 同步補上：

```typescript
  form.publish_at = null
  form.expires_at = null
```

- [ ] **Step 2: openEdit 帶入既有值**

`openEdit(row)` 內：

```typescript
  form.publish_at = (row.publish_at as string | null) ?? null
  form.expires_at = (row.expires_at as string | null) ?? null
```

`AnnouncementItem` interface 補欄位：

```typescript
  publish_at?: string | null
  expires_at?: string | null
  status?: 'scheduled' | 'active' | 'expired'
```

- [ ] **Step 3: Submit 帶入 payload**

`createAnnouncement` / `updateAnnouncement` payload 內加：

```typescript
      publish_at: form.publish_at,
      expires_at: form.expires_at,
```

- [ ] **Step 4: Template 加 datetime picker（在「家長端」divider 之前）**

```vue
<el-divider content-position="left">排程</el-divider>

<el-form-item label="發佈時間">
  <el-date-picker
    v-model="form.publish_at"
    type="datetime"
    placeholder="留空＝立即發佈"
    value-format="YYYY-MM-DDTHH:mm:ss"
    style="width: 100%;"
  />
</el-form-item>
<el-form-item label="到期時間">
  <el-date-picker
    v-model="form.expires_at"
    type="datetime"
    placeholder="留空＝永久"
    value-format="YYYY-MM-DDTHH:mm:ss"
    style="width: 100%;"
  />
</el-form-item>
```

- [ ] **Step 5: Table 加 status column**

在「優先級」column 之後加：

```vue
<el-table-column label="狀態" width="90" align="center">
  <template #default="{ row }">
    <el-tag v-if="row.status === 'scheduled'" type="info" size="small">預定</el-tag>
    <el-tag v-else-if="row.status === 'expired'" size="small">已過期</el-tag>
    <el-tag v-else type="success" size="small">進行中</el-tag>
  </template>
</el-table-column>
```

- [ ] **Step 6: 跑 typecheck + build**

Run:
```bash
cd ivy-frontend && npm run typecheck && npm run build
```
Expected: 0 error。

- [ ] **Step 7: Vitest — render status tag 三狀態 + form datetime binding**

於 `tests/unit/views/AnnouncementView.test.js` 加：

```javascript
import { mount } from '@vue/test-utils'
import { describe, it, expect, vi } from 'vitest'
import ElementPlus from 'element-plus'
import AnnouncementView from '@/views/AnnouncementView.vue'

vi.mock('@/api/announcements', () => ({
  getAnnouncements: vi.fn().mockResolvedValue({
    data: {
      items: [
        { id: 1, title: '排定中', content: '', priority: 'normal', is_pinned: false, status: 'scheduled' },
        { id: 2, title: '進行', content: '', priority: 'normal', is_pinned: false, status: 'active' },
        { id: 3, title: '已過期', content: '', priority: 'normal', is_pinned: false, status: 'expired' },
      ],
    },
  }),
  createAnnouncement: vi.fn(),
  updateAnnouncement: vi.fn(),
  deleteAnnouncement: vi.fn(),
  getAnnouncementParentRecipients: vi.fn().mockResolvedValue({ data: { items: [] } }),
  replaceAnnouncementParentRecipients: vi.fn(),
}))
vi.mock('@/stores/employee', () => ({ useEmployeeStore: () => ({ employees: [], fetchEmployees: vi.fn() }) }))
vi.mock('@/stores/classroom', () => ({ useClassroomStore: () => ({ classrooms: [], fetchClassrooms: vi.fn() }) }))

describe('AnnouncementView status tag', () => {
  it('renders 預定 / 進行中 / 已過期 tags', async () => {
    const wrapper = mount(AnnouncementView, { global: { plugins: [ElementPlus] } })
    await new Promise((r) => setTimeout(r, 0))  // microtask flush
    const text = wrapper.text()
    expect(text).toContain('預定')
    expect(text).toContain('進行中')
    expect(text).toContain('已過期')
  })
})
```

- [ ] **Step 8: 跑 vitest**

Run: `cd ivy-frontend && npx vitest run tests/unit/views/AnnouncementView.test.js`
Expected: PASS（含新測試）。

- [ ] **Step 9: Commit**

```bash
cd ivy-frontend && git add src/views/AnnouncementView.vue tests/unit/views/AnnouncementView.test.js
git commit -m "feat(ui): announcement form datetime picker + status column"
```

---

## Self-Review checklist（implementer 完成後跑）

- [ ] **Backend full pytest**：`cd ivy-backend && pytest tests/ -q 2>&1 | tail -10` — 與 main 相同的綠/紅平衡
- [ ] **Frontend typecheck + build + vitest**：`cd ivy-frontend && npm run typecheck && npm run build && npx vitest run` — 全綠
- [ ] **OpenAPI drift check**：`cd ivy-frontend && npm run gen:api:check` — 無 drift
- [ ] **Alembic single head**：`cd ivy-backend && alembic heads | wc -l` — 1
- [ ] **手測（user 端）**：
  - 新增公告，留空 publish_at + 留空 expires_at → 立即發佈、家長即收 LINE
  - 新增公告，publish_at=now+2min + 家長 recipients=all → 不應立即收 LINE；2 分鐘後 scheduler tick 推播
  - 設 expires_at 過去時間 → 員工/家長 portal 看不到，admin list 顯示「已過期」
  - 編輯既有公告改 publish_at=未來 → admin list status 變 scheduled，portal/parent 看不到

---

## Out-of-scope（不在本 plan）

- 取消已排程公告專屬 endpoint（user 改 publish_at=null 或 expires_at=now 即達成）
- 重複排程（每週/每月）
- 員工端 LINE 推播
- 通知偏好設定（家長收到 LINE 後不想收的 opt-out）

# P1 韌性 Phase 2：LINE retry via NotificationLog augment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** LINE 推送失敗持久化 + 5-min scheduler tick 自動重試 max 3 次 + backoff 30s/5min/30min。Augment 既有 `NotificationLog` 表（不另建 outbox），加 3 column；解開「inbox UX」與「retry audit」耦合讓 14 個 LINE-only 家長事件也能被 retry。

**Architecture:** Alembic migration 加 `line_retry_count` / `line_next_retry_at` / `is_inbox_visible` 三 column。`dispatch._fan_out` 改寫成「matrix 含 line/ws/in_app 任一即寫 log row」並依 `'in_app' in channels` 設 `is_inbox_visible`；LINE 失敗時用 `log_session` 寫 `line_next_retry_at`（避 phantom retry on rollback）。新檔 `services/notification/retry_scheduler.py` 提供 `tick_line_retry()` async function，註冊到 `main.py` lifespan scheduler（複用 leave-quota-expiry scheduler 同一個 asyncio loop）。

**Tech Stack:** SQLAlchemy 2.x + Alembic + asyncio scheduler；無新 dependency。

**Spec**: `docs/superpowers/specs/2026-05-28-p1-external-integration-resilience-design.md` §6

---

## File Structure

| 動作 | 路徑 | 責任 |
|------|------|------|
| Create | `alembic/versions/20260528_notifretry01_notification_log_retry_columns.py` | 加 3 column + partial index |
| Modify | `models/notification_log.py` | 加 3 column ORM mapping |
| Modify | `services/notification/dispatch.py` | `_fan_out` 改寫；無條件寫 log row + 失敗時 schedule retry |
| Create | `services/notification/retry_scheduler.py` | `tick_line_retry()` async function + helper |
| Modify | `main.py` | lifespan scheduler 註冊 `tick_line_retry` 每 5 min |
| Create | `tests/notification/test_retry_scheduler.py` | scheduler unit + integration test |
| Modify | `tests/notification/test_dispatch_fan_out.py` | 補既有 test 新行為（log row 對 parent.* 也寫；is_inbox_visible 對 LINE-only False）|

---

## Task 1: Alembic migration + ORM column (TDD-lite)

**Files:**
- Create: `alembic/versions/20260528_notifretry01_notification_log_retry_columns.py`
- Modify: `models/notification_log.py`

- [ ] **Step 1.1: Write migration**

```python
"""notification_logs add line retry columns + is_inbox_visible

Revision ID: notifretry01
Revises: mergeheads06
Create Date: 2026-05-28

對應 spec §6.2 Phase 2 P1 resilience。
- line_retry_count / line_next_retry_at：scheduler 撈 retry pending row
- is_inbox_visible：解開 inbox UX 與 retry audit 耦合（14 個 LINE-only 家長事件
  log row 也寫，但不出現在員工 inbox）
"""
from alembic import op
import sqlalchemy as sa

revision = "notifretry01"
down_revision = "mergeheads06"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "notification_logs",
        sa.Column("line_retry_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "notification_logs",
        sa.Column("line_next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notification_logs",
        sa.Column(
            "is_inbox_visible",
            sa.Boolean,
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.create_index(
        "ix_notif_log_line_retry_pending",
        "notification_logs",
        ["line_next_retry_at"],
        postgresql_where=sa.text(
            "line_next_retry_at IS NOT NULL AND line_retry_count < 3"
        ),
    )


def downgrade():
    op.drop_index("ix_notif_log_line_retry_pending", table_name="notification_logs")
    op.drop_column("notification_logs", "is_inbox_visible")
    op.drop_column("notification_logs", "line_next_retry_at")
    op.drop_column("notification_logs", "line_retry_count")
```

- [ ] **Step 1.2: Update ORM mapping**

In `models/notification_log.py`, add 3 columns after `channels_failed`:

```python
    line_retry_count = Column(Integer, nullable=False, default=0, server_default="0")
    line_next_retry_at = Column(DateTime(timezone=True), nullable=True)
    is_inbox_visible = Column(Boolean, nullable=False, default=True, server_default=text("true"))
```

Import `Boolean` from sqlalchemy if not already.

- [ ] **Step 1.3: Verify alembic upgrade runs clean**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be
alembic upgrade heads 2>&1 | tail -5
alembic heads 2>&1 | tail -3
```

Expected: single head `notifretry01`. If multi-head, abort + investigate.

- [ ] **Step 1.4: Commit**

```bash
git add alembic/versions/20260528_notifretry01_notification_log_retry_columns.py models/notification_log.py
git commit -m "feat(notif): NotificationLog augment 3 column for LINE retry (Phase 2)

對應 spec §6.2。新欄：
- line_retry_count int default 0
- line_next_retry_at timestamptz null（partial index 撈 pending retry）
- is_inbox_visible bool default true（解開 inbox UX 與 retry audit 耦合）

下個 commit 改 dispatch._fan_out 啟用 retry write path。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: dispatch._fan_out 改寫（核心）+ test

**Files:**
- Modify: `services/notification/dispatch.py`
- Modify: `tests/notification/test_dispatch_fan_out.py`

### 行為變更

**Before**：only writes log row when `"in_app" in evt.channels` → 14 個 LINE-only 事件無 log row
**After**：writes log row when ANY of `("in_app", "line", "ws") in evt.channels`；`is_inbox_visible = "in_app" in evt.channels`；LINE 失敗時寫 `line_next_retry_at = now + 30s`（用 `log_session` 寫）

### 關鍵 patch（surgical 用 bash python3 繞 black hook）

- [ ] **Step 2.1: Write failing tests**

Append to `tests/notification/test_dispatch_fan_out.py`:

```python
class TestPhase2LineRetry:
    """Phase 2 P1 resilience：log row 寫所有 line/ws 事件 + LINE 失敗 schedule retry."""

    def test_parent_line_only_event_writes_log_row(self, session, monkeypatch):
        """parent.fee_due (LINE-only) 也須寫 log row（之前只有 in_app 事件才寫）."""
        from services.notification import dispatch
        from models.database import User, NotificationLog
        from datetime import datetime

        # 建一個 parent user 含 line_user_id
        user = User(
            username="p1",
            line_user_id="Uparent1",
            line_follow_confirmed_at=datetime.now(),
            is_active=True,
        )
        session.add(user)
        session.commit()

        # mock LINE adapter（避免真打 API）
        sent = []
        monkeypatch.setattr(
            "services.notification.dispatch._get_line_adapter",
            lambda: type("A", (), {"send": lambda self, evt, r, log_id: sent.append(evt.event_type)})(),
        )

        dispatch.enqueue(
            session=session,
            event_type="parent.fee_due",
            recipient_user_id=user.id,
            context={"student_name": "X", "item_name": "T", "amount": 100, "due_date": "2026-06-01"},
        )
        session.commit()

        rows = session.query(NotificationLog).filter_by(event_type="parent.fee_due").all()
        assert len(rows) == 1, "parent.fee_due 須寫 log row（即使無 in_app）"
        assert rows[0].is_inbox_visible is False, "LINE-only 事件 is_inbox_visible 須 False"

    def test_in_app_event_is_inbox_visible_true(self, session, monkeypatch):
        from services.notification import dispatch
        from models.database import User, NotificationLog
        from datetime import datetime

        user = User(username="emp1", is_active=True)
        session.add(user)
        session.commit()

        monkeypatch.setattr(
            "services.notification.dispatch._get_line_adapter",
            lambda: type("A", (), {"send": lambda self, evt, r, log_id: None})(),
        )

        dispatch.enqueue(
            session=session,
            event_type="leave.submitted",
            recipient_user_id=user.id,
            context={"submitter_name": "X", "leave_type": "事假", "start": "2026-06-01", "end": "2026-06-01", "leave_hours": 4},
        )
        session.commit()

        row = session.query(NotificationLog).filter_by(event_type="leave.submitted").first()
        assert row is not None
        assert row.is_inbox_visible is True

    def test_line_failure_schedules_retry(self, session, monkeypatch):
        from services.notification import dispatch
        from models.database import User, NotificationLog
        from datetime import datetime

        user = User(
            username="p2",
            line_user_id="Uparent2",
            line_follow_confirmed_at=datetime.now(),
            is_active=True,
        )
        session.add(user)
        session.commit()

        # mock adapter throws → fan_out 寫 channels_failed + line_next_retry_at
        class Boom:
            def send(self, evt, r, log_id):
                raise ConnectionError("LINE down")
        monkeypatch.setattr(
            "services.notification.dispatch._get_line_adapter", lambda: Boom(),
        )

        dispatch.enqueue(
            session=session,
            event_type="parent.fee_due",
            recipient_user_id=user.id,
            context={"student_name": "X", "item_name": "T", "amount": 100, "due_date": "2026-06-01"},
        )
        session.commit()

        row = session.query(NotificationLog).filter_by(event_type="parent.fee_due").first()
        assert row is not None
        assert row.line_next_retry_at is not None, "LINE 失敗須 schedule retry"
        assert row.line_retry_count == 0, "首次失敗 count 仍 0（scheduler tick 才會 +1）"
        assert any(f.get("channel") == "line" for f in row.channels_failed)

    def test_business_rollback_no_retry_phantom(self, session, monkeypatch):
        """phantom retry on rollback guard：業務 tx rollback 後不應留 log row."""
        from services.notification import dispatch
        from models.database import User, NotificationLog

        user = User(username="p3", is_active=True)
        session.add(user)
        session.commit()

        dispatch.enqueue(
            session=session,
            event_type="parent.fee_due",
            recipient_user_id=user.id,
            context={"student_name": "X", "item_name": "T", "amount": 100, "due_date": "2026-06-01"},
        )
        session.rollback()  # 業務 tx 滾回

        rows = session.query(NotificationLog).filter_by(event_type="parent.fee_due").all()
        assert rows == [], "業務 rollback 不應留 NotificationLog（log_session 也未 commit）"
```

- [ ] **Step 2.2: Run tests to verify they fail (or skip if conftest issue)**

```bash
pytest tests/notification/test_dispatch_fan_out.py::TestPhase2LineRetry -v
```

If session fixture不存在請 mimic 既有測試 fixture pattern。

- [ ] **Step 2.3: Modify dispatch.py `_fan_out`**

用 bash python3 string.replace 改 `services/notification/dispatch.py:272-375` 的 `_fan_out` function。

需要的關鍵改動：

1. 條件 `if "in_app" in evt.channels` 改為 `if any(ch in evt.channels for ch in ("in_app", "line", "ws"))`
2. 在 `log_row = NotificationLog(...)` 加 `is_inbox_visible="in_app" in evt.channels`
3. LINE failure branch（`failed.append({"channel": "line", ...})` 後）寫：
   ```python
   if log_id is not None:
       from datetime import datetime, timedelta, timezone as _tz
       row = log_session.query(NotificationLog).get(log_id)
       if row is not None:
           row.line_next_retry_at = datetime.now(_tz.utc) + timedelta(seconds=30)
           # commit 之後在 finally 前
   ```
4. `_inbox_ws_push` 呼叫條件改為 `if "in_app" in evt.channels and evt.recipient_user_id is not None`（之前已 in_app guard，補強顯式）

完整 patch via bash python3：

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be && python3 <<'PY'
with open("services/notification/dispatch.py", "r") as f:
    content = f.read()

# 1. 把 "if 'in_app' in evt.channels:" 寫 log 的條件擴張
old_cond = '''        # 只有 in_app 在 matrix 內才寫 log row（家長域沒 in_app 就不寫，但仍跑 line/ws）
        log_id: int | None = None
        if "in_app" in evt.channels:'''
new_cond = '''        # Phase 2 (P1 resilience)：matrix 含 line/ws/in_app 任一就寫 log row，
        # is_inbox_visible 由 in_app 決定（解開 inbox UX 與 retry audit 耦合）。
        log_id: int | None = None
        _has_durable_channel = any(ch in evt.channels for ch in ("in_app", "line", "ws"))
        if _has_durable_channel and evt.recipient_user_id is not None:'''
content = content.replace(old_cond, new_cond, 1)

# 2. 加 is_inbox_visible 到 NotificationLog() 建構
old_log = '''            log_row = NotificationLog(
                recipient_user_id=evt.recipient_user_id,
                event_type=evt.event_type,
                sender_id=evt.sender_id,
                source_entity_type=evt.source_entity_type,
                source_entity_id=evt.source_entity_id,
                title=rendered.title,
                body=rendered.body,
                payload_json=dict(evt.context),
                deep_link=rendered.deep_link,
                channels_attempted=list(active_channels),
                channels_succeeded=["in_app"],
                channels_failed=[],
            )'''
new_log = '''            log_row = NotificationLog(
                recipient_user_id=evt.recipient_user_id,
                event_type=evt.event_type,
                sender_id=evt.sender_id,
                source_entity_type=evt.source_entity_type,
                source_entity_id=evt.source_entity_id,
                title=rendered.title,
                body=rendered.body,
                payload_json=dict(evt.context),
                deep_link=rendered.deep_link,
                channels_attempted=list(active_channels),
                channels_succeeded=["in_app"] if "in_app" in evt.channels else [],
                channels_failed=[],
                is_inbox_visible="in_app" in evt.channels,
            )'''
content = content.replace(old_log, new_log, 1)

# 3. _inbox_ws_push 顯式 guard in_app（雖然位置上現在還在 if 內 OK，加 conditional 防 LINE-only event 也被 push）
old_ws_push = '''            # in_app 路徑後立刻推 inbox WS；失敗只 warning 不算 in_app failure
            if evt.recipient_user_id is not None:'''
new_ws_push = '''            # in_app 路徑後立刻推 inbox WS；失敗只 warning 不算 in_app failure
            # Phase 2: 顯式 guard 'in_app' in channels（避免 LINE-only 事件也 push 員工 inbox）
            if "in_app" in evt.channels and evt.recipient_user_id is not None:'''
content = content.replace(old_ws_push, new_ws_push, 1)

# 4. LINE failure 後 schedule retry
old_line_fail = '''                try:
                    _get_line_adapter().send(line_evt, rendered, log_id=log_id or 0)
                    succeeded.append("line")
                except Exception as exc:
                    logger.exception(
                        "LINE channel failed event=%s user=%s group=%s",
                        evt.event_type,
                        evt.recipient_user_id,
                        evt.line_group_id,
                    )
                    failed.append({"channel": "line", "error": type(exc).__name__})
                continue'''
new_line_fail = '''                try:
                    _get_line_adapter().send(line_evt, rendered, log_id=log_id or 0)
                    succeeded.append("line")
                except Exception as exc:
                    logger.exception(
                        "LINE channel failed event=%s user=%s group=%s",
                        evt.event_type,
                        evt.recipient_user_id,
                        evt.line_group_id,
                    )
                    failed.append({"channel": "line", "error": type(exc).__name__})
                    # Phase 2 (P1 resilience)：schedule retry（用 log_session，
                    # 業務 tx rollback 不會留 phantom retry，因 log_row 寫在獨立 log_session）
                    if log_id is not None:
                        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                        _retry_row = log_session.query(NotificationLog).get(log_id)
                        if _retry_row is not None:
                            _retry_row.line_next_retry_at = _dt.now(_tz.utc) + _td(seconds=30)
                continue'''
content = content.replace(old_line_fail, new_line_fail, 1)

with open("services/notification/dispatch.py", "w") as f:
    f.write(content)

# Verify all 4 patches applied
import subprocess
for marker in ["Phase 2 (P1 resilience)", "is_inbox_visible=", "顯式 guard", "_retry_row ="]:
    n = subprocess.check_output(["grep", "-c", marker, "services/notification/dispatch.py"]).decode().strip()
    print(f"{marker}: {n} matches")
PY
```

- [ ] **Step 2.4: Run new tests + existing dispatch tests**

```bash
pytest tests/notification/test_dispatch_fan_out.py -v 2>&1 | tail -30
```

Expected: 4 新 PASS + 既有 PASS 全綠。如既有 test 對 log row 數量有 assertion 可能需更新（之前只算 in_app 事件；現在 LINE-only 也算）。修到綠。

- [ ] **Step 2.5: Commit**

```bash
git add services/notification/dispatch.py tests/notification/test_dispatch_fan_out.py
git commit -m "feat(notif): dispatch._fan_out 寫 log row + schedule LINE retry (Phase 2)

對應 spec §6.3。行為變更：
- 14 個 LINE-only 家長事件也寫 NotificationLog row（is_inbox_visible=False）
- LINE 失敗時 line_next_retry_at = now+30s（用 log_session，避 phantom rollback）
- _inbox_ws_push 顯式 in_app guard 防 LINE-only 事件污染員工 inbox

下個 commit 加 retry scheduler tick。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Retry scheduler + 註冊 + test

**Files:**
- Create: `services/notification/retry_scheduler.py`
- Modify: `main.py`
- Create: `tests/notification/test_retry_scheduler.py`

- [ ] **Step 3.1: Write `retry_scheduler.py`**

```python
"""services/notification/retry_scheduler.py — LINE retry scheduler tick.

對應 spec §6.4。每 5 min 撈 NotificationLog.line_next_retry_at <= now() AND
line_retry_count < 3 的 row → 用 LINE_HANDLERS 重新 render + 重發。

Backoff：30s → 5min → 30min（指數）。第 3 次失敗 mark final=true。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from models.base import get_session_factory
from models.database import NotificationLog
from services.notification.dispatch import PendingEvent, _get_line_adapter, _resolve_line_user_id
from services.notification.renderers import render
from utils.external_calls import tagged_capture

logger = logging.getLogger(__name__)

_BACKOFF_SECONDS = [30, 300, 1800]  # tick 0→1, 1→2, 2→final
_MAX_RETRIES = 3
_TICK_LIMIT = 100  # 單 tick 上限


def tick_line_retry(now_provider=lambda: datetime.now(timezone.utc)) -> dict:
    """每 5 分鐘 tick：撈 pending LINE retry 重發，回 metric dict."""
    session = get_session_factory()()
    metric = {"attempted": 0, "succeeded": 0, "failed": 0, "final_failed": 0}
    try:
        now = now_provider()
        rows = (
            session.query(NotificationLog)
            .filter(
                NotificationLog.line_next_retry_at.is_not(None),
                NotificationLog.line_next_retry_at <= now,
                NotificationLog.line_retry_count < _MAX_RETRIES,
            )
            .limit(_TICK_LIMIT)
            .all()
        )
        metric["attempted"] = len(rows)

        for row in rows:
            try:
                ok = _retry_line_push(session, row)
                if ok:
                    row.line_next_retry_at = None
                    row.channels_succeeded = list(row.channels_succeeded) + ["line(retry)"]
                    metric["succeeded"] += 1
                else:
                    _schedule_next_or_final(row, now)
                    if row.line_retry_count >= _MAX_RETRIES:
                        metric["final_failed"] += 1
                    else:
                        metric["failed"] += 1
            except Exception as exc:
                logger.exception("tick_line_retry row=%s failed", row.id)
                tagged_capture(exc, tag="line", level="error")
                _schedule_next_or_final(row, now)
                metric["failed"] += 1

        session.commit()
    finally:
        session.close()
    return metric


def _retry_line_push(session, row: NotificationLog) -> bool:
    """Reconstruct PendingEvent 從 log row + 重發 LINE.

    Returns True 表示成功（LINE adapter 沒拋）；False 表示 LINE 仍失敗。
    """
    line_user_id = _resolve_line_user_id(session, row.recipient_user_id)
    if line_user_id is None:
        # 用戶不再可達（unfollow/inactive）— mark final，停止 retry
        row.line_retry_count = _MAX_RETRIES
        row.line_next_retry_at = None
        return False

    evt = PendingEvent(
        event_type=row.event_type,
        recipient_user_id=line_user_id,
        context=dict(row.payload_json),
        sender_id=row.sender_id,
        source_entity_type=row.source_entity_type,
        source_entity_id=row.source_entity_id,
        channels=("line",),
        line_group_id=None,
    )
    rendered = render(row.event_type, row.payload_json)
    try:
        _get_line_adapter().send(evt, rendered, log_id=row.id)
        return True
    except Exception:
        return False


def _schedule_next_or_final(row: NotificationLog, now: datetime) -> None:
    """更新 retry_count；若達 max mark final 並寫 channels_failed."""
    row.line_retry_count += 1
    if row.line_retry_count >= _MAX_RETRIES:
        row.line_next_retry_at = None
        failed = list(row.channels_failed)
        failed.append({"channel": "line", "error": "max_retries", "final": True})
        row.channels_failed = failed
    else:
        # backoff index：第 1 次失敗用 BACKOFF[0]=30s（首發 schedule 已用），
        # tick 後 line_retry_count=1 用 BACKOFF[1]=300s，tick 後 =2 用 BACKOFF[2]=1800s
        delay = _BACKOFF_SECONDS[min(row.line_retry_count, len(_BACKOFF_SECONDS) - 1)]
        row.line_next_retry_at = now + timedelta(seconds=delay)
```

- [ ] **Step 3.2: Write scheduler test**

```python
# tests/notification/test_retry_scheduler.py
"""Phase 2 P1 resilience：retry scheduler tick unit + integration test."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
import pytest


class TestTickLineRetry:
    def test_picks_pending_row_and_succeeds(self, session, monkeypatch):
        from services.notification.retry_scheduler import tick_line_retry
        from models.database import NotificationLog, User

        user = User(
            username="p", line_user_id="U1", is_active=True,
            line_follow_confirmed_at=datetime.now(),
        )
        session.add(user)
        session.commit()

        row = NotificationLog(
            recipient_user_id=user.id,
            event_type="parent.fee_due",
            title="t", body="b",
            payload_json={"student_name":"X","item_name":"I","amount":100,"due_date":"2026-06-01"},
            channels_attempted=["line"], channels_succeeded=[], channels_failed=[{"channel":"line","error":"X"}],
            line_next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            line_retry_count=0,
            is_inbox_visible=False,
        )
        session.add(row)
        session.commit()

        monkeypatch.setattr(
            "services.notification.retry_scheduler._get_line_adapter",
            lambda: MagicMock(send=MagicMock(return_value=None)),
        )

        result = tick_line_retry()
        session.refresh(row)
        assert result["attempted"] == 1
        assert result["succeeded"] == 1
        assert row.line_next_retry_at is None
        assert "line(retry)" in row.channels_succeeded

    def test_third_failure_marks_final(self, session, monkeypatch):
        from services.notification.retry_scheduler import tick_line_retry
        from models.database import NotificationLog, User

        user = User(username="p", line_user_id="U2", is_active=True, line_follow_confirmed_at=datetime.now())
        session.add(user)
        session.commit()

        row = NotificationLog(
            recipient_user_id=user.id, event_type="parent.fee_due",
            title="t", body="b",
            payload_json={"student_name":"X","item_name":"I","amount":1,"due_date":"2026-06-01"},
            channels_attempted=["line"], channels_succeeded=[],
            channels_failed=[{"channel":"line","error":"X"}],
            line_next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            line_retry_count=2,  # 已試 2 次（首發+1 tick）；本 tick 後達 3
            is_inbox_visible=False,
        )
        session.add(row)
        session.commit()

        # adapter 仍失敗
        class Boom:
            def send(self, *a, **k): raise ConnectionError("still down")
        monkeypatch.setattr("services.notification.retry_scheduler._get_line_adapter", lambda: Boom())

        result = tick_line_retry()
        session.refresh(row)
        assert result["final_failed"] == 1
        assert row.line_retry_count == 3
        assert row.line_next_retry_at is None
        assert any(f.get("final") is True for f in row.channels_failed)

    def test_unreachable_user_marks_final_immediately(self, session, monkeypatch):
        """user inactive / no line_user_id → mark final，不再 retry."""
        from services.notification.retry_scheduler import tick_line_retry
        from models.database import NotificationLog, User

        user = User(username="p3", is_active=False)  # inactive
        session.add(user)
        session.commit()

        row = NotificationLog(
            recipient_user_id=user.id, event_type="parent.fee_due",
            title="t", body="b",
            payload_json={"student_name":"X","item_name":"I","amount":1,"due_date":"2026-06-01"},
            channels_attempted=["line"], channels_succeeded=[],
            channels_failed=[{"channel":"line","error":"X"}],
            line_next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=5),
            line_retry_count=0,
            is_inbox_visible=False,
        )
        session.add(row)
        session.commit()

        result = tick_line_retry()
        session.refresh(row)
        # _retry_line_push 偵測 user unreachable → mark final
        assert row.line_retry_count == 3
        assert row.line_next_retry_at is None
```

- [ ] **Step 3.3: Wire into main.py scheduler**

Find the scheduler tick wiring in `main.py` (look for `leave_quota_expiry` scheduler or similar lifespan tick registration) and add `tick_line_retry` registration with 5-min interval.

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be && grep -n 'leave_quota_expiry\|asyncio.create_task\|scheduler.*tick\|TICK_' main.py | head -10
```

Apply same pattern. Use bash python3 string.replace to add `from services.notification.retry_scheduler import tick_line_retry` import + scheduler tick at the same place where existing scheduler ticks are wired.

- [ ] **Step 3.4: Run tests**

```bash
pytest tests/notification/test_retry_scheduler.py -v
```

Expected: 3 PASS。如 session fixture 缺再 mimic 既有 conftest pattern。

- [ ] **Step 3.5: Commit**

```bash
git add services/notification/retry_scheduler.py tests/notification/test_retry_scheduler.py main.py
git commit -m "feat(notif): tick_line_retry scheduler 每 5 min 重發 pending row (Phase 2)

對應 spec §6.4。backoff 30s/5min/30min；3 次失敗 mark final
（channels_failed 加 final=true）。用 LINE_HANDLERS reconstruct PendingEvent
重新 render 重發；user unreachable 直接 mark final 不重試。

註冊到 main.py lifespan scheduler（與 leave_quota_expiry 共用 asyncio loop）。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## 完成定義

Phase 2 ship = 3 atomic commit：
1. alembic migration + ORM column
2. dispatch._fan_out 行為變更（log row + schedule retry + is_inbox_visible）
3. retry scheduler + main.py 註冊

全套 backend pytest 零 regression（既有 dispatch test 若對 row 數 assert 須校正）。

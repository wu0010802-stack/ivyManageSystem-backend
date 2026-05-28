# Observability / Kill Switch / Friendly Error Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修三項 P0 防護 — (3) scheduler 心跳 + uptime monitor、(4) maintenance/read-only kill switch、(5) 家長友善錯誤訊息 + parent router 漸進改 BusinessError。

**Architecture:** 6 PR（每 repo 3 個獨立 PR）；各自獨立 merge；FE-A 依賴 BE-B 的 envelope code；FE-C 依賴 BE-C 的 BusinessError code。BE-A 用 DB-backed `scheduler_heartbeats` 表持久化（解決既有 in-memory metrics restart 丟失問題）；BE-B 用 env-only middleware 避開事故時 DB 可能掛；BE-C 限縮 ~50 處家長端路徑用既有 BusinessError envelope path。

**Tech Stack:** FastAPI / SQLAlchemy 2.x / Alembic / pytest / Vue 3 / Vite / TypeScript / Element Plus / Vitest

**Spec:** `docs/superpowers/specs/2026-05-28-observability-killswitch-friendly-error-design.md`

---

## Phase 概覽

| Phase | PR | 工作量 | 依賴 |
|-------|----|---|------|
| 1 | BE-A scheduler heartbeat + /health/schedulers | ~12 task | 無 |
| 2 | BE-B maintenance / read-only kill switch | ~6 task | 無 |
| 3 | BE-C parent BusinessError ~50 處 | ~10 task | 無 |
| 4 | FE-A MaintenanceView + 503 redirect | ~7 task | 依 Phase 2 |
| 5 | FE-B 5xx / network friendly fallback | ~4 task | 無 |
| 6 | FE-C useFriendlyError + errorCodeRegistry | ~8 task | 依 Phase 3 |

並行：Phase 1 / 2 / 3 / 5 可同時開工；Phase 4 等 Phase 2 merge；Phase 6 等 Phase 3 merge。

---

## Phase 1 — BE-A: Scheduler Heartbeat + /health/schedulers

**Worktree**：`/Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/feat-observability-killswitch-2026-05-28-backend`（branch `feat/observability-heartbeat-killswitch-friendly-error-2026-05-28-backend`，已建好）

### Task 1.1 — 建立 `SchedulerHeartbeat` ORM model

**Files:**
- Create: `models/scheduler_heartbeat.py`
- Modify: `models/__init__.py`（中央 import 註冊）

- [ ] **Step 1: Write failing test**

`tests/test_scheduler_heartbeat_model.py`:

```python
from datetime import datetime, timezone
from models.base import Base, get_engine
from models.scheduler_heartbeat import SchedulerHeartbeat
from sqlalchemy.orm import Session


def test_heartbeat_create_and_query(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
    from importlib import reload
    from models import base as base_mod
    reload(base_mod)
    Base.metadata.create_all(base_mod.get_engine())

    with Session(base_mod.get_engine()) as s:
        hb = SchedulerHeartbeat(
            scheduler_name="test_sched",
            expected_interval_seconds=300,
            consecutive_failures=0,
            last_rows_processed=0,
        )
        s.add(hb)
        s.commit()
        row = s.query(SchedulerHeartbeat).filter_by(scheduler_name="test_sched").one()
        assert row.expected_interval_seconds == 300
        assert row.last_success_at is None
```

Run: `pytest tests/test_scheduler_heartbeat_model.py -v` → expect FAIL (model not exists)

- [ ] **Step 2: Implement model**

`models/scheduler_heartbeat.py`:

```python
"""scheduler_heartbeats — DB-backed heartbeat for 13 schedulers.

PK = scheduler_name；無 FK；純 ops 表。每次 scheduler tick 結尾 UPDATE
last_success_at；/health/schedulers 用 expected_interval_seconds 算 lag。

In-memory metrics (utils/scheduler_observability) 仍保留作 per-process 觀測；
DB heartbeat 解決 process restart 丟失問題。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base


class SchedulerHeartbeat(Base):
    __tablename__ = "scheduler_heartbeats"

    scheduler_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    last_rows_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

Modify `models/__init__.py` to add:
```python
from models.scheduler_heartbeat import SchedulerHeartbeat  # noqa: F401
```

- [ ] **Step 3: Run test, verify pass**

Run: `pytest tests/test_scheduler_heartbeat_model.py -v` → PASS

- [ ] **Step 4: Commit**

```bash
git add models/scheduler_heartbeat.py models/__init__.py tests/test_scheduler_heartbeat_model.py
git commit -m "feat(models): SchedulerHeartbeat ORM for DB-backed scheduler heartbeat"
```

### Task 1.2 — Alembic migration `schedhb01`

**Files:**
- Create: `alembic/versions/<rev>_schedhb01_scheduler_heartbeats.py`

- [ ] **Step 1: 確認當前 head**

```bash
alembic heads
```

Note revision id（assume `XXXX`）作為 `down_revision`。

- [ ] **Step 2: Generate migration manually**

`alembic/versions/schedhb01_scheduler_heartbeats.py`:

```python
"""scheduler_heartbeats table + seed 13 rows

Revision ID: schedhb01
Revises: <當前 head>
Create Date: 2026-05-28
"""

from alembic import op
import sqlalchemy as sa

revision = "schedhb01"
down_revision = "<當前 head>"  # 替換為 Step 1 拿到的 id
branch_labels = None
depends_on = None


SCHEDULER_INTERVALS = {
    "activity_waitlist": 300,
    "medication_reminder": 300,
    "graduation": 3600,
    "salary_snapshot": 86400,
    "official_calendar": 86400,
    "finance_reconciliation": 86400,
    "recruitment_term_advance": 86400,
    "pii_retention": 86400,
    "security_gc": 86400,
    "leave_quota_expiry": 3600,
    "line_token_health": 86400,
    "notification_retry": 300,
    "pending_uploads": 300,
}


def upgrade() -> None:
    op.create_table(
        "scheduler_heartbeats",
        sa.Column("scheduler_name", sa.String(64), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error_message", sa.Text, nullable=True),
        sa.Column("expected_interval_seconds", sa.Integer, nullable=False),
        sa.Column("last_rows_processed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    for name, interval in SCHEDULER_INTERVALS.items():
        op.execute(
            sa.text(
                "INSERT INTO scheduler_heartbeats "
                "(scheduler_name, expected_interval_seconds, consecutive_failures, last_rows_processed, updated_at) "
                "VALUES (:n, :i, 0, 0, NOW())"
            ).bindparams(n=name, i=interval)
        )


def downgrade() -> None:
    op.drop_table("scheduler_heartbeats")
```

- [ ] **Step 3: Verify single head**

```bash
alembic heads
```

Expected: 1 head (`schedhb01`).

- [ ] **Step 4: Test upgrade & downgrade**

```bash
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/schedhb01_scheduler_heartbeats.py
git commit -m "feat(db): schedhb01 scheduler_heartbeats table + seed 13 rows"
```

### Task 1.3 — 擴充 `utils/scheduler_observability.py` 加 DB persist

**Files:**
- Modify: `utils/scheduler_observability.py:126-173`
- Test: `tests/test_scheduler_heartbeat_persist.py`

- [ ] **Step 1: Write failing test**

```python
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from utils.scheduler_observability import scheduler_iteration, reset_for_tests


def test_scheduler_iteration_persists_success(db_session):
    reset_for_tests()
    # Seed row
    from models.scheduler_heartbeat import SchedulerHeartbeat
    db_session.add(SchedulerHeartbeat(scheduler_name="test_sched", expected_interval_seconds=300))
    db_session.commit()

    with scheduler_iteration("test_sched", expected_interval_seconds=300):
        pass  # success path

    db_session.expire_all()
    row = db_session.query(SchedulerHeartbeat).filter_by(scheduler_name="test_sched").one()
    assert row.last_success_at is not None
    assert row.consecutive_failures == 0


def test_scheduler_iteration_persists_failure(db_session):
    reset_for_tests()
    from models.scheduler_heartbeat import SchedulerHeartbeat
    db_session.add(SchedulerHeartbeat(scheduler_name="fail_sched", expected_interval_seconds=300))
    db_session.commit()

    try:
        with scheduler_iteration("fail_sched", expected_interval_seconds=300):
            raise ValueError("boom")
    except ValueError:
        pass  # iteration swallows by design — but defensive

    db_session.expire_all()
    row = db_session.query(SchedulerHeartbeat).filter_by(scheduler_name="fail_sched").one()
    assert row.last_failure_at is not None
    assert row.consecutive_failures == 1
    assert "boom" in (row.last_error_message or "")


def test_scheduler_iteration_db_write_failure_swallowed():
    """DB UPDATE 失敗時 scheduler loop 不應中斷。"""
    reset_for_tests()
    with patch("utils.scheduler_observability._persist_heartbeat", side_effect=Exception("db down")):
        with scheduler_iteration("any", expected_interval_seconds=300):
            pass  # no exception bubble
```

Run: `pytest tests/test_scheduler_heartbeat_persist.py -v` → FAIL

- [ ] **Step 2: Extend scheduler_observability.py**

Modify `utils/scheduler_observability.py`:

```python
# Add new import at top
from contextlib import contextmanager
from typing import Iterator

# Existing _MetricsStore unchanged
# Existing scheduler_iteration signature:
@contextmanager
def scheduler_iteration(
    scheduler_name: str,
    expected_interval_seconds: int | None = None,  # NEW kwarg
) -> Iterator[None]:
    """既有 in-memory metrics + 新增 DB heartbeat persist。

    expected_interval_seconds：若 caller 不傳，僅更新 in-memory；傳了則同時 UPSERT
    scheduler_heartbeats DB row。DB 寫失敗會 swallow（log warning），不影響 loop。
    """
    stats = _METRICS.get_or_create(scheduler_name)
    with _METRICS._lock:
        stats.total_runs += 1
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        now = datetime.now(timezone.utc)
        with _METRICS._lock:
            stats.consecutive_failures += 1
            stats.total_failures += 1
            stats.last_failure_at = now
            stats.last_error_message = f"{type(exc).__name__}: {exc}"[:500]
            failure_count = stats.consecutive_failures
        if failure_count >= ALERT_THRESHOLD:
            logger.warning("%s 連續 %d 次失敗，上報 Sentry: %s", scheduler_name, failure_count, exc, exc_info=True)
            capture_exception(exc, level="error")
        else:
            logger.warning("%s 第 %d 次失敗（<%d 暫不上報）: %s", scheduler_name, failure_count, ALERT_THRESHOLD, exc)
        if expected_interval_seconds is not None:
            _persist_heartbeat(scheduler_name, success=False, error_message=stats.last_error_message,
                               expected_interval_seconds=expected_interval_seconds)
    else:
        now = datetime.now(timezone.utc)
        with _METRICS._lock:
            stats.consecutive_failures = 0
            stats.last_success_at = now
            stats.last_error_message = None
        if expected_interval_seconds is not None:
            _persist_heartbeat(scheduler_name, success=True, error_message=None,
                               expected_interval_seconds=expected_interval_seconds)


def _persist_heartbeat(scheduler_name: str, success: bool, error_message: str | None,
                       expected_interval_seconds: int) -> None:
    """獨立 transaction 寫 scheduler_heartbeats row；失敗 swallow。"""
    try:
        from models.base import get_session
        from models.scheduler_heartbeat import SchedulerHeartbeat
        with get_session() as s:
            row = s.query(SchedulerHeartbeat).filter_by(scheduler_name=scheduler_name).one_or_none()
            now = datetime.now(timezone.utc)
            if row is None:
                row = SchedulerHeartbeat(
                    scheduler_name=scheduler_name,
                    expected_interval_seconds=expected_interval_seconds,
                )
                s.add(row)
            if success:
                row.last_success_at = now
                row.consecutive_failures = 0
                row.last_error_message = None
            else:
                row.last_failure_at = now
                row.consecutive_failures = (row.consecutive_failures or 0) + 1
                row.last_error_message = (error_message or "")[:1000]
            s.commit()
    except Exception:  # noqa: BLE001
        logger.warning("scheduler_heartbeat DB persist failed for %s", scheduler_name, exc_info=True)
```

注：若 `models.base.get_session` 不存在/簽章不同，subagent 需查 codebase 找對應 session helper（通常是 `models.base.SessionLocal` 或 `get_db_session`）。

- [ ] **Step 3: Run test, verify pass**

Run: `pytest tests/test_scheduler_heartbeat_persist.py -v` → PASS

- [ ] **Step 4: Run existing scheduler_observability tests, verify no regression**

Run: `pytest tests/ -k scheduler -v`

- [ ] **Step 5: Commit**

```bash
git add utils/scheduler_observability.py tests/test_scheduler_heartbeat_persist.py
git commit -m "feat(observability): scheduler_iteration DB persist + expected_interval_seconds kwarg"
```

### Task 1.4 — 8 scheduler 改造（每 scheduler 一 commit）

For each scheduler in this list, do TDD round (test that scheduler tick UPDATEs heartbeat row):

| Scheduler | File | Interval |
|-----------|------|----------|
| graduation | `services/graduation_scheduler.py` | 3600 |
| official_calendar | `services/official_calendar_scheduler.py` | 86400 |
| finance_reconciliation | `services/finance_reconciliation_scheduler.py` | 86400 |
| security_gc | `services/security_gc_scheduler.py` | 86400 (or actual) |
| leave_quota_expiry | `services/leave_quota_expiry_scheduler.py` | 3600 |
| line_token_health | `services/line_token_health_scheduler.py` | 86400 |
| notification_retry | `services/notification/retry_scheduler.py` | 300 |
| pending_uploads | `services/notification/pending_uploads_scheduler.py` | 300 |

For 5 schedulers already using `scheduler_iteration` (`activity_waitlist`, `medication_reminder`, `salary_snapshot`, `pii_retention`, `recruitment_term_advance`)，只需加 `expected_interval_seconds=N` kwarg。

- [ ] **Step 1: For each of 8 schedulers, wrap tick with `scheduler_iteration`**

Pattern：找 scheduler 主迴圈，把既有 `try/except + logger.exception` 區塊改為：

```python
# Before
while not stop_event.is_set():
    try:
        await some_tick_logic()
    except Exception:
        logger.exception("graduation scheduler tick failed")
    await asyncio.sleep(GRADUATION_INTERVAL_SECONDS)

# After
from utils.scheduler_observability import scheduler_iteration

GRADUATION_INTERVAL_SECONDS = 3600

while not stop_event.is_set():
    with scheduler_iteration("graduation", expected_interval_seconds=GRADUATION_INTERVAL_SECONDS):
        await some_tick_logic()
    await asyncio.sleep(GRADUATION_INTERVAL_SECONDS)
```

注意：原 `try/except` 整段被 `with` 取代。`scheduler_iteration` 內部已 catch + log + Sentry throttle，loop 不會被 raise 中斷。

- [ ] **Step 2: For 5 already-instrumented schedulers, add kwarg**

`services/activity_waitlist_scheduler.py:72`：
```python
# Before
with scheduler_iteration("activity_waitlist"):
# After
with scheduler_iteration("activity_waitlist", expected_interval_seconds=300):
```

同 pattern 改 `salary_snapshot`、`pii_retention`、`medication_reminder`、`recruitment_term_advance`。

- [ ] **Step 3: Per-scheduler regression test**

Run narrow tests for each scheduler:
```bash
pytest tests/ -k "activity_waitlist or graduation or salary_snapshot or ..." -v
```

Expected: all green.

- [ ] **Step 4: Commit each scheduler separately**

13 個小 commit（每 scheduler 一個）：
```bash
git add services/graduation_scheduler.py
git commit -m "feat(observability): graduation scheduler 接 scheduler_iteration DB heartbeat"
# ... 重複 12 次
```

### Task 1.5 — `GET /health/schedulers` endpoint

**Files:**
- Modify: `api/health.py`
- Test: `tests/api/test_health_schedulers.py`

- [ ] **Step 1: Write failing test**

```python
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from models.scheduler_heartbeat import SchedulerHeartbeat


def test_schedulers_health_all_green(client, db_session):
    now = datetime.now(timezone.utc)
    db_session.add(SchedulerHeartbeat(
        scheduler_name="sched_a", expected_interval_seconds=300,
        last_success_at=now - timedelta(seconds=60),
    ))
    db_session.commit()

    r = client.get("/health/schedulers")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert len(data["schedulers"]) == 1


def test_schedulers_health_lagging(client, db_session):
    now = datetime.now(timezone.utc)
    db_session.add(SchedulerHeartbeat(
        scheduler_name="sched_b", expected_interval_seconds=300,
        last_success_at=now - timedelta(seconds=900),  # > 2 * 300
    ))
    db_session.commit()

    r = client.get("/health/schedulers")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "degraded"
    assert len(data["lagging"]) == 1
    assert data["lagging"][0]["name"] == "sched_b"


def test_schedulers_health_never_ran_not_lagging(client, db_session):
    db_session.add(SchedulerHeartbeat(
        scheduler_name="sched_c", expected_interval_seconds=300,
        last_success_at=None,
    ))
    db_session.commit()

    r = client.get("/health/schedulers")
    assert r.status_code == 200
```

Run: `pytest tests/api/test_health_schedulers.py -v` → FAIL

- [ ] **Step 2: Implement endpoint**

Modify `api/health.py` — add at bottom:

```python
from datetime import datetime, timezone

from fastapi.responses import JSONResponse

from models.base import get_session
from models.scheduler_heartbeat import SchedulerHeartbeat


@router.get("/schedulers")
async def schedulers_health():
    """檢查所有 scheduler heartbeat lag。

    無權限（UptimeRobot 公開可打）。
    200 = 全綠；503 = 至少一個 scheduler lag > 2 × expected_interval。
    啟動後尚未跑過（last_success_at IS NULL）的視為「未滿足 lag 條件」，回 200。
    """
    now = datetime.now(timezone.utc)
    schedulers = []
    lagging = []
    with get_session() as s:
        rows = s.query(SchedulerHeartbeat).all()
        for row in rows:
            if row.last_success_at is None:
                lag_seconds = None
                is_lagging = False
            else:
                lag_seconds = (now - row.last_success_at).total_seconds()
                is_lagging = lag_seconds > 2 * row.expected_interval_seconds
            item = {
                "name": row.scheduler_name,
                "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
                "lag_seconds": lag_seconds,
                "expected_interval_seconds": row.expected_interval_seconds,
                "consecutive_failures": row.consecutive_failures,
            }
            schedulers.append(item)
            if is_lagging:
                lagging.append(item)
    if lagging:
        return JSONResponse(
            status_code=503,
            content={"status": "degraded", "lagging": lagging, "schedulers": schedulers},
        )
    return {"status": "ok", "schedulers": schedulers}
```

- [ ] **Step 3: Run test, verify pass**

Run: `pytest tests/api/test_health_schedulers.py -v` → PASS

- [ ] **Step 4: Commit**

```bash
git add api/health.py tests/api/test_health_schedulers.py
git commit -m "feat(health): GET /health/schedulers endpoint with lag detection"
```

### Task 1.6 — `POST /api/internal/uptime-webhook` endpoint

**Files:**
- Create: `api/internal/uptime_webhook.py`
- Modify: `main.py` (include_router)
- Modify: `config/ops.py` (add `UPTIME_ROBOT_WEBHOOK_TOKEN` env)
- Test: `tests/api/internal/test_uptime_webhook.py`

- [ ] **Step 1: Write failing test**

```python
from unittest.mock import patch


def test_uptime_webhook_invalid_token(client):
    r = client.post("/api/internal/uptime-webhook?token=wrong", json={})
    assert r.status_code == 401


def test_uptime_webhook_alert_push(client, monkeypatch):
    monkeypatch.setenv("UPTIME_ROBOT_WEBHOOK_TOKEN", "secret")
    monkeypatch.setenv("OPS_ALERT_LINE_GROUP_ID", "Cxxx")
    payload = {
        "monitorFriendlyName": "/health/ready",
        "alertType": "1",  # 1=down, 2=up
        "alertDetails": "Connection timeout",
    }
    with patch("services.line_service.LineService.push_text_to_group") as mock_push:
        r = client.post("/api/internal/uptime-webhook?token=secret", json=payload)
        assert r.status_code == 200
        mock_push.assert_called_once()
        msg = mock_push.call_args[0][1]
        assert "/health/ready" in msg
        assert "宕機" in msg or "down" in msg.lower()
```

Run: `pytest tests/api/internal/test_uptime_webhook.py -v` → FAIL

- [ ] **Step 2: Implement endpoint**

Create `api/internal/uptime_webhook.py`:

```python
"""UptimeRobot webhook receiver — push alert 到 OPS_ALERT_LINE_GROUP_ID。

UptimeRobot 設定 alert contact 為此 endpoint URL，token 用 query param 驗證
（UptimeRobot 不支援 custom header in free tier）。
"""

import logging

from fastapi import APIRouter, HTTPException, Request

from config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/internal", tags=["internal"])


@router.post("/uptime-webhook")
async def uptime_webhook(token: str, request: Request):
    settings = get_settings()
    expected_token = settings.ops.uptime_robot_webhook_token
    if not expected_token or token != expected_token:
        raise HTTPException(status_code=401, detail="invalid token")

    payload = await request.json()
    monitor = payload.get("monitorFriendlyName", "<unknown>")
    alert_type = payload.get("alertType", "")  # "1"=down, "2"=up
    details = payload.get("alertDetails", "")

    if alert_type == "1":
        msg = f"⚠️ 監控告警：{monitor} 宕機\n細節：{details}"
    elif alert_type == "2":
        msg = f"✅ 監控恢復：{monitor} 已上線"
    else:
        msg = f"ℹ️ 監控更新：{monitor}\n細節：{details}"

    group_id = settings.ops.ops_alert_line_group_id
    if not group_id:
        logger.warning("OPS_ALERT_LINE_GROUP_ID 未設定，跳過 LINE push")
        return {"status": "skipped"}

    try:
        from services.line_service import LineService
        line_service = LineService()
        line_service.push_text_to_group(group_id, msg)
    except Exception:
        logger.exception("LINE push failed in uptime webhook")
        return {"status": "push_failed"}
    return {"status": "ok"}
```

Modify `main.py` to add:
```python
from api.internal import uptime_webhook as _uptime_webhook
app.include_router(_uptime_webhook.router)
```

Modify `config/ops.py` (or `config/_settings.py`) — add field:
```python
class OpsSettings(BaseModel):
    # ... 既有
    uptime_robot_webhook_token: str | None = None  # NEW
```

- [ ] **Step 3: Run test, verify pass**

Run: `pytest tests/api/internal/test_uptime_webhook.py -v` → PASS

- [ ] **Step 4: Commit**

```bash
git add api/internal/uptime_webhook.py main.py config/ops.py tests/api/internal/test_uptime_webhook.py
git commit -m "feat(ops): uptime-webhook receiver pushes to OPS_ALERT_LINE_GROUP_ID"
```

### Task 1.7 — UptimeRobot 設定 runbook

**Files:**
- Create: `docs/sop/uptime-monitor-setup.md`

- [ ] **Step 1: Write runbook**

`docs/sop/uptime-monitor-setup.md`:

```markdown
# UptimeRobot 監控設定 SOP

**目的**：5 分鐘內偵測系統宕機 / scheduler 連環失敗，自動推播 LINE 告警群。

**前置條件**：
- prod 已部署 `/health/ready`、`/health/schedulers`、`/api/internal/uptime-webhook`
- prod env 已設 `UPTIME_ROBOT_WEBHOOK_TOKEN`（建議 `openssl rand -hex 16`）+ `OPS_ALERT_LINE_GROUP_ID`

## 步驟

### 1. 註冊 UptimeRobot 免費帳號

https://uptimerobot.com/signUp — 免費版可建 50 monitor / 最短 1 分鐘間隔（5 分鐘為下限）。

### 2. 加 Monitor 1：`/health/ready`

- Monitor Type: HTTP(s)
- Friendly Name: `Ivy /health/ready`
- URL: `https://<prod-domain>/health/ready`
- Monitoring Interval: 1 minute（付費）或 5 minutes（免費）
- Expected: HTTP 200

### 3. 加 Monitor 2：`/health/schedulers`

同上，URL 改為 `/health/schedulers`，Interval 5 分鐘即可（scheduler lag 容忍時間長）。

### 4. 設定 Alert Contact — LINE Webhook

- My Settings → Alert Contacts → Add Alert Contact
- Type: Webhook
- Friendly Name: `Ivy LINE Group`
- URL to Notify: `https://<prod-domain>/api/internal/uptime-webhook?token=<UPTIME_ROBOT_WEBHOOK_TOKEN 的值>`
- POST Value (JSON): UptimeRobot 預設變數即可，不需自訂
- Apply contact to all monitors

### 5. 加 Email Alert Contact（備援）

Type: Email；填入接收信箱（建議 dev/ops 群組信箱）。

### 6. 整合驗證

1. 在 prod 暫停一個 scheduler（例：env `MEDICATION_REMINDER_ENABLED=0` 後重啟）
2. 等 ~10 分鐘（2 × tick interval）讓 lag > threshold
3. UptimeRobot 應觸發 `/health/schedulers` 503 告警
4. 預期：LINE 群收到 `⚠️ 監控告警：Ivy /health/schedulers 宕機...`
5. 恢復 env，驗證收到 `✅ 監控恢復`

## 故障排除

| 症狀 | 可能原因 | 處置 |
|------|---------|------|
| LINE 告警未收到 | token 不符 | 比對 prod env vs UptimeRobot webhook URL |
| 5xx 重複告警 | UptimeRobot 預設每分鐘重試 | 在 UptimeRobot 設 retry 5 次再告警 |
| 啟動後 scheduler 短暫顯示 lag | 第一次 tick 尚未跑 | 預期；spec 設計上 `last_success_at IS NULL` 不告警 |
```

- [ ] **Step 2: Commit**

```bash
git add docs/sop/uptime-monitor-setup.md
git commit -m "docs(sop): UptimeRobot setup runbook"
```

### Task 1.8 — KillSwitch bypass 預留 `/health/schedulers` 路徑

**注意**：此 task 為 BE-B 接 BE-A 後的銜接點。BE-A merge 後，BE-B 的 bypass list 必含 `/health/schedulers`、`/api/internal/uptime-webhook`。BE-A 不直接動 BE-B 程式碼；只標記在 BE-A PR description。

### Phase 1 完成檢查

- [ ] `scheduler_heartbeats` 表存在 + 13 row seed
- [ ] `utils/scheduler_observability.py` 接 DB persist + `expected_interval_seconds` kwarg
- [ ] 13 scheduler 全用 `scheduler_iteration` + interval kwarg
- [ ] `/health/schedulers` 端點 200/503 邏輯正確
- [ ] `/api/internal/uptime-webhook` 受 token 保護 + 推 LINE
- [ ] `docs/sop/uptime-monitor-setup.md` 完成
- [ ] `pytest` 全綠（既有 + 新加 ≥ 8 test）

---

## Phase 2 — BE-B: Maintenance / Read-Only Kill Switch

**Worktree**：新建（base origin/main）

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git worktree add .claude/worktrees/feat-kill-switch-2026-05-28-backend -b feat/kill-switch-maintenance-readonly-2026-05-28-backend origin/main
cd .claude/worktrees/feat-kill-switch-2026-05-28-backend
```

### Task 2.1 — 加 env field 到 OpsSettings

**Files:**
- Modify: `config/ops.py`
- Test: `tests/config/test_ops_settings.py`

- [ ] **Step 1: Write failing test**

```python
def test_maintenance_mode_default_off(monkeypatch):
    monkeypatch.delenv("MAINTENANCE_MODE", raising=False)
    from config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.ops.maintenance_mode is False
    assert s.ops.read_only_mode is False
    assert s.ops.maintenance_message  # has default


def test_maintenance_mode_env_on(monkeypatch):
    monkeypatch.setenv("MAINTENANCE_MODE", "1")
    monkeypatch.setenv("MAINTENANCE_MESSAGE", "升級中")
    from config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    assert s.ops.maintenance_mode is True
    assert s.ops.maintenance_message == "升級中"
```

Run: `pytest tests/config/test_ops_settings.py -v` → FAIL

- [ ] **Step 2: Add fields**

Modify `config/ops.py`:

```python
class OpsSettings(BaseModel):
    # ... 既有欄位
    maintenance_mode: bool = Field(default=False, alias="MAINTENANCE_MODE")
    read_only_mode: bool = Field(default=False, alias="READ_ONLY_MODE")
    maintenance_message: str = Field(
        default="系統維護中，請稍後再試",
        alias="MAINTENANCE_MESSAGE",
    )
```

確保 alias 正確映射 env；若 config 用 pydantic-settings 的 `env_prefix`，調整。

- [ ] **Step 3: Run test, verify pass**

Run: `pytest tests/config/test_ops_settings.py -v` → PASS

- [ ] **Step 4: Commit**

```bash
git add config/ops.py tests/config/test_ops_settings.py
git commit -m "feat(config): add MAINTENANCE_MODE / READ_ONLY_MODE / MAINTENANCE_MESSAGE env"
```

### Task 2.2 — 實作 `KillSwitchMiddleware`

**Files:**
- Create: `utils/kill_switch.py`
- Test: `tests/test_kill_switch.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils.kill_switch import KillSwitchMiddleware


def _make_app(maintenance=False, read_only=False, message="維護中"):
    app = FastAPI()
    app.add_middleware(KillSwitchMiddleware)

    @app.get("/test")
    async def t():
        return {"ok": True}

    @app.post("/test")
    async def t_post():
        return {"ok": True}

    @app.get("/health/live")
    async def l():
        return {"alive": True}

    # Patch settings
    from config import get_settings
    get_settings.cache_clear()

    class _Ops:
        maintenance_mode = maintenance
        read_only_mode = read_only
        maintenance_message = message

    class _Settings:
        ops = _Ops()
    import config
    config.get_settings = lambda: _Settings()
    return app


def test_maintenance_blocks_get():
    app = _make_app(maintenance=True)
    r = TestClient(app).get("/test")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "MAINTENANCE_MODE"
    assert r.json()["detail"]["message"] == "維護中"
    assert r.headers["retry-after"] == "300"


def test_maintenance_bypasses_health_live():
    app = _make_app(maintenance=True)
    r = TestClient(app).get("/health/live")
    assert r.status_code == 200


def test_read_only_blocks_post():
    app = _make_app(read_only=True)
    r = TestClient(app).post("/test")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "READ_ONLY_MODE"


def test_read_only_allows_get():
    app = _make_app(read_only=True)
    r = TestClient(app).get("/test")
    assert r.status_code == 200


def test_normal_passes_through():
    app = _make_app()
    r = TestClient(app).get("/test")
    assert r.status_code == 200
```

Run: `pytest tests/test_kill_switch.py -v` → FAIL

- [ ] **Step 2: Implement KillSwitchMiddleware**

`utils/kill_switch.py`:

```python
"""KillSwitchMiddleware — env-driven maintenance / read-only 503 短路。

env-only：避開事故時 DB 可能掛；zeabur dashboard 直接 flip env 即生效。

Bypass paths：
- /health/{live,ready,schedulers} — UptimeRobot 仍能監控
- /api/internal/uptime-webhook    — UptimeRobot 告警仍能進來
- /auth/login, /auth/refresh      — admin 緊急進入
"""

from __future__ import annotations

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from config import get_settings


class KillSwitchMiddleware(BaseHTTPMiddleware):
    BYPASS_PATHS = frozenset({
        "/health/live",
        "/health/ready",
        "/health/schedulers",
        "/api/internal/uptime-webhook",
        "/auth/login",
        "/auth/refresh",
    })

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.BYPASS_PATHS:
            return await call_next(request)

        settings = get_settings()
        ops = settings.ops

        if ops.maintenance_mode:
            return _maintenance_response("MAINTENANCE_MODE", ops.maintenance_message)
        if ops.read_only_mode and request.method not in ("GET", "HEAD", "OPTIONS"):
            return _maintenance_response("READ_ONLY_MODE", "系統暫時唯讀，編輯功能暫不可用")
        return await call_next(request)


def _maintenance_response(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": {"message": message, "code": code, "retry_after": 300}},
        headers={"Retry-After": "300"},
    )
```

- [ ] **Step 3: Run tests, verify pass**

Run: `pytest tests/test_kill_switch.py -v` → PASS

- [ ] **Step 4: Commit**

```bash
git add utils/kill_switch.py tests/test_kill_switch.py
git commit -m "feat(ops): KillSwitchMiddleware env-driven maintenance/read-only 503"
```

### Task 2.3 — main.py 註冊 middleware

**Files:**
- Modify: `main.py` (around line 927-953)

- [ ] **Step 1: Write integration test**

```python
def test_main_kill_switch_integration(monkeypatch):
    monkeypatch.setenv("MAINTENANCE_MODE", "1")
    # Force reload main
    from importlib import reload
    import main
    reload(main)

    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.get("/api/employees")  # any non-bypass endpoint
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "MAINTENANCE_MODE"

    # Bypass still works
    r = client.get("/health/live")
    assert r.status_code == 200
```

Run → FAIL

- [ ] **Step 2: Modify main.py middleware stack**

`main.py` around line 927-953:

```python
# Audit 仍最內層
from utils.audit import AuditMiddleware
app.add_middleware(AuditMiddleware)

# NEW: KillSwitch in Audit 之後 add（wrapper Audit），所以 maintenance 不寫 audit log
from utils.kill_switch import KillSwitchMiddleware
app.add_middleware(KillSwitchMiddleware)

from utils.security_headers import SecurityHeadersMiddleware
app.add_middleware(SecurityHeadersMiddleware)

# ... 其餘不變
```

- [ ] **Step 3: Run integration test, verify pass**

- [ ] **Step 4: Commit**

```bash
git add main.py tests/test_main_kill_switch_integration.py
git commit -m "feat(main): wire KillSwitchMiddleware into stack (after AuditMiddleware)"
```

### Task 2.4 — Regression：跑全 backend pytest

- [ ] **Step 1: Full test suite**

```bash
pytest tests/ -x --tb=short 2>&1 | tail -30
```

Expected: 全綠或 baseline 既有 fail 不增加。

- [ ] **Step 2: Push PR**

```bash
git push origin feat/kill-switch-maintenance-readonly-2026-05-28-backend
# 開 PR 至 origin/main
```

### Phase 2 完成檢查

- [ ] 3 個新 env 已 register
- [ ] Middleware 註冊位置正確（Audit 後 / SecurityHeaders 前）
- [ ] Bypass list 6 條全驗
- [ ] Envelope shape: `{detail: {message, code, retry_after}}`
- [ ] `Retry-After: 300` header
- [ ] 零回歸

---

## Phase 3 — BE-C: Parent BusinessError 漸進升級（~50 處）

**Worktree**：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git worktree add .claude/worktrees/feat-parent-business-error-2026-05-28-backend -b feat/parent-business-error-2026-05-28-backend origin/main
cd .claude/worktrees/feat-parent-business-error-2026-05-28-backend
```

### Task 3.1 — `ErrorCode` enum registry

**Files:**
- Create: `utils/error_codes.py`
- Test: `tests/test_error_codes.py`

- [ ] **Step 1: Write failing test**

```python
def test_error_code_enum():
    from utils.error_codes import ErrorCode
    assert ErrorCode.BIND_CODE_INVALID.value == "BIND_CODE_INVALID"
    assert ErrorCode.LINE_BINDING_EXPIRED.value == "LINE_BINDING_EXPIRED"
    assert ErrorCode.STUDENT_NOT_FOUND.value == "STUDENT_NOT_FOUND"


def test_error_code_no_duplicates():
    from utils.error_codes import ErrorCode
    values = [e.value for e in ErrorCode]
    assert len(values) == len(set(values))
```

- [ ] **Step 2: Implement**

`utils/error_codes.py`:

```python
"""ErrorCode registry — 集中註冊家長端 BusinessError code，避免 typo。

家長端 frontend errorCodeRegistry.ts 須與此清單對齊（手動同步；
follow-up codegen 可從 OpenAPI 抓 discriminator）。
"""

from enum import Enum


class ErrorCode(str, Enum):
    # 家長綁定流程
    BIND_CODE_INVALID = "BIND_CODE_INVALID"
    BIND_CODE_EXPIRED = "BIND_CODE_EXPIRED"
    BIND_CODE_ALREADY_USED = "BIND_CODE_ALREADY_USED"

    # LIFF 認證
    LINE_BINDING_EXPIRED = "LINE_BINDING_EXPIRED"
    LINE_BINDING_NOT_FOUND = "LINE_BINDING_NOT_FOUND"
    LINE_PROFILE_FETCH_FAILED = "LINE_PROFILE_FETCH_FAILED"

    # 家長存取資源
    STUDENT_NOT_FOUND = "STUDENT_NOT_FOUND"
    STUDENT_NOT_LINKED_TO_PARENT = "STUDENT_NOT_LINKED_TO_PARENT"
    PORTAL_DATA_UNAVAILABLE = "PORTAL_DATA_UNAVAILABLE"
    CONTACT_BOOK_NOT_PUBLISHED = "CONTACT_BOOK_NOT_PUBLISHED"

    # 家長端通用
    CONSENT_REQUIRED = "CONSENT_REQUIRED"
    DSR_REQUEST_INVALID = "DSR_REQUEST_INVALID"
    PARENT_NOT_AUTHORIZED = "PARENT_NOT_AUTHORIZED"
```

- [ ] **Step 3: Commit**

```bash
git add utils/error_codes.py tests/test_error_codes.py
git commit -m "feat(errors): ErrorCode enum registry for parent BusinessError"
```

### Task 3.2 — `services/business_errors/parent.py` BusinessError subclasses

**Files:**
- Create: `services/business_errors/__init__.py`
- Create: `services/business_errors/parent.py`
- Test: `tests/services/test_business_errors_parent.py`

- [ ] **Step 1: Write failing test**

```python
from services.business_errors.parent import (
    BindCodeInvalid, LineBindingExpired, StudentNotFound,
)


def test_bind_code_invalid_envelope():
    err = BindCodeInvalid()
    assert err.code == "BIND_CODE_INVALID"
    assert err.status_code == 400
    assert err.message == "綁定碼無效或已過期"


def test_bind_code_invalid_custom_message():
    err = BindCodeInvalid("自訂訊息")
    assert err.message == "自訂訊息"


def test_business_error_raises_envelope_via_handler(client, monkeypatch):
    """整合：raise BusinessError → handler 回 envelope shape。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services.business_errors.parent import BindCodeInvalid
    from utils.exception_handlers import register_handlers

    app = FastAPI()
    register_handlers(app)  # 既有 P0c 落地的 handler

    @app.get("/raise")
    async def r():
        raise BindCodeInvalid()

    c = TestClient(app)
    r = c.get("/raise")
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["code"] == "BIND_CODE_INVALID"
    assert body["detail"]["message"] == "綁定碼無效或已過期"
```

- [ ] **Step 2: Implement**

`services/business_errors/__init__.py`：
```python
"""Domain-specific BusinessError subclasses."""
```

`services/business_errors/parent.py`:

```python
"""家長端 BusinessError subclasses — 使用既有 utils/exception_handlers envelope。

每個 subclass 帶定義好的 code、status_code、default_message。
新增 family 時請參考 ErrorCode enum 已註冊的 code。
"""

from utils.error_codes import ErrorCode
from utils.exception_handlers import BusinessError


class _ParentBusinessError(BusinessError):
    """Base — 家長端 BusinessError 共通父類別（純語意）。"""
    pass


class BindCodeInvalid(_ParentBusinessError):
    code = ErrorCode.BIND_CODE_INVALID.value
    status_code = 400
    default_message = "綁定碼無效或已過期"


class BindCodeExpired(_ParentBusinessError):
    code = ErrorCode.BIND_CODE_EXPIRED.value
    status_code = 400
    default_message = "綁定碼已過期，請重新取得"


class BindCodeAlreadyUsed(_ParentBusinessError):
    code = ErrorCode.BIND_CODE_ALREADY_USED.value
    status_code = 409
    default_message = "此綁定碼已被使用"


class LineBindingExpired(_ParentBusinessError):
    code = ErrorCode.LINE_BINDING_EXPIRED.value
    status_code = 401
    default_message = "您的綁定已過期，請重新登入"


class LineBindingNotFound(_ParentBusinessError):
    code = ErrorCode.LINE_BINDING_NOT_FOUND.value
    status_code = 404
    default_message = "找不到綁定資料，請重新綁定"


class LineProfileFetchFailed(_ParentBusinessError):
    code = ErrorCode.LINE_PROFILE_FETCH_FAILED.value
    status_code = 502
    default_message = "無法取得 LINE 個人資料，請稍後再試"


class StudentNotFound(_ParentBusinessError):
    code = ErrorCode.STUDENT_NOT_FOUND.value
    status_code = 404
    default_message = "找不到對應的學生資料"


class StudentNotLinkedToParent(_ParentBusinessError):
    code = ErrorCode.STUDENT_NOT_LINKED_TO_PARENT.value
    status_code = 403
    default_message = "您無權存取此學生資料"


class PortalDataUnavailable(_ParentBusinessError):
    code = ErrorCode.PORTAL_DATA_UNAVAILABLE.value
    status_code = 404
    default_message = "資料暫時無法存取"


class ContactBookNotPublished(_ParentBusinessError):
    code = ErrorCode.CONTACT_BOOK_NOT_PUBLISHED.value
    status_code = 404
    default_message = "本日聯絡簿尚未發布"


class ConsentRequired(_ParentBusinessError):
    code = ErrorCode.CONSENT_REQUIRED.value
    status_code = 403
    default_message = "請先完成同意聲明後再使用"


class DsrRequestInvalid(_ParentBusinessError):
    code = ErrorCode.DSR_REQUEST_INVALID.value
    status_code = 400
    default_message = "資料請求內容無效"


class ParentNotAuthorized(_ParentBusinessError):
    code = ErrorCode.PARENT_NOT_AUTHORIZED.value
    status_code = 403
    default_message = "您沒有權限執行此操作"
```

- [ ] **Step 3: Run test, verify pass**

- [ ] **Step 4: Commit**

```bash
git add services/business_errors/ tests/services/test_business_errors_parent.py
git commit -m "feat(errors): ParentBusinessError subclasses + envelope contract test"
```

### Task 3.3-3.10 — 升級 8 個 router

Each task targets one router file. TDD pattern:

1. Find inline `raise HTTPException(...)` in target router
2. For each call site that matches a known parent error, replace with `BusinessError` subclass
3. Add test that hits the endpoint, asserts `response.json()["detail"]["code"] == "EXPECTED_CODE"`
4. Commit

**Target router 清單**：

| Task | Router File | 估計處數 |
|------|-------------|---------|
| 3.3 | `api/parent/bind.py` | 8 處 |
| 3.4 | `api/parent/auth.py` | 6 處 |
| 3.5 | `api/parent/me.py`（DSR/consent）| 5 處 |
| 3.6 | `api/portal/contact_book.py` | 4 處 |
| 3.7 | `api/portal/student.py` | 5 處 |
| 3.8 | `api/portal/health.py` 家長端 | 4 處 |
| 3.9 | `api/auth.py` LIFF 區塊 | ~10 處（搜 `liff_` / `line_user_id` 上下文） |
| 3.10 | `api/parent/` 其他剩餘 | ~5 處 |

**每 task pattern**（subagent 重複套用）：

- [ ] **Step 1: 列出 router 內所有 `raise HTTPException`**

```bash
grep -n "raise HTTPException" api/parent/bind.py
```

- [ ] **Step 2: 對每個 call site，按語意對應 BusinessError**

範例：
```python
# Before
if not bind_code or bind_code.is_expired():
    raise HTTPException(status_code=400, detail="綁定碼無效或已過期")

# After
from services.business_errors.parent import BindCodeInvalid
if not bind_code or bind_code.is_expired():
    raise BindCodeInvalid()
```

對應 mapping：
- "綁定碼無效" → `BindCodeInvalid`
- "綁定碼已過期" → `BindCodeExpired`
- "此綁定碼已被使用" → `BindCodeAlreadyUsed`
- "綁定已過期" → `LineBindingExpired`
- "找不到綁定" → `LineBindingNotFound`
- "找不到學生" → `StudentNotFound`
- "無權存取" → `StudentNotLinkedToParent` / `ParentNotAuthorized`（看上下文）
- "尚未同意" → `ConsentRequired`
- "聯絡簿尚未發布" → `ContactBookNotPublished`

不確定的 case 保留原 HTTPException（不強拆）。

- [ ] **Step 3: 加 narrow test**

```python
def test_bind_endpoint_returns_BIND_CODE_INVALID(client):
    r = client.post("/api/parent/bind", json={"bind_code": "invalid"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "BIND_CODE_INVALID"
```

- [ ] **Step 4: 跑該 router 既有 test，確認無回歸**

```bash
pytest tests/api/parent/test_bind.py -v
```

如有既有 test 比對 `r.json()["detail"] == "綁定碼無效或已過期"` 字串，須改為比對 `r.json()["detail"]["message"] == "綁定碼無效或已過期"`。

- [ ] **Step 5: Commit per router**

```bash
git add api/parent/bind.py tests/api/parent/test_bind.py
git commit -m "feat(parent): bind.py inline HTTPException → BusinessError subclasses"
```

### Task 3.11 — Phase 3 全 regression

- [ ] **Step 1: 全套件**

```bash
pytest tests/ -x --tb=short 2>&1 | tail -30
```

Expected: baseline fail 不增加；新加 ≥ 10 test 全綠。

### Phase 3 完成檢查

- [ ] `utils/error_codes.py` enum 註冊 14 個 code
- [ ] `services/business_errors/parent.py` 14 個 subclass
- [ ] 8 個 router 升級完成（~50 處）
- [ ] 非家長端 router（admin / portal teacher / hr）未動
- [ ] 既有 router test 已調整 detail shape 比對

---

## Phase 4 — FE-A: MaintenanceView + 503 Redirect

**Worktree**：

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git fetch origin main
git worktree add .claude/worktrees/feat-maintenance-view-2026-05-28-frontend -b feat/maintenance-view-503-redirect-2026-05-28-frontend origin/main
cd .claude/worktrees/feat-maintenance-view-2026-05-28-frontend
```

### Task 4.1 — `views/MaintenanceView.vue`（管理端）

**Files:**
- Create: `src/views/MaintenanceView.vue`
- Test: `tests/views/MaintenanceView.test.js`

- [ ] **Step 1: Write failing test**

```javascript
import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import MaintenanceView from '@/views/MaintenanceView.vue'

describe('MaintenanceView', () => {
  it('shows default message when no message prop', () => {
    const w = mount(MaintenanceView, { global: { plugins: [ElementPlus] } })
    expect(w.text()).toContain('系統維護中')
  })

  it('shows custom message from query', () => {
    const w = mount(MaintenanceView, {
      props: { message: '升級中，預計 23:00 完成' },
      global: { plugins: [ElementPlus] },
    })
    expect(w.text()).toContain('升級中')
  })

  it('clicking refresh probes /health/ready', async () => {
    const mockGet = vi.fn().mockResolvedValue({ status: 200 })
    vi.mock('@/api', () => ({ default: { get: mockGet } }))
    const w = mount(MaintenanceView, { global: { plugins: [ElementPlus] } })
    await w.find('button').trigger('click')
    expect(mockGet).toHaveBeenCalledWith('/health/ready')
  })
})
```

Run: `vitest run tests/views/MaintenanceView.test.js` → FAIL

- [ ] **Step 2: Implement**

`src/views/MaintenanceView.vue`:

```vue
<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import api from '@/api'

interface Props {
  message?: string
}
const props = withDefaults(defineProps<Props>(), {
  message: '系統維護中，請稍後再試',
})

const refreshing = ref(false)

async function tryRefresh() {
  refreshing.value = true
  try {
    const r = await api.get('/health/ready')
    if (r.status === 200) {
      window.location.reload()
    }
  } catch (_) {
    ElMessage.warning('仍在維護中，請稍後再試')
  } finally {
    refreshing.value = false
  }
}
</script>

<template>
  <div class="maintenance-view">
    <div class="maintenance-card">
      <h1>🛠️ 系統維護中</h1>
      <p>{{ props.message }}</p>
      <el-button :loading="refreshing" type="primary" @click="tryRefresh">
        重新整理
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.maintenance-view {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: #f5f7fa;
}
.maintenance-card {
  text-align: center;
  padding: 48px;
  background: white;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
}
h1 { font-size: 2rem; margin-bottom: 16px; }
p { color: #606266; margin-bottom: 24px; }
</style>
```

- [ ] **Step 3: Run test, verify pass**

- [ ] **Step 4: Add route in `src/router/index.ts`**

```typescript
{
  path: '/maintenance',
  name: 'Maintenance',
  component: () => import('@/views/MaintenanceView.vue'),
  meta: { public: true, hideNav: true },
}
```

- [ ] **Step 5: Commit**

```bash
git add src/views/MaintenanceView.vue src/router/index.ts tests/views/MaintenanceView.test.js
git commit -m "feat(maintenance): admin MaintenanceView + /maintenance route"
```

### Task 4.2 — `parent/views/MaintenanceView.vue`（家長端）

**Files:**
- Create: `src/parent/views/MaintenanceView.vue`
- Modify: `src/parent/router/index.ts`

- [ ] **Step 1: Implement parent MaintenanceView**

`src/parent/views/MaintenanceView.vue` — 風格較簡潔 LIFF-friendly（mobile）：

```vue
<script setup lang="ts">
import { ref } from 'vue'
import parentApi from '@/parent/api'

interface Props {
  message?: string
}
const props = withDefaults(defineProps<Props>(), {
  message: '系統維護中，請稍後再回來',
})

const refreshing = ref(false)
async function tryRefresh() {
  refreshing.value = true
  try {
    const r = await parentApi.get('/health/ready')
    if (r.status === 200) window.location.reload()
  } catch (_) {
    // silent
  } finally {
    refreshing.value = false
  }
}
</script>

<template>
  <div class="parent-maintenance">
    <div class="card">
      <div class="emoji">🐳</div>
      <h2>系統升級中</h2>
      <p>{{ props.message }}</p>
      <button :disabled="refreshing" @click="tryRefresh">
        {{ refreshing ? '檢查中...' : '重新載入' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.parent-maintenance {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(180deg, #e0f7fa 0%, #ffffff 100%);
  padding: 24px;
}
.card {
  background: white;
  border-radius: 16px;
  padding: 32px 24px;
  text-align: center;
  box-shadow: 0 4px 12px rgba(0,0,0,0.08);
  max-width: 360px;
}
.emoji { font-size: 4rem; margin-bottom: 16px; }
h2 { font-size: 1.5rem; margin-bottom: 12px; color: #00838f; }
p { color: #546e7a; line-height: 1.6; margin-bottom: 24px; }
button {
  background: #00acc1;
  color: white;
  border: none;
  padding: 12px 32px;
  border-radius: 24px;
  font-size: 1rem;
  cursor: pointer;
}
button:disabled { opacity: 0.5; }
</style>
```

Add route in `src/parent/router/index.ts`：
```typescript
{ path: '/parent/maintenance', component: () => import('@/parent/views/MaintenanceView.vue'), meta: { public: true } }
```

- [ ] **Step 2: Test + commit**

### Task 4.3 — admin axios interceptor 加 503 + MAINTENANCE_MODE redirect

**Files:**
- Modify: `src/api/index.ts:88-97`

- [ ] **Step 1: Write failing test**

```javascript
import { describe, it, expect, vi } from 'vitest'

describe('axios interceptor maintenance redirect', () => {
  it('503 + code=MAINTENANCE_MODE redirects to /maintenance', async () => {
    const mockReplace = vi.fn()
    vi.mock('@/router', () => ({ default: { replace: mockReplace } }))
    const apiModule = await import('@/api')
    // simulate error
    const error = {
      response: { status: 503, data: { detail: { message: '維護中', code: 'MAINTENANCE_MODE' } } },
    }
    // trigger interceptor manually (extract from module)
    // ...subagent 視 codebase 實作位置調整
    // expect: mockReplace called with '/maintenance'
  })
})
```

- [ ] **Step 2: Modify interceptor**

`src/api/index.ts` 在處理 displayMessage 之前加 503 redirect：

```typescript
const status = error.response?.status
const rawDetail = error.response?.data?.detail

if (status === 503 && rawDetail && typeof rawDetail === 'object' && rawDetail.code === 'MAINTENANCE_MODE') {
  // 不重複 redirect（避免 maintenance view 內呼叫 API 失敗又 redirect）
  if (router.currentRoute.value.path !== '/maintenance') {
    router.replace({
      path: '/maintenance',
      query: { message: rawDetail.message },
    })
  }
  return Promise.reject(error)
}

if (status === 503 && rawDetail && typeof rawDetail === 'object' && rawDetail.code === 'READ_ONLY_MODE') {
  ElMessage.warning(rawDetail.message || '系統暫時唯讀')
  return Promise.reject(error)
}

// 既有 displayMessage 邏輯不變
```

- [ ] **Step 3: Test + commit**

### Task 4.4 — parent axios interceptor 同樣處理

**Files:**
- Modify: `src/parent/api/index.ts:144-158`

- [ ] **Step 1: Mirror admin interceptor changes for parent**

差異：redirect 到 `/parent/maintenance`、用 parent router instance。

- [ ] **Step 2: Test + commit**

### Task 4.5 — MaintenanceView 元件接 `message` query param

**Files:**
- Modify: 兩個 MaintenanceView

讓 `?message=...` query 能覆寫 props.message：

```typescript
import { useRoute } from 'vue-router'
const route = useRoute()
const displayMessage = computed(() => 
  (route.query.message as string) || props.message
)
```

模板用 `displayMessage` 取代 `props.message`。

### Task 4.6 — Phase 4 regression

```bash
npm run typecheck
npm run test  # vitest
npm run build  # 確認沒 typescript / import 錯誤
```

### Phase 4 完成檢查

- [ ] `MaintenanceView.vue`（admin + parent）+ 路由
- [ ] 兩端 interceptor 對 503 + code 做 redirect / warning
- [ ] `vitest` + `typecheck` + `build` 全綠
- [ ] 手測：後端設 `MAINTENANCE_MODE=1` 重啟 → 兩端 LIFF / 後台都 redirect

---

## Phase 5 — FE-B: 5xx / Network Friendly Fallback

**Worktree**：

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git worktree add .claude/worktrees/feat-friendly-error-fallback-2026-05-28-frontend -b feat/friendly-error-fallback-2026-05-28-frontend origin/main
```

### Task 5.1 — 擴充 `DEFAULT_MESSAGES`

**Files:**
- Modify: `src/utils/errorHandler.ts`
- Test: `tests/utils/errorHandler.test.js`

- [ ] **Step 1: Write failing test**

```javascript
import { describe, it, expect } from 'vitest'
import { classifyError, DEFAULT_MESSAGES } from '@/utils/errorHandler'

describe('errorHandler', () => {
  it('5xx classification', () => {
    expect(classifyError({ response: { status: 500 } })).toBe('SERVER_ERROR')
    expect(classifyError({ response: { status: 503 } })).toBe('SERVER_ERROR')
  })

  it('network error classification', () => {
    expect(classifyError({ message: 'Network Error' })).toBe('NETWORK_ERROR')
    expect(classifyError({ code: 'ECONNABORTED' })).toBe('TIMEOUT')
  })

  it('DEFAULT_MESSAGES 含友善 5xx 與 network', () => {
    expect(DEFAULT_MESSAGES.SERVER_ERROR).toMatch(/服務.*無法/)
    expect(DEFAULT_MESSAGES.NETWORK_ERROR).toMatch(/網路.*異常/)
  })
})
```

- [ ] **Step 2: Modify errorHandler.ts**

```typescript
export const DEFAULT_MESSAGES = {
  SERVER_ERROR: '服務暫時無法使用，請稍後再試。若持續發生請聯絡園所',
  NETWORK_ERROR: '網路連線異常，請檢查網路後重試',
  TIMEOUT: '伺服器回應逾時，請稍後再試',
  UNAUTHORIZED: '請重新登入',
  // ... 既有
}

export enum ErrorType {
  SERVER_ERROR = 'SERVER_ERROR',
  NETWORK_ERROR = 'NETWORK_ERROR',
  TIMEOUT = 'TIMEOUT',
  // ... 既有
}

export function classifyError(error: any): ErrorType {
  if (!error.response) {
    if (error.code === 'ECONNABORTED') return ErrorType.TIMEOUT
    return ErrorType.NETWORK_ERROR
  }
  const status = error.response.status
  if (status >= 500) return ErrorType.SERVER_ERROR
  if (status === 401) return ErrorType.UNAUTHORIZED
  // ... 既有
}
```

- [ ] **Step 3: Commit**

### Task 5.2 — 更新 axios interceptor displayMessage 優先序

**Files:**
- Modify: `src/api/index.ts:88-97`
- Modify: `src/parent/api/index.ts:144-158`

- [ ] **Step 1: 兩端 interceptor 同步加 fallback**

```typescript
const friendly = classifyError(error)
const fallbackMessage = DEFAULT_MESSAGES[friendly] || null

if (rawDetail && typeof rawDetail === 'object' && rawDetail.message) {
  error.displayMessage = rawDetail.message as string
  error.errorDetail = rawDetail
} else if (typeof rawDetail === 'string' && rawDetail) {
  error.displayMessage = rawDetail
  error.errorDetail = null
} else {
  error.displayMessage = fallbackMessage  // ← KEY CHANGE: 不再 null
  error.errorDetail = null
}
```

- [ ] **Step 2: Test**

```javascript
it('5xx no detail → fallback to SERVER_ERROR message', () => {
  const error = { response: { status: 500 } }
  // run interceptor
  // expect error.displayMessage === DEFAULT_MESSAGES.SERVER_ERROR
})

it('network error → NETWORK_ERROR message', () => {
  const error = { message: 'Network Error' }
  // expect error.displayMessage === DEFAULT_MESSAGES.NETWORK_ERROR
})
```

- [ ] **Step 3: Commit**

### Task 5.3 — Phase 5 regression

```bash
npm run test && npm run typecheck && npm run build
```

### Phase 5 完成檢查

- [ ] `DEFAULT_MESSAGES.SERVER_ERROR/NETWORK_ERROR/TIMEOUT` 友善文字
- [ ] 兩端 interceptor 對 5xx + no detail fallback
- [ ] `vitest` 全綠

---

## Phase 6 — FE-C: useFriendlyError + errorCodeRegistry

**依賴**：Phase 3 (BE-C) merged。需 backend 已部署回傳 `code` 欄位。

**Worktree**：

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git worktree add .claude/worktrees/feat-friendly-error-codes-2026-05-28-frontend -b feat/friendly-error-codes-2026-05-28-frontend origin/main
```

### Task 6.1 — `src/utils/errorCodeRegistry.ts`

**Files:**
- Create: `src/utils/errorCodeRegistry.ts`
- Test: `tests/utils/errorCodeRegistry.test.js`

- [ ] **Step 1: Write failing test**

```javascript
import { describe, it, expect } from 'vitest'
import { ERROR_CODE_REGISTRY } from '@/utils/errorCodeRegistry'

describe('errorCodeRegistry', () => {
  it('家長綁定 codes 全列', () => {
    expect(ERROR_CODE_REGISTRY.BIND_CODE_INVALID.nextStep).toBeTruthy()
    expect(ERROR_CODE_REGISTRY.LINE_BINDING_EXPIRED.nextStep).toBeTruthy()
    expect(ERROR_CODE_REGISTRY.STUDENT_NOT_FOUND.message).toBeTruthy()
  })
})
```

- [ ] **Step 2: Implement**

`src/utils/errorCodeRegistry.ts`:

```typescript
/**
 * 家長端 error code → 顯示訊息 + next-step hint 註冊表。
 *
 * 與後端 utils/error_codes.py ErrorCode enum 對齊（手動同步；
 * 後續 follow-up codegen 可自動化）。
 */

export interface FriendlyError {
  message: string
  nextStep?: string
  level?: 'error' | 'warning' | 'info'
}

export const ERROR_CODE_REGISTRY: Record<string, FriendlyError> = {
  // 綁定流程
  BIND_CODE_INVALID: {
    message: '綁定碼無效或已過期',
    nextStep: '請至 LINE 主選單重新取得綁定碼，或聯絡園所協助',
    level: 'warning',
  },
  BIND_CODE_EXPIRED: {
    message: '綁定碼已過期',
    nextStep: '請聯絡園所重新產生新的綁定碼',
    level: 'warning',
  },
  BIND_CODE_ALREADY_USED: {
    message: '此綁定碼已被使用',
    nextStep: '如非您本人操作請立即聯絡園所',
    level: 'warning',
  },

  // LIFF 認證
  LINE_BINDING_EXPIRED: {
    message: '您的綁定已過期',
    nextStep: '請點選下方「重新綁定」，或聯絡園所重新發送邀請',
    level: 'warning',
  },
  LINE_BINDING_NOT_FOUND: {
    message: '找不到您的綁定資料',
    nextStep: '請聯絡園所建立綁定',
    level: 'error',
  },
  LINE_PROFILE_FETCH_FAILED: {
    message: '無法取得 LINE 個人資料',
    nextStep: '請稍後再試，或重新開啟 LIFF 頁面',
    level: 'warning',
  },

  // 家長存取
  STUDENT_NOT_FOUND: {
    message: '找不到對應的學生資料',
    nextStep: '請確認綁定的學生仍在學；如有疑問請聯絡園所',
    level: 'error',
  },
  STUDENT_NOT_LINKED_TO_PARENT: {
    message: '您無權存取此學生資料',
    nextStep: '請聯絡園所確認綁定關係',
    level: 'error',
  },
  PORTAL_DATA_UNAVAILABLE: {
    message: '資料暫時無法存取',
    nextStep: '請稍後再試；持續發生請聯絡園所',
    level: 'warning',
  },
  CONTACT_BOOK_NOT_PUBLISHED: {
    message: '今日聯絡簿尚未發布',
    nextStep: '老師通常於下午 5 點前發布；若有疑問請聯絡老師',
    level: 'info',
  },

  // 同意 / DSR
  CONSENT_REQUIRED: {
    message: '請先完成同意聲明',
    nextStep: '點選下方「我同意」按鈕後即可使用',
    level: 'info',
  },
  DSR_REQUEST_INVALID: {
    message: '資料請求內容無效',
    nextStep: '請重新檢查表單欄位後再送出',
    level: 'warning',
  },
  PARENT_NOT_AUTHORIZED: {
    message: '您沒有權限執行此操作',
    nextStep: '如有疑問請聯絡園所',
    level: 'error',
  },
}
```

- [ ] **Step 3: Commit**

### Task 6.2 — `useFriendlyError` composable

**Files:**
- Create: `src/composables/useFriendlyError.ts`
- Test: `tests/composables/useFriendlyError.test.js`

- [ ] **Step 1: Write failing test**

```javascript
import { describe, it, expect } from 'vitest'
import { useFriendlyError } from '@/composables/useFriendlyError'

describe('useFriendlyError', () => {
  const { getFriendly } = useFriendlyError()

  it('已知 code → message + nextStep', () => {
    const err = {
      errorDetail: { code: 'BIND_CODE_INVALID', message: '綁定碼無效或已過期' },
      displayMessage: '綁定碼無效或已過期',
    }
    const f = getFriendly(err as any)
    expect(f.message).toBe('綁定碼無效或已過期')
    expect(f.nextStep).toMatch(/LINE 主選單/)
    expect(f.level).toBe('warning')
  })

  it('未知 code → fallback to displayMessage', () => {
    const err = { errorDetail: { code: 'UNKNOWN_X' }, displayMessage: '其他錯誤' }
    const f = getFriendly(err as any)
    expect(f.message).toBe('其他錯誤')
    expect(f.nextStep).toBeUndefined()
  })

  it('無 errorDetail → fallback to displayMessage', () => {
    const err = { displayMessage: '網路連線異常' }
    const f = getFriendly(err as any)
    expect(f.message).toBe('網路連線異常')
  })
})
```

- [ ] **Step 2: Implement**

`src/composables/useFriendlyError.ts`:

```typescript
import { ERROR_CODE_REGISTRY, type FriendlyError } from '@/utils/errorCodeRegistry'
import type { AxiosError } from 'axios'

interface ApiErrorDetail {
  message?: string
  code?: string
  [key: string]: unknown
}

interface ApiError extends AxiosError {
  displayMessage?: string | null
  errorDetail?: ApiErrorDetail | null
}

export function useFriendlyError() {
  function getFriendly(error: ApiError): FriendlyError {
    const detail = error.errorDetail
    const code = detail?.code

    if (code && ERROR_CODE_REGISTRY[code]) {
      const registry = ERROR_CODE_REGISTRY[code]
      return {
        message: detail.message || registry.message,
        nextStep: registry.nextStep,
        level: registry.level,
      }
    }

    return {
      message: error.displayMessage || '發生未預期的錯誤',
      level: 'error',
    }
  }

  return { getFriendly }
}
```

- [ ] **Step 3: Commit**

### Task 6.3-6.7 — 改造家長端關鍵元件用 `useFriendlyError`

對家長端高流量元件做改造：

| Task | Component | 對應錯誤情境 |
|------|-----------|------------|
| 6.3 | `parent/views/BindStudentView.vue` | 綁定流程錯誤 |
| 6.4 | `parent/views/HomeView.vue` | LINE binding expired |
| 6.5 | `parent/views/ContactBookView.vue` | 聯絡簿存取錯誤 |
| 6.6 | `parent/views/PrivacyRightsView.vue` | DSR 請求錯誤 |
| 6.7 | `parent/views/ConsentView.vue` | Consent required |

**每 task pattern**：

- [ ] **Step 1: 找元件 catch 區塊**

```typescript
// Before
try {
  await api.post(...)
} catch (err) {
  ElMessage.error(err.displayMessage || '發生錯誤')
}
```

- [ ] **Step 2: 改用 useFriendlyError**

```vue
<script setup lang="ts">
import { useFriendlyError } from '@/composables/useFriendlyError'

const { getFriendly } = useFriendlyError()
const errorState = ref<FriendlyError | null>(null)

async function submit() {
  try {
    await api.post(...)
  } catch (err) {
    errorState.value = getFriendly(err as any)
  }
}
</script>

<template>
  <el-alert
    v-if="errorState"
    :type="errorState.level || 'error'"
    :title="errorState.message"
    :closable="false"
  >
    <template v-if="errorState.nextStep" #default>
      <div class="next-step">💡 {{ errorState.nextStep }}</div>
    </template>
  </el-alert>
</template>
```

- [ ] **Step 3: Test + commit per component**

### Task 6.8 — Phase 6 regression

```bash
npm run test && npm run typecheck && npm run build
```

### Phase 6 完成檢查

- [ ] `errorCodeRegistry.ts` 14 條 code
- [ ] `useFriendlyError` composable
- [ ] 5 個家長端關鍵元件改用 friendly error
- [ ] 手測：dev 後端觸發 `BIND_CODE_INVALID` → 家長端顯示 message + 💡 nextStep

---

## 全 Project 完成檢查 / Cross-Phase Validation

執行於所有 PR merged 後（user 操作）：

- [ ] prod `alembic upgrade head` 成功（schedhb01）
- [ ] prod env 已設 `UPTIME_ROBOT_WEBHOOK_TOKEN`、保留 `MAINTENANCE_MODE=0` / `READ_ONLY_MODE=0`
- [ ] UptimeRobot 已設 2 個 monitor + 1 個 LINE webhook
- [ ] 手測 1：暫停 `medication_reminder` env → 10 分鐘後收到 LINE 告警 → 恢復收到回復通知
- [ ] 手測 2：設 `MAINTENANCE_MODE=1` 5 分鐘 → 家長 LIFF + 後台都 redirect 到維護頁
- [ ] 手測 3：家長端 dev 觸發 `BIND_CODE_INVALID` → 看到 nextStep
- [ ] 手測 4：dev mock 500 response → 看到「服務暫時無法使用」而非「Request failed with status code 500」

---

## Out of Scope / Follow-up

- 全 codebase 1200+ 處 inline HTTPException 統一改 envelope（churn vs 收益不成正比）
- errorCodeRegistry codegen from OpenAPI（手動同步 14 條先 work）
- DB-backed maintenance config + UI（env-only 已 cover P0）
- Prometheus / Grafana metrics 接 `/health/schedulers`
- 多 worker `scheduler_heartbeat` DB 寫衝突（已有 advisory_lock 保證單 worker run scheduler，此情境不發生）

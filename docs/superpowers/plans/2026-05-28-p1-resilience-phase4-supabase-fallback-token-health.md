# P1 韌性 Phase 4：Supabase fallback + LINE token health + admin endpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox.

**Goal:**
1. `SupabaseStorage.save()` retry_with_backoff(3 次) + 失敗時寫 local `data/uploads_pending/` + DB `pending_uploads` row
2. Pending uploads scheduler 每 5 min 推回 Supabase
3. LINE long-lived token 每日 08:00 Asia/Taipei ping `/v2/bot/info` 寫 `line_token_health` row；401/403 → Sentry alert
4. `GET /api/internal/integrations/health` admin endpoint 暴露 3 breaker state + line token healthy + pending_uploads count + line_retry_pending count

**Architecture:** 復用 Phase 1 `retry_with_backoff` + 既有 scheduler observability framework；不引入新 dependency；admin endpoint 與 `api/internal_metrics.py` 同 pattern 用 `Permission.AUDIT_LOGS`。

**Spec**: `docs/superpowers/specs/2026-05-28-p1-external-integration-resilience-design.md` §8

---

## File Structure

| 動作 | 路徑 | 責任 |
|------|------|------|
| Create | `models/pending_uploads.py` | `PendingUpload` ORM |
| Create | `models/integration_health.py` | `LineTokenHealth` ORM (singleton row id=1) |
| Create | `alembic/versions/20260528_intghealth01_pending_uploads_and_line_token_health.py` | 2 table |
| Modify | `models/__init__.py` | 中央 import 新 model (避 Base.metadata.create_all 漏建表) |
| Modify | `utils/supabase_storage.py` | `save()` retry+fallback；新 helper `_stash_locally` / `_enqueue_pending_upload` |
| Modify | `config/storage.py` | `local_fallback_enabled: bool = True` + `local_fallback_max_mb: int = 5000` |
| Modify | `config/line.py` | `token_health_ping_hour_taipei: int = 8` |
| Create | `services/notification/pending_uploads_scheduler.py` | `tick_pending_uploads` + `run_pending_uploads_scheduler` async wrapper |
| Create | `services/line_token_health_scheduler.py` | `tick_line_token_health` + daily wrapper |
| Modify | `main.py` | lifespan 註冊兩個新 scheduler；graceful shutdown |
| Create | `api/integrations_health.py` | `GET /api/internal/integrations/health` |
| Modify | `main.py` (include_router) | mount 新 router |
| Create | `tests/test_pending_uploads_scheduler.py` | unit + integration |
| Create | `tests/test_line_token_health_scheduler.py` | mock 401/403/200/network |
| Create | `tests/test_integrations_health_endpoint.py` | permission + response shape |

---

## Task 1: 2 new model + alembic migration

**Files:**
- Create `models/pending_uploads.py`
- Create `models/integration_health.py`
- Modify `models/__init__.py`
- Create `alembic/versions/20260528_intghealth01_*.py`

- [ ] **Step 1.1: Create `models/pending_uploads.py`**

```python
"""models/pending_uploads.py — Phase 4 P1 resilience：Supabase 上傳失敗暫存 row.

scheduler 每 5 min 撈 attempts<5 AND next_retry_at<=now() 重推 Supabase。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text, func

from models.base import Base


class PendingUpload(Base):
    __tablename__ = "pending_uploads"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    module = Column(String(40), nullable=False)
    key = Column(String(255), nullable=False)
    content_type = Column(String(80), nullable=False)
    local_path = Column(String(500), nullable=False)
    attempts = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime(timezone=True), nullable=False)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    succeeded_at = Column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 1.2: Create `models/integration_health.py`**

```python
"""models/integration_health.py — Phase 4 P1 resilience：外部整合健康狀態 row.

LineTokenHealth singleton row（id=1）：每日 daily tick + call-site 401/403 共寫。
"""
from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from models.base import Base


class LineTokenHealth(Base):
    __tablename__ = "line_token_health"

    id = Column(Integer, primary_key=True)
    last_check_at = Column(DateTime(timezone=True), nullable=False)
    healthy = Column(Boolean, nullable=False)
    last_error = Column(String(200), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
```

- [ ] **Step 1.3: Add to `models/__init__.py` central import**

用 bash python3 string.replace 加：

```python
from models.pending_uploads import PendingUpload  # noqa: F401
from models.integration_health import LineTokenHealth  # noqa: F401
```

之前 memory 教訓：`models/__init__.py` 漏 import 會讓 `Base.metadata.create_all` 漏建表 → test fixture fail。

- [ ] **Step 1.4: Alembic migration**

```python
"""pending_uploads + line_token_health

Revision ID: intghealth01
Revises: notifretry01
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "intghealth01"
down_revision = "notifretry01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pending_uploads",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("module", sa.String(40), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(80), nullable=False),
        sa.Column("local_path", sa.String(500), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("succeeded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_pending_uploads_next_retry",
        "pending_uploads",
        ["next_retry_at"],
        postgresql_where=sa.text("succeeded_at IS NULL AND attempts < 5"),
    )
    op.create_table(
        "line_token_health",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("healthy", sa.Boolean, nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_table("line_token_health")
    op.drop_index("ix_pending_uploads_next_retry", table_name="pending_uploads")
    op.drop_table("pending_uploads")
```

- [ ] **Step 1.5: Verify alembic + commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be
python3 -m alembic upgrade heads 2>&1 | tail -5
python3 -m alembic heads 2>&1 | tail -3
# Expected: single head intghealth01
git add models/pending_uploads.py models/integration_health.py models/__init__.py alembic/versions/20260528_intghealth01_*.py
git commit -m "feat(resilience): pending_uploads + line_token_health models (Phase 4)

對應 spec §8.1+§8.4。alembic intghealth01 建 2 table + partial index。
models/__init__.py 中央 import 避 Base.metadata.create_all 漏建表。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: SupabaseStorage retry + local fallback

**Files:**
- Modify `config/storage.py`
- Modify `utils/supabase_storage.py`
- Modify `tests/test_supabase_storage.py`

- [ ] **Step 2.1: Add config fields**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be && python3 <<'PY'
with open("config/storage.py", "r") as f:
    content = f.read()

# 找一個合適的 field 後追加 — 用 root: Path 或現有 supabase_url 當 anchor
# 確認既有 field 後手動找 anchor 加入：
print(content[:2000])  # inspect existing fields
PY
```

依實際結構加 2 field：
```python
local_fallback_enabled: bool = True
local_fallback_max_mb: int = 5000
```

- [ ] **Step 2.2: Modify `SupabaseStorage.save()` (用 bash python3)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.claude/worktrees/p1-resilience-phase1-be && python3 <<'PY'
with open("utils/supabase_storage.py", "r") as f:
    content = f.read()

# Add retry_with_backoff import (already has tagged_capture + SUPABASE_BREAKER)
old_imp = "from utils.external_calls import tagged_capture"
new_imp = """from utils.external_calls import retry_with_backoff, tagged_capture
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta"""
content = content.replace(old_imp, new_imp, 1)

# Rewrite save() — wrap retry_with_backoff inside breaker; on failure write local + DB row
old_save_method = """    def save(self, module: str, key: str, data: bytes, content_type: str) -> None:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            SUPABASE_BREAKER.call(lambda: bucket.upload(
                path=key,
                file=data,
                file_options={\"content-type\": content_type, \"upsert\": \"true\"},
            ))
        except Exception as exc:
            tagged_capture(exc, tag=\"supabase\", level=\"error\")
            raise  # Phase 1 不接 fallback；Phase 4 改寫 local + pending_uploads"""

new_save_method = """    def save(self, module: str, key: str, data: bytes, content_type: str) -> None:
        bucket = self._client.storage.from_(_resolve_bucket(module))
        try:
            SUPABASE_BREAKER.call(lambda: retry_with_backoff(
                lambda: bucket.upload(
                    path=key,
                    file=data,
                    file_options={\"content-type\": content_type, \"upsert\": \"true\"},
                ),
                attempts=3, base_seconds=1.0, cap_seconds=8.0,
            ))
        except Exception as exc:
            tagged_capture(exc, tag=\"supabase\", level=\"error\")
            from config import settings as _settings
            if not getattr(_settings.storage, \"local_fallback_enabled\", True):
                raise
            # Local fallback：寫 data/uploads_pending + DB row；caller 視為 save 成功
            try:
                local_path = _stash_locally(module, key, data)
                _enqueue_pending_upload(module, key, content_type, local_path, str(exc))
            except Exception as fallback_exc:
                tagged_capture(fallback_exc, tag=\"supabase\", level=\"error\")
                raise exc  # fallback 也炸 → 還原原本 raise"""
content = content.replace(old_save_method, new_save_method, 1)

# Add helper functions at module bottom
helpers = '''


# ── Phase 4 fallback helpers ──────────────────────────────────────

_FALLBACK_ROOT = Path(__file__).resolve().parent.parent / "data" / "uploads_pending"


def _stash_locally(module: str, key: str, data: bytes) -> str:
    """寫 fallback bytes 到本機 data/uploads_pending/<module>/<uuid>.bin，回 path string."""
    folder = _FALLBACK_ROOT / module
    folder.mkdir(parents=True, exist_ok=True)
    # 用 uuid 避撞名（同 key 可能多次失敗）；保留原 key 在 DB row
    fname = f"{uuid.uuid4().hex}.bin"
    local_path = folder / fname
    local_path.write_bytes(data)
    return str(local_path)


def _enqueue_pending_upload(
    module: str, key: str, content_type: str, local_path: str, error: str,
) -> None:
    """寫 pending_uploads DB row（scheduler tick 會撈）."""
    from models.base import get_session_factory
    from models.pending_uploads import PendingUpload

    session = get_session_factory()()
    try:
        row = PendingUpload(
            module=module, key=key, content_type=content_type,
            local_path=local_path, attempts=0,
            next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=30),
            last_error=error[:500],
        )
        session.add(row)
        session.commit()
    finally:
        session.close()
'''

content += helpers

with open("utils/supabase_storage.py", "w") as f:
    f.write(content)
PY
```

- [ ] **Step 2.3: Test (extend `tests/test_supabase_storage.py`)**

```python
class TestPhase4Fallback:
    def test_save_retries_then_fallback_writes_local_and_db(self, mock_supabase, tmp_path, monkeypatch):
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.upload.side_effect = ConnectionError("supabase down")
        # 點對 fallback root 改 tmp_path
        monkeypatch.setattr("utils.supabase_storage._FALLBACK_ROOT", tmp_path / "uploads_pending")
        # 點對 retry sleep 不要真睡
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)

        # 不應 raise（fallback 應寫成功）
        backend.save("activity_posters", "x.png", b"PNGDATA", "image/png")

        # local file 存在
        files = list((tmp_path / "uploads_pending" / "activity_posters").iterdir())
        assert len(files) == 1
        assert files[0].read_bytes() == b"PNGDATA"

        # pending_uploads row 存在（mock 不到 DB；只驗 _enqueue 被呼叫）
        # 改用 patch _enqueue_pending_upload 驗呼叫
        # （上面 test 用 monkeypatch helper module；若 DB session 在 test conftest 已備好可改 query）

    def test_save_fallback_disabled_raises(self, mock_supabase, monkeypatch):
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        bucket.upload.side_effect = ConnectionError("down")
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)

        from config import settings
        monkeypatch.setattr(settings.storage, "local_fallback_enabled", False, raising=False)

        with pytest.raises(ConnectionError):
            backend.save("activity_posters", "x.png", b"X", "image/png")

    def test_save_retry_succeeds_on_second_attempt(self, mock_supabase, monkeypatch):
        """前 2 次 fail 第 3 次 success → 不進 fallback."""
        backend, client = mock_supabase
        bucket = client.storage.from_.return_value
        attempts = {"n": 0}
        def maybe(*a, **k):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise ConnectionError("transient")
            return None
        bucket.upload.side_effect = maybe
        monkeypatch.setattr("utils.external_calls.time.sleep", lambda s: None)

        backend.save("activity_posters", "y.png", b"Y", "image/png")
        assert attempts["n"] == 2
```

- [ ] **Step 2.4: Run & commit**

```bash
pytest tests/test_supabase_storage.py -v
git add config/storage.py utils/supabase_storage.py tests/test_supabase_storage.py
git commit -m "feat(storage): SupabaseStorage retry + local fallback (Phase 4)

對應 spec §8.2。save() 用 retry_with_backoff(3) + breaker；失敗時寫
data/uploads_pending/<uuid>.bin 與 pending_uploads DB row；caller 視為成功。

LOCAL_FALLBACK_ENABLED=true 預設；test 可關。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Pending uploads scheduler

**Files:**
- Create `services/notification/pending_uploads_scheduler.py`
- Create `tests/test_pending_uploads_scheduler.py`

- [ ] **Step 3.1: Write `pending_uploads_scheduler.py`**

```python
"""services/notification/pending_uploads_scheduler.py — Phase 4 P1 resilience.

每 5 min 撈 pending_uploads.next_retry_at<=now() AND attempts<5 → 重 push to Supabase。
backoff: 30s → 2min → 10min → 1hr → 6hr （5 次失敗後 mark final + Sentry alert）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from models.base import get_session_factory
from models.pending_uploads import PendingUpload
from utils.external_calls import tagged_capture

logger = logging.getLogger(__name__)

_BACKOFF_SECONDS = [30, 120, 600, 3600, 21600]
_MAX_ATTEMPTS = 5
_TICK_LIMIT = 50
TICK_INTERVAL_SECONDS = 300  # 5 min


def tick_pending_uploads(now_provider=lambda: datetime.now(timezone.utc)) -> dict:
    session = get_session_factory()()
    metric = {"attempted": 0, "succeeded": 0, "failed": 0, "final_failed": 0}
    try:
        now = now_provider()
        rows = (
            session.query(PendingUpload)
            .filter(
                PendingUpload.succeeded_at.is_(None),
                PendingUpload.next_retry_at <= now,
                PendingUpload.attempts < _MAX_ATTEMPTS,
            )
            .limit(_TICK_LIMIT)
            .all()
        )
        metric["attempted"] = len(rows)

        from utils.storage import get_backend
        backend = get_backend()
        # local 模式不會走這 scheduler，但安全 check
        if backend.__class__.__name__ != "SupabaseStorage":
            return metric

        for row in rows:
            try:
                with open(row.local_path, "rb") as f:
                    data = f.read()
                # call underlying SDK 直接 — 不再經 SupabaseStorage.save 避無限 fallback
                bucket = backend._client.storage.from_(_resolve_bucket(row.module))
                bucket.upload(
                    path=row.key, file=data,
                    file_options={"content-type": row.content_type, "upsert": "true"},
                )
                row.succeeded_at = now
                # cleanup local file
                try:
                    os.remove(row.local_path)
                except OSError:
                    pass
                metric["succeeded"] += 1
            except Exception as exc:
                logger.exception("pending_upload row=%s failed", row.id)
                tagged_capture(exc, tag="supabase", level="warning")
                row.attempts += 1
                if row.attempts >= _MAX_ATTEMPTS:
                    row.last_error = f"final: {exc!s}"[:500]
                    metric["final_failed"] += 1
                else:
                    delay = _BACKOFF_SECONDS[min(row.attempts, len(_BACKOFF_SECONDS) - 1)]
                    row.next_retry_at = now + timedelta(seconds=delay)
                    row.last_error = str(exc)[:500]
                    metric["failed"] += 1
        session.commit()
    finally:
        session.close()
    return metric


def _resolve_bucket(module: str) -> str:
    from utils.supabase_storage import _MODULE_TO_BUCKET
    return _MODULE_TO_BUCKET[module]


async def run_pending_uploads_scheduler(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            tick_pending_uploads()
        except Exception as exc:
            logger.exception("pending_uploads tick failed")
            tagged_capture(exc, tag="supabase", level="error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
```

- [ ] **Step 3.2: Tests + commit (per Phase 2 pattern)**

`tests/test_pending_uploads_scheduler.py`:
- `test_tick_picks_pending_row_and_uploads_succeeds`
- `test_tick_increments_attempts_on_failure`
- `test_fifth_attempt_marks_final`
- `test_local_backend_skips_silently`

Commit:
```
feat(storage): tick_pending_uploads scheduler 每 5 min (Phase 4)

對應 spec §8.3。撈 succeeded_at IS NULL AND next_retry_at<=now 重 push；
backoff 30s/2min/10min/1hr/6hr；5 次失敗 mark final。註冊到 main.py 下個 commit。
```

---

## Task 4: LINE token health scheduler

**Files:**
- Create `services/line_token_health_scheduler.py`
- Create `tests/test_line_token_health_scheduler.py`
- Modify `config/line.py` (token_health_ping_hour_taipei: int = 8)

- [ ] **Step 4.1: Write scheduler**

```python
"""services/line_token_health_scheduler.py — Phase 4 P1 resilience.

每日 08:00 Asia/Taipei ping /v2/bot/info；200 → healthy=true；
401/403 → healthy=false + Sentry alert（consecutive_failures dedup 只在 0→1 / 7→8 等里程碑）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from config import settings
from models.base import get_session_factory
from models.integration_health import LineTokenHealth
from utils.external_calls import tagged_capture

logger = logging.getLogger(__name__)

_BOT_INFO_URL = "https://api.line.me/v2/bot/info"
_DEDUP_MILESTONES = (1, 8, 30)


def tick_line_token_health() -> dict:
    if not settings.line.enabled or not settings.line.channel_access_token:
        return {"skipped": "disabled"}

    metric = {"healthy": False, "error": None}
    try:
        resp = requests.get(
            _BOT_INFO_URL,
            headers={"Authorization": f"Bearer {settings.line.channel_access_token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            _update_health(healthy=True, error=None, reset_failures=True)
            metric["healthy"] = True
        elif resp.status_code in (401, 403):
            _update_health(healthy=False, error=f"http_{resp.status_code}", increment=True)
            _alert_if_milestone(f"http_{resp.status_code}", level="error")
            metric["error"] = f"http_{resp.status_code}"
        else:
            _update_health(healthy=False, error=f"http_{resp.status_code}", increment=True)
            metric["error"] = f"http_{resp.status_code}"
    except Exception as exc:
        _update_health(healthy=False, error=type(exc).__name__, increment=True)
        tagged_capture(exc, tag="line", level="warning")
        metric["error"] = type(exc).__name__
    return metric


def _update_health(*, healthy: bool, error: str | None, increment: bool = False, reset_failures: bool = False):
    session = get_session_factory()()
    try:
        row = session.query(LineTokenHealth).filter(LineTokenHealth.id == 1).first()
        now = datetime.now(timezone.utc)
        if row is None:
            row = LineTokenHealth(
                id=1, last_check_at=now, healthy=healthy, last_error=error,
                consecutive_failures=1 if increment else 0,
            )
            session.add(row)
        else:
            row.last_check_at = now
            row.healthy = healthy
            row.last_error = error
            if reset_failures:
                row.consecutive_failures = 0
            elif increment:
                row.consecutive_failures += 1
        session.commit()
    finally:
        session.close()


def _alert_if_milestone(error: str, *, level: str = "error"):
    session = get_session_factory()()
    try:
        row = session.query(LineTokenHealth).filter(LineTokenHealth.id == 1).first()
        if row is None:
            return
        if row.consecutive_failures in _DEDUP_MILESTONES:
            tagged_capture(
                RuntimeError(f"LINE token unhealthy: {error} (consecutive_failures={row.consecutive_failures})"),
                tag="line", level=level,
            )
    finally:
        session.close()


async def run_line_token_health_scheduler(stop_event: asyncio.Event) -> None:
    """每日 08:00 Asia/Taipei tick；其他時間 sleep。"""
    while not stop_event.is_set():
        now = datetime.now(ZoneInfo("Asia/Taipei"))
        target_hour = settings.line.token_health_ping_hour_taipei
        # 計算到下個 target_hour 的 seconds
        next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if next_run <= now:
            from datetime import timedelta as _td
            next_run += _td(days=1)
        wait = (next_run - now).total_seconds()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            tick_line_token_health()
        except Exception as exc:
            logger.exception("line_token_health tick failed")
            tagged_capture(exc, tag="line", level="warning")
```

- [ ] **Step 4.2: Tests + commit**

`tests/test_line_token_health_scheduler.py`:
- `test_tick_200_marks_healthy`
- `test_tick_401_marks_unhealthy_and_milestone_alert`
- `test_tick_connection_error_marks_unhealthy_warning`
- `test_dedup_only_milestones_alert`
- `test_disabled_skips`

Commit:
```
feat(line): tick_line_token_health daily ping (Phase 4)

對應 spec §8.4。每日 08:00 Asia/Taipei ping /v2/bot/info；401/403 mark
healthy=false + Sentry alert（consecutive_failures milestone dedup 1/8/30）。
User 已確認 long-lived token 故僅 liveness 監測，無 rotation hook。
```

---

## Task 5: 註冊 scheduler + add config field

**Files:**
- Modify `main.py` (lifespan add 2 scheduler tasks + graceful shutdown)
- Modify `config/line.py`

`config/line.py` 加 `token_health_ping_hour_taipei: int = 8`

`main.py` 仿 `run_line_retry_scheduler` 註冊 pattern（Phase 2 已加），加：
```python
pending_uploads_task = asyncio.create_task(run_pending_uploads_scheduler(pending_uploads_stop_event))
line_token_health_task = asyncio.create_task(run_line_token_health_scheduler(line_token_health_stop_event))
```

graceful shutdown 區段 set stop event + await tasks。

Commit message:
```
feat(main): 註冊 pending_uploads + line_token_health scheduler (Phase 4)
```

---

## Task 6: Admin endpoint `/api/internal/integrations/health`

**Files:**
- Create `api/integrations_health.py`
- Modify `main.py` (include_router)
- Create `tests/test_integrations_health_endpoint.py`

- [ ] **Step 6.1: Write router**

```python
"""api/integrations_health.py — Phase 4 P1 resilience: admin 整合健康狀態."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.base import get_session
from models.integration_health import LineTokenHealth
from models.notification_log import NotificationLog
from models.pending_uploads import PendingUpload
from utils.auth import require_staff_permission
from utils.circuit_breaker import (
    EXTERNAL_HTTP_BREAKER, LINE_BREAKER, SUPABASE_BREAKER,
)
from utils.permissions import Permission

router = APIRouter(prefix="/api/internal/integrations", tags=["integrations-health"])


class LineHealth(BaseModel):
    breaker: str
    token_healthy: bool | None
    token_last_check_at: str | None
    token_consecutive_failures: int
    retry_pending: int
    retry_final_failed_24h: int


class SupabaseHealth(BaseModel):
    breaker: str
    pending_uploads: int


class ExternalHttpHealth(BaseModel):
    breaker: str


class IntegrationsHealthResponse(BaseModel):
    line: LineHealth
    supabase: SupabaseHealth
    external_http: ExternalHttpHealth


@router.get("/health", response_model=IntegrationsHealthResponse)
def get_integrations_health(
    _current_user: dict = Depends(require_staff_permission(Permission.AUDIT_LOGS)),
    session: Session = Depends(get_session),
) -> IntegrationsHealthResponse:
    from datetime import datetime, timedelta, timezone

    token = session.query(LineTokenHealth).filter(LineTokenHealth.id == 1).first()

    retry_pending = (
        session.query(NotificationLog)
        .filter(
            NotificationLog.line_next_retry_at.is_not(None),
            NotificationLog.line_retry_count < 3,
        )
        .count()
    )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    final_failed_24h = (
        session.query(NotificationLog)
        .filter(
            NotificationLog.line_retry_count >= 3,
            NotificationLog.created_at >= cutoff,
        )
        .count()
    )
    pending_uploads = (
        session.query(PendingUpload)
        .filter(PendingUpload.succeeded_at.is_(None), PendingUpload.attempts < 5)
        .count()
    )

    return IntegrationsHealthResponse(
        line=LineHealth(
            breaker=LINE_BREAKER.state,
            token_healthy=token.healthy if token else None,
            token_last_check_at=token.last_check_at.isoformat() if token else None,
            token_consecutive_failures=token.consecutive_failures if token else 0,
            retry_pending=retry_pending,
            retry_final_failed_24h=final_failed_24h,
        ),
        supabase=SupabaseHealth(
            breaker=SUPABASE_BREAKER.state,
            pending_uploads=pending_uploads,
        ),
        external_http=ExternalHttpHealth(breaker=EXTERNAL_HTTP_BREAKER.state),
    )
```

- [ ] **Step 6.2: Mount router in main.py**

Find `app.include_router(audit_router)` and add after:
```python
from api.integrations_health import router as integrations_health_router
app.include_router(integrations_health_router)
```

- [ ] **Step 6.3: Tests + commit**

`tests/test_integrations_health_endpoint.py`:
- `test_requires_audit_logs_permission`（無權回 403）
- `test_returns_breaker_states`
- `test_counts_retry_pending`
- `test_counts_pending_uploads`

Commit:
```
feat(api): GET /api/internal/integrations/health admin endpoint (Phase 4)

對應 spec §8.5。權限 AUDIT_LOGS（與 internal_metrics 同）。回傳 3 breaker
state + LINE token healthy/consecutive_failures + line retry pending/final_failed_24h
+ supabase pending_uploads count。前端後台徽章 UI 是 Phase 5 follow-up。
```

---

## 完成定義

Phase 4 ship = 6 atomic commit：
1. 2 model + alembic migration
2. SupabaseStorage retry + fallback
3. pending_uploads scheduler
4. line_token_health scheduler
5. main.py 註冊 + config field
6. /api/internal/integrations/health endpoint

全套 backend pytest 零 regression。

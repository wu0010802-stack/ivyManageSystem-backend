# 通知中央 Dispatcher Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立通知中央 dispatcher 的 Phase 1 後端骨架：`services/notification/dispatch.py` 對外 `enqueue()` 入口 + SQLAlchemy `after_commit` hook 自動 fan-out + `notification_logs` 持久層 + 既有 `parent_notification_preferences` 重命名為合表 `notification_preferences`。Phase 1 不切換任何 caller，純 additive，行為零變化。

**Architecture:** 17 個 event_type 兩級命名（`{domain}.{action}`）+ 宣告式 `CHANNEL_MATRIX` → 三通道 fan-out（in_app 必，line/ws 可關）。`enqueue()` 只將事件註冊到 `session.info`，SQLAlchemy `after_commit` listener 在 tx commit 後 drain queue 並開 short-lived session 寫 `notification_logs` row、過 preference gate、呼叫 LINE / WS adapter。WS sync→async bridge 複用既有 `utils/event_loop.get_main_loop()`。

**Tech Stack:** FastAPI, SQLAlchemy 2.x event system, Pydantic v2（不用，dispatch 純 service 層）, Alembic, pytest, PostgreSQL（dev / prod）/ SQLite（tests via `test_db_session` fixture）。

**Spec：** `docs/superpowers/specs/2026-05-25-notification-dispatch-design.md`（已 commit `3d7fcb1`）。

**Phase 1 不含：** 21+ caller 遷移（Phase 2，4 個 PR 分批）、員工通知中心 UI（Phase 3，FE 4 元件 + BE 4 endpoint + WS endpoint 補完）、outbox 升級（Phase 4，defer）。

**完成判準：**
1. `dispatch.enqueue(session, event_type="leave.approved", recipient_user_id=…, context={…})` + `session.commit()` 可看到 `notification_logs` 多 row
2. 既有 LINE/WS 通知零回歸（4486+ pytest 全綠）
3. 家長 preference `event_type` 加 `parent.` 前綴後 GET/PUT 仍正確
4. WS bridge mock 測試通過（在無 WS subscriber 時不拋例外）

---

## 檔案結構

**新建（11）：**
- `alembic/versions/<YYYYMMDD>_notif01_notification_consolidation.py` — migration
- `services/notification/dispatch.py` — 對外 `enqueue()` + `install_session_hooks()` + `_fan_out()` + `_pref_enabled()`
- `services/notification/event_types.py` — 17 event_type 字串常數 + `NOTIFICATION_EVENT_TYPES: frozenset`
- `services/notification/channel_matrix.py` — `Channel` typing + `CHANNEL_MATRIX` dict
- `services/notification/renderers.py` — `Rendered` dataclass + `RENDERERS` dict + `render()` + 17 renderer 函式
- `services/notification/_channels/__init__.py` — package marker
- `services/notification/_channels/line.py` — `LineAdapter` + `LINE_HANDLERS` thin dispatch
- `services/notification/_channels/ws.py` — `WsAdapter` + `_inbox_ws_push` 同步 wrapper（用 `utils/event_loop.get_main_loop`）
- `models/notification_log.py` — `NotificationLog` model
- `api/inbox_ws.py` — Phase 1 skeleton（hub key + `inbox_broadcast_user()`；WS endpoint 留 Phase 3）
- `tests/notification/__init__.py` — test package

**修改（4）：**
- `models/database.py` — re-export `NotificationLog`（與既有 `ParentNotificationPreference` 並列）
- `main.py` — lifespan 加 `dispatch.install_session_hooks(get_session_factory())` 與 sanity assert
- `api/parent_portal/notifications.py` — `PARENT_NOTIFICATION_EVENT_TYPES` 7 個值加 `parent.` 前綴 + GET/PUT 仍接受舊 key（過渡相容）
- `tests/conftest.py` — `test_db_session` fixture 在 swap factory 後 call `install_session_hooks` 重綁

**不動：**
- `services/line_service.py`（全 21 method 保留；dispatch 內部繼續 call，Phase 4 才退役）
- `services/notification/approval_notifier.py`（Phase 2 PR-A 才刪）
- `api/dismissal_ws.py` / `api/contact_book_ws.py`（WS manager 結構不動）
- 任何 router / service caller（Phase 1 零行為改變）

**前端（separate PR after BE merged，sibling repo `ivy-frontend`）：** Task 16 列出但不在本 plan 中執行 — Phase 1 BE merged 後另開 frontend 短 PR。

---

## 約定

- 路徑都從 `ivy-backend/` 起算（worktree 內為 `<worktree>/ivy-backend/...`；本 repo 即根目錄起算）
- 測試框架：`pytest` + `pytest-asyncio`（既有；conftest.py 已配）
- 測試命令：`pytest tests/notification/ -v`（Phase 1 新增測試集中此目錄）
- 全套回歸命令：`pytest tests/ -x --tb=short`（Phase 1 結束時跑一次確認 4486+ 全綠）
- Commit 格式：`feat(notification): …` / `test(notification): …` / `docs(notification): …`；遵循 workspace CLAUDE.md Conventional Commits
- migration 命名沿用既有慣例 `<YYYYMMDD>_<shortid>_<description>.py`，shortid 用 `notif01`

---

## Task 0: 建立 worktree

**Files:** N/A（git operation）

- [ ] **Step 1: 建立 worktree 與 branch**

```bash
cd ~/Desktop/ivy-backend
git worktree add .claude/worktrees/notification-dispatch-phase-1-2026-05-25-backend \
  -b feat/notification-dispatch-phase-1-2026-05-25-backend main
cd .claude/worktrees/notification-dispatch-phase-1-2026-05-25-backend
pwd  # 確認在 worktree 內
```

Expected: `pwd` 輸出 `.claude/worktrees/notification-dispatch-phase-1-2026-05-25-backend` 結尾。

- [ ] **Step 2: 確認 alembic head**

```bash
alembic heads
```

Expected: `mergeheads02 (head)`（單一 head）

如果出現多個 head 表示有別的並行 worktree 已 merge head，請先 stop 並回報 user — Phase 1 migration `down_revision` 必須對齊單一 head。

---

## Task 1: 17 個 event_type 常數 + frozenset

**Files:**
- Create: `services/notification/__init__.py`
- Create: `services/notification/event_types.py`
- Create: `tests/notification/__init__.py`
- Create: `tests/notification/test_event_types.py`

- [ ] **Step 1: 建 package markers（empty files）**

```bash
mkdir -p services/notification tests/notification
touch services/notification/__init__.py tests/notification/__init__.py
```

注意：`services/notification/__init__.py` 已經存在（內含 approval_notifier import），不要覆蓋。確認：

```bash
test -s services/notification/__init__.py && echo "已有內容，保留" || echo "空檔，OK"
```

- [ ] **Step 2: 寫失敗測試 `tests/notification/test_event_types.py`**

```python
"""event_type 常數與集合的契約測試。"""

import pytest
from services.notification.event_types import NOTIFICATION_EVENT_TYPES


def test_event_types_contains_all_17_v1_events():
    expected = {
        # 員工域 (12)
        "leave.submitted", "leave.approved", "leave.rejected",
        "overtime.submitted", "overtime.approved", "overtime.rejected",
        "punch_correction.approved", "punch_correction.rejected",
        "salary.batch_completed",
        "activity.waitlist_promoted",
        "pos.unlock_requested",
        "dismissal.created",
        # 家長域 (7)
        "parent.message_received",
        "parent.announcement",
        "parent.event_ack_required",
        "parent.fee_due",
        "parent.leave_result",
        "parent.attendance_alert",
        "parent.contact_book_published",
    }
    assert NOTIFICATION_EVENT_TYPES == expected


def test_event_types_is_frozenset():
    assert isinstance(NOTIFICATION_EVENT_TYPES, frozenset)


def test_event_types_count_is_19():
    # 員工 12 + 家長 7 = 19
    assert len(NOTIFICATION_EVENT_TYPES) == 19
```

- [ ] **Step 3: 跑測試確認失敗**

```bash
pytest tests/notification/test_event_types.py -v
```

Expected: ImportError / ModuleNotFoundError（`services.notification.event_types` 不存在）

- [ ] **Step 4: 實作 `services/notification/event_types.py`**

```python
"""通知 event_type 命名空間：兩級 {domain}.{action}。

v1 共 19 個 event（員工 12 + 家長 7）。新增 event 時：
1. 加進此 frozenset
2. 在 channel_matrix.py 加對應 channel tuple
3. 在 renderers.py 加 @renderer 裝飾的函式
4. （家長端）若家長可關 → notification_preferences row 由 caller 控
"""

from __future__ import annotations

# 員工域
LEAVE_SUBMITTED = "leave.submitted"
LEAVE_APPROVED = "leave.approved"
LEAVE_REJECTED = "leave.rejected"
OVERTIME_SUBMITTED = "overtime.submitted"
OVERTIME_APPROVED = "overtime.approved"
OVERTIME_REJECTED = "overtime.rejected"
PUNCH_CORRECTION_APPROVED = "punch_correction.approved"
PUNCH_CORRECTION_REJECTED = "punch_correction.rejected"
SALARY_BATCH_COMPLETED = "salary.batch_completed"
ACTIVITY_WAITLIST_PROMOTED = "activity.waitlist_promoted"
POS_UNLOCK_REQUESTED = "pos.unlock_requested"
DISMISSAL_CREATED = "dismissal.created"

# 家長域
PARENT_MESSAGE_RECEIVED = "parent.message_received"
PARENT_ANNOUNCEMENT = "parent.announcement"
PARENT_EVENT_ACK_REQUIRED = "parent.event_ack_required"
PARENT_FEE_DUE = "parent.fee_due"
PARENT_LEAVE_RESULT = "parent.leave_result"
PARENT_ATTENDANCE_ALERT = "parent.attendance_alert"
PARENT_CONTACT_BOOK_PUBLISHED = "parent.contact_book_published"

NOTIFICATION_EVENT_TYPES: frozenset[str] = frozenset({
    LEAVE_SUBMITTED, LEAVE_APPROVED, LEAVE_REJECTED,
    OVERTIME_SUBMITTED, OVERTIME_APPROVED, OVERTIME_REJECTED,
    PUNCH_CORRECTION_APPROVED, PUNCH_CORRECTION_REJECTED,
    SALARY_BATCH_COMPLETED,
    ACTIVITY_WAITLIST_PROMOTED,
    POS_UNLOCK_REQUESTED,
    DISMISSAL_CREATED,
    PARENT_MESSAGE_RECEIVED, PARENT_ANNOUNCEMENT, PARENT_EVENT_ACK_REQUIRED,
    PARENT_FEE_DUE, PARENT_LEAVE_RESULT, PARENT_ATTENDANCE_ALERT,
    PARENT_CONTACT_BOOK_PUBLISHED,
})
```

- [ ] **Step 5: 跑測試確認通過**

```bash
pytest tests/notification/test_event_types.py -v
```

Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add services/notification/__init__.py services/notification/event_types.py \
  tests/notification/__init__.py tests/notification/test_event_types.py
git commit -m "feat(notification): add event_types module (19 v1 event_type constants + frozenset)"
```

---

## Task 2: Channel matrix + Channel typing

**Files:**
- Create: `services/notification/channel_matrix.py`
- Test: `tests/notification/test_channel_matrix.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""channel matrix 覆蓋 + 順序測試。"""

import pytest
from services.notification.event_types import NOTIFICATION_EVENT_TYPES
from services.notification.channel_matrix import CHANNEL_MATRIX, Channel


def test_channel_matrix_covers_all_event_types():
    """每個 event_type 必須有 matrix 對映。"""
    missing = NOTIFICATION_EVENT_TYPES - set(CHANNEL_MATRIX.keys())
    assert not missing, f"matrix 漏配 event_type: {missing}"


def test_channel_matrix_no_extra_keys():
    """matrix 不應有 event_types 沒列的 key（防 typo）。"""
    extra = set(CHANNEL_MATRIX.keys()) - NOTIFICATION_EVENT_TYPES
    assert not extra, f"matrix 有 event_types 未定義的 key: {extra}"


def test_employee_events_default_in_app_and_line():
    """員工域（不含 dismissal）預設 (in_app, line)。"""
    employee_events = [e for e in NOTIFICATION_EVENT_TYPES
                       if not e.startswith("parent.") and e != "dismissal.created"]
    for ev in employee_events:
        assert CHANNEL_MATRIX[ev] == ("in_app", "line"), \
            f"{ev} 應 ('in_app', 'line') 實 {CHANNEL_MATRIX[ev]}"


def test_dismissal_created_is_line_and_ws_no_in_app():
    """群組推播：LINE + 班級 WS，不寫個人 in_app。"""
    assert CHANNEL_MATRIX["dismissal.created"] == ("line", "ws")


def test_parent_events_default_line_only_except_realtime_ones():
    """家長預設 LINE only，message_received + contact_book_published 加 WS。"""
    assert CHANNEL_MATRIX["parent.message_received"] == ("line", "ws")
    assert CHANNEL_MATRIX["parent.contact_book_published"] == ("line", "ws")
    line_only = ["parent.announcement", "parent.event_ack_required",
                 "parent.fee_due", "parent.leave_result", "parent.attendance_alert"]
    for ev in line_only:
        assert CHANNEL_MATRIX[ev] == ("line",), f"{ev} 應 ('line',) 實 {CHANNEL_MATRIX[ev]}"


def test_channel_type_literal():
    """Channel 應為 Literal 三值。"""
    import typing
    args = typing.get_args(Channel)
    assert set(args) == {"in_app", "line", "ws"}
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_channel_matrix.py -v
```

Expected: ImportError

- [ ] **Step 3: 實作 `services/notification/channel_matrix.py`**

```python
"""event_type → 預設通道對映（宣告式 dict）。

規則：
- 'in_app' 不檢查 preference，一律寫 notification_logs；in_app 路徑由
  dispatch._fan_out 內聯實作，落 log 後自動 push inbox WS
- 'line' / 'ws' 過 notification_preferences gate（缺 row = enabled）
- 'ws' channel 只處理非 inbox WS（parent.* / dismissal.created）；員工 inbox WS
  由 _fan_out 直接呼叫 _inbox_ws_push，不經 ws adapter
- 順序即 fan-out 順序，但實作上 in_app 強制最先（log_id 是 line/ws 前置依賴）

新增 event_type 時必須在此 matrix 加一筆，否則 dispatch._fan_out 不會發。
"""

from __future__ import annotations

from typing import Literal

Channel = Literal["in_app", "line", "ws"]

CHANNEL_MATRIX: dict[str, tuple[Channel, ...]] = {
    "leave.submitted":              ("in_app", "line"),
    "leave.approved":               ("in_app", "line"),
    "leave.rejected":               ("in_app", "line"),
    "overtime.submitted":           ("in_app", "line"),
    "overtime.approved":            ("in_app", "line"),
    "overtime.rejected":            ("in_app", "line"),
    "punch_correction.approved":    ("in_app", "line"),
    "punch_correction.rejected":    ("in_app", "line"),
    "salary.batch_completed":       ("in_app", "line"),
    "activity.waitlist_promoted":   ("in_app", "line"),
    "pos.unlock_requested":         ("in_app", "line"),
    "dismissal.created":            ("line", "ws"),
    "parent.message_received":      ("line", "ws"),
    "parent.announcement":          ("line",),
    "parent.event_ack_required":    ("line",),
    "parent.fee_due":               ("line",),
    "parent.leave_result":          ("line",),
    "parent.attendance_alert":      ("line",),
    "parent.contact_book_published":("line", "ws"),
}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
pytest tests/notification/test_channel_matrix.py -v
```

Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add services/notification/channel_matrix.py tests/notification/test_channel_matrix.py
git commit -m "feat(notification): add CHANNEL_MATRIX (19 event_type → channel tuple)"
```

---

## Task 3: NotificationLog model

**Files:**
- Create: `models/notification_log.py`
- Modify: `models/database.py`（re-export）
- Test: `tests/notification/test_notification_log_model.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""NotificationLog model schema 與 default 行為測試。"""

import pytest
from datetime import datetime
from sqlalchemy import inspect

from models.database import NotificationLog


def test_notification_log_table_name():
    assert NotificationLog.__tablename__ == "notification_logs"


def test_notification_log_required_columns_present():
    cols = {c.name for c in NotificationLog.__table__.columns}
    required = {
        "id", "recipient_user_id", "event_type", "sender_id",
        "title", "body", "payload_json",
        "source_entity_type", "source_entity_id", "deep_link",
        "channels_attempted", "channels_succeeded", "channels_failed",
        "read_at", "created_at",
    }
    assert required.issubset(cols), f"缺欄: {required - cols}"


def test_notification_log_id_is_bigint():
    id_col = NotificationLog.__table__.columns["id"]
    # SQLAlchemy 在 SQLite 上 BigInteger 變 INTEGER；只檢查 python type info
    assert id_col.primary_key is True
    assert id_col.autoincrement is True


def test_notification_log_recipient_required():
    recipient = NotificationLog.__table__.columns["recipient_user_id"]
    assert recipient.nullable is False


def test_notification_log_create_with_defaults(test_db_session):
    row = NotificationLog(
        recipient_user_id=1, event_type="leave.approved",
        title="t", body="b",
    )
    test_db_session.add(row)
    test_db_session.flush()
    assert row.id is not None
    assert row.payload_json == {}
    assert row.channels_attempted == []
    assert row.channels_succeeded == []
    assert row.channels_failed == []
    assert row.read_at is None
    assert isinstance(row.created_at, datetime)
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_notification_log_model.py -v
```

Expected: ImportError（`NotificationLog` 未在 `models.database`）

- [ ] **Step 3: 實作 `models/notification_log.py`**

```python
"""通知中央 dispatcher 的持久層 row（in-app log + audit）。

單筆 row 代表一個 event 的完整 fan-out 結果（不是每通道一筆）。
三個 channels_* JSON 欄位記錄通道狀態：
- channels_attempted: 解析 matrix + preference gate 後實際嘗試的 channel
- channels_succeeded: 成功送出（含 in_app 寫 log 本身）
- channels_failed: [{"channel": "line", "error": "..."}, ...]

title/body/deep_link 由 renderer 預渲染寫入；payload_json 保留結構化 context
供前端深用（avatar / status chip）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    text,
)

from models.base import Base


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        Index(
            "ix_notif_log_recipient_unread",
            "recipient_user_id",
            "read_at",
            postgresql_where=text("read_at IS NULL"),
        ),
        Index("ix_notif_log_recipient_created", "recipient_user_id", "created_at"),
        Index("ix_notif_log_source", "source_entity_type", "source_entity_id"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    recipient_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_type = Column(String(60), nullable=False)
    sender_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title = Column(String(120), nullable=False)
    body = Column(Text, nullable=False)
    payload_json = Column(JSON, nullable=False, default=dict)
    source_entity_type = Column(String(40), nullable=True)
    source_entity_id = Column(Integer, nullable=True)
    deep_link = Column(String(255), nullable=True)
    channels_attempted = Column(JSON, nullable=False, default=list)
    channels_succeeded = Column(JSON, nullable=False, default=list)
    channels_failed = Column(JSON, nullable=False, default=list)
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
```

- [ ] **Step 4: 在 `models/database.py` re-export**

找到 `models/database.py:126` 的 `from models.parent_notification import ParentNotificationPreference` 那行，下方加：

```python
from models.notification_log import NotificationLog  # noqa: F401  (re-export)
```

並在 `__all__`（line 266 附近）加入 `"NotificationLog",`：

```python
__all__ = [
    ...,
    "ParentNotificationPreference",
    "NotificationLog",
    ...
]
```

- [ ] **Step 5: 跑測試確認通過**

```bash
pytest tests/notification/test_notification_log_model.py -v
```

Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add models/notification_log.py models/database.py tests/notification/test_notification_log_model.py
git commit -m "feat(notification): add NotificationLog model (in-app log + audit row)"
```

---

## Task 4: Alembic migration `notif01_notification_consolidation`

**Files:**
- Create: `alembic/versions/<YYYYMMDD>_notif01_notification_consolidation.py`
- Test: `tests/notification/test_migration_notif01.py`

- [ ] **Step 1: 產生 migration 框架**

```bash
alembic revision -m "notification consolidation (rename + notification_logs)" \
  --rev-id "notif01_consolidation"
```

Expected: 在 `alembic/versions/` 出現新檔，檔名格式 `<auto-timestamp>_notif01_consolidation_notification_consolidation.py`。

**注意檔名**：alembic 預設用 timestamp 前綴；要對齊 repo 慣例 `<YYYYMMDD>_<shortid>_<longname>.py` 須手動 rename：

```bash
TODAY=$(date +%Y%m%d)
mv alembic/versions/*notif01_consolidation_notification_consolidation.py \
   alembic/versions/${TODAY}_notif01_notification_consolidation.py
```

- [ ] **Step 2: 寫失敗測試**

```python
"""migration notif01 up/down 對 SQLite 跑（symmetric reversibility）。"""

import pytest
from alembic.config import Config
from alembic import command


def test_notif01_upgrade_creates_notification_logs(test_db_session, tmp_path, monkeypatch):
    """直接 import notif01 module 跑 up + down 應對稱。
    完整 alembic upgrade head 太耗時且依賴上游 chain；此處單元測試只驗 notif01 函式體。
    """
    from sqlalchemy import inspect

    # 預先建 parent_notification_preferences 表（模擬 notif01 之前的狀態）
    from models.parent_notification import ParentNotificationPreference
    ParentNotificationPreference.__table__.create(test_db_session.bind, checkfirst=True)

    # 預建一筆舊家長 pref row 驗 backfill
    test_db_session.execute(
        ParentNotificationPreference.__table__.insert().values(
            user_id=999, event_type="message_received", channel="line", enabled=True
        )
    )
    test_db_session.commit()

    # 跑 upgrade
    import importlib
    import sys
    mod_name = None
    for name in sys.modules:
        if "notif01" in name:
            mod_name = name
            break
    if mod_name is None:
        # 動態 import
        from pathlib import Path
        for path in Path("alembic/versions").glob("*notif01*.py"):
            spec = importlib.util.spec_from_file_location("notif01_mod", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            break
    else:
        mod = sys.modules[mod_name]

    # 用 alembic op 跑 upgrade — 需要 op binding
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    ctx = MigrationContext.configure(test_db_session.bind.connect())
    op = Operations(ctx)
    # monkeypatch op global in module
    import alembic.op as alembic_op_module
    monkeypatch.setattr(alembic_op_module, "rename_table", op.rename_table)
    monkeypatch.setattr(alembic_op_module, "execute", op.execute)
    monkeypatch.setattr(alembic_op_module, "create_index", op.create_index)
    monkeypatch.setattr(alembic_op_module, "create_table", op.create_table)
    monkeypatch.setattr(alembic_op_module, "drop_index", op.drop_index)
    monkeypatch.setattr(alembic_op_module, "drop_table", op.drop_table)

    mod.upgrade()

    inspector = inspect(test_db_session.bind)
    tables = inspector.get_table_names()
    assert "notification_preferences" in tables
    assert "notification_logs" in tables
    assert "parent_notification_preferences" not in tables

    # 驗 backfill: row 的 event_type 已加前綴
    from sqlalchemy import text
    result = test_db_session.execute(
        text("SELECT event_type FROM notification_preferences WHERE user_id=999")
    ).fetchone()
    assert result is not None
    assert result[0] == "parent.message_received"

    # 跑 downgrade
    mod.downgrade()
    inspector = inspect(test_db_session.bind)
    tables = inspector.get_table_names()
    assert "notification_logs" not in tables
    assert "parent_notification_preferences" in tables
    assert "notification_preferences" not in tables

    # 驗反向 backfill: row 的 event_type 已去前綴
    result = test_db_session.execute(
        text("SELECT event_type FROM parent_notification_preferences WHERE user_id=999")
    ).fetchone()
    assert result[0] == "message_received"
```

注意：此測試直接 import migration module 並 patch `alembic.op`，避免完整 alembic chain 對 SQLite 的相容問題。實際 PG 上的 migration 是否 work 由 Task 16 整合測試（手動 `alembic upgrade head` 對 dev DB）驗。

- [ ] **Step 3: 跑測試確認失敗**

```bash
pytest tests/notification/test_migration_notif01.py -v
```

Expected: AssertionError 或 op 找不到表（migration 空）

- [ ] **Step 4: 實作 migration**

編輯 `alembic/versions/<YYYYMMDD>_notif01_notification_consolidation.py`：

```python
"""notification consolidation (rename + notification_logs)

Revision ID: notif01_consolidation
Revises: mergeheads02
Create Date: 2026-05-25

操作：
1. rename parent_notification_preferences → notification_preferences
2. ALTER CONSTRAINT uq_parent_notif_pref_triple → uq_notif_pref_triple
3. create index ix_notif_pref_user_event
4. UPDATE event_type 加 'parent.' 前綴（既有 7 個值）
5. CREATE TABLE notification_logs + 三 index

downgrade 反向。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "notif01_consolidation"
down_revision = "mergeheads02"
branch_labels = None
depends_on = None


PARENT_OLD_EVENT_TYPES = (
    "message_received", "announcement", "event_ack_required", "fee_due",
    "leave_result", "attendance_alert", "contact_book_published",
)


def upgrade() -> None:
    # 1. rename 表
    op.rename_table("parent_notification_preferences", "notification_preferences")

    # 2. constraint rename — PG only；SQLite 不支援 ALTER CONSTRAINT
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE notification_preferences "
            "RENAME CONSTRAINT uq_parent_notif_pref_triple TO uq_notif_pref_triple"
        )

    # 3. index
    op.create_index(
        "ix_notif_pref_user_event",
        "notification_preferences",
        ["user_id", "event_type"],
    )

    # 4. backfill event_type 前綴
    in_clause = ",".join(f"'{ev}'" for ev in PARENT_OLD_EVENT_TYPES)
    op.execute(
        f"UPDATE notification_preferences "
        f"SET event_type = 'parent.' || event_type "
        f"WHERE event_type IN ({in_clause})"
    )

    # 5. create notification_logs
    op.create_table(
        "notification_logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "recipient_user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(60), nullable=False),
        sa.Column(
            "sender_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("payload_json", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("source_entity_type", sa.String(40), nullable=True),
        sa.Column("source_entity_id", sa.Integer, nullable=True),
        sa.Column("deep_link", sa.String(255), nullable=True),
        sa.Column("channels_attempted", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("channels_succeeded", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("channels_failed", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("read_at", sa.DateTime, nullable=True),
        sa.Column(
            "created_at", sa.DateTime, nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # PG partial index for unread；SQLite 不支援 postgresql_where 但 alembic 會 fallback skip
    if bind.dialect.name == "postgresql":
        op.create_index(
            "ix_notif_log_recipient_unread",
            "notification_logs",
            ["recipient_user_id", "read_at"],
            postgresql_where=sa.text("read_at IS NULL"),
        )
    else:
        op.create_index(
            "ix_notif_log_recipient_unread",
            "notification_logs",
            ["recipient_user_id", "read_at"],
        )
    op.create_index(
        "ix_notif_log_recipient_created",
        "notification_logs",
        ["recipient_user_id", "created_at"],
    )
    op.create_index(
        "ix_notif_log_source",
        "notification_logs",
        ["source_entity_type", "source_entity_id"],
    )


def downgrade() -> None:
    # 反向 5
    op.drop_index("ix_notif_log_source", table_name="notification_logs")
    op.drop_index("ix_notif_log_recipient_created", table_name="notification_logs")
    op.drop_index("ix_notif_log_recipient_unread", table_name="notification_logs")
    op.drop_table("notification_logs")

    # 反向 4
    in_clause = ",".join(f"'parent.{ev}'" for ev in PARENT_OLD_EVENT_TYPES)
    op.execute(
        f"UPDATE notification_preferences "
        f"SET event_type = SUBSTR(event_type, 8) "
        f"WHERE event_type IN ({in_clause})"
    )

    # 反向 3
    op.drop_index("ix_notif_pref_user_event", table_name="notification_preferences")

    # 反向 2
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE notification_preferences "
            "RENAME CONSTRAINT uq_notif_pref_triple TO uq_parent_notif_pref_triple"
        )

    # 反向 1
    op.rename_table("notification_preferences", "parent_notification_preferences")
```

- [ ] **Step 5: 跑測試確認通過**

```bash
pytest tests/notification/test_migration_notif01.py -v
```

Expected: 1 passed

- [ ] **Step 6: 手動驗 dev DB 可以 upgrade**

```bash
alembic upgrade head
alembic current
```

Expected: `alembic current` 顯示 `notif01_consolidation (head)`。

跑 query 驗 backfill：

```bash
psql ivymanagement -c "SELECT DISTINCT event_type FROM notification_preferences;"
```

Expected: 所有 event_type 都以 `parent.` 開頭（如果原本沒 row，輸出空，那也 OK）

- [ ] **Step 7: 跑 downgrade 驗反向**

```bash
alembic downgrade -1
alembic current
```

Expected: `mergeheads02 (head)`，`SELECT … FROM parent_notification_preferences` 看到 row 已去前綴。

跑回 head：

```bash
alembic upgrade head
```

- [ ] **Step 8: Commit**

```bash
git add alembic/versions/*notif01*.py tests/notification/test_migration_notif01.py
git commit -m "feat(notification): add notif01 migration (rename parent_notification_preferences + create notification_logs)"
```

---

## Task 5: Rendered dataclass + render() + RENDERERS dict + 17 renderer

**Files:**
- Create: `services/notification/renderers.py`
- Test: `tests/notification/test_renderers.py`

- [ ] **Step 1: 寫失敗測試（涵蓋 happy path + 缺 renderer fallback + render 例外 fallback）**

```python
"""renderer 純函式測試。"""

import pytest
from services.notification.renderers import Rendered, render, RENDERERS
from services.notification.event_types import NOTIFICATION_EVENT_TYPES


def test_all_event_types_have_renderer():
    """每個 event_type 必須有 renderer。"""
    missing = NOTIFICATION_EVENT_TYPES - set(RENDERERS.keys())
    assert not missing, f"缺 renderer: {missing}"


def test_render_leave_approved_happy_path():
    ctx = {
        "reviewer_name": "張主任", "leave_type": "事假",
        "start": "2026-06-01", "end": "2026-06-02", "leave_id": 42,
    }
    r = render("leave.approved", ctx)
    assert "張主任" in r.title
    assert "核准" in r.title
    assert "事假" in r.body
    assert r.deep_link == "/portal/leaves/42"


def test_render_unknown_event_type_fallback():
    """未註冊 event_type → 不拋例外，回 placeholder Rendered。"""
    r = render("unknown.event", {})
    assert r.title.startswith("(")
    assert "unknown.event" in r.title


def test_render_function_raises_returns_failure_placeholder():
    """renderer 函式內部炸 → render() catch + 回 (渲染失敗)。"""
    # ctx 缺必要 key → leave.approved renderer KeyError
    r = render("leave.approved", {})
    assert r.title == "(渲染失敗)"
    assert "leave.approved" in r.body
    assert r.deep_link is None


def test_render_parent_message_received_happy_path():
    ctx = {
        "teacher_name": "王老師", "student_name": "小明",
        "body_preview": "今天小明很乖", "thread_id": 7,
    }
    r = render("parent.message_received", ctx)
    assert "王老師" in r.title
    assert "小明" in r.title or "小明" in r.body
    assert r.deep_link is not None
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_renderers.py -v
```

Expected: ImportError

- [ ] **Step 3: 實作 `services/notification/renderers.py`**

```python
"""每 event_type 對應一個 (title, body, deep_link) 純函式 renderer。

新增 event_type 必須在此檔加一個 @renderer(...) 裝飾的函式，否則 _fan_out
fallback 為 placeholder title（不會拋例外，但通知中心顯示「(event_type)」很醜）。

renderer 內部炸例外時 render() 會 catch + 回 (渲染失敗)，log row 仍會寫入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rendered:
    title: str
    body: str
    deep_link: str | None


RENDERERS: dict[str, Callable[[dict], Rendered]] = {}


def renderer(event_type: str):
    def deco(fn: Callable[[dict], Rendered]) -> Callable[[dict], Rendered]:
        RENDERERS[event_type] = fn
        return fn
    return deco


def render(event_type: str, ctx: dict) -> Rendered:
    fn = RENDERERS.get(event_type)
    if fn is None:
        return Rendered(title=f"({event_type})", body="", deep_link=None)
    try:
        return fn(ctx)
    except Exception:
        logger.exception("renderer 失敗 event=%s", event_type)
        return Rendered(
            title="(渲染失敗)", body=f"event_type={event_type}", deep_link=None
        )


# ────────────────────── 員工域 ──────────────────────

@renderer("leave.submitted")
def _r_leave_submitted(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['submitter_name']} 送出請假申請",
        body=f"{ctx['leave_type']} {ctx['start']} ~ {ctx['end']}",
        deep_link=f"/approvals/leaves/{ctx['leave_id']}",
    )


@renderer("leave.approved")
def _r_leave_approved(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['reviewer_name']} 已核准你的請假",
        body=f"{ctx['leave_type']} {ctx['start']} ~ {ctx['end']}",
        deep_link=f"/portal/leaves/{ctx['leave_id']}",
    )


@renderer("leave.rejected")
def _r_leave_rejected(ctx: dict) -> Rendered:
    body = f"{ctx['leave_type']} {ctx['start']} ~ {ctx['end']}"
    if ctx.get("rejection_reason"):
        body += f"\n原因：{ctx['rejection_reason']}"
    return Rendered(
        title=f"{ctx['reviewer_name']} 已駁回你的請假",
        body=body,
        deep_link=f"/portal/leaves/{ctx['leave_id']}",
    )


@renderer("overtime.submitted")
def _r_overtime_submitted(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['submitter_name']} 送出加班申請",
        body=f"{ctx['ot_date']} {ctx['ot_type']}",
        deep_link=f"/approvals/overtimes/{ctx['overtime_id']}",
    )


@renderer("overtime.approved")
def _r_overtime_approved(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['reviewer_name']} 已核准你的加班",
        body=f"{ctx['ot_date']} {ctx['ot_type']}",
        deep_link=f"/portal/overtimes/{ctx['overtime_id']}",
    )


@renderer("overtime.rejected")
def _r_overtime_rejected(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['reviewer_name']} 已駁回你的加班",
        body=f"{ctx['ot_date']} {ctx['ot_type']}",
        deep_link=f"/portal/overtimes/{ctx['overtime_id']}",
    )


@renderer("punch_correction.approved")
def _r_punch_corr_approved(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['reviewer_name']} 已核准你的補打卡",
        body=f"日期：{ctx['target_date']}",
        deep_link=f"/portal/punch-corrections/{ctx['correction_id']}",
    )


@renderer("punch_correction.rejected")
def _r_punch_corr_rejected(ctx: dict) -> Rendered:
    body = f"日期：{ctx['target_date']}"
    if ctx.get("rejection_reason"):
        body += f"\n原因：{ctx['rejection_reason']}"
    return Rendered(
        title=f"{ctx['reviewer_name']} 已駁回你的補打卡",
        body=body,
        deep_link=f"/portal/punch-corrections/{ctx['correction_id']}",
    )


@renderer("salary.batch_completed")
def _r_salary_batch(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['year']}/{ctx['month']:02d} 薪資批次已完成",
        body=f"共 {ctx.get('count', 0)} 筆",
        deep_link=f"/salary/{ctx['year']}/{ctx['month']}",
    )


@renderer("activity.waitlist_promoted")
def _r_activity_waitlist(ctx: dict) -> Rendered:
    return Rendered(
        title=f"候補轉正：{ctx['course_name']}",
        body=f"學生：{ctx.get('student_name', '')}",
        deep_link=f"/activity/courses/{ctx['course_id']}",
    )


@renderer("pos.unlock_requested")
def _r_pos_unlock(ctx: dict) -> Rendered:
    return Rendered(
        title=f"POS 解鎖請求：{ctx['requester_name']}",
        body=ctx.get("reason", ""),
        deep_link=f"/pos/unlock-requests/{ctx['request_id']}",
    )


@renderer("dismissal.created")
def _r_dismissal_created(ctx: dict) -> Rendered:
    body = f"班級：{ctx['classroom_name']}"
    if ctx.get("note"):
        body += f"\n備註：{ctx['note']}"
    return Rendered(
        title=f"接送通知：{ctx['student_name']}",
        body=body,
        deep_link=None,  # 群組推播，無個人深連結
    )


# ────────────────────── 家長域 ──────────────────────

@renderer("parent.message_received")
def _r_parent_message(ctx: dict) -> Rendered:
    snippet = (ctx.get("body_preview") or "(附件)").strip()
    if len(snippet) > 60:
        snippet = snippet[:60] + "…"
    title = f"💬 {ctx['teacher_name']} 傳了新訊息"
    if ctx.get("student_name"):
        title += f"（{ctx['student_name']}）"
    return Rendered(
        title=title,
        body=snippet,
        deep_link=f"/parent/messages/{ctx['thread_id']}" if ctx.get("thread_id") else "/parent/messages",
    )


@renderer("parent.announcement")
def _r_parent_announcement(ctx: dict) -> Rendered:
    return Rendered(
        title=f"📣 園所公告：{ctx['title']}",
        body=ctx.get("preview", "")[:80],
        deep_link=f"/parent/announcements/{ctx['announcement_id']}",
    )


@renderer("parent.event_ack_required")
def _r_parent_event_ack(ctx: dict) -> Rendered:
    return Rendered(
        title=f"📋 待簽事件：{ctx['event_title']}",
        body=f"請於 {ctx.get('deadline', '盡快')} 前完成簽核",
        deep_link=f"/parent/event-ack/{ctx['event_id']}",
    )


@renderer("parent.fee_due")
def _r_parent_fee_due(ctx: dict) -> Rendered:
    return Rendered(
        title=f"💰 學費到期：{ctx['amount']} 元",
        body=f"繳費期限：{ctx['due_date']}",
        deep_link="/parent/fees",
    )


@renderer("parent.leave_result")
def _r_parent_leave_result(ctx: dict) -> Rendered:
    verb = "已核准" if ctx["approved"] else "已駁回"
    body = f"{ctx['leave_type']} {ctx['start']} ~ {ctx['end']}"
    if not ctx["approved"] and ctx.get("review_note"):
        body += f"\n原因：{ctx['review_note']}"
    return Rendered(
        title=f"{ctx['student_name']} 的請假 {verb}",
        body=body,
        deep_link="/parent/leaves",
    )


@renderer("parent.attendance_alert")
def _r_parent_attendance(ctx: dict) -> Rendered:
    return Rendered(
        title=f"⚠️ {ctx['student_name']} 出席異常",
        body=ctx.get("detail", ""),
        deep_link="/parent/attendance",
    )


@renderer("parent.contact_book_published")
def _r_parent_contact_book(ctx: dict) -> Rendered:
    return Rendered(
        title=f"📖 {ctx['student_name']} 今日聯絡簿已發布",
        body=f"日期：{ctx['date']}",
        deep_link=f"/parent/contact-book/{ctx['date']}",
    )
```

- [ ] **Step 4: 跑測試確認通過**

```bash
pytest tests/notification/test_renderers.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add services/notification/renderers.py tests/notification/test_renderers.py
git commit -m "feat(notification): add 19 event_type renderers + render() fallback"
```

---

## Task 6: dispatch.py — `enqueue()` + `PendingEvent` + ValueError check

**Files:**
- Create: `services/notification/dispatch.py`
- Test: `tests/notification/test_dispatch_enqueue.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""dispatch.enqueue 入口契約測試。"""

import pytest
from services.notification import dispatch


def test_enqueue_unknown_event_type_raises(test_db_session):
    with pytest.raises(ValueError, match="未知 event_type"):
        dispatch.enqueue(
            test_db_session,
            event_type="bogus.event",
            recipient_user_id=1,
            context={},
        )


def test_enqueue_stores_pending_event_on_session_info(test_db_session):
    dispatch.enqueue(
        test_db_session,
        event_type="leave.approved",
        recipient_user_id=42,
        context={"reviewer_name": "X", "leave_type": "事假",
                 "start": "2026-06-01", "end": "2026-06-02", "leave_id": 1},
    )
    queue = test_db_session.info[dispatch._QUEUE_KEY]
    assert len(queue) == 1
    evt = queue[0]
    assert evt.event_type == "leave.approved"
    assert evt.recipient_user_id == 42
    assert evt.channels == ("in_app", "line")


def test_enqueue_copies_context_dict(test_db_session):
    """context 應被拷貝，caller 後續改 dict 不影響 pending event。"""
    ctx = {"a": 1}
    dispatch.enqueue(
        test_db_session, event_type="leave.approved",
        recipient_user_id=1, context=ctx,
    )
    ctx["a"] = 999
    queue = test_db_session.info[dispatch._QUEUE_KEY]
    assert queue[0].context == {"a": 1}


def test_enqueue_with_channels_override(test_db_session):
    dispatch.enqueue(
        test_db_session, event_type="leave.approved",
        recipient_user_id=1, context={},
        channels_override=("in_app",),
    )
    queue = test_db_session.info[dispatch._QUEUE_KEY]
    assert queue[0].channels == ("in_app",)


def test_enqueue_appends_multiple_events(test_db_session):
    for i in range(3):
        dispatch.enqueue(
            test_db_session, event_type="leave.approved",
            recipient_user_id=i, context={},
        )
    queue = test_db_session.info[dispatch._QUEUE_KEY]
    assert len(queue) == 3
    assert [e.recipient_user_id for e in queue] == [0, 1, 2]


def test_enqueue_includes_source_entity_fields(test_db_session):
    dispatch.enqueue(
        test_db_session, event_type="leave.approved",
        recipient_user_id=1, context={},
        sender_id=7, source_entity_type="leave_request", source_entity_id=99,
    )
    evt = test_db_session.info[dispatch._QUEUE_KEY][0]
    assert evt.sender_id == 7
    assert evt.source_entity_type == "leave_request"
    assert evt.source_entity_id == 99
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_dispatch_enqueue.py -v
```

Expected: ImportError

- [ ] **Step 3: 實作 `services/notification/dispatch.py`（只實作 enqueue + PendingEvent + _QUEUE_KEY；hook 與 _fan_out 留 Task 7、8）**

```python
"""通知中央 dispatcher：唯一對外入口 + after_commit 自動 fan-out。

Lifecycle：
1. caller `dispatch.enqueue(session=..., event_type=..., ...)`
   → 事件註冊到 session.info[_QUEUE_KEY]，**尚未發送**
2. caller `session.commit()` 觸發 SQLAlchemy after_commit listener
   → `_drain_after_commit(session)` 拉出 queue 逐筆 `_fan_out`
3. `_fan_out`：開 short-lived session 寫 notification_logs row
   → 過 preference gate（in_app 不過）
   → in_app 路徑同時 _inbox_ws_push 給 recipient
   → 呼叫 line/ws adapter
4. caller `session.rollback()` 觸發 `_clear_on_rollback` 清空 queue

任何 fan-out 失敗只 log + 寫入 channels_failed，絕不 re-raise（業務 tx 已 commit）。

session 必須來自 models.base.get_session_factory()，parent_db / spike_rls
等其他 factory 不受監聽。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from services.notification.channel_matrix import CHANNEL_MATRIX, Channel
from services.notification.event_types import NOTIFICATION_EVENT_TYPES

logger = logging.getLogger(__name__)

_QUEUE_KEY = "ivy_notification_queue"


@dataclass(frozen=True)
class PendingEvent:
    event_type: str
    recipient_user_id: Optional[int]
    context: dict
    sender_id: Optional[int]
    source_entity_type: Optional[str]
    source_entity_id: Optional[int]
    channels: tuple[Channel, ...]


def enqueue(
    session: Session,
    *,
    event_type: str,
    recipient_user_id: Optional[int],
    context: dict,
    sender_id: Optional[int] = None,
    source_entity_type: Optional[str] = None,
    source_entity_id: Optional[int] = None,
    channels_override: Optional[tuple[Channel, ...]] = None,
) -> None:
    """註冊一筆通知事件到當前 session 的 queue。

    tx commit 後由 after_commit hook 自動 fan-out（in_app log + LINE + WS）。
    rollback 則自動丟棄（透過 after_rollback hook）。

    Args:
        session: 主庫 session（必須來自 models.base.get_session_factory()）
        event_type: 必須在 NOTIFICATION_EVENT_TYPES 內，否則 ValueError
        recipient_user_id: 接收者 user_id；None 表群組推播（如 dismissal.created）
        context: renderer 用的 dict，會被淺拷貝
        sender_id: 觸發者 user_id（顯示「誰發的」）
        source_entity_type / source_entity_id: 反查源頭；未來 outbox idempotency key
        channels_override: 罕用，特殊 case 覆蓋 CHANNEL_MATRIX
    """
    if event_type not in NOTIFICATION_EVENT_TYPES:
        raise ValueError(f"未知 event_type: {event_type}")
    channels = channels_override or CHANNEL_MATRIX.get(event_type, ())
    if not channels:
        logger.debug("event_type %s 無 channel 設定，略過", event_type)
        return
    queue = session.info.setdefault(_QUEUE_KEY, [])
    queue.append(PendingEvent(
        event_type=event_type,
        recipient_user_id=recipient_user_id,
        context=dict(context),
        sender_id=sender_id,
        source_entity_type=source_entity_type,
        source_entity_id=source_entity_id,
        channels=channels,
    ))
```

- [ ] **Step 4: 跑測試確認通過**

```bash
pytest tests/notification/test_dispatch_enqueue.py -v
```

Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add services/notification/dispatch.py tests/notification/test_dispatch_enqueue.py
git commit -m "feat(notification): add dispatch.enqueue + PendingEvent (no hooks yet)"
```

---

## Task 7: `install_session_hooks()` + after_commit/after_rollback handlers

**Files:**
- Modify: `services/notification/dispatch.py`（加 hooks + install function + _drain stub）
- Modify: `tests/conftest.py`（test_db_session fixture swap 後 reinstall hooks）
- Test: `tests/notification/test_dispatch_hooks.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""after_commit / after_rollback hook 行為測試。

注意：test_db_session fixture 必須在 swap factory 後 reinstall hooks（Task 7 conftest 改動），
否則 hook 綁在 production factory，test factory commit 不觸發。
"""

import pytest
from unittest.mock import patch

from services.notification import dispatch


def test_after_commit_drains_queue_and_calls_fan_out(test_db_session):
    with patch.object(dispatch, "_fan_out") as mock_fan_out:
        dispatch.enqueue(
            test_db_session,
            event_type="leave.approved",
            recipient_user_id=42,
            context={"reviewer_name": "X", "leave_type": "事假",
                     "start": "2026-06-01", "end": "2026-06-02", "leave_id": 1},
        )
        assert dispatch._QUEUE_KEY in test_db_session.info
        test_db_session.commit()

    # commit 後 queue 應被 pop
    assert dispatch._QUEUE_KEY not in test_db_session.info
    assert mock_fan_out.call_count == 1


def test_after_rollback_clears_queue_without_fan_out(test_db_session):
    with patch.object(dispatch, "_fan_out") as mock_fan_out:
        dispatch.enqueue(
            test_db_session, event_type="leave.approved",
            recipient_user_id=42,
            context={"reviewer_name": "X", "leave_type": "事假",
                     "start": "2026-06-01", "end": "2026-06-02", "leave_id": 1},
        )
        test_db_session.rollback()

    assert dispatch._QUEUE_KEY not in test_db_session.info
    assert mock_fan_out.call_count == 0


def test_after_commit_with_empty_queue_is_no_op(test_db_session):
    """commit 但沒 enqueue 不應炸。"""
    with patch.object(dispatch, "_fan_out") as mock_fan_out:
        test_db_session.commit()
    mock_fan_out.assert_not_called()


def test_after_commit_one_fan_out_failure_does_not_block_others(test_db_session):
    """一筆 _fan_out 拋例外，後面的還是會被 call。"""
    call_count = [0]

    def fake_fan_out(evt):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("first fails")

    with patch.object(dispatch, "_fan_out", side_effect=fake_fan_out):
        for i in range(3):
            dispatch.enqueue(
                test_db_session, event_type="leave.approved",
                recipient_user_id=i,
                context={"reviewer_name": "X", "leave_type": "事假",
                         "start": "2026-06-01", "end": "2026-06-02", "leave_id": 1},
            )
        # commit 不應 re-raise
        test_db_session.commit()

    assert call_count[0] == 3


def test_install_session_hooks_idempotent():
    """重複呼叫 install 不應綁多次 hook。"""
    from sqlalchemy import event
    from models.base import get_session_factory

    factory = get_session_factory()
    dispatch.install_session_hooks(factory)
    dispatch.install_session_hooks(factory)
    # 沒有公開 API 直接 count listener，但確保不拋例外 + 之後行為仍正確
    # 用 _hooks_installed_factories sentinel 驗
    assert factory in dispatch._HOOKS_INSTALLED
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_dispatch_hooks.py -v
```

Expected: AttributeError（`install_session_hooks` / `_fan_out` / `_HOOKS_INSTALLED` 不存在）

- [ ] **Step 3: 修改 `services/notification/dispatch.py`，加 hooks + install function + _fan_out stub**

在檔尾加：

```python
# ────────────────────── Hooks ──────────────────────

from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

_HOOKS_INSTALLED: set[sessionmaker] = set()


def install_session_hooks(factory: sessionmaker) -> None:
    """把 after_commit / after_rollback listener 綁到指定 session factory。

    Idempotent — 重複呼叫對同一 factory 不會綁多次。

    必須在 app startup（main.py lifespan）呼叫一次，傳入 models.base.get_session_factory()。
    test fixture 在 swap factory 後也須再呼叫一次以綁到 test factory。
    """
    if factory in _HOOKS_INSTALLED:
        return
    event.listen(factory, "after_commit", _drain_after_commit)
    event.listen(factory, "after_rollback", _clear_on_rollback)
    _HOOKS_INSTALLED.add(factory)


def _drain_after_commit(session: Session) -> None:
    pending = session.info.pop(_QUEUE_KEY, None)
    if not pending:
        return
    for evt in pending:
        try:
            _fan_out(evt)
        except Exception:
            logger.exception(
                "dispatch fan-out 失敗 event=%s recipient=%s",
                evt.event_type, evt.recipient_user_id,
            )
            # 絕不 re-raise — 一筆 fan-out 失敗不能影響後續


def _clear_on_rollback(session: Session) -> None:
    session.info.pop(_QUEUE_KEY, None)


def _fan_out(evt: PendingEvent) -> None:
    """Task 12 實作；現為 stub 讓 hook 測試可 mock。"""
    raise NotImplementedError("Task 12 實作")
```

- [ ] **Step 4: 修改 `tests/conftest.py`，在 `test_db_session` fixture 內 swap 完 factory 後 call install_session_hooks**

找到 `tests/conftest.py:151` 附近 `test_session_factory = sessionmaker(bind=test_engine)`，下方加：

```python
    test_session_factory = sessionmaker(bind=test_engine)

    # 把 dispatch 的 after_commit / after_rollback 重綁到 test factory
    # 不重綁的話 hook 會綁在 production factory，test commit 不觸發
    try:
        from services.notification import dispatch as _dispatch
        _dispatch.install_session_hooks(test_session_factory)
    except ImportError:
        pass  # dispatch 模組未建（Task 6 之前）

    old_engine = base_module._engine
    ...
```

注意：fixture teardown 後 `_HOOKS_INSTALLED` 仍記著舊 test factory，但 factory 物件被 dispose 後就只是 dangling reference，不會影響後續 test（每次 fixture 都建新 factory）。為了避免記憶體洩漏可以在 teardown 加：

```python
    # teardown 後段，base_module 還原後加
    try:
        from services.notification import dispatch as _dispatch
        _dispatch._HOOKS_INSTALLED.discard(test_session_factory)
    except ImportError:
        pass
```

- [ ] **Step 5: 跑測試確認通過**

```bash
pytest tests/notification/test_dispatch_hooks.py -v
```

Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add services/notification/dispatch.py tests/conftest.py tests/notification/test_dispatch_hooks.py
git commit -m "feat(notification): add install_session_hooks + after_commit/after_rollback handlers"
```

---

## Task 8: `api/inbox_ws.py` skeleton（hub key + `inbox_broadcast_user`）

**Files:**
- Create: `api/inbox_ws.py`
- Test: `tests/notification/test_inbox_ws_skeleton.py`

- [ ] **Step 1: 看一下既有 contact_book_ws 的 hub 用法（參考用，不改）**

```bash
grep -n "hub\|broadcast\|subscribe" api/contact_book_ws.py | head -20
```

確認既有 `hub` 來自哪個 module（很可能是 `utils.ws_hub` 或類似）。

```bash
grep -rn "^from.*import hub\|^class.*Hub\b" api/ utils/ services/ 2>/dev/null | grep -v __pycache__ | head -10
```

記下 hub import path（下一步要用）。

- [ ] **Step 2: 寫失敗測試**

```python
"""inbox_ws skeleton 行為測試。"""

import pytest
from api import inbox_ws


def test_inbox_user_key_returns_tuple():
    assert inbox_ws.INBOX_USER_KEY(42) == ("inbox_user", 42)


@pytest.mark.asyncio
async def test_inbox_broadcast_user_no_subscribers_is_no_op():
    """無 WS subscriber 時 broadcast 不應拋例外。"""
    await inbox_ws.inbox_broadcast_user(999, {"event_type": "leave.approved"})
    # 沒拋例外即通過
```

- [ ] **Step 3: 跑測試確認失敗**

```bash
pytest tests/notification/test_inbox_ws_skeleton.py -v
```

Expected: ImportError

- [ ] **Step 4: 實作 `api/inbox_ws.py`（Phase 1 skeleton；WS endpoint 與 JWT auth 在 Phase 3 補完）**

```python
"""員工通知中心 WS — Phase 1 skeleton。

Phase 1 只提供：
- INBOX_USER_KEY: hub subscription key constructor
- inbox_broadcast_user: 推送 helper（無 subscriber 時 no-op）

Phase 3 補完：
- @router.websocket("/inbox") endpoint
- JWT cookie auth → user_id → subscribe
- 重連 / heartbeat
"""

from __future__ import annotations

from typing import Any

# TODO Phase 3: 拿掉這個 import 註解，補實際 hub import path
# 從 Step 1 grep 結果填入正確 import；範例：
from api.contact_book_ws import hub  # 沿用既有 hub 實例（單例）


INBOX_USER_KEY = lambda user_id: ("inbox_user", user_id)


async def inbox_broadcast_user(user_id: int, payload: dict[str, Any]) -> None:
    """推送一筆通知給單一員工的 inbox WS subscriber。

    無 subscriber 時 no-op（hub.broadcast 內部處理）。
    Phase 1 caller 為 services/notification/_channels/ws.py:_inbox_ws_push（同步 wrapper）。
    """
    await hub.broadcast([INBOX_USER_KEY(user_id)], payload)
```

實際 import path 依 Step 1 grep 結果調整。如果 `contact_book_ws.hub` 不能直接 re-use（例如循環 import）改 `from utils.ws_hub import hub` 等。

- [ ] **Step 5: 跑測試確認通過**

```bash
pytest tests/notification/test_inbox_ws_skeleton.py -v
```

Expected: 2 passed

如果 import path 不對改完再跑。

- [ ] **Step 6: Commit**

```bash
git add api/inbox_ws.py tests/notification/test_inbox_ws_skeleton.py
git commit -m "feat(notification): add inbox_ws skeleton (Phase 1; WS endpoint Phase 3)"
```

---

## Task 9: `_channels/ws.py` + `WsAdapter` + `_inbox_ws_push`

**Files:**
- Create: `services/notification/_channels/__init__.py`
- Create: `services/notification/_channels/ws.py`
- Test: `tests/notification/test_channels_ws.py`

- [ ] **Step 1: 建 package marker**

```bash
touch services/notification/_channels/__init__.py
```

- [ ] **Step 2: 寫失敗測試**

```python
"""WS adapter + inbox WS push 同步 wrapper 測試。"""

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from services.notification.dispatch import PendingEvent
from services.notification.renderers import Rendered
from services.notification._channels import ws as ws_adapter_mod


@pytest.fixture
def fake_loop():
    """提供一個跑著的 loop 供 run_coroutine_threadsafe，並 patch get_main_loop。"""
    loop = asyncio.new_event_loop()
    import threading
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    with patch("services.notification._channels.ws.get_main_loop", return_value=loop):
        yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)


def _evt(event_type, **kwargs):
    return PendingEvent(
        event_type=event_type,
        recipient_user_id=kwargs.get("recipient_user_id", 1),
        context=kwargs.get("context", {}),
        sender_id=None,
        source_entity_type=None,
        source_entity_id=None,
        channels=kwargs.get("channels", ("line", "ws")),
    )


def test_ws_adapter_raises_when_loop_unregistered():
    with patch("services.notification._channels.ws.get_main_loop", return_value=None):
        adapter = ws_adapter_mod.WsAdapter()
        with pytest.raises(RuntimeError, match="loop"):
            adapter.send(
                _evt("parent.announcement"),
                Rendered(title="t", body="b", deep_link=None),
                log_id=1,
            )


def test_ws_adapter_parent_event_calls_broadcast_parent(fake_loop):
    with patch("services.notification._channels.ws.broadcast_parent",
               new=AsyncMock()) as mock_bp:
        adapter = ws_adapter_mod.WsAdapter()
        adapter.send(
            _evt("parent.message_received", recipient_user_id=42),
            Rendered(title="t", body="b", deep_link="/x"),
            log_id=99,
        )
        # async mock 在 fake_loop thread 內被 awaited；給點時間
        import time; time.sleep(0.1)
        mock_bp.assert_awaited_once()
        args = mock_bp.call_args[0]
        assert args[0] == 42  # parent user_id
        payload = args[1]
        assert payload["log_id"] == 99
        assert payload["event_type"] == "parent.message_received"


def test_ws_adapter_dismissal_calls_classroom_broadcast(fake_loop):
    with patch.object(ws_adapter_mod, "dismissal_manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock()
        adapter = ws_adapter_mod.WsAdapter()
        adapter.send(
            _evt("dismissal.created", recipient_user_id=None,
                 context={"classroom_id": 7}),
            Rendered(title="t", body="b", deep_link=None),
            log_id=1,
        )
        import time; time.sleep(0.1)
        mock_mgr.broadcast.assert_awaited_once()
        args = mock_mgr.broadcast.call_args[0]
        assert args[0] == 7  # classroom_id


def test_ws_adapter_unsupported_event_type_raises(fake_loop):
    """員工 inbox event 不應走 ws adapter（應走 _inbox_ws_push）。"""
    adapter = ws_adapter_mod.WsAdapter()
    with pytest.raises(RuntimeError, match="不支援"):
        adapter.send(
            _evt("leave.approved", channels=("in_app", "line")),  # 假設誤呼
            Rendered(title="t", body="b", deep_link=None),
            log_id=1,
        )


def test_inbox_ws_push_calls_inbox_broadcast_user(fake_loop):
    with patch("api.inbox_ws.inbox_broadcast_user", new=AsyncMock()) as mock_bcast:
        ws_adapter_mod._inbox_ws_push(
            _evt("leave.approved", recipient_user_id=42),
            Rendered(title="t", body="b", deep_link="/x"),
            log_id=99,
        )
        import time; time.sleep(0.1)
        mock_bcast.assert_awaited_once()
        args = mock_bcast.call_args[0]
        assert args[0] == 42
        assert args[1]["log_id"] == 99


def test_inbox_ws_push_loop_unregistered_raises():
    with patch("services.notification._channels.ws.get_main_loop", return_value=None):
        with pytest.raises(RuntimeError, match="loop"):
            ws_adapter_mod._inbox_ws_push(
                _evt("leave.approved", recipient_user_id=42),
                Rendered(title="t", body="b", deep_link=None),
                log_id=1,
            )
```

- [ ] **Step 3: 跑測試確認失敗**

```bash
pytest tests/notification/test_channels_ws.py -v
```

Expected: ImportError

- [ ] **Step 4: 實作 `services/notification/_channels/ws.py`**

```python
"""WS channel adapter：sync→async bridge via utils.event_loop.get_main_loop。

職責劃分：
- WsAdapter.send：只處理 parent.* 與 dismissal.created（broadcast_parent /
  dismissal_manager.broadcast）
- _inbox_ws_push：員工通知中心 realtime 推送，給 dispatch._fan_out 在 in_app
  路徑直接呼叫；不經 WsAdapter（兩者語意不同 — 員工 inbox 失敗不算 channel
  failure，只 warning）

兩者皆透過 asyncio.run_coroutine_threadsafe 把 coroutine 投回主 loop，避免在
threadpool worker 內起新 loop（會打死 WS transport，B1-B4 round 4 bug）。
"""

from __future__ import annotations

import logging
from typing import Any

from api.contact_book_ws import broadcast_parent
from api.dismissal_ws import manager as dismissal_manager
from utils.event_loop import get_main_loop

logger = logging.getLogger(__name__)

_WS_TIMEOUT_SECONDS = 2.0


def _build_payload(evt, rendered, log_id: int) -> dict[str, Any]:
    return {
        "event_type": evt.event_type,
        "title": rendered.title,
        "body": rendered.body,
        "deep_link": rendered.deep_link,
        "log_id": log_id,
    }


class WsAdapter:
    """非 inbox 的 WS 推送 channel（parent.* / dismissal.created）。"""

    def send(self, evt, rendered, *, log_id: int) -> None:
        loop = get_main_loop()
        if loop is None:
            raise RuntimeError("WS loop 未註冊（main.py lifespan 漏 set_main_loop）")
        import asyncio
        coro = self._dispatch(evt, rendered, log_id)
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        fut.result(timeout=_WS_TIMEOUT_SECONDS)

    async def _dispatch(self, evt, rendered, log_id) -> None:
        payload = _build_payload(evt, rendered, log_id)
        if evt.event_type.startswith("parent."):
            await broadcast_parent(evt.recipient_user_id, payload)
        elif evt.event_type == "dismissal.created":
            classroom_id = evt.context.get("classroom_id")
            if classroom_id is None:
                raise ValueError("dismissal.created 缺 context['classroom_id']")
            await dismissal_manager.broadcast(classroom_id, payload)
        else:
            raise RuntimeError(
                f"ws channel 不支援 event_type={evt.event_type}；"
                "員工 inbox WS 應走 _inbox_ws_push 不經此 adapter"
            )


# ── 員工 inbox WS 推送：給 dispatch._fan_out 直接呼叫 ──

async def _inbox_ws_push_async(evt, rendered, log_id) -> None:
    from api.inbox_ws import inbox_broadcast_user
    payload = _build_payload(evt, rendered, log_id)
    await inbox_broadcast_user(evt.recipient_user_id, payload)


def _inbox_ws_push(evt, rendered, log_id) -> None:
    """同步 wrapper，給 _fan_out 在 in_app 路徑呼叫。

    失敗由 caller swallow（不影響 channels_succeeded）。
    """
    loop = get_main_loop()
    if loop is None:
        raise RuntimeError("WS loop 未註冊")
    import asyncio
    fut = asyncio.run_coroutine_threadsafe(
        _inbox_ws_push_async(evt, rendered, log_id), loop
    )
    fut.result(timeout=_WS_TIMEOUT_SECONDS)
```

- [ ] **Step 5: 跑測試確認通過**

```bash
pytest tests/notification/test_channels_ws.py -v
```

Expected: 6 passed

如果 import 順序問題（例如 `api.contact_book_ws` 起手 import 又 import 回 dispatch）需要把 import 延後到 method 內。實測決定。

- [ ] **Step 6: Commit**

```bash
git add services/notification/_channels/__init__.py services/notification/_channels/ws.py \
  tests/notification/test_channels_ws.py
git commit -m "feat(notification): add WsAdapter + _inbox_ws_push sync wrapper (event_loop bridge)"
```

---

## Task 10: `_channels/line.py` + `LineAdapter` + `LINE_HANDLERS` thin dispatch

**Files:**
- Create: `services/notification/_channels/line.py`
- Test: `tests/notification/test_channels_line.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""LINE adapter 測試：thin dispatch 表 + fallback push_text。"""

import pytest
from unittest.mock import MagicMock

from services.notification._channels.line import LineAdapter, LINE_HANDLERS
from services.notification.dispatch import PendingEvent
from services.notification.renderers import Rendered


def _evt(event_type, **kwargs):
    return PendingEvent(
        event_type=event_type,
        recipient_user_id=kwargs.get("recipient_user_id", 1),
        context=kwargs.get("context", {}),
        sender_id=None,
        source_entity_type=None,
        source_entity_id=None,
        channels=("line",),
    )


def test_line_adapter_fallback_push_text_for_unmapped_event_type():
    """Phase 1：LINE_HANDLERS 為空（全部走 fallback push_text_to_user）。"""
    fake_ls = MagicMock()
    adapter = LineAdapter(fake_ls)
    adapter.send(
        _evt("leave.approved", recipient_user_id=42),
        Rendered(title="標題", body="內文", deep_link="/x"),
        log_id=1,
    )
    fake_ls.push_text_to_user.assert_called_once()
    args = fake_ls.push_text_to_user.call_args[0]
    assert args[0] == 42
    assert "標題" in args[1]
    assert "內文" in args[1]


def test_line_adapter_calls_handler_when_registered():
    """若 LINE_HANDLERS 有對映，應 dispatch 給 handler。"""
    fake_ls = MagicMock()
    handler_called = []

    def my_handler(ls, evt, rendered):
        handler_called.append((ls, evt.event_type, rendered.title))

    LINE_HANDLERS["leave.approved"] = my_handler
    try:
        adapter = LineAdapter(fake_ls)
        adapter.send(
            _evt("leave.approved"),
            Rendered(title="t", body="b", deep_link=None),
            log_id=1,
        )
        assert len(handler_called) == 1
        assert handler_called[0] == (fake_ls, "leave.approved", "t")
        # fallback 不應被呼叫
        fake_ls.push_text_to_user.assert_not_called()
    finally:
        del LINE_HANDLERS["leave.approved"]


def test_line_handlers_is_empty_at_phase_1():
    """Phase 1 不註冊任何 handler，全部走 fallback。Phase 2 PR-A 開始填入。"""
    assert LINE_HANDLERS == {}
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_channels_line.py -v
```

Expected: ImportError

- [ ] **Step 3: 確認 LineService 是否有 `push_text_to_user` 公開方法**

```bash
grep -n "def push_text_to_user\|def _push_to_user" services/line_service.py | head -5
```

如有 `_push_to_user`（私有）則我們要在 LineService 加 public alias，或 adapter 直接 call private（不建議）。

簡單作法：直接 call 既有 public method；如果只有 `_push_to_user` 則本 task 額外加一行 public alias：

```python
# services/line_service.py 內，找到 _push_to_user 定義，下方加：
def push_text_to_user(self, user_id: str, text: str) -> None:
    """Public alias，給 dispatch adapter 用。"""
    self._push_to_user(user_id, text)
```

- [ ] **Step 4: 實作 `services/notification/_channels/line.py`**

```python
"""LINE channel adapter — thin dispatch 到既有 line_service.notify_* method。

Phase 1：LINE_HANDLERS 為空 dict，所有 event 走 fallback push_text_to_user
（純 text）。Phase 2 PR-A 開始按 router 遷移時為每個 event 註冊對映 handler
（function(line_service, evt, rendered) -> None），讓 LINE Flex / quick reply
等複雜推送繼續用既有 line_service method。

Phase 4 完成時 line_service 重構為純 builder + 一個 push 入口，本檔 LINE_HANDLERS
不再需要。
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# event_type → handler(line_service, evt, rendered)
LINE_HANDLERS: dict[str, Callable] = {}


class LineAdapter:
    def __init__(self, line_service):
        self._ls = line_service

    def send(self, evt, rendered, *, log_id: int) -> None:
        # log_id 留作 Phase 4 push receipt 追蹤；v1 不用
        handler = LINE_HANDLERS.get(evt.event_type)
        if handler is None:
            text = (rendered.title or "") + ("\n" + rendered.body if rendered.body else "")
            if evt.recipient_user_id is None:
                logger.warning(
                    "LINE adapter fallback：event=%s 無 recipient_user_id 跳過",
                    evt.event_type,
                )
                return
            # 注意：recipient_user_id 是 User.id (int)，但 LineService.push_text_to_user
            # 需 LINE user_id (str)。Phase 1 fallback 在 _fan_out 應已先 resolve；
            # 但若直接傳 int 進來會炸 — Phase 2 caller 遷移時 _fan_out 預先 resolve。
            # Phase 1 fallback 暫時只支援 str；int 視為「未 resolve」跳過。
            if not isinstance(evt.recipient_user_id, str):
                logger.warning(
                    "LINE adapter fallback：recipient_user_id 非 LINE user_id (int=%s) 跳過",
                    evt.recipient_user_id,
                )
                return
            self._ls.push_text_to_user(evt.recipient_user_id, text)
            return
        handler(self._ls, evt, rendered)
```

注意：`recipient_user_id` 在 `enqueue` 時是 `int`（user.id），但 LINE push 需要 LINE user_id (str)。本 Phase 1 的 fallback 路徑直接拒絕 int（不發），因為 Phase 1 沒有 caller 真的會送通知（測試是 mock，prod 是 Phase 2 才有 caller）。Phase 2 PR-A 時 _fan_out 會在呼叫 LINE adapter 前透過 `should_push_to_parent` / 員工的對應 query 解析 line_user_id 並覆寫到 evt（或傳第二個參數）—**此細節在 Phase 2 plan 細化**。

- [ ] **Step 5: 跑測試確認通過**

```bash
pytest tests/notification/test_channels_line.py -v
```

Expected: 3 passed（注意 fallback test 用 `recipient_user_id=42` 是 int，會走 warning 路徑不 call push — 要修測試讓 fallback test 用 str）

修正測試 `test_line_adapter_fallback_push_text_for_unmapped_event_type`：

```python
def test_line_adapter_fallback_push_text_for_unmapped_event_type():
    fake_ls = MagicMock()
    adapter = LineAdapter(fake_ls)
    # 用 str recipient 模擬 Phase 2 resolve 後的狀態
    adapter.send(
        _evt("leave.approved", recipient_user_id="Uxxxxxxxxxx"),
        Rendered(title="標題", body="內文", deep_link="/x"),
        log_id=1,
    )
    fake_ls.push_text_to_user.assert_called_once()
    args = fake_ls.push_text_to_user.call_args[0]
    assert args[0] == "Uxxxxxxxxxx"
    assert "標題" in args[1]
```

再加一條：

```python
def test_line_adapter_skips_when_recipient_is_int_with_warning(caplog):
    """Phase 1 fallback：未 resolve 為 LINE user_id 的 int 應 skip + warning。"""
    fake_ls = MagicMock()
    adapter = LineAdapter(fake_ls)
    with caplog.at_level("WARNING"):
        adapter.send(
            _evt("leave.approved", recipient_user_id=42),
            Rendered(title="t", body="b", deep_link=None),
            log_id=1,
        )
    fake_ls.push_text_to_user.assert_not_called()
    assert any("recipient_user_id" in r.message for r in caplog.records)
```

跑測試：

```bash
pytest tests/notification/test_channels_line.py -v
```

Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add services/notification/_channels/line.py services/line_service.py \
  tests/notification/test_channels_line.py
git commit -m "feat(notification): add LineAdapter + LINE_HANDLERS thin dispatch (fallback push_text)"
```

---

## Task 11: dispatch._fan_out — log row + active_channels + adapter loop + in_app 路徑

**Files:**
- Modify: `services/notification/dispatch.py`（替換 `_fan_out` stub 為實作）
- Test: `tests/notification/test_dispatch_fan_out.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""dispatch._fan_out 整合測試：log row + channels_* + adapter 呼叫順序。"""

import pytest
from unittest.mock import patch, MagicMock

from services.notification import dispatch
from services.notification.dispatch import PendingEvent


def _pevt(event_type, channels, **kwargs):
    return PendingEvent(
        event_type=event_type,
        recipient_user_id=kwargs.get("recipient_user_id", 42),
        context=kwargs.get("context", {
            "reviewer_name": "X", "leave_type": "事假",
            "start": "2026-06-01", "end": "2026-06-02", "leave_id": 1,
        }),
        sender_id=kwargs.get("sender_id"),
        source_entity_type=kwargs.get("source_entity_type"),
        source_entity_id=kwargs.get("source_entity_id"),
        channels=channels,
    )


def test_fan_out_writes_log_row_with_rendered_fields(test_db_session):
    from models.database import NotificationLog
    with patch("services.notification._channels.ws._inbox_ws_push"):
        with patch("services.notification.dispatch._line_adapter") as mock_la:
            dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))

    rows = test_db_session.query(NotificationLog).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "leave.approved"
    assert row.recipient_user_id == 42
    assert "X" in row.title
    assert row.deep_link == "/portal/leaves/1"
    assert "in_app" in row.channels_attempted
    assert "in_app" in row.channels_succeeded


def test_fan_out_in_app_path_calls_inbox_ws_push(test_db_session):
    with patch("services.notification.dispatch._inbox_ws_push") as mock_ipush, \
         patch("services.notification.dispatch._line_adapter"):
        dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))
    mock_ipush.assert_called_once()


def test_fan_out_inbox_ws_push_failure_does_not_mark_in_app_failed(test_db_session):
    """inbox WS 失敗只 warning，in_app 仍算 succeeded（log row 已寫）。"""
    from models.database import NotificationLog
    with patch("services.notification.dispatch._inbox_ws_push",
               side_effect=RuntimeError("hub down")), \
         patch("services.notification.dispatch._line_adapter"):
        dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))

    row = test_db_session.query(NotificationLog).first()
    assert "in_app" in row.channels_succeeded
    assert all(f.get("channel") != "in_app" for f in row.channels_failed)


def test_fan_out_line_adapter_failure_marks_channels_failed(test_db_session):
    from models.database import NotificationLog
    mock_la = MagicMock()
    mock_la.send.side_effect = RuntimeError("LINE 5xx")
    with patch("services.notification.dispatch._inbox_ws_push"), \
         patch("services.notification.dispatch._line_adapter", mock_la):
        dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))

    row = test_db_session.query(NotificationLog).first()
    assert any(f.get("channel") == "line" and "LINE 5xx" in str(f.get("error", ""))
               for f in row.channels_failed)
    assert "line" not in row.channels_succeeded


def test_fan_out_parent_event_skips_in_app(test_db_session):
    """家長域 v1 不寫 in_app（matrix 沒給 in_app）。"""
    from models.database import NotificationLog
    mock_ws = MagicMock()
    with patch("services.notification.dispatch._ws_adapter", mock_ws), \
         patch("services.notification.dispatch._line_adapter") as mock_la:
        dispatch._fan_out(_pevt(
            "parent.announcement", ("line",),
            context={"title": "x", "preview": "y", "announcement_id": 1},
        ))

    # 家長域不寫 log row
    rows = test_db_session.query(NotificationLog).all()
    assert rows == []
    # 仍走 line adapter
    mock_la.send.assert_called_once()


def test_fan_out_preference_disabled_skips_line(test_db_session):
    """user 關閉 line preference 應跳過 LINE adapter（in_app 仍寫）。"""
    from models.database import NotificationPreference  # rename 後新名
    test_db_session.add(NotificationPreference(
        user_id=42, event_type="leave.approved", channel="line", enabled=False,
    ))
    test_db_session.commit()

    mock_la = MagicMock()
    with patch("services.notification.dispatch._inbox_ws_push"), \
         patch("services.notification.dispatch._line_adapter", mock_la):
        dispatch._fan_out(_pevt("leave.approved", ("in_app", "line")))

    mock_la.send.assert_not_called()
    from models.database import NotificationLog
    row = test_db_session.query(NotificationLog).first()
    assert "in_app" in row.channels_succeeded
    assert "line" not in row.channels_succeeded
    assert "line" not in row.channels_attempted  # 被 gate 篩掉就不算 attempted
```

注意 `NotificationPreference` — Task 4 migration 已 rename 表但**還沒重命名 model class**。`models/parent_notification.py` 還是 `class ParentNotificationPreference`。短期相容：在 `models/parent_notification.py` 加 alias `NotificationPreference = ParentNotificationPreference` 並 re-export，或乾脆 rename class。**選擇 rename class** 因為這是 Phase 1 一次到位的清理。

加 Step 1.5：在實作 fan_out 前 rename model class。

- [ ] **Step 1.5: rename model class `ParentNotificationPreference → NotificationPreference`（兩階段相容）**

修改 `models/parent_notification.py`：

```python
# 在檔尾 class ParentNotificationPreference 後加 alias
NotificationPreference = ParentNotificationPreference
```

修改 `models/database.py:126`：

```python
from models.parent_notification import ParentNotificationPreference, NotificationPreference  # noqa
```

並在 `__all__` 加 `"NotificationPreference"`。

**rename class 完整版**（Phase 1 一次清理）暫不做（會牽動 `api/parent_portal/notifications.py` 與測試），用 alias 模式即可，待 Phase 2 一次清理。

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_dispatch_fan_out.py -v
```

Expected: AttributeError（`_fan_out` 還是 stub raise NotImplementedError 或 `_line_adapter` 不存在）

- [ ] **Step 3: 實作 `_fan_out` 在 `services/notification/dispatch.py` 內**

替換 Task 7 留下的 `_fan_out` stub：

```python
# 將原本的 stub 替換為以下實作

from models.base import get_session_factory
from services.notification._channels.line import LineAdapter
from services.notification._channels.ws import WsAdapter, _inbox_ws_push
from services.notification.renderers import render

# Adapter singletons（lazy-init；Phase 1 LINE adapter 用 module-level fake_ls 註入 in tests）
_line_adapter: LineAdapter | None = None
_ws_adapter: WsAdapter | None = None


def _get_line_adapter() -> LineAdapter:
    global _line_adapter
    if _line_adapter is None:
        # 從既有 line_service singleton 取（main.py:183 `line_service = LineService()`）
        from main import line_service
        _line_adapter = LineAdapter(line_service)
    return _line_adapter


def _get_ws_adapter() -> WsAdapter:
    global _ws_adapter
    if _ws_adapter is None:
        _ws_adapter = WsAdapter()
    return _ws_adapter


def _pref_enabled(session, user_id: int | None, event_type: str, channel: str) -> bool:
    """偏好 gate：缺 row = True；row 存在看 enabled 欄。

    無 recipient（群組推播）視為 enabled（gate 不適用）。
    DB 異常 fail-closed 沿用既有 should_push_to_parent 慣例。
    """
    if user_id is None:
        return True
    try:
        from models.database import NotificationPreference
        row = (
            session.query(NotificationPreference)
            .filter(
                NotificationPreference.user_id == user_id,
                NotificationPreference.event_type == event_type,
                NotificationPreference.channel == channel,
            )
            .first()
        )
        if row is None:
            return True
        return bool(row.enabled)
    except Exception as exc:
        logger.warning("_pref_enabled failed (fail-closed): %s", exc)
        return False


def _fan_out(evt: PendingEvent) -> None:
    """tx commit 後實際發送：寫 log → 過 gate → 呼叫 adapter。

    任何 channel 失敗只記 channels_failed，不 re-raise。
    """
    log_session = get_session_factory()()
    try:
        rendered = render(evt.event_type, evt.context)

        # 篩 active channels：in_app 必（matrix 有就一定走）；line/ws 過 pref gate
        active_channels: list[str] = []
        for ch in evt.channels:
            if ch == "in_app":
                active_channels.append(ch)
            elif _pref_enabled(log_session, evt.recipient_user_id, evt.event_type, ch):
                active_channels.append(ch)

        # 只有 in_app 在 matrix 內才寫 log row（家長域沒 in_app 就不寫，但仍跑 line/ws）
        log_id: int | None = None
        if "in_app" in evt.channels:
            log_row = NotificationLog(
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
                channels_succeeded=["in_app"],  # in_app 在 INSERT 成功瞬間算成功
                channels_failed=[],
            )
            log_session.add(log_row)
            log_session.commit()
            log_id = log_row.id

            # in_app 路徑後立刻推 inbox WS；失敗只 warning 不算 in_app failure
            if evt.recipient_user_id is not None:
                try:
                    _inbox_ws_push(evt, rendered, log_id)
                except Exception as exc:
                    logger.warning(
                        "inbox WS push 失敗 log_id=%s event=%s: %s",
                        log_id, evt.event_type, exc,
                    )

        # 跑 line / ws adapter（in_app 已處理過跳過）
        succeeded: list[str] = []
        failed: list[dict] = []
        for ch in active_channels:
            if ch == "in_app":
                continue
            adapter = _get_line_adapter() if ch == "line" else _get_ws_adapter()
            try:
                adapter.send(evt, rendered, log_id=log_id or 0)
                succeeded.append(ch)
            except Exception as exc:
                logger.exception(
                    "channel %s failed event=%s recipient=%s",
                    ch, evt.event_type, evt.recipient_user_id,
                )
                failed.append({"channel": ch, "error": type(exc).__name__})

        # 更新 log row 的 channels_succeeded / channels_failed（若有寫 log）
        if log_id is not None and (succeeded or failed):
            row = log_session.query(NotificationLog).get(log_id)
            if row is not None:
                row.channels_succeeded = list(row.channels_succeeded) + succeeded
                row.channels_failed = list(row.channels_failed) + failed
                log_session.commit()
    finally:
        log_session.close()


# 必要 imports 在檔頂補上：
# from models.database import NotificationLog
```

在檔頂 import 區補：

```python
from models.database import NotificationLog
```

- [ ] **Step 4: 跑測試確認通過**

```bash
pytest tests/notification/test_dispatch_fan_out.py -v
```

Expected: 6 passed

可能要 tweak `recipient_user_id` 在 LineAdapter fallback 是否為 str；測試已 mock `_line_adapter` 所以不會走實際 push，OK。

- [ ] **Step 5: Commit**

```bash
git add services/notification/dispatch.py models/parent_notification.py models/database.py \
  tests/notification/test_dispatch_fan_out.py
git commit -m "feat(notification): implement _fan_out (log row + pref gate + adapter loop)"
```

---

## Task 12: main.py lifespan wiring — install_session_hooks + sanity assert

**Files:**
- Modify: `main.py`（lifespan 內加 install + WS loop assert）
- Test: `tests/notification/test_main_wiring.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""main.py lifespan 把 dispatch hook 綁到 production session factory + WS loop 已註冊。"""

import pytest
from unittest.mock import patch


def test_main_lifespan_installs_dispatch_hooks():
    """import main 後 dispatch._HOOKS_INSTALLED 應包含 production factory。"""
    # main 是 FastAPI app；lifespan 是 contextmanager，import main 不會跑
    # 此 test 透過直接 call lifespan 函式體驗
    import asyncio
    from main import app_lifespan, app
    from models.base import get_session_factory
    from services.notification import dispatch

    # clear sentinel
    dispatch._HOOKS_INSTALLED.discard(get_session_factory())

    async def run():
        async with app_lifespan(app):
            pass

    asyncio.run(run())
    assert get_session_factory() in dispatch._HOOKS_INSTALLED


def test_main_lifespan_sets_main_loop():
    import asyncio
    from main import app_lifespan, app
    from utils.event_loop import get_main_loop

    async def run():
        async with app_lifespan(app):
            assert get_main_loop() is not None

    asyncio.run(run())
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_main_wiring.py -v
```

Expected: AssertionError（hook 未綁）

- [ ] **Step 3: 修改 `main.py` lifespan**

找到 `main.py:283 on_startup()` 上方（`set_main_loop(_main_loop)` 之後），加：

```python
    # 通知中央 dispatcher：把 after_commit / after_rollback hook 綁到主庫 session factory
    from services.notification import dispatch as _notification_dispatch
    from models.base import get_session_factory as _get_factory
    _notification_dispatch.install_session_hooks(_get_factory())
    logger.info("notification dispatch hooks installed")
```

- [ ] **Step 4: 跑測試確認通過**

```bash
pytest tests/notification/test_main_wiring.py -v
```

Expected: 2 passed

如果第一個 test 因為 main.py 其它 import 副作用炸（線上 DB / Sentry / scheduler 啟動），改用更直接的測法：直接 mock 掉 `on_startup` 等其它副作用，只跑 dispatch 那一行。或 skip 並改為手動驗證（uvicorn 啟動 + 看 log）。

- [ ] **Step 5: Commit**

```bash
git add main.py tests/notification/test_main_wiring.py
git commit -m "feat(notification): wire dispatch hooks in main.py lifespan"
```

---

## Task 13: `api/parent_portal/notifications.py` event_type 加 `parent.` 前綴（過渡相容）

**Files:**
- Modify: `models/parent_notification.py`（更新 `PARENT_NOTIFICATION_EVENT_TYPES` 常數加前綴）
- Modify: `api/parent_portal/notifications.py`（GET response 用新 key；PUT 接受新舊 key 過渡）
- Test: `tests/test_parent_notification_prefs.py`（既有測試更新預期 keys）

- [ ] **Step 1: 看一下既有 test**

```bash
cat tests/test_parent_notification_prefs.py | head -50
```

理解既有測試對 keys 的期待。

- [ ] **Step 2: 更新 `PARENT_NOTIFICATION_EVENT_TYPES` 常數加前綴**

修改 `models/parent_notification.py:25-33`：

```python
PARENT_NOTIFICATION_EVENT_TYPES = (
    "parent.message_received",
    "parent.announcement",
    "parent.event_ack_required",
    "parent.fee_due",
    "parent.leave_result",
    "parent.attendance_alert",
    "parent.contact_book_published",
)
```

- [ ] **Step 3: `api/parent_portal/notifications.py` PUT 接受新舊 key 過渡相容**

修改 `update_preferences` 內 unknown 檢查那段（line 76-84）：

```python
    OLD_TO_NEW = {ev.replace("parent.", ""): ev for ev in PARENT_NOTIFICATION_EVENT_TYPES}
    normalized: dict[str, bool] = {}
    unknown: list[str] = []
    for k, v in payload.prefs.items():
        if k in PARENT_NOTIFICATION_EVENT_TYPES:
            normalized[k] = v
        elif k in OLD_TO_NEW:
            normalized[OLD_TO_NEW[k]] = v   # 舊 key 自動轉新
        else:
            unknown.append(k)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的 event_type：{unknown}；可選值：{list(PARENT_NOTIFICATION_EVENT_TYPES)}",
        )
    # 後續 for loop 改用 normalized 取代 payload.prefs
    for ev, enabled in normalized.items():
        ...
```

GET response shape 不變（仍是 `{prefs: {event_type: bool}}`），但 keys 換成新 `parent.*` 前綴。

- [ ] **Step 4: 更新既有 test_parent_notification_prefs.py 預期 keys**

把測試中所有 `"message_received"` / `"announcement"` 等 7 個舊 key 改為 `"parent.message_received"` / `"parent.announcement"` 等新 key（GET 預期）。PUT 測試可同時加一個「送舊 key 也成功（過渡相容）」的 case。

執行：

```bash
pytest tests/test_parent_notification_prefs.py -v
```

調整測試直到全綠。

- [ ] **Step 5: 跑既有 router 全套確認零回歸**

```bash
pytest tests/test_parent_notification_prefs.py tests/test_parent_portal*.py -v
```

Expected: 全綠

- [ ] **Step 6: Commit**

```bash
git add models/parent_notification.py api/parent_portal/notifications.py \
  tests/test_parent_notification_prefs.py
git commit -m "feat(notification): rename parent event_types to parent.* prefix (PUT backward compat)"
```

---

## Task 14: 整合 smoke test — `enqueue → commit → log row + adapter 呼叫`

**Files:**
- Test: `tests/notification/test_integration_smoke.py`

- [ ] **Step 1: 寫測試**

```python
"""Phase 1 整合 smoke：完整 enqueue → commit → fan-out → log row。"""

import pytest
from unittest.mock import patch, MagicMock

from services.notification import dispatch
from models.database import NotificationLog


def test_full_lifecycle_employee_event(test_db_session):
    """員工域：enqueue → commit → 應寫 log row + line adapter 被呼叫。"""
    with patch("services.notification.dispatch._inbox_ws_push") as mock_ipush, \
         patch("services.notification.dispatch._get_line_adapter") as mock_get_la:
        mock_la = MagicMock()
        mock_get_la.return_value = mock_la

        dispatch.enqueue(
            test_db_session,
            event_type="leave.approved",
            recipient_user_id=42,
            context={"reviewer_name": "張主任", "leave_type": "事假",
                     "start": "2026-06-01", "end": "2026-06-02", "leave_id": 1},
            sender_id=7,
            source_entity_type="leave_request",
            source_entity_id=1,
        )
        test_db_session.commit()

    rows = test_db_session.query(NotificationLog).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.recipient_user_id == 42
    assert row.sender_id == 7
    assert row.event_type == "leave.approved"
    assert row.source_entity_id == 1
    assert "張主任" in row.title
    assert "in_app" in row.channels_succeeded

    mock_la.send.assert_called_once()
    mock_ipush.assert_called_once()


def test_full_lifecycle_parent_event_no_log_row(test_db_session):
    """家長域：無 in_app channel → 不寫 log row，但 adapter 仍呼叫。"""
    with patch("services.notification.dispatch._get_line_adapter") as mock_get_la, \
         patch("services.notification.dispatch._get_ws_adapter") as mock_get_ws:
        mock_la = MagicMock()
        mock_ws = MagicMock()
        mock_get_la.return_value = mock_la
        mock_get_ws.return_value = mock_ws

        dispatch.enqueue(
            test_db_session,
            event_type="parent.message_received",
            recipient_user_id=99,
            context={"teacher_name": "王老師", "student_name": "小明",
                     "body_preview": "今天很乖", "thread_id": 7},
        )
        test_db_session.commit()

    rows = test_db_session.query(NotificationLog).all()
    assert rows == []  # 家長域 v1 不寫 in_app

    mock_la.send.assert_called_once()  # LINE
    mock_ws.send.assert_called_once()  # parent WS


def test_rollback_does_not_send(test_db_session):
    """rollback 後 queue 清空，無 adapter 呼叫。"""
    with patch("services.notification.dispatch._get_line_adapter") as mock_get_la:
        mock_la = MagicMock()
        mock_get_la.return_value = mock_la

        dispatch.enqueue(
            test_db_session, event_type="leave.approved",
            recipient_user_id=42,
            context={"reviewer_name": "X", "leave_type": "事假",
                     "start": "2026-06-01", "end": "2026-06-02", "leave_id": 1},
        )
        test_db_session.rollback()

    mock_la.send.assert_not_called()
    rows = test_db_session.query(NotificationLog).all()
    assert rows == []
```

- [ ] **Step 2: 跑測試確認通過**

```bash
pytest tests/notification/test_integration_smoke.py -v
```

Expected: 3 passed

如有 mock path 不對的問題（例如 `_get_line_adapter` 不存在）回頭調 Task 11 的實作（adapter getter 名稱統一）。

- [ ] **Step 3: Commit**

```bash
git add tests/notification/test_integration_smoke.py
git commit -m "test(notification): add Phase 1 integration smoke test"
```

---

## Task 15: 全套回歸 + push branch

**Files:** N/A

- [ ] **Step 1: 跑 dispatch package 全套**

```bash
pytest tests/notification/ -v --tb=short
```

Expected: 全綠（約 35+ 條測試）

- [ ] **Step 2: 跑全 backend 測試確認零回歸**

```bash
pytest tests/ -x --tb=short -q
```

Expected: 4486+ passed（與 main HEAD 數字一致）。失敗的話定位是否 dispatch 副作用造成的 import side effect / global state pollution。

- [ ] **Step 3: 重啟本機 backend 跑 smoke**

```bash
# 從 worktree 切回主 repo（或繼續用 worktree 跑也可）
cd ~/Desktop/ivyManageSystem && ./start.sh
```

開另一終端 curl：

```bash
# 確認 server 起來且 /docs 看得到（dispatch 不影響 router）
curl -sf http://localhost:8088/docs > /dev/null && echo "BE OK"
```

不需要驗證 dispatch 真的發 LINE — 那是 Phase 2 caller 接上才會發。Phase 1 只驗 backend 啟動正常。

`Ctrl+C` 停 server。

- [ ] **Step 4: Push branch**

```bash
cd ~/Desktop/ivy-backend/.claude/worktrees/notification-dispatch-phase-1-2026-05-25-backend
git push -u origin feat/notification-dispatch-phase-1-2026-05-25-backend
```

- [ ] **Step 5: 開 PR**

```bash
gh pr create --title "feat(notification): Phase 1 dispatch skeleton + notification_logs + rename pref table" \
  --body "$(cat <<'EOF'
## Summary
- 新增 `services/notification/dispatch.py` 中央 dispatcher 入口（`enqueue()` + SQLAlchemy `after_commit` hook 自動 fan-out）
- 新增 `notification_logs` 表（in-app 持久層 + audit）
- Rename `parent_notification_preferences` → `notification_preferences`（合表，未來員工 preference 共用）
- 19 個 event_type 兩級命名 + 宣告式 `CHANNEL_MATRIX` + 17 renderer
- LINE / WS adapter（thin wrapper；line_service 21 method 保留 Phase 2 才遷）
- `api/inbox_ws.py` skeleton（hub key + broadcast helper；WS endpoint Phase 3 補）
- Phase 1 **零 caller 切換**，純 additive 行為零變化

## Spec / Plan
- Spec: `docs/superpowers/specs/2026-05-25-notification-dispatch-design.md`
- Plan: `docs/superpowers/plans/2026-05-25-notification-dispatch-phase-1.md`

## Migration
- `notif01_consolidation` 套用：rename 表 + 加 index + backfill `parent.` 前綴 + 建 `notification_logs`
- 本機已驗 up/down 對稱
- Downgrade 反向 backfill 去前綴

## Frontend coordination
- 家長 preference key `"message_received"` → `"parent.message_received"`；PUT 過渡相容收舊 key
- Frontend `src/api/notifications.ts` + `NotificationPrefsView` keys 需更新 → **separate PR after this merged**

## Test plan
- [ ] CI 全綠
- [ ] OpenAPI drift CI 過（schema.d.ts 應更新，因 PARENT_NOTIFICATION_EVENT_TYPES enum 變動）
- [ ] 本機 alembic up/down 對稱
- [ ] 本機重啟 backend `/docs` 可開
- [ ] Phase 1 不會發任何實際通知（無 caller）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: 通知 user PR 開好**

回報 PR URL + 提示「CI 過後手動 merge → 之後 Phase 2 plan 另開」。

---

## Task 16: 前端 keys 更新（separate PR，repo: ivy-frontend）

**Note:** 不在本 backend plan scope；BE 本 PR merged 後另開 frontend 短 PR。本 task 只列指令給 user 參考。

- [ ] **Step 1: 切到 frontend worktree**

```bash
cd ~/Desktop/ivy-frontend
git worktree add .claude/worktrees/notification-prefs-prefix-2026-05-25-frontend \
  -b feat/notification-prefs-prefix-2026-05-25-frontend main
cd .claude/worktrees/notification-prefs-prefix-2026-05-25-frontend
```

- [ ] **Step 2: regen OpenAPI types**

```bash
# 確認 BE Phase 1 已 merge + 本地 BE 在跑（schema 才能 dump）
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
cd ~/Desktop/ivy-frontend && npm run gen:api
```

- [ ] **Step 3: 在 `src/api/notifications.ts` 與 `src/views/NotificationPrefsView.vue`（或對應檔）**

把 7 個事件 key constant 加 `parent.` 前綴：

```typescript
// before
const EVENT_TYPES = ['message_received', 'announcement', ...] as const;
// after
const EVENT_TYPES = [
  'parent.message_received', 'parent.announcement',
  'parent.event_ack_required', 'parent.fee_due',
  'parent.leave_result', 'parent.attendance_alert',
  'parent.contact_book_published',
] as const;
```

對應 UI 顯示的中文 label 對映 dict 也要 update key。

- [ ] **Step 4: 跑 vitest + typecheck + build**

```bash
npm test -- src/views/NotificationPrefsView
npm run typecheck
npm run build
```

- [ ] **Step 5: Commit + push + PR**

```bash
git add src/api/notifications.ts src/views/NotificationPrefsView.vue src/api/_generated/schema.d.ts
git commit -m "feat(notification): rename parent pref keys to parent.* prefix"
git push -u origin feat/notification-prefs-prefix-2026-05-25-frontend
gh pr create --title "feat(notification): rename parent pref keys to parent.* prefix" \
  --body "..."
```

---

## Self-Review checklist

完成所有 task 後，自我檢查：

- [ ] **Spec 對映**：
  - Spec §1（背景）→ 不需 task，是動機文件
  - Spec §2（總覽圖）→ Task 1-12 共同實現
  - Spec §3.1-3.4（17 event + matrix + dispatch API）→ Task 1, 2, 6
  - Spec §4.1（notification_logs schema）→ Task 3, 4
  - Spec §4.2（rename + backfill）→ Task 4, 13
  - Spec §5.1（enqueue）→ Task 6
  - Spec §5.2（after_commit hook）→ Task 7
  - Spec §5.3（_fan_out + short-lived session）→ Task 11
  - Spec §5.4（renderer）→ Task 5
  - Spec §5.5（WS adapter + bridge）→ Task 9
  - Spec §5.6（LINE adapter）→ Task 10
  - Spec §5.7（scheduler）→ test_db_session fixture install hook 自動涵蓋
  - Spec §6.1（Phase 1）→ 本 plan 全部
  - Spec §6.2 Phase 2（caller 遷移）→ **不在本 plan，下次另開**
  - Spec §6.3 Phase 3（員工通知中心 UI）→ **不在本 plan，下次另開**
  - Spec §7（員工通知中心 UI）→ Phase 3
  - Spec §8.1（錯誤處理矩陣）→ Task 7（hook swallow），11（adapter failure），渲染 fallback in Task 5
  - Spec §8.2（測試覆蓋）→ Task 1-14 累計
  - Spec §8.3（OpenAPI drift）→ Task 15 Step 5 PR 列入 CI gate
  - Spec §8.4（PII）→ adapter 錯誤訊息已 type only（_fan_out logger.exception 不夾 payload）

- [ ] **Placeholder 掃描**：搜 `TODO|TBD|FIXME|placeholder|...` — Step 1 grep
  ```bash
  grep -n "TODO\|TBD\|FIXME" docs/superpowers/plans/2026-05-25-notification-dispatch-phase-1.md
  ```
  排除：型別語法 `tuple[..., ...]`、Step 內 elision `...`、Task 16 「待 Phase 3」標註

- [ ] **Type / 命名一致性**：
  - `_QUEUE_KEY` 在 dispatch.py 定義、test 直接 import → ✓
  - `Channel = Literal["in_app", "line", "ws"]` 在 channel_matrix.py → ✓
  - `Rendered` dataclass 在 renderers.py → ✓ adapter 都用同型別
  - `PendingEvent` 在 dispatch.py → ✓ test 與 _fan_out 都用同型別
  - `NotificationLog` re-export 在 models/database.py → ✓
  - `NotificationPreference` alias 在 models/parent_notification.py → ✓

- [ ] **回歸保護**：Task 15 跑全套 4486+ 確認零回歸

---

## 後續

Phase 1 merged 後：
1. 通知 user 開 Phase 2 plan（21+ caller 分 4 PR 遷移）
2. 通知 user 開 Phase 3 plan（員工通知中心 UI 前端 + 後端 4 endpoint + WS endpoint）
3. Phase 4 outbox 升級 + 900 天 GC 列為 backlog


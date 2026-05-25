# 通知中央 Dispatcher Phase 3 Implementation Plan — 員工通知中心 UI

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把員工通知從「LINE 訊息流瞬間消失」升級為「持久通知中心」：後端補完 4 REST endpoint + 1 WS endpoint，前端在 `AdminHeader` 加門鈴 icon + 紅點未讀數 + 右側抽屜列表 + WS realtime 更新。

**Architecture:** Phase 1 已寫 `notification_logs` table + dispatch.py fan-out + `api/inbox_ws.py` skeleton（hub key + broadcast helper）。本 Phase 3 補：(1) 後端 4 個 REST endpoint（list / unread_count / mark_read / mark_all_read）+ JWT auth 的 `/inbox` WebSocket endpoint。(2) 前端 `src/api/notifications.ts` + Pinia store + composable + 5 個 component。每筆通知 click 走 deep_link router.push 並 mark_read。

**Tech Stack:** FastAPI（後端）, Vue 3 `<script setup lang="ts">` + Element Plus + Pinia + Vitest（前端）, OpenAPI codegen 防漂移, contact_book_ws 的 hub pattern（複用）。

**Spec：** `docs/superpowers/specs/2026-05-25-notification-dispatch-design.md` §7

**前置：** Phase 1 已 merged（dispatch + log + inbox_ws skeleton）。Phase 2 不必先完成 — Phase 3 後端只用 Phase 1 schema，前端只用 dispatch 寫入的 log row（無論哪個 caller 寫的都能顯示）。

**Phase 3 不含：** 家長端通知中心（spec 明確 defer）、settings 通知偏好 UI（員工 preference 表 schema 已就緒但 UI defer 到 Phase 4）。

**完成判準：**
1. 員工登入後右上看到門鈴 icon
2. dispatch.enqueue 一筆 `leave.approved` → 門鈴紅點數字 +1（realtime via WS）
3. click 門鈴 → drawer 列表顯示該筆 + 「全部已讀」按鈕
4. click 單筆 → mark_read + `router.push(deep_link)` + drawer 關閉
5. 重整頁面後未讀數仍正確（持久層 OK）

---

## 檔案結構

**新建 / 修改（Backend，~1.5 工作日）：**
- `api/notifications.py`（新）— 4 個 REST endpoint
- `api/inbox_ws.py`（modify — 從 Phase 1 skeleton 升級）— 加 `@router.websocket("/inbox")` endpoint + JWT cookie auth
- `main.py`（modify）— include `api/notifications.py` router + `api/inbox_ws.py` router（如尚未掛載）
- `schemas/notifications.py`（新，OpenAPI 用）— `NotificationItem` / `UnreadCount` / `MarkReadResponse`
- `tests/notification/test_api_notifications.py`（新）— 涵蓋 6 case：list 分頁、unread_count、mark_read、mark_all_read、self-isolation、家長不能讀
- `tests/notification/test_inbox_ws_endpoint.py`（新）— 涵蓋 3 case：JWT auth pass、auth fail、subscribe + receive

**新建（Frontend，repo: ivy-frontend，~5 工作日）：**
- `src/api/notifications.ts` — 4 endpoint wrapper（OpenAPI 型別）
- `src/composables/useInbox.ts` — WS subscribe + Pinia glue + reconnect
- `src/stores/inbox.ts` — Pinia store: items / unreadCount / loading / hasMore
- `src/components/inbox/InboxBell.vue` — 門鈴 icon + 紅點數字（含 `99+`）
- `src/components/inbox/InboxDrawer.vue` — 右側 el-drawer 360px + infinite scroll
- `src/components/inbox/InboxItem.vue` — 單筆 card
- `src/views/AdminHeader.vue`（modify） — 加 `<InboxBell>` 在 UserMenuDropdown 左邊
- `src/App.vue`（modify） — useInbox composable 在 mount 時建立 WS 連線

**新測試（Frontend）：**
- `src/composables/__tests__/useInbox.spec.ts` — WS message handling / dedupe by log_id / reconnect
- `src/stores/__tests__/inbox.spec.ts` — mark_read optimistic update / rollback on 500
- `src/components/inbox/__tests__/InboxBell.spec.ts` — 未讀數渲染 / `99+`
- `src/components/inbox/__tests__/InboxDrawer.spec.ts` — list / mark_read flow / 空狀態

**OpenAPI codegen sync：**
- 後端 PR merged 後 frontend 跑 `npm run gen:api` 更新 `src/api/_generated/schema.d.ts`

**不動：**
- `services/notification/dispatch.py` — Phase 1 已穩定
- `models/notification_log.py` — Phase 1 schema 完整
- Phase 2 caller — Phase 3 不依賴特定 caller，只要 dispatch.enqueue 寫了 log 就能顯示

---

## 約定

- **後端 PR 先合**，前端 PR 才能 regen schema.d.ts。SOP 同 workspace CLAUDE.md
- 兩 repo 各開 worktree
- 後端 worktree：`ivy-backend/.claude/worktrees/notification-inbox-phase-3-2026-05-25-backend`
- 前端 worktree：`ivy-frontend/.claude/worktrees/notification-inbox-phase-3-2026-05-25-frontend`
- TDD：後端 pytest，前端 vitest

---

## 後端 Phase 3a：REST endpoints + WS endpoint（~1.5 工作日，1 PR）

### Task BE0：建立 worktree

```bash
cd ~/Desktop/ivy-backend
git worktree add .claude/worktrees/notification-inbox-phase-3-2026-05-25-backend \
  -b feat/notification-inbox-phase-3-2026-05-25-backend main
cd .claude/worktrees/notification-inbox-phase-3-2026-05-25-backend
pwd
```

### Task BE1：Pydantic schema

**Files:**
- Create: `schemas/notifications.py`

- [ ] **Step 1: 看一下既有 schema 慣例**

```bash
ls schemas/ | head -10
cat schemas/employees.py | head -30
```

- [ ] **Step 2: 寫 schema**

```python
"""通知中心 REST endpoint 用 Pydantic schemas。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class NotificationItem(BaseModel):
    id: int
    event_type: str
    title: str
    body: str
    deep_link: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    sender_id: Optional[int] = None
    sender_name: Optional[str] = None  # users.display_name pre-joined
    created_at: datetime
    read_at: Optional[datetime] = None


class NotificationListResponse(BaseModel):
    items: list[NotificationItem]
    next_before_id: Optional[int] = None  # 下一頁 anchor (id 比此小)


class UnreadCountResponse(BaseModel):
    count: int


class MarkReadResponse(BaseModel):
    id: int
    read_at: datetime


class MarkAllReadResponse(BaseModel):
    marked: int


class WsInboxPayload(BaseModel):
    """WS 推送的 payload 結構（dispatch._fan_out 透過 ws.py:_inbox_ws_push 送出）。
    僅作文件用；不在 endpoint 上強制 validate。"""
    event_type: str
    title: str
    body: str
    deep_link: Optional[str] = None
    log_id: int
```

- [ ] **Step 3: Commit**

```bash
git add schemas/notifications.py
git commit -m "feat(notification): add Pydantic schemas for inbox REST endpoints"
```

### Task BE2：REST endpoints

**Files:**
- Create: `api/notifications.py`
- Test: `tests/notification/test_api_notifications.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""員工通知中心 REST endpoint 測試。"""

import pytest
from datetime import datetime
from unittest.mock import patch

from fastapi.testclient import TestClient


def _setup_user_with_notifications(session, count: int = 25):
    """建一個 user + N 筆 notification_logs。"""
    from models.database import User, NotificationLog
    user = User(
        id=100, username="employee1", password_hash="x",
        role="teacher", is_active=True,
    )
    session.add(user)
    for i in range(count):
        session.add(NotificationLog(
            recipient_user_id=100,
            event_type="leave.approved",
            title=f"通知 {i}",
            body=f"內文 {i}",
            payload_json={"i": i},
            channels_attempted=["in_app"],
            channels_succeeded=["in_app"],
            channels_failed=[],
        ))
    session.commit()
    return user


def test_list_returns_items_descending_by_created_at(test_db_session):
    _setup_user_with_notifications(test_db_session, 5)
    from main import app
    with TestClient(app) as client:
        # mock JWT auth → user_id=100
        with patch("api.notifications.require_authenticated",
                   return_value={"user_id": 100}):
            resp = client.get("/api/notifications?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 5
    assert body["items"][0]["title"] == "通知 4"  # latest first
    assert body["next_before_id"] is None  # 沒下一頁


def test_list_pagination_with_before_id(test_db_session):
    _setup_user_with_notifications(test_db_session, 25)
    from main import app
    with TestClient(app) as client:
        with patch("api.notifications.require_authenticated",
                   return_value={"user_id": 100}):
            r1 = client.get("/api/notifications?limit=10")
            assert len(r1.json()["items"]) == 10
            next_id = r1.json()["next_before_id"]
            assert next_id is not None
            r2 = client.get(f"/api/notifications?limit=10&before_id={next_id}")
            assert len(r2.json()["items"]) == 10
            # 不重複
            r1_ids = {i["id"] for i in r1.json()["items"]}
            r2_ids = {i["id"] for i in r2.json()["items"]}
            assert not r1_ids & r2_ids


def test_unread_count_returns_only_unread(test_db_session):
    _setup_user_with_notifications(test_db_session, 5)
    from models.database import NotificationLog
    # 把前 2 個標為已讀
    rows = test_db_session.query(NotificationLog).order_by(NotificationLog.id).limit(2).all()
    for r in rows:
        r.read_at = datetime.now()
    test_db_session.commit()
    from main import app
    with TestClient(app) as client:
        with patch("api.notifications.require_authenticated",
                   return_value={"user_id": 100}):
            resp = client.get("/api/notifications/unread_count")
    assert resp.json()["count"] == 3


def test_mark_read_sets_read_at(test_db_session):
    _setup_user_with_notifications(test_db_session, 1)
    from models.database import NotificationLog
    row = test_db_session.query(NotificationLog).first()
    assert row.read_at is None
    from main import app
    with TestClient(app) as client:
        with patch("api.notifications.require_authenticated",
                   return_value={"user_id": 100}):
            resp = client.post(f"/api/notifications/{row.id}/mark_read")
    assert resp.status_code == 200
    test_db_session.refresh(row)
    assert row.read_at is not None


def test_mark_all_read_only_marks_own_unread(test_db_session):
    _setup_user_with_notifications(test_db_session, 5)
    # 加另一個 user 的 row 驗 self-isolation
    from models.database import NotificationLog
    test_db_session.add(NotificationLog(
        recipient_user_id=999, event_type="leave.approved",
        title="別人的", body="x",
        channels_attempted=["in_app"], channels_succeeded=["in_app"], channels_failed=[],
    ))
    test_db_session.commit()
    from main import app
    with TestClient(app) as client:
        with patch("api.notifications.require_authenticated",
                   return_value={"user_id": 100}):
            resp = client.post("/api/notifications/mark_all_read")
    assert resp.json()["marked"] == 5  # 只 mark 自己的 5 筆
    # user 999 的 row 仍未讀
    other = test_db_session.query(NotificationLog).filter_by(recipient_user_id=999).first()
    assert other.read_at is None


def test_mark_read_other_user_returns_404_or_403(test_db_session):
    _setup_user_with_notifications(test_db_session, 1)
    from models.database import NotificationLog
    other_row = NotificationLog(
        recipient_user_id=999, event_type="leave.approved",
        title="別人的", body="x",
        channels_attempted=["in_app"], channels_succeeded=["in_app"], channels_failed=[],
    )
    test_db_session.add(other_row)
    test_db_session.commit()
    from main import app
    with TestClient(app) as client:
        with patch("api.notifications.require_authenticated",
                   return_value={"user_id": 100}):
            resp = client.post(f"/api/notifications/{other_row.id}/mark_read")
    assert resp.status_code in (403, 404)
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
pytest tests/notification/test_api_notifications.py -v
```

Expected: ModuleNotFoundError (`api.notifications` 不存在)

- [ ] **Step 3: 實作 `api/notifications.py`**

```python
"""員工通知中心 REST endpoints — list / unread_count / mark_read / mark_all_read。

權限：require_authenticated()，自我隔離（只能讀寫 own user_id 的 row）。
家長端不掛此 router（家長走 LIFF，無通知中心 UI）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from models.base import get_session_dep
from models.database import NotificationLog, User
from schemas.notifications import (
    NotificationItem,
    NotificationListResponse,
    UnreadCountResponse,
    MarkReadResponse,
    MarkAllReadResponse,
)
from utils.auth import require_authenticated

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _to_item(row: NotificationLog, sender_name: Optional[str]) -> NotificationItem:
    return NotificationItem(
        id=row.id,
        event_type=row.event_type,
        title=row.title,
        body=row.body,
        deep_link=row.deep_link,
        payload=row.payload_json or {},
        sender_id=row.sender_id,
        sender_name=sender_name,
        created_at=row.created_at,
        read_at=row.read_at,
    )


@router.get("", response_model=NotificationListResponse)
def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    before_id: Optional[int] = Query(None),
    only_unread: bool = Query(False),
    current_user: dict = Depends(require_authenticated),
    session: Session = Depends(get_session_dep),
):
    user_id = current_user["user_id"]
    q = (
        session.query(NotificationLog, User.display_name)
        .outerjoin(User, NotificationLog.sender_id == User.id)
        .filter(NotificationLog.recipient_user_id == user_id)
    )
    if only_unread:
        q = q.filter(NotificationLog.read_at.is_(None))
    if before_id is not None:
        q = q.filter(NotificationLog.id < before_id)
    rows = q.order_by(NotificationLog.id.desc()).limit(limit + 1).all()

    has_more = len(rows) > limit
    items = [_to_item(row, sender_name) for row, sender_name in rows[:limit]]
    next_before_id = items[-1].id if has_more and items else None

    return NotificationListResponse(items=items, next_before_id=next_before_id)


@router.get("/unread_count", response_model=UnreadCountResponse)
def get_unread_count(
    current_user: dict = Depends(require_authenticated),
    session: Session = Depends(get_session_dep),
):
    user_id = current_user["user_id"]
    count = (
        session.query(NotificationLog)
        .filter(
            NotificationLog.recipient_user_id == user_id,
            NotificationLog.read_at.is_(None),
        )
        .count()
    )
    return UnreadCountResponse(count=count)


@router.post("/{notification_id}/mark_read", response_model=MarkReadResponse)
def mark_read(
    notification_id: int,
    current_user: dict = Depends(require_authenticated),
    session: Session = Depends(get_session_dep),
):
    user_id = current_user["user_id"]
    row = (
        session.query(NotificationLog)
        .filter(NotificationLog.id == notification_id)
        .first()
    )
    if row is None or row.recipient_user_id != user_id:
        # 不洩漏存在性：返回 404
        raise HTTPException(status_code=404, detail="通知不存在")
    if row.read_at is None:
        row.read_at = datetime.now()
        session.flush()
    return MarkReadResponse(id=row.id, read_at=row.read_at)


@router.post("/mark_all_read", response_model=MarkAllReadResponse)
def mark_all_read(
    current_user: dict = Depends(require_authenticated),
    session: Session = Depends(get_session_dep),
):
    user_id = current_user["user_id"]
    now = datetime.now()
    result = (
        session.query(NotificationLog)
        .filter(
            NotificationLog.recipient_user_id == user_id,
            NotificationLog.read_at.is_(None),
        )
        .update({NotificationLog.read_at: now}, synchronize_session=False)
    )
    session.flush()
    return MarkAllReadResponse(marked=result)
```

- [ ] **Step 4: 掛 router 到 main.py**

```bash
grep -n "include_router\|from api import" main.py | head -10
```

找到 router include 區，加：

```python
from api import notifications as notifications_router
app.include_router(notifications_router.router, prefix="/api")
```

- [ ] **Step 5: 跑測試確認通過**

```bash
pytest tests/notification/test_api_notifications.py -v
```

Expected: 6 passed

如 TestClient + require_authenticated mock 有問題，調整 mock 策略（用 `dependency_overrides` 或 monkeypatch）。

- [ ] **Step 6: Commit**

```bash
git add api/notifications.py main.py tests/notification/test_api_notifications.py
git commit -m "feat(notification): add 4 REST endpoints (list/unread_count/mark_read/mark_all_read)"
```

### Task BE3：WS endpoint with JWT auth

**Files:**
- Modify: `api/inbox_ws.py`（升級 skeleton 為 full endpoint）
- Test: `tests/notification/test_inbox_ws_endpoint.py`

- [ ] **Step 1: 看 contact_book_ws.py 的 WS endpoint pattern**

```bash
sed -n '60,110p' api/contact_book_ws.py
```

理解 JWT auth + subscribe pattern。

- [ ] **Step 2: 寫失敗測試**

```python
"""inbox WS endpoint 測試：JWT auth + subscribe + receive。"""

import pytest
import json
from unittest.mock import patch


@pytest.mark.anyio
async def test_inbox_ws_rejects_missing_jwt():
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as client:
        with pytest.raises(Exception):  # WS 連線失敗
            with client.websocket_connect("/api/notifications/inbox"):
                pass


@pytest.mark.anyio
async def test_inbox_ws_accepts_valid_jwt_and_subscribes(test_db_session):
    """有效 JWT 連 WS 後，broadcast 給該 user_id 應收得到。"""
    from fastapi.testclient import TestClient
    from main import app
    from api.inbox_ws import inbox_broadcast_user

    with TestClient(app) as client:
        with patch("api.inbox_ws._authenticate_ws", return_value={"user_id": 100}):
            with client.websocket_connect("/api/notifications/inbox") as ws:
                # 給 hub 一點時間註冊 subscription
                import asyncio
                await asyncio.sleep(0.05)
                await inbox_broadcast_user(100, {"event_type": "leave.approved", "log_id": 1})
                data = ws.receive_json()
                assert data["event_type"] == "leave.approved"
                assert data["log_id"] == 1


@pytest.mark.anyio
async def test_inbox_ws_other_user_does_not_receive(test_db_session):
    """User A 連 WS，broadcast 給 User B 不應收到。"""
    from fastapi.testclient import TestClient
    from main import app
    from api.inbox_ws import inbox_broadcast_user

    with TestClient(app) as client:
        with patch("api.inbox_ws._authenticate_ws", return_value={"user_id": 100}):
            with client.websocket_connect("/api/notifications/inbox") as ws:
                import asyncio
                await asyncio.sleep(0.05)
                await inbox_broadcast_user(999, {"event_type": "x"})
                # 不應有 message — 用短 timeout receive
                try:
                    data = ws.receive_json(timeout=0.5)
                    pytest.fail(f"User 100 不應收到別人的 broadcast: {data}")
                except Exception:
                    pass  # timeout 即正確
```

- [ ] **Step 3: 跑測試確認失敗**

```bash
pytest tests/notification/test_inbox_ws_endpoint.py -v
```

Expected: failure（WS endpoint 還沒實作）

- [ ] **Step 4: 升級 `api/inbox_ws.py`**

```python
"""員工通知中心 WS。

Phase 3 完整實作：
- @router.websocket("/inbox") endpoint
- JWT cookie auth
- subscribe inbox key 後等 broadcast
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from utils.ws_hub import ChannelHub
from utils.auth import decode_access_token

logger = logging.getLogger(__name__)

hub = ChannelHub()
router = APIRouter(prefix="/notifications", tags=["notifications"])


INBOX_USER_KEY = lambda user_id: ("inbox_user", user_id)


async def inbox_broadcast_user(user_id: int, payload: dict[str, Any]) -> None:
    """推送一筆通知給單一員工的 inbox WS subscriber。"""
    await hub.broadcast([INBOX_USER_KEY(user_id)], payload)


async def _authenticate_ws(ws: WebSocket) -> dict | None:
    """從 WS cookie 取 JWT，decode 後回 user dict（test 可 mock 此函式）。"""
    token = ws.cookies.get("access_token")
    if not token:
        return None
    try:
        return decode_access_token(token)
    except Exception:
        return None


@router.websocket("/inbox")
async def inbox_ws(ws: WebSocket):
    user = await _authenticate_ws(ws)
    if user is None:
        await ws.close(code=4401)
        return
    user_id = user["user_id"]
    await ws.accept()
    await hub.subscribe(ws, [INBOX_USER_KEY(user_id)])
    try:
        while True:
            # 等 client message 或 disconnect；無 message 時 hub 推送會自動寫到 ws
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unsubscribe(ws)
```

注意 `utils.auth.decode_access_token` 可能名稱不一樣 — grep 確認：

```bash
grep -n "def decode_access_token\|def verify_token" utils/auth.py
```

如果是別的名字（例如 `_decode_token`），改用實際名稱。

- [ ] **Step 5: 掛 ws router 到 main.py**

如尚未掛載，加：

```python
from api import inbox_ws as inbox_ws_module
app.include_router(inbox_ws_module.router, prefix="/api")
```

- [ ] **Step 6: 跑測試確認通過**

```bash
pytest tests/notification/test_inbox_ws_endpoint.py -v
```

Expected: 3 passed

WS test 通常 flaky；如果 race condition 導致 timeout 行為不穩，調整 `asyncio.sleep` 或改用 mock-based test 而非 real WS。

- [ ] **Step 7: Commit**

```bash
git add api/inbox_ws.py main.py tests/notification/test_inbox_ws_endpoint.py
git commit -m "feat(notification): add WS endpoint /inbox + JWT cookie auth"
```

### Task BE4：全套測試 + push + PR

- [ ] **Step 1: 全套**

```bash
pytest tests/ -x --tb=short -q --ignore=tests/spike_rls 2>&1 | tail -8
```

Expected: 5012+ passed（+ 9 新 test）

- [ ] **Step 2: push + PR**

```bash
git push -u origin feat/notification-inbox-phase-3-2026-05-25-backend
gh pr create --title "feat(notification): Phase 3 BE — inbox REST endpoints + WS subscribe" \
  --body "$(cat <<'EOF'
## Summary
員工通知中心後端：4 REST endpoint + WS subscribe endpoint。

- `GET /api/notifications` — list 分頁
- `GET /api/notifications/unread_count` — 紅點數
- `POST /api/notifications/{id}/mark_read` — 單筆已讀
- `POST /api/notifications/mark_all_read` — 全部已讀
- `WS /api/notifications/inbox` — JWT auth + subscribe inbox events

Self-isolation：員工只能讀寫自己的 row（admin 不能讀別人）。
家長端 router 不掛此 prefix。

## Test plan
- [ ] CI 全綠
- [ ] OpenAPI drift CI 過（schema.d.ts 更新）
- [ ] 前端 PR 接著開（separate PR in ivy-frontend）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## 前端 Phase 3b：員工通知中心 UI（~5 工作日，1 PR；在 BE 3a merged 後）

### Task FE0：建立 worktree + regen OpenAPI

```bash
cd ~/Desktop/ivy-frontend && git fetch origin main
git worktree add .claude/worktrees/notification-inbox-phase-3-2026-05-25-frontend \
  -b feat/notification-inbox-phase-3-2026-05-25-frontend origin/main
cd .claude/worktrees/notification-inbox-phase-3-2026-05-25-frontend

# Regen OpenAPI types（後端 BE 3a 必須先 merged）
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
cd ~/Desktop/ivy-frontend/.claude/worktrees/notification-inbox-phase-3-2026-05-25-frontend
npm run gen:api

git add src/api/_generated/schema.d.ts
git commit -m "chore(notification): regen OpenAPI types for inbox endpoints"
```

### Task FE1：API wrapper

**Files:**
- Create: `src/api/notifications.ts`

- [ ] **Step 1: 寫 api wrapper**

```typescript
import api from './index';
import type { ApiResponse, ApiQuery, AxiosResp } from './_generated/typed';

export type NotificationItem = ApiResponse<'/notifications', 'get'>['items'][number];
export type NotificationListResponse = ApiResponse<'/notifications', 'get'>;
export type UnreadCountResponse = ApiResponse<'/notifications/unread_count', 'get'>;

export function listNotifications(
  params: ApiQuery<'/notifications', 'get'> = {},
): Promise<NotificationListResponse> {
  return api.get('/notifications', { params }).then((r) => r.data);
}

export function getUnreadCount(): Promise<UnreadCountResponse> {
  return api.get('/notifications/unread_count').then((r) => r.data);
}

export function markRead(id: number): Promise<void> {
  return api.post(`/notifications/${id}/mark_read`).then(() => undefined);
}

export function markAllRead(): Promise<{ marked: number }> {
  return api.post('/notifications/mark_all_read').then((r) => r.data);
}
```

- [ ] **Step 2: typecheck**

```bash
npm run typecheck 2>&1 | tail -10
```

Expected: 0 error

- [ ] **Step 3: Commit**

```bash
git add src/api/notifications.ts
git commit -m "feat(notification): add api/notifications.ts wrapper"
```

### Task FE2：Pinia store

**Files:**
- Create: `src/stores/inbox.ts`
- Test: `src/stores/__tests__/inbox.spec.ts`

- [ ] **Step 1: 寫失敗測試**

```typescript
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useInboxStore } from '../inbox';

vi.mock('@/api/notifications', () => ({
  listNotifications: vi.fn(),
  getUnreadCount: vi.fn(),
  markRead: vi.fn(),
  markAllRead: vi.fn(),
}));

describe('useInboxStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
  });

  it('fetchUnreadCount updates state', async () => {
    const { getUnreadCount } = await import('@/api/notifications');
    (getUnreadCount as any).mockResolvedValue({ count: 7 });
    const store = useInboxStore();
    await store.fetchUnreadCount();
    expect(store.unreadCount).toBe(7);
  });

  it('markRead optimistic + rollback on error', async () => {
    const { markRead, listNotifications } = await import('@/api/notifications');
    (listNotifications as any).mockResolvedValue({
      items: [{ id: 1, read_at: null, title: 't', body: 'b', event_type: 'x',
                 created_at: new Date().toISOString(), payload: {} }],
      next_before_id: null,
    });
    const store = useInboxStore();
    await store.fetchPage();
    store.unreadCount = 1;

    (markRead as any).mockRejectedValue(new Error('500'));
    await store.markRead(1).catch(() => {});

    // rollback：read_at 應回 null，unreadCount 應回 1
    expect(store.items[0].read_at).toBeNull();
    expect(store.unreadCount).toBe(1);
  });

  it('markAllRead clears unread', async () => {
    const { markAllRead } = await import('@/api/notifications');
    (markAllRead as any).mockResolvedValue({ marked: 5 });
    const store = useInboxStore();
    store.unreadCount = 5;
    store.items = [
      { id: 1, read_at: null } as any,
      { id: 2, read_at: null } as any,
    ];
    await store.markAllRead();
    expect(store.unreadCount).toBe(0);
    expect(store.items.every((i) => i.read_at !== null)).toBe(true);
  });

  it('prependFromWs prepends + dedupes by id', async () => {
    const store = useInboxStore();
    store.items = [
      { id: 5, title: 'old' } as any,
    ];
    store.unreadCount = 0;
    store.prependFromWs({ id: 6, title: 'new', read_at: null } as any);
    expect(store.items[0].id).toBe(6);
    expect(store.unreadCount).toBe(1);
    // dedupe: 收到重複 id 6 不會再加
    store.prependFromWs({ id: 6, title: 'new again' } as any);
    expect(store.items.length).toBe(2);
    expect(store.unreadCount).toBe(1);
  });
});
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
npm test -- src/stores/__tests__/inbox.spec.ts 2>&1 | tail -15
```

- [ ] **Step 3: 實作 `src/stores/inbox.ts`**

```typescript
import { defineStore } from 'pinia';
import { ref } from 'vue';
import {
  listNotifications,
  getUnreadCount,
  markRead as apiMarkRead,
  markAllRead as apiMarkAllRead,
  type NotificationItem,
} from '@/api/notifications';

export const useInboxStore = defineStore('inbox', () => {
  const items = ref<NotificationItem[]>([]);
  const unreadCount = ref(0);
  const loading = ref(false);
  const hasMore = ref(true);
  const nextBeforeId = ref<number | null>(null);

  async function fetchUnreadCount() {
    const r = await getUnreadCount();
    unreadCount.value = r.count;
  }

  async function fetchPage(reset = false) {
    if (loading.value) return;
    loading.value = true;
    try {
      const params: Record<string, unknown> = { limit: 20 };
      if (!reset && nextBeforeId.value !== null) {
        params.before_id = nextBeforeId.value;
      }
      const r = await listNotifications(params);
      if (reset) {
        items.value = r.items;
      } else {
        items.value.push(...r.items);
      }
      nextBeforeId.value = r.next_before_id ?? null;
      hasMore.value = r.next_before_id !== null;
    } finally {
      loading.value = false;
    }
  }

  async function markRead(id: number) {
    const target = items.value.find((i) => i.id === id);
    if (!target || target.read_at !== null) return;
    const prevReadAt = target.read_at;
    target.read_at = new Date().toISOString();
    unreadCount.value = Math.max(0, unreadCount.value - 1);
    try {
      await apiMarkRead(id);
    } catch (err) {
      target.read_at = prevReadAt;
      unreadCount.value += 1;
      throw err;
    }
  }

  async function markAllRead() {
    await apiMarkAllRead();
    const now = new Date().toISOString();
    items.value.forEach((i) => {
      if (i.read_at === null) i.read_at = now;
    });
    unreadCount.value = 0;
  }

  function prependFromWs(item: NotificationItem) {
    if (items.value.some((i) => i.id === item.id)) return;  // dedupe
    items.value.unshift(item);
    if (item.read_at === null) unreadCount.value += 1;
  }

  function reset() {
    items.value = [];
    unreadCount.value = 0;
    hasMore.value = true;
    nextBeforeId.value = null;
  }

  return {
    items,
    unreadCount,
    loading,
    hasMore,
    fetchUnreadCount,
    fetchPage,
    markRead,
    markAllRead,
    prependFromWs,
    reset,
  };
});
```

- [ ] **Step 4: 跑測試確認通過 + Commit**

```bash
npm test -- src/stores/__tests__/inbox.spec.ts 2>&1 | tail -10
git add src/stores/inbox.ts src/stores/__tests__/inbox.spec.ts
git commit -m "feat(notification): add Pinia inbox store (optimistic mark_read + WS prepend)"
```

### Task FE3：useInbox composable（WS connect + Pinia glue + reconnect）

**Files:**
- Create: `src/composables/useInbox.ts`
- Test: `src/composables/__tests__/useInbox.spec.ts`

- [ ] **Step 1: 看 contact_book WS reconnect pattern**

```bash
grep -rln "WebSocket\b\|new WebSocket" src/composables src/api src/utils 2>/dev/null | head -5
cat src/composables/<existing-ws-composable>.ts | head -60
```

- [ ] **Step 2: 寫測試 + 實作**

```typescript
// src/composables/useInbox.ts
import { onMounted, onUnmounted, ref } from 'vue';
import { useInboxStore } from '@/stores/inbox';

const WS_BASE = import.meta.env.VITE_WS_BASE_URL || '/api';
const RECONNECT_INTERVAL_MS = 3000;
const MAX_RECONNECT_INTERVAL_MS = 30000;

export function useInbox() {
  const store = useInboxStore();
  const ws = ref<WebSocket | null>(null);
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectInterval = RECONNECT_INTERVAL_MS;
  let manualClose = false;

  function connect() {
    const url = `${WS_BASE.replace(/^http/, 'ws').replace(/\/api$/, '')}/api/notifications/inbox`;
    ws.value = new WebSocket(url);

    ws.value.onopen = () => {
      reconnectInterval = RECONNECT_INTERVAL_MS;
    };
    ws.value.onmessage = (e) => {
      try {
        const payload = JSON.parse(e.data);
        store.prependFromWs(payload);
      } catch (err) {
        console.warn('inbox WS payload parse failed', err);
      }
    };
    ws.value.onerror = () => { /* 由 onclose 處理重連 */ };
    ws.value.onclose = () => {
      ws.value = null;
      if (!manualClose) scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      reconnectInterval = Math.min(reconnectInterval * 2, MAX_RECONNECT_INTERVAL_MS);
      connect();
    }, reconnectInterval);
  }

  function disconnect() {
    manualClose = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (ws.value) {
      ws.value.close();
      ws.value = null;
    }
  }

  onMounted(() => {
    store.fetchUnreadCount();
    connect();
  });

  onUnmounted(disconnect);

  return { ws, disconnect, reconnect: connect };
}
```

測試重點：mock `WebSocket` global + onmessage 觸發 store.prependFromWs + onclose 後 scheduleReconnect。

```typescript
// src/composables/__tests__/useInbox.spec.ts
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { setActivePinia, createPinia } from 'pinia';
import { defineComponent, h } from 'vue';
import { useInbox } from '../useInbox';
import { useInboxStore } from '@/stores/inbox';

vi.mock('@/api/notifications', () => ({
  listNotifications: vi.fn().mockResolvedValue({ items: [], next_before_id: null }),
  getUnreadCount: vi.fn().mockResolvedValue({ count: 0 }),
  markRead: vi.fn(),
  markAllRead: vi.fn(),
}));

describe('useInbox', () => {
  let ws: any;

  beforeEach(() => {
    setActivePinia(createPinia());
    ws = { close: vi.fn(), onopen: null, onmessage: null, onerror: null, onclose: null };
    (global as any).WebSocket = vi.fn(() => ws);
  });

  it('prepends WS message to store', async () => {
    const TestComp = defineComponent({
      setup() { useInbox(); return () => h('div'); },
    });
    mount(TestComp);
    await new Promise((r) => setTimeout(r, 0));
    const store = useInboxStore();
    ws.onmessage({ data: JSON.stringify({ id: 1, title: 't', read_at: null }) });
    expect(store.items[0].id).toBe(1);
    expect(store.unreadCount).toBe(1);
  });
});
```

- [ ] **Step 3: 跑測試 + Commit**

```bash
npm test -- src/composables/__tests__/useInbox.spec.ts 2>&1 | tail -10
git add src/composables/useInbox.ts src/composables/__tests__/useInbox.spec.ts
git commit -m "feat(notification): add useInbox composable (WS connect + reconnect)"
```

### Task FE4：3 個 component（InboxBell / InboxDrawer / InboxItem）

**Files:**
- Create: `src/components/inbox/InboxBell.vue`
- Create: `src/components/inbox/InboxDrawer.vue`
- Create: `src/components/inbox/InboxItem.vue`
- Test: `src/components/inbox/__tests__/InboxBell.spec.ts`
- Test: `src/components/inbox/__tests__/InboxDrawer.spec.ts`

- [ ] **Step 1: InboxBell（門鈴 icon + 紅點）**

```vue
<script setup lang="ts">
import { computed } from 'vue';
import { Bell } from '@element-plus/icons-vue';
import { useInboxStore } from '@/stores/inbox';

const store = useInboxStore();
const emit = defineEmits<{ click: [] }>();

const displayCount = computed(() => {
  if (store.unreadCount === 0) return '';
  return store.unreadCount > 99 ? '99+' : String(store.unreadCount);
});
</script>

<template>
  <div class="inbox-bell" @click="emit('click')">
    <el-badge :value="displayCount" :hidden="!displayCount" type="danger">
      <el-icon :size="20"><Bell /></el-icon>
    </el-badge>
  </div>
</template>

<style scoped>
.inbox-bell {
  cursor: pointer;
  padding: 8px;
  display: inline-flex;
  align-items: center;
}
</style>
```

測試 `InboxBell.spec.ts`：unreadCount=0 不顯示 badge / unreadCount=42 顯示 "42" / unreadCount=150 顯示 "99+" / click emit 事件

- [ ] **Step 2: InboxItem**

```vue
<script setup lang="ts">
import type { NotificationItem } from '@/api/notifications';

defineProps<{ item: NotificationItem }>();
const emit = defineEmits<{ click: [NotificationItem] }>();
</script>

<template>
  <div class="inbox-item" :class="{ unread: !item.read_at }" @click="emit('click', item)">
    <div class="inbox-item__dot" v-if="!item.read_at" />
    <div class="inbox-item__body">
      <div class="inbox-item__title">{{ item.title }}</div>
      <div class="inbox-item__text">{{ item.body }}</div>
      <div class="inbox-item__time">{{ new Date(item.created_at).toLocaleString('zh-TW') }}</div>
    </div>
  </div>
</template>

<style scoped>
.inbox-item {
  display: flex;
  padding: 12px 16px;
  border-bottom: 1px solid #f0f0f0;
  cursor: pointer;
  transition: background 0.2s;
}
.inbox-item:hover { background: #fafafa; }
.inbox-item.unread { background: #f0f7ff; }
.inbox-item__dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: #f56c6c;
  margin-right: 8px; margin-top: 8px;
}
.inbox-item__title { font-weight: 500; margin-bottom: 4px; }
.inbox-item__text { color: #606266; font-size: 13px; margin-bottom: 4px; }
.inbox-item__time { color: #909399; font-size: 12px; }
</style>
```

- [ ] **Step 3: InboxDrawer**

```vue
<script setup lang="ts">
import { watch } from 'vue';
import { useRouter } from 'vue-router';
import { useInboxStore } from '@/stores/inbox';
import InboxItem from './InboxItem.vue';
import type { NotificationItem } from '@/api/notifications';

const props = defineProps<{ modelValue: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [boolean] }>();

const store = useInboxStore();
const router = useRouter();

watch(() => props.modelValue, async (open) => {
  if (open && store.items.length === 0) {
    await store.fetchPage(true);
  }
});

async function onItemClick(item: NotificationItem) {
  try {
    await store.markRead(item.id);
  } catch {
    // optimistic 已 rollback；UI 仍跳轉
  }
  if (item.deep_link) router.push(item.deep_link);
  emit('update:modelValue', false);
}

async function onScroll(e: Event) {
  const t = e.target as HTMLElement;
  if (
    store.hasMore &&
    !store.loading &&
    t.scrollHeight - t.scrollTop - t.clientHeight < 80
  ) {
    await store.fetchPage(false);
  }
}

async function onMarkAll() {
  try {
    await store.markAllRead();
  } catch (err) {
    console.error('mark_all_read failed', err);
  }
}
</script>

<template>
  <el-drawer
    :model-value="modelValue"
    @update:model-value="emit('update:modelValue', $event)"
    title="通知中心"
    size="360px"
    direction="rtl"
  >
    <template #header="{ titleId }">
      <div class="inbox-drawer__header">
        <span :id="titleId">通知中心</span>
        <el-button v-if="store.unreadCount > 0" size="small" link @click="onMarkAll">
          全部已讀
        </el-button>
      </div>
    </template>
    <div class="inbox-drawer__body" @scroll="onScroll">
      <template v-if="store.items.length > 0">
        <InboxItem
          v-for="item in store.items"
          :key="item.id"
          :item="item"
          @click="onItemClick"
        />
        <div v-if="store.loading" class="inbox-drawer__loading">載入中…</div>
        <div v-if="!store.hasMore && !store.loading" class="inbox-drawer__end">
          沒有更多了
        </div>
      </template>
      <el-empty
        v-else-if="!store.loading"
        description="目前沒有通知"
        :image-size="80"
      />
    </div>
  </el-drawer>
</template>

<style scoped>
.inbox-drawer__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.inbox-drawer__body {
  height: calc(100vh - 60px);
  overflow-y: auto;
}
.inbox-drawer__loading,
.inbox-drawer__end {
  text-align: center;
  color: #909399;
  padding: 16px;
  font-size: 13px;
}
</style>
```

- [ ] **Step 4: 跑測試 + commit**

```bash
npm test -- src/components/inbox/ 2>&1 | tail -10
git add src/components/inbox/ src/components/inbox/__tests__/
git commit -m "feat(notification): add InboxBell + InboxDrawer + InboxItem components"
```

### Task FE5：整合 AdminHeader + App.vue

**Files:**
- Modify: `src/views/AdminHeader.vue`（加 InboxBell + drawer state）
- Modify: `src/App.vue`（call `useInbox()` 建 WS 連線）

- [ ] **Step 1: AdminHeader**

```bash
grep -n "UserMenuDropdown\|<el-header\|class=\"admin-header" src/views/AdminHeader.vue | head -5
```

找到 UserMenuDropdown 引用位置，在它左邊加：

```vue
<script setup lang="ts">
import { ref } from 'vue';
import InboxBell from '@/components/inbox/InboxBell.vue';
import InboxDrawer from '@/components/inbox/InboxDrawer.vue';
// ... existing imports

const inboxOpen = ref(false);
</script>

<template>
  <header class="admin-header">
    <!-- ... existing left content (logo / nav) ... -->
    <div class="admin-header__right">
      <InboxBell @click="inboxOpen = true" />
      <UserMenuDropdown />
    </div>
    <InboxDrawer v-model="inboxOpen" />
  </header>
</template>
```

- [ ] **Step 2: App.vue**

```vue
<script setup lang="ts">
import { useInbox } from '@/composables/useInbox';
// ... existing imports
// 只在登入後 mount 時 useInbox（如果 App.vue 是 root，需要在 admin layout 才掛）
// 若 App.vue 不能判斷 auth state，移到 AdminLayout.vue 或 AdminHeader.vue 內 setup 段
useInbox();
</script>
```

注意：useInbox 立刻 connect WS，未登入會 401 close。可在 AdminLayout（已驗證 admin 才進）內掛比較乾淨。實作時看現有檔案結構決定。

- [ ] **Step 3: build + smoke**

```bash
npm run typecheck && npm run build 2>&1 | tail -10
```

Expected: 0 typecheck error, build success

- [ ] **Step 4: Commit**

```bash
git add src/views/AdminHeader.vue src/App.vue
git commit -m "feat(notification): integrate InboxBell into AdminHeader + useInbox at App mount"
```

### Task FE6：手動驗證 + push + PR

- [ ] **Step 1: dev server 起來手動驗**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
# 開 http://localhost:5173 登入 admin
# 開另一終端 trigger 一個 notification：
cd ~/Desktop/ivy-backend
python -c "
from models.base import get_session_factory
from services.notification import dispatch
s = get_session_factory()()
dispatch.enqueue(
    s,
    event_type='leave.approved',
    recipient_user_id=<your-admin-user-id>,
    context={'reviewer_name': 'test', 'leave_type': '事假', 'start': '2026-06-01', 'end': '2026-06-02', 'leave_id': 1},
)
s.commit()
"
# 看門鈴紅點 +1，drawer 開啟看到該筆
```

- [ ] **Step 2: 全 vitest**

```bash
cd ~/Desktop/ivy-frontend/.claude/worktrees/notification-inbox-phase-3-2026-05-25-frontend
npm test 2>&1 | tail -10
```

Expected: 全綠 + ~10+ 新測試

- [ ] **Step 3: push + PR**

```bash
git push -u origin feat/notification-inbox-phase-3-2026-05-25-frontend
gh pr create --title "feat(notification): Phase 3 FE — 員工通知中心 (門鈴 + drawer + WS realtime)" \
  --body "$(cat <<'EOF'
## Summary
員工通知中心前端：
- InboxBell 門鈴 icon + 紅點未讀數（>99 顯示 99+）
- InboxDrawer 右側抽屜 360px + infinite scroll + 全部已讀按鈕
- InboxItem 單筆 card（未讀 highlight + 紅點）
- useInbox composable WS 連線 + 自動重連
- Pinia store optimistic mark_read + rollback on error
- 整合至 AdminHeader（UserMenuDropdown 左邊）

## Test plan
- [ ] CI 全綠（vitest + typecheck + build）
- [ ] 手動驗：admin 登入後 dispatch.enqueue 一筆 → 門鈴 +1 + drawer 顯示
- [ ] 手動驗：click 單筆 → mark_read + router.push deep_link + drawer 關閉
- [ ] 手動驗：「全部已讀」按鈕清空未讀

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review checklist

- [ ] **BE 4 endpoint 都過 self-isolation 測試**（admin 不能讀別人）
- [ ] **WS endpoint 拒絕無 JWT**
- [ ] **OpenAPI schema.d.ts 跟 schema 同步**（前端 PR 第一個 commit 為 regen schema）
- [ ] **vitest 4 個元件測試 + composable 測試 + store 測試 = 12+ 條全綠**
- [ ] **build pass + typecheck 0 error**
- [ ] **手動端對端驗證一次完整 flow（dispatch → 門鈴 → drawer → mark_read → 跳轉）**

## 預估時程

| Sub-phase | 工作日 | PR 數 |
|----------|-------|-------|
| BE 3a (REST + WS) | 1.5 | 1 |
| FE 3b (UI) | 5 | 1 |
| **合計** | **6.5 工作日** | **2 PR**（依序 merge） |

## 後續

Phase 3 merged 後：
- Phase 4（outbox + GC + Sentry 告警 + 員工 preference UI）列為 backlog
- 通知中心可開放對家長端（schema 已支援，前端另開 Phase 5）

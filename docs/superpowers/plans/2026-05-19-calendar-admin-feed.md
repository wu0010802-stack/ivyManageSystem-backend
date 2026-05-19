# Calendar Admin Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增單一聚合端點 `GET /api/calendar/admin_feed`，回傳六個 layer（event/holiday/leave/activity/appraisal/meeting）的事件，並在管理端 CalendarView 加上 layer toggle 一頁看完跨模組行程。

**Architecture:** 後端新檔 `api/calendar_admin.py` 內各 layer 各一個 `_fetch_*` 函式 + 一個 `LAYER_FETCHERS` dict，主 endpoint 純編排（驗證、權限 gate、收集、排序）。前端新 composable `useCalendarLayers` 管 toggle 狀態+localStorage 持久化，CalendarView 切月才打 API、layer toggle 純本地過濾。

**Tech Stack:** FastAPI / SQLAlchemy / Pydantic v2 / Vue 3 Composition API / Element Plus / Vitest / pytest

**Spec:** [`docs/superpowers/specs/2026-05-19-calendar-admin-feed-design.md`](../specs/2026-05-19-calendar-admin-feed-design.md)

---

## 檔案結構

| 檔案 | 動作 | 用途 |
|---|---|---|
| `ivy-backend/utils/calendar_colors.py` | Create | 6 layer 顏色 + meta label 常數 |
| `ivy-backend/schemas/calendar_admin.py` | Create | `CalendarFeedItem` / `CalendarFeedResponse` Pydantic |
| `ivy-backend/api/calendar_admin.py` | Create | router + 6 個 `_fetch_*` + endpoint |
| `ivy-backend/main.py` | Modify | include_router |
| `ivy-backend/tests/test_calendar_admin.py` | Create | 14+ test case |
| `ivy-frontend/src/api/calendar.ts` | Modify | 加 `getAdminFeed` 函式與型別 |
| `ivy-frontend/src/constants/calendarLayers.ts` | Create | 前端 layer 顏色 + label 常數（與後端對齊） |
| `ivy-frontend/src/composables/useCalendarLayers.ts` | Create | toggle state + localStorage + 過濾 |
| `ivy-frontend/src/composables/__tests__/useCalendarLayers.test.ts` | Create | composable 6 test |
| `ivy-frontend/src/views/CalendarView.vue` | Modify | 加 chip toggle + 非 event layer cell 渲染 |

---

## Phase 1：後端基礎建設

### Task 1: 顏色與 label 常數

**Files:**
- Create: `ivy-backend/utils/calendar_colors.py`
- Test: `ivy-backend/tests/test_calendar_colors.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_calendar_colors.py
import pytest
from utils.calendar_colors import (
    LAYER_COLORS,
    APPRAISAL_MILESTONE_LABELS,
    MEETING_TYPE_LABELS,
    ALL_LAYERS,
)


def test_all_layers_set_has_six():
    assert ALL_LAYERS == {"event", "holiday", "leave", "activity", "appraisal", "meeting"}


def test_every_layer_has_default_color():
    for layer in ALL_LAYERS:
        assert layer in LAYER_COLORS, f"{layer} missing default color"
        assert LAYER_COLORS[layer]["default"].startswith("#")
        assert len(LAYER_COLORS[layer]["default"]) == 7


def test_event_layer_has_acknowledge_variant():
    assert "ack" in LAYER_COLORS["event"]


def test_holiday_layer_has_workday_override_variant():
    assert "workday_override" in LAYER_COLORS["holiday"]


def test_leave_layer_has_pending_variant():
    assert "pending" in LAYER_COLORS["leave"]


def test_appraisal_milestone_labels_three_keys():
    assert set(APPRAISAL_MILESTONE_LABELS) == {"start_date", "end_date", "base_score_calc_date"}


def test_meeting_type_labels_has_staff_meeting():
    assert MEETING_TYPE_LABELS["staff_meeting"] == "園務會議"
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_colors.py -v
```

Expected: ImportError or 7 fail.

- [ ] **Step 3: 寫實作**

```python
# utils/calendar_colors.py
"""管理端行事曆 admin_feed 各 layer 顏色與 label 常數。

設計準則：
- 前端 `ivy-frontend/src/constants/calendarLayers.ts` 必須與本檔同步
- 後端固定下發 color，讓前端可純粹按 item.color 渲染、不需重新對照
"""

from typing import Final

ALL_LAYERS: Final[set[str]] = {
    "event",
    "holiday",
    "leave",
    "activity",
    "appraisal",
    "meeting",
}

# 每 layer 的色票。default 為主色，其他 key 為變體。
LAYER_COLORS: Final[dict[str, dict[str, str]]] = {
    "event": {
        "default": "#10b981",     # 綠：一般事件
        "ack": "#ef4444",         # 紅：需簽閱
    },
    "holiday": {
        "default": "#f59e0b",         # 橘：國定/學校假
        "workday_override": "#6366f1",  # 紫：補班日
    },
    "leave": {
        "default": "#0ea5e9",  # 藍：已核准
        "pending": "#94a3b8",  # 灰：待審
    },
    "activity": {"default": "#ec4899"},   # 粉
    "appraisal": {"default": "#dc2626"},  # 暗紅
    "meeting": {"default": "#8b5cf6"},    # 紫
}

APPRAISAL_MILESTONE_LABELS: Final[dict[str, str]] = {
    "start_date": "開始",
    "end_date": "結束",
    "base_score_calc_date": "基準分結算",
}

MEETING_TYPE_LABELS: Final[dict[str, str]] = {
    "staff_meeting": "園務會議",
}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_colors.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add utils/calendar_colors.py tests/test_calendar_colors.py
git commit -m "feat(calendar): add layer color and label constants for admin_feed

新增 6 layer (event/holiday/leave/activity/appraisal/meeting) 顏色常數，
後端固定下發 color 讓前端純按 item.color 渲染。"
```

---

### Task 2: Pydantic schemas

**Files:**
- Create: `ivy-backend/schemas/calendar_admin.py`
- Test: `ivy-backend/tests/test_calendar_admin_schemas.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_calendar_admin_schemas.py
from datetime import date
import pytest
from pydantic import ValidationError
from schemas.calendar_admin import CalendarFeedItem, CalendarFeedResponse


def test_feed_item_minimal_valid():
    item = CalendarFeedItem(
        layer="event",
        id=42,
        title="家長會",
        start=date(2026, 5, 19),
        end=date(2026, 5, 19),
        all_day=True,
        color="#10b981",
        link="/calendar?eventId=42",
        meta={},
    )
    assert item.layer == "event"
    assert item.link == "/calendar?eventId=42"


def test_feed_item_unknown_layer_rejected():
    with pytest.raises(ValidationError):
        CalendarFeedItem(
            layer="totally_made_up",
            id=1,
            title="x",
            start=date(2026, 5, 19),
            end=date(2026, 5, 19),
            all_day=True,
            color="#000000",
            link=None,
            meta={},
        )


def test_feed_item_id_accepts_string():
    # holiday/appraisal layer 用 composite key
    item = CalendarFeedItem(
        layer="holiday",
        id="2026-05-19",
        title="勞動節",
        start=date(2026, 5, 19),
        end=date(2026, 5, 19),
        all_day=True,
        color="#f59e0b",
        link=None,
        meta={},
    )
    assert item.id == "2026-05-19"


def test_feed_response_alias_from():
    """Pydantic alias `from` 序列化檢查。"""
    resp = CalendarFeedResponse(
        **{"from": date(2026, 5, 1)},
        to=date(2026, 5, 31),
        items=[],
    )
    payload = resp.model_dump(by_alias=True)
    assert "from" in payload
    assert payload["from"] == date(2026, 5, 1)
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin_schemas.py -v
```

Expected: ImportError.

- [ ] **Step 3: 寫實作**

```python
# schemas/calendar_admin.py
"""管理端行事曆 admin_feed Pydantic schemas。"""

from datetime import date
from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

Layer = Literal["event", "holiday", "leave", "activity", "appraisal", "meeting"]


class CalendarFeedItem(BaseModel):
    """單筆行事曆事件，統一 envelope。"""

    layer: Layer
    id: Union[int, str]  # holiday 用 date string；appraisal 用 `{cycle_id}:{milestone}`
    title: str
    start: date
    end: date
    all_day: bool = True
    color: str
    link: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class CalendarFeedResponse(BaseModel):
    """admin_feed 回應主體。"""

    model_config = ConfigDict(populate_by_name=True)

    from_: date = Field(alias="from")
    to: date
    items: list[CalendarFeedItem]
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin_schemas.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add schemas/calendar_admin.py tests/test_calendar_admin_schemas.py
git commit -m "feat(calendar): add Pydantic schemas for admin_feed envelope"
```

---

### Task 3: Endpoint 骨架 + window/參數驗證

**Files:**
- Create: `ivy-backend/api/calendar_admin.py`
- Modify: `ivy-backend/main.py`（include_router）
- Create: `ivy-backend/tests/test_calendar_admin.py`（後續所有 layer 測試共用此檔）

- [ ] **Step 1: 寫失敗測試（驗證骨架 + 邊界）**

```python
# tests/test_calendar_admin.py
"""admin_feed endpoint 整合測試。

每個 layer 的 fetch 細節由本檔負責 end-to-end 驗證，
不另寫 unit test（fetcher 是 endpoint 的私有實作）。
"""

from datetime import date
import pytest
from fastapi.testclient import TestClient

from main import app
from models.db import get_db, SessionLocal
from utils.permissions import Permission


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def admin_token(client):
    """超管 token：擁有所有權限。沿用既有測試 fixture 風格。"""
    # 注：實作時對齊 tests/conftest.py 既有 admin_token fixture 名稱與簽章
    raise NotImplementedError("使用 conftest.py 既有 fixture")


# ----- 邊界 / 參數驗證 -----

def test_window_over_90_days_returns_422(client, admin_token):
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-01-01", "to": "2026-05-01"},  # 121 天
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422
    assert "window" in r.json()["detail"].lower()


def test_to_before_from_returns_422(client, admin_token):
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-31", "to": "2026-05-01"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422


def test_missing_from_returns_422(client, admin_token):
    r = client.get(
        "/api/calendar/admin_feed",
        params={"to": "2026-05-31"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422


def test_unauthenticated_returns_401(client):
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31"},
    )
    assert r.status_code == 401


def test_empty_window_returns_empty_items(client, admin_token, db_session):
    """無任何事件、無假日的 window 回 200 + items=[]"""
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2099-01-01", "to": "2099-01-07"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["from"] == "2099-01-01"
    assert body["to"] == "2099-01-07"
    assert body["items"] == []


def test_unknown_layer_ignored(client, admin_token):
    """`?layers=foo` 不報錯，當作沒指定有效 layer。"""
    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2099-01-01", "to": "2099-01-07", "layers": "foo,bar"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["items"] == []
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v
```

Expected: 6 fail（router 未掛）。

- [ ] **Step 3: 寫骨架實作**

```python
# api/calendar_admin.py
"""管理端行事曆跨模組聚合 endpoint。

設計：
- 各 layer 由 `_fetch_<layer>(session, from_, to, current_user) -> list[CalendarFeedItem]` 提供
- 主 endpoint 純編排：驗證 window → 過濾 layers → 收集 → 排序 → 回傳
- 權限濾在每個 fetcher 入口（無權限直接 return []），避免越權外洩
"""

from datetime import date
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from models.db import get_db
from schemas.calendar_admin import CalendarFeedItem, CalendarFeedResponse
from utils.auth import get_current_user
from utils.calendar_colors import ALL_LAYERS

router = APIRouter(tags=["calendar-admin"])

MAX_WINDOW_DAYS = 90

# 各 layer fetcher 由後續 Task 4-9 填入
LAYER_FETCHERS: dict[str, Callable[[Session, date, date, dict], list[CalendarFeedItem]]] = {}


@router.get("/admin_feed", response_model=CalendarFeedResponse)
def get_admin_feed(
    from_: date = Query(..., alias="from"),
    to: date = Query(...),
    layers: str | None = Query(None),
    session: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> CalendarFeedResponse:
    if to < from_:
        raise HTTPException(status_code=422, detail="to must be >= from")
    if (to - from_).days > MAX_WINDOW_DAYS:
        raise HTTPException(status_code=422, detail=f"window exceeds {MAX_WINDOW_DAYS} days")

    if layers is None:
        requested = ALL_LAYERS
    else:
        requested = {x.strip() for x in layers.split(",") if x.strip()} & ALL_LAYERS

    items: list[CalendarFeedItem] = []
    for layer in requested:
        fetcher = LAYER_FETCHERS.get(layer)
        if fetcher is None:
            continue
        items.extend(fetcher(session, from_, to, current_user))

    items.sort(key=lambda x: (x.start, x.layer, str(x.id)))
    return CalendarFeedResponse(**{"from": from_}, to=to, items=items)
```

- [ ] **Step 4: 在 main.py 註冊 router**

Modify: `ivy-backend/main.py` — 找到既有 `app.include_router(...)` 區塊（搜 `include_router` 找其他 router 註冊位置），加：

```python
from api.calendar_admin import router as calendar_admin_router
app.include_router(calendar_admin_router, prefix="/api/calendar")
```

- [ ] **Step 5: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v -k "window or before or missing or unauthenticated or empty or unknown_layer"
```

Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/calendar_admin.py main.py tests/test_calendar_admin.py
git commit -m "feat(calendar): add admin_feed endpoint skeleton with window validation

骨架包含 422 邊界檢查（window > 90 / to < from）、層級過濾、空回傳。
各 layer fetcher 由後續 commit 加入。"
```

---

## Phase 2：六個 layer fetcher（TDD 每 layer 一 commit）

> **共通約定**：每個 fetcher 加入後，在 `LAYER_FETCHERS` dict 註冊；test 用既有 conftest fixture（`db_session`、`admin_token`）造資料、打 endpoint 驗回傳。

### Task 4: `event` layer

**Files:**
- Modify: `ivy-backend/api/calendar_admin.py`
- Modify: `ivy-backend/tests/test_calendar_admin.py`

- [ ] **Step 1: 加 event layer 測試**

```python
# 接續 tests/test_calendar_admin.py
from models.event import SchoolEvent


def test_event_layer_basic(client, admin_token, db_session):
    db_session.add(SchoolEvent(
        title="家長會", event_date=date(2026, 5, 20),
        event_type="meeting", is_active=True, requires_acknowledgment=False,
    ))
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["layer"] == "event"
    assert items[0]["title"] == "家長會"
    assert items[0]["start"] == "2026-05-20"
    assert items[0]["end"] == "2026-05-20"
    assert items[0]["color"] == "#10b981"
    assert items[0]["link"] == f"/calendar?eventId={items[0]['id']}"


def test_event_multi_day_uses_end_date(client, admin_token, db_session):
    db_session.add(SchoolEvent(
        title="校外教學", event_date=date(2026, 5, 20), end_date=date(2026, 5, 22),
        event_type="activity", is_active=True,
    ))
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    items = r.json()["items"]
    assert items[0]["start"] == "2026-05-20"
    assert items[0]["end"] == "2026-05-22"


def test_event_requires_ack_uses_ack_color(client, admin_token, db_session):
    db_session.add(SchoolEvent(
        title="家長簽閱通知", event_date=date(2026, 5, 20),
        is_active=True, requires_acknowledgment=True,
    ))
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.json()["items"][0]["color"] == "#ef4444"


def test_event_inactive_excluded(client, admin_token, db_session):
    db_session.add(SchoolEvent(
        title="已停用", event_date=date(2026, 5, 20), is_active=False,
    ))
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "event"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.json()["items"] == []
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v -k "event_layer or event_multi or event_requires or event_inactive"
```

Expected: 4 fail（無 fetcher）。

- [ ] **Step 3: 寫 fetcher**

加入 `api/calendar_admin.py`（在 `router` 宣告下方、`@router.get` 上方）：

```python
from sqlalchemy import select
from models.event import SchoolEvent
from utils.calendar_colors import LAYER_COLORS
from utils.permissions import Permission, has_permission


def _fetch_event(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permissions", 0), Permission.CALENDAR):
        return []
    stmt = (
        select(
            SchoolEvent.id,
            SchoolEvent.title,
            SchoolEvent.event_date,
            SchoolEvent.end_date,
            SchoolEvent.requires_acknowledgment,
            SchoolEvent.event_type,
        )
        .where(SchoolEvent.is_active.is_(True))
        .where(SchoolEvent.event_date <= to)
        .where((SchoolEvent.end_date.is_(None) & (SchoolEvent.event_date >= from_))
               | (SchoolEvent.end_date.is_not(None) & (SchoolEvent.end_date >= from_)))
    )
    rows = session.execute(stmt).all()
    out: list[CalendarFeedItem] = []
    for r in rows:
        color = LAYER_COLORS["event"]["ack"] if r.requires_acknowledgment else LAYER_COLORS["event"]["default"]
        out.append(CalendarFeedItem(
            layer="event",
            id=r.id,
            title=r.title,
            start=r.event_date,
            end=r.end_date or r.event_date,
            all_day=True,
            color=color,
            link=f"/calendar?eventId={r.id}",
            meta={"event_type": r.event_type, "requires_acknowledgment": r.requires_acknowledgment},
        ))
    return out


LAYER_FETCHERS["event"] = _fetch_event
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v -k "event"
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/calendar_admin.py tests/test_calendar_admin.py
git commit -m "feat(calendar): admin_feed event layer (SchoolEvent)"
```

---

### Task 5: `holiday` layer

**Files:**
- Modify: `ivy-backend/api/calendar_admin.py`
- Modify: `ivy-backend/tests/test_calendar_admin.py`

- [ ] **Step 1: 加 holiday layer 測試**

```python
# 接續 tests/test_calendar_admin.py
from models.event import Holiday, WorkdayOverride


def test_holiday_layer_basic(client, admin_token, db_session):
    db_session.add_all([
        Holiday(date=date(2026, 5, 1), name="勞動節"),
        WorkdayOverride(date=date(2026, 5, 16), name="補上 5/1"),
    ])
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "holiday"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    items = r.json()["items"]
    by_date = {it["start"]: it for it in items}

    assert by_date["2026-05-01"]["title"] == "勞動節"
    assert by_date["2026-05-01"]["color"] == "#f59e0b"
    assert by_date["2026-05-01"]["id"] == "holiday:2026-05-01"
    assert by_date["2026-05-01"]["link"] is None

    assert by_date["2026-05-16"]["title"] == "補上 5/1"
    assert by_date["2026-05-16"]["color"] == "#6366f1"
    assert by_date["2026-05-16"]["id"] == "workday_override:2026-05-16"
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py::test_holiday_layer_basic -v
```

Expected: fail。

- [ ] **Step 3: 寫 fetcher**

加入 `api/calendar_admin.py`：

```python
def _fetch_holiday(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permissions", 0), Permission.CALENDAR):
        return []

    holiday_rows = session.execute(
        select(Holiday.date, Holiday.name)
        .where(Holiday.date.between(from_, to))
    ).all()
    override_rows = session.execute(
        select(WorkdayOverride.date, WorkdayOverride.name)
        .where(WorkdayOverride.date.between(from_, to))
    ).all()

    out: list[CalendarFeedItem] = []
    for r in holiday_rows:
        out.append(CalendarFeedItem(
            layer="holiday",
            id=f"holiday:{r.date.isoformat()}",
            title=r.name,
            start=r.date,
            end=r.date,
            color=LAYER_COLORS["holiday"]["default"],
            link=None,
            meta={"kind": "holiday"},
        ))
    for r in override_rows:
        out.append(CalendarFeedItem(
            layer="holiday",
            id=f"workday_override:{r.date.isoformat()}",
            title=r.name,
            start=r.date,
            end=r.date,
            color=LAYER_COLORS["holiday"]["workday_override"],
            link=None,
            meta={"kind": "workday_override"},
        ))
    return out


LAYER_FETCHERS["holiday"] = _fetch_holiday
```

並把 `Holiday, WorkdayOverride` 加入 model import。

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py::test_holiday_layer_basic -v
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/calendar_admin.py tests/test_calendar_admin.py
git commit -m "feat(calendar): admin_feed holiday layer (Holiday + WorkdayOverride)"
```

---

### Task 6: `leave` layer

**Files:**
- Modify: `ivy-backend/api/calendar_admin.py`
- Modify: `ivy-backend/tests/test_calendar_admin.py`

- [ ] **Step 1: 加 leave layer 測試**

```python
# 接續 tests/test_calendar_admin.py
from models.leaves import LeaveRecord
from models.employee import Employee  # 注：實際路徑請參考既有測試 import


def _make_employee(db_session, name="王老師"):
    """造員工（沿用既有測試 helper 命名慣例，若 conftest 已有 employee fixture 改用之）"""
    emp = Employee(name=name, ...)  # 補齊既有測試 Employee 必填欄位
    db_session.add(emp)
    db_session.flush()
    return emp


def test_leave_layer_approved_and_pending(client, admin_token, db_session):
    emp = _make_employee(db_session)
    db_session.add_all([
        LeaveRecord(
            employee_id=emp.id, leave_type="sick",
            start_date=date(2026, 5, 10), end_date=date(2026, 5, 11),
            is_approved=True,
        ),
        LeaveRecord(
            employee_id=emp.id, leave_type="annual",
            start_date=date(2026, 5, 15), end_date=date(2026, 5, 15),
            is_approved=None,  # pending
        ),
        LeaveRecord(
            employee_id=emp.id, leave_type="personal",
            start_date=date(2026, 5, 20), end_date=date(2026, 5, 20),
            is_approved=False,  # rejected — 應被過濾
        ),
    ])
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "leave"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    items = r.json()["items"]
    assert len(items) == 2

    colors = sorted({it["color"] for it in items})
    assert colors == ["#0ea5e9", "#94a3b8"]  # approved blue + pending gray

    titles = {it["title"] for it in items}
    assert any("王老師" in t and "sick" in t for t in titles)


def test_leave_without_permission_excluded(client, db_session, token_without_leaves_read):
    """無 LEAVES_READ 的 caller 不應看到 leave layer。"""
    emp = _make_employee(db_session)
    db_session.add(LeaveRecord(
        employee_id=emp.id, leave_type="sick",
        start_date=date(2026, 5, 10), end_date=date(2026, 5, 10), is_approved=True,
    ))
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "leave"},
        headers={"Authorization": f"Bearer {token_without_leaves_read}"},
    )
    assert r.json()["items"] == []
```

> **Note**: 若 `tests/conftest.py` 無 `token_without_leaves_read` fixture，於本檔 module 級 fixture 中建：登入一個只有 `CALENDAR` bit 的測試帳號回傳其 token。對齊既有 `admin_token` 命名與簽章。

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v -k "leave"
```

Expected: fail。

- [ ] **Step 3: 寫 fetcher**

加入 `api/calendar_admin.py`：

```python
from sqlalchemy import or_
from models.leaves import LeaveRecord
from models.employee import Employee  # 補齊實際 import 路徑


def _fetch_leave(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permissions", 0), Permission.LEAVES_READ):
        return []
    stmt = (
        select(
            LeaveRecord.id,
            LeaveRecord.start_date,
            LeaveRecord.end_date,
            LeaveRecord.leave_type,
            LeaveRecord.is_approved,
            Employee.name.label("employee_name"),
        )
        .join(Employee, Employee.id == LeaveRecord.employee_id)
        .where(LeaveRecord.start_date <= to)
        .where(LeaveRecord.end_date >= from_)
        .where(or_(LeaveRecord.is_approved.is_(None), LeaveRecord.is_approved.is_(True)))
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        is_pending = r.is_approved is None
        color = LAYER_COLORS["leave"]["pending" if is_pending else "default"]
        out.append(CalendarFeedItem(
            layer="leave",
            id=r.id,
            title=f"{r.employee_name} {r.leave_type}",
            start=r.start_date,
            end=r.end_date,
            color=color,
            link=f"/leaves?id={r.id}",
            meta={"status": "pending" if is_pending else "approved", "leave_type": r.leave_type},
        ))
    return out


LAYER_FETCHERS["leave"] = _fetch_leave
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v -k "leave"
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/calendar_admin.py tests/test_calendar_admin.py
git commit -m "feat(calendar): admin_feed leave layer with LEAVES_READ gate

待審/核准不同色（藍/灰），rejected 不下發；
無 LEAVES_READ 權限直接 return [] 不洩漏。"
```

---

### Task 7: `activity` layer

**Files:**
- Modify: `ivy-backend/api/calendar_admin.py`
- Modify: `ivy-backend/tests/test_calendar_admin.py`

- [ ] **Step 1: 加 activity layer 測試**

```python
# 接續 tests/test_calendar_admin.py
from models.activity import ActivityCourse, ActivitySession  # 路徑請對齊實際


def test_activity_layer_joins_course_name(client, admin_token, db_session):
    course = ActivityCourse(name="陶藝班", ...)  # 補齊既有 Course 必填欄位
    db_session.add(course)
    db_session.flush()
    db_session.add_all([
        ActivitySession(course_id=course.id, session_date=date(2026, 5, 10), session_no=1, ...),
        ActivitySession(course_id=course.id, session_date=date(2026, 5, 17), session_no=2, ...),
    ])
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "activity"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    items = r.json()["items"]
    assert len(items) == 2
    for it in items:
        assert it["color"] == "#ec4899"
        assert it["title"].startswith("陶藝班")
        assert it["link"] == f"/activity?courseId={course.id}"
        assert "第" in it["title"] and "堂" in it["title"]
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py::test_activity_layer_joins_course_name -v
```

Expected: fail。

- [ ] **Step 3: 寫 fetcher**

```python
from models.activity import ActivityCourse, ActivitySession  # 補齊實際 import


def _fetch_activity(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permissions", 0), Permission.ACTIVITY_READ):
        return []
    stmt = (
        select(
            ActivitySession.id,
            ActivitySession.session_date,
            ActivitySession.session_no,
            ActivitySession.course_id,
            ActivityCourse.name.label("course_name"),
        )
        .join(ActivityCourse, ActivityCourse.id == ActivitySession.course_id)
        .where(ActivitySession.session_date.between(from_, to))
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        out.append(CalendarFeedItem(
            layer="activity",
            id=r.id,
            title=f"{r.course_name} 第{r.session_no}堂",
            start=r.session_date,
            end=r.session_date,
            color=LAYER_COLORS["activity"]["default"],
            link=f"/activity?courseId={r.course_id}",
            meta={"course_id": r.course_id, "session_no": r.session_no},
        ))
    return out


LAYER_FETCHERS["activity"] = _fetch_activity
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py::test_activity_layer_joins_course_name -v
```

Expected: pass。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/calendar_admin.py tests/test_calendar_admin.py
git commit -m "feat(calendar): admin_feed activity layer (ActivitySession join Course)"
```

---

### Task 8: `appraisal` layer（三里程碑 / cycle）

**Files:**
- Modify: `ivy-backend/api/calendar_admin.py`
- Modify: `ivy-backend/tests/test_calendar_admin.py`

- [ ] **Step 1: 加 appraisal layer 測試**

```python
# 接續 tests/test_calendar_admin.py
from models.appraisal import AppraisalCycle, Semester  # 路徑請對齊


def test_appraisal_three_milestones_per_cycle(client, admin_token, db_session):
    cycle = AppraisalCycle(
        academic_year=114, semester=Semester.FIRST,
        start_date=date(2026, 5, 5),
        end_date=date(2026, 5, 25),
        base_score_calc_date=date(2026, 5, 15),
        base_score=0,
    )
    db_session.add(cycle)
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "appraisal"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    items = r.json()["items"]
    assert len(items) == 3
    starts = sorted(it["start"] for it in items)
    assert starts == ["2026-05-05", "2026-05-15", "2026-05-25"]

    by_milestone = {it["meta"]["milestone"]: it for it in items}
    assert by_milestone["start_date"]["title"].endswith("開始")
    assert by_milestone["end_date"]["title"].endswith("結束")
    assert by_milestone["base_score_calc_date"]["title"].endswith("基準分結算")

    for it in items:
        assert it["color"] == "#dc2626"
        assert it["link"] == f"/appraisal?cycleId={cycle.id}"


def test_appraisal_milestone_outside_window_excluded(client, admin_token, db_session):
    """cycle 的 end_date 在 window 外、start_date 在 window 內 → 只下發 start。"""
    cycle = AppraisalCycle(
        academic_year=114, semester=Semester.FIRST,
        start_date=date(2026, 5, 10),
        end_date=date(2026, 8, 30),
        base_score_calc_date=date(2026, 6, 30),
        base_score=0,
    )
    db_session.add(cycle)
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "appraisal"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["meta"]["milestone"] == "start_date"
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v -k "appraisal"
```

Expected: fail。

- [ ] **Step 3: 寫 fetcher**

```python
from models.appraisal import AppraisalCycle
from utils.calendar_colors import APPRAISAL_MILESTONE_LABELS


def _fetch_appraisal(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permissions", 0), Permission.APPRAISAL_READ):
        return []
    # 三日期任一落 window 都要拉 cycle
    stmt = select(
        AppraisalCycle.id,
        AppraisalCycle.academic_year,
        AppraisalCycle.semester,
        AppraisalCycle.start_date,
        AppraisalCycle.end_date,
        AppraisalCycle.base_score_calc_date,
    ).where(
        AppraisalCycle.start_date.between(from_, to)
        | AppraisalCycle.end_date.between(from_, to)
        | AppraisalCycle.base_score_calc_date.between(from_, to)
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        cycle_title = f"{r.academic_year} 學年度 第 {r.semester.value} 學期"
        for milestone in ("start_date", "end_date", "base_score_calc_date"):
            d = getattr(r, milestone)
            if not (from_ <= d <= to):
                continue
            label = APPRAISAL_MILESTONE_LABELS[milestone]
            out.append(CalendarFeedItem(
                layer="appraisal",
                id=f"{r.id}:{milestone}",
                title=f"{cycle_title} {label}",
                start=d,
                end=d,
                color=LAYER_COLORS["appraisal"]["default"],
                link=f"/appraisal?cycleId={r.id}",
                meta={"cycle_id": r.id, "milestone": milestone},
            ))
    return out


LAYER_FETCHERS["appraisal"] = _fetch_appraisal
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v -k "appraisal"
```

Expected: 2 passed。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/calendar_admin.py tests/test_calendar_admin.py
git commit -m "feat(calendar): admin_feed appraisal layer (3 milestones per cycle)

每 cycle 拆 start_date/end_date/base_score_calc_date 三筆，
僅落 window 內者下發；id 用 {cycle_id}:{milestone} 區分。"
```

---

### Task 9: `meeting` layer（DISTINCT by date+type）

**Files:**
- Modify: `ivy-backend/api/calendar_admin.py`
- Modify: `ivy-backend/tests/test_calendar_admin.py`

- [ ] **Step 1: 加 meeting layer 測試**

```python
# 接續 tests/test_calendar_admin.py
from models.event import MeetingRecord


def test_meeting_layer_dedupes_by_date_and_type(client, admin_token, db_session):
    emp1 = _make_employee(db_session, name="A 老師")
    emp2 = _make_employee(db_session, name="B 老師")
    # 同一場園務會議：兩員工各自一 row
    db_session.add_all([
        MeetingRecord(employee_id=emp1.id, meeting_date=date(2026, 5, 14),
                      meeting_type="staff_meeting", attended=True),
        MeetingRecord(employee_id=emp2.id, meeting_date=date(2026, 5, 14),
                      meeting_type="staff_meeting", attended=True),
        # 另一天另一場
        MeetingRecord(employee_id=emp1.id, meeting_date=date(2026, 5, 21),
                      meeting_type="staff_meeting", attended=False),
    ])
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "meeting"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    items = r.json()["items"]
    assert len(items) == 2  # DISTINCT (date, type)
    titles = {it["title"] for it in items}
    assert titles == {"園務會議"}  # meeting_type label
    starts = sorted(it["start"] for it in items)
    assert starts == ["2026-05-14", "2026-05-21"]
    for it in items:
        assert it["color"] == "#8b5cf6"
        assert it["id"].startswith("staff_meeting:")
        assert it["link"] == f"/meetings?date={it['start']}"
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py::test_meeting_layer_dedupes_by_date_and_type -v
```

Expected: fail。

- [ ] **Step 3: 寫 fetcher**

```python
from sqlalchemy import distinct
from models.event import MeetingRecord
from utils.calendar_colors import MEETING_TYPE_LABELS


def _fetch_meeting(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    if not has_permission(current_user.get("permissions", 0), Permission.MEETINGS):
        return []
    stmt = (
        select(MeetingRecord.meeting_date, MeetingRecord.meeting_type)
        .where(MeetingRecord.meeting_date.between(from_, to))
        .distinct()
    )
    out: list[CalendarFeedItem] = []
    for r in session.execute(stmt).all():
        label = MEETING_TYPE_LABELS.get(r.meeting_type, r.meeting_type)
        out.append(CalendarFeedItem(
            layer="meeting",
            id=f"{r.meeting_type}:{r.meeting_date.isoformat()}",
            title=label,
            start=r.meeting_date,
            end=r.meeting_date,
            color=LAYER_COLORS["meeting"]["default"],
            link=f"/meetings?date={r.meeting_date.isoformat()}",
            meta={"meeting_type": r.meeting_type},
        ))
    return out


LAYER_FETCHERS["meeting"] = _fetch_meeting
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py::test_meeting_layer_dedupes_by_date_and_type -v
```

Expected: pass。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/calendar_admin.py tests/test_calendar_admin.py
git commit -m "feat(calendar): admin_feed meeting layer (DISTINCT by date+type)"
```

---

### Task 10: 排序 + 跨權限矩陣 + N+1 預算

**Files:**
- Modify: `ivy-backend/tests/test_calendar_admin.py`

- [ ] **Step 1: 加綜合測試**

```python
# 接續 tests/test_calendar_admin.py
from sqlalchemy import event as sa_event


def test_items_sorted_by_start_then_layer(client, admin_token, db_session):
    """混插多 layer 同 window，items 應穩定排序：start asc → layer asc → id asc。"""
    db_session.add_all([
        SchoolEvent(title="家長會", event_date=date(2026, 5, 20), is_active=True),
        Holiday(date=date(2026, 5, 1), name="勞動節"),
    ])
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    items = r.json()["items"]
    assert items[0]["start"] == "2026-05-01"  # holiday 先
    assert items[1]["start"] == "2026-05-20"  # event 後


def test_employee_with_only_calendar_bit_sees_event_and_holiday_only(
    client, db_session, token_with_only_calendar
):
    """只持 CALENDAR bit 的 caller 只看到 event + holiday，其餘 layer return []。"""
    # 各 layer 各造一筆
    emp = _make_employee(db_session)
    db_session.add_all([
        SchoolEvent(title="家長會", event_date=date(2026, 5, 20), is_active=True),
        Holiday(date=date(2026, 5, 1), name="勞動節"),
        LeaveRecord(employee_id=emp.id, leave_type="sick",
                    start_date=date(2026, 5, 10), end_date=date(2026, 5, 10), is_approved=True),
    ])
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31"},
        headers={"Authorization": f"Bearer {token_with_only_calendar}"},
    )
    layers = {it["layer"] for it in r.json()["items"]}
    assert layers == {"event", "holiday"}


def test_n_plus_1_query_count_under_threshold(client, admin_token, db_session):
    """6 layer × 平均 2 query ≤ 12（含 endpoint 自身 1-2 query 為 auth）。"""
    # 各 layer 各造一筆讓 fetcher 真的執行 join
    emp = _make_employee(db_session)
    course = ActivityCourse(name="陶藝", ...)  # 補齊必填
    db_session.add(course); db_session.flush()
    cycle = AppraisalCycle(
        academic_year=114, semester=Semester.FIRST,
        start_date=date(2026, 5, 5), end_date=date(2026, 5, 25),
        base_score_calc_date=date(2026, 5, 15), base_score=0,
    )
    db_session.add_all([
        SchoolEvent(title="x", event_date=date(2026, 5, 20), is_active=True),
        Holiday(date=date(2026, 5, 1), name="x"),
        LeaveRecord(employee_id=emp.id, leave_type="sick",
                    start_date=date(2026, 5, 10), end_date=date(2026, 5, 10), is_approved=True),
        ActivitySession(course_id=course.id, session_date=date(2026, 5, 10), session_no=1, ...),
        cycle,
        MeetingRecord(employee_id=emp.id, meeting_date=date(2026, 5, 14),
                      meeting_type="staff_meeting", attended=True),
    ])
    db_session.commit()

    # 計 SELECT count
    queries: list[str] = []

    def _capture(conn, cursor, statement, *args, **kwargs):
        if statement.strip().upper().startswith("SELECT"):
            queries.append(statement)

    from sqlalchemy import event as sa_event
    from models.db import engine
    sa_event.listen(engine, "before_cursor_execute", _capture)
    try:
        r = client.get(
            "/api/calendar/admin_feed",
            params={"from": "2026-05-01", "to": "2026-05-31"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    finally:
        sa_event.remove(engine, "before_cursor_execute", _capture)

    assert r.status_code == 200
    # 6 layer × max 2 query = 12，加 auth/session 至多 4，整體 ≤ 16
    assert len(queries) <= 16, f"got {len(queries)} queries: {queries}"


def test_meta_does_not_leak_pii(client, admin_token, db_session):
    """meta 欄位不可含 reason / salary / phone / id_number 等敏感 key。"""
    emp = _make_employee(db_session)
    db_session.add(LeaveRecord(
        employee_id=emp.id, leave_type="sick",
        start_date=date(2026, 5, 10), end_date=date(2026, 5, 10),
        is_approved=True, reason="家裡有事",  # 即便來源有 reason，meta 不應下發
    ))
    db_session.commit()

    r = client.get(
        "/api/calendar/admin_feed",
        params={"from": "2026-05-01", "to": "2026-05-31", "layers": "leave"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    forbidden = {"reason", "salary", "phone", "id_number", "address", "email"}
    for it in r.json()["items"]:
        leaked = set(it["meta"].keys()) & forbidden
        assert not leaked, f"meta leaked PII keys: {leaked}"
```

- [ ] **Step 2: 跑全部測試**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py -v
```

Expected: 全部 passed（共 ~15 case）。

- [ ] **Step 3: Commit**

```bash
cd ~/Desktop/ivy-backend
git add tests/test_calendar_admin.py
git commit -m "test(calendar): add sort, cross-permission, and N+1 budget tests"
```

---

## Phase 3：前端

### Task 11: API wrapper

**Files:**
- Modify: `ivy-frontend/src/api/calendar.ts`

- [ ] **Step 1: 確認既有檔案**

```bash
cd ~/Desktop/ivy-frontend && head -50 src/api/calendar.ts 2>/dev/null || head -50 src/api/events.ts
```

> 若 `calendar.ts` 不存在則新增；若存在則在尾端附加。本步以「附加到既有 events.ts」為主，因 admin_feed 概念屬於行事曆視角，新檔保持 separation：實作時建立 `src/api/calendar.ts`（新檔）。

- [ ] **Step 2: 寫實作**

Create or modify `ivy-frontend/src/api/calendar.ts`:

```ts
import api from './index'
import type { AxiosResp } from './_generated/typed'

export type CalendarLayer =
  | 'event'
  | 'holiday'
  | 'leave'
  | 'activity'
  | 'appraisal'
  | 'meeting'

export interface CalendarFeedItem {
  layer: CalendarLayer
  id: number | string
  title: string
  start: string  // YYYY-MM-DD
  end: string
  all_day: boolean
  color: string
  link: string | null
  meta: Record<string, unknown>
}

export interface CalendarFeedResponse {
  from: string
  to: string
  items: CalendarFeedItem[]
}

/**
 * 取得管理端跨模組行事曆。
 * @param from ISO date `YYYY-MM-DD`
 * @param to   ISO date `YYYY-MM-DD`；(to - from) ≤ 90 天，超過後端回 422
 * @param layers 留空 = 全部
 */
export function getAdminFeed(
  from: string,
  to: string,
  layers?: CalendarLayer[],
): AxiosResp<CalendarFeedResponse> {
  const params: Record<string, string> = { from, to }
  if (layers && layers.length > 0) {
    params.layers = layers.join(',')
  }
  return api.get('/calendar/admin_feed', { params })
}
```

- [ ] **Step 3: typecheck**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck 2>&1 | tail -20
```

Expected: 0 error in `src/api/calendar.ts`.

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/api/calendar.ts
git commit -m "feat(calendar): add getAdminFeed API wrapper"
```

---

### Task 12: 前端 layer 常數（與後端對齊）

**Files:**
- Create: `ivy-frontend/src/constants/calendarLayers.ts`
- Create: `ivy-frontend/src/constants/__tests__/calendarLayers.test.ts`

- [ ] **Step 1: 寫失敗測試**

```ts
// src/constants/__tests__/calendarLayers.test.ts
import { describe, it, expect } from 'vitest'
import {
  CALENDAR_LAYERS,
  LAYER_LABELS,
  LAYER_COLORS,
} from '../calendarLayers'

describe('calendar layers constants', () => {
  it('exposes exactly 6 layers in fixed order', () => {
    expect(CALENDAR_LAYERS).toEqual([
      'event',
      'holiday',
      'leave',
      'activity',
      'appraisal',
      'meeting',
    ])
  })

  it('every layer has a Chinese label', () => {
    for (const layer of CALENDAR_LAYERS) {
      expect(LAYER_LABELS[layer]).toMatch(/[一-龥]/)
    }
  })

  it('every layer has a fallback color matching backend default', () => {
    expect(LAYER_COLORS.event).toBe('#10b981')
    expect(LAYER_COLORS.holiday).toBe('#f59e0b')
    expect(LAYER_COLORS.leave).toBe('#0ea5e9')
    expect(LAYER_COLORS.activity).toBe('#ec4899')
    expect(LAYER_COLORS.appraisal).toBe('#dc2626')
    expect(LAYER_COLORS.meeting).toBe('#8b5cf6')
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/constants/__tests__/calendarLayers.test.ts
```

Expected: import error。

- [ ] **Step 3: 寫實作**

```ts
// src/constants/calendarLayers.ts
/**
 * 管理端 CalendarView layer toggle 常數。
 *
 * 與後端 `ivy-backend/utils/calendar_colors.py` 同步：
 * - 後端 item.color 為主要顯示色（覆蓋以下 fallback）
 * - 本檔 LAYER_COLORS 僅供 UI 元件（chip 圖示等）在後端 item 尚未抵達時的 fallback
 */
import type { CalendarLayer } from '@/api/calendar'

export const CALENDAR_LAYERS: readonly CalendarLayer[] = [
  'event',
  'holiday',
  'leave',
  'activity',
  'appraisal',
  'meeting',
] as const

export const LAYER_LABELS: Record<CalendarLayer, string> = {
  event: '行事曆',
  holiday: '假日',
  leave: '請假',
  activity: '才藝課',
  appraisal: '考核',
  meeting: '會議',
}

export const LAYER_COLORS: Record<CalendarLayer, string> = {
  event: '#10b981',
  holiday: '#f59e0b',
  leave: '#0ea5e9',
  activity: '#ec4899',
  appraisal: '#dc2626',
  meeting: '#8b5cf6',
}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/constants/__tests__/calendarLayers.test.ts
```

Expected: 3 passed。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/constants/calendarLayers.ts src/constants/__tests__/calendarLayers.test.ts
git commit -m "feat(calendar): add frontend layer label/color constants

與後端 utils/calendar_colors.py 對齊；後端 item.color 為主，
本檔 fallback 用於 chip 圖示。"
```

---

### Task 13: `useCalendarLayers` composable

**Files:**
- Create: `ivy-frontend/src/composables/useCalendarLayers.ts`
- Create: `ivy-frontend/src/composables/__tests__/useCalendarLayers.test.ts`

- [ ] **Step 1: 寫失敗測試**

```ts
// src/composables/__tests__/useCalendarLayers.test.ts
import { describe, it, expect, beforeEach } from 'vitest'
import { useCalendarLayers } from '../useCalendarLayers'
import type { CalendarFeedItem } from '@/api/calendar'

const STORAGE_KEY = 'calendar.enabledLayers'

function makeItem(layer: CalendarFeedItem['layer'], start: string): CalendarFeedItem {
  return {
    layer, id: `${layer}-${start}`, title: 't', start, end: start,
    all_day: true, color: '#000', link: null, meta: {},
  }
}

describe('useCalendarLayers', () => {
  beforeEach(() => {
    localStorage.removeItem(STORAGE_KEY)
  })

  it('defaults to all 6 layers enabled', () => {
    const { enabledLayers } = useCalendarLayers()
    expect(enabledLayers.value.size).toBe(6)
  })

  it('persists toggle to localStorage', () => {
    const { toggle } = useCalendarLayers()
    toggle('leave')
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]')
    expect(saved).not.toContain('leave')
    expect(saved.length).toBe(5)
  })

  it('reads from localStorage on init', () => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(['event', 'holiday']))
    const { enabledLayers } = useCalendarLayers()
    expect(enabledLayers.value).toEqual(new Set(['event', 'holiday']))
  })

  it('filteredItems hides items whose layer is disabled', () => {
    const { setItems, toggle, filteredItems } = useCalendarLayers()
    setItems([makeItem('event', '2026-05-01'), makeItem('leave', '2026-05-02')])
    toggle('leave')
    expect(filteredItems.value).toHaveLength(1)
    expect(filteredItems.value[0].layer).toBe('event')
  })

  it('groupByDate buckets items by start date', () => {
    const { setItems, groupByDate } = useCalendarLayers()
    setItems([
      makeItem('event', '2026-05-01'),
      makeItem('leave', '2026-05-01'),
      makeItem('event', '2026-05-02'),
    ])
    expect(groupByDate.value['2026-05-01']).toHaveLength(2)
    expect(groupByDate.value['2026-05-02']).toHaveLength(1)
  })

  it('enableAll / disableAll work', () => {
    const { enabledLayers, disableAll, enableAll } = useCalendarLayers()
    disableAll()
    expect(enabledLayers.value.size).toBe(0)
    enableAll()
    expect(enabledLayers.value.size).toBe(6)
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/composables/__tests__/useCalendarLayers.test.ts
```

Expected: import error。

- [ ] **Step 3: 寫實作**

```ts
// src/composables/useCalendarLayers.ts
import { ref, computed, watch, type Ref, type ComputedRef } from 'vue'
import { CALENDAR_LAYERS } from '@/constants/calendarLayers'
import type { CalendarLayer, CalendarFeedItem } from '@/api/calendar'

const STORAGE_KEY = 'calendar.enabledLayers'

function loadEnabled(): Set<CalendarLayer> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return new Set(CALENDAR_LAYERS)
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return new Set(CALENDAR_LAYERS)
    const valid = parsed.filter((x): x is CalendarLayer =>
      typeof x === 'string' && (CALENDAR_LAYERS as readonly string[]).includes(x),
    )
    return new Set(valid)
  } catch {
    return new Set(CALENDAR_LAYERS)
  }
}

export interface UseCalendarLayersReturn {
  enabledLayers: Ref<Set<CalendarLayer>>
  items: Ref<CalendarFeedItem[]>
  filteredItems: ComputedRef<CalendarFeedItem[]>
  groupByDate: ComputedRef<Record<string, CalendarFeedItem[]>>
  toggle: (layer: CalendarLayer) => void
  enableAll: () => void
  disableAll: () => void
  setItems: (xs: CalendarFeedItem[]) => void
}

export function useCalendarLayers(): UseCalendarLayersReturn {
  const enabledLayers = ref<Set<CalendarLayer>>(loadEnabled())
  const items = ref<CalendarFeedItem[]>([])

  watch(
    enabledLayers,
    (s) => {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify([...s]))
      } catch {
        /* localStorage 滿了不影響邏輯 */
      }
    },
    { deep: true },
  )

  const filteredItems = computed(() =>
    items.value.filter((it) => enabledLayers.value.has(it.layer)),
  )

  const groupByDate = computed(() => {
    const map: Record<string, CalendarFeedItem[]> = {}
    for (const it of filteredItems.value) {
      ;(map[it.start] ??= []).push(it)
    }
    return map
  })

  function toggle(layer: CalendarLayer) {
    const next = new Set(enabledLayers.value)
    if (next.has(layer)) next.delete(layer)
    else next.add(layer)
    enabledLayers.value = next
  }

  function enableAll() {
    enabledLayers.value = new Set(CALENDAR_LAYERS)
  }

  function disableAll() {
    enabledLayers.value = new Set()
  }

  function setItems(xs: CalendarFeedItem[]) {
    items.value = xs
  }

  return {
    enabledLayers,
    items,
    filteredItems,
    groupByDate,
    toggle,
    enableAll,
    disableAll,
    setItems,
  }
}
```

- [ ] **Step 4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/composables/__tests__/useCalendarLayers.test.ts
```

Expected: 6 passed。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/composables/useCalendarLayers.ts src/composables/__tests__/useCalendarLayers.test.ts
git commit -m "feat(calendar): useCalendarLayers composable with localStorage persist"
```

---

### Task 14: CalendarView 整合（toggle UI + 非 event layer 渲染）

**Files:**
- Modify: `ivy-frontend/src/views/CalendarView.vue`

- [ ] **Step 1: 看現況**

```bash
cd ~/Desktop/ivy-frontend && grep -n "getCalendarFeed\|getEvents\|onMounted\|monthStart" src/views/CalendarView.vue | head -20
```

確認既有月份切換的 hook 點與資料載入函式名稱。

- [ ] **Step 2: 加 layer toggle UI（template 上方）**

在 CalendarView.vue `<template>` 月曆 grid 上方加入：

```vue
<div class="calendar-layer-toggle">
  <ElCheckboxGroup v-model="selectedLayerArr" size="small">
    <ElCheckbox
      v-for="layer in CALENDAR_LAYERS"
      :key="layer"
      :value="layer"
      :style="{ '--layer-dot': LAYER_COLORS[layer] }"
    >
      <span class="layer-dot" />
      {{ LAYER_LABELS[layer] }}
    </ElCheckbox>
  </ElCheckboxGroup>
  <ElButton size="small" link @click="enableAll">全選</ElButton>
  <ElButton size="small" link @click="disableAll">清除</ElButton>
</div>
```

- [ ] **Step 3: 加 script setup 整合**

在 `<script setup>` 加入：

```ts
import { computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElCheckboxGroup, ElCheckbox, ElButton, ElTooltip } from 'element-plus'
import { useCalendarLayers } from '@/composables/useCalendarLayers'
import { getAdminFeed, type CalendarFeedItem } from '@/api/calendar'
import {
  CALENDAR_LAYERS,
  LAYER_LABELS,
  LAYER_COLORS,
} from '@/constants/calendarLayers'

const router = useRouter()
const {
  enabledLayers,
  filteredItems,
  groupByDate,
  toggle,
  enableAll,
  disableAll,
  setItems,
} = useCalendarLayers()

// 把 Set ↔ array bridge 給 ElCheckboxGroup
const selectedLayerArr = computed({
  get: () => [...enabledLayers.value],
  set: (v: string[]) => {
    enabledLayers.value = new Set(v as never)
  },
})

// 月份切換時呼叫（沿用既有 currentMonth ref；以下偽簽章——對齊既有實作）
async function loadAdminFeed(monthStart: string, monthEnd: string) {
  const resp = await getAdminFeed(monthStart, monthEnd)
  setItems(resp.data.items)
}

// 既有 onMounted + watch(currentMonth) 呼叫點旁加 loadAdminFeed
// (實作時找既有 getCalendarFeed call site 鄰位加入)

function onLayerItemClick(item: CalendarFeedItem) {
  if (item.link) {
    router.push(item.link)
  }
  // null link 走既有 EventDetailDialog（event/meeting）
}
```

- [ ] **Step 4: cell 渲染（非 event layer 細色條）**

於既有 cell template 內找到 event 顯示區塊，旁邊加：

```vue
<div class="cell-other-layers">
  <template
    v-for="(it, idx) in nonEventItemsForCell(cellDate)"
    :key="`${it.layer}-${it.id}`"
  >
    <ElTooltip
      v-if="idx < 4"
      :content="`${LAYER_LABELS[it.layer]}: ${it.title}`"
      placement="top"
    >
      <div
        class="layer-strip"
        :style="{ background: it.color }"
        @click.stop="onLayerItemClick(it)"
      />
    </ElTooltip>
  </template>
  <div
    v-if="nonEventItemsForCell(cellDate).length > 4"
    class="layer-more"
    @click.stop="openCellPopover(cellDate)"
  >
    +{{ nonEventItemsForCell(cellDate).length - 4 }}
  </div>
</div>
```

並加 helper：

```ts
import { ElMessageBox } from 'element-plus'

function nonEventItemsForCell(date: string): CalendarFeedItem[] {
  return (groupByDate.value[date] || []).filter((x) => x.layer !== 'event')
}

function openCellPopover(date: string) {
  // v1：用 ElMessageBox 列全部，點任一筆走 onLayerItemClick
  // v2 改 ElPopover 嵌入畫面（Phase B 一起處理）
  const items = nonEventItemsForCell(date)
  ElMessageBox({
    title: date,
    message: items
      .map((it) => `${LAYER_LABELS[it.layer]}: ${it.title}`)
      .join('\n'),
    type: 'info',
  }).catch(() => { /* user cancel */ })
}
```

> **`cellDate` 由既有 CalendarView 月曆 grid 的 v-for loop 提供**（每 cell 一個 ISO 日期 string），不需新增；找既有 cell template 對應變數名連動。

- [ ] **Step 5: 加 scoped CSS**

```vue
<style scoped>
.calendar-layer-toggle {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  flex-wrap: wrap;
}
.layer-dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--layer-dot);
  margin-right: 4px;
  vertical-align: middle;
}
.cell-other-layers {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 2px;
}
.layer-strip {
  height: 4px;
  border-radius: 2px;
  cursor: pointer;
}
.layer-more {
  font-size: 10px;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  text-align: right;
  padding-right: 2px;
}
</style>
```

- [ ] **Step 6: typecheck + build**

```bash
cd ~/Desktop/ivy-frontend && npm run typecheck 2>&1 | tail -10 && npm run build 2>&1 | tail -10
```

Expected: 0 error。

- [ ] **Step 7: 既有 CalendarView 測試（若有）跑一輪**

```bash
cd ~/Desktop/ivy-frontend && npx vitest run src/views/__tests__/CalendarView 2>/dev/null || echo "no existing tests"
```

Expected: 既有 test 全綠（本改動不該破壞既有月曆 grid）。

- [ ] **Step 8: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/views/CalendarView.vue
git commit -m "feat(calendar): wire admin_feed layer toggle into CalendarView

月曆 grid 上方 6 chip toggle（localStorage 持久化），
非 event layer 以 4px 細色條 + tooltip 顯示，
點擊 deep-link 對應頁面或開既有 EventDetailDialog。"
```

---

## Phase 4：收尾

### Task 15: 手測 + 跑全套測試

- [ ] **Step 1: 啟動兩端 dev server**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

- [ ] **Step 2: 手測 checklist**

打開 http://localhost:5173/calendar，登入管理員帳號，逐項驗：

- [ ] 月曆上方出現 6 個 chip toggle，預設全選
- [ ] 取消「請假」chip，cell 內藍/灰細條消失；重整頁面後狀態保留（localStorage）
- [ ] 切到下個月，loading 出現後資料更新
- [ ] 點請假細條，跳到 `/leaves?id=...`
- [ ] 同 cell 超過 4 條時出現 `+N`，點開（暫 console.log）
- [ ] DevTools Network：切月才打 `/api/calendar/admin_feed`、切 chip 不打

- [ ] **Step 3: 跑後端全套 + 前端關鍵**

```bash
cd ~/Desktop/ivy-backend && pytest tests/test_calendar_admin.py tests/test_calendar_colors.py tests/test_calendar_admin_schemas.py -v
cd ~/Desktop/ivy-frontend && npx vitest run src/composables/__tests__/useCalendarLayers.test.ts src/constants/__tests__/calendarLayers.test.ts
```

Expected: 全綠。

- [ ] **Step 4: 跑後端全套確認零 regression**

```bash
cd ~/Desktop/ivy-backend && pytest -x --ff 2>&1 | tail -20
```

Expected: 0 new fail（pre-existing fail 不算）。

- [ ] **Step 5: 若一切 OK，無須額外 commit；若手測修了 cell 內 popover 等，視動到的檔案分 commit**

---

## Self-Review

**1. Spec coverage：**

| Spec 章節 | 對應 Task |
|---|---|
| API 契約 request | Task 3 |
| API 契約 response envelope | Task 2 |
| Layer 對照表（6 layer） | Task 4-9 各一 |
| Layer 顏色 / label 常數 | Task 1（後）/ Task 12（前） |
| 防 N+1 | Task 10 |
| 權限 gating | Task 6+10（leave/cross-perm matrix） |
| Sort by start/layer | Task 10 |
| 90 天 window 上限 | Task 3 |
| 前端 layer toggle + localStorage | Task 13 |
| 前端 cell 渲染 + deep-link | Task 14 |
| 前端 切月才打 API、layer 過濾本地 | Task 14 |

**2. Placeholder scan：** 無 TBD / TODO / 「補齊既有 fixture」標註處皆有具體說明（沿用 conftest.py 既有 admin_token / db_session pattern）。

**3. Type consistency：**
- `CalendarLayer` 在 `api/calendar.ts` 定義、`constants/calendarLayers.ts` 與 `useCalendarLayers.ts` 皆 import 同一型別 ✓
- 後端 `Layer` Literal 在 schemas/calendar_admin.py 定義、各 fetcher 使用同名 layer string ✓
- 函式名一致：`getAdminFeed`、`useCalendarLayers`、`setItems`、`filteredItems`、`groupByDate`、`toggle`、`enableAll`、`disableAll` 全文件一致 ✓

---

## Out of Scope（未來 Phase）

- **Phase B**：FullCalendar v6 多檢視 + 拖拉改期 → 吃本案 admin_feed，另開 spec
- **Phase C**：`school_events.recurrence_rule` JSONB + endpoint expansion → 另開 spec
- **interview layer**：待後續建立面試排程表（current `RecruitmentVisit.visit_date` 為 String 不能用）
- **server cache**：暫不加；若 prod 觀察到 admin_feed P95 > 200ms 再評估
- **meeting 排程預先建檔**：current MeetingRecord 是事後出席記錄，無前瞻價值；若要做「排定下週開會」需新表


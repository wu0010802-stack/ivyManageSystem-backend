# 通知中央 dispatcher 設計（2026-05-25）

## 1. 背景與動機

幼稚園系統的通知 fan-out 目前**沒有中央 router**，三通道（LINE push / WebSocket / in-app）各走各的：

- **LINE**：`services/line_service.py` 877 行單一巨類，21 個 `notify_*` method，員工與家長混雜
- **WS**：兩個獨立 manager — `api/dismissal_ws.py`（自製 `DismissalConnectionManager`）+ `api/contact_book_ws.py`（用共用 `hub`），結構不一
- **In-app**：**目前根本沒有持久層**（grep `Notification` model / `notification_log` table 都查不到）
- **既有「中央」**：`services/notification/approval_notifier.py` 只服務員工 3 種審核結果（leave / overtime / punch_correction），3 個 caller，是個 thin wrapper，並未過 preference gate
- **家長端 preference 完整**：`ParentNotificationPreference` 表 + 7 event_type + sparse row + `is_pref_enabled` 純函式 + `should_push_to_parent` gate（line_service 內）
- **員工端 preference**：**完全沒有** — `notify_leave_submitted` / `notify_*_result` 等直接 push，從不檢查偏好

這造成四個具體痛點：

1. 新增 event 找不到統一入口，每次都複製 LINE call site → preference 漏接、PII 漏遮、log 漏寫
2. 員工永遠無法關閉 LINE 推播，下班還是會被進修 / 簽核訊息打擾
3. 沒有 in-app 持久層，所有通知只活在 LINE 訊息流 / WS 廣播當下，事後無從追查
4. 與招生 Phase B 規劃中的 4.1 event bus / 4.3 offboarding 是同一張地圖，但目前沒有可以「掛 subscriber」的中心點

本 spec 收斂三通道為單一 dispatch.py 入口，補齊 in-app 持久層與員工 preference，並透過 SQLAlchemy `after_commit` hook 解決「caller 必須記得在 commit 後呼叫」的不可靠性。

## 2. 設計總覽

```
┌─────────────────────────────────────────────────────────────┐
│  Caller (router / service / scheduler)                       │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ dispatch.enqueue(                                    │    │
│  │   session, event_type="leave.approved",              │    │
│  │   recipient_user_id=42, context={...},               │    │
│  │   sender_id=current_user.id,                         │    │
│  │   source_entity_type="leave_request",                │    │
│  │   source_entity_id=1234)                             │    │
│  └─────────────────────────────────────────────────────┘    │
│                       │ (僅註冊到 session.info)              │
│                       ▼ tx commit 觸發                       │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ SQLAlchemy after_commit hook → _drain(session)       │    │
│  └─────────────────────────────────────────────────────┘    │
│                       │                                      │
│         ┌─────────────┼──────────────┐                       │
│         ▼             ▼              ▼                       │
│  ┌────────────┐ ┌──────────┐ ┌──────────────┐               │
│  │ in-app log │ │ pref gate│ │ channel fan-out             │
│  │ INSERT     │ │ (per ch) │ │  ├─ LINE adapter             │
│  │ 一筆       │ │          │ │  └─ WS adapter (async bridge)│
│  └────────────┘ └──────────┘ └──────────────┘               │
└─────────────────────────────────────────────────────────────┘
```

**單一規則**：所有員工 / 家長通知一律走 `dispatch.enqueue(session=…, event_type=…, recipient_user_id=…, context=…)`。沒有第二個入口。

### 新檔與保留檔

**新增**：
- `services/notification/dispatch.py` — 對外入口 `enqueue()` + `_drain()` + `_fan_out()`（含 in-app 落 log 與 inbox WS push 邏輯，因 log_id 是其他 adapter 的前置依賴，無法抽到 adapter）
- `services/notification/channel_matrix.py` — event_type → 預設通道對映
- `services/notification/event_types.py` — 17 event_type 字串常數 + 集合
- `services/notification/renderers.py` — 每 event_type 一個 `(title, body, deep_link)` 純函式 renderer
- `services/notification/_channels/line.py` — LINE adapter（內部 call 既有 `line_service.notify_*`，過渡期 thin dispatch 表）
- `services/notification/_channels/ws.py` — WS adapter（sync→async bridge；只處理 parent.* 與 dismissal.created）+ `_inbox_ws_push` 同步 wrapper（員工通知中心 realtime 推送，給 `dispatch._fan_out` 直接呼叫）
- `models/notification_log.py` — `NotificationLog` model
- `api/inbox_ws.py` — Phase 1 skeleton（hub key + `inbox_broadcast_user()`）；Phase 3 補 WS endpoint
- `api/notifications.py` — 員工通知中心 4 REST endpoint（Phase 3）
- Alembic `notif01_consolidation` migration

**保留不動**：
- `services/line_service.py` 全部 21 method（dispatch 內部繼續 call，Phase 4 才退役）
- `api/dismissal_ws.py` / `api/contact_book_ws.py` WS manager 結構不動，dispatch 透過它們既有的 `broadcast_*` 函式 fan-out

**刪除**：
- `services/notification/approval_notifier.py` — Phase 2 PR-A 完成後 dispatch 取代

## 3. Event taxonomy + Channel matrix

### 3.1 命名規約

採兩級 `{domain}.{action}`，對齊未來 4.1 event bus 命名慣例。Glob 友善（`leave.*` 可批次配置），結構清晰。

### 3.2 v1 收 17 個 event_type

```
員工域：
  leave.submitted              # 員工送假 → 簽核者
  leave.approved               # 簽核 → 員工本人
  leave.rejected               # 簽核 → 員工本人
  overtime.submitted           # 員工送加班 → 簽核者
  overtime.approved            # 簽核 → 員工本人
  overtime.rejected            # 簽核 → 員工本人
  punch_correction.approved    # 簽核 → 員工本人
  punch_correction.rejected    # 簽核 → 員工本人
  salary.batch_completed       # 月底結薪完成 → HR / admin
  activity.waitlist_promoted   # 候補轉正 → 員工
  pos.unlock_requested         # POS 解鎖請求 → 主管
  dismissal.created            # 接送通知 → 班級群組（LINE 群組 push）

家長域（沿用既有 7 個 + 加 parent. 前綴）：
  parent.message_received
  parent.announcement
  parent.event_ack_required
  parent.fee_due
  parent.leave_result
  parent.attendance_alert
  parent.contact_book_published
```

`event_types.py` 提供 `NOTIFICATION_EVENT_TYPES: frozenset[str]`，作為 dispatch 入口檢核來源。

### 3.3 Channel matrix（宣告式 dict）

```python
# services/notification/channel_matrix.py
from typing import Literal

Channel = Literal["in_app", "line", "ws"]

CHANNEL_MATRIX: dict[str, tuple[Channel, ...]] = {
    # event_type → 預設啟用通道（順序即 fan-out 順序）
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
    "dismissal.created":            ("line", "ws"),   # 群組推播 LINE + 教師 WS，不寫個人 in_app
    "parent.message_received":      ("line", "ws"),
    "parent.announcement":          ("line",),
    "parent.event_ack_required":    ("line",),
    "parent.fee_due":               ("line",),
    "parent.leave_result":          ("line",),
    "parent.attendance_alert":      ("line",),
    "parent.contact_book_published":("line", "ws"),
}
```

**規則**：

- `in_app` 不檢查 preference（員工通知中心要全紀錄，看不看由 UI 處理）— 一律寫 `notification_logs`
- in_app 處理路徑由 `_fan_out` 內聯實作（**不抽成獨立 adapter**），原因：log_id 是其他 channel 的前置依賴，必須先落 log 才能呼叫 line/ws adapter
- in_app 路徑寫 log 完成後，`_fan_out` 自動呼叫 `_inbox_ws_push` 把通知推給 `recipient_user_id`（員工通知中心 realtime toast / 紅點更新）；失敗只 warning 不算 in_app failure
- `line` / `ws` 過 `notification_preferences` gate，缺 row = enabled（沿用稀疏 row 慣例）
- `ws` channel **只負責非 inbox 的 WS 推送**：parent.* 走 `broadcast_parent`、`dismissal.created` 走 classroom WS（既有 `dismissal_ws.manager.broadcast`）
- 家長端 v1 不寫 in_app（LIFF mini-app 現階段沒通知中心 UI）
- `dismissal.created` 是「群組推播」不是個人推播，channel 為 `("line", "ws")` 但 recipient_user_id 略 → adapter 內由 `context["classroom_id"]` 決定 target

### 3.4 Dispatch 公開 API

```python
def enqueue(
    session: Session,
    *,
    event_type: str,
    recipient_user_id: int | None,   # None 表群組推播（dismissal）
    context: dict,                    # 渲染 LINE / WS payload 用
    sender_id: int | None = None,
    source_entity_type: str | None = None,  # e.g. "leave_request"
    source_entity_id: int | None = None,    # 反查源頭 + 未來冪等鍵
    channels_override: tuple[Channel, ...] | None = None,  # 罕用
) -> None
```

`source_entity_type` + `source_entity_id` v1 不做 dedupe，欄位先留著，Phase 4 outbox 升級時 idempotency key 用得到。

## 4. 資料模型

### 4.1 `notification_logs`（新表）

```python
class NotificationLog(Base):
    __tablename__ = "notification_logs"
    __table_args__ = (
        Index("ix_notif_log_recipient_unread",
              "recipient_user_id", "read_at",
              postgresql_where=text("read_at IS NULL")),
        Index("ix_notif_log_recipient_created",
              "recipient_user_id", "created_at"),
        Index("ix_notif_log_source",
              "source_entity_type", "source_entity_id"),
    )

    id                  = Column(BigInteger, primary_key=True, autoincrement=True)
    recipient_user_id   = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                                 nullable=False)
    event_type          = Column(String(60), nullable=False)
    sender_id           = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                                 nullable=True)
    title               = Column(String(120), nullable=False)
    body                = Column(Text, nullable=False)
    payload_json        = Column(JSON, nullable=False, default=dict)
    source_entity_type  = Column(String(40), nullable=True)
    source_entity_id    = Column(Integer, nullable=True)
    deep_link           = Column(String(255), nullable=True)
    channels_attempted  = Column(JSON, nullable=False, default=list)
    channels_succeeded  = Column(JSON, nullable=False, default=list)
    channels_failed     = Column(JSON, nullable=False, default=list)
    read_at             = Column(DateTime, nullable=True)
    created_at          = Column(DateTime, default=datetime.now, nullable=False)
```

**設計重點**：

- **單筆 row 代表一個 event 的完整 fan-out 結果**（不是每通道一筆）。三個 `channels_*` JSON 欄位記錄通道狀態
- `title` / `body` 在 enqueue 時由 renderer 預先渲染寫入 → 通知中心 list 零 join
- `payload_json` 留結構化資料給前端深用（avatar / status chip），與 title/body 重複但用途不同
- `deep_link` 由 renderer 預組 → 加新 event 時前端零改動
- partial index `ix_notif_log_recipient_unread` 加速「未讀紅點數量」query

**PII**：title / body 含人名（員工 / 學生）— 與既有 LINE push 同等敏感，不送 Sentry。Retention policy 列為 Phase 4 follow-up（建議 900 天 GC）。

### 4.2 `parent_notification_preferences` rename → `notification_preferences`

```python
# Alembic 不改 schema 欄位，只改表名 + 加索引 + backfill event_type 前綴
op.rename_table("parent_notification_preferences", "notification_preferences")
op.execute("ALTER TABLE notification_preferences "
           "RENAME CONSTRAINT uq_parent_notif_pref_triple TO uq_notif_pref_triple")
op.create_index(
    "ix_notif_pref_user_event",
    "notification_preferences",
    ["user_id", "event_type"],
    unique=False,
)
op.execute("""
    UPDATE notification_preferences
       SET event_type = 'parent.' || event_type
     WHERE event_type IN (
       'message_received','announcement','event_ack_required','fee_due',
       'leave_result','attendance_alert','contact_book_published'
     )
""")
```

**前端衝擊**：

- `GET/PUT /api/parent/notifications/preferences` response shape `{prefs: {event_type: bool}}` 不變
- keys 從 `"message_received"` 變 `"parent.message_received"`
- 前端 `src/api/notifications.ts` + `NotificationPrefsView` 同步改 key
- 已綁定的家長 row 由 migration 自動 backfill，UX 0 衝擊

### 4.3 event_type 不用 enum 欄

`String(60)` + code-side 列舉表，新增 event 不要 DB migration。DB 不檢查 event_type 是 trade-off，由 dispatch 入口 ValueError 把關 + pytest 覆蓋。

### 4.4 Migration

```
revision: notif01_consolidation
  up:
    1. rename parent_notification_preferences → notification_preferences
    2. ALTER CONSTRAINT RENAME
    3. CREATE INDEX ix_notif_pref_user_event
    4. UPDATE event_type 加 parent. 前綴
    5. CREATE TABLE notification_logs + 三 index
  down:
    1. DROP TABLE notification_logs
    2. UPDATE event_type 去 parent. 前綴
    3. DROP INDEX ix_notif_pref_user_event
    4. ALTER CONSTRAINT RENAME back
    5. rename notification_preferences → parent_notification_preferences
```

## 5. Dispatch 內部執行

### 5.1 入口 + 排隊

```python
# services/notification/dispatch.py
from sqlalchemy.orm import Session
from dataclasses import dataclass

@dataclass(frozen=True)
class PendingEvent:
    event_type: str
    recipient_user_id: int | None
    context: dict
    sender_id: int | None
    source_entity_type: str | None
    source_entity_id: int | None
    channels: tuple[Channel, ...]

_QUEUE_KEY = "ivy_notification_queue"

def enqueue(
    session: Session,
    *,
    event_type: str,
    recipient_user_id: int | None,
    context: dict,
    sender_id: int | None = None,
    source_entity_type: str | None = None,
    source_entity_id: int | None = None,
    channels_override: tuple[Channel, ...] | None = None,
) -> None:
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

### 5.2 after_commit hook

```python
from sqlalchemy import event
from models.database import SessionLocal  # 主庫 session factory

@event.listens_for(SessionLocal, "after_commit")
def _drain_after_commit(session: Session) -> None:
    pending = session.info.pop(_QUEUE_KEY, None)
    if not pending:
        return
    for evt in pending:
        try:
            _fan_out(evt)
        except Exception:
            logger.exception("dispatch fan-out 失敗 event=%s recipient=%s",
                             evt.event_type, evt.recipient_user_id)
            # 絕不 re-raise — 一筆 fan-out 失敗不能影響後續 commit

@event.listens_for(SessionLocal, "after_rollback")
def _clear_on_rollback(session: Session) -> None:
    session.info.pop(_QUEUE_KEY, None)
```

**為什麼只掛 `SessionLocal` 不是全域 `Session`**：parent_db / spike_rls 等其他 session factory 不應誤觸（家長端 RLS spike 在跑，分離很重要）。

### 5.3 寫 log 用 short-lived session

after_commit 觸發時原 session 已 commit，不能再用它寫 log。新開 short-lived session：

```python
def _fan_out(evt: PendingEvent) -> None:
    log_session = SessionLocal()
    try:
        log_row = NotificationLog(
            recipient_user_id=evt.recipient_user_id,
            event_type=evt.event_type,
            sender_id=evt.sender_id,
            source_entity_type=evt.source_entity_type,
            source_entity_id=evt.source_entity_id,
            payload_json=evt.context,
            channels_attempted=list(evt.channels),
            channels_succeeded=[],
            channels_failed=[],
            title="", body="", deep_link=None,
        )
        rendered = render(evt.event_type, evt.context)
        log_row.title = rendered.title
        log_row.body = rendered.body
        log_row.deep_link = rendered.deep_link

        active_channels = [
            ch for ch in evt.channels
            if ch == "in_app"  # in_app 強制
            or _pref_enabled(log_session, evt.recipient_user_id, evt.event_type, ch)
        ]

        if "in_app" in evt.channels:
            log_row.channels_succeeded.append("in_app")

        log_session.add(log_row)
        log_session.commit()
        log_id = log_row.id

        # in_app 寫完 log 後立刻推 inbox WS（員工通知中心 realtime）
        # 失敗不阻 LINE，不算 in_app failure（log row 已寫入算成功）
        if "in_app" in active_channels and evt.recipient_user_id is not None:
            try:
                _inbox_ws_push(evt, rendered, log_id)
            except Exception as exc:
                logger.warning("inbox WS push 失敗 log_id=%s: %s", log_id, exc)

        for ch in active_channels:
            if ch == "in_app":
                continue
            adapter = CHANNEL_ADAPTERS[ch]
            try:
                adapter.send(evt, rendered, log_id=log_id)
                _mark_success(log_session, log_id, ch)
            except Exception as exc:
                _mark_failure(log_session, log_id, ch, str(exc))
    finally:
        log_session.close()
```

### 5.4 Renderer

每 event_type 對應一個純函式 renderer：

```python
# services/notification/renderers.py
@dataclass(frozen=True)
class Rendered:
    title: str
    body: str
    deep_link: str | None

RENDERERS: dict[str, Callable[[dict], Rendered]] = {}

def renderer(event_type: str):
    def deco(fn):
        RENDERERS[event_type] = fn
        return fn
    return deco

@renderer("leave.approved")
def _r_leave_approved(ctx: dict) -> Rendered:
    return Rendered(
        title=f"{ctx['reviewer_name']} 已核准你的請假",
        body=f"{ctx['leave_type']} {ctx['start']} ~ {ctx['end']}",
        deep_link=f"/portal/leaves/{ctx['leave_id']}",
    )

def render(event_type: str, ctx: dict) -> Rendered:
    fn = RENDERERS.get(event_type)
    if fn is None:
        return Rendered(title=f"({event_type})", body="", deep_link=None)
    try:
        return fn(ctx)
    except Exception:
        logger.exception("renderer 失敗 event=%s", event_type)
        return Rendered(title="(渲染失敗)", body=f"event_type={event_type}", deep_link=None)
```

LINE adapter 吃 `Rendered.title + body` 組 plain text；複雜的 LINE Flex（既有 `notify_parent_message_received` 那種帶 quick reply）保留呼叫既有 `line_service` method — adapter 內 switch by event_type，過渡期 Flex message 不重寫。

### 5.5 WS adapter：sync→async bridge

```python
# services/notification/_channels/ws.py
import asyncio
from api.contact_book_ws import broadcast_parent
from api.dismissal_ws import manager as dismissal_manager

_EVENT_LOOP: asyncio.AbstractEventLoop | None = None

def register_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _EVENT_LOOP
    _EVENT_LOOP = loop

class WsAdapter:
    """ws channel：只處理非 inbox 的 WS 推送（parent.* / dismissal.created）。
    員工 inbox WS 由 _fan_out 在 in_app 落 log 後直接呼叫 _inbox_ws_push，不走此 adapter。"""

    def send(self, evt, rendered, *, log_id: int):
        if _EVENT_LOOP is None:
            raise RuntimeError("WS loop 未註冊（main.py startup 漏 register_loop）")
        coro = self._dispatch(evt, rendered, log_id)
        fut = asyncio.run_coroutine_threadsafe(coro, _EVENT_LOOP)
        fut.result(timeout=2.0)  # WS broadcast 應 < 100ms

    async def _dispatch(self, evt, rendered, log_id):
        payload = {
            "event_type": evt.event_type,
            "title": rendered.title,
            "body": rendered.body,
            "deep_link": rendered.deep_link,
            "log_id": log_id,
        }
        if evt.event_type.startswith("parent."):
            await broadcast_parent(evt.recipient_user_id, payload)
        elif evt.event_type == "dismissal.created":
            await dismissal_manager.broadcast(evt.context["classroom_id"], payload)
        else:
            raise RuntimeError(
                f"ws channel 不支援 event_type={evt.event_type}；"
                "員工 inbox WS 應走 _inbox_ws_push 不經此 adapter"
            )


# 員工 inbox WS 推送：in_app adapter 路徑專用
async def _inbox_ws_push_async(evt, rendered, log_id):
    from api.inbox_ws import inbox_broadcast_user
    payload = {
        "event_type": evt.event_type,
        "title": rendered.title,
        "body": rendered.body,
        "deep_link": rendered.deep_link,
        "log_id": log_id,
    }
    await inbox_broadcast_user(evt.recipient_user_id, payload)


def _inbox_ws_push(evt, rendered, log_id):
    """供 _fan_out 同步呼叫。loop 未註冊 raise，由 caller swallow。"""
    if _EVENT_LOOP is None:
        raise RuntimeError("WS loop 未註冊")
    fut = asyncio.run_coroutine_threadsafe(
        _inbox_ws_push_async(evt, rendered, log_id), _EVENT_LOOP
    )
    fut.result(timeout=2.0)
```

LINE adapter 簽名同步加 `log_id`：

```python
class LineAdapter:
    def send(self, evt, rendered, *, log_id: int):
        # log_id 留作未來 push receipt 追蹤；v1 不用
        ...
```

`main.py` `@app.on_event("startup")` 註冊：`register_loop(asyncio.get_event_loop())`。

### 5.6 LINE adapter

```python
# services/notification/_channels/line.py
class LineAdapter:
    def __init__(self, line_service):
        self._ls = line_service

    def send(self, evt, rendered, *, log_id: int):
        handler = LINE_HANDLERS.get(evt.event_type)
        if handler is None:
            self._ls.push_text_to_user(
                evt.recipient_user_id,
                rendered.title + "\n" + rendered.body,
            )
            return
        handler(self._ls, evt, rendered)
```

`LINE_HANDLERS` 是 event_type → 既有 line_service method 的 thin dispatch 表。Phase 1 全部 21 method 都列進去；Phase 4 才把 line_service.notify_* 重構為純 builder + 一個 push 入口。

### 5.7 Scheduler 場景

scheduler（`finance_reconciliation_scheduler` 等）跑在 APScheduler 自己的 thread，自己創 session。前提是它們也用 `models.database.SessionLocal`（已是現況），event hook 同樣會在 scheduler thread commit 時觸發。

## 6. Phase 拆分與遷移節奏

### 6.1 Phase 1：骨架落地（Backend，~5 工作日，1 PR）

- 新檔：`dispatch.py` / `channel_matrix.py` / `renderers.py` / `_channels/{line,ws}.py` / `event_types.py` / `models/notification_log.py`
- 新檔 `api/inbox_ws.py` **skeleton**：只包 hub key 常數 + `inbox_broadcast_user()` helper（無 subscribers 時 no-op）；WS endpoint 留到 Phase 3 補
- migration `notif01_consolidation`
- `main.py` startup hook 註冊 SQLAlchemy event listener + WS loop
- 家長端 `api/parent_portal/notifications.py` 的 7 個 event_type 改加 `parent.` 前綴；shape 不變
- 前端家長端 `src/api/notifications.ts` + `NotificationPrefsView` 同步 keys（依 workspace SOP 後端 merged 再改前端）
- **任何 caller 不動**
- pytest：dispatch 入口 + after_commit hook + rollback 清空 + WS bridge mock + 3 sample renderer + inbox_broadcast_user no-subscriber no-op

**完成判準**：python shell 手動 `dispatch.enqueue(...)` + commit 可看到 `notification_logs` 多 row + 既有 LINE/WS 通知零回歸 + inbox WS push 雖無 subscriber 但不拋例外。

### 6.2 Phase 2：Caller 遷移（Backend，~8 工作日，分 4 PR）

按域分批，每域一個 PR；每域結束時驗 (1) pytest 全綠 (2) 手動 trigger 通知看 LINE + log row 同時到。

| 批次 | Caller 範圍 | 影響 router | event_type |
|------|-----------|-----------|-----------|
| PR-A | 簽核三件（approval_notifier 三 caller） | leaves.py / overtimes.py / punch_corrections.py | `leave.*` / `overtime.*` / `punch_correction.*` |
| PR-B | 家長域全部 | parent_portal/* / portal/contact_book / announcements | `parent.*` |
| PR-C | 薪資 / 接送 / 才藝 / POS | salary/calculate / dismissal_calls / activity/* / portal/salary | `salary.*` / `dismissal.*` / `activity.*` / `pos.*` |
| PR-D | 收尾 + line_service 退役 | 把 `line_service.notify_*` 21 method 從 public 改為 `_notify_*` + deprecation comment | — |

每 PR 後 grep `line_service.notify_` / `approval_notifier` 確認該域歸零。`approval_notifier.py` PR-A 後刪除。

### 6.3 Phase 3：員工通知中心 UI（Frontend，~5 工作日，1 PR + Backend 1 工作日）

§7 細展開。Backend 在 Phase 1 已有 schema，Phase 3 開頭加 4 endpoint + WS subscribe。

### 6.4 Phase 4（defer，不在本 spec scope）

- Outbox pattern 升級
- `notification_logs` 900 天 GC scheduler
- Sentry 告警（fan-out 連續失敗）
- LINE Flex Message 集中重寫
- `interview_arranged` 等新 event 加入（招生 Phase B 完成後）

### 6.5 預估時程

| Phase | 工作日 | 完成判準 |
|-------|-------|---------|
| 1 | 5 (BE) + 0.5 (FE) | dispatch 可用、家長 pref key 換前綴、零回歸 |
| 2 | 8 (BE) | grep 21 caller 全歸零、line_service.notify_* 變內部 |
| 3 | 5 (FE) + 1 (BE endpoint) | 員工通知中心可用、未讀紅點即時更新 |
| **合計** | **約 4 週** | dispatch 唯一出口、in-app log 完整、員工有 inbox UI |

## 7. 員工通知中心 UI

### 7.1 Backend endpoints

```
GET  /api/notifications                  # list, 分頁
     query: ?limit=20&before_id=12345&only_unread=true
     response: {items: NotificationItem[], next_before_id: int | null}

GET  /api/notifications/unread_count
     response: {count: 42}

POST /api/notifications/{id}/mark_read
     response: {id, read_at}

POST /api/notifications/mark_all_read
     response: {marked: 42}
```

權限：`require_authenticated()` + `recipient_user_id == current_user.id` 自我隔離；admin **不** 能讀別人通知。

`NotificationItem` shape：

```typescript
{ id, event_type, title, body, deep_link, payload, sender_name, created_at, read_at }
```

`sender_name` 從 `users.display_name` 預 join 一次，list endpoint 一個 query 拿完。家長端不暴露此 endpoint（前綴 `/api/parent` 不 mount）。

### 7.2 員工 WS subscribe

`api/inbox_ws.py` Phase 1 已有 skeleton（hub key + broadcast helper），Phase 3 補完 WS endpoint + JWT auth：

```python
INBOX_USER_KEY = lambda uid: ("inbox_user", uid)

async def inbox_broadcast_user(user_id: int, payload: dict) -> None:
    await hub.broadcast([INBOX_USER_KEY(user_id)], payload)

@router.websocket("/inbox")
async def inbox_ws(ws: WebSocket):
    # JWT cookie auth → user_id → subscribe INBOX_USER_KEY(user_id)
    ...
```

WS payload `{event_type, title, body, deep_link, log_id}` — 前端收到後 ① unread_count + 1 ② 若 drawer 開著 prepend 一筆 ③ 顯示 toast。

### 7.3 Frontend

```
src/
  api/notifications.ts              # 4 endpoint wrapper（OpenAPI codegen 接型別）
  composables/useInbox.ts           # WS subscribe + Pinia store glue
  stores/inbox.ts                   # Pinia: items / unread_count / loading
  components/inbox/
    InboxBell.vue                   # 門鈴 icon + 紅點數字
    InboxDrawer.vue                 # 右側抽屜
    InboxItem.vue                   # 單筆 card
```

**UX 細節**：

- 門鈴在 `AdminHeader.vue` 右上、`UserMenuDropdown` 左邊
- 未讀 > 99 顯示 `99+`
- click 門鈴 → 開 drawer（Element Plus `el-drawer` 右側 360px）
- drawer 開啟自動 `GET /notifications?limit=20`；scroll 到底加載 `before_id` 分頁
- click 單筆 → `mark_read` + `router.push(deep_link)` + drawer 關閉
- 「全部已讀」按鈕在 drawer 頁首
- WS 連線在 `App.vue` mounted 時建立，登出 disconnect；重連策略沿用 `contact_book_ws` exponential backoff
- 空狀態：「目前沒有通知」+ 暗灰 SVG（沿用 IvyKids 設計語言）

**測試**：vitest 涵蓋 `useInbox`（WS message handling / dedupe by log_id）、`stores/inbox`（mark_read optimistic update / rollback on 500）、`InboxBell`（未讀數渲染 / `99+`）、`InboxDrawer`（list / mark_read flow / 空狀態）。E2E smoke 不加。

### 7.4 家長端通知中心：明確不做

家長端 v1 仍純 LINE 推播（已有 `NotificationPrefsView`）。未來若做，本 spec schema 已支援（channel matrix 補 `in_app` + 家長 WS subscribe），不阻塞。

## 8. 錯誤處理 / 測試 / 跨端 contract

### 8.1 錯誤處理矩陣

| 失敗點 | 行為 | 紀錄 |
|-------|-----|------|
| `enqueue` 時 event_type 未知 | `ValueError` 立刻拋 | caller 立即看到，pytest fail-fast |
| `enqueue` 時 `recipient_user_id` 不存在 | 不檢查，fan-out 時 fail-closed | log row 仍寫但 channels_failed 標 `recipient_not_found` |
| after_commit hook 內 `_fan_out` 拋例外 | `logger.exception` swallow，**不 re-raise** | 應用層完全無感 |
| Renderer 拋例外 | log row 仍寫，title=`(渲染失敗)` channels_failed 全標 `render_error` | 通知中心顯示「渲染失敗」 |
| LINE adapter `push` 5xx / timeout | log row INSERT 成功、channels_failed 加 `{"channel":"line","error":...}` | v1 不重試；Phase 4 outbox |
| WS adapter `fut.result(timeout=2.0)` 超時 | channels_failed 加 `{"channel":"ws","error":"timeout"}` | 監控 N 次同類 → 告警 (Phase 4) |
| `notification_logs` INSERT 失敗（DB down） | 整體進 `logger.exception`，LINE / WS 也跳過 | 應用層無感，但通知整體丟失。極罕見場景 |
| Preference gate 失敗 | fail-closed（不發），沿用 `should_push_to_parent` 慣例 | log warning |
| WS event loop 未註冊 | ws adapter `RuntimeError` → channels_failed 標 `ws_loop_unregistered`；inbox WS 失敗只 logger.warning（log row 已寫入算成功） | main.py startup 加 assert 避免 prod 漏 |
| inbox WS 推送失敗（loop unregistered / hub 異常） | `logger.warning` swallow；**不影響 channels_succeeded**（in_app 的成功定義是 log row INSERT，realtime push 是 best-effort） | 前端開 drawer 時走 REST 重抓，realtime 漏掉自會 reconcile |
| Scheduler thread commit | after_commit hook 一樣觸發 | 與 router 同行為 |

**核心原則**：dispatch / fan-out 任何失敗都絕對不能影響業務 tx。

### 8.2 測試覆蓋

| 層級 | 對象 | 條數 |
|-----|-----|------|
| Unit | `dispatch.enqueue` 入口檢核 | 6 |
| Unit | `channel_matrix` 對應正確（17 event_type 各一條） | 17 |
| Unit | 每個 renderer 純函式 happy path | 17 |
| Unit | `is_pref_enabled` gate | 3 |
| Integration | after_commit hook 觸發 → log row 寫入 | 2 |
| Integration | after_rollback hook 清空 queue | 1 |
| Integration | LINE adapter mock + 5xx 失敗記 channels_failed | 3 |
| Integration | WS adapter mock loop + broadcast + timeout 記失敗 | 3 |
| Integration | Renderer 拋例外 → log 仍寫 + title=渲染失敗 | 1 |
| Integration | Migration up/down 對 PG 跑 | 1 |
| Integration | 4 endpoint（list 分頁 / unread_count / mark_read / mark_all_read）+ self-isolation | 6 |
| Integration | 既有 router 行為不變（沿用既有 LINE notify 測試） | 沿用 |
| Frontend | `useInbox` / `stores/inbox` / `InboxBell` / `InboxDrawer` | 12 |
| **合計新增** | | **約 72 條** |

**回歸保護**：Phase 2 每 PR 跑 BE 全套（4486+ pytest）+ FE 全套（2400+ vitest），任一回歸不 merge。

### 8.3 跨端 contract（OpenAPI 防漂移）

- §7 4 endpoint + 家長 preference key 變動 → 後端 merged 後跑 `dump_openapi.py` → 前端 `npm run gen:api` → `schema.d.ts` 更新同步入
- CI `openapi-drift` job 已接，任何 router 改 response 漏 regen schema 會 fail
- 前端 `src/api/notifications.ts` 一律用 `import type { ApiResponse, ApiBody } from './_generated/typed'` 接型別

### 8.4 PII 對齊（CLAUDE.md #8）

- `notification_logs.title / body / payload_json` 含人名 → 不送 Sentry（dispatch logger.exception 已 swallow，stack trace 不含 payload）
- adapter 報錯時不夾 payload，只 log `event_type` / `channel` / `error_class`
- workspace CLAUDE.md 補一段「通知一律走 dispatch.enqueue」

## 9. 與 4.1 event bus / 4.3 offboarding 邊界

**dispatch ≠ event bus**。dispatch 是同步 fan-out（簽核當下要回 toast），event bus 是異步訂閱（多 subscriber、可重放）。兩者關係：

- dispatch.py 完全可獨立運作，不依賴 4.1 event bus 存在
- 未來 4.1 event bus 落地後，dispatch 成為其中一個 sync subscriber（subscribe `*` 或特定 domain）
- 4.3 offboarding 的「離職事件」會 emit 到 event bus；通知層接住該 event 後 emit `employee.offboarded` → dispatch → LINE 給 HR + in_app 給管理員
- 本 spec 不討論 4.1 / 4.3，只確保 dispatch 對外 API（`enqueue(event_type, recipient, context)`）對未來訂閱模型開放

## 10. 風險與緩解

1. **過渡期雙發**：dispatch + 既有 line_service 並存（Phase 2 中段）可能同一通知雙發。緩解：每 PR 移除舊 caller 時 grep verify；CI 加 grep gate 禁 `line_service.notify_` 出現在已遷的 router
2. **pytest 假綠**：pytest fixture 若用 `nested=True` 包整個測試會永遠不 commit → dispatch 不觸發 → 測試假綠。緩解：dispatch 測試專用 fixture 強制 `commit()`，`tests/conftest.py` 文件警示
3. **WS timeout 卡 endpoint**：`fut.result(timeout=2.0)` 若 WS hub 異常每筆通知多 2 秒延遲。緩解：監控；v1 採 wait + 2s timeout，Phase 4 outbox 時換 fire-and-forget
4. **後端缺 `response_model=` 致前端拿 unknown**：4 個 notification endpoint 一律寫 `response_model=NotificationItemResponse` 等，避免 codegen 出 `unknown`
5. **Migration backfill 失敗**：若 backfill UPDATE 失敗整個 migration rollback；新表 `notification_logs` 已建但前綴未加 → 家長 pref 全 disabled。緩解：migration 內加 try/except 包 backfill，失敗 rollback 整 migration（Alembic 預設行為，僅需 verify）

## 11. 文檔產出

- `services/notification/dispatch.py` 檔首 docstring 解釋整套 lifecycle
- workspace CLAUDE.md 增段：「通知一律走 `dispatch.enqueue`，禁直接 `line_service.notify_*`」
- 前端 CLAUDE.md 增段：「inbox UI 位置 / WS subscribe」
- 本 spec

## 12. 決策日誌

| 決定 | 選項 | 理由 |
|-----|-----|-----|
| Scope | B（cleanup + in-app log + employee pref）+ C add-on（員工通知中心 UI） | 純 cleanup 不解員工 preference 0 的痛點；全面重構風險高 |
| Event naming | A 兩級 `{domain}.{action}` | Glob 友善（`leave.*`），對齊未來 4.1 event bus |
| Tx timing | A sync post-commit 自動化 | 業務 tx 不被 fan-out 拖；caller 零樣板治本 fix「忘記在 commit 後呼叫」 |
| Preference schema | A rename 合表 | `users.role` 已能區分家長 vs 員工；不需 principal_type |
| Migration | A strangler fig | line_service 21 method 一次砍 PR 過大；每 router 一 commit 可進可退 |
| Internals | A per-session queue + SQLA after_commit hook | tx 語意嚴格對齊；對外 API 不變，未來換 outbox 內部可替換 |

# SPEC-008：通知統一 Dispatch

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | `services/notification/__init__.py`、`services/notification/dispatch.py`、`services/notification/channel_matrix.py`、`services/notification/event_types.py`、`services/notification/renderers.py`、`services/notification/_channels/__init__.py`、`services/notification/_channels/line.py`、`services/notification/_channels/ws.py` |
| Related | `docs/superpowers/specs/2026-05-25-notification-dispatch-design.md`、`docs/superpowers/specs/2026-05-26-line-service-phase4-retirement-design.md`、`models/notification_log.py`、`models/parent_notification.py`、`main.py`（lifespan 啟動 `install_session_hooks`）、`.github/workflows/ci.yml`（`notification-dispatch-gate` job） |

---

## Overview

通知系統採「事件驅動 + 統一入口 + after_commit 同步」設計，所有 caller 一律透過 `services.notification.dispatch.enqueue` 註冊事件，於 SQLAlchemy session commit 後再實際 fan-out 至 in_app / LINE / WS 三個通道，避免「DB 已 rollback 但通知已發」的不一致狀態。

核心原則：

- **事件先註冊、tx 後發送**：`enqueue()` 只把事件寫入 `session.info[_QUEUE_KEY]`；實際發送由 `_drain_after_commit` listener 在 `after_commit` 事件中執行；`_clear_on_rollback` 在 rollback 時清空 queue。
- **唯一入口契約**：`api/` 與 `services/` 不得直接呼叫 `line_service.notify_*` 或 `line_service.push_to_user`；由 `notification-dispatch-gate` CI job 強制（regex `_?line_(service|svc)\._?notify_` 與 `_?line_service\.push_to_user`），白名單為 `services/line_service.py` 與 `services/notification/_channels/`。
- **宣告式 channel matrix**：event_type → channel tuple 集中在 `channel_matrix.py`；新增 event 必須三處同步：`event_types.py` 加常數、`channel_matrix.py` 加路由、`renderers.py` 加 `@renderer` 函式。
- **Channel 失敗隔離**：任一 channel 失敗只在 `notification_logs.channels_failed` 留紀錄並 `logger.exception`，絕不 re-raise（業務 tx 已 commit，rollback 通知無意義）。
- **Phase 4 退役（2026-05-26 cutover）**：原 `line_service.{,_}notify_*` 全數轉為 `services/notification/_channels/line.py` 的私有 `_h_*` handler；caller 改走 `dispatch.enqueue`，dismissal 群組推送透過 `line_group_id` 參數走 `LINE_HANDLERS["dismissal.created"]`，admin 觸發的 growth report 推送走 `dispatch.send_to_line_user_sync` 同步 API 取得 ACK。

`main.py` 啟動時於 `app_lifespan` 內呼叫 `install_session_hooks(get_session_factory())`（line 281-285），把 `after_commit` / `after_rollback` listener 綁到主庫 session factory；測試 fixture swap factory 後需再呼叫一次。

---

## Interface Definitions

### Python Public API（`services/notification/dispatch.py`）

`dispatch.py` 對外暴露 3 個 public function 與 1 個 public dataclass；其餘以 `_` 前綴為內部 helper。

| Symbol | Type | 用途 |
|--------|------|------|
| `PendingEvent` | `@dataclass(frozen=True)` | 封裝待發事件；存於 `session.info[_QUEUE_KEY]` 列表中等待 commit |
| `enqueue(session, *, event_type, recipient_user_id, context, sender_id=None, source_entity_type=None, source_entity_id=None, channels_override=None, line_group_id=None)` | function | 通知系統 **唯一對外入口**；註冊事件到當前 session queue，等 `session.commit()` 後由 after_commit listener fan-out |
| `install_session_hooks(factory: sessionmaker)` | function | App 啟動時把 `after_commit` / `after_rollback` listener 綁到指定 session factory；idempotent（同 factory 重複呼叫只綁一次） |
| `send_to_line_user_sync(line_user_id: str, event_type: str, context: dict) -> bool` | function | **同步**推送 LINE 個人通知並回 ACK；caller 需 sync 拿 sent_count（如 `api/portfolio/reports.py` send-line 端點）走此 API。不寫 `NotificationLog`、不過 preference gate（caller 自管 idempotency） |

`enqueue()` 完整契約：

- `event_type` 必須屬於 `NOTIFICATION_EVENT_TYPES`，否則 raise `ValueError`
- `recipient_user_id` 為 `int | None`；`None` 表群組推播（如 `dismissal.created`，無 in_app 個人收件人）
- `context: dict` 會被 **淺拷貝** 後存入 `PendingEvent.context`
- `channels_override` 罕用；用於特殊情境覆蓋 `CHANNEL_MATRIX`
- `line_group_id` 為 LINE 群組推送 mode 旗標（Phase 4 Section 2 加入）；設值時 LINE adapter 改走 `push_text_to_group(group_id, text)`，跳過 `_resolve_line_user_id` 個人解析
- 若 session 未開 transaction，會 `session.begin()` 以確保 after_rollback hook 能觸發

私有 helper（caller 不可直接呼叫，列出供 SPEC 完整性參考）：

| Symbol | 用途 |
|--------|------|
| `_drain_after_commit(session)` | after_commit listener；pop queue 並逐筆 `_fan_out` |
| `_clear_on_rollback(session)` | after_rollback listener；pop queue 丟棄 |
| `_fan_out(evt)` | 開 short-lived session：render → 過 preference gate → 寫 NotificationLog → 推 inbox WS / LINE / WS adapter；任何 channel 失敗 swallow 並寫 `channels_failed` |
| `_pref_enabled(session, user_id, event_type, channel)` | 偏好 gate；缺 row = True、DB 異常 fail-closed |
| `_resolve_line_user_id(session, user_id)` | `User.id` → `User.line_user_id`（需 `is_active` 且 `line_follow_confirmed_at` 非空）；fail-closed |
| `_get_line_adapter()` / `_get_ws_adapter()` | lazy-init adapter singleton |

模組層常數：

- `_QUEUE_KEY = "ivy_notification_queue"`：session.info dict key
- `_HOOKS_INSTALLED: set[sessionmaker]`：已安裝 hook 的 factory 集合（idempotency 用）
- `_line_adapter`、`_ws_adapter`：lazy-init singleton（首次呼叫時建立）

### Event Types（`event_types.py`）

v1 共 **23 個** event_type，採兩級命名空間 `{domain}.{action}`。完整列表彙整於 `NOTIFICATION_EVENT_TYPES: frozenset[str]`，新增 event 須同步加進此 frozenset 與 `channel_matrix.py`、`renderers.py`。

#### 員工域（12 個）

| Event | Python 常數 | 對應 channel | 說明 |
|-------|------------|--------------|------|
| `leave.submitted` | `LEAVE_SUBMITTED` | `in_app`, `line` | 員工送出請假申請（推送給 reviewer） |
| `leave.approved` | `LEAVE_APPROVED` | `in_app`, `line` | 請假審核核准（推送給申請者） |
| `leave.rejected` | `LEAVE_REJECTED` | `in_app`, `line` | 請假審核駁回（推送給申請者） |
| `overtime.submitted` | `OVERTIME_SUBMITTED` | `in_app`, `line` | 員工送出加班申請 |
| `overtime.approved` | `OVERTIME_APPROVED` | `in_app`, `line` | 加班審核核准 |
| `overtime.rejected` | `OVERTIME_REJECTED` | `in_app`, `line` | 加班審核駁回 |
| `punch_correction.approved` | `PUNCH_CORRECTION_APPROVED` | `in_app`, `line` | 補打卡核准 |
| `punch_correction.rejected` | `PUNCH_CORRECTION_REJECTED` | `in_app`, `line` | 補打卡駁回 |
| `salary.batch_completed` | `SALARY_BATCH_COMPLETED` | `in_app`, `line` | 薪資批次完成 |
| `activity.waitlist_promoted` | `ACTIVITY_WAITLIST_PROMOTED` | `in_app`, `line` | 才藝候補轉正（員工觀點通知） |
| `pos.unlock_requested` | `POS_UNLOCK_REQUESTED` | `in_app`, `line` | POS 日結解鎖請求 |
| `dismissal.created` | `DISMISSAL_CREATED` | `line`, `ws` | 接送通知建立（群組推送，無 in_app） |

#### 家長域（7 個）

| Event | Python 常數 | 對應 channel | 說明 |
|-------|------------|--------------|------|
| `parent.message_received` | `PARENT_MESSAGE_RECEIVED` | `line`, `ws` | 家長收到老師訊息（LINE 帶 quick-reply postback） |
| `parent.announcement` | `PARENT_ANNOUNCEMENT` | `line` | 園所公告 |
| `parent.event_ack_required` | `PARENT_EVENT_ACK_REQUIRED` | `line` | 待簽閱事件 |
| `parent.fee_due` | `PARENT_FEE_DUE` | `line` | 學費到期 |
| `parent.leave_result` | `PARENT_LEAVE_RESULT` | `line` | 學生請假審核結果（給家長） |
| `parent.attendance_alert` | `PARENT_ATTENDANCE_ALERT` | `line` | 出席異常提醒 |
| `parent.contact_book_published` | `PARENT_CONTACT_BOOK_PUBLISHED` | `line`, `ws` | 每日聯絡簿發布 |

#### 才藝家長域（3 個）

| Event | Python 常數 | 對應 channel | 說明 |
|-------|------------|--------------|------|
| `activity.waitlist_reminder` | `ACTIVITY_WAITLIST_REMINDER` | `line` | T-24h 候補提醒（給家長） |
| `activity.waitlist_final_reminder` | `ACTIVITY_WAITLIST_FINAL_REMINDER` | `line` | T-6h 候補最後提醒 |
| `activity.waitlist_expired` | `ACTIVITY_WAITLIST_EXPIRED` | `line` | 候補名額已過期 |

#### 家長 Growth Report（1 個）

| Event | Python 常數 | 對應 channel | 說明 |
|-------|------------|--------------|------|
| `growth_report.published` | `GROWTH_REPORT_PUBLISHED` | `line` | 成長報告發布；admin 推送透過 `send_to_line_user_sync` 取得 ACK |

### Channel Matrix（`channel_matrix.py`）

`CHANNEL_MATRIX: dict[str, tuple[Channel, ...]]` 採宣告式 dict，`Channel = Literal["in_app", "line", "ws"]`。

路由規則：

- **`in_app`** — 不檢查 preference，一律寫 `notification_logs`；in_app 路徑由 `dispatch._fan_out` 內聯實作，落 log 後自動 push inbox WS
- **`line` / `ws`** — 過 `notification_preferences` gate（缺 row = enabled）
- **`ws` channel** — 只處理非 inbox WS（`parent.*` / `dismissal.created`）；員工 inbox WS 由 `_fan_out` 直接呼叫 `_inbox_ws_push`，不經 `WsAdapter`
- **順序意義** — tuple 順序即 fan-out 順序，但實作上 `in_app` 強制最先（`log_id` 是 line/ws 的前置依賴）
- **未在 matrix 內的 event** — `_fan_out` 不會發送；caller `enqueue` 仍能通過 event_type 檢查（因 enum 存在於 `NOTIFICATION_EVENT_TYPES`），但 `CHANNEL_MATRIX.get(...)` 回空 tuple 後 `enqueue` 直接 return [unverified — 視為 matrix 是強制要求]

### Renderers（`renderers.py`）

每個 event_type 對應一個 `@renderer(event_type)` 裝飾的純函式，回傳 `Rendered(title, body, deep_link)` dataclass。

| renderer 函式 | event_type | deep_link 模板 |
|--------------|-----------|----------------|
| `_r_leave_submitted` | `leave.submitted` | `/approvals/leaves/{leave_id}` |
| `_r_leave_approved` | `leave.approved` | `/portal/leaves/{leave_id}` |
| `_r_leave_rejected` | `leave.rejected` | `/portal/leaves/{leave_id}` |
| `_r_overtime_submitted` | `overtime.submitted` | `/approvals/overtimes/{overtime_id}` |
| `_r_overtime_approved` | `overtime.approved` | `/portal/overtimes/{overtime_id}` |
| `_r_overtime_rejected` | `overtime.rejected` | `/portal/overtimes/{overtime_id}` |
| `_r_punch_corr_approved` | `punch_correction.approved` | `/portal/punch-corrections/{correction_id}` |
| `_r_punch_corr_rejected` | `punch_correction.rejected` | `/portal/punch-corrections/{correction_id}` |
| `_r_salary_batch` | `salary.batch_completed` | `/salary/{year}/{month}` |
| `_r_activity_waitlist` | `activity.waitlist_promoted` | `/activity/courses/{course_id}` |
| `_r_pos_unlock` | `pos.unlock_requested` | `/pos/unlock-requests/{request_id}` |
| `_r_dismissal_created` | `dismissal.created` | `None`（群組推播） |
| `_r_parent_message` | `parent.message_received` | `/parent/messages/{thread_id}` 或 `/parent/messages` |
| `_r_parent_announcement` | `parent.announcement` | `/parent/announcements/{announcement_id}` |
| `_r_parent_event_ack` | `parent.event_ack_required` | `/parent/event-ack/{event_id}` |
| `_r_parent_fee_due` | `parent.fee_due` | `/parent/fees` |
| `_r_parent_leave_result` | `parent.leave_result` | `/parent/leaves` |
| `_r_parent_attendance` | `parent.attendance_alert` | `/parent/attendance` |
| `_r_parent_contact_book` | `parent.contact_book_published` | `/parent/contact-book/{date}` |
| `_r_activity_waitlist_reminder` | `activity.waitlist_reminder` | `/activity/courses/{course_id}` |
| `_r_activity_waitlist_final` | `activity.waitlist_final_reminder` | `/activity/courses/{course_id}` |
| `_r_activity_waitlist_expired` | `activity.waitlist_expired` | `/activity/courses/{course_id}` |
| `_r_growth_report` | `growth_report.published` | `/parent/growth-reports/{report_id}` |

容錯：

- `render()` 找不到對應 renderer 回 `Rendered(title=f"({event_type})", body="", deep_link=None)`（不拋例外，但通知中心顯示「`(event_type)`」原值）
- renderer 內部炸例外時，`render()` catch 後回 `Rendered(title="(渲染失敗)", body=f"event_type={event_type}", deep_link=None)`，log row 仍會寫入

### Channel Adapters（`_channels/line.py` + `_channels/ws.py`）

兩個 adapter 為 dispatch 內部 helper；caller 應避免直接呼叫，一律走 `dispatch.enqueue` 或 `dispatch.send_to_line_user_sync`。

#### LINE Channel（`_channels/line.py`）

| Symbol | 簽章 | 用途 |
|--------|------|------|
| `LineAdapter(line_service)` | class | dispatch 用的 LINE channel adapter；包裝 `LineService` singleton |
| `LineAdapter.send(evt, rendered, *, log_id)` | method | 依 `evt.event_type` 查 `LINE_HANDLERS`，找到走專屬 handler；找不到 fallback 走 `push_text_to_user` 純文字 |
| `LINE_HANDLERS: dict[str, Callable]` | dict | event_type → `_h_*` handler 函式（共 23 筆，覆蓋全部 event）|
| `_h_*` handler 函式 | `(ls, evt, rendered) -> None \| bool` | 每 event_type 一個專屬訊息構建邏輯；皆呼叫 `line_service._push_to_user` / `_push_to_user_with_quick_reply` / `push_text_to_group` |
| `_parse_date(value)` / `_parse_datetime(value)` | helper | context 內欄位可能是 isoformat str 或 date/datetime，統一解析 |

特殊 handler 行為：

- `_h_parent_message_received` — 帶 quick-reply postback（`thread_id` 給定時，附 LINE postback button `data=f"thread_id={thread_id}"`）
- `_h_activity_waitlist_final_reminder` — 計算 `hours_left = max(1, int((deadline - now) // 3600))`；deadline 缺值 fallback 為 6 hr
- `_h_dismissal_created` — 群組推送專用；`group_id = evt.line_group_id or ls._target_id`；兩者皆空時 `logger.warning` 略過
- `_h_growth_report_published` — 回 `bool`（push API 結果），讓 `send_to_line_user_sync` 拿真實 ACK；支援 caller 傳 `context["custom_message"]` 覆蓋預設模板

#### WS Channel（`_channels/ws.py`）

| Symbol | 簽章 | 用途 |
|--------|------|------|
| `WsAdapter()` | class | 非 inbox 的 WS 推送 channel（`parent.*` / `dismissal.created`） |
| `WsAdapter.send(evt, rendered, *, log_id)` | method | 透過 `asyncio.run_coroutine_threadsafe` 把 coroutine 投回主 loop，timeout 2 秒 |
| `WsAdapter._dispatch(evt, rendered, log_id)` | async method | 依 event_type 路由：`parent.*` → `broadcast_parent`；`dismissal.created` → `dismissal_manager.broadcast(classroom_id, ...)`；其他 raise `RuntimeError` |
| `_inbox_ws_push(evt, rendered, log_id)` | function | 員工 inbox WS 同步 wrapper；給 `dispatch._fan_out` 在 in_app 路徑直接呼叫（不經 `WsAdapter`） |
| `_inbox_ws_push_async(evt, rendered, log_id)` | async function | 內部呼叫 `api.inbox_ws.inbox_broadcast_user(recipient_user_id, payload)` |
| `_build_payload(evt, rendered, log_id)` | helper | 組 WS payload `{event_type, title, body, deep_link, log_id}` |
| `_WS_TIMEOUT_SECONDS = 2.0` | const | WS coroutine future wait timeout |

職責劃分：員工 inbox WS 失敗只 `logger.warning`，不算 `channels_failed`（語意上 inbox 補位即可，realtime 推送失敗可接受）；`WsAdapter.send` 失敗會記入 `channels_failed`。

兩個推送點皆透過 `utils.event_loop.get_main_loop()` 拿主 event loop（`main.py` 啟動時 `set_main_loop(asyncio.get_running_loop())`），避免在 threadpool worker 內起新 loop 打死 WS transport（B1-B4 round 4 bug 注記）。

---

## DTO Definitions

### `PendingEvent` dataclass（`dispatch.py`）

```python
@dataclass(frozen=True)
class PendingEvent:
    event_type: str
    recipient_user_id: Optional[int]
    context: dict
    sender_id: Optional[int]
    source_entity_type: Optional[str]
    source_entity_id: Optional[int]
    channels: tuple[Channel, ...]
    line_group_id: Optional[str] = None
```

存於 `session.info[_QUEUE_KEY]: list[PendingEvent]`；commit 後 `_drain_after_commit` 取出 fan-out。

### `Rendered` dataclass（`renderers.py`）

```python
@dataclass(frozen=True)
class Rendered:
    title: str
    body: str
    deep_link: str | None
```

由 renderer 函式回傳；寫入 `NotificationLog.title` / `body` / `deep_link`。

### `notification_logs` 表（`models/notification_log.py`）

| 欄位 | 型別 | 約束 / 說明 |
|------|------|-------------|
| `id` | `BigInteger` | PK autoincrement |
| `recipient_user_id` | `Integer` | FK `users.id` ON DELETE CASCADE，NOT NULL |
| `event_type` | `String(60)` | NOT NULL |
| `sender_id` | `Integer` | FK `users.id` ON DELETE SET NULL，nullable |
| `title` | `String(120)` | NOT NULL（renderer 預渲染） |
| `body` | `Text` | NOT NULL |
| `payload_json` | `JSON` | NOT NULL default `dict`；結構化 context（前端深用 avatar / status chip） |
| `source_entity_type` | `String(40)` | nullable，反查源頭用 |
| `source_entity_id` | `Integer` | nullable |
| `deep_link` | `String(255)` | nullable |
| `channels_attempted` | `JSON` | NOT NULL default `list`；matrix + preference gate 後實際嘗試的 channel |
| `channels_succeeded` | `JSON` | NOT NULL default `list`；成功送出（含 in_app 寫 log 本身） |
| `channels_failed` | `JSON` | NOT NULL default `list`；`[{"channel": "line", "error": "..."}, ...]` |
| `read_at` | `DateTime` | nullable（naive Taipei） |
| `created_at` | `DateTime` | NOT NULL，`default=now_taipei_naive` |

索引：

- `ix_notif_log_recipient_unread`：partial `(recipient_user_id, read_at)` WHERE `read_at IS NULL`（未讀列表加速）
- `ix_notif_log_recipient_created`：`(recipient_user_id, created_at)`
- `ix_notif_log_source`：`(source_entity_type, source_entity_id)`

> 單筆 row 代表一個 event 的完整 fan-out 結果（**非每通道一筆**）。家長域沒 `in_app` 的 event 不會寫 log row，但仍會跑 line / ws adapter。

### `notification_preferences` 表（`models/parent_notification.py`）

| 欄位 | 型別 | 約束 / 說明 |
|------|------|-------------|
| `id` | `Integer` | PK autoincrement |
| `user_id` | `Integer` | FK `users.id` ON DELETE CASCADE，NOT NULL |
| `event_type` | `String(40)` | NOT NULL |
| `channel` | `String(10)` | NOT NULL，default `"line"`，server_default `"line"` |
| `enabled` | `Boolean` | NOT NULL，default `True`，server_default `true` |
| `created_at` | `DateTime` | NOT NULL，`default=now_taipei_naive` |
| `updated_at` | `DateTime` | NOT NULL，`default=now_taipei_naive`，`onupdate=now_taipei_naive` |

約束：`UniqueConstraint(user_id, event_type, channel)` 名稱 `uq_notif_pref_triple`；index `ix_parent_notif_pref_user` on `user_id`。

> **稀疏 row 模型**：row 缺 = enabled（預設全開）；row 存在看 `enabled` 欄。新增 event_type 不需資料遷移。`PARENT_NOTIFICATION_CHANNELS = ("line",)` — v1 只支援 LINE channel。

別名：`NotificationPreference = ParentNotificationPreference`（Phase 2 過渡相容）。

---

## Business Rules

### 1. 唯一入口契約

- caller（`api/` 與 `services/` 全部模組）**必須** 走 `dispatch.enqueue` 或 `dispatch.send_to_line_user_sync`
- **禁止** 直接呼叫 `line_service.notify_*` / `line_service._notify_*` / `line_service.push_to_user`
- 由 `notification-dispatch-gate` CI job 強制（`.github/workflows/ci.yml` line 391-437）：
  - Regex 1：`_?line_(service|svc)\._?notify_`
  - Regex 2：`_?line_service\.push_to_user|_?line_svc\.push_to_user`
- 白名單（允許出現上述 pattern 的路徑）：
  - `services/line_service.py`（method 自己定義在此）
  - `services/notification/_channels/`（dispatch LINE adapter）

### 2. after_commit / after_rollback 同步契約

- `enqueue()` 只寫 `session.info[_QUEUE_KEY]`，**不立即** 發送
- `session.commit()` 觸發 `_drain_after_commit` → pop queue 逐筆 `_fan_out`
- `session.rollback()` 觸發 `_clear_on_rollback` → pop queue 丟棄（業務 tx 失敗則通知不發）
- session 必須來自 `models.base.get_session_factory()`，parent_db / spike_rls 等其他 factory 不受監聽
- `install_session_hooks()` idempotent；測試 fixture swap factory 後須再呼叫一次以綁到 test factory

### 3. Channel 失敗隔離

- `_fan_out` 內任一 channel 失敗只記 `channels_failed` + `logger.exception`，**絕不 re-raise**
- 一筆 fan-out 失敗不能影響後續事件（`_drain_after_commit` 內外層 try/except 也吞例外）
- 推送無 retry / DLQ 機制 [needs review — 設計規格可能有提；此程式碼層級未見 retry queue]

### 4. Preference Gate

- `in_app` channel **不檢查** preference，matrix 有就一定寫 log
- `line` / `ws` channel 過 `_pref_enabled` gate：
  - 無 `recipient_user_id`（群組推播）視為 enabled
  - DB 查詢 `notification_preferences` 缺 row 視為 enabled（稀疏模型）
  - row 存在看 `enabled` 欄
  - DB 異常 fail-closed（回 False；沿用 `should_push_to_parent` 慣例）

### 5. LINE Channel 路由

LINE adapter（`LineAdapter.send`）對 LINE channel 三類路由：

- **註冊 handler**：`event_type` 在 `LINE_HANDLERS` 內 → 走專屬 `_h_*` handler（含 Flex / quick-reply 等互動 UI）
- **未註冊 handler**：fallback 走 `line_service.push_text_to_user(recipient_user_id, title + "\n" + body)` 純文字
- **群組 mode**（`evt.line_group_id is not None`）：跳過 `_resolve_line_user_id` 個人解析；handler 內走 `push_text_to_group`；當 `line_group_id` 與 `line_service._target_id` 都空時略過並 warning

LINE 個人推送前置：`_fan_out` 先呼叫 `_resolve_line_user_id` 把 `User.id` 換成 `User.line_user_id`：

- `user.is_active` 必為真
- `user.line_user_id` 必非空
- `user.line_follow_confirmed_at` 必非空（家長 LINE Login 後綁定確認）
- 任一不符 → `channels_failed.append({"channel": "line", "error": "unreachable_user"})`
- DB 異常 fail-closed

### 6. WS Channel 路由

- `WsAdapter` 只處理 `parent.*`（`broadcast_parent(user_id, payload)`）與 `dismissal.created`（`dismissal_manager.broadcast(classroom_id, payload)`）
- 員工 inbox WS（`api.inbox_ws.inbox_broadcast_user`）由 `_inbox_ws_push` 直接呼叫，**不經** `WsAdapter`
- 員工 inbox WS 失敗只 `logger.warning`，不算 channel failure（in_app log 已落庫即達持久通知；realtime push 失敗可降級）
- WS coroutine 透過 `asyncio.run_coroutine_threadsafe` 投回 `utils.event_loop.get_main_loop()` 主 loop；timeout 2 秒
- `dismissal.created` WS 推送要求 `context["classroom_id"]` 必存在，否則 raise `ValueError`

### 7. in_app log 寫入規則

- 只有當 `"in_app" in evt.channels` 才寫 `NotificationLog` row（家長域沒 in_app 就不寫 log，但仍跑 line / ws）
- log row 寫入後 `log_id` 傳給 line / ws adapter（供未來 push receipt 追蹤；v1 未用）
- log row 寫入後 `succeeded` / `failed` 結果 append 到 `channels_succeeded` / `channels_failed`（list 累加，**非** 覆蓋）

### 8. `send_to_line_user_sync` 特殊契約

- 同步 API，回 `bool`（True 若 LINE API 200）
- **不寫** `NotificationLog`、**不過** preference gate（caller 已自管 `line_sent_at` idempotency）
- 用於 admin explicit action（如 admin 觸發 growth report 推送），caller 需 sync 拿 `sent_count` 決定是否回滾 claim
- `line_user_id` 空 → return False
- 走 `LINE_HANDLERS[event_type]` handler；未註冊則 fallback 走 `LineAdapter.send` 純文字
- handler 回 `None` 視為成功；handler 回 `bool` 走實值（目前只 `_h_growth_report_published` 回 bool）

### 9. Event 新增 SOP

新增 event_type 必須四處同步：

1. `event_types.py` 加 Python 常數 + 加入 `NOTIFICATION_EVENT_TYPES` frozenset
2. `channel_matrix.py` 加 `CHANNEL_MATRIX` entry（無則 dispatch 略過不發）
3. `renderers.py` 加 `@renderer(event_type)` 函式（無則 fallback 顯示 `(event_type)`，醜但不炸）
4. （家長端可關）caller 控 `notification_preferences` row
5. （需 LINE 互動 UI）`_channels/line.py` 加 `_h_*` handler + 註冊到 `LINE_HANDLERS`

### 10. 設定不一致守則

- `event_type` 不在 `NOTIFICATION_EVENT_TYPES` → `enqueue` raise `ValueError`
- `event_type` 在 `NOTIFICATION_EVENT_TYPES` 但不在 `CHANNEL_MATRIX` → `enqueue` log debug 後 return（不發送）
- `event_type` 在 matrix 但無 renderer → `_fan_out` 不炸，但 log row `title=f"({event_type})"`
- renderer 內部例外 → log row `title="(渲染失敗)"`，發送繼續

### Changelog

| Version | Date | Changes |
|---------|------|---------|
| v0.1 | 2026-05-28 | Initial draft；覆蓋 Phase 4 retirement 後的 dispatch 統一入口、23 個 event_type、4 個 public API、CI gate enforcement |

# Design: 行事曆多檢視 + 拖拉改期（Phase B / FullCalendar）

**Date:** 2026-05-19
**Scope:** ivy-frontend（CalendarView 重寫）+ ivy-backend（拖拉 PATCH 端點驗證）
**Predecessor:** [`2026-05-19-calendar-admin-feed-design.md`](2026-05-19-calendar-admin-feed-design.md)（Phase A — 提供 admin_feed 資料源）
**Target files:**
- `ivy-frontend/package.json`（add `@fullcalendar/core@^6` + `@fullcalendar/vue3` + `@fullcalendar/daygrid` + `@fullcalendar/timegrid` + `@fullcalendar/list` + `@fullcalendar/interaction`）
- `ivy-frontend/src/views/CalendarView.vue`（重寫 — 從自建月曆 grid 換成 FullCalendar 實例）
- `ivy-frontend/src/composables/useCalendarLayers.ts`（modify — `filteredItems` 轉成 FullCalendar `EventSourceInput`）
- `ivy-frontend/src/components/calendar/CalendarToolbar.vue`（新檔 — 6 chip toggle 從 CalendarView 抽出，沿用 Phase A 邏輯）
- `ivy-backend/api/events.py`（modify — 既有 PATCH 端點驗證 drag-rescheduled date 仍合理）

## 動機

Phase A 月曆只有單一月檢視。**Admin 想看「下週」要心算**月份；想改期要打開 dialog、改日期、儲存（4 動作）。FullCalendar 提供月/週/日/列表 4 view + 拖拉直接改期（1 動作），大幅縮短常見操作。

Phase A 的跨模組 layer toggle 已就位、admin_feed 已提供統一 envelope — 本 phase **只動 UI 框架、零後端契約變化**（除了 drag 觸發既有 PATCH /api/events/{id}）。

## Goals

1. CalendarView 換成 FullCalendar v6（vue3 wrapper）4 view：`dayGridMonth` / `timeGridWeek` / `timeGridDay` / `listWeek`
2. 4 view toolbar 切換按鈕（toolbar 由 FullCalendar header 內建提供）
3. **拖拉改期僅限 `event` layer**（admin 持有 CALENDAR bit）— drop callback 呼叫既有 `PATCH /api/events/{event_id}` 改 `event_date` / `end_date`
4. 其他 layer item read-only（`editable: false`）— drop 嘗試會被 FullCalendar 直接禁止
5. 保留 Phase A 6 chip layer toggle，從 CalendarView 抽到 `CalendarToolbar.vue` 元件
6. Bundle size：FullCalendar 6 + 4 plugin ≤ 200 KB gzip（測量驗收）

## Non-Goals

- 從拖拉直接編輯標題/內容 — 拖拉只改日期
- 跨月拖拉動畫（FullCalendar 內建即可，不另客製）
- 取代 EventDetailDialog — 點事件仍開既有 dialog
- 替換家長端 `parent/views/CalendarView.vue` — 家長端是週行程聚合不適合 FullCalendar
- 整合 Phase C 的 recurrence_rule（若 Phase C 已 merged，下面有相容章節；若沒，本 spec 無 dependency）

## 套件選型

| 候選 | License | Bundle gzip | 結論 |
|---|---|---|---|
| **FullCalendar v6 + vue3 wrapper** | MIT (core + 4 view + interaction plugin) | ~180 KB | ✅ 採用 |
| v-calendar / vue-cal | MIT | smaller | view 不齊全（無 list / 弱 drag）— pass |
| 自建 | — | 0 | week/day view + drag 自寫工 ~1.5 週 — pass |
| FullCalendar Premium (時間表 / 資源 view) | 商用授權 | n/a | 用不到 |

## 拖拉行為

### 前端

FullCalendar `eventDrop` callback：

```ts
async function onEventDrop(info: EventDropArg) {
  const item = info.event.extendedProps as CalendarFeedItem
  if (item.layer !== 'event') {
    info.revert()
    ElMessage.warning('此項目不能拖拉改期')
    return
  }
  // 解析 occurrence id（Phase C 後格式 `{pk}@{date}`）
  const eventId = String(item.id).split('@')[0]
  try {
    await updateEvent(eventId, {
      event_date: formatISODate(info.event.start),
      end_date: info.event.end ? formatISODate(info.event.end) : null,
    })
    ElMessage.success('已更新事件日期')
    refreshFeed()  // 重撈當前 view window 的 admin_feed
  } catch (e) {
    info.revert()
    ElMessage.error('更新失敗，已還原')
  }
}
```

### 後端

`api/events.py` 既有 `PATCH /api/events/{id}` 已可改 `event_date` / `end_date`，不需新端點。本 spec **僅補測試** 驗證 drag-rescheduled 資料正確落地。

**Phase C 相容**：若 Phase C 已落地、drag 一個重複事件 → 後端應拒絕 422「重複事件請從編輯 dialog 改規則」，避免「拖一次只改一筆」的 UX 陷阱。若 Phase C 未落地，所有事件都可拖。

## View 互動矩陣

| View | 顯示 | 拖拉 | 點擊 |
|---|---|---|---|
| `dayGridMonth` | 一月 7×N grid，all-day events | ✅ event layer 跨日拖 | 開 EventDetailDialog |
| `timeGridWeek` | 一週 7 列 × 24 小時，含 start_time/end_time | ✅ event layer 改時段 | 同上 |
| `timeGridDay` | 一天 24 小時 | ✅ 同 week | 同上 |
| `listWeek` | 一週列表 | ❌（list view FC 預設不支援） | 同上 |

### Time grid 與 all_day 衝突

Phase A 的 admin_feed 一律送 `all_day: true`。timeGridWeek/Day 要呈現精確時段需後端送 `all_day: false` + `start_time` / `end_time`。

**本 spec 折衷**：
- `event` layer：若 `SchoolEvent.start_time` 不為 null，改送 `all_day: false` + 組合 ISO datetime
- 其他 layer（leave/activity/...）：維持 `all_day: true`（time grid 中顯示為 top all-day 帶）

需修改 Phase A 的 `_fetch_event`：

```python
if r.start_time and r.end_time:
    start_dt = datetime.combine(r.event_date, time.fromisoformat(r.start_time))
    end_dt = datetime.combine(r.end_date or r.event_date, time.fromisoformat(r.end_time))
    out.append(CalendarFeedItem(
        ..., start=start_dt.isoformat(), end=end_dt.isoformat(), all_day=False,
    ))
else:
    # 原 all-day 邏輯
```

但 `CalendarFeedItem.start/end` 目前是 `date` 型別。需擴成 `date | datetime` (序列化為 ISO 8601)。前端 type 也要對齊（string 已可吃兩種格式）。

## Phase A → Phase B Migration

需動到 Phase A 的部分（不是純 additive）：

1. `schemas/calendar_admin.py.CalendarFeedItem.start/end` 改成 `date | datetime`（pydantic v2 union 即可）
2. `_fetch_event` 加 start_time/end_time 判斷
3. CalendarView 整支重寫（自建 grid → FullCalendar）— 舊 cell-renderer / cell-events / cell-other-layers 全部刪
4. `useCalendarLayers.ts` 加 `toFullCalendarEvents()` helper，把 `filteredItems` 轉成 FC 的 `EventInput[]`

**回退策略**：因為是 UI 重寫不是 additive，回退需要 git revert 整個 commit。建議分 2 commit：
- commit A：後端 datetime support + 測試
- commit B：前端 FullCalendar 整合（可獨立 revert 而後端保留）

## 測試

### Backend（補在 `tests/test_calendar_admin.py`）

| 測試 | 目的 |
|---|---|
| `test_event_with_start_time_returns_datetime` | start_time 有值時 `all_day: false` + ISO datetime |
| `test_event_without_start_time_remains_date` | 沒 start_time 維持 all-day |
| `test_drag_patch_updates_event_date` | PATCH endpoint 接 drag 改的 date 落地（既有測試強化） |
| `test_drag_patch_rejects_recurring_event` | 若 Phase C 已落地、recurring event PATCH 直接改 event_date → 422 |

### Frontend

- `CalendarView` UI 改動量大，**E2E 用 Playwright** 跑 3 個 happy path：切月、切 timeGridWeek、拖一個 event 改期
- `useCalendarLayers.test.ts` 加 `toFullCalendarEvents` 單元測試（純函式好測）
- `CalendarToolbar.test.ts` 沿用 Phase A 6 chip 測試

### Bundle size 驗收

```
npm run build && du -sh dist/assets/*.js | sort -h | tail -10
```

主要 chunk（含 FC）應 < 200KB gzip。超過則啟用 dynamic import 拆 chunk：

```ts
const FullCalendar = defineAsyncComponent(() => import('@fullcalendar/vue3'))
```

## 後端契約變化（影響 Phase A）

| 欄位 | 原型別 | 新型別 |
|---|---|---|
| `CalendarFeedItem.start` | `date` (str `YYYY-MM-DD`) | `date \| datetime` (str ISO 8601) |
| `CalendarFeedItem.end` | 同 | 同 |
| `CalendarFeedItem.all_day` | 一律 true | true / false |

Phase A 的測試需更新；其他 layer 不變（仍送 date + all_day=true）。

## Locale / Timezone

- FullCalendar locale `@fullcalendar/core/locales/zh-tw` — 月/週名稱中文
- Timezone：後端 `utils/taipei_time` 已統一台灣時區；FullCalendar 用瀏覽器 timezone 即可（admin 都在台灣）

## Rollout

- Phase A 已 merged main，本案在新 branch 從 main 起
- 後端 commit 先（datetime support + 測試）→ 前端 commit 後（FullCalendar 整合）
- 兩 repo 各自 PR

## Out of Scope（v2 候選）

- 多 calendar source（個人事件 vs 學校事件 vs 假日）的 FC 多 calendar feature
- 拖拉多選（FC Premium）
- export to ics
- print view 客製

## Open Decisions

無 — 待 user review。

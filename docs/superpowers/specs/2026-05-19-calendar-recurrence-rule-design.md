# Design: 行事曆重複事件規則（Phase C / recurrence_rule）

**Date:** 2026-05-19
**Scope:** ivy-backend（schema + Alembic + 共享 expander）+ ivy-frontend（事件編輯 dialog）
**Predecessor:** [`2026-05-19-calendar-admin-feed-design.md`](2026-05-19-calendar-admin-feed-design.md)（Phase A）
**Target files:**
- `ivy-backend/alembic/versions/<id>_school_events_recurrence_rule.py`（新檔，加 nullable JSONB 欄）
- `ivy-backend/models/event.py`（modify `SchoolEvent` 加欄位）
- `ivy-backend/utils/recurrence.py`（新檔，~120 行 — pure function expander + 規則驗證）
- `ivy-backend/api/events.py`（modify create/update — 接 `recurrence_rule` 欄位 + 驗證）
- `ivy-backend/api/calendar_admin.py`（modify `_fetch_event` — query-time expansion）
- `ivy-backend/api/parent_portal/calendar.py`（modify — 同樣 expansion，家長端跟進）
- `ivy-backend/tests/test_recurrence.py`（新檔，~250 行）
- `ivy-frontend/src/components/calendar/RecurrenceEditor.vue`（新檔，~150 行）
- `ivy-frontend/src/views/CalendarView.vue`（modify — event 編輯 dialog 嵌入 `RecurrenceEditor`）

## 動機

每週園務會議、每月演習、學期初家長會這些**目前要手動逐筆建**。一年的週例會 = 50 筆 admin 重複勞動，且編輯時間要逐筆改。

家長端 `parent_portal/calendar.py` 與管理端 `api/calendar_admin.py` 都讀 `SchoolEvent`，兩端都要看到展開後的所有 occurrence。

## Goals

1. `school_events` 加 nullable `recurrence_rule` JSONB 欄，支援 3 種 rule type
2. **Query-time expansion**（不入庫個別 occurrence）— 一筆 source row + rule，由 expander pure function 展開成多個虛擬 occurrence
3. `_fetch_event`（admin_feed）與家長 `calendar.py` 共用同一 expander
4. 前端事件編輯 dialog 嵌「重複設定」section，使用者勾選類型 + 結束日

## Non-Goals

- RFC 5545 全集（VEVENT/RRULE/EXDATE/RDATE/UNTIL/BYWEEKNO/BYYEARDAY…）— YAGNI，admin 用不到
- **Edit-this-only / edit-this-and-future / exception 表** — v1 只支援 **edit-all-only**：改 source row = 所有 occurrence 跟著改；某次取消請手動單獨建 `recurrence_rule=null` 的事件並把原規則 until 提前
- iCal/ics 輸出 — Phase D 候選
- Phase B (FullCalendar 多檢視) 整合 — 不在本 spec；但 Phase B 若在前就一起做、無 hard order

## 資料模型

### `SchoolEvent` 新欄位

```python
recurrence_rule: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="重複規則；null 表單次事件")
```

不加 `recurrence_parent_id`、不加 exception 表 — 因為 edit-all-only 不需要記原 source。

### Rule 型別（JSONB 內容）

三型別、closed schema，validator 用 Pydantic：

```python
# type a — 每週 X
{"type": "weekly", "weekday": 0-6, "until": "YYYY-MM-DD"}
# weekday: 0=Mon..6=Sun（Python isoweekday-1，避免 ISO/JS 約定混亂）

# type b — 每月 N 號
{"type": "monthly_day", "day": 1-31, "until": "YYYY-MM-DD"}
# day 28 以上若該月無此日 → 該月跳過（不退回月底）

# type c — 每月第 N 個星期 X
{"type": "monthly_nth", "nth": 1-5 | -1, "weekday": 0-6, "until": "YYYY-MM-DD"}
# nth: -1 表「最後一個」；1-5 表第 N 個；該月若不存在第 5 個跳過
```

### 業務規則

1. **`until` 必填且 inclusive**；超過 `until` 不產 occurrence
2. **`until - event_date ≤ 730 days`**（2 年上限，runaway 防護）；違反 422
3. **`event_date` 是「規則第一個 occurrence 的日期」**；後續 occurrence 由規則展開
4. **`weekday`-規則：event_date 必須 match 規則**（例：rule `weekly weekday=1` 則 event_date 必為週二）；violation 422
5. **多日事件 + 重複**：若 `end_date IS NOT NULL`，每個 occurrence 的長度 = `end_date - event_date`；展開時保持
6. **取消單次**：admin 在 UI 上「刪除單次」會把該次的日期加入 `until` 前一天（不開 exception 表）

## Expander 介面

`utils/recurrence.py`：

```python
from datetime import date

def expand_event(
    event_date: date,
    end_date: date | None,
    rule: dict | None,
    window_from: date,
    window_to: date,
) -> list[tuple[date, date]]:
    """展開事件成 [(start, end), ...]；rule=None 回 [(event_date, end_date or event_date)] 一筆。

    僅回 window 內的 occurrence；其他略過。
    純函式、無 DB 依賴、易測試。
    """
```

### `_fetch_event` 整合（Phase A）

```python
for r in session.execute(stmt).all():
    for occ_start, occ_end in expand_event(r.event_date, r.end_date, r.recurrence_rule, from_, to):
        out.append(CalendarFeedItem(
            layer="event",
            id=f"{r.id}@{occ_start.isoformat()}" if r.recurrence_rule else r.id,
            # ... 其他欄位同前
            start=occ_start,
            end=occ_end,
        ))
```

**id 格式變化**：單次事件 id 仍為 int PK；重複事件 occurrence id 為 `{event_id}@{date}` 字串。前端 deep-link 用 `eventId={pk}` 部分解析（split `@`）即可。

### Query 變化

`_fetch_event` 的 WHERE clause 要放寬 — 不只 `event_date <= to`，也要把可能在 window 內展開的 source row 拉出：

```python
# 原：
.where(SchoolEvent.event_date <= to)
.where(or_(end_date IS NULL & event_date >= from_, end_date IS NOT NULL & end_date >= from_))

# 改為：
.where(or_(
    # 非重複事件：原邏輯
    SchoolEvent.recurrence_rule.is_(None) & (...原邏輯),
    # 重複事件：source event_date <= to AND rule.until >= from_
    SchoolEvent.recurrence_rule.is_not(None)
        & (SchoolEvent.event_date <= to)
        & (cast(SchoolEvent.recurrence_rule['until'].astext, Date) >= from_),
))
```

## 前端

### `RecurrenceEditor.vue`

嵌在事件編輯 dialog（既有 `EventDetailDialog` 或新的 `EventEditDialog`）：

```vue
<div class="recurrence-section">
  <el-checkbox v-model="enabled">每週/每月重複</el-checkbox>
  <template v-if="enabled">
    <el-radio-group v-model="ruleType">
      <el-radio value="weekly">每週 X</el-radio>
      <el-radio value="monthly_day">每月 N 號</el-radio>
      <el-radio value="monthly_nth">每月第 N 個星期 X</el-radio>
    </el-radio-group>
    <!-- 動態欄位 -->
    <el-select v-if="ruleType === 'weekly'" v-model="weekday">
      <el-option v-for="(label, idx) in WEEKDAYS" :key="idx" :value="idx" :label="label" />
    </el-select>
    <el-input-number v-if="ruleType === 'monthly_day'" v-model="day" :min="1" :max="31" />
    <!-- monthly_nth: nth select + weekday select -->
    <el-date-picker v-model="until" type="date" placeholder="結束日" />
  </template>
</div>
```

`v-model` 一個 `RecurrenceRule | null` 物件給 parent。

### CalendarView occurrence 處理

- Cell 內顯示時，重複事件每個 occurrence 都是獨立 strip — 不需特殊標記（id 已含 `@date`）
- 點 occurrence → 開 dialog，dialog 內 banner：「此事件每週重複，編輯會影響所有日期」+ 取消單次按鈕

## 測試

### Backend（`tests/test_recurrence.py`）

| 測試 | 目的 |
|---|---|
| `test_weekly_expand_4_weeks` | 每週週二 × 4 週 = 4 occurrence |
| `test_monthly_day_15_full_year` | 每月 15 號 × 12 = 12 occurrence |
| `test_monthly_day_31_skips_short_months` | 每月 31 號在 2/4/6/9/11 月跳過 |
| `test_monthly_nth_first_monday` | 每月第一個週一 × 6 月 |
| `test_monthly_nth_last_friday` | nth=-1 表最後一個 |
| `test_until_inclusive` | until 日期本身要產 occurrence |
| `test_window_clipping` | window 在 rule 範圍中段 → 只回中段 occurrence |
| `test_multi_day_recurring` | event_date+end_date 跨日 + weekly rule = 每週都跨日 |
| `test_invalid_weekday_mismatch_rejected` | event_date 是週三但 rule weekday=1 → 422 |
| `test_until_more_than_730_days_rejected` | runaway 防護 |
| `test_recurrence_null_returns_single_occurrence` | 向後相容 |

### Backend integration（補在 `tests/test_calendar_admin.py`）

| 測試 | 目的 |
|---|---|
| `test_admin_feed_expands_weekly_event` | endpoint 確實展開、id 格式 `{pk}@{date}` |
| `test_admin_feed_recurrence_window_outside_source_returns_occurrences` | source `event_date` 在 window 前但 rule 展開的 occurrence 在 window 內 |

### Frontend（`RecurrenceEditor.test.ts`）

至少 5 case：enabled toggle、3 種 type 切換、weekday/day/nth 欄位 emit 正確 payload、until 必填。

## Migration

```python
# alembic/versions/recurr01_school_events_recurrence_rule.py
def upgrade():
    op.add_column(
        "school_events",
        sa.Column("recurrence_rule", postgresql.JSONB, nullable=True, comment="重複規則；null 表單次事件"),
    )
    # 無 default，舊資料保持 null

def downgrade():
    op.drop_column("school_events", "recurrence_rule")
```

無 backfill、無索引（rule 內 until 不適合 BTREE，且本 endpoint 用 cast 拉，量小不需 GIN）。

## 安全 / 效能

- Expander 純函式，window 內最多 ~104 occurrence（每週 × 2 年）≤ 簡單 list
- 90 天 admin_feed window 下，每 source row 最多展開 ~13 occurrence（weekly 90/7）— 整體 item 量級不變
- 不暴露 `recurrence_rule` 給家長端讀者（家長 API 只回 occurrence，不送 rule 細節）

## Rollout / Rollback

- Migration: nullable column，零 downtime
- 後端：modify event router 接 `recurrence_rule` 欄位（可選，舊客戶端傳 null 不影響）
- 前端：`RecurrenceEditor` 為新元件，舊事件 rule=null 不顯示
- Rollback：drop column + revert 後端 expansion logic

## Out of Scope（v2 候選）

- Edit-this-only / exception 表
- weekly 多 weekday（每週一、三、五）— 可用三筆 rule 或加 `weekdays: int[]` 欄位
- 假日跳過（節日不開會）— 需 join Holiday 表，留 v2
- 月最後一週「倒數第 N 天」型別

## Open Decisions

無 — 待 user review。

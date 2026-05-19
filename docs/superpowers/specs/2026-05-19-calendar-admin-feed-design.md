# Design: 管理端行事曆跨模組圖層（admin_feed）

**Date:** 2026-05-19
**Scope:** ivy-backend（新 router）+ ivy-frontend（CalendarView 圖層 toggle）
**Target files:**
- `ivy-backend/api/calendar_admin.py`（新檔，~280 行）
- `ivy-backend/schemas/calendar_admin.py`（新檔，~60 行）
- `ivy-backend/tests/test_calendar_admin.py`（新檔，~320 行）
- `ivy-frontend/src/api/calendar.ts`（既有，加 `getAdminFeed`）
- `ivy-frontend/src/views/CalendarView.vue`（既有，加 layer toggle + 渲染）
- `ivy-frontend/src/composables/useCalendarLayers.ts`（新檔，~120 行）

## 動機

家長端 `api/parent_portal/calendar.py` 已聚合 events / announcements / fees / leaves / medications 成週行程；**管理端 `CalendarView.vue` 只看 SchoolEvent**，admin 想知道「今天誰請假、面試行程、活動課時段、考核截止日」要切 4 個頁面（LeavesView / RecruitmentView / ActivityView / AppraisalCurrentSemesterOverview）。

本案不是 UI 重做，而是**先補資料聚合**，讓現有月曆 grid 一頁看完所有跨模組行程；Phase B（FullCalendar 多檢視）與 Phase C（recurrence_rule）皆吃此 endpoint。

## Goals

1. 單一聚合端點 `GET /api/calendar/admin_feed`，回傳 window 內全部 layer 事件（unified envelope）
2. 後端按 caller 權限濾掉看不到的 layer（不送到前端再藏），避免越權外洩
3. 前端 CalendarView 加 6 個 chip toggle（預設全開），cell 內以小色塊+tooltip 顯示非 event layer
4. 點 layer item 直接 deep-link 到對應頁面（leave → `LeavesView` 帶 filter，activity → `ActivityView` 該筆）

## Non-Goals

- Phase B（多檢視 / 拖拉改期）— 另開 spec
- Phase C（recurrence_rule）— 另開 spec
- `overtime`、`fee_due` 兩 layer（overtime 事後紀錄無前瞻價值；fee_due 是家長視角）
- `interview` layer — `models/recruitment.py` 內 `RecruitmentVisit.visit_date` 是 `String` 非 `Date`，無法穩定按日期過濾；待後續正式建立「面試排程」表後另加
- Server-side cache（query 量小 + 權限維度複雜，cache key 太繁，預估單次 < 100ms 不值得）
- 家長端 `parent_portal/calendar.py` 不動（兩端使用情境不同，強行共用會耦合）

## API 契約

### Request

```
GET /api/calendar/admin_feed?from=2026-05-01&to=2026-05-31&layers=event,holiday,leave,activity,appraisal,meeting
```

| Param | Type | 必填 | 規則 |
|---|---|---|---|
| `from` | `date` (ISO) | ✓ | inclusive |
| `to` | `date` (ISO) | ✓ | inclusive；`(to - from).days` ≤ 90，超過回 422 |
| `layers` | `str` (comma) | ✗ | 預設全部；caller 無權限的 layer 自動剔除（不報錯） |

### Response

```python
class CalendarFeedItem(BaseModel):
    layer: Literal['event','holiday','leave','activity','appraisal','meeting']
    id: int | str            # 來源表 PK；holiday 用 date string 因可能來自 WorkdayOverride；appraisal 用 `{cycle_id}:{milestone}` 區分同 cycle 三日期
    title: str
    start: date              # inclusive
    end: date                # inclusive；單日事件 start == end
    all_day: bool            # 目前所有 layer 一律 True（Phase B timeGrid 才需要 false）
    color: str               # hex `#RRGGBB`，前端 fallback 用，後端固定下發確保跨端一致
    link: str | None         # 前端 router push 目標，None 表純顯示無深連
    meta: dict[str, Any]     # layer-specific 附加欄位（誰請假、課程名、面試者）

class CalendarFeedResponse(BaseModel):
    from_: date = Field(alias='from')
    to: date
    items: list[CalendarFeedItem]
    # 不回 totals / page — 90 天上限保證 items < 千筆量級
```

### Layer 對照表

| layer | 資料源（class @ table） | 日期欄位 | 權限 bit | color | link 模板 | title 規則 |
|---|---|---|---|---|---|---|
| `event` | `SchoolEvent @ school_events` | `event_date` (start) + `end_date` (nullable) | `CALENDAR` (1<<2) | `#10b981` 綠 / 需簽閱類 `#ef4444` 紅 | `/calendar?eventId={id}` | `event.title` |
| `holiday` | `Holiday @ holidays` + `WorkdayOverride @ workday_overrides` | `date` | `CALENDAR` (1<<2) | 假日 `#f59e0b` 橘 / 補班 `#6366f1` 紫 | `null` | `name` |
| `leave` | `LeaveRecord @ leave_records` join `Employee` | `start_date` + `end_date` | `LEAVES_READ` (1<<5) | 核准 `#0ea5e9` 藍 / 待審 `#94a3b8` 灰 | `/leaves?id={id}` | `{employee.name} {leave_type}` |
| `activity` | `ActivitySession @ activity_sessions` join `ActivityCourse` | `session_date` | `ACTIVITY_READ` (1<<27) | `#ec4899` 粉 | `/activity?courseId={course_id}` | `{course.name} 第{session_no}堂` |
| `appraisal` | `AppraisalCycle @ appraisal_cycles` | **三里程碑** `start_date` / `end_date` / `base_score_calc_date` 各產一筆 | `APPRAISAL_READ` (1<<55) | `#dc2626` 暗紅 | `/appraisal?cycleId={id}` | `{cycle.title} {milestone_label}` |
| `meeting` | `MeetingRecord @ meeting_records` | `meeting_date` | `MEETINGS` (1<<7) | `#8b5cf6` 紫 | `/meetings?id={id}` | `meeting_type` 中文 label（無 title 欄位） |

**篩選規則細節**：
- `leave`：`is_approved IS NULL`（pending）或 `IS TRUE`（approved）；`IS FALSE`（rejected）不出
- `event` 含 `end_date`：multi-day 事件 start=event_date / end=end_date or event_date
- `appraisal`：一個 cycle 產 3 筆（三日期皆 NOT NULL）；`milestone_label` 為 `開始` / `結束` / `基準分結算`；cycle.title 由 `{academic_year} 學年度 第 {semester.value} 學期` 拼出
- `meeting`：MeetingRecord 是「每員工出席紀錄」非排程；按 (meeting_date, meeting_type) DISTINCT 聚合一筆代表「那天有開會」

> 顏色清單後端 hardcode 為 `utils/calendar_colors.py` 常數；前端 `useCalendarLayers.ts` 引同份（手動同步，加註 substring 對齊測試）。

## 後端實作

### 路由註冊

`main.py` 加：
```python
from api.calendar_admin import router as calendar_admin_router
app.include_router(calendar_admin_router, prefix='/api/calendar', tags=['calendar-admin'])
```

### Query 策略（防 N+1）

每個 layer fetch 函式簽章一致：

```python
def _fetch_<layer>(
    session: Session, from_: date, to: date, current_user: dict
) -> list[CalendarFeedItem]:
    ...
```

關鍵：
- **單一 SELECT + JOIN**，不 loop 逐筆查關聯（例：`leave` 一次 `JOIN employees` 拿 name；`activity` 一次 `JOIN courses` 拿 title）
- **僅選需要欄位**（不 `SELECT *`，特別是 `leave_records.attachments_json` 不取）
- **WHERE 過濾用 date column index**（確認 `school_events.start_date` / `leave_records.start_date` / `activity_sessions.session_date` 都有 index；缺即列為本案 follow-up migration）
- **權限濾在 fetch 入口**：`if not has_permission(current_user, Permission.LEAVES): return []`，跳過整個 query

### Endpoint 主體

```python
@router.get('/admin_feed', response_model=CalendarFeedResponse)
def get_admin_feed(
    from_: date = Query(..., alias='from'),
    to: date = Query(...),
    layers: str | None = Query(None),
    session: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if (to - from_).days > 90:
        raise HTTPException(422, 'window exceeds 90 days')
    if to < from_:
        raise HTTPException(422, 'to must be >= from')

    requested = set(layers.split(',')) if layers else ALL_LAYERS
    requested &= ALL_LAYERS  # 過濾未知值

    items: list[CalendarFeedItem] = []
    for layer in requested:
        items.extend(LAYER_FETCHERS[layer](session, from_, to, current_user))

    items.sort(key=lambda x: (x.start, x.layer, x.id))
    return CalendarFeedResponse(from_=from_, to=to, items=items)
```

`LAYER_FETCHERS` 為 `dict[str, Callable]`，新增 layer 時加一行即可。

### Audit

不寫 audit log（讀取端點 + 高頻呼叫，audit 體積會爆；列表類 endpoint 本來就不 audit，對齊既有慣例）。

## 前端實作

### `src/api/calendar.ts` 新增

```ts
export interface CalendarFeedItem {
  layer: 'event' | 'holiday' | 'leave' | 'interview' | 'activity' | 'appraisal' | 'meeting'
  id: number | string
  title: string
  start: string  // YYYY-MM-DD
  end: string
  all_day: boolean
  color: string
  link: string | null
  meta: Record<string, unknown>
}

export function getAdminFeed(
  from: string, to: string, layers?: CalendarFeedItem['layer'][]
): Promise<AxiosResp<{ from: string; to: string; items: CalendarFeedItem[] }>>
```

### `useCalendarLayers.ts` composable

封裝：
- `enabledLayers: Ref<Set<Layer>>`（預設全開，localStorage 持久化 key `calendar.enabledLayers`）
- `toggle(layer)` / `enableAll()` / `disableAll()`
- `filteredItems: ComputedRef<CalendarFeedItem[]>`（從原始 items 按 enabledLayers 過濾）
- `groupByDate: ComputedRef<Record<string, CalendarFeedItem[]>>`（cell render 用）

### `CalendarView.vue` 改動

- 月曆 grid 上方加 `<ElCheckboxGroup>` 7 個 chip + 「全選/清除」按鈕
- 月份切換時呼叫 `getAdminFeed(monthStart, monthEnd, [...enabledLayers])`（layer 變動只本地過濾、不重打 API；切月才打）
- Cell 渲染：
  - `event` layer 維持原有大色條（含 title 文字）
  - 其他 layer 收成 4px 高細色條，hover 顯 ElTooltip 含 title + meta
  - 同 cell 超過 4 條時 collapse 成 `+N` chip，點開彈 popover 列全部
- 點任一 item 若 `link` 非 null，`router.push(link)`；null 則開原本 EventDetailDialog（僅 event/meeting 兩 layer 有 dialog）

### 視覺退讓

非 event layer 一律 read-only（不可拖拉、不可在月曆 inline 編輯）。要修改請走 deep-link 到對應頁面。Phase B 才開放拖拉。

## 測試

### 後端（`tests/test_calendar_admin.py`）

| 測試 | 目的 |
|---|---|
| `test_window_over_90_days_returns_422` | 邊界檢查 |
| `test_to_before_from_returns_422` | 邊界檢查 |
| `test_no_layers_param_returns_all_permitted` | 預設全 layer |
| `test_layer_without_permission_omitted_silently` | 安全：無 LEAVES bit 看不到 leave |
| `test_unknown_layer_ignored` | `?layers=foo` 不報錯 |
| `test_event_layer_color_and_link` | layer 對照表正確 |
| `test_holiday_workday_override_merged` | 補班日 vs 國定假日同 layer 不衝突 |
| `test_leave_pending_vs_approved_different_color` | `is_approved` NULL vs TRUE 區分 |
| `test_leave_rejected_excluded` | `is_approved IS FALSE` 不下發 |
| `test_activity_session_joins_course_name` | JOIN ActivityCourse.name |
| `test_appraisal_three_milestones_per_cycle` | start/end/base_score_calc 各一筆，null 跳過 |
| `test_meeting_uses_meeting_type_label` | 無 title 欄位、用 type label |
| `test_items_sorted_by_start_then_layer` | 排序穩定 |
| `test_n_plus_1_query_count_under_threshold` | 用 `sqlalchemy.event` 計 query 數，整體 ≤ 12（6 layer × 平均 2 query） |
| `test_employee_with_leaves_bit_only_sees_leave_layer` | 跨權限矩陣 |

至少 14 case，覆蓋全 layer + 權限 + 邊界。

### 前端（`src/components/__tests__/useCalendarLayers.test.ts`）

至少 6 case：localStorage 持久化、toggle 互斥、filteredItems 同步、enableAll/disableAll、groupByDate 排序、空 items。

### 不做的測試

- E2E 月份切換 → 既有 vitest 已覆蓋月曆 grid，加 unit 測 `useCalendarLayers` 即可
- 後端壓測 → 90 天 window + 7 layer 在 dev 灌入 1 學期假資料手測即可，不上 k6

## 效能

- 預估單次 query 時間：dev 28 員工 + 1 學期資料，預估 < 80ms（7 layer 並非並發、逐次跑；若實測 > 200ms 再評估 `asyncio.gather`）
- Response body 上限估算：90 天 × 假設平均 5 item/day = 450 item，每 item ~200B → ~90KB，可接受
- 前端切月才打 API、layer toggle 純本地過濾，避免「點 chip 就重打」

## 安全 / 權限

- 後端 fetch 入口檢查權限（不是後置 filter）
- `link` 欄位後端產生固定 path，不接受前端傳入的 redirect target
- `meta` 內**不放敏感欄位**（leave 的 reason 文字不下發、salary 數字不下發；只給 admin 月曆視覺需要的最小集合：員工名、假別、課程名）
- Sentry PII denylist 對齊：`meta` 不應觸發 scrubber，但加 `test_meta_does_not_leak_pii` 主動驗

## Rollout / Rollback

- 後端：新檔，加 router include，**無 migration**
- 前端：CalendarView 改動有 feature toggle 嗎？→ **不加**。回退靠 git revert 兩個 commit 即可
- 兩 commit 分離：
  - `feat(calendar): add admin_feed cross-module endpoint`（後端，含 test）
  - `feat(calendar): add layer toggle to CalendarView`（前端，含 test）

## Open Decisions（待 user 補答）

無 — 設計階段已拍板，進 implementation plan 時若遇到 query 細節（例：activity_sessions 表名實際為何）再現場決定。

## 後續（不在本 spec）

- Phase B：FullCalendar v6 多檢視 + 拖拉 → 新 spec
- Phase C：school_events recurrence_rule + expansion → 新 spec
- overtime / fee_due layer 補上 → 若 user 後來想看再加（單 layer 加只動 `LAYER_FETCHERS` 一行 + 顏色表 + 測試）

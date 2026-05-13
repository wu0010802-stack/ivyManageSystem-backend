# 才藝候補名單自動遞補補完 — 設計

- **日期**：2026-05-13
- **範圍**：跨前後端（ivy-backend + ivy-frontend）
- **方案代號**：方案 A（最小補完）
- **作者**：Claude + 業主對齊

---

## 1. 目的

把已開發 60% 的候補名單自動遞補功能補到「可實際營運」，並對家長透明化候補狀態。

## 2. 現況盤點（為什麼是「補完」而非「新做」）

| 既有資產 | 位置 |
|------|------|
| `RegistrationCourse.status` 三態：`enrolled` / `waitlist` / `promoted_pending` | `models/activity.py:204-240` |
| 容量管控：`enrolled + promoted_pending` 佔位（`OCCUPYING_STATUSES`） | `models/activity.py` |
| `ActivityCourse.capacity` | `models/activity.py` |
| 退課自動觸發遞補：`_auto_promote_first_waitlist()` | `services/activity_service.py:992-1067` |
| 家長確認升位：`confirm_waitlist_promotion()` | `services/activity_service.py:689-731` |
| 家長放棄升位（含遞補下一位）：`decline_waitlist_promotion()` | `services/activity_service.py:733-777` |
| 過期掃描函式：`sweep_expired_pending_promotions()` | `services/activity_service.py:779-883` |
| LINE 升位通知模板：`notify_activity_waitlist_promoted/reminder/expired` | `services/line_service.py:490-527` |
| 手動 sweep endpoint：`POST /activity/waitlist/sweep-expired` | `api/activity/registrations.py:680` |
| 升位 API：`POST /activity/registrations/{reg}/courses/{course}/promote` | `api/activity/registrations_items.py` |
| 公開頁查詢 token：`publicQueryByToken` + 確認/放棄 | `api/activity/public.py` |

**主要缺口**：
1. **無定時排程** — `sweep_expired_pending_promotions()` 只能手動呼叫，逾期單會卡住。
2. **無 T-6h 最後提醒** — 既有只有 T-24h 一段（且也是靠 sweep 才會發）。
3. **公開頁不顯示候補位次** — 家長不知道自己排第幾，焦慮感與骚擾客服。
4. **Admin 無一鍵升位 UI** — API 已存在（`promoteWaitlist`），前端候補 Drawer 沒接。

## 3. Scope

### In scope
- 加 in-process scheduler 自動跑 sweep（仿 `salary_snapshot_scheduler.py`）
- 擴 sweep 邏輯：T-24h（既有）+ T-6h（新）雙階段提醒
- 加 `RegistrationCourse.final_reminder_sent_at` 欄位 + LINE 通知模板
- 公開查詢回傳 `waitlist_position` + `waitlist_total`
- Admin 候補 Drawer 加「升位」按鈕（含二次確認 dialog）
- 對應 pytest + Vitest 測試

### Out of scope（明確不做）
- ❌ 引入 APScheduler / Celery / 外部 cron（用 in-house pattern）
- ❌ 改升位順序演算法（仍 FIFO；優先級 = 方案 C）
- ❌ 候補儀表板（升位次數/平均等待時長/放棄率 = 方案 B）
- ❌ 強制 admin 手動升位填理由（FIFO 跳順序是園長正常職權；非金流動作）
- ❌ 移除手動 `POST /waitlist/sweep-expired` endpoint（保留為排程備援）
- ❌ 候補 → enrolled 的繳費流程變動（沿用既有）

## 4. 資料模型變更

### 4.1 `RegistrationCourse` 加欄位

```python
# models/activity.py
final_reminder_sent_at = Column(
    DateTime,
    nullable=True,
    comment="T-6h 最後提醒發送時間（既有 reminder_sent_at 為 T-24h）",
)
```

`reminder_sent_at` 既有 comment 同步更新為 `"T-24h 提醒發送時間"`（已正確）。

### 4.2 Alembic Migration

- 檔名：`alembic/versions/<rev>_add_final_reminder_to_reg_courses.py`
- Upgrade：`ALTER TABLE reg_courses ADD COLUMN final_reminder_sent_at TIMESTAMP NULL`
- Downgrade：`ALTER TABLE reg_courses DROP COLUMN final_reminder_sent_at`
- 對既有資料無影響（新欄位 NULL）

## 5. 後端變更

### 5.1 新檔：`services/activity_waitlist_scheduler.py`

仿 `services/salary_snapshot_scheduler.py` 結構：

```python
ACTIVITY_WAITLIST_SCHEDULER_ENABLED  # env 開關（"1"/"true"/"yes" 啟用）
ACTIVITY_WAITLIST_CHECK_INTERVAL = 300  # 預設 5 分鐘，可 env 覆蓋

def scheduler_enabled() -> bool: ...

def check_and_sweep_once() -> dict:
    """單次 tick：呼叫 sweep_expired_pending_promotions。Idempotent。"""

async def run_activity_waitlist_scheduler(stop_event: asyncio.Event):
    """每 CHECK_INTERVAL_SECONDS 呼一次 sweep；失敗 log 不中斷。"""
```

### 5.2 `main.py` 掛載

仿 `salary_snapshot_scheduler` 在 lifespan startup 區段：

```python
from services import activity_waitlist_scheduler as _wl_sched
if _wl_sched.scheduler_enabled():
    activity_waitlist_stop_event = asyncio.Event()
    activity_waitlist_task = asyncio.create_task(
        _wl_sched.run_activity_waitlist_scheduler(activity_waitlist_stop_event)
    )
```

shutdown 區段：設 stop_event + await task。

### 5.3 擴 `sweep_expired_pending_promotions()`

`services/activity_service.py:779-883` 內部新增 T-6h 提醒邏輯：

```
對每筆 status='promoted_pending' 的 RegistrationCourse（SELECT ... FOR UPDATE SKIP LOCKED）：
  remaining = confirm_deadline - now

  IF remaining <= 0:
      # 過期：既有邏輯
      → 刪除 + 推 LINE 「逾期自動放棄」+ _auto_promote_first_waitlist 遞補下一位
      → 計入 result["expired"]

  ELIF remaining <= 6h AND final_reminder_sent_at IS NULL:
      → 推 LINE T-6h final reminder
      → final_reminder_sent_at = now
      → 計入 result["final_reminded"]（新計數）

  ELIF remaining <= 24h AND reminder_sent_at IS NULL:
      → 推 LINE T-24h reminder（既有）
      → reminder_sent_at = now
      → 計入 result["reminded"]（既有）
```

關鍵設計：
- **LINE 推送失敗時不寫戳記** — 下一輪繼續嘗試。實作上：先 send，成功才更新欄位。
- **`SELECT FOR UPDATE SKIP LOCKED`** — 多 worker 同時跑互不阻塞，跳過正在處理的列。

### 5.4 LINE 通知模板（新）

`services/line_service.py` 新增：

```python
def notify_activity_waitlist_final_reminder(
    session, registration_course, hours_remaining
) -> bool:
    """T-6h 最後提醒。文案範例：
    「⏰ 最後提醒｜OO 才藝候補升位即將於 X 小時後失效，請盡快確認」
    + 確認連結
    回傳 True/False（是否推送成功）。
    """
```

### 5.5 公開查詢回傳位次

`api/activity/public.py` 的 `query-by-token` 端點 response 中，每筆 `registration_courses` 加：

```python
{
    "course_id": ...,
    "course_name": ...,
    "status": "waitlist",
    "waitlist_position": 3,    # 自己也算（1 起）
    "waitlist_total": 8,        # 該課程候補總人數
    # ... 既有欄位
}
```

計算 SQL：
```sql
-- waitlist_position（含自己）
SELECT count(*) FROM reg_courses
WHERE course_id = :cid
  AND status = 'waitlist'
  AND created_at <= :self_created_at;

-- waitlist_total
SELECT count(*) FROM reg_courses
WHERE course_id = :cid
  AND status = 'waitlist';
```

**`promoted_pending` 不計入**（已升位待確認，不在候補佇列）。

**status 不是 `waitlist` 時**：兩欄都回 `null`（不要回 0，避免前端誤判）。

### 5.6 既有手動 sweep router 保留

`POST /activity/waitlist/sweep-expired`（`api/activity/registrations.py:680`）**不動**。
- 排程未啟用時的備援
- 部署事故時 admin 手動觸發
- 既有測試已覆蓋

## 6. 前端變更

### 6.1 `ActivityPublicQueryView.vue`（公開頁）

每筆候補課程顯示位次資訊：

```html
<div v-if="course.status === 'waitlist'" class="waitlist-info">
  <span class="badge badge-waitlist">⏳ 候補中</span>
  <span class="position">
    目前第 <strong>{{ course.waitlist_position }}</strong> 位
    <span class="text-muted">/ 共 {{ course.waitlist_total }} 位</span>
  </span>
</div>
```

文案調整：
- 若 `waitlist_position == 1`：加註「您是下一位候補；如有空位將自動通知」（吸引）
- 若 `waitlist_total == 1`：合併顯示為「您是目前唯一候補者」

### 6.2 `ActivityCourseView.vue`（admin 課程管理）

候補名單 Drawer 每行加按鈕：

```html
<button @click="confirmPromote(reg)" class="btn btn-sm btn-primary">
  ⬆️ 升位
</button>
```

點擊行為：
1. 顯示確認 dialog：「將跳過順序，立即升此候補為待確認狀態（48 小時內請家長確認）。系統會自動推送 LINE 通知。確定？」
2. 確認後呼叫既有 `promoteWaitlist(regId, courseId)`
3. 成功後 toast「已升位」+ 刷新 Drawer
4. 失敗（例如該家長已被前一個升位）顯示錯誤

**新增前端 API 函式**：無（`promoteWaitlist` 已存在於 `src/api/activity.js`）。

### 6.3 樣式

沿用既有 token 與 brand（IvyKids 深綠 + 緞帶等）。新增 class：
- `.waitlist-info`
- `.waitlist-position`
- 既有 `.btn-primary` 用 `ActivityCourseView.vue` 自帶風格

## 7. 環境變數

| 變數 | 預設 | 說明 |
|------|------|------|
| `ACTIVITY_WAITLIST_SCHEDULER_ENABLED` | （空，停用） | 設 `1` 啟用 in-process scheduler |
| `ACTIVITY_WAITLIST_CHECK_INTERVAL` | `300`（秒） | 掃描間隔 |

寫入 `.env.example`（後端）+ `ivy-backend/CLAUDE.md` 的 env 章節。

## 8. 邊界與失敗處理

| 情境 | 處理 |
|------|------|
| 多 worker 同時跑 sweep | `SELECT ... FOR UPDATE SKIP LOCKED` 在 promoted_pending 查詢 |
| LINE 推送失敗 | `line_service` 既有 try/except；scheduler tick 不中斷；reminder 戳記只在成功推送後寫入 |
| 候補佇列空時退課 | `_auto_promote_first_waitlist` 既有：什麼都不做，名額空著 |
| 同家長同學生重複報名 | 既有 unique constraint（`student_id + course_id`）防護 |
| 公開查詢 `waitlist_position` 計算時 status 非 waitlist | 兩欄回 `null` |
| 跨日結退款守衛 | 退款流程不變；自動升位純內部狀態流轉，不過 `_require_daily_close_unlocked` |
| Scheduler 未啟用 | 系統可正常運作；過期單需 admin 手動 sweep；新增 startup log 提示 |
| Admin 手動升位時前一位剛被自動升 | 既有 `promoteWaitlist` 應回 409 / 已升位錯誤；前端展示錯誤 |
| 公開頁 `waitlist_total` 為 0 但自己 status=waitlist | 不可能（自己計入）；若 race 出現視為 1 |
| Migration 失敗回滾 | downgrade 已定義；舊 code 不依賴 `final_reminder_sent_at` |

## 9. 稽核

- Scheduler 觸發的 sweep：log info（`activity waitlist scheduler tick: expired=X reminded=Y final_reminded=Z`）；資料層變更（刪除過期 RC、狀態變更）由 AuditMiddleware + RegistrationChange 既有兩層記錄。
- Admin 手動升位：AuditMiddleware 自動記錄（既有），operator + entity_id 完備。

## 10. 測試計畫

### 10.1 後端（pytest）

**新檔**：`tests/test_activity_waitlist_scheduler.py`
- `test_scheduler_disabled_by_default`
- `test_check_and_sweep_once_idempotent`（連跑兩次第二次 expired=0）
- `test_scheduler_skips_when_no_pending`（候補佇列空時不報錯）
- `test_scheduler_tick_continues_on_line_failure`（mock LINE 推送失敗，下一輪重試）

**擴 `tests/test_activity_waitlist_promotion.py`**：
- `test_final_reminder_sent_at_t_minus_6h`（剩 5h59m 時發送，戳記寫入）
- `test_final_reminder_not_resent`（已發過第二次不重發）
- `test_t24_and_t6_reminder_independent`（兩戳記獨立）
- `test_line_failure_does_not_write_reminder_stamp`

**擴 `tests/test_activity_public.py`**（如不存在則新建）：
- `test_query_by_token_returns_waitlist_position`
- `test_waitlist_position_excludes_promoted_pending`
- `test_waitlist_position_null_for_enrolled`
- `test_waitlist_total_matches_queue_size`

### 10.2 前端（Vitest）

**新檔**：`src/views/public/__tests__/ActivityPublicQueryView.waitlist.test.js`
- 候補課程顯示位次（mocked api response）
- `waitlist_position == 1` 顯示「下一位」提示
- `waitlist_total == 1` 顯示「唯一候補者」

**擴 `src/views/activity/__tests__/ActivityCourseView.test.js`**：
- 點擊「升位」按鈕觸發確認 dialog
- 確認後呼叫 `promoteWaitlist` 並刷新 Drawer
- API 失敗時顯示錯誤訊息

## 11. 部署 Checklist

1. 後端 migration：`alembic upgrade heads`
2. 後端 .env 加：
   ```
   ACTIVITY_WAITLIST_SCHEDULER_ENABLED=1
   # ACTIVITY_WAITLIST_CHECK_INTERVAL=300  # 預設值，可省
   ```
3. 後端重啟（startup log 應出現 `activity waitlist scheduler started (interval=300s)`）
4. 驗證：人工建一筆 promoted_pending 把 confirm_deadline 設為過去 → 等 ≤ 5 分鐘 → 應自動轉為過期 + 推送 + 遞補
5. 前端部署：候補家長查詢頁應看到位次
6. Admin 課程管理候補 Drawer 應看到「升位」按鈕
7. 監控 24 小時：log 中無 `scheduler tick failed` 異常

## 12. 回滾計畫

| 元件 | 回滾方式 |
|------|---------|
| Scheduler | env `ACTIVITY_WAITLIST_SCHEDULER_ENABLED=0` 重啟 |
| Migration | `alembic downgrade -1` |
| 前端 | 還原前一個 build |
| 後端 code | revert commit |

舊 sweep router 始終可用，回滾後仍可手動觸發。

## 13. 風險與緩解

| 風險 | 影響 | 緩解 |
|------|------|------|
| Scheduler 在多 worker 部署下重複推 LINE | 家長一次收到多通 | `SELECT FOR UPDATE SKIP LOCKED` + 戳記檢查 |
| `final_reminder_sent_at` migration 大表卡住 | 上線阻塞 | 加欄位（nullable, no default）為 metadata-only ALTER，PostgreSQL 秒級完成；reg_courses 預期 < 10 萬列 |
| LINE 服務在排程觸發瞬間故障 | 部分提醒漏發 | 戳記不寫入 → 下輪重發；user 透過手動 sweep router 強制重試 |
| 公開頁位次計算 N+1 | response 慢 | 一次 query 兩個 count 即可；課程通常 < 20 筆候補 |
| Admin 跳順序升位被濫用 | FIFO 公平性受損 | AuditMiddleware 留軌跡，可事後追查 |

## 14. 預估工時

| 項目 | 時間 |
|------|------|
| Migration + model | 0.5 hr |
| Scheduler 服務 + main.py 掛載 | 1.5 hr |
| 擴 sweep 邏輯（T-6h） | 1 hr |
| LINE 通知模板 | 0.5 hr |
| 公開查詢加 position/total | 1 hr |
| Admin Drawer 升位按鈕 | 1 hr |
| 後端測試（新+擴） | 2 hr |
| 前端測試 | 1.5 hr |
| 整合驗證 + commit 分割 | 1 hr |
| **合計** | **約 10 hr / 1.5 天** |

## 15. 後續方案（不本案範疇）

- **方案 B**：候補儀表板（升位次數、平均等待時長、放棄率、課程候補健康度 badge）
- **方案 C**：升位順序政策化（在校生 > 兄姊在園 > 一般家長；或時段優先級）
- **方案 D**：候補家長端可主動「放棄候補」按鈕（目前只能透過 publicDeclinePromotion 在升位時放棄）

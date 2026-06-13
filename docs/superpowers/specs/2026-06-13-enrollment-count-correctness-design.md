# 在籍人數正確性三層方案（節慶/超額獎金人數來源）

日期：2026-06-13
狀態：已與使用者確認三層全做

## 問題

節慶獎金、超額獎金的計算以「班級在籍人數 ÷ 目標人數」為核心，但學生進進出出，
計算時的人數有四個錯誤來源：

1. 🔴 **退學生永遠在籍（潛伏 bug）**：`services/student_lifecycle.transition()` 對
   轉出/退學只設 `withdrawal_date` 不設 `graduation_date`，而薪資在籍判斷
   `services/student_enrollment.student_active_on_filter` 只看
   `enrollment_date/graduation_date`——退學生持續灌水班級人數。
   （dev DB 現況 0 筆受影響，屬流程一啟用即錯的潛伏雷；年終側
   `year_end/enrollment_rates._enrolled_on_filter` 有看 withdrawal_date，兩側不一致）
2. 🟠 **歷史重算套「現在的班」**：`classroom_student_count_map` 按學生現態
   `classroom_id` 分組；轉班後重算歷史月份，學生被數進新班。系統已有
   `StudentClassroomTransfer` 歷史表與 `gov_moe/monthly_calculator.classroom_at_month_end`
   反查，薪資側未接。
3. 🟠 **學生異動不標薪資過期**：lifecycle 轉移、轉班、入退學日期補登都不會
   `needs_recalc`（考勤/請假/設定異動都會），結薪精靈不會警示。
4. 🟡 **月底單日快照語意**：月中退算 0、月底進算整月，對進出敏感（對齊業主
   Excel 慣例，屬設計而非 bug）。

## 三層解法

### L1 修正確性洞（數字語意不變）

- **L1a**：`student_active_on_filter` 補 `withdrawal_date IS NULL OR withdrawal_date > 基準日`
  （對齊 year_end `_enrolled_on_filter` 的嚴格性）。
- **L1b**：`classroom_student_count_map` 改為轉班歷史感知——bulk 撈
  `StudentClassroomTransfer.transferred_at <= 基準日` 每生最新一筆覆寫現態
  `classroom_id`（語意同 `classroom_at_month_end`，但一次 query 全體，避免 N+1）。
- **L1c**：新 helper `mark_salary_stale_for_enrollment_event(session, event_date)`：
  事件月所屬發放月（含）之後的**發放月**（2/6/9/12）未封存 SalaryRecord 全標
  `needs_recalc=True`（人數只影響發放月的節慶/超額；全員受影響故不分員工）。
  掛載點：① `student_lifecycle.transition()` ② `api/students.py` /
  `api/classrooms.py` 寫入 `StudentClassroomTransfer` 處 ③ `api/students.py`
  學生編輯端點異動 `enrollment_date/graduation_date/withdrawal_date/classroom_id` 時。

### L2 月度在籍人數快照（制度性解法）

新表 `class_enrollment_snapshots`（migration `enrsnap01`，down=actuq01）：

| 欄位 | 型別 | 說明 |
|---|---|---|
| snapshot_year / snapshot_month | int | 對象月份 |
| classroom_id | int NULL | NULL=全校總數列 |
| student_count | Numeric(6,1) | 支援按日加權小數 |
| count_mode | varchar(20) | month_end / daily_weighted / manual |
| is_confirmed / confirmed_by / confirmed_at | | HR 確認狀態 |
| generated_at / updated_by / adjust_reason | | 產生與手調軌跡 |

唯一性：(year, month, classroom_id) partial unique ×2（classroom NULL 列另一條）。

新 service `services/salary/enrollment_snapshot.py`：
- `compute_live_counts(session, year, month, mode)` → `{"school": float, "classes": {cid: float}}`
  （含 L1a/L1b 的 withdrawal＋轉班歷史感知；mode 見 L3）
- `generate_snapshot(...)` upsert＋回傳與前版 diff；已確認列預設不覆寫（force 例外）
- `resolve_bonus_counts(session, year, month)` → 有快照讀快照、無快照 fallback
  `compute_live_counts`（空表零漂移，既有測試不變）

**引擎接點**（獎金人數的所有來源站點改走 `resolve_bonus_counts`）：
- `engine.calculate_festival_bonus_breakdown` 的 school_active／classroom_count_map fallback
- bulk 預載 `_build_period_monthly_context`
- `api/salary/festival.py` period-accrual 端點的 monthly_ctx_cache
- `services/finance/salary_field_breakdown.py`（snapshot 主月＋`_build_festival_period_rows`）

API（`api/salary/enrollment_snapshot.py`，掛 salary router）：
- `GET  /salaries/enrollment-snapshot?year&month` 列出（含涵蓋月展開與確認狀態）
- `POST /salaries/enrollment-snapshot/generate` body {year, month}（產生/重產，回 diff）
- `PATCH /salaries/enrollment-snapshot/{id}` 手調人數（需 reason ≥10 字）→ 標 stale
- `POST /salaries/enrollment-snapshot/confirm` body {year, month}
權限：讀 SALARY_READ、寫 SALARY_WRITE；手調寫 audit。
編輯/重產若值變動 → `mark_salary_stale_for_enrollment_event`。

### L3 按日加權模式（語意切換，預設關閉）

- `BonusConfig.enrollment_count_mode` varchar(20) server_default `'month_end'`
  （migration `enrmode01`，down=enrsnap01）；PUT /config/bonus schema 加欄位。
- `daily_weighted`：在籍人數 = Σ(每日在籍生數) ÷ 當月日曆天數，保留 1 位小數。
  班級歸屬按日分段（轉班日起算新班；用 transfer 歷史建每生月內區段）。
- 只影響 `compute_live_counts` 與快照產生；引擎公式（基數×人數÷目標）不變，
  ratio 接受小數人數。
- 前端獎金設定面板加模式 select；切換需金流簽核權限（沿用 PUT /bonus 既有閘）。

## 前端（ivy-frontend）

- `SalarySettleView` Step1（StepPrecheck）加「班級人數快照」面板：
  顯示結算月（發放月時展開涵蓋各月）各班人數、模式、確認狀態；
  按鈕：產生/重新產生、行內編輯（填原因）、確認本期。
- `BonusConfigPanel` 加人數模式 select。
- `src/api/salary.ts` 加 4 個 wrapper；`npm run gen:api` 同步型別。

## 風險與緩解

- **零漂移保證**：無快照＋mode=month_end（預設）時 `resolve_bonus_counts` 與既有
  即時計算逐位元相同；全部既有測試必須不改而綠（L1a/L1b 除外——它們修的就是錯值，
  受影響測試需逐一檢視是否原本就斷言了錯誤行為）。
- **L1a 影響面**：所有用 `student_active_on_filter` 的站點同步變嚴格（屬修正）；
  以 grep 全站點逐一檢視。
- **快照與封存**：已封存月份的快照禁止編輯/重產（同 finalize 語意）。
- **效能**：快照命中時發放月 4 個月人數 0 次學生表掃描；未命中 fallback 與現狀同。

## 測試

- L1a：退學/轉出學生不再計入（薪資側）；NULL withdrawal 不變。
- L1b：轉班後重算歷史月，人數歸當時班；無轉班紀錄 fallback 現態。
- L1c：lifecycle/轉班/編輯日期 → 對應發放月未封存 record needs_recalc；封存不動。
- L2：快照 CRUD、resolve 優先序、空表 fallback 零漂移、手調標 stale、引擎讀快照算獎金。
- L3：daily_weighted 數值（含月中入退/轉班分段）、模式切換經 PUT /bonus 新版本生效。

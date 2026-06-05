# 期間感知設定解析（Period-Aware Salary Config Resolver）設計

> 日期：2026-06-05
> 範圍：ivy-backend（薪資引擎 + 年終 builder + 設定查詢層）
> 起因：ERP 設計體檢 A3（已結算可解封重算缺期間鎖）+ A4（法定常數硬編碼、歷史重算套錯費率）
> 決策：範圍 B（修正 + 補齊一致化）／方案 1（年度欄位解析）／缺設定 fail-loud

---

## 1. 背景與問題（為何做）

設定層的「資料模型」其實**已大致年度化**——`BonusConfig.config_year`、`InsuranceRate.rate_year`、
`InsuranceBracket.effective_year`、`GradeTarget.config_year` 都有年度欄位，`AttendancePolicy` 有
`effective_date`。`insurance_service.py:781 load_brackets_from_db(year)` 也已示範了正確的「依所屬年度載入」模式。

**真正的缺陷在查詢層，不在資料模型**。薪資引擎有兩條 config 載入路徑，且都沒用年度欄位當解析 key：

| 路徑 | 站點 | 現況查詢 | 問題 |
|---|---|---|---|
| 當期計算 | `engine.py:734` InsuranceRate | `filter(is_active==True).order_by(id.desc())` | 忽略 `rate_year`，永遠抓最新 |
| 當期計算 | `engine.py:768` BonusConfig | `filter(is_active==True).order_by(id.desc())` | 忽略 `config_year` |
| 當期計算 | `engine.py:752` AttendancePolicy | `filter(is_active==True)` | 忽略 `effective_date` |
| 當期計算 | `engine.py:96` PositionSalaryConfig | `order_by(id.desc())` | **連年度欄位都沒有** |
| 歷史重算 | `engine.py:478 _select_active_at` | `created_at <= 當月最後一日, id.desc()` | 用「設定列建立時間」當生效依據，**不是**年度欄位 |
| 年終 | `settlement_builder.py:78` 等 4 處 | `BonusConfig.id.desc()` | 忽略結算年度 |

**隱性 bug 範例**：2026 年 6 月為預備明年新建一筆 `config_year=2027` 的 BonusConfig（created_at=2026-06）。
重算 **2026 年 7 月**薪資時，`_select_active_at(2026, 7)` 撿 `created_at <= 2026-07-31` 裡 id 最大的
→ 撿到那筆 2027 設定 → **把明年的獎金額度套到今年的薪資**。`config_year` 欄位本可擋掉，但查詢忽略了它。

**附帶安全面**（`engine.py:486-499` 註解自承）：用 `is_active`/`id desc` 撿，admin 可塞惡意金額且
`is_active=False` 的列被歷史補算撿到；緩解所依賴的 `finance_approve` 守衛「待補」。本設計改用
「年度 + version」解析後會**縮小**此攻擊面（惡意列需 match 目標年度才會被撿），但完整守衛屬範圍 C，
本設計不含（列為 follow-up）。

---

## 2. 目標與非目標

**目標**
- 歷史月份重算永遠使用「該月所屬年度」的設定（修掉拿錯年度設定的 bug）。
- 新年度上線 = 在 DB 新增一列，**不必改 code 重新部署**（法遵）。
- 把散落的當期/歷史/年終查詢收斂成**單一** period-aware resolver。
- 缺該年度設定時 **fail-loud**：擋下結算、要求行政先補設定，薪資絕不靜默套到錯的費率。

**非目標（YAGNI）**
- 不做 `effective_from/effective_to` 日期區間（台灣法定幾乎 1/1 生效；年中只有訂正，用 version tiebreaker 即可）。
- 不做通用 key-value effective config 單表（會丟掉現有型別化欄位結構）。
- 不做 config 寫入端 `finance_approve + audit` 守衛（範圍 C，另立）。
- 不做 A3 的「期間關帳唯讀」DB trigger（本設計只解「歷史重算正確性」這半；關帳唯讀另立）。

---

## 3. 核心元件：`resolve_config`

新增模組 `services/salary/config_resolver.py`。

### 3.1 解析演算法（fail-loud）

```
resolve_config(session, model, year, *, year_col, version_col="version") -> row
  1. rows = session.query(model)
            .filter(getattr(model, year_col) == year)
            .order_by(model.<version_col>.desc(), model.id.desc())
            .first()
  2. row 命中 → 回傳（同年多筆 = 取最高 version，即「該年度最新訂正版」）
  3. row 為 None → raise PayrollConfigMissingError(config_type=model.__name__, year=year)
```

- **tiebreaker**：`version` 為主、`id` 為輔。代表「該年度目前最佳已知值」。
- **無前一年 fallback、無 hardcode fallback**（fail-loud 決策）。
- `PayrollConfigMissingError` 定義在 `services/salary/config_resolver.py`（或既有 errors 模組），
  攜帶 `config_type` 與 `year`，供 API 層轉成可讀訊息。

### 3.2 語意改變（刻意，需明示）

廢除 `_select_active_at` 的 `created_at <= 當月最後一日` 邏輯，改為「年度 + 最高 version」。

理由：年中對舊設定的**訂正**應**回溯套用整年**。例：1 月費率打錯、6 月修正 → 1~12 月重算都該用修正值。
舊的 created_at-cutoff 會把早月凍結在「後來才知道是錯的」值上，較不正確。

→ 既有測試 `test_swap_uses_version_active_at_month_end` 前提改變，需**重寫**為新語意。

### 3.3 模型 → 年度欄位對照（resolver 呼叫端用）

| Model | year_col | 備註 |
|---|---|---|
| `InsuranceRate` | `rate_year`（西元，已確認） | |
| `BonusConfig` | `config_year`（**西元**，已確認：`api/config/bonus.py:243` 寫死 2026） | 月薪路徑用結算月所屬西元年；年終路徑見 §6 R2 |
| `AttendancePolicy` | 新增 `config_year`（見 §4，採 (b)） | |
| `PositionSalaryConfig` | `config_year`（**本設計新增**） | |
| `InsuranceBracket` | `effective_year`（西元） | resolver 回「該年所有級距列」而非單列（見 §5.2） |
| `GradeTarget` | **透過 `bonus_config_id` FK 隨已解析的 BonusConfig 一併載入**（見 §6 R3） | 不獨立用 `config_year` 解析，避免雙來源不一致 |

---

## 4. Schema 變更（極小，一支 migration）

- **`PositionSalaryConfig` 加 `config_year INTEGER NOT NULL`**（唯一缺年度欄位的設定表）。
  - index `(config_year, version)`。
  - migration backfill：既有列設為「當前年度」（見 §7 上線）。
- **`AttendancePolicy`**：已有 `effective_date`。兩種做法二選一（實作時定）：
  - (a) resolver 從 `effective_date` 萃取年份比對；或
  - (b) 比照其他表新增 `config_year` 欄位（一致性較佳，migration 略大）。
  - **預設採 (b)** 以統一 resolver 介面；若 (a) 更省則於 plan 階段定案。
- 其餘表（`BonusConfig`/`InsuranceRate`/`InsuranceBracket`/`GradeTarget`）**不動結構**。
- migration 須：單 head、`alembic-roundtrip` up/down 對稱（過 CI gate）、`alembic-symmetry-lint` 通過。

---

## 5. 查詢站點收斂（修 bug 的本體）

### 5.1 統一兩條路徑
當期計算路徑（`_load_config_from_db_locked`，`engine.py:730` 附近）與歷史重算路徑
（`_apply_configs_for_month`，`engine.py:581`）**都改走 `resolve_config(…, year)`**。
當期計算的 year = 正在結算的 (year, month) 之 year。兩條路徑從此共用同一解析語意。

| 站點 | 改為 |
|---|---|
| `engine.py:96` PositionSalaryConfig | `resolve_config(session, PositionSalaryConfig, year, year_col="config_year")` |
| `engine.py:734` InsuranceRate | `resolve_config(session, InsuranceRate, year, year_col="rate_year")` |
| `engine.py:752` AttendancePolicy | `resolve_config(session, AttendancePolicy, year, …)` |
| `engine.py:768` BonusConfig | `resolve_config(session, BonusConfig, year, year_col="config_year")` |
| `engine.py:478 _select_active_at` | **刪除**（被 resolver 取代） |
| `engine.py:581 _apply_configs_for_month` | 內部改用 resolver |
| 年終 `settlement_builder.py:78`（含 `festival_base_for_role`）/ `after_class_award.py:92` / `semester_dividend.py:135` / `attendance_deductions.py:103` | `resolve_config(…, 結算年度)`；**但年度 key 映射須先過 §6 R2 業主確認**——確認前此 4 處維持現行 `id.desc()` 並標 TODO，月薪路徑先落地（兩者解耦） |

### 5.2 `load_brackets_from_db` 對齊
`insurance_service.py:781` 現行邏輯：`effective_year==year` → 無則 fallback 最近前一年 → 無則 hardcode。
改為 fail-loud：**`effective_year==year` 無列即 raise `PayrollConfigMissingError`**（移除前一年 fallback 與 hardcode fallback）。
brackets 一年一組（多列），resolver 對 brackets 回「該年所有列」，故 brackets 用獨立薄包裝
（`resolve_brackets(session, year)`）而非通用單列 `resolve_config`。

> 注意：移除 hardcode fallback 後，`INSURANCE_TABLE_2026` 常數失去 runtime 角色。
> 可保留為「seed migration 的資料來源」或測試 fixture，但**引擎不再讀它**。

---

## 6. 風險與待定（實作階段定案）

- **R1 — gold recon 位移風險（最高）**：任何把「latest active」改成「年度解析」的站點，都可能讓現有
  薪資 gold 測試數字位移。緩解：migration backfill 確保「當前年度」列 = 原 latest-active 值，使現況數字不動；
  gold recon 必須在改 code 前後都跑、零位移才算過。
- **R2 — 年終學年（民國）→ BonusConfig 西元 config_year 映射【✅ 已實作 2026-06-05】**：
  業主確認映射為 **`config_year = academic_year + 1911`（民國曆年）**——學年 114 → 西元 2025、學年 115 → 西元 2026。
  此與民國→西元換算（民國 N 年 = 西元 N+1911）、年終既有 proration（`settlement_builder` proration_start
  `academic_year+1911`）與考勤期間（`attendance_deductions._period_bounds` `academic_year+1911`）**同一民國曆年基準**。
  > ⚠ 歷史修正：本 spec 最初誤建議 `N+1912`（作者算術錯誤，且「次年初發放」理由與 +1912 也對不上）；
  > 已於落地時更正為 **+1911** 並經業主確認。
  實作：新增 `settlement_builder.bonus_config_for_academic_year(db, academic_year)`（config_year=N+1911、
  空表→None 沿用內建預設、有料缺年度→fail-loud）；`festival_base_for_role` 加 `academic_year=` 參數
  （None=latest 舊行為供測試）；4 站點改用之。commit `e6e9c06`。
- **R3 — GradeTarget 解析方式（已定案）**：`GradeTarget` 同時有 `config_year` 與 `bonus_config_id` FK。
  **採 FK 路線**：載入「已解析 BonusConfig」的 `grade_targets` relationship（`BonusConfig.grade_targets` backref），
  確保 GradeTarget 與所選 BonusConfig 版本一致，不另用 `config_year` 解析（避免雙來源漂移）。
  實作前確認現行 engine 載 GradeTarget 的站點並一併改為走此關聯。
- **R4 — fail-loud 打到 dev/seed 環境**：dev DB 或 seed 流程若缺當年度設定列會被擋。
  緩解：seed 腳本與 migration backfill 都要產生當年度列；測試 fixture 比照。
- **R5 — `needs_recalc` 互動**：改設定仍須觸發 `needs_recalc`（維持現行 mark_stale 行為），確保歷史月份會被重算到新值。

---

## 7. 上線與部署安全

- **migration backfill**：
  - `PositionSalaryConfig`：加欄 + 既有列 `config_year = 當前年度`。
  - 其餘表：掃描是否每個「有薪資資料的年度」都有對應設定列；缺則由現有 latest-active 複製補當年度列
    （確保 fail-loud 上線當天不誤擋進行中的年度與可重算的歷史年度）。
- **prod 現況**：後端服務在 Zeabur 目前 SUSPENDED，migration 待 resume 才會跑（依 workspace CLAUDE.md §收尾紀律）。
- **alembic**：單 head、roundtrip 對稱、CI gate 全綠。
- 合併前 `gen:api` 不需要（無 response schema 變動；若 API 錯誤碼/訊息新增屬 4xx body 則確認前端處理）。

---

## 8. 測試計畫（TDD：先紅後綠）

**resolver 單元（`tests/test_config_resolver.py`）**
- 命中該年度 → 回該列。
- 同年多 version → 回最高 version。
- 缺該年度 → raise `PayrollConfigMissingError`（攜帶 config_type + year）。

**頭號回歸（修 bug 證據）**
- DB 同時存在 `config_year=2026` 與 `config_year=2027` 的 BonusConfig，重算 2026/7 →
  **必須拿到 2026 列**（現行程式會錯拿 2027）。InsuranceRate 同型測試。

**gold recon 不位移**
- 既有薪資 gold 測試（含義華 Excel 對帳）在 backfill 後數字**完全不變**。

**語意改寫**
- 重寫 `test_swap_uses_version_active_at_month_end` 為「年度 + 最高 version」語意。

**fail-loud 行為**
- 單筆 calc 缺年度設定 → API 回 422 + 可讀訊息。
- bulk calc 缺年度設定 → 在 per-employee savepoint 迴圈**之前**整批中止（非逐人失敗），回明確訊息。

**年終**
- 年終 builder 依結算年度解析 BonusConfig 的測試（涵蓋 R2 的年度 key 正確性）。

**時區 matrix**：沿用 CI 既有 Asia/Taipei × UTC matrix。

---

## 9. 元件邊界小結

| 元件 | 做什麼 | 怎麼用 | 依賴 |
|---|---|---|---|
| `config_resolver.resolve_config` | 給 (model, year) 回該年度最新版設定列；缺則 raise | 引擎/年終以年度呼叫 | SQLAlchemy session、各 config model 的年度欄位 |
| `config_resolver.resolve_brackets` | 給 year 回該年所有級距列；缺則 raise | `insurance_service` | InsuranceBracket |
| `PayrollConfigMissingError` | fail-loud 訊號 | API 層接住轉 422；bulk 預檢中止 | — |
| migration（PositionSalaryConfig 加欄 + backfill） | 補當年度列確保不誤擋 | 一次性 | alembic |

---

## 10. Follow-ups（本設計不含，明列）

- 範圍 C：config 寫入端（`api/config.py`/`api/insurance.py`）`finance_approve + audit` 守衛。
- A3 後半：期間關帳唯讀（CLOSED 期間禁止 SalaryRecord UPDATE，DB trigger 或 service 守衛）。
- 觀測：缺設定告警（fail-loud 觸發時推 LINE 給行政）。

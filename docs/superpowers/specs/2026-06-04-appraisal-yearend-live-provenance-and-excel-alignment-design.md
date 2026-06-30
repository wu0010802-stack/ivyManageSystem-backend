# 考核 / 年終：即時顯示 + provenance 下鑽 + 考核對齊 Excel — 設計

- 日期：2026-06-04
- 狀態：design draft（已與 user brainstorm 定案核心決策，待 user 審 spec → writing-plans）
- 範圍：跨前後端（ivy-backend FastAPI + ivy-frontend Vue 3）
- 相關：
  - 金標準 Excel：`義華校資料/薪資福利/114年年終經營績效1150213.xls`（年終）、`114上考核表1150315xls.xls`（考核）
  - 既有 spec：`2026-06-01-year-end-bonus-e-automation-phase1-design.md`、`2026-06-02-year-end-bonus-e-automation-phase2-design.md`
  - 對照筆記：workspace `.scratch/excel-vs-system-recon-2026-06-03.md`、`.scratch/year-end-phase2-excel-recon-2026-06-02.md`

---

## 1. 背景與問題

園內考核（半年一期）與年終分紅獎金（年度）原本全在 Excel 手算。專案已把**年終獎金 E化引擎**（`services/year_end/`，6 步引擎 + 6 表 + phase2 auto_derive）與**考核管理**（`services/appraisal/`，calibrate + 簽核）落地，且大多數計算輸入其實已能從其他模組（招生 / 考勤 / 請假 / 才藝報名 / 獎懲 / 班級人數）即時推導。

但目前有兩個落差：

1. **前端仍以「手填欄 / 寫死預設」呈現**（例：年終 Config 招生目標寫死 160、達成比率寫死 83.6；考核基準分手填），沒有把後端已經算好的即時值與其來源攤出來。使用者要的是「UI 根據其他功能的資料即時顯示」。
2. **考核計算邏輯在 calibrate 重構時偏離了業主 Excel**（舊生註冊率語意被改成留校率、帶班人數改定額、特教生加分被拿掉、獎金基礎額分組與金額不符、bonus_rate 生效日造成 114 學年 silent-0）。業主要求**以 Excel 為準對齊回去**。

本設計同時處理這兩件互相耦合的事。

### 對照重點（詳見 `.scratch/excel-vs-system-recon-2026-06-03.md`）
- **年終**：本體公式 `(基本薪+節慶基數)×平均績效% → ×機構達成% → +扣項 → ×到職比例` 與 9 類特別獎金，phase2 已對金標準逐人吻合（節慶差額、定額罰則、舊生率÷編制、學期紅利、鼓勵才藝皆已對齊）。**殘餘缺口**：教課教師獎勵金(H)、超額獎金(I)、研習/自強缺席仍手填；扣款期間（曆年 vs 學年）待確認；兩個特例（李麗珍實領/12、未簽約固定 600）未建模。
- **考核**：差異集中區，見 §4 規則對齊清單。

---

## 2. 目標與非目標

### 目標
1. 後端建一套可重用的 **derivation provenance 服務層**：每個自動值統一回傳 `{值, 算式 breakdown, 逐筆來源紀錄, 跳轉連結}`（深度3 透明度，考核與年終共用）。
2. 前端在**現有頁面**（年終 grid/config、考核當期總覽）上，把手填/寫死欄改成「即時自動值 + 來源徽章 + 右側抽屜下鑽」。
3. 考核計算邏輯**對齊業主 Excel**，並補逐人回歸測試對 `114上考核表`。
4. 簽核改為純流程狀態（不凍結金額）；**實付證據改由轉帳名冊匯出當下的不可變快照承接**。

### 非目標（YAGNI）
- 不重做統一儀表板（取向 B 已排除）；只增強現有頁面（取向 A）。
- 不重寫現有 `auto_derive` / 考核引擎；只**加一層 provenance adapter** 擴充輸出（取向 C）。
- P4（教課老師授課歸屬自動化、研習/自強逐員工出席模組）列為可選，預設不在本批次。

---

## 3. 核心設計決策（brainstorm 定案）

| # | 決策 | 選擇 | 理由 / 後果 |
|---|------|------|------|
| D1 | 即時值 vs 簽核流程 | **B：grid 永遠即時** | 金額即使簽核後也反映最新底層資料；簽核變純流程狀態。**推翻現有 finalized-drift 凍結護欄**。 |
| D2 | 實付證據定錨 | **轉帳名冊匯出存 timestamped 快照** | grid 永遠即時，但每次匯出轉帳名冊凍結一份不可變快照當實付證據（補償 D1）。 |
| D3 | provenance 透明度 | **深度 3：逐筆原始紀錄 + 跳轉來源模組** | 最透明，員工爭議時最有說服力；每個自動項都要做明細 API。 |
| D4 | 整體架構取向 | **A + C 混合** | 後端統一 provenance 服務層（C）+ 前端增強現有頁面（A）。 |
| D5 | 考核邏輯方向 | **以 Excel 為準對齊回去** | 見 §4。 |
| D6 | 前端下鑽 UX | **右側抽屜（el-drawer）** | 寬表 + 長明細最合適，不動表格。 |

---

## 4. 考核對齊 Excel — 規則變更清單（P1）

> 誠實邊界：以下是**結構對照**。標 ⚠ 的確切級距/門檻值需在 P1 做一次「逐人對 `114上考核表` 的 reconciliation pass」並經業主確認才定死。

| Excel 欄/規則 | 現行系統 | 對齊方向 | 風險 |
|---|---|---|---|
| **M 舊生註冊率**（100%→+6 / 95→0 / 90→−1.7 / 80→−3 / else −6） | `RETURNING_RATE_0315` tiers **已同結構** | 驗證數值即可 | ✓ 低 |
| **J/K 休學人數**（9/15、3/15 兩欄） | 被改成單一「班級留校率」 | 回 Excel 休學人數公式 `(全園休學×2 + 試讀休學×1 − 回園×1) / 全園班級數`，分兩個基準日 | ⚠ 試讀/回園無 lifecycle 狀態 |
| **N 帶班人數** | `CLASS_HEADCOUNT_BONUS` 手填×(+2) | 回 Excel「編制以上加／以下扣」 | ⚠ 確切級距待推導 |
| **P 特教生 +分** | **無對應項** | 新增 SPED 加分項 | ⚠ 加分規則 + 特教生名單來源待確認 |
| **獎金基礎額分組** | 5 組、金額不符（ASSISTANT 4500/3000、STAFF 5000/3500、COOK 3500/2500） | 回 Excel **3 組**：園長主任 8000/5000、教師·行政·全職才藝·廚師·司機 6000/4000、助理 5500/3500 | 改 `AppraisalBonusRate` seed/設定 |
| **bonus_rate 生效日** | seed `effective_from=2026-08-01` → 114 學年查無 rate → 獎金靜默 0 | 補 114 學年 rate（或調生效日） | 解 silent-0 bug |
| **請假扣分** | `LEAVE` 每天 −1 從第一天 | 確認是否有「事假3天/病假6天」免扣門檻 | ⚠ 確認 |
| **到職未滿** | proration 有定義但 recompute 未呼叫 | 對齊 Excel「未滿一年/未簽約不計考核」 | ⚠ 跳過 vs 折算 |

### 業主待確認清單（P1 實作前定）
1. J/K 休學人數：完整公式 vs `on_leave` 近似（**預設近似**：試讀/回園當 0、公式註明簡化）
2. 帶班人數級距（編制±N 加減幾分）
3. 特教生加分規則 + 名單來源
4. 請假「事假3天/病假6天」免扣門檻是否存在
5. 到職未滿：跳過 vs 折算
6. 獎金基礎額 3 組金額（STAFF/COOK 是否真的跟教師同 6000/4000）
7. 教課獎勵 / 超額獎金 是否排 P4 自動化

> 註：`models/appraisal.py` 已有承接「原始資料（如休學人數、註冊率小數）」的欄位，可承接休學人數原始值回溯。

---

## 5. 後端：統一 provenance 服務層（P0，取向 C）

### 5.1 核心介面 `DerivedValue`

```
DerivedValue {
  key             # 例 'attendance_late_deduction'
  value           # 算出的數字
  formula_summary # 可讀摘要「5 次遲到 × −50/次 · 114.02–115.01」
  breakdown       # 結構化組成 {count, unit, period, ...}
  source_records  # 逐筆原始紀錄 [{date, label, amount, module, source_id}]
  deep_link       # 跳轉來源模組路由+filter
  is_override     # 是否被手動覆寫
  override_meta   # {原自動值, 覆寫者, 時間, 原因}
}
```

正確性保證（測試）：`Σ source_records.amount == value`。

### 5.2 結構
- 既有 `services/year_end/auto_derive/*`、`services/appraisal/{status_aggregator, rule_applier}` 已在算「值」→ **加 provenance adapter**，使其同時產出 `breakdown + source_records`，不重寫核心。
- 新增 `services/provenance/`：統一介面 + 各 domain provider（attendance / enrollment / activity / disciplinary / meeting）。
- **batch provider**：一次查全 cycle 全員（沿用既有 bulk 模式如 `status_aggregator`），避免 grid 每格即時查的 N+1。
- 明細 API：`GET /{module}/derivation/{key}?employee_id=&cycle_id=` → 回 `DerivedValue`。

### 5.3 資料流（年終為例）
```
其他模組原始資料（考勤/招生/才藝/獎懲）
   │ 即時查（batch）
   ▼
provenance provider ──► DerivedValue{值, breakdown, source_records, deep_link}
   │
   ├─► 年終 grid：顯示值 + 右側抽屜下鑽   ← D1 永遠即時
   └─► build-settlements：persist settlement（供列表/簽核狀態/明細條 PDF）

匯出轉帳名冊 ──► 寫入新表 payout_roster_snapshot（immutable 實付證據）
```

### 5.4 簽核流程（D1 之後）
- settlement 仍 persist（為列表 / 簽核狀態 / PDF），但**金額顯示以即時 DerivedValue 為準**。
- 簽核狀態（會計簽 / 老闆核定）= 純流程 flag，**不 gate 金額**。
- **改寫現有 finalized-drift 凍結護欄**（不再阻止已簽 settlement 被重算覆寫）。
- 新表 `payout_roster_snapshot(cycle_id, exported_at, exported_by, rows[employee_id, amount, account, name])`，append-only / immutable。

### 5.5 手動覆寫（深度3 一致）
- 沿用既有 `source_ref="auto:*"` 隔離機制（auto 不覆寫 manual）。
- 覆寫**必填原因**（money audit），記 `override_meta`。
- 抽屜顯示「原自動值 X → 手動 Y ＋ 覆寫者/時間/原因」。

### 5.6 錯誤處理
1. 查無紀錄 → `value=0`、`formula_summary="無紀錄"`，不報錯。
2. 來源 API 失敗 → 該格顯示「來源暫時無法載入 + retry」，不阻斷整表。
3. 費率/rate 查無（114 silent-0 那類）→ 明確 warning「查無對應費率」，**絕不靜默 0**。

---

## 6. 前端：即時總表 + 右側抽屜（P2/P3，取向 A）

### 6.1 總表呈現（增強現有 `YearEndGridView` / 考核 `CurrentSemesterOverview`）
- 自動算的格子：值 + **來源徽章（藍點）**，可點下鑽。
- 手填格：**黃底 ✎**（只剩真正無資料源者：超額獎金、研習/自強缺席）。
- 狀態徽章保留（草稿/會計已簽/已核定），但依 D1 **不凍結金額**。

### 6.2 下鑽（D6 右側抽屜）
- 點自動格 → `el-drawer` 從右滑出，顯示 `DerivedValue`：算式摘要 → 逐筆 source_records → 「在 X 模組查看 →」deep-link。
- 抽屜元件**考核與年終共用**（吃 `DerivedValue` 介面）。
- 測試：teleport stub（`global: { stubs: { teleport: true } }`）。

### 6.3 Config 頁即時化（`YearEndConfigView`）
- 招生目標、達成比率、各班舊生率等寫死/手填欄 → 改顯示即時自動值（接 recruitment intake / enrollment_rates），保留手動覆寫。

### 6.4 轉帳名冊匯出
- 匯出當下寫入 `payout_roster_snapshot`；若偵測到底層資料已和上次快照不一致 → 提示「資料已變動，建議重匯名冊」（此提示僅在匯名冊觸發，不凍結 grid）。

---

## 7. 分期

| 階段 | 內容 | 端 | 可獨立上線 |
|---|---|---|---|
| **P0** | provenance 服務層 + `DerivedValue` 介面 + 明細 API + batch provider | BE | ✓ |
| **P1** | 考核規則對齊 Excel（§4）+ 逐人回歸測試 + 解 silent-0 | BE | ✓ |
| **P2** | 年終 grid/config 即時值 + 右側抽屜 + `payout_roster_snapshot` | BE+FE | ✓ |
| **P3** | 考核當期總覽即時值 + 右側抽屜 | BE+FE | ✓ |
| **P4（可選）** | 教課老師授課歸屬（自動化教課獎勵金）、研習/自強逐員工出席模組 | BE+FE | ✓ |

---

## 8. 測試策略
- **P1 考核對齊**：regression-first——先補「能重現 `114上考核表` 數字」的逐人測試**再改**；純函式測（休學人數公式、帶班人數、舊生率 tiers、`基礎額×分數%`、114 rate）。
- **P0 provenance**：每個 provider 測 `Σ source_records.amount == value`。
- **P2 年終**：維持金標準逐人測試綠（不回歸）+ `payout_roster_snapshot` immutability 測試。
- **前端**：抽屜元件（teleport stub）、即時值渲染、覆寫必填原因。

---

## 9. 風險
1. **D1 推翻凍結護欄** → 補償＝`payout_roster_snapshot`；殘留風險：簽核後未重匯名冊就轉帳 → §6.4 匯名冊時漂移提示。
2. **即時查 N+1** → batch provider 強制。
3. **考核資料缺口**（試讀/回園無狀態）→ 近似先上、公式註明。
4. **教課老師 / 研習自強** → P4 才補，P1–P3 維持手填。
5. **跨前後端同步**（CLAUDE.md）：新 endpoint/schema 改動需 `dump_openapi` + `gen:api`；新 PII 欄位（如 source_records 帶姓名）檢查 Sentry denylist 兩端對齊。
6. **Alembic**：新表 `payout_roster_snapshot` migration 需 downgrade 完整、合併前手動 `alembic upgrade heads`。

---

## 10. 開放問題（已收斂為 §4 業主待確認清單，spec 審查後在 writing-plans 前定）
見 §4 七項。其餘設計決策已於 brainstorm 定案（§3）。

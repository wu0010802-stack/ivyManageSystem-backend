# 薪資「額外加給」手填欄位設計（值週／活動加班費對齊）

- 日期：2026-06-04
- 範圍：前後端（FastAPI + Vue 3）
- 起因：對照園所實際薪資表（`義華薪資115.03/115.05sx.xlsx`），「薪資表」C 欄是一個每月名目不同的彈性加項（值週／活動加班費／補發…），目前系統無對應欄位。業主決策：以**手填欄位**呈現（非自動計算），金額併入應領。

---

## 1. 目標與非目標

**目標**
- 在月薪明細新增一個「額外加給」手填欄位（金額 + 可編輯名目），會計透過既有「薪資手動調整」介面輸入。
- 金額併入應領（gross），自動隨實領（net）進入「薪資轉帳名冊」，與 Excel C 欄行為一致。
- 薪資單顯示當月實際名目（如「值週」「活動加班費」）。

**非目標（YAGNI）**
- 不自動推導值週費（業主明確表示手填）。
- 不建證照獎金／全勤獎金／推薦獎金（本次不做）。
- 不支援單月多筆加項明細（Excel 現況為單格單值；如需多筆，會計自行加總並於名目註記）。
- 全校目標人數（176）等**設定值對齊**為獨立工作項，不在本 spec。

---

## 2. 資料模型

`models/salary.py` `SalaryRecord` 新增兩欄：

| 欄位 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `extra_allowance` | `Money` | 0 | 額外加給（值週/活動加班費等，手填） |
| `extra_allowance_label` | `String(50)` | NULL | 額外加給名目 |

**⚠ 同步加到 `SalarySnapshot`（`salary_snapshots`）**：`services/finance/salary_snapshot_service._payload_columns()` 以「兩表欄位交集」反射複製，**漏欄則快照遺失**（稽核重印歷史薪條時憑空消失，memory 既有教訓）。故兩欄必須同時加到 `SalaryRecord` 與 `SalarySnapshot` 兩個 model；snapshot service 本身不需改（反射自動帶入）。

Alembic migration（parent = 目前 head `allergyenc01`）：
- `Money` 底層為 `Numeric(12, 2)`（見 `models/types.py`）。
- `upgrade`：對 **`salary_records` 與 `salary_snapshots` 兩表** 各 `add_column` 兩欄（`extra_allowance` `Numeric(12,2)` server_default '0'、`extra_allowance_label` `String(50)` nullable）。
- `downgrade`：兩表各 `drop_column` 兩欄（完整可逆）。

---

## 3. 引擎：納入應領（gross），不課二代健保

**核心點：手填值真正進 gross 的關鍵在 `totals.py`（手動調整/重算路徑），engine 計算路徑同步保持一致。**

- `services/salary/breakdown.py`：`SalaryBreakdown` 加 `extra_allowance: float = 0`、`extra_allowance_label: Optional[str] = None`（引擎不自動算，恆為 0/None）。
- `services/salary/engine.py`
  - `_fill_salary_record`：加 `_apply("extra_allowance", breakdown.extra_allowance)` 與 `_apply("extra_allowance_label", breakdown.extra_allowance_label)`，沿用既有 `manual_overrides` 保護機制（手填欄位重算時保留）。
  - gross 組裝（行 ~1892）：加 `+ breakdown.extra_allowance`。自動計算時 breakdown 值為 0，無副作用；只是讓兩條 gross 公式一致。
- `services/salary/totals.py` `recompute_record_totals`：`gross_salary` 公式加 `+ (record.extra_allowance or 0)`。**這是手填值在「有 manual_overrides」重算路徑進 gross 的實際生效點。**
- **不**將 `extra_allowance` 加入 `services/salary/supplementary_premium.py` 的 `BONUS_FIELDS_FOR_YTD` → 不計入二代健保補充保費累計（值週/活動加班費屬經常性給予，比照 `overtime_pay`、`meeting_overtime_pay`）。

資料流：`net_salary = gross_salary − total_deduction`，`extra_allowance` 已在 gross，故自動進 base 轉帳名冊（`transfer_roster.py` 用 `net_salary`，無需改）。

---

## 4. 手填介面（後端 API）

`api/salary/manual_adjust.py`：
- `AdjustModel` 加 `extra_allowance: Optional[float] = Field(None, ge=0, le=500_000)`。
- `AdjustModel` 加 `extra_allowance_label: Optional[str] = Field(None, max_length=50)`（文字欄，與數值欄分開驗證）。
- `EDITABLE_SALARY_FIELDS` 加 `"extra_allowance": "額外加給"`（設值時依既有邏輯加入 `manual_overrides`，重算保留）。
- `extra_allowance_label` 文字欄：與數值欄分流處理（不套 `ge/le`）。**設值時必須把 `"extra_allowance_label"` 加入 `manual_overrides`**，使 `_fill_salary_record` 的 `_apply("extra_allowance_label", …)` 在重算時跳過覆寫、保留名目（與金額欄同步鎖定）。並記稽核 log（沿用既有 `EDITABLE_SALARY_FIELDS` 稽核敘述格式，名目以字串值記錄）。
- 回應 dict 補 `extra_allowance`、`extra_allowance_label`。

---

## 5. 顯示

- `services/finance/salary_slip.py` `_build_earnings_table`：當 `extra_allowance > 0` 時，在「月薪應發合計」列之前多一列；名目顯示 `extra_allowance_label`（空白 → fallback「額外加給」），金額顯示 `extra_allowance`。同檔 `generate_salary_excel` 一併補。
- `api/salary/detail.py`（及 `manual_adjust.py` 回應）：salary record JSON 補 `extra_allowance`、`extra_allowance_label`。
- 轉帳名冊：不改（金額隨 net 自動帶入 base 名冊）。

---

## 6. 前端

- 薪資手動調整元件（manual adjust 對應 SFC）：加「額外加給」金額輸入 + 「名目」文字輸入；送出時帶 `extra_allowance` / `extra_allowance_label`。
- 薪資單/明細顯示元件：`extra_allowance > 0` 時顯示該列（名目 + 金額）。
- 後端 response_model 變動後執行 `npm run gen:api` 更新 `schema.d.ts`（CI openapi-drift 會擋）。

---

## 7. 測試

**後端（pytest）**
- 手填 `extra_allowance` 後 gross/net 正確含此項。
- manual_adjust 設值 → `manual_overrides` 含 `extra_allowance`；重算（`_fill_salary_record` override 路徑）後值與名目保留。
- `extra_allowance` **不**計入二代健保補充保費（與含 `special_bonus` 的對照組比較）。
- `extra_allowance_label` 持久化 + 長度上限驗證（>50 字 422）。
- base 轉帳名冊金額含 `extra_allowance`（經 net_salary）。
- salary_slip 在 `extra_allowance > 0` 時輸出該列、名目 fallback 正確。

**前端（vitest）**
- 手動調整元件能輸入金額 + 名目並送出對應 payload。
- 薪資單元件在有額外加給時渲染該列。

---

## 8. 風險與注意

- **雙路徑 gross 漂移**：gross 公式存在 `engine.py` 與 `totals.py` 兩份；本設計兩處都改並有測試覆蓋（memory 既有教訓）。
- **二代健保誤課**：務必不加入 `BONUS_FIELDS_FOR_YTD`；測試明確驗證。
- **PostToolUse black hook**：ivy-backend `.py` Edit 後自動 black，surgical edit 注意（memory 既有教訓）。
- **migration 合併前**手動 `alembic upgrade heads`（dev DB head 目前 `allergyenc01`）。

---

## 9. 分開 commit（依 workspace SOP）

- 後端 commit 一筆（model + migration + engine/totals + manual_adjust + salary_slip + detail + tests）
- 前端 commit 一筆（manual adjust UI + slip 顯示 + gen:api schema.d.ts）
- 前後端分支各一支；後端先（migration 先行）。

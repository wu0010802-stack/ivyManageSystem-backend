# 薪資歷史完整明細（各項獎金/收入/扣款攤開）設計

- 日期：2026-06-06
- 範圍：跨前後端（ivy-backend `api/salary` + ivy-frontend `SalaryHistoryPanel`）
- 目標畫面：**管理端 SalaryView →「薪資歷史」分頁**（單員工 N 月歷史）

---

## 1. 背景與問題

「計算薪資」分頁已把單月所有獎金/收入欄位攤開，但**「薪資歷史」分頁**（`SalaryHistoryPanel.vue` ← `GET /salaries/history`）只用一個「獎金」欄彙總，而該彙總 `calculate_display_bonus_total(r)` 僅含 `festival + overtime + performance + special`，**語意錯且不完整**：

- 漏掉：主管紅利、加班費、園務會議加班、生日禮金、額外加給、特休未休折現、考核年終。
- 更關鍵：它把 **節慶/超額**（其實是「另行轉帳、不進實發」）算進「獎金」，卻**漏掉進實發的主管紅利** → 與應發/實發對不起來。

使用者要求：「薪資歷史裡面要包含所有紀錄像是獎金之類的」=> 把每月**所有**收入/獎金/扣款明細攤開，且數字要對得起來。

## 2. 目標 / 非目標

**目標**
- 薪資歷史每一列可展開，顯示該月完整薪條明細：① 進帳收入 ② 另行轉帳 ③ 扣款，三區各有小計，並落到實發。
- 摘要列的「獎金合計」改為**能對帳**的數字。
- 數值一律來自 `SalaryRecord` persisted 欄位，**零重算 → 零漂移**。

**非目標（YAGNI）**
- 不做列印/匯出薪條 PDF（已有 `services/finance/salary_slip.py`，未來可接）。
- 不動教師自助 Portal 歷史（本次只做管理端）。
- 不改 `calculate_display_bonus_total` 既有語意（portal/報表仍在用，避免連帶回歸）。
- 不改任何薪資**計算**邏輯（engine/totals 不動），純展示層。

## 3. 已驗證的事實基礎（用真實 dev PG 資料定讞）

權威來源：`services/salary/totals.py::recompute_record_totals`（公式）＋ `services/finance/salary_slip.py`（官方薪條 PDF 分區）＋ 對 dev DB `salary_records` 233 列實查。

| 群組 | 欄位 | 是否進 `gross_salary`/`net_salary` |
|------|------|------------------------------------|
| **進帳收入** | `base_salary`、`hourly_total`、`performance_bonus`、`special_bonus`、**`supervisor_dividend`**、`overtime_pay`、`meeting_overtime_pay`、`birthday_bonus`、`extra_allowance`(+`extra_allowance_label`) | ✅ 進 |
| **另行轉帳** | **`festival_bonus`**、**`overtime_bonus`**、`appraisal_year_end_bonus`、`unused_leave_payout` | ❌ 不進（表外、獨立金流） |
| **扣款** | `labor_insurance_employee`、`health_insurance_employee`、`pension_employee`、`late_deduction`、`early_leave_deduction`、`missing_punch_deduction`、`leave_deduction`、`absence_deduction`、`other_deduction` | （= `total_deduction`） |

**驗證結果：**
- `net_salary = gross_salary − total_deduction`：**233/233 列全部成立**（authoritative invariant）。
- 真實列 `id=5`：`gross 7950 = base 2950 + supervisor_dividend 5000`；`festival_bonus 26000` **不在** gross → 確認主管紅利進實發、節慶為另行轉帳。
- `appraisal_year_end_bonus`/`unused_leave_payout` 在 dev DB 全為 0（與 CLAUDE.md §10：engine 一律填 0、考核年終改走年終 E化獨立轉帳 一致），仍需在 UI 預留（年終 E化寫入後或舊資料可能 > 0）。

**重要陷阱（守衛）**
- ⚠ `supervisor_dividend` 的**欄位註解寫「主管紅利（獨立轉帳）」與 CLAUDE.md §11 措辭具誤導性**——它實際**進 gross/net**。本設計以驗證過的 `totals.py` + 官方薪條 + PG 實查為準。
- ⚠ `supplementary_health_employee`（二代健保補充保費）**已併入** `health_insurance_employee`；明細中只作健保下的**資訊子列**，**不可**再加進扣款總額（否則 double-count）。
- ⚠ `meeting_absence_deduction`（園務會議缺席扣節慶）**已在 engine 內從 `festival_bonus` 扣抵**（engine.py:1752-1754），**不**另列為扣款。
- ⚠ 少數 seed 列「各進帳收入欄位加總 ≠ persisted gross」（dev DB `bypass_standard_base` 灌的測試列，差額無對應欄位，`manual_overrides=[]`）。因此：**`gross_salary`/`total_deduction`/`net_salary` 一律顯示 persisted 值當權威小計**；進帳收入區補一條「其他（未分類）」吸收差額，使該區永遠對得回應發。

## 4. 取徑

**取徑 A（採用）** — 擴充現有 `GET /salaries/history`，每月一次回傳完整明細（含後端組好的分組+小計）。資料本就在 record 上、歷史 ≤60 列，一次撈完無 N+1、無新端點。

取徑 B（不採用）：展開時才 lazy 打單月明細端點 → 每次展開多一次請求、多維護一個端點，歷史筆數不多不划算。

## 5. 後端設計（ivy-backend）

### 5.1 純函式 helper（可單元測試的正確性核心）

於 `api/salary_fields.py`（既有顯示 helper `calculate_display_bonus_total` 所在、`records.py` 已 import）新增純函式：

```python
def build_history_breakdown(record) -> dict:
    """從 SalaryRecord persisted 欄位組出歷史明細分組（純展示，不重算）。

    回傳分組 + 小計；小計一律採 record persisted 值（gross/total_deduction/net）
    當權威，income 區補「其他（未分類）」吸收 gross 與已知收入欄位的差額。
    """
```

回傳結構（每月一筆）：

```jsonc
{
  "income": [                       // 進帳收入（計入應發）
    {"key": "base_salary",        "label": "底薪",        "amount": 2950},
    {"key": "performance_bonus",  "label": "績效獎金",    "amount": 0},
    {"key": "special_bonus",      "label": "特別獎金",    "amount": 0},
    {"key": "supervisor_dividend","label": "主管紅利",    "amount": 5000},
    {"key": "overtime_pay",       "label": "加班費",      "amount": 0},
    {"key": "meeting_overtime_pay","label": "園務會議加班","amount": 0},
    {"key": "birthday_bonus",     "label": "生日禮金",    "amount": 0},
    {"key": "hourly_total",       "label": "時薪總計",    "amount": 0},   // 僅時薪制非 0
    {"key": "extra_allowance",    "label": "額外加給",    "amount": 0, "note": extra_allowance_label},
    {"key": "other_income",       "label": "其他（未分類）","amount": 0}  // = gross − base − hourly − Σ其餘；0 時前端隱藏
  ],
  "income_subtotal": 7950,          // = record.gross_salary（權威）
  "separate_transfer": [            // 另行轉帳（不進應發/實發）
    {"key": "festival_bonus",          "label": "節慶獎金",    "amount": 26000},
    {"key": "overtime_bonus",          "label": "超額獎金",    "amount": 0},
    {"key": "appraisal_year_end_bonus","label": "考核年終獎金","amount": 0},
    {"key": "unused_leave_payout",     "label": "特休未休折現","amount": 0}
  ],
  "separate_subtotal": 26000,
  "deductions": [                   // 扣款（= total_deduction）
    {"key": "labor_insurance_employee", "label": "勞保",     "amount": -xxx},
    {"key": "health_insurance_employee","label": "健保",     "amount": -xxx,
       "children": [{"key":"supplementary_health_employee","label":"其中：二代健保補充保費","amount":-xx,"informational":true}]},
    {"key": "pension_employee",         "label": "勞退自提", "amount": -xxx},
    {"key": "late_deduction",           "label": "遲到扣款", "amount": -xxx},
    {"key": "early_leave_deduction",    "label": "早退扣款", "amount": -xxx},
    {"key": "missing_punch_deduction",  "label": "未打卡扣款","amount": -xxx},
    {"key": "leave_deduction",          "label": "請假扣款", "amount": -xxx},
    {"key": "absence_deduction",        "label": "曠職扣款", "amount": -xxx},
    {"key": "other_deduction",          "label": "其他扣款", "amount": -xxx}
  ],
  "deduction_subtotal": 4604,       // = record.total_deduction（權威）
  "net_salary": 3346                // = record.net_salary（權威）
}
```

設計守衛（在 helper 內以斷言/註解固定）：
- `income_subtotal` 取 `record.gross_salary`（非各 income 項相加）。
- `deduction_subtotal` 取 `record.total_deduction`；`supplementary_health_employee` 僅作 `children` informational，**不**進清單金額加總。
- `other_income = gross − base − hourly − (performance+special+supervisor+overtime_pay+meeting_overtime+birthday+extra_allowance)`。
- 不出現 `meeting_absence_deduction`（已在 festival 內扣抵）。

### 5.2 Response schema（`schemas/salary_records.py`）

`SalaryHistoryItemOut` 維持現有 flat 欄位（摘要列用，已被前端消費）並新增：
- `in_gross_bonus: float`（摘要「獎金合計」= `gross_salary − base_salary − hourly_total`；`# pii-allow:`）。
- `separate_transfer_total: float`（另行轉帳小計，摘要可選顯示）。
- `payslip_detail: SalaryHistoryBreakdownOut`（上述三區結構，巢狀 Pydantic model；各子項加 `# pii-allow:`）。**命名避開既有 `SalaryRecordItemOut.breakdown`（人數來源 enrollment，前端 `SalaryBreakdown.vue` 已消費），避免語意撞名。**
- 既有 `total_bonus` 保留但 docstring 標註 deprecated（前端歷史改用 `in_gross_bonus`；不移除以免動到 schema 契約/其他潛在消費者）。

新增巢狀 model：`SalaryHistoryLineOut`（key/label/amount/note?/informational?/children?）、`SalaryHistoryBreakdownOut`（income/income_subtotal/separate_transfer/separate_subtotal/deductions/deduction_subtotal/net_salary）。

### 5.3 端點（`api/salary/records.py::get_salary_history`）

迴圈內每筆 `r` 呼叫 `build_history_breakdown(r)` 填入 `payslip_detail`，並補 `in_gross_bonus`/`separate_transfer_total`。權限（`SALARY_READ` + `_enforce_self_or_full_salary`）、audit log 不變。

## 6. 前端設計（ivy-frontend）

- `SalaryHistoryPanel.vue`：`el-table` 加 `type="expand"`。`HistoryRow` interface 對齊新 OpenAPI 型別（`payslip_detail` + `in_gross_bonus`）。摘要「獎金」欄改顯示 `in_gross_bonus`，欄名改「獎金合計」。其餘欄（底薪/保險/應發/扣款/實發）不變；圖表不變。
- 新增 `src/views/salary/SalaryHistoryDetail.vue`：props 收 `payslip_detail`，渲染三區（進帳收入 / 另行轉帳 / 扣款），每項 `money()` 格式化；`amount === 0` 的非關鍵列淡化或隱藏（`other_income`、全零另行轉帳列預設隱藏）；`informational` 子列縮排小字；各區顯示後端小計，最後落 `net_salary`。
- 型別來源：`import type { AxiosResp, ApiResponse } from '@/api/_generated/typed'`，跑 `npm run gen:api` 後對齊（見 §8）。

## 7. 測試計畫

**後端（pytest，純函式優先）** `tests/test_salary_history_breakdown.py`：
- `build_history_breakdown`：① `income_subtotal == record.gross_salary` ② `deduction_subtotal == record.total_deduction` ③ `net_salary == gross − total_deduction` ④ 另行轉帳項（festival/overtime/appraisal/unused_leave）**不**出現在 income ⑤ `supplementary_health` 為 informational child、**不**進 deduction 加總 ⑥ seed 殘差 → `other_income` 吸收使 income 各項+other == gross ⑦ `supervisor_dividend` 出現在 income（回歸防呆，釘住「主管紅利進實發」這個易錯點）。
- 端點測試：`GET /salaries/history` 回傳含 `breakdown` 三區與 `in_gross_bonus`；權限/self-guard 沿用既有。

**前端（Vitest）** `SalaryHistoryDetail.spec.ts`：三區渲染、零值隱藏、informational 子列縮排、另行轉帳標示、小計顯示。

## 8. 部署 / 型別同步 / CI

- response_model 變動 → `python scripts/dump_openapi.py`（後端）+ `npm run gen:api`（前端），只 commit 前端 `schema.d.ts`。
- 兩 repo `openapi-drift` CI 會驗；merge 前手動 `npm run gen:api:check`。
- 跨 repo parity：新增 schema 欄位為純薪資金額，沿用既有 `# pii-allow:` 慣例；Sentry denylist 無需改（API 回應非 Sentry event）。
- 無 DB schema 變動、無 migration、無 Alembic、無權限新增。

## 9. 分支與收尾

- 後端、前端各開一條從 `origin/main` 切的 worktree 分支（`feat/salary-history-breakdown-<date>-be` / `-fe`），spec 於後端分支首 commit 一併納入。
- 後端先（契約）→ 前端後（接型別）。各 repo 分開 commit。
- DoD：push + CI 綠 + worktree remove（CLAUDE.md §收尾紀律）。

## 10. 風險

- **誤分組 = 金流錯誤**：已用 PG 實查定讞，並以 pytest 回歸釘住「主管紅利進實發 / 節慶為另行轉帳 / 補充保費不重複」三個易錯點。
- **巢狀 schema 型別**：OpenAPI 巢狀 model 需 gen:api 正確產出；前端以 typed helper 對齊，避免手寫漂移。
- **other_income 出現非零**：實務上僅 dev seed 殘差；prod 引擎算出的 gross 會對齊，正常為 0（隱藏）。若 prod 非零代表上游資料異常，反而是有用訊號（可加 console.warn）。

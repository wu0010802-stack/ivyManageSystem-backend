# 年終獎金 E 化重構 — 階段 1：自動結算引擎接線 + Excel 式後台

- 日期：2026-06-01
- 狀態：design draft，待 user review
- 範圍：跨前後端（後端重心：結算引擎接線 + 設定/產生 API；前端：Excel 式總表頁 + 本期設定頁）
- 起點：USER 想重構「年終獎金」與「考核年終 payout」兩功能，讓後台**像 Excel 一樣呈現**，並**用專案資料 E 化原本手 key 的方式**

---

## 1. 背景

園內每年 2 月發「年終分紅獎金」，目前完全在 Excel（`114年年終經營績效.xls`，22 sheets）手工計算：

- **主結算（年終獎金）**：6 步串聯 `(基本薪+節慶)×平均績效% ×機構達成比率% +各項扣款，再×到職月/12`
- **8 項特別獎金**：113上/下考核獎金、113上/下學期紅利、114上才藝鼓勵、114上教課獎勵、114上超額、114.8-115.01節慶差額
- **每人一張發放明細表**（A 年終獎金 + B 特別獎金 = 實際金額）+ **獨立年終轉帳名冊**

### 1.1 重要現況：系統已蓋好八成，但「引擎未接線」

前人已實作完整 year_end 基建（`models/year_end.py` 6 表、`services/year_end/engine.py` 6 步純函式引擎 + 測試、簽核流程、`print_pdf.py` 明細/名冊、`excel_io.py` 匯入匯出、`appraisal_sync.py` 考核橋接），但：

- `compute_settlement`（6 步引擎）**全 codebase 僅測試呼叫，零 production caller**
- production **唯一**建立 `year_end_settlements` 的路徑是 `excel_io.import_year_end_to_db`——即**解析 USER 手 key 的 Excel 把值塞進 DB**，引擎根本沒跑
- 後台無「Excel 式總表」呈現頁（現有前端是逐張結算單簽核頁）
- 考核 payout（`appraisal_sync` + `AppraisalPayoutView`）已接上**2 月薪資單獨立帶款**，與「併入年終」的需求衝突（見 §6）

**結論**：本重構不是從零做，而是**接線 + 呈現 + 設定 + 整合**。引擎/表/輸出多數可復用。

---

## 2. 需求摘要（brainstorm 已對齊）

| 項 | 決議 |
|---|---|
| 呈現 | 後台「Excel 式年終總表」：一人一列、各組成一欄、最右合計；白底=自動算、黃底=可手改（只有超額、獎懲） |
| 推進方式 | **分階段**（階段 1 先讓 USER 脫離 Excel；階段 2 再把剩餘手填項全自動） |
| 獎金項目 | 就這 8 項，無遺漏、無新增 |
| 年終本質 | 一人一個**總額** = 主結算(A) + 8 項特別獎金(B)；**考核獎金是 B 裡的兩欄**，全部一起算進合計、發一筆 |
| 發放 | 年終為**獨立一筆轉帳**，與 2 月月薪分開轉（不進 gross_salary / 勞健保 / 應發），同在 2 月發 |
| 簽核 | **兩關：會計 → 老闆**（簡化現有三關） |
| 機構達成比率 | = 在籍 ÷ 人工填的目標人數（兩學期平均），**可自動算**，保留手動覆寫 |
| 人工項 | 真正逐人手動只剩 **獎懲** 與 **超額獎金**；其餘 🟢 自動 或 ⚙️ 每學期設定一次 |

---

## 3. 範圍

### 3.1 階段 1 — IN

1. **本期設定頁**（每學年設一次，**開新年度自動複製上一年設定再微調** — 決策⑤）：全校目標人數（兩學期）、各班編制人數、機構達成比率（自動算+可覆寫）、考核基準獎金表/才藝單價/教課單價/扣款費率（落地到既有設定表）
2. **自動結算服務**（核心新工）：跨員工拉專案資料 → 餵 `compute_settlement` → upsert `year_end_settlements` + `employee_year_end_snapshot`，取代 Excel import
3. **階段 1 自動拉的引擎輸入**：基本薪（職位標準/個人，待決策①）、節慶基數（角色查表）、到職比例、全校達成率、機構達成比率、班級經營績效、考核獎金（見 §5 資料源對應表）
4. **Excel 式總表頁**（前端新頁）：一人一列、各欄組成、合計；黃底可改（超額/獎懲）；列可展開看 6 步明細
5. **輸出**（復用既有）：每人明細表 PDF、年終轉帳名冊、橫向總表 xlsx
6. **兩關簽核（會計→老闆）** + **獨立年終轉帳**
7. **考核併入年終**：移除 salary engine 的 2 月考核 pull，改由年終結算 step6 納入（§6）

### 3.2 階段 1 — 暫以「手填或沿用」呈現（→ 階段 2 全自動）

- 班級舊生達成率（step1 六率之一）
- 學期紅利、才藝鼓勵、教課獎勵、節慶差額（4 項特別獎金）
- 考勤類扣款（事假 / 病假 / 遲到早退 / 會議缺席）

> 階段 1 這些欄位**照樣在總表呈現、照樣加總**，只是值由 HR 手填（或沿用既有 Excel 匯入 / 既有 special_bonus CRUD）。引擎與呈現不變，階段 2 只換「值的來源」。

### 3.3 永遠人工

- 超額獎金（逐月超收，業主裁量）
- 獎懲（大過小功，人事決定）

### 3.4 OUT（階段 2/3）

- 才藝鼓勵 / 教課獎勵 / 節慶差額 / 學期紅利 / 考勤扣款的**全自動推導**（需接 activity / 考勤 / 月度在園歷史）
- 班級舊生達成率自動化（需以 `Student.enrollment_school_year` 新寫舊生判定）
- 所得稅扣繳（年終為實領，會計表外處理）
- 二代健保補充保費自動扣繳：依決策⑥；若 A 則納入階段 1，若 B 則表外處理（見 §6.3）

---

## 4. 架構與資料流

```
[本期設定頁] (每學年一次)
  └→ upsert OrgYearSettings(兩學期: 目標人數/達成率/機構比率/會議扣款)
  └→ upsert ClassEnrollmentTarget(各班: 編制人數/班導/副導)
  └→ 獎金標準/費率寫 BonusConfig / cycle.params_snapshot

[產生年終結算] (HR 按「重新試算」)
  services/year_end/settlement_builder.py (新)
   1. resolve cycle(academic_year) + 參與員工(在職 + HR 勾選的離職)
   2. 每員工蒐集引擎輸入:
        base_salary, festival_total → snapshot
        PerformanceRates(全校達成率上下 / 班經營上下 / 班舊生上下)
        org_achievement_rate, DeductionBreakdown, hire_months
        special_bonus_total = SUM(special_bonus_items)  ← 含考核兩筆
   3. compute_settlement(...) → upsert employee_year_end_snapshot + year_end_settlement
   4. 自動算項寫入；手填項沿用既有值 / 預設 0
   5. 寫 audit_log

[Excel 式總表頁]
  GET 列出 settlements(一人一列 + 各 special_bonus 欄 + 合計)
  PATCH 單格(超額/獎懲) → 重算該員 settlement
  展開 → 該員 6 步明細 dialog

[簽核 2 關]
  DRAFT → 會計簽(ACCOUNTING_SIGNED) → 老闆 finalize(FINALIZED)
  (跳過 SUPERVISOR_SIGNED stage)

[輸出]
  GET /print/slip(每人明細 PDF) / /print/roster(轉帳名冊) / /export/summary(xlsx)

[考核 payout]  ← 重構
  appraisal_sync.generate_payouts() 仍寫 special_bonus_items(APPRAISAL_HALF_*)
  但 salary engine 不再於 2 月 pull → 不重複發；考核金額只經年終 step6 + 年終轉帳發放
```

**核心原則**：`year_end_settlements` 是年終的 single source；`special_bonus_items` 是 8 項特別獎金的 single source（step6 SUM 進 total）；年終**不經 SalaryRecord**（獨立轉帳）。

---

## 5. 資料源對應表（階段 1 自動拉）

| 引擎輸入 | 來源 | 判定 | 備註 / caveat |
|---|---|---|---|
| `base_salary` | 每位員工**月薪實際底薪**（復用 salary engine base 解析，honor `employee.py:158` flag） | ✅ | **決策①=A**：多數人=`PositionSalaryConfig` 職位標準（教師 36160），資深者=個人含年資 `Employee.base_salary`（林姿妙 45499、呂麗珍 44300）；對齊 Excel 逐人 |
| `festival_total` | 角色節慶獎金**基數**查表（`BonusConfig`，依職位）| ✅ | **更正（2026-06-02）**：Excel 驗算證實是「單筆基數」非全年加總——蔡宜倩 36160+**2000**=38160×97%=37015.2 ✓（2000=head_teacher_ab）、呂宜凡 +1200=assistant_ab ✓、呂麗珍 +6500=principal ✓。比原想更簡單（查表即可，無需逐月編排）。`EmployeeYearEndSnapshot.festival_total` 註解「2/6/9/12 加總」亦為誤導，一併修正 |
| 全校達成率(上/下) | `count_students_active_on(基準日)` ÷ `OrgYearSettings.enrollment_target` | ✅ | **決策③**：在籍 filter 改用更嚴格條件（排除已退學/lifecycle 非 active），避免分母虛增 |
| 機構達成比率 | 兩學期全校達成率平均（full-year）/ 單學期（partial） | ✅ | 對齊 USER 說明；寫入 `OrgYearSettings.org_achievement_rate`，保留手動覆寫 |
| 班級經營績效(上/下) | 各班逐月在園(`classroom_at_month_end` / `monthly_enrollment_snapshots`) 平均 ÷ `ClassEnrollmentTarget.head_count_target` | ✅ | caveat：歷史班級歸屬精度依轉班紀錄完整度 |
| 班級舊生達成率(上/下) | — | ⚠️→階段2 | 階段 1 手填 `ClassEnrollmentTarget.returning_student_rate`；階段 2 以 `Student.enrollment_school_year` 自動 |
| 考核獎金(兩筆) | `AppraisalSummary.bonus_amount` → `appraisal_sync` 寫 `special_bonus_items` | ✅ | **決策②**：抓哪兩學期？Excel=前一學年上+下；現碼=N-1下+N上 → 需修正（見 §6.2） |
| `hire_months` / 比例 | `Employee.hire_date` / `resign_date` 對**民國日曆年**（roc+1911 的 1/1–12/31）重疊月數 | ⚠️ | **更正（2026-06-02 Task3 review）**：proration 期間是民國日曆年（114年度=2025 Jan–Dec），**非學年 Aug–Jul**（郭玟秀「114.01~114.10共10個月」）。auto 給日曆年基準值；但 Excel 的 10/4.5/3 含產假排除/簽約日/半月等人工判斷 → `hire_months` **可手動覆寫**（存 `settlement.calc_meta.hire_months_override`，Task 6 manual patch 設定，build 時優先採用），re-build 保留 |
| 扣款(事假/病假/遲到/會議) | — | ⚠️→階段2 | 階段 1 手填 `settlement.deduction_*`；階段 2 接考勤/假單 |
| 超額獎金 / 獎懲 | HR 手填 | 🔴 永遠人工 | 超額 → `special_bonus_items(EXCESS_ENROLLMENT)`；獎懲 → `settlement.deduction_disciplinary` |

---

## 6. 考核 payout 重構（併入年終）

### 6.1 現況衝突

現有 `services/salary/appraisal_year_end.query_appraisal_year_end_bonus` 在 **2 月 calculate 時被 salary engine 呼叫**，把考核獎金寫進 `SalaryRecord.appraisal_year_end_bonus`（隨 2 月薪資單帶出）。但 USER 要的是「考核獎金是年終總額的一部分、走年終獨立轉帳」。若兩者並存 → **重複發**。

### 6.2 重構方案

1. **保留** `appraisal_sync.generate_payouts()`：仍把 `AppraisalSummary.bonus_amount` 寫入 `special_bonus_items(APPRAISAL_HALF_BONUS_FIRST/SECOND)`——這是考核→年終的橋。
2. **修正取的學期**（決策②）：依 Excel 實務改為「前一完整學年上學期 + 下學期」（114年度年終 → 113上 + 113下）。理由：當年度上學期考核 3 月才算完，趕不上 2 月年終。`appraisal_sync.resolve_target_cycles` + `period_label` 對應一併修正。**注意**：改為「同學年上+下」後，FIRST=上=較早、SECOND=下=較晚，與學期順序一致；`SpecialBonusType` docstring（models/year_end.py:79-80）原「FIRST/SECOND 為時間順序、與學期相反」的反轉警語**已過時**，須移除而非沿用。
3. **移除** salary engine 的 2 月 pull：`services/salary/engine.py` 不再呼叫 `query_appraisal_year_end_bonus`；`SalaryRecord.appraisal_year_end_bonus` 停用（保留 column 向後相容，恆 0，或標 deprecated）。
4. 考核金額**只**經年終 step6（`special_bonus_total`）→ `total_amount` → 年終轉帳發放。
5. 前端 `AppraisalPayoutView` 的「生成考核 payout」併入年終結算流程（生成結算時自動觸發 appraisal sync，或保留為設定頁子步驟）。

> ⚠️ rollout 安全：若 prod 已有 2 月薪資帶過考核款，需確認無重複；目前 dev DB year_end 表 0 row，且考核 payout 疑未實際運營，重構風險低（rollout 前 USER 確認）。

### 6.3 二代健保補充保費連動（決策⑥=B）

USER 拍板 **B：年終為實領、補充保費由會計表外處理**。據此：

- 年終結算 `total_amount` / 轉帳名冊 / 明細表**不計算、不扣** 2.11% 補充保費（年終淨額 = total_amount）。
- 移除 salary engine 的 2 月考核 pull 後，2 月二代健保年累計 base **不再含考核款**（CLAUDE.md #11 名單將移除 `appraisal_year_end_bonus`）——此即園方意圖（考核已移入年終、年終表外處理）。
- **Rollout 檢查**：確認 prod 無「已用舊路在 2 月薪資累計過考核款的二代健保紀錄」需回算；目前 year_end 表 0 row、考核 payout 疑未運營，風險低。
- **文件**：rollout 時更新 workspace CLAUDE.md #11，移除 `appraisal_year_end_bonus` 於二代健保累計名單，並註明年終補充保費表外。

---

## 7. 後端

### 7.1 新檔 `services/year_end/settlement_builder.py`（核心）

```python
def build_settlements(db, academic_year, included_resigned_ids, actor_id, *, recompute=True) -> BuildResult:
    """跨員工跑引擎產生 year_end_settlements + employee_year_end_snapshot。
       advisory lock 包整個 transaction；idempotent（重跑覆寫未 finalized，FINALIZED 略過）。"""

# 純函式 / thin query 分離（單元測試重點）：
def gather_performance_rates(db, cycle, employee) -> PerformanceRates: ...
def resolve_org_achievement_rate(org_settings, employee_period) -> Decimal: ...
def gather_deductions(db, cycle, employee) -> DeductionBreakdown: ...   # 階段1：讀既有手填值
def compute_festival_total(db, employee, cycle) -> Decimal: ...         # 逐月加總
def compute_hire_months(employee, cycle) -> Decimal: ...
```

### 7.2 新檔 `services/year_end/enrollment_rates.py`

```python
def school_achievement_rate(db, calc_date, target) -> Decimal     # 在籍÷目標（嚴格 filter）
def class_performance_rate(db, classroom_id, months, target) -> Decimal  # 月平均在園÷編制
```
（封裝 §5 caveat：統一在籍判定條件）

### 7.3 新增 API（掛現有 `api/year_end`）

| Method | Path | Permission | 用途 |
|---|---|---|---|
| POST | `/year_end/cycles/{id}/build-settlements` | YEAR_END_WRITE | 跑引擎產生/重算全員結算（試算） |
| PATCH | `/year_end/settlements/{id}/manual` | YEAR_END_WRITE | 改單員手填項（超額/獎懲）→ 重算該員 |
| GET | `/year_end/cycles/{id}/grid` | YEAR_END_READ | Excel 式總表（一人一列 + 各 special 欄 + 合計） |
| POST | `/year_end/settlements/{id}/sign_accounting` | (會計) | 既有，2 關第 1 關 |
| POST | `/year_end/settlements/{id}/finalize` | (老闆) | 既有，2 關第 2 關 |

復用既有：cycles / org_settings / class_targets CRUD、special_bonuses CRUD、print/export 端點。

### 7.4 簽核 2 關映射

沿用 `YearEndSettlementStatus`，**跳過 SUPERVISOR_SIGNED**：`DRAFT → ACCOUNTING_SIGNED（會計）→ FINALIZED（老闆）`。權限：會計關用既有會計權限、finalize 用 `YEAR_END_FINALIZE`（老闆/負責人）。`sign_supervisor` 端點保留但前端不用（或標 deprecated）。

---

## 8. 前端

| 檔 | 動作 |
|---|---|
| `src/views/yearEnd/YearEndGridView.vue` | **new**：Excel 式總表（一人一列、各欄組成、合計、黃底可改、展開明細 dialog、頂部試算/生成/匯出/簽核） |
| `src/views/yearEnd/YearEndConfigView.vue`（或既有設定頁擴充） | **new/擴充**：本期設定（招生與班級 / 獎金標準 / 扣款費率 三分頁） |
| `src/api/yearEnd.ts` | 加 build-settlements / grid / manual-patch wrapper（typed via OpenAPI） |
| `src/api/_generated/schema.d.ts` | regen |
| `src/components/layout/AdminSidebar.vue` / `router` | 入口 + route |
| 既有 `YearEndDetailView` / `AppraisalPayoutView` | 視整合程度調整（payout 併入年終流程） |

---

## 9. 測試策略

| 層 | 涵蓋 |
|---|---|
| `tests/test_year_end_settlement_builder.py` | build idempotent / FINALIZED 略過 / 離職勾選 / 重算；以 Excel 真實 case（蔡宜倩 40106.71、郭玟秀 19354）驗端到端 |
| `tests/test_year_end_enrollment_rates.py` | 全校達成率（嚴格 filter）/ 班級經營績效（逐月平均÷編制）純函式 |
| `tests/test_year_end_appraisal_refactor.py` | 取前學年上+下、salary engine 不再 pull、無重複發 |
| 前端 vitest | Grid render / 手填重算 / 展開明細 / 簽核 2 關 / 試算流程 |

**驗收金標準**：用 `114年年終經營績效.xls` 的數字，系統算出的 settlement 與 Excel 逐人吻合（容差 ≤1 元 vs **實際整數核發單**，進位 HALF_UP）。

> **已知行為（partial-year 零頭）**：引擎逐步 Decimal q2（subtotal）+ proration q4，與 Excel 的 float 連乘在「到職比例非整數」時會有 sub-dollar 落差（郭玟秀 payable 14631.77 vs Excel 14632.35，但對整數核發單 19354 差 0.23 ≤1）。屬既有引擎進位規格（與 2026-05-25 money-rounding HALF_UP rollout 一致，系統值即政府標準）；實際轉帳為整數，不影響核發。若 HR 要求 partial-year 分位完全對齊舊 Excel，為 follow-up（需動共用引擎 proration 精度，本期不做）。

---

## 10. 關鍵決策（USER 全部拍板 2026-06-02）

1. **決策①=A｜基本薪俸來源** → **每人月薪實際底薪**（復用 salary engine base 解析，honor `employee.py:158` flag）：多數=職位標準（教師 36160），資深=個人含年資（林姿妙 45499、呂麗珍 44300）。對齊 Excel 逐人（match Excel 金標準）。
2. **決策②=前學年上+下｜考核學期** → **前一完整學年上+下**（114年度年終 = 113上 + 113下）。需修正 `appraisal_sync`（原為 113下+114上）。
3. **決策③=嚴格｜在籍 filter** → 排除已退學 / lifecycle 非 active 算達成率分母；統一 `enrollment_rates.py` 在籍判定。
4. **決策④=查表｜節慶獎金** → 角色節慶基數查表（`BonusConfig`，單筆非全年加總）。Excel 驗算推翻原「逐月加總」（見 §5）。
5. **決策⑤=複製上一年｜設定承襲** → 開新年度自動帶入上一年 `OrgYearSettings` / `ClassEnrollmentTarget` / 費率，HR 改有變動的幾項。
6. **決策⑥=B｜二代健保補充保費** → **年終為實領、補充保費由會計表外處理**（沿用現行 appraisal payout「不接」立場）。年終結算 / 轉帳 / 明細表**不扣** 2.11%。連帶：移除 salary engine 考核 pull 後，2 月二代健保 base 不再含考核款——此即園方意圖（見 §6.3）。

---

## 11. 不在本 spec 範圍（階段 2/3）

- 才藝鼓勵 / 教課獎勵 / 節慶差額 / 學期紅利 / 考勤扣款 全自動推導
- 班級舊生達成率自動化（`enrollment_school_year`）
- 所得稅 / 二代健保補充保費
- 節慶獎金模組與年終差額的雙向即時聯動

---

## 12. 影響檔案總覽

**後端**：`services/year_end/settlement_builder.py`(new)、`enrollment_rates.py`(new)、`api/year_end/__init__.py`(+3 端點)、`services/salary/engine.py`(移除考核 pull)、`services/year_end/appraisal_sync.py`(修學期)、`services/salary/appraisal_year_end.py`(deprecate)、`models/year_end.py SpecialBonusType`(docstring)、tests ×3

**前端**：`views/yearEnd/YearEndGridView.vue`(new)、`YearEndConfigView.vue`(new)、`api/yearEnd.ts`、`schema.d.ts`、sidebar/router、tests

**復用不動**：`engine.py`(6 步)、`print_pdf.py`、`excel_io.py` export、6 張表 schema、`appraisal_sync` 主體

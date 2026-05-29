# SPEC-003：時薪基準與最低工資

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | `services/salary/hourly.py`、`services/salary/minimum_wage.py`、`services/salary/constants.py`（時薪 / 最低工資相關常數）|
| Related | SPEC-001（engine 主流程）、SPEC-004（扣繳）、`services/overtime_pay_calculator.py`（加班費）、`services/finance/art_teacher_payroll.py`（才藝老師時薪明細） |

## Overview

本 SPEC 規範薪資領域中**時薪計算**與**最低工資合規驗證**兩條主軸：

1. **時薪基準（hourly base rate）**：依勞基法以「月薪 ÷ 30 ÷ 8」推算單位時薪，作為下列場景之共同分母：
   - 時薪制員工日工時拆分（正常 8h、加班 OT1 9–10h、加班 OT2 11h+）與分段倍率計薪（`hourly.py`）
   - 遲到/早退按分鐘比例扣款（`deduction.py`：`base_salary / (MONTHLY_BASE_DAYS × 8 × 60)`）
   - 月薪制加班費基底（`services/overtime_pay_calculator.py`）
   - 日薪推算（`utils.calc_daily_salary`：`base_salary / MONTHLY_BASE_DAYS`）
   - 未休特休折算（`unused_leave_pay.py`）
2. **最低工資保護（勞基法第 21 條）**：員工建檔／編輯時，月薪與時薪不得低於法定基本工資；違反時回 400 並帶 `BELOW_MINIMUM_WAGE` error code。
3. **才藝老師時薪明細**：才藝老師走獨立 `ArtTeacherPayrollEntry` 多筆明細（`hours × hourly_rate + 超額 + 加給活動 = total_amount`），不走時薪制日工時分段，由 `services/finance/art_teacher_payroll.py` 統一重算金額。

## Interface Definitions

### `services/salary/hourly.py`

| 函式 | 簽章 | 用途 |
|------|------|------|
| `_calc_lunch_overlap_hours` | `(start: datetime, end: datetime, ref_date: date) -> float` | 計算 `[start, end)` 與 `ref_date` 12:00–13:00 午休窗口的重疊時數（0.0 ~ 1.0 hr）；跨夜班需以 `punch_in.date()` 與 `effective_out.date()` 兩個日期各呼叫一次。 |
| `_compute_hourly_daily_hours` | `(punch_in: datetime, punch_out: Optional[datetime], work_end_t: time, max_hours: float = MAX_DAILY_WORK_HOURS) -> float` | 計算時薪制員工單日有效工時：缺下班打卡以排班 `work_end_t` 補填、扣午休、套每日上限；時空穿越情境（補填後 ≤ 上班 / 跨夜後超出每日上限）回傳 0.0。 |
| `_calc_daily_hourly_pay_with_cap` | `(hours: float, rate: float, remaining_ot_quota: float = float("inf")) -> tuple[float, float]` | 勞基法 §24 分段計費（0–8h ×1.0、9–10h ×1.34、11h+ ×1.67），支援勞基法 §32 II 月度 46h 加班上限；回傳 `(pay, ot_used)`，超出 quota 的加班時數仍須付薪但倍率退回 1.0。 |
| `_calc_daily_hourly_pay` | `(hours: float, rate: float) -> float` | 向後相容版本：等同 `remaining_ot_quota=inf`，不套月度上限。新呼叫端應改用 `_calc_daily_hourly_pay_with_cap` 並累計 `ot_used`。 |

> 名稱以 `_` 開頭，但實際被 `engine.py`（2 處 call site：`_load_attendance_result` line 2312/2317、3523/3528）、`__init__.py` 與測試模組匯出使用，屬「package-internal public API」。

### `services/salary/minimum_wage.py`

| 函式 | 簽章 | 用途 |
|------|------|------|
| `get_minimum_wage` | `(at_date: date) -> Tuple[int, int]` | 取指定日期適用的基本工資 `(monthly, hourly)`；目前固定回傳 `(MINIMUM_MONTHLY_WAGE, MINIMUM_HOURLY_WAGE)`，`at_date` 參數已 `del` 不參與計算 [needs review]（為未來歷年度差異版面預留 hook）。 |
| `validate_minimum_wage` | `(employee_type: str, base_salary: float, hourly_rate: float) -> None` | 驗證薪資不低於今日（`today_taipei()`）法定基本工資；底薪／時薪為 0 視為「尚未設定」不檢查；非 0 且低於法定值時拋 `HTTPException(400, code="BELOW_MINIMUM_WAGE")`。 |

### Call sites

| 呼叫端 | 位置 | 用途 |
|--------|------|------|
| `api/employees.py:439–446` | 員工新增（POST `/employees`） | `validate_minimum_wage` 守門 |
| `api/employees.py:642–649` | 員工編輯（PUT `/employees/{id}`） | `validate_minimum_wage` 守門 |
| `services/salary/insurance_salary.py:12, 57` | 投保薪資驗證 | 引用 `MINIMUM_MONTHLY_WAGE` 作為投保下限（勞保條例第 14 條）|
| `services/salary/engine.py:2312/2317、3523/3528` | 月薪計算（時薪制 hourly branch） | 呼叫 `_compute_hourly_daily_hours` + `_calc_daily_hourly_pay_with_cap`，配合 `MAX_MONTHLY_OVERTIME_HOURS = 46.0` enforce 月度上限 |
| `services/overtime_pay_calculator.py:31, 47` | 月薪制加班費 | 自定義 `MONTHLY_BASE_DAYS = 30` 區域常數（與 `salary.constants` 同值，**未共用 import**）[needs review] |
| `services/salary/deduction.py:15, 40` | 遲到／早退按分鐘扣款 | `base_salary / (MONTHLY_BASE_DAYS × 8 × 60)` |
| `services/salary/utils.py:251–253` | `calc_daily_salary(base_salary)` | `base_salary / MONTHLY_BASE_DAYS` |

## DTO Definitions

本 SPEC 範圍內無獨立 Pydantic DTO；對外契約以 `SalaryBreakdown`（`services/salary/breakdown.py`）中時薪相關欄位呈現：

| 欄位 | 類型 | 用途 |
|------|------|------|
| `work_hours` | `float` | 時薪制當月總工時（已扣午休、已套每日 12h 上限），由 `_compute_hourly_daily_hours` 累計後 `round_half_up(.., 2)` |
| `hourly_rate` | `float` | 時薪制員工的 `Employee.hourly_rate` 原值 |
| `hourly_total` | `float` | 時薪制當月總薪資；engine 依優先序決定：①才藝老師明細合計 ②`hourly_calculated_pay`（勞基法分段結果）③`hourly_rate × work_hours`（向後相容測試用） |

### 模組常數

| 常數 | 值 | 來源 | 用途 |
|------|----|------|------|
| `MONTHLY_BASE_DAYS` | `30` | `services/salary/constants.py:5` | 勞基法時薪基準分母（月薪 ÷ 30 ÷ 8） |
| `MAX_DAILY_WORK_HOURS` | `12.0` | `services/salary/constants.py:6` | 時薪制每日工時上限（正常 8h + 最高加班 4h）；用於 `_compute_hourly_daily_hours` 防止打卡異常灌水 |
| `HOURLY_OT1_RATE` | `1.34` | `services/salary/constants.py:11` | 日工時第 9–10 小時倍率（勞基法 §24）|
| `HOURLY_OT2_RATE` | `1.67` | `services/salary/constants.py:12` | 日工時第 11 小時起倍率（勞基法 §24）|
| `HOURLY_REGULAR_HOURS` | `8` | `services/salary/constants.py:13` | 正常日工時上限 |
| `HOURLY_OT1_CAP_HOURS` | `10` | `services/salary/constants.py:14` | OT1 分段上限（到第 10 小時止）|
| `SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS` | `240.0` | `services/salary/constants.py:18` | 勞基法 §43 + 勞工請假規則第 4 條：普通傷病假年內 30 日（240h）內折半薪 |
| `MINIMUM_MONTHLY_WAGE` | `29500` | `services/salary/minimum_wage.py:14` | 法定基本工資（月薪），2026-05-28 時值 [needs review]（無年度版本對應表，僅單一常數）|
| `MINIMUM_HOURLY_WAGE` | `196` | `services/salary/minimum_wage.py:15` | 法定基本工資（時薪），同上 [needs review] |
| `MAX_MONTHLY_OVERTIME_HOURS` | `46.0` | `utils/constants.py:34` | 勞基法 §32 II 月度延長工時上限；於 engine 配合 `_calc_daily_hourly_pay_with_cap` 計薪 |
| `ESTIMATED_MONTHLY_HOURS` | `176` | `services/salary/insurance_salary.py:14` | 時薪制估算月工時（22 工作日 × 8h），用於投保薪資下限推算 |

## Business Rules

### BR-1：時薪基底公式（勞基法）
- 月薪 → 時薪：`hourly_base = base_salary / MONTHLY_BASE_DAYS / 8`（即 `base_salary / 30 / 8`）
- 月薪 → 日薪：`daily_salary = base_salary / MONTHLY_BASE_DAYS`（`utils.calc_daily_salary`）
- 月薪 → 每分鐘工資（遲到／早退扣款用）：`base_salary / (MONTHLY_BASE_DAYS × 8 × 60)`（`deduction.py:40`）
- `MONTHLY_BASE_DAYS = 30` 為勞基法定義，作為跨模組共同分母

### BR-2：加班費時薪基底只取 base_salary
- 月薪制加班費（`services/overtime_pay_calculator.py:47`）以 `hourly_base = base_salary / MONTHLY_BASE_DAYS / DAILY_WORK_HOURS` 計算
- **不含** 任何加給、津貼、績效獎金、節慶獎金、主管紅利（CLAUDE.md「加班費時薪基準」不變式）
- `base_salary <= 0` 直接拋 400「該員工底薪未設定或為 0，無法計算加班費」

### BR-3：時薪制單日工時計算（`_compute_hourly_daily_hours`）
1. 有 `punch_out` → 使用實際時間；無 → 以排班 `work_end_t` 補填
2. 補填後 `effective_out <= punch_in`：嘗試 +1 天（跨夜班），若仍 > `MAX_DAILY_WORK_HOURS` 視為資料異常回 0.0
3. 扣午休 12:00–13:00：對 `{punch_in.date(), effective_out.date()}` 兩個日期各算一次（cover 跨夜班跨越隔日午休）
4. 結果套 `max(0.0, min(diff, max_hours))` 雙重保護（浮點誤差 + 每日上限）

### BR-4：時薪制日薪分段計費（`_calc_daily_hourly_pay_with_cap`，勞基法 §24）
| 分段 | 時數區間 | 倍率 |
|------|----------|------|
| 正常 | 0 – `HOURLY_REGULAR_HOURS`（8h） | ×1.0 |
| OT1 | 第 9 – `HOURLY_OT1_CAP_HOURS`（10h） | ×`HOURLY_OT1_RATE`（1.34） |
| OT2 | 第 11h 起 | ×`HOURLY_OT2_RATE`（1.67） |

回傳 `round_half_up(pay, 2)`（四捨五入至 2 位小數）。

### BR-5：時薪制月度 46h 加班上限（勞基法 §32 II）
- `remaining_ot_quota` 由呼叫端遞減（engine 中以 `monthly_ot_used` 累計）
- 超出 quota 的加班時數**仍須付薪**，但倍率退回 1.0（不加成）
- engine 在達到上限時記 `logger.warning`（"當月加班時數觸及 46h 上限，後續加班以 1.0 倍率計薪"）
- 排序：按 `punch_in_time` 由早到晚消耗 quota（engine `_load_attendance_result` line 2305–2307、3517–3519）

### BR-6：最低工資合規驗證（勞基法 §21）
- 觸發點：員工 POST / PUT，`api/employees.py` 顯式呼叫 `validate_minimum_wage`
- `employee_type == "regular"`：`base_salary > 0` 且 `base_salary < MINIMUM_MONTHLY_WAGE` → 400
- `employee_type == "hourly"`：`hourly_rate > 0` 且 `hourly_rate < MINIMUM_HOURLY_WAGE` → 400
- 底薪／時薪為 0（未設定）**不檢查**，允許新員工建檔暫時留白
- Error payload：
  ```json
  {
    "code": "BELOW_MINIMUM_WAGE",
    "message": "月薪 NT${value} 低於法定基本工資 NT${minimum}（勞基法第 21 條）",
    "context": {"employee_type": "...", "minimum": ..., "current": ...}
  }
  ```
- 取值時間基準：`today_taipei()`（不查歷史 effective date）[needs review]

### BR-7：投保薪資下限聯動最低工資（`insurance_salary.py`）
- `validate_insurance_salary` 引用 `MINIMUM_MONTHLY_WAGE`：投保薪資 > 0 且 < `MINIMUM_MONTHLY_WAGE` 拋 400（勞保條例第 14 條）
- 時薪制估算月工資 = `hourly_rate × ESTIMATED_MONTHLY_HOURS（176）`，用於 `resolve_insurance_salary_raw` 計算投保下限

### BR-8：才藝老師時薪計算差異（`services/finance/art_teacher_payroll.py`）
- 走獨立 `ArtTeacherPayrollEntry` 多筆明細，**不**走 `_compute_hourly_daily_hours` + `_calc_daily_hourly_pay_with_cap` 日工時分段
- `recompute_entry_amounts` 計算：
  - `base_amount = round_half_up(hours × hourly_rate)`
  - `total_amount = base_amount + excess_amount + activity_bonus`
- engine `_build_breakdown` 優先序（`engine.py:1811–1818`）：
  1. `art_teacher_entries_total`（明細 SUM）> 0 → `breakdown.hourly_total = art_entries_total`
  2. `hourly_calculated_pay`（勞基法分段計薪結果）
  3. `hourly_rate × work_hours`（向後相容測試情境）
- API 端點（`api/art_teacher_payroll.py`）：
  - `GET /api/art-teacher-payroll`（list，按工號 + entry id 排序）
  - `POST /api/art-teacher-payroll`（建立；強制 `emp.employee_type == "hourly"`）
  - `PUT /api/art-teacher-payroll/{entry_id}`（更新；自動重算金額）
  - `DELETE /api/art-teacher-payroll/{entry_id}`
  - `GET /api/art-teacher-payroll/import-template`（範本下載）
  - `POST /api/art-teacher-payroll/batch-import`（Excel 批次匯入）
  - `GET /api/art-teacher-payroll/{year}/{month}/roster`（清冊匯出）
  - 權限：讀 `Permission.SALARY_READ`、寫 `Permission.SALARY_WRITE`

### BR-9：未休特休折算工資（勞基法 §38 IV）
- `calculate_unused_leave_compensation(unused_hours, hourly_wage) = unused_hours × hourly_wage`
- 時薪由呼叫端依員工類型決定（unused_leave_pay.py:6–8 docstring）：
  - 月薪制：月薪 ÷ 30 ÷ 8（即 BR-1 公式）
  - 時薪制：直接使用 `hourly_rate`

### BR-10：常數重複定義 [needs review]
- `MONTHLY_BASE_DAYS = 30` 同時定義於 **3 處**：
  1. `services/salary/constants.py:5` — 唯一 SoT 候選
  2. `services/overtime_pay_calculator.py:31` — 月薪制加班費實際使用
  3. `api/overtimes.py:95` — 區域常數，宣告後未使用（**dead code**，2026-05-28 DRIFT check 補列）
- 三者值相同但無共用 import；若日後勞基法修改基準日數，第 2 處遺漏即造成加班費錯誤
- **建議修法（給另一位開發者）**：
  1. 刪除 `api/overtimes.py:95` 該行（dead code，無 caller，刪除即解）
  2. 將 `services/overtime_pay_calculator.py:31` 的區域定義改為 `from services.salary.constants import MONTHLY_BASE_DAYS`
  3. 跑 `pytest tests/ -k "overtime or salary"` 確認無 regression
- **接受標準**：`grep -rn "MONTHLY_BASE_DAYS = 30" --include="*.py" .` 結果僅剩 `services/salary/constants.py:5` 一處

### BR-11：最低工資版本管理 [needs review]
- `get_minimum_wage(at_date)` 簽章已預留 `at_date` 參數，但 line 20 `del at_date` 表示尚未實作年度版本對應
- 目前固定回傳 `(29500, 196)`；若基本工資調整，須修改硬編碼常數
- 設計意圖（hook 預留）vs. 實作完整度（無歷史版本表）不一致

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| v0.1 | 2026-05-28 | Initial draft：時薪基底公式、勞基法 §24 分段計費、§32 II 月度 46h 上限、§21 最低工資守門、§38 IV 未休特休折算；才藝老師時薪明細獨立流程；標記 `MONTHLY_BASE_DAYS` 重複定義與最低工資版本管理為 [needs review]。 |
| v0.1 | 2026-05-28 | DRIFT delta：BR-10 補列第 3 處 `MONTHLY_BASE_DAYS = 30` 於 `api/overtimes.py:95`（dead code），加入「建議修法」與「接受標準」供另位開發者實作。 |

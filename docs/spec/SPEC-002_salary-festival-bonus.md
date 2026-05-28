# SPEC-002：薪資節慶獎金與會議扣款

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | `services/salary/festival.py`；及 `totals.py` / `engine.py` 內節慶相關呼叫；`utils.py` 內發放月 helper；`api/salary/festival.py`；`api/salary/manual_adjust.py` 節慶連動規則 |
| Related | SPEC-001（engine 主流程） |

## Overview

節慶獎金（`festival_bonus`）為園所對教職員的「比例制獎金」，依在籍人數達成率乘以職位基數計算，於每年 **2、6、9、12 月** 集中發放（對應發放期：2 月發 12+1 月、6 月發 2–5 月、9 月發 6–8 月、12 月發 9–11 月）。

業務目標：
- 與月薪（`gross_salary`）**完全隔離**：節慶獎金與超額獎金 / 主管紅利透過 `bonus_amount` 旗標走「另行轉帳」流程，不計入 `gross_salary`，因此也不影響淨薪計算與勞健保扣繳基礎。
- 與園務會議出勤狀況耦合：`meeting_absence_deduction`（園務會議缺席扣款）只從 `festival_bonus` 扣減，**不進入** `total_deduction`，避免出勤瑕疵連動影響月薪實領。
- 三類人員（主管 / 辦公室 / 帶班老師）走獨立計算路徑，但同樣套用「滿 3 個月年資門檻」與「事/病假累計 > 40h 全清零」的全勤性條件。

代碼來源：`services/salary/festival.py`（363 行純函式集合，被 `services/salary/engine.py` 的 `SalaryEngine` 委派呼叫）。

---

## Interface Definitions

### 內部 Python 函式（`services/salary/festival.py` 對外）

以下 11 個函式於 `services/salary/__init__.py` 公開 re-export，並由 `SalaryEngine` 以 thin wrapper（注入 config dict）轉呼叫；本檔僅描述 `festival.py` 自身簽章。

| # | 函式 | 簽章 | 用途 |
|---|------|------|------|
| 1 | `set_active_grade_map` | `(grade_map: Optional[dict]) -> None` | 由 `SalaryEngine.load_config_from_db()` / `_restore_config_state` 注入「職稱→獎金等級」對應表至 module-level cache；`None` 表清除注入退回 `POSITION_GRADE_MAP` fallback。執行緒安全由 caller 負責（engine 端已有 `_config_swap_lock`）。 |
| 2 | `_resolve_grade_map` | `(grade_map: Optional[dict] = None) -> dict` | 私有解析鏈：caller 帶入 > module cache > hardcode `POSITION_GRADE_MAP`。 |
| 3 | `get_position_grade` | `(position: str, grade_map: Optional[dict] = None) -> Optional[str]` | 由職位字串（如 `"幼兒園教師"`）查出等級 `"A"`/`"B"`/`"C"`；未知職位回 `None`。 |
| 4 | `get_festival_bonus_base` | `(position: str, role: str, bonus_base: dict) -> float` | 取得帶班教師的節慶獎金基數。`role` 接受 `head_teacher` / `assistant_teacher` / `art_teacher`。等級查不到時 fallback `"C"` 並 `logger.warning`；查到後若值為 `None`（DB NULL）以 `or 0` 防禦避免 `TypeError`。 |
| 5 | `get_target_enrollment` | `(grade_name: str, has_assistant: bool, is_shared_assistant: bool, target_map: dict) -> int` | 取得節慶獎金「在籍目標人數」。`grade_name` 為年級（大班/中班/小班/幼幼班）；`is_shared_assistant` 優先於 `has_assistant`，分別取 `shared_assistant` / `2_teachers` / `1_teacher`。 |
| 6 | `get_supervisor_dividend` | `(title: str, position: str, dividend_map: dict, supervisor_role: str = "") -> float` | 主管紅利金額（園長/主任/組長/副組長）。優先順序：`supervisor_role` > `position` > `title`；皆無命中回 `0`。注意：紅利在 engine 路徑屬於 `gross_salary` 一環（見 totals.py），但若同時為「獎金另行轉帳」也會列在 `bonus_amount`。 |
| 7 | `get_supervisor_festival_bonus` | `(title: str, position: str, bonus_map: dict, supervisor_role: str = "") -> Optional[float]` | 主管節慶獎金基數。命中規則同上；回 `None` 表「非主管路徑」（caller 用 `is not None` 判別走主管路徑 vs 一般辦公室路徑）。 |
| 8 | `get_office_festival_bonus_base` | `(position: str, title: str, office_map: dict) -> Optional[float]` | 司機/美編/行政 節慶獎金基數；命中規則同上。回 `None` 表非辦公室人員。 |
| 9 | `get_overtime_target` | `(grade_name: str, has_assistant: bool, is_shared_assistant: bool, target_map: dict) -> int` | 超額獎金目標人數（與節慶目標表不同；見 Business Rules）。 |
| 10 | `get_overtime_per_person` | `(role: str, grade_name: str, per_person_map: dict) -> float` | 超額獎金每人金額。 |
| 11 | `is_eligible_for_festival_bonus` | `(hire_date, reference_date=None, festival_months: int = 3) -> bool` | 入職年資門檻檢查。`hire_date` 接受 `date` 或 `'YYYY-MM-DD'` 字串；`reference_date` 預設為 `today_taipei()`；`festival_months` 預設 3（由 `BonusConfig`/`AttendancePolicy.festival_bonus_months` 覆寫）。`hire_date=None` 或字串格式錯誤皆回 `True`（容錯預設可領）。 |
| 12 | `calculate_overtime_bonus` | `(role, grade_name, current_enrollment, has_assistant, is_shared_assistant, overtime_target_map, overtime_per_person_map) -> dict` | 超額獎金計算。`role == "art_teacher"` 強制 `is_shared_assistant=True` 並改用 `assistant_teacher` 表查每人金額。回傳 `{overtime_bonus, overtime_target, overtime_count, per_person}`。 |
| 13 | `calculate_festival_bonus_v2` | `(position, role, grade_name, current_enrollment, has_assistant, is_shared_assistant, bonus_base, target_enrollment_map, overtime_target_map, overtime_per_person_map) -> dict` | 帶班老師節慶獎金主入口（v2 = 依職位等級+角色計算）。`role == "art_teacher"` 強制 `is_shared_assistant=True`；節慶獎金 = `base_amount × (current / target)`，`target <= 0` 時兩者皆歸 0；同時呼叫 `calculate_overtime_bonus`。回傳 `{festival_bonus, overtime_bonus, target, ratio, base_amount, overtime_target, overtime_count, overtime_per_person}`。 |

### 對外 HTTP 端點（節慶獎金預覽）

| 方法 | 路徑 | 權限 | 用途 |
|------|------|------|------|
| GET | `/salaries/festival-bonus` | `SALARY_READ` + 全員視野（非「僅本人薪資」） | 當月各員工節慶獎金 breakdown（呼叫 `engine.calculate_festival_bonus_breakdown`） |
| GET | `/salaries/festival-bonus/period-accrual` | `SALARY_READ` + 全員視野 | 該月所屬發放期累積至今的節慶/超額/會議扣款明細（呼叫 `engine.calculate_period_accrual_row`） |

兩端點在路由內以 `_resolve_salary_viewer_employee_id(current_user) is not None` 排除僅可看自己的角色（F-013）。

### Engine 端的計算入口（與 festival 直接相關）

| 函式 | 來源 | 用途 |
|------|------|------|
| `SalaryEngine.calculate_festival_bonus_v2` | `engine.py:1735` | thin wrapper 委派至 `_festival.calculate_festival_bonus_v2`，注入 `self._bonus_base` 等 config |
| `SalaryEngine._calculate_bonuses` | `engine.py:1398` | 將節慶獎金 / 超額獎金 / 主管紅利 / 生日禮金寫入 `breakdown`，含三類人員分流、發放月遮罩、override、全勤條件、`skip_payroll_bonuses` 短路 |
| `SalaryEngine._calculate_deductions` | `engine.py:1542` | 在發放月計算 `meeting_absence_deduction` 並從 `breakdown.festival_bonus` 扣減（`max(0, …)` 不為負） |
| `SalaryEngine._calculate_classroom_bonus_result` | `engine.py:1005` | 帶班老師統一節慶獎金計算入口（含跨班共用副班導 / 美師的「在籍人數加權平均」） |
| `SalaryEngine.calculate_festival_bonus_breakdown` | `engine.py:1879` | UI 預覽用：單員工×單月明細，回傳 `{name, category, bonusBase, targetEnrollment, currentEnrollment, ratio, festivalBonus, overtimeBonus, remark}` |
| `SalaryEngine.calculate_period_accrual_row` | `engine.py:2080` | 期間累積單月列：`{festival_bonus, overtime_bonus, meeting_absence_deduction, category}` |
| `SalaryEngine._compute_period_accrual_totals` | `engine.py:~2690`[unverified] | 發放月迭代呼叫 `calculate_period_accrual_row` 累加期間總額；每月 swap 對應月份的 `BonusConfig`/`AttendancePolicy`/`InsuranceRate` 版本（`config_for_month`） |
| `SalaryEngine._adjust_period_totals_for_discipline` | `engine.py:2714` | 從發放月期間累積總額扣減 pending 懲處；扣減順序為 **節慶獎金優先扣完再動超額**（業主慣例） |

---

## DTO Definitions

### `SalaryBreakdown`（`services/salary/breakdown.py:8`）

```python
@dataclass
class SalaryBreakdown:
    # 節慶與超額相關（皆 float, default=0）
    festival_bonus: float = 0
    overtime_bonus: float = 0
    supervisor_dividend: float = 0          # 主管紅利
    birthday_bonus: float = 0
    meeting_overtime_pay: float = 0          # 園務會議加班費
    meeting_absence_deduction: float = 0     # 園務會議未出席扣節慶獎金（註解原文）
    meeting_attended: int = 0
    meeting_absent: int = 0
    personal_sick_leave_hours: float = 0     # 事+病假累計（>40h 時取消獎金）
    # 獎金獨立轉帳
    bonus_separate: bool = False
    bonus_amount: float = 0
```

### `SalaryRecord` 節慶相關欄位（`models/salary.py:197+`）

| 欄位 | 型別 | 預設 | 註解（comment） |
|------|------|------|---------|
| `festival_bonus` | `Money` | `0` | 節慶獎金 |
| `overtime_bonus` | `Money` | `0` | 超額獎金 |
| `supervisor_dividend` | `Money` | `0` | 主管紅利（獨立轉帳） |
| `birthday_bonus` | `Money` | `0` | 生日禮金 |
| `meeting_overtime_pay` | `Money` | `0` | 園務會議加班費 |
| `meeting_absence_deduction` | `Money` | `0` | 園務會議缺席扣節慶獎金 |
| `bonus_separate` | `Boolean` | `False` | 獎金是否獨立轉帳 |
| `bonus_amount` | `Money` | `0` | 獨立轉帳獎金金額（`festival + overtime + supervisor_dividend`） |
| `bonus_config_id` | `FK -> bonus_configs.id` | NULL | 計算當下使用的獎金設定版本（稽核用） |
| `attendance_policy_id` | `FK -> attendance_policies.id` | NULL | 計算當下使用的考勤政策版本（稽核用） |

`SalarySnapshot` 同步擁有以上金額/計數欄位（`models/salary.py:367+`）。

### `BonusConfig`（`models/config.py:55`）— 節慶獎金可調設定

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| `head_teacher_ab` | `Float` | `2000` | 班導 A/B 等級基數 |
| `head_teacher_c` | `Float` | `1500` | 班導 C 等級基數 |
| `assistant_teacher_ab` | `Float` | `1200` | 副班導 A/B 等級基數 |
| `assistant_teacher_c` | `Float` | `1200` | 副班導 C 等級基數 |
| `art_teacher_festival` | `Float, NULL` | `NULL`（沿用 `FESTIVAL_BONUS_BASE["art_teacher"]["A"]=2000`） | 美語/才藝教師節慶獎金基數（A/B/C 同值） |
| `principal_festival` | `Float` | `6500` | 園長節慶獎金基數 |
| `director_festival` | `Float` | `3500` | 主任節慶獎金基數 |
| `leader_festival` | `Float` | `2000` | 組長節慶獎金基數 |
| `driver_festival` | `Float` | `1000` | 司機節慶獎金基數 |
| `designer_festival` | `Float` | `1000` | 美編節慶獎金基數 |
| `admin_festival` | `Float` | `2000` | 行政節慶獎金基數 |
| `principal_dividend` / `director_dividend` / `leader_dividend` / `vice_leader_dividend` | `Float` | `5000` / `4000` / `3000` / `1500` | 主管紅利 |
| `overtime_head_normal` / `overtime_head_baby` | `Float` | `400` / `450` | 班導超額獎金每人金額（大/中/小班 vs 幼幼班） |
| `overtime_assistant_normal` / `overtime_assistant_baby` | `Float` | `100` / `150` | 副班導超額獎金每人金額 |
| `school_wide_target` | `Integer` | `160` | 全校在籍目標人數（主管 / 辦公室路徑用） |
| `meeting_default_hours` | `Float, NULL` | `NULL`（沿用 `DEFAULT_MEETING_HOURS = 2`） | 每場園務會議計幾小時加班費。**不在 engine 內讀取**：由 `api/meetings.py` 直接讀並寫入 `MeetingRecord.overtime_pay` |
| `meeting_absence_penalty` | `Integer, NULL` | `NULL`（沿用 `DEFAULT_MEETING_ABSENCE_PENALTY = 100`） | 缺席園務會議扣節慶獎金金額 |
| `warning_deduction` | `Float` | `1000` | 警告一支懲處預設扣節慶/超額獎金 |
| `minor_offense_deduction` | `Float` | `3000` | 小過一支懲處預設扣節慶/超額獎金 |
| `major_offense_deduction` | `Float` | `0` | 大過一支懲處預設扣款（業主未定，由個案指定） |

### `AttendancePolicy` 節慶相關欄位（`models/config.py:27`）

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| `festival_bonus_months` | `Integer` | `3` | 入職滿幾個月才能領節慶獎金（注入 `is_eligible_for_festival_bonus`） |

**[unverified]** `AttendancePolicy` 其他欄位（`late_deduction` / `early_leave_deduction` / `missing_punch_deduction`）已 deprecated 不影響本 SPEC 範圍。

### 常數預設（`services/salary/constants.py`）

| 常數 | 值 | 用途 |
|------|-----|------|
| `POSITION_GRADE_MAP` | `{"幼兒園教師":"A","教保員":"B","助理教保員":"C"}` | 職位→等級對應 |
| `FESTIVAL_BONUS_BASE` | `head_teacher: A/B=2000 C=1500; assistant_teacher: A/B/C=1200; art_teacher: A/B/C=2000` | 帶班節慶獎金基數預設 |
| `TARGET_ENROLLMENT` | 大班 `{2_teachers:24,1_teacher:12,shared_assistant:20}`；中班 `{24,12,18}`；小班 `{24,12,16}`；幼幼班 `{15,7,12}` | 節慶獎金目標人數（**與超額目標不同**） |
| `OVERTIME_TARGET` | 大班 `{25,13,20}`；中班 `{23,12,18}`；小班 `{21,11,16}`；幼幼班 `{14,7,12}` | 超額獎金目標人數 |
| `OVERTIME_BONUS_PER_PERSON` | `head_teacher: 大/中/小=400 幼幼=450; assistant_teacher: 大/中/小=100 幼幼=150` | 超額獎金每人金額 |
| `SUPERVISOR_DIVIDEND` | `{園長:5000, 主任:4000, 組長:3000, 副組長:1500}` | 主管紅利預設 |
| `SUPERVISOR_FESTIVAL_BONUS` | `{園長:6500, 主任:3500, 組長:2000}` | 主管節慶獎金基數預設 |
| `OFFICE_FESTIVAL_BONUS_BASE` | `{司機:1000, 美編:1000, 行政:2000}` | 辦公室節慶獎金基數預設 |
| `DEFAULT_MEETING_ABSENCE_PENALTY` | `100` | 會議缺席每次扣款（fallback；由 `BonusConfig.meeting_absence_penalty` 覆寫） |
| `DEFAULT_MEETING_HOURS` | `2` | 每場會議加班時數（fallback；由 `BonusConfig.meeting_default_hours` 覆寫；**不在 engine 內讀取**） |

---

## Business Rules

### R-1：發放月規則

- 節慶獎金發放月 = `{2, 6, 9, 12}`，定義在 `services/salary/utils.py:195 get_bonus_distribution_month`。
- 非發放月一律於 `_calculate_bonuses` 中強制 `festival_bonus = 0, overtime_bonus = 0`（`engine.py:1499`）。
- 發放月「結算期間」對應：
  - **2 月** → 結算 `[(y-1, 12), (y, 1)]`
  - **6 月** → 結算 `[(y, 2), (y, 3), (y, 4), (y, 5)]`
  - **9 月** → 結算 `[(y, 6), (y, 7), (y, 8)]`
  - **12 月** → 結算 `[(y, 9), (y, 10), (y, 11)]`

  （定義於 `get_distribution_period_months`，與 `get_current_period_passed_months` 對齊）

- 業主 2026-04-25 確認：發放月當月以「期間累積總額」覆蓋當月單月計算（`period_festival_override` / `period_overtime_override`）。覆蓋值由 `_compute_period_accrual_totals` 逐月呼叫 `calculate_period_accrual_row` 累加；每月透過 `config_for_month` swap 對應月份的歷史 `BonusConfig`/`AttendancePolicy`/`InsuranceRate` 版本。

### R-2：節慶獎金與 `gross_salary` 完全隔離（**核心不變式**）

- `recompute_record_totals`（`services/salary/totals.py:23`）計算 `gross_salary` 時 **不含** `festival_bonus` 與 `overtime_bonus`；只含 `base_salary + hourly_total + performance_bonus + special_bonus + supervisor_dividend + meeting_overtime_pay + birthday_bonus + overtime_pay`。
- `_calculate_deductions` 在發放月扣減 `meeting_absence_deduction` 後仍只更新 `breakdown.festival_bonus`，**不**回寫 `gross_salary`（`engine.py:1676`）。
- engine 主路徑於 `calculate_salary` 也是同公式（`engine.py:1835`）：`gross_salary = base + performance + special + supervisor_dividend + birthday`（不含 festival / overtime）。
- `recompute_record_totals` 同時計算 `bonus_amount = festival_bonus + overtime_bonus + supervisor_dividend`，並設 `bonus_separate = bonus_amount > 0`。`bonus_amount` 為「顯示用聚合」，**不可** 直接當「另行轉帳金額」使用（因 `supervisor_dividend` 已包在 `gross_salary` 中，否則主管紅利會雙付）；實際另行轉帳列只取 `festival + overtime`（`services/salary/salary_slip.py`）[unverified — 見原始 totals.py:50-54 註解]。

### R-3：`meeting_absence_deduction` 只扣 `festival_bonus`，**不進** `total_deduction`

- 寫入點：`engine.py:1673-1678`
  ```python
  if get_bonus_distribution_month(month):
      absent_for_deduction = meeting_context.get("absent_period", absent)
      breakdown.meeting_absence_deduction = absent_for_deduction * self._meeting_absence_penalty
      breakdown.festival_bonus = max(0, breakdown.festival_bonus - breakdown.meeting_absence_deduction)
  ```
- `recompute_record_totals` 計算 `total_deduction` 時 **不含** `meeting_absence_deduction`（`totals.py:39-49`），僅含勞健保 + 勞退 + 遲到/早退/未打卡/請假/曠職/其他扣款。
- `calculate_salary` 主路徑 `total_deduction` 公式同樣不含（`engine.py:1685-1694`）。
- `max(0, …)` 保證 `festival_bonus` 不為負（即「會議扣款最多扣到 0，不會倒扣月薪」）。
- **僅發放月扣**：非發放月 `meeting_absence_deduction` 維持 `0`（受 `if get_bonus_distribution_month(month):` 守衛）；缺席記錄會累積至下一個發放月才結算。
- 「會議扣款區間」：發放月以 `meeting_context["absent_period"]` 為準（含當期前幾個月的缺席累計，與 `get_meeting_deduction_period_start` 對齊）；fallback 到當月 `absent`。
- 此規則的設計動機（推測）：節慶獎金為「比例制獎金 + 行為糾正獎金」，避免出勤瑕疵連動影響月薪實領；月薪走法定扣款另一個閘門。

### R-4：`bonus_separate` 旗標

- 主流程於 `calculate_salary`（`engine.py:1854-1858`）：
  ```python
  breakdown.bonus_separate = (festival_bonus + overtime_bonus + supervisor_dividend) > 0
  breakdown.bonus_amount = festival_bonus + overtime_bonus + supervisor_dividend
  ```
- `recompute_record_totals` 重算路徑：`bonus_separate = bonus_amount > 0`（等價）。
- 業務語意：當期間有任一獎金 > 0，視為「另行轉帳」案件，前端會分開顯示。

### R-5：節慶獎金資格門檻（年資 / 入職滿 N 月）

- 預設 `festival_bonus_months = 3`，由 `AttendancePolicy` 覆寫。
- 資格基準日為 **薪資月份的月底**（`_get_bonus_reference_date`，`engine.py:981`）。歷史 bug 修正：原以月份首日判斷，導致「2025-11-15 入職員工於 2026-02-28 已滿 3 個月，但以 2026-02-01 為準仍不足」誤排除。
- `hire_date = None` 或字串解析失敗 → 預設可領（`festival.py:219-227`）。
- 不符合年資時：帶班/主管/辦公室三條路徑皆寫 `festival_bonus = 0`、`overtime_bonus = 0`，但 `supervisor_dividend` 不受影響（主管紅利與職務掛鉤，不與出勤掛鉤）。
- 預覽端點 `calculate_festival_bonus_breakdown` 對未滿 3 月者 `remark = "未滿3個月"`。

### R-6：三類人員分流（`_calculate_bonuses`，`engine.py:1398`）

優先順序（嚴格序）：

1. **主管路徑**（`supervisor_festival_base is not None`）：
   - `festival_bonus = round_half_up(supervisor_festival_base × school_enrollment / school_wide_target)`
   - `school_wide_target = self._school_wide_target or 160`
   - `overtime_bonus = 0`（主管無超額獎金）
2. **辦公室路徑**（`office_staff_context and emp_position` 且 `office_base is not None`）：
   - 公式同主管（全校比例制）
   - `overtime_bonus = 0`
   - **注意**：`office_base == 0` 視為「設為 0」，`is None` 才視為「未設定（跳過此路徑）」
3. **帶班老師路徑**（`classroom_context and emp_position`）：呼叫 `_calculate_classroom_bonus_result`：
   - `festival_bonus = base_amount × (current_enrollment / target_enrollment)`，`target <= 0` 時兩者皆歸 0
   - `overtime_bonus = max(0, current - overtime_target) × overtime_per_person`
   - **共用副班導 / 跨班美師**：對所有共用班的計算結果 **按在籍人數加權平均**（`_calculate_classroom_bonus_result`，`engine.py:1030-1077`）。若總在籍 = 0 → 平均值歸 0 避免 `ZeroDivisionError`。
4. **舊版相容路徑**（`bonus_settings`）：走 `engine.calculate_bonus()`（`services/salary/deduction.py:82`）。`target <= 0` 會 `logger.warning` 並 `ratio = 0`。

### R-7：美師（`art_teacher`）特別處理

- `calculate_festival_bonus_v2`（`festival.py:322-325`）強制 `is_shared_assistant = True`（用 `shared_assistant` 目標人數欄位）。
- `calculate_overtime_bonus`（`festival.py:262-267`）強制 `is_shared_assistant = True`，**並** 用 `assistant_teacher` 表查每人金額（不是 `art_teacher`，因 `OVERTIME_BONUS_PER_PERSON` 沒有 `art_teacher` 鍵）。
- `FESTIVAL_BONUS_BASE["art_teacher"]` 有獨立基數（A/B/C 一律 2000，依「第十二條」**[unverified — 引自 constants.py:67 註解 `依第十二條一律 2000`]**）。

### R-8：等級未知時 fallback 為 C 級

- `get_festival_bonus_base`（`festival.py:69-75`）：`POSITION_GRADE_MAP` 查不到對應職位 → fallback `"C"` + `logger.warning`。
- 設計考量（原註解）：「正常分流（office/supervisor 走獨立路徑）下不應觸發；觸發代表分流誤判或 position 拼字異常」。
- DB NULL 防禦：`bonus_base[role].get(grade, 0) or 0`，避免 `None × ratio` 觸發 `TypeError` 中斷整批薪資。

### R-9：節慶/超額獎金全勤條件（事/病假）

- `_calculate_bonuses`（`engine.py:1510-1515`）：當月事假 + 病假累計 `> 40` 小時 → `festival_bonus = 0, overtime_bonus = 0`。
- 主管紅利 `supervisor_dividend` **不受此條件影響**（與職務掛鉤、不與出勤掛鉤）。
- 此規則 **僅在發放月當月** 檢查；`calculate_period_accrual_row` 累積期間 **不套用**（須以 tooltip 在 UI 聲明，見 `engine.py:2089-2091` 註解）。

### R-10：`skip_payroll_bonuses` 全面歸零

- `_calculate_bonuses`（`engine.py:1534-1540`）：員工檔旗標 `skip_payroll_bonuses=True` 時，在所有 override / 期間累積 / 全勤條件套用 **之後** 短路歸零下列：`festival_bonus`、`overtime_bonus`、`supervisor_dividend`、`birthday_bonus`、`performance_bonus`、`special_bonus`。
- 業主用途：總園長指示不薪轉、不作帳的特殊個案。基本薪 + 勞健保仍正常計算。

### R-11：生日禮金（壽星 $500）

- `_calculate_bonuses`（`engine.py:1518-1526`）：員工 `birthday` 月份 = 當前薪資月份 → `birthday_bonus = 500`。
- 落入 `gross_salary`（recompute_record_totals 公式內），非另行轉帳；亦受 `skip_payroll_bonuses` 影響。
- **[needs review]** `birthday_bonus` 金額硬編碼 `500`，未抽出常數也未進 `BonusConfig`；如業主未來想調整需修改程式碼。

### R-12：手動調整（`api/salary/manual_adjust.py`）連動規則

- HR 透過 `manual_adjust_salary` 編輯 `meeting_absence_deduction` 但 **未** 同時手動覆寫 `festival_bonus` 時：系統自動連動將 `festival_bonus` 重設為 `max(0, old_festival_bonus + old_meeting_absence - new_meeting_absence)`（`manual_adjust.py:226-235`）。
- 此連動產生的 `|delta|` 仍計入 `total_abs_delta`（避免「降 meeting_absence 連動 …」的 audit 失準）[unverified — 註解不完整]。
- 受 `manual_adjust` 動過的欄位被加入 `SalaryRecord.manual_overrides`，重算時 `_fill_salary_record` 跳過 `_apply` 寫入，並改走 `recompute_record_totals` 重算總額（`engine.py:179-182`）。

### R-13：歷史月份重算的「設定版本切片」

- `SalaryEngine.config_for_month`（with-context manager）以該月最後一日為時間切片，選 `created_at <= 月底` 中最新的 `BonusConfig`/`AttendancePolicy`/`InsuranceRate`；皆無則 fallback 最舊版本。
- `_snapshot_config_state` / `_restore_config_state` 完整 snapshot 受影響的 engine 屬性（含 `_bonus_base`、`_supervisor_festival_bonus`、`_office_festival_bonus_base`、`_meeting_absence_penalty`、`_position_grade_map` 等）。
- **稽核保證**：`SalaryRecord.bonus_config_id` / `attendance_policy_id` 必填，可回溯當下版本（`_fill_salary_record`，`engine.py:138-139`）。
- 已知攻擊面：admin 持 `SALARY_WRITE` 可建惡意金額且 `is_active=False` 的 `BonusConfig`/`InsuranceRate`，等歷史補算被 `id desc` 撿到。緩解依賴：(a) `BonusConfig` INSERT/UPDATE 必須走 `finance_approve` + audit；(b) `needs_recalc` 全標守衛。**[needs review — 緩解 (a) 在 `api/config.py` / `api/insurance.py` 待補]**。

### R-14：發放月期間累積扣 pending 懲處

- `_adjust_period_totals_for_discipline`（`engine.py:2714`）：
  - 從發放月累積總額扣減 `services.disciplinary.get_pending_actions` 的金額。
  - **扣減順序**：節慶獎金 **優先扣完** 才動超額（業主慣例，與 Excel 案例一致）。
  - 應扣 > 可用時截斷（最多扣到 0）。
- 發放月寫入 record 後由 `_mark_discipline_applied` 標記 pending 懲處為已抵扣，避免雙重扣款。

### R-15：四捨五入規則

- `festival.py` 所有金額輸出走 `utils.rounding.round_half_up`（`calculate_festival_bonus_v2` / `calculate_overtime_bonus` 回傳前皆呼叫）。
- engine 端 `_calculate_bonuses` 在覆蓋 override / 主管全校比例計算後再次 `round_half_up`，避免 float 累積誤差。

### R-16：發放月「在職員工」過濾規則

- `api/salary/festival.py` 兩端點皆以 `_active_employees_in_month_filter(year, month)` 過濾。
- 商業語意（`api/salary/festival.py:147-149` 註解）：節慶獎金以發放月當日在職為條件，**期中離職者即使已累積部分獎金亦不會於發放月領取**；預覽功能維持此規則，避免管理者對實發金額產生誤判。
- **[needs review]** 期中離職員工的「未領節慶獎金」處置（轉發 / 結清 / 棄領）未在程式碼中明確定義，可能涉及勞基法工資請求權，建議與業主確認 SOP。

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| v0.1 | 2026-05-28 | Initial draft，涵蓋 `services/salary/festival.py` 13 個對外函式 + `engine.py` 內 6 個 festival 相關方法 + `api/salary/festival.py` 2 個端點 + 16 條 business rules |

# 年終獎金 E 化重構 — 階段 2：自動推導（接專案資料 + 規則設定）

- 日期：2026-06-02
- 狀態：design draft，待 user review
- 範圍：後端為主（自動推導服務 + BonusConfig 設定擴充）；前端少量（設定頁欄位 + 試算回報未配對筆數）
- 前置：階段 1 已 ship（BE merge local main `17838e7`+`1f58393`、FE `6b31f682`）。本階段把階段 1「手填/沿用」的項目改為自動。

## 1. 背景

階段 1 把年終 6 步引擎接上線、做出 Excel 式總表 + 設定頁 + 兩關簽核，但下列獎金/扣款仍為 HR 手填（spec 1 §3.2 OUT）：才藝鼓勵、教課獎勵、節慶差額、學期紅利、考勤類扣款、班級舊生達成率。階段 2 將其中**資料源具備**者改為自動推導，並把所需「規則參數」集中到 `BonusConfig`。

## 2. 可行性總表（已逐項驗證，附 file:line 證據見 §5）

| # | 項目 | 判定 | 缺口 |
|---|---|---|---|
| ③ | 節慶差額 FESTIVAL_DIFF | 🟢 純自動 | 無（接 SalaryRecord 逐月已發 + 階段1 在園/目標 + 角色基數） |
| ⑤a | 考勤扣款（遲到/早退/事假/病假/會議缺席） | 🟢 資料齊 | 僅缺扣款費率設定 |
| ① | 才藝鼓勵 AFTER_CLASS_AWARD | 🟡 | 金額級距/單價設定 + 報名 classroom_id 未配對缺口（須回報非靜默少算） |
| ④ | 學期紅利 SEMESTER_DIVIDEND | 🟡 | 門檻/金額設定（才藝率算法已有、舊生率同⑥） |
| ⑥ | 班級舊生達成率 | 🟡 | 邏輯接 `enrollment_school_year`；prod backfill 完整度未定 → graceful fallback |
| ② | 教課獎勵 TEACHING_EXTRA | 🔴 **OUT** | `ActivityCourse` 無授課老師連結，資料源不存在 → 維持手填 |
| ⑤b | 自強/研習/尾牙缺席 | 🔴 **OUT** | 無員工層級活動出席表 → 維持手填 |

## 3. 範圍

### 3.1 IN
1. **自動推導服務**（5 項）：③ 節慶差額、⑤a 考勤扣款、① 才藝鼓勵、④ 學期紅利、⑥ 班級舊生達成率。
2. **BonusConfig 設定擴充**（規則參數集中）：才藝鼓勵金額級距 + 才藝老師單價、學期紅利門檻 + 金額、考勤扣款費率。前端 `BonusConfigPanel` 加對應欄位。
3. **build_settlements 整合**：refresh 階段呼叫上述服務，把值寫進 `special_bonus_items`（①③④）/ `settlement.deduction_*`（⑤a）/ `class_returning_rate`（⑥），**全部尊重手動覆寫**。
4. **① 未配對回報**：試算結果回傳「未配對報名筆數」，前端 Grid 試算後顯示提醒。

### 3.2 OUT（維持手填，spec 註明未來資料基建）
- ② 教課獎勵：需先補 `ActivityCourse` 授課老師指派（新欄位/關聯表）+ 園所實際指派習慣。
- ⑤b 自強/研習/尾牙缺席：需先補員工層級活動出席登錄表 + 園所登錄習慣。
- 這兩項在年終總表/設定頁維持階段 1 的手填（manual patch / special_bonus CRUD），spec 標「未來階段：先導入登錄基建」。

## 4. 架構與資料流

```
build_settlements(refresh_rates=True) 的 refresh 階段擴充：
  services/year_end/auto_derive.py（新，編排）逐員工/逐班：
    ① after_class_award(db, cycle, classroom) → upsert special_bonus_items(AFTER_CLASS_AWARD)
    ③ festival_diff(db, cycle, employee)      → upsert special_bonus_items(FESTIVAL_DIFF)（可負）
    ④ semester_dividend(db, cycle, employee)  → upsert special_bonus_items(SEMESTER_DIVIDEND_*)
    ⑤a attendance_deductions(db, cycle, emp)  → 寫 settlement.deduction_late/personal/sick/meeting
    ⑥ class_returning_rate(db, cycle, class)  → 寫 ClassEnrollmentTarget.returning_student_rate
  → 之後 compute_settlement 照常跑（已讀上述值）

override 原則（沿用階段 1）：
  - special_bonus_items：若該筆 source_ref 標記為手動（manual）或 calc_meta.manual=true → 不覆寫
  - settlement.deduction_*：若 calc_meta 有對應 *_override → 用 override（manual patch 設定）
  - returning_student_rate：若該班學生 enrollment_school_year 不完整 → fallback 沿用既有手填值（不寫半套）
```

**核心原則**：自動推導只是把 refresh 從「只算達成率」擴成「同時算各獎金/扣款」；single source 仍是 `special_bonus_items` / `settlement` / `ClassEnrollmentTarget`；手動覆寫永遠優先。

## 5. 各項算法、資料源、Excel map

### ① 才藝鼓勵（AFTER_CLASS_AWARD）— `services/year_end/auto_derive/after_class_award.py`
- 資料源：`models/activity.py:205 RegistrationCourse(registration_id, course_id, status)` + `activity.py:132 ActivityRegistration.classroom_id` + `activity.py:110 class_name`。班導 `ClassEnrollmentTarget.head_teacher_employee_id`。
- 算法（對齊 Excel「鼓勵課後才藝統計表」）：每班 J = `COUNT(RegistrationCourse)` JOIN registration WHERE `classroom_id=班 AND status IN ('enrolled','promoted_pending') AND school_year=N AND semester=上`（**人次，用 COUNT 非 distinct**——勿用 appraisal `status_aggregator.py:269` 的 distinct 學生數）。獎勵金 L = J × K，**K 為每班才藝鼓勵單價（園所依班別/年齡組設定，非由在籍人數推導）**——Excel 驗證：天堂鳥(22人)K=75、牡丹(13)K=85、芙蓉(17)K=110，與在籍人數非單調，是大/中/小/幼班單價。發給該班班導 → special_bonus_items(AFTER_CLASS_AWARD, classroom_id, calc_meta={J, K})。
- 才藝老師（Jocelyn/Katrina）：全校總人次 × 單價（設定）→ 另寫 special_bonus_items；老師身分由設定指定（無 course→teacher 連結）。
- **未配對缺口**：`classroom_id IS NULL` 或 `match_status != matched`（`activity.py:143`）的報名不計 → 回傳 `unmatched_count`，試算回報。

### ③ 節慶差額（FESTIVAL_DIFF）— `services/year_end/auto_derive/festival_diff.py`
- 資料源：`models/salary.py:197 SalaryRecord.festival_bonus`（`uq` emp+year+month）逐月已發；階段 1 `enrollment_rates.count_enrolled_on` / `class_performance_rate`；`settlement_builder.festival_base_for_role`。
- 算法（對齊 Excel「節慶獎金比例差額」）：對 8 月～次年 1 月每月 m：應領 = 角色節慶基數 × (當月在園 / 目標)；差額_m = 應領 − `SalaryRecord(emp, m).festival_bonus`；6 月加總 → special_bonus_items(FESTIVAL_DIFF, 可負)。
- **決策待確認**：分母「目標」班導用班級編制（`ClassEnrollmentTarget.head_count_target`）、非帶班用全校目標（`OrgYearSettings.enrollment_target`）——對齊 Excel D 欄（班導用班目標、辦公室用 160）。

### ④ 學期紅利（SEMESTER_DIVIDEND_FIRST/SECOND）— `services/year_end/auto_derive/semester_dividend.py`
- 才藝率：復用 `services/appraisal/status_aggregator.py:241 _aggregate_activity_rate`（此處 distinct 學生＝參加率，語意對）。舊生率：同⑥。
- 算法：紅利 = (舊生率 ≥ 舊生門檻 ? 紅利_舊生 : 0) + (才藝率 ≥ 才藝門檻 ? 紅利_才藝 : 0)，逐學期寫 special_bonus_items。門檻 + 金額（500/1000）為設定。

### ⑤a 考勤扣款 — `services/year_end/auto_derive/attendance_deductions.py`
- 資料源：遲到/早退 `models/attendance.py:53 is_late / :54 is_early_leave`；事假 `models/leave.py PERSONAL` + `leave_hours`（approved）；病假 `SICK`（育嬰假：LeaveType 僅 MATERNITY/PATERNITY，無 parental——須與 HR 確認對應）；會議缺席 `models/event.py:87 MeetingRecord.attended=false`。
- 算法：各項次數/天數 × 設定費率 → 寫 settlement.deduction_late / deduction_personal_leave / deduction_sick_leave / deduction_meeting（皆負）。
- **勿用** appraisal `status_aggregator.py:141 Attendance.status=='absent'` 當事/病假源（不分類）；用 `LeaveRecord.leave_type`。
- **費率結構決策**：Excel 遲到呈現級距（5次-300…）但年終扣款多為逐月已算之加總；本階段以「可設定費率（每次/每日）」為主；若園所用級距，模型為 tiers（依 HR 實際費率表設定，settings setup 時確認）。

### ⑥ 班級舊生達成率 — `services/year_end/auto_derive/returning_rate.py`
- 資料源：`models/classroom.py:158 Student.enrollment_school_year`（永久身分鍵）。
- 算法：某班舊生 = 該班學生中 `enrollment_school_year < cycle.academic_year` 者；舊生率 = 舊生數 / 目標（`ClassEnrollmentTarget` 目標欄）。寫 `ClassEnrollmentTarget.returning_student_rate`（階段 1 為手填值）。
- **避陷阱**：勿用考核 `status_aggregator.py ClassRetentionAggregate.retention_rate`（留校率，語意不同）。
- **graceful fallback**：若該班有學生 `enrollment_school_year IS NULL`（prod backfill 未完成）→ 不自動寫，沿用既有手填值；試算回報「N 班因學號未回填沿用手填」。

## 6. 設定（擴充 `models/config.py BonusConfig` + 前端 BonusConfigPanel）

新增欄位（版本化沿用既有 BonusConfig 機制）：
- 才藝鼓勵：`after_class_award_unit_price`（每班/年齡組單價 K，建議鍵為 classroom_id 或年齡組——settings setup 時確認園所是用班還是年齡組）、`art_teacher_unit_price`（才藝老師單價）
- 學期紅利：`dividend_returning_threshold`、`dividend_returning_amount`(500)、`dividend_activity_threshold`、`dividend_activity_amount`(1000)
- 考勤扣款：`late_deduction_per_time`、`personal_leave_deduction_per_day`、`sick_leave_deduction_per_day`（會議缺席已有 `meeting_absence_penalty` / `OrgYearSettings.meeting_absence_deduction`）
- 前端 `BonusConfigPanel` 加「年終規則」分頁；金流硬化沿用既有（reason + ACTIVITY_PAYMENT_APPROVE）。

## 7. 測試策略

| 層 | 涵蓋 |
|---|---|
| 各 auto_derive 純函式/查詢測試 | 每項用 Excel 真實 case 對帳（才藝鼓勵逐班 J×K、節慶差額逐月、紅利門檻、考勤扣款、舊生率）容差 ≤1 |
| build 整合 | refresh 後 special_bonus_items/deduction/returning_rate 自動寫入；**override 不被覆寫**（核心回歸）；⑥ fallback；① unmatched_count 回報 |
| 前端 | BonusConfigPanel 年終規則欄位存取；Grid 試算後顯示 unmatched/fallback 提醒 |

**驗收金標準**：用 `114年年終經營績效.xls` 各組成 sheet 逐項對帳；自動值與 Excel ≤1；手動覆寫優先驗證。

## 8. 決策（已與 user 對齊）
1. **自動但可覆寫**：自動推導不覆寫手動值（沿用階段 1 override pattern）。
2. **設定放 BonusConfig**（與既有獎金設定同處）。
3. **⑥ graceful fallback**：enrollment_school_year 不完整則沿用手填，不寫半套。
4. **②教課 + ⑤b 活動出席 OUT**：維持手填，未來補登錄基建。

## 9. 影響檔案
**後端**：`services/year_end/auto_derive/`（新，5 子模組 + 編排）、`services/year_end/settlement_builder.py`（refresh 擴充呼叫）、`models/config.py`（BonusConfig 加欄）、`api/config/bonus.py`（schema）、alembic（BonusConfig 加欄 migration）、tests。
**前端**：`BonusConfigPanel.vue`（年終規則分頁）、`YearEndGridView.vue`（試算後 unmatched/fallback 提醒）、schema.d.ts regen、tests。

## 10. 不在本 spec（未來）
- ② 教課獎勵自動化（先補 ActivityCourse 授課老師指派基建 + 園所指派習慣）
- ⑤b 自強/研習/尾牙缺席自動化（先補員工活動出席登錄）
- 考勤扣款級距費率表（若 HR 採級距而非每次/每日）

# 年終獎金 E 化 階段 2（自動推導）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** 把階段 1 手填的 5 項（③節慶差額、⑤a考勤扣款、①才藝鼓勵、④學期紅利、⑥舊生率）改為「build 時自動從專案資料推導」，規則參數集中到 BonusConfig，**手動覆寫永遠優先**。

**Architecture:** 新增 `services/year_end/auto_derive/`（5 子模組 + 編排），由 `settlement_builder.build_settlements` 的 refresh 階段呼叫，寫入 `special_bonus_items`（①③④）/ `settlement.deduction_*`（⑤a）/ `ClassEnrollmentTarget.returning_student_rate`（⑥）。BonusConfig 加規則欄位。②教課 + ⑤b 活動出席 OUT（維持手填）。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、pytest、Decimal HALF_UP；前端 Vue3 TS + Vitest。

**對應 spec：** `docs/superpowers/specs/2026-06-02-year-end-bonus-e-automation-phase2-design.md`

**驗收金標準：** 用 `114年年終經營績效.xls` 各 sheet 逐項對帳（≤1）；手動覆寫不被自動蓋掉（核心回歸）。

**worktree base：** 從 **local main**（依賴階段 1 已 merge 的 settlement_builder/special_bonus_items/enrollment_rates，只在 local main）。

---

## File Structure
| 檔案 | 動作 |
|---|---|
| `models/config.py` BonusConfig | 加規則欄位（才藝單價/紅利門檻金額/考勤費率） |
| `alembic/versions/<rev>_bonusconfig_year_end_rules.py` | new（加欄，nullable + default） |
| `services/year_end/auto_derive/__init__.py` | new（編排 `derive_all(db, cycle) -> DeriveReport`） |
| `services/year_end/auto_derive/after_class_award.py` ①, `festival_diff.py` ③, `semester_dividend.py` ④, `attendance_deductions.py` ⑤a, `returning_rate.py` ⑥ | new |
| `services/year_end/settlement_builder.py` | refresh 階段呼叫 derive_all（尊重 override）+ 回 report |
| `api/config/bonus.py` + schemas | 加規則欄位 |
| `tests/test_year_end_auto_derive_*.py` | new（逐項 Excel 對帳 + override 回歸） |
| 前端 `BonusConfigPanel.vue`、`YearEndGridView.vue`、schema.d.ts | 設定欄位 + 試算提醒 |

---

## Task B1: BonusConfig 規則欄位 + migration

**Files:** `models/config.py`, `alembic/versions/<rev>_*.py`, `api/config/bonus.py`(+schema), `tests/test_bonus_config_year_end_rules.py`

- [ ] **Step 1: 失敗測試**
```python
def test_bonus_config_year_end_rule_fields(db_session):
    cfg = make_bonus_config(
        art_teacher_unit_price=30,
        dividend_returning_threshold=Decimal("0.9"), dividend_returning_amount=500,
        dividend_activity_threshold=Decimal("0.8"), dividend_activity_amount=1000,
        late_deduction_per_time=100, personal_leave_deduction_per_day=500,
        sick_leave_deduction_per_day=500,
        after_class_award_unit_price={"天堂鳥": 75, "牡丹": 85},  # JSON: 班名→K
    )
    assert cfg.dividend_returning_amount == 500
```
- [ ] **Step 2: FAIL** — `python3 -m pytest tests/test_bonus_config_year_end_rules.py -v`
- [ ] **Step 3: 實作** — `models/config.py BonusConfig` 加欄（皆 nullable / 合理 default，沿用既有 Float/JSON 慣例）：`art_teacher_unit_price`、`dividend_returning_threshold/amount`、`dividend_activity_threshold/amount`、`late_deduction_per_time`、`personal_leave_deduction_per_day`、`sick_leave_deduction_per_day`、`after_class_award_unit_price`(JSON 班名/年齡組→K)。alembic 加欄 migration（nullable + server_default，含 downgrade）。`api/config/bonus.py` PUT/GET schema + 複製欄位邏輯（沿用既有版本化 copy）。
- [ ] **Step 4: PASS** + 既有 bonus config 測試不回歸
- [ ] **Step 5: Commit** — `feat(year-end): BonusConfig 加 phase2 規則欄位(才藝單價/紅利門檻/考勤費率)`

> migration 含 backfill 風險低（純加欄）；但合併前須 `alembic upgrade heads`（記取既有教訓）。

---

## Task B2: ① 才藝鼓勵 after_class_award

**Files:** `services/year_end/auto_derive/after_class_award.py`, `tests/test_year_end_auto_derive_after_class.py`

- [ ] **Step 1: 失敗測試（Excel 對帳）**
```python
def test_after_class_award_per_class(db_session, seed_cycle_114, seed_activity_regs):
    # 天堂鳥(班導林佳穎) 才藝報名人次 J=25, K=75 → 1875
    # 牡丹(陳品棻) J=13, K=85 → 1105
    report = aca.derive_after_class_award(db_session, cycle_114)
    items = _special_items(db_session, cycle_114, bonus_type='AFTER_CLASS_AWARD')
    assert _amount_for(items, emp_lin_jy) == Decimal("1875")
    assert _amount_for(items, emp_chen) == Decimal("1105")

def test_after_class_award_reports_unmatched(db_session, seed_cycle_114):
    # classroom_id IS NULL 的報名 → 不計、回報 unmatched_count
    ...
    assert report.unmatched_count == 2
```
- [ ] **Step 2: FAIL**
- [ ] **Step 3: 實作** — `derive_after_class_award(db, cycle) -> AcaReport(written, unmatched_count)`：每班 J = `COUNT(RegistrationCourse)` JOIN ActivityRegistration WHERE `classroom_id=班 AND status IN ('enrolled','promoted_pending') AND school_year=cycle.academic_year AND semester=上`（**COUNT 非 distinct**）。K = `BonusConfig.after_class_award_unit_price[班名/年齡組]`。L = J×K → upsert special_bonus_items(AFTER_CLASS_AWARD, employee=班導, classroom_id, calc_meta={J,K})，**若該筆已 manual 則跳過**。`classroom_id IS NULL` / `match_status!='matched'` 的報名計入 unmatched_count（不計獎金）。才藝老師單價另算（設定指定老師 + 全校總人次 × art_teacher_unit_price）。
- [ ] **Step 4: PASS**（1875/1105/2465 等對帳）
- [ ] **Step 5: Commit** — `feat(year-end): 才藝鼓勵自動推導(人次×單價,回報未配對)`

---

## Task B3: ③ 節慶差額 festival_diff

**Files:** `services/year_end/auto_derive/festival_diff.py`, `tests/test_year_end_auto_derive_festival_diff.py`

- [ ] **Step 1: 失敗測試**
```python
def test_festival_diff_sum_over_six_months(db_session, seed_cycle_114, seed_monthly):
    # 蔡宜倩：逐月 應領(基數×在園/目標) − 已發(SalaryRecord.festival_bonus)，8月~1月加總 = 1975
    fd.derive_festival_diff(db_session, cycle_114)
    assert _amount_for(items, emp_tsai, 'FESTIVAL_DIFF') == Decimal("1975")
```
- [ ] **Step 2: FAIL**
- [ ] **Step 3: 實作** — 對 cycle 對應的 8 月~次年 1 月每月 m：在園 = 班導用 `enrollment_rates`(該班 month-end) / 非帶班用全校；目標 = 班導 `ClassEnrollmentTarget.head_count_target` / 非帶班 `OrgYearSettings.enrollment_target`；應領 = `festival_base_for_role` × (在園/目標)；已發 = `SalaryRecord(emp,year,m).festival_bonus`（無則 0）；差額 = 應領−已發；6 月加總 → upsert special_bonus_items(FESTIVAL_DIFF, 可負)，manual 則跳過。
- [ ] **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat(year-end): 節慶差額自動推導(逐月應領vs已發加總)`

---

## Task B4: ④ 學期紅利 semester_dividend

**Files:** `services/year_end/auto_derive/semester_dividend.py`, tests

- [ ] **Step 1: 失敗測試**
```python
def test_semester_dividend_thresholds(db_session, seed_cycle_114):
    # 舊生率≥門檻→500 + 才藝率≥門檻→1000；蔡宜倩上學期 = 1500
    sd.derive_semester_dividend(db_session, cycle_114)
    assert _amount_for(items, emp_tsai, 'SEMESTER_DIVIDEND_FIRST') == Decimal("1500")
```
- [ ] **Step 2: FAIL**
- [ ] **Step 3: 實作** — 才藝率復用 `services/appraisal/status_aggregator._aggregate_activity_rate`（distinct 學生＝參加率）；舊生率用 B6 的 returning_rate。紅利 = (舊生率≥`dividend_returning_threshold`?`dividend_returning_amount`:0)+(才藝率≥`dividend_activity_threshold`?`dividend_activity_amount`:0)，逐學期 upsert special_bonus_items(SEMESTER_DIVIDEND_FIRST/SECOND)，manual 跳過。
- [ ] **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat(year-end): 學期紅利自動推導(舊生率+才藝率門檻)`

---

## Task B5: ⑤a 考勤扣款 attendance_deductions

**Files:** `services/year_end/auto_derive/attendance_deductions.py`, tests

- [ ] **Step 1: 失敗測試**
```python
def test_attendance_deductions(db_session, seed_cycle_114, seed_attendance):
    # 蔡佩汶 遲到 71 次 × 100 = -7100（對帳 Excel O 欄）
    ad.derive_attendance_deductions(db_session, cycle_114)
    st = _settlement(db_session, cycle_114, emp_pwf)
    assert st.deduction_late == Decimal("-7100")
```
- [ ] **Step 2: FAIL**
- [ ] **Step 3: 實作** — 遲到次數 = `COUNT(Attendance WHERE is_late AND 期間)` × `late_deduction_per_time`；事假 = `SUM(LeaveRecord.leave_hours WHERE leave_type=PERSONAL AND approved)` 換算天 × `personal_leave_deduction_per_day`；病假同(SICK)；會議缺席 = `COUNT(MeetingRecord WHERE attended=false)` × `meeting_absence`。寫 settlement.deduction_late/personal_leave/sick_leave/meeting（皆負），**若 calc_meta 有 *_override 則用 override**（manual patch 設的）。期間 = 民國日曆年（對齊 proration，與階段 1 一致）。
- [ ] **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat(year-end): 考勤扣款自動推導(遲到/事假/病假/會議,尊重override)`

> 育嬰假：LeaveType 無 parental，僅 MATERNITY/PATERNITY → 與 HR 確認對應哪個 leave_type；spec 已標。

---

## Task B6: ⑥ 班級舊生率 returning_rate（含 graceful fallback）

**Files:** `services/year_end/auto_derive/returning_rate.py`, tests

- [ ] **Step 1: 失敗測試**
```python
def test_returning_rate_from_enrollment_school_year(db_session, seed_cycle_114):
    # 某班 9 生中 7 個 enrollment_school_year < 114 → 舊生率 7/目標
    rr.derive_returning_rate(db_session, cycle_114)
    ct = _class_target(db_session, cycle_114, classroom_id, semester_first=True)
    assert ct.returning_student_rate == Decimal("0.926")  # 對帳 Excel N 欄

def test_returning_rate_fallback_when_backfill_incomplete(db_session, seed_cycle_114):
    # 班內有學生 enrollment_school_year IS NULL → 不寫,沿用既有手填,report.fallback_classes+=1
    ct.returning_student_rate = Decimal("0.95")  # 既有手填
    report = rr.derive_returning_rate(db_session, cycle_114)
    assert ct.returning_student_rate == Decimal("0.95")  # 未被覆寫
    assert report.fallback_classes == 1
```
- [ ] **Step 2: FAIL**
- [ ] **Step 3: 實作** — 某班舊生 = 該班學生中 `enrollment_school_year < cycle.academic_year`；率 = 舊生數/目標。**若該班任一在籍學生 `enrollment_school_year IS NULL` → 不寫（fallback 沿用既有手填）+ report.fallback_classes++**。勿用考核 retention_rate。
- [ ] **Step 4: PASS**
- [ ] **Step 5: Commit** — `feat(year-end): 班級舊生率自動推導(enrollment_school_year,未回填則fallback)`

---

## Task B7: build_settlements 整合 derive_all + override + report

**Files:** `services/year_end/auto_derive/__init__.py`, `services/year_end/settlement_builder.py`, `tests/test_year_end_auto_derive_integration.py`

- [ ] **Step 1: 失敗測試（核心：自動 + override 不被蓋）**
```python
def test_build_auto_derives_all(db_session, seed_full_114):
    sb.build_settlements(db_session, 114, set(), 1, refresh_rates=True)
    # special_bonus_items 自動寫入 AFTER_CLASS_AWARD/FESTIVAL_DIFF/SEMESTER_DIVIDEND；deduction 自動
    ...
def test_manual_override_not_clobbered_by_auto(db_session, seed_full_114):
    # 先 manual patch 設 deduction_disciplinary=-6000 + 一筆 manual EXCESS → build 後仍在
    sb.build_settlements(db_session, 114, set(), 1)
    assert st.deduction_disciplinary == Decimal("-6000")
    assert _manual_excess_preserved()
def test_derive_report_surfaces_unmatched_and_fallback(db_session, seed_full_114):
    res = sb.build_settlements(db_session, 114, set(), 1)
    assert res.derive_report.unmatched_count >= 0 and res.derive_report.fallback_classes >= 0
```
- [ ] **Step 2: FAIL**
- [ ] **Step 3: 實作** — `auto_derive/__init__.py derive_all(db, cycle) -> DeriveReport(unmatched_count, fallback_classes, ...)` 依序呼叫 B2-B6。`settlement_builder.build_settlements` 在 refresh 後（compute_settlement 前）呼叫 derive_all；`BuildResult` 加 `derive_report`。override 規則：special_bonus_items 的 manual 筆（calc_meta.manual / source_ref 標記）不覆寫；deduction 有 calc_meta override 用 override（與階段 1 一致）。
- [ ] **Step 4: PASS** + `python3 -m pytest tests/ -k "year_end" -q`（零回歸，含階段 1 對帳蔡/林/郭仍過）
- [ ] **Step 5: Commit** — `feat(year-end): build refresh 整合 5 項自動推導(override優先+report)`

---

## Task F1（前端）: BonusConfigPanel 年終規則分頁 + regen

**Files:** `ivy-backend dump_openapi` → `ivy-frontend schema.d.ts`（絕對路徑 regen）、`src/views/.../BonusConfigPanel.vue`、`src/api/config.ts`、tests

- [ ] regen（worktree 用 `npx openapi-typescript /abs/path/openapi.json ... --alphabetize`）→ 加「年終規則」分頁（才藝單價/紅利門檻金額/考勤費率）→ vitest + typecheck + commit。

## Task F2（前端）: GridView 試算後顯示 unmatched/fallback 提醒

**Files:** `src/views/yearEnd/YearEndGridView.vue`、test

- [ ] build 回的 `derive_report.unmatched_count`/`fallback_classes` > 0 時，試算成功訊息附提醒（「N 筆才藝報名未配對班級未計入」「N 班學號未回填沿用手填舊生率」）→ vitest + commit。

---

## Self-Review（對 spec §3/§5/§8 核對）
- ③⑤a①④⑥ 各一 Task（B3/B5/B2/B4/B6）+ 設定 B1 + 整合 B7 + 前端 F1/F2 ✅
- override 優先：B5/B7 測試鎖定 ✅；②教課 + ⑤b 活動出席 OUT（不在任務）✅
- 每項 Excel 對帳測試（1875/1105/1975/1500/-7100/0.926）✅
- 待 HR 數值（K表/門檻/費率/育嬰假對應/節慶分母）= settings setup + B5 註記，不卡實作 ✅

## Execution Handoff
後端 B1-B7 先（subagent 逐任務，worktree 從 local main）→ 前端 F1-F2。每項 Excel 對帳；B7 override 回歸為最重要 gate。

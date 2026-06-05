# 全 114 學年假資料 seed 設計（dev 手測/展示用）

- 日期：2026-06-05
- 目標：讓 dev DB 每個**使用者面對的模組**都有橫跨整個 114 學年（上學期 2025/08–2026/01 + 下學期 2026/02–2026/07）的可看資料，供手測與展示。
- 範圍決策（已與 user 對齊）：① 完整 114 學年（上+下）② 保留並補強現有 162 生 / 21 員工（idempotent，不刪不動現有）③ 涵蓋全部使用者模組（含冷門）。

## 現況（dev DB，alembic head=`yebnd01`）
- 已有：11 班（全 tagged school_year=114, semester=2）、162 active 學生、21 員工、7 users、253 guardians、員工考勤 894、排班 209、薪資 47（最完整月份 2026/3 與 2026/6 各 21）、學費 960、活動課 8 / 報名 63、公告 8、會議 57、評量 80、事件 20、接送 29。
- 幾乎全空（要補）：**學生每日出缺勤(1)**、員工請假(4)、加班(1)、學生請假(0)、聯絡簿(0)、成長報告(0)、觀察(1)、里程碑(3)、生長量測(1)、用藥(0)、特教IEP(0)、招生訪視(0)、競品(0)、家長詢問(0)、員工證照/合約/學歷(0)、離職(0)、獎懲(0)、DSR(0)、繳費分錄/活動繳費(0~1)。
- **整個上學期(114/1)無任何資料**。

## 做法
1. **擴充 `scripts/seed_test_data_114_2.py` 加 `--term {114_1,114_2,all}`**：
   - 把學期常數（SCHOOL_YEAR/SEMESTER/PERIOD/TERM_START/TERM_END/TODAY）收進 `TERMS` dict + `set_term(key)`，呼叫時切換。
   - 步驟分兩類：**一次性**（students 班級遷移、guardians）只跑一次；**逐學期**（fees / attendance / shifts / overtime / activities / announcements / student_records / allowances / salary / meetings）逐學期跑。
   - 參數化既有 hardcode 日期：fee 名稱「上/下學期」+ 繳費日、salary 以 2026/3 為範本鋪該學期各月（salary_year 隨月份跨 2025/2026）、meeting/overtime 日期由 term 推導。
   - **新增 2 個逐學期步驟**：`step_student_attendance`（學生每日出缺勤，bulk_insert，per-student-term gate，約 88% 出席/8% 請假/3% 遲到/1% 曠課）、`step_employee_leaves`（員工請假，每學期數筆）。
   - 上學期 today=2026/01/20（全過去，鋪滿）；下學期 today=2026/06/05（**不生未來**，修掉 stale 的 `TODAY=2026/04/19`）。
2. **新增 `scripts/seed/` 套件**裝冷門模組，每檔一支冪等 `step()`，import `scripts/seed/_common.py` 共用 helper。平行 agent 各寫一檔互不衝突；`__init__.py` 與 runner 由整合者彙整（agent 不碰）。
3. **runner `scripts/seed_full_year.py`** 依序跑：主腳本逐學期步驟 → seed 套件冷門模組。

## 冷門模組（agent 一檔一支 `step()`）
聯絡簿（entry/ack/reply/template）、學生請假、成長報告+觀察+里程碑、生長量測+用藥+過敏、特教IEP+文件+補助、招生（period/month→visits→競品→家長詢問）、員工檔案（證照/合約/學歷）、離職+獎懲、個資（DSR+consent log）、考核完整 cycle+年終 cycle、繳費分錄（student_fee_payments+activity_payment_records）。

## 冪等與安全契約
- 每步先 `exists` 查再寫；重跑不重複、**不動/不刪現有資料**。
- 不生未來資料（下學期上限 = 今天 2026-06-05）。
- 約束：year_end 金額落 ±100萬 CHECK 內；`lifecycle_status` 一律走 `utils/student_lifecycle.set_lifecycle_status`，不 raw UPDATE。
- 保留現有 admin/teacher/parent 測試帳號。
- agent 只建自己的 `scripts/seed/<module>.py`，**不改** `__init__.py`/runner/主腳本/其他 agent 檔。

## 跳過（基建表，非使用者功能）
rate_limit_buckets、jwt_blocklist、scheduler_heartbeats、*_cache、*_sync_states、*_staging、alembic_version、pg_stat_statements、各 refresh_tokens。

## 驗收（row 數 >0 ≠ 畫面看得到）
灌完挑 6 個高價值模組過讀取閘確認會渲染：
1. 家長端 portal（用**非終態**學生 + 監護人 PII 未被 GC）
2. 教師端 own-class scope（用 teacher 帳號，非 admin）
3. 招生漏斗（visits 有 period/month 父層）
4. 考核/年終（cycle state 正確才看得到）
5. 聯絡簿（注意 `announcement_parent_recipients` RLS 已知缺）
6. 學生每日出缺勤（教師日點名頁）

## 已知簡化
上下學期共用同一批 11 班（grade 結構整學年穩定），不另建 114/1 平行班級。

---

## 落地結果（2026-06-05）

**入口**：`python -m scripts.seed_full_year`（核心 `--term all` + 自動探索 `scripts/seed/*.py` 共 17 個冷門模組）。**全程冪等**：跑一次 runner，全表零 drift（已驗證）。

**核心(逐學期，上+下)**：員工考勤 3,858、學生每日出缺勤 35,242（174 生全覆蓋）、薪資 233（全年）、學費 2,088、員工請假 63、排班 817、會議 247、才藝課 16。

**冷門模組(scripts/seed/)**：聯絡簿 202、學生請假 45、成長報告 60/觀察 88/里程碑 29、量測 121/用藥 12+20/過敏 8、特教 IEP 6/文件 6/補助 2、招生 visits 40/競品(competitor_school)5/詢問 15、員工合約 22/學歷 31/證照 36、離職 3/獎懲 3、DSR 8/consent 21、考核 participants 21/summaries 21、年終 settlements 19、繳費分錄 300+145、親師訊息 thread 1/msg 6、daily_shifts 328、公告對象 26+76/已讀 12/活動回條 6、在學證明 10/補休 1/美術鐘點 8/學費減免 8/補班 3、學費範本 24、年終設定 org_year 2/班級編制 22/年級招生 4。

**驗收(API 實打 200)**：員工、學生出缺勤(上學期 9/10月 + 下學期 3月)、請假、加班、才藝課、招生漏斗 board 皆 200 帶 seed 資料。

**年終 rebuild 地雷已拆**：補了 `org_year_settings`/`class_enrollment_targets` 後，離線實跑 `build_settlements(refresh_rates=True)`：7 筆 DRAFT 重算為非 0、12 筆已簽跳過、0 筆歸零（全在 ±100萬 CHECK 內）。

**刻意留空（49 表）**：
- **runtime/transient/audit**（不該 seed）：jwt_blocklist、rate_limit_buckets、*_refresh_tokens、password_history、pending_uploads、*binding_codes/device_setup_codes、*_cache、*_staging、sync_*、line_webhook/reply、medical_access_log、activity_pos_daily_close*、salary_calc_jobs、data_quality_reports、gov_data_snapshots、unused_leave_payout_log、appraisal_summary_log/manual_event_counts、competitor_change_log/note/penalty/tag、recruitment_event_log、registration_changes。
- **法定/參考資料(由專屬 migration/匯入流程載入,非假資料)**：insurance_brackets/tables、minimum_wage_history、system_configs、position_salary_configs、deduction_rules、bonus_settings、class_bonus_settings、salary_items、special_bonus_items、appraisal_score_item_catalog。⚠ insurance/minimum_wage 空 → 薪資「重算」需先載統計級距(現有 salary 是複製值，顯示無虞)。
- **已退場/被取代/孤兒待確認**：fee_items（c3 已 DROP）、recruitment_competitors（被 competitor_school 取代）、registration_supplies、monthly_fixed_costs/vendor_payments（疑屬已移除的經營分析）、notification_preferences/line_configs（使用者/管理者操作時才產生）。

**已知限制**：① 家長端真實登入僅覆蓋 student 1（dev DB 只有 1 個綁定的家長 User）；其餘學生家長端資料需由 admin impersonation 預覽。② 親師訊息、家長已讀/回條同受家長 User 稀少限制。③ 薪資「重算」、保險設定頁需法定級距資料(留空)。

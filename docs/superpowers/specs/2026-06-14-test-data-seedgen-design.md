# 可參數化全年測試資料產生器 `seedgen` 設計

- 日期：2026-06-14
- 作者：Claude（與 user 對齊需求）
- 狀態：設計已通過，待寫實作計畫
- 目標 repo：`ivy-backend`

## 1. 目標

打造一支**全新、可參數化、決定論**的測試資料產生器 `scripts/seedgen/`，把本機 dev DB
（`postgresql://yilunwu@localhost:5432/ivymanagement`）**全清業務資料後重灌**成「一整個學年、涵蓋所有功能面」
的可手測 / 可展示資料集。

### 已與 user 對齊的決策

| 項目 | 決定 |
|---|---|
| 目標 DB | 本機 dev DB（`ivymanagement`）。**prod / Zeabur 絕不碰** |
| 做法 | **重寫新的可參數化 seeder**（非沿用 `seed_test_data_114_2.py`，但參考其領域知識） |
| 既有資料 | **全清業務資料後重灌**（TRUNCATE 業務+營運設定，保留 schema 與系統參考種子） |
| 預設學年 | **114 學年（2025-08 ~ 2026-07）**，可用 `--year` 參數改 |
| 規模 | **比照現況**：約 6~8 班、150~180 學生、~23 員工（含班導/助教/行政/才藝時薪/主管） |
| 時間進度 | **學年中段快照**，預設 `today=2026-02-16`：前段月份已結算、當月進行中、未來留空 |
| 衍生財務資料 | **跑真實 SalaryEngine / 年終引擎**算出薪資與年終，內部一致、順帶驗證引擎（Approach A） |

## 2. 現況勘查（2026-06-14）

- **alembic 無漂移**：code head 與 dev DB current 都在 `enrdwt01`，乾淨同步。新 seeder 用當前 ORM
  模型不會撞 schema。（記憶中的 `enrsnap01`/`cfgyrfx01` 等在未合併 worktree，不在目前 main。）
- dev DB 現有 23 員工 / 176 學生 / 169 表。
- **引擎必需的法定/設定表多為空**，這是 Approach A 的關鍵前置：

| 表 | 現有筆數 | 處置 |
|---|---|---|
| `insurance_brackets` | 0 | **config 模組必須種**（從 canonical migration 資料萃取，勞保/健保/勞退級距，含 2025+2026 config_year） |
| `position_salary_configs` | 0 | **config 模組必須種**（各職位底薪 × config_year 2025/2026） |
| `appraisal_score_item_catalog` | 0 | **config 模組必須種**（15 項考核計分目錄） |
| `deduction_rules` | 0 | config 模組種 |
| `system_configs` | 0 | config 模組種（引擎讀的系統參數） |
| `insurance_rates` | 1 | 重建（補齊 2025/2026） |
| `bonus_configs` / `grade_targets` / `attendance_policies` | 1 / 4 / 1 | 重建（每 config_year 一套） |
| `appraisal_bonus_rates` | 10 | 已有，重建以對齊學年 |
| `permission_definitions` / `roles` | 64 / 7 | **保留**（alembic 種，不動） |

> 既有 `seed_test_data_114_2.py` 因 insurance/position 空而「複製數值不跑引擎」。本設計反其道而行：
> config 模組補齊這些法定表，使引擎能離線實算。年終引擎離線跑 `build_settlements(refresh_rates=True)`
> 在舊 seeder 已驗證可行，薪資引擎 `SalaryEngine._compute_and_persist_single_employee`（engine.py:3745）
> 同為可離線編排的入口。

## 3. 架構

### 3.1 套件佈局（不動既有腳本）

新套件 `scripts/seedgen/`。**不刪、不改** `scripts/seed_*.py` 與 `scripts/seed/`
（`seed_e2e_ci.py` 仍被 CI 用）。

```
scripts/seedgen/
  __main__.py          # CLI 入口：解析參數 → 安全護欄 → wipe → 依序跑模組 → summary
  config.py            # SeedConfig dataclass（year/today/scale/rng_seed/wipe/...）
  context.py           # SeedContext：session + config + RNG + 已建實體 registry
  guard.py             # 破壞性安全護欄（拒絕非 localhost dev DB）
  wipe.py              # TRUNCATE 業務+營運設定表（FK-safe）
  fake.py              # 決定論 faker：台灣姓名/電話/地址/身分證/日期
  reference_data.py    # 法定參考資料（insurance brackets/rates、position 底薪、考核目錄）
  calendar.py          # 學年月份序列 + 每月狀態（closed/in_progress/future）推導
  verify.py            # 灌後 summary 與內部一致性抽查
  modules/
    m00_config.py      # 設定型：class_grades/job_titles/policies/bonus/insurance/fee_templates/...
    m01_org.py         # employees → classrooms(回填班導) → users(已知帳號)
    m02_students.py    # recruitment_visits → students(lifecycle 狀態機) → guardians + binding
    m03_attendance.py  # 逐月打卡 + shift_assignments + daily_shifts
    m04_leave_ot.py    # 員工請假 / 加班 / 補打卡（含當月 pending）
    m05_fees.py        # fee_records + payments + refunds + adjustments
    m06_salary.py      # 已結月跑 SalaryEngine 結算；當月留 in-progress
    m07_activities.py  # 才藝報名/候補/POS 出帳/點名/用品
    m08_portal.py      # 公告+已讀、聯絡簿+回覆、放學接送、會議、行事曆
    m09_parent.py      # 親師訊息、通知偏好、同意書、student_leave_request
    m10_medical.py     # 過敏/用藥醫囑+給藥/量測/里程碑/成長報告（敏感欄位加密）
    m11_special_ed.py  # IEP/身障文件/特教加給/gov_moe 月報快照
    m12_appraisal.py   # 上學期 cycle 完成評分；下學期進行中
    m13_year_end.py    # 114 年終 cycle，settlement 走引擎到 SUPERVISOR_SIGNED
    m14_audit_misc.py  # audit_logs/notification_log/vendor_payments/offboarding/disciplinary
```

每個 `modules/mNN_*.py` 匯出 `def seed(ctx: SeedContext) -> None:`，職責單一、可獨立讀懂與測試。
模組間透過 `ctx.registry`（例如 `ctx.employees`, `ctx.classrooms`, `ctx.students`）互相引用，
不重查 DB。

### 3.2 CLI

```bash
python -m scripts.seedgen --year 114 --today 2026-02-16 --scale standard --wipe --yes
# 預設不帶 --wipe = dry-run：印出將清哪些表、將建多少筆，不動 DB
# --scale {small,standard,large}；--rng-seed N（預設固定）；--only mNN（debug 單模組）
```

### 3.3 安全護欄（`guard.py`）

破壞性 wipe 前強制檢查：
1. 解析 `settings.core.database_url`，**主機必須是 localhost/127.0.0.1 且 dbname=ivymanagement**，
   否則 `abort`。
2. `settings.core.env` 必須非 production。
3. remote 特徵（supabase/neon/render/zeabur/含 `?sslmode`）一律 abort。
4. 唯一繞過：顯式 `--i-know-not-dev`（僅供極端情況，預設不存在於文件範例）。

杜絕誤刷 Zeabur prod。

## 4. Wipe 策略（`wipe.py`）

- 對「業務 + 營運設定」表執行 `TRUNCATE ... RESTART IDENTITY CASCADE`（單一交易、FK-safe）。
- **保留**：
  - schema 本身與 `alembic_version`
  - alembic 種的系統參考：`permission_definitions`、`roles`
  - （`insurance_brackets` 等法定表現為空，由 config 模組重種，不在保留清單也無妨）
- **明確跳過（不清不種）**——runtime/transient/audit-internal：
  `jwt_blocklist`、`rate_limit_buckets`、`*_refresh_tokens`、`password_history`、`pending_uploads`、
  `*binding_codes`/`*device_setup_codes`、`*_cache`、`*_staging`、`*_sync_states`、
  `line_webhook_events`/`line_reply_contexts`、`scheduler_heartbeats`/`scheduler_watermarks`、
  `salary_calc_jobs`、`data_quality_reports`。
  （部分如 audit_logs / medical_access_log 由對應模組少量寫入以展示稽核頁，非留全空。）

wipe 清單由「自動列舉 `Base.metadata` 全表 − 保留集 − 跳過集」產生，避免硬編漏表；產生後在
dry-run 印出供人工核對。

## 5. 時間模型（`calendar.py`）

預設 `today=2026-02-16`，學年 2025-08 ~ 2026-07。每月狀態：

| 月份 | 狀態 | 內容 |
|---|---|---|
| 2025-08 ~ 2026-01 | **closed** | 考勤/排班/請假/加班皆已核；學費收訖；薪資已結算（跑引擎）；活動已點名 |
| 2026-02 | **in_progress** | 打卡到 today=02-16 為止；有 pending 假單/加班/補打卡；薪資未結；年終簽核中 |
| 2026-03 ~ 2026-07 | **future** | 僅排程性資料（行事曆/未來活動 session），不生交易 |

> 可調：把 `--today` 改到 2026-05-16 即得「更多已結月份 + 年終已 FINALIZED」。spec 預設取 02-16
> 以呈現「年終獎金簽核進行中」這個頭牌流程。

`utils/student_lifecycle.set_lifecycle_status` / `services/student_lifecycle.transition()` 為
lifecycle 唯一寫入路徑，模組不得 raw UPDATE。

## 6. 決定論

`SeedConfig.rng_seed` 預設固定整數。所有隨機（姓名/電話/出缺勤分佈/金額抖動）走 `ctx.rng`
（`random.Random(seed)`）。同參數重跑 → 同一份資料，利於回歸與對帳。**不使用** `Date.now()` 類
非決定來源；「today」一律取自 `config.today`。

## 7. 引擎整合（薪資 / 年終）—— Approach A

### 7.1 薪資（`m06_salary.py`）

- 前置：config 模組已種 `insurance_brackets/rates`、`position_salary_configs`（含 config_year
  2025+2026）、`bonus_configs`/`grade_targets`、`attendance_policies`、`deduction_rules`；
  org/attendance/leave 模組已寫入該月員工考勤與假單。
- 對每個 **closed 月份**，以離線方式編排 `SalaryEngine`（比照 `main.py` 的服務注入），呼叫
  bulk 結算入口（`_compute_and_persist_single_employee` / 其 bulk 包裝）寫入 `SalaryRecord`
  （+ `SalarySnapshot`）。
- 當月（in_progress）**不結算**，留待手測按「計算薪資」。
- **fallback 階梯**（實作時若離線完整編排過於脆弱）：
  1. 用引擎純函式 `SalaryEngine.calculate_salary()`（engine.py:1834）取得 breakdown，自行寫
     `SalaryRecord`（仍內部一致）。
  2. 最後手段：以單一範本月複製並標註（與舊 seeder 同等級，僅當 1 不可行）。
- **前置完整性 preflight**：salary 模組開跑前 assert 所有引擎前置已備齊，缺則 fail-loud 印出缺哪張表，
  把脆弱性轉成明確訊號。

### 7.2 年終（`m13_year_end.py`）

- 建 114 `YearEndCycle` + `OrgYearSettings` + `ClassEnrollmentTarget`。
- 跑 `services/year_end/settlement_builder.build_settlements(refresh_rates=True)` 產生
  `EmployeeYearEndSnapshot` + `YearEndSettlement`。
- 狀態設為 `SUPERVISOR_SIGNED`（簽核進行中），金額落 ±100 萬 CHECK 內。

## 8. 模組職責摘要（依 FK 拓撲）

1. **m00_config** — 全設定型 + 法定參考表（§2 表格列出的必種項）。
2. **m01_org** — `employees`（7 職稱、含 hourly 才藝/主管）→ `classrooms`（回填班導/助教 FK）→
   `users`（admin/teacher/parent 已知帳號）。處理 employees↔classrooms 循環 FK（classroom_id nullable，
   先建 employee 再建 classroom 回填）。
3. **m02_students** — `recruitment_visits` → `students`（lifecycle 走狀態機，多數 active、少量
   on_leave/withdrawn/graduated/prospect 以涵蓋各態）→ `guardians` + `guardian_binding_codes`。
4. **m03_attendance** — 逐月員工 `attendances`（~88% normal / late / early_leave / leave）+
   `shift_assignments` + `daily_shifts` + 學生每日 `student_attendances`。
5. **m04_leave_ot** — `leave_records`（含補休來源）、`overtime_records`、`punch_correction_requests`；
   closed 月已核、當月留 pending。
6. **m05_fees** — `student_fee_records`（unpaid/partial/paid 三態）+ `payments` + `refunds` +
   `adjustments`（學費減免）。
7. **m06_salary** — 見 §7.1。
8. **m07_activities** — `activity_courses`/`supplies`/`settings` 已在 config；本模組寫
   `registrations`/`registration_courses`（含 waitlist）/`registration_supplies`/`payment_records`/
   `pos_daily_close`/`activity_attendance`/`parent_inquiries`。
9. **m08_portal** — `announcements`(+recipients/reads)、`student_contact_book_entries`(+ack/reply)、
   `student_dismissal_calls`、`meeting_records`、`school_events`、`holidays`、`workday_overrides`、
   `event_acknowledgments`。
10. **m09_parent** — `parent_message_threads`/`parent_messages`、`parent_notification_preferences`、
    `policy_versions`+`parent_consent_logs`、`student_leave_requests`。
11. **m10_medical** — `student_allergies`、`student_medication_orders`+`logs`、`student_measurements`、
    `student_milestones`、`student_growth_reports`、`student_observations`、少量 `medical_access_log`。
    敏感欄位走既有加密（`MEDICAL_FIELD_ENCRYPTION_KEY`）。
12. **m11_special_ed** — `student_iep_records`、`student_disability_documents`、
    `special_education_subsidies`、`monthly_enrollment_snapshots`、`enrollment_certificates`。
13. **m12_appraisal** — 上學期 `appraisal_cycles`(CLOSED) + participants + score_items + summaries（已評）；
    下學期 cycle OPEN（進行中）。
14. **m13_year_end** — 見 §7.2。
15. **m14_audit_misc** — 少量 `audit_logs`、`notification_log`、`vendor_payments`、
    `employee_offboarding_records`、`disciplinary_actions`、`employee_contracts/education/certificates`、
    `dsr_requests`。

## 9. 驗證（`verify.py` / `--verify`）

1. 灌後印每表筆數 summary。
2. 內部一致性 assert：closed 月薪資總額 = 各 breakdown 加總；年終金額在 CHECK 內；學生 lifecycle
   值域合法；無孤兒 FK。
3. 高價值模組讀取閘抽查（沿用舊 seeder 驗收清單）：家長 portal（非終態學生）、教師 own-class scope、
   招生漏斗、考核/年終 cycle 狀態、聯絡簿、學生日點名。

## 10. 範圍外 / 不做

- 不碰 prod / Zeabur。
- 不改既有 `scripts/seed*`、不改 CI seed。
- 不在 pytest 內當 fixture 用（純 dev DB 灌庫工具；未來若要抽 factory 另案）。
- 不種 §4 跳過清單的 runtime/transient 表。

## 11. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 離線跑薪資引擎前置不齊 → raise | config 模組補齊法定表 + salary preflight fail-loud + fallback 階梯 |
| 誤刷 prod | `guard.py` 多重 localhost/dev 檢查，預設 dry-run |
| 引擎服務注入複雜 | 比照 `main.py init_*_services`；先用純函式 fallback 驗證可行再上 persist 路徑 |
| FK 循環（employee↔classroom） | classroom_id nullable，先建 employee 再回填 |
| 金額 CHECK（年終 ±100 萬） | 規模/分佈設計使金額落界內 + verify 抽查 |
| TRUNCATE CASCADE 誤連保留表 | wipe 清單自動列舉 − 保留集，dry-run 印出人工核對 |

## 12. 實作策略

模組彼此獨立（共享 `SeedContext` 介面），適合 workflow 平行打造：先做 m00_config / 基礎設施
（context/guard/wipe/fake/reference_data/calendar）→ 再平行各 domain 模組 → 整合 orchestrator →
端到端跑 dry-run → 實灌 → verify。每模組附最小自我檢查。

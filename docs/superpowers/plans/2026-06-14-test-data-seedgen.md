# 可參數化全年測試資料產生器 `seedgen` 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打造 `scripts/seedgen/` 套件，把本機 dev DB 全清業務資料後重灌成一整個 114 學年、涵蓋所有功能面、內部一致（薪資/年終跑真引擎）、決定論的測試資料。

**Architecture:** 模組化套件，共享 `SeedContext`（session + config + RNG + 已建實體 registry）。基礎設施（config/context/guard/wipe/fake/calendar/reference_data）先建為硬契約；14 個 domain 模組各匯出 `seed(ctx)`，建檔互相獨立、執行由 orchestrator 依 FK 拓撲排序。薪資走 `engine.process_bulk_salary_calculation`、年終走 `build_settlements`（production 同路徑）。

**Tech Stack:** Python 3.x、SQLAlchemy 2.0、現有 ORM 模型（`models/`、`config/`）、現有引擎（`services/salary`、`services/year_end`）。

工作目錄：worktree `/Users/yilunwu/Desktop/ivy-backend/.worktrees/seedgen`（branch `feat/seedgen-test-data-2026-06-14-be`，off local main）。所有 `python` 用 `/Users/yilunwu/Desktop/ivy-backend/.venv/bin/python`，cwd 設在 worktree。所有 git 用 `git -C /Users/yilunwu/Desktop/ivy-backend/.worktrees/seedgen`。

---

## 共享契約（所有模組必讀，不可漂移）

**`SeedConfig`（config.py，frozen dataclass）欄位：** `academic_year:int=114`、`today:date=date(2026,2,16)`、`scale:str="standard"`、`rng_seed:int=20260614`、`wipe:bool=False`、`confirm:bool=False`、`allow_non_dev:bool=False`、`only:tuple[str,...]=()`。
- `year_start` property = `date(academic_year+1911, 8, 1)`（114→2025-08-01）
- `year_end` property = `date(academic_year+1912, 7, 31)`（114→2026-07-31）
- `scale_profile` property 回 dict：standard→`{classrooms:7, students:170, employees:23}`；small→`{3,60,12}`；large→`{12,420,42}`

**`SeedContext`（context.py，dataclass）欄位（registry，模組讀寫的唯一介面）：**
- `session: Session`、`config: SeedConfig`、`rng: random.Random`
- `class_grades: list`、`job_titles: dict[str, JobTitle]`
- `employees: list[Employee]`、`employees_by_role: dict[str, list[Employee]]`（role key：`supervisor/admin/accountant/homeroom/assistant/art/support`）
- `classrooms: list[Classroom]`、`users: dict[str, User]`（username→User）
- `students: list[Student]`、`students_active: list[Student]`、`guardians: list[Guardian]`
- `counts: dict[str, int]`、method `log(table:str, n:int)` 累加筆數
- method `closed_months() -> list[tuple[int,int]]`、`current_month() -> tuple[int,int]`（委派 calendar.py）

**模組協定：** 每 `modules/mNN_*.py` 匯出 `def seed(ctx: SeedContext) -> None:`。模組只透過 `ctx` registry 取依賴，不重查已建實體。寫完一律 `ctx.log(table, n)`。lifecycle 變更一律經 `utils.student_lifecycle.set_lifecycle_status`，**禁** raw UPDATE。金額用既有 `round_half_up`（禁 builtin `round()`）。

**orchestrator 執行序（__main__.py，每模組跑完 `ctx.session.commit()`）：** m00 → m01 → m02 → m03 → m04 → m05 → m06 → m07 → m08 → m09 → m10 → m11 → m12 → m13 → m14 → verify。

---

## Phase 0：基礎設施與契約（必先完成，後續所有模組依賴）

### Task 0.1：套件骨架 + SeedConfig

**Files:** Create `scripts/seedgen/__init__.py`、`scripts/seedgen/config.py`、`tests/seedgen/__init__.py`、`tests/seedgen/test_config.py`

- [ ] **Step 1: 寫失敗測試** `tests/seedgen/test_config.py`

```python
from datetime import date
from scripts.seedgen.config import SeedConfig

def test_year_bounds_114():
    c = SeedConfig(academic_year=114)
    assert c.year_start == date(2025, 8, 1)
    assert c.year_end == date(2026, 7, 31)

def test_scale_profile_standard():
    c = SeedConfig()
    p = c.scale_profile
    assert p["classrooms"] == 7 and p["employees"] == 23 and p["students"] == 170

def test_default_today_mid_year():
    assert SeedConfig().today == date(2026, 2, 16)
```

- [ ] **Step 2: 跑測試確認失敗** `... .venv/bin/python -m pytest tests/seedgen/test_config.py -q`（Expected: ModuleNotFoundError）
- [ ] **Step 3: 實作 config.py**（frozen `@dataclass`，欄位與 property 如上「共享契約」；`scale_profile` 用 dict 對照表）
- [ ] **Step 4: 跑測試確認通過**（Expected: 3 passed）
- [ ] **Step 5: Commit** `git -C <wt> add scripts/seedgen/__init__.py scripts/seedgen/config.py tests/seedgen && git -C <wt> commit -m "feat(seedgen): SeedConfig 參數與學年/規模衍生"`

### Task 0.2：SeedContext + registry

**Files:** Create `scripts/seedgen/context.py`、`tests/seedgen/test_context.py`

- [ ] **Step 1: 失敗測試**：建 `SeedContext(session=None, config=SeedConfig(), rng=random.Random(1))`，斷言 `ctx.log("students", 5); ctx.log("students", 3); assert ctx.counts["students"] == 8`；斷言 `ctx.closed_months()[0] == (2025, 8)` 且 `(2026, 2) not in ctx.closed_months()` 且 `ctx.current_month() == (2026, 2)`。
- [ ] **Step 2: 跑測試確認失敗**
- [ ] **Step 3: 實作 context.py**（dataclass 欄位如「共享契約」；`closed_months`/`current_month` 委派 `from .calendar import closed_months, current_month`——calendar 在 Task 0.5 建，此步先 import 會失敗，故 **Task 0.5 完成後** 再跑 Step 4）
- [ ] **Step 4: 跑測試確認通過**（依賴 Task 0.5）
- [ ] **Step 5: Commit**

### Task 0.3：安全護欄 guard.py

**Files:** Create `scripts/seedgen/guard.py`、`tests/seedgen/test_guard.py`

- [ ] **Step 1: 失敗測試**

```python
import pytest
from scripts.seedgen.guard import assert_dev_db, GuardError

def test_localhost_ivymanagement_ok():
    assert_dev_db("postgresql://yilunwu@localhost:5432/ivymanagement", env="development", allow_non_dev=False)

@pytest.mark.parametrize("url", [
    "postgresql://u:p@db.zeabur.internal:5432/ivymanagement",
    "postgresql://u:p@aws-0-x.pooler.supabase.com:6543/postgres",
    "postgresql://yilunwu@localhost:5432/ivymanagement?sslmode=require",
])
def test_remote_rejected(url):
    with pytest.raises(GuardError):
        assert_dev_db(url, env="development", allow_non_dev=False)

def test_production_env_rejected():
    with pytest.raises(GuardError):
        assert_dev_db("postgresql://yilunwu@localhost:5432/ivymanagement", env="production", allow_non_dev=False)

def test_override():
    assert_dev_db("postgresql://x@remote/db", env="production", allow_non_dev=True)
```

- [ ] **Step 2: 跑測試確認失敗**
- [ ] **Step 3: 實作 guard.py**：`assert_dev_db(url, env, allow_non_dev)`：`allow_non_dev` 為 True 直接放行；否則用 `urllib.parse.urlsplit` 解析，要求 `hostname in {"localhost","127.0.0.1"}` 且 `path.lstrip("/")=="ivymanagement"` 且 query 不含 `sslmode` 且 `env != "production"`，任一不符 raise `GuardError`。
- [ ] **Step 4: 跑測試確認通過**
- [ ] **Step 5: Commit**

### Task 0.4：決定論 faker fake.py

**Files:** Create `scripts/seedgen/fake.py`、`tests/seedgen/test_fake.py`

- [ ] **Step 1: 失敗測試**：`Faker(random.Random(42))` 兩個同 seed 實例產生的 `name("M")`/`phone()`/`id_number()` 序列相同；不同 seed 不同；`phone()` 符合 `^09\d{8}$`；`id_number()` 符合 `^[A-Z][12]\d{8}$`；`name()` 長度 2~3。
- [ ] **Step 2: 跑測試確認失敗**
- [ ] **Step 3: 實作 fake.py**：`class Faker` 持有注入的 `rng`。提供 `name(gender)`、`phone()`、`id_number(gender)`、`address()`、`birthday(min_age,max_age, ref=today)`、`amount(low,high,step)`。姓名常數可參考（**值，非 import**）`scripts/seed_test_data_114_2.py` 的 `SURNAMES` 與名字字元池，全部改走 `self.rng.choice`，不碰全域 `random`。
- [ ] **Step 4: 跑測試確認通過**
- [ ] **Step 5: Commit**

### Task 0.5：學年日曆 calendar.py

**Files:** Create `scripts/seedgen/calendar.py`、`tests/seedgen/test_calendar.py`

- [ ] **Step 1: 失敗測試**

```python
from datetime import date
from scripts.seedgen.calendar import month_status, all_months, closed_months, current_month, workdays

def test_status_partition():
    cfg_today = date(2026, 2, 16); ys = date(2025,8,1); ye = date(2026,7,31)
    assert month_status(2025, 8, cfg_today) == "closed"
    assert month_status(2026, 1, cfg_today) == "closed"
    assert month_status(2026, 2, cfg_today) == "in_progress"
    assert month_status(2026, 3, cfg_today) == "future"

def test_closed_months_count():
    months = closed_months(date(2025,8,1), date(2026,2,16))
    assert (2025,8) in months and (2026,1) in months and (2026,2) not in months
    assert len(months) == 6

def test_workdays_excludes_weekends():
    wd = workdays(2025, 9, upto=None)
    assert all(d.weekday() < 5 for d in wd)
```

- [ ] **Step 2: 跑測試確認失敗**
- [ ] **Step 3: 實作 calendar.py**：`month_status(y,m,today)` 比較該月首日/末日與 today（整月 ≤ today 末日且 today ≥ 月末→closed；含 today→in_progress；月首 > today→future）；`all_months(ys,ye)`、`closed_months(ys,today)`、`current_month(today)`、`workdays(y,m,upto)`（排除六日，`upto` 截到該日）。
- [ ] **Step 4: 跑測試確認通過**（順手讓 Task 0.2 Step 4 也通過）
- [ ] **Step 5: Commit**

### Task 0.6：法定參考資料 reference_data.py

**Files:** Create `scripts/seedgen/reference_data.py`、`tests/seedgen/test_reference_data.py`

先 Read `alembic/versions/20260507_d9e0f1g2h3i4_insurance_brackets_to_db.py` 取得勞保/健保級距 canonical 值；Read `models/config.py:249-360`（InsuranceRate/InsuranceBracket/PositionSalaryConfig 欄位）；Read 考核目錄 seed migration（`20260511_a7p8p9r0i1s2_appraisal_seed_catalog.py`）取 15 項。

- [ ] **Step 1: 失敗測試**：`insurance_brackets()` 回 list[dict]，筆數 == migration 的筆數（讀 migration 確認，預期 ~82），每筆有 `salary_min/salary_max/labor_insurance_salary/health_insurance_salary` 等鍵；`position_salary_standards()` 回各職稱底薪 dict 且涵蓋 m01 會用到的 7 職稱；`appraisal_catalog()` 回 15 項。
- [ ] **Step 2: 跑測試確認失敗**
- [ ] **Step 3: 實作 reference_data.py**：以「純 Python 常數」收錄上述 canonical 值（不 import migration），函式回傳 list[dict]，供 m00 落庫。底薪值參考引擎 `services/salary/engine.py` 的 `POSITION_GRADE_MAP` / 既有 position 標準，確保與引擎一致。
- [ ] **Step 4: 跑測試確認通過**
- [ ] **Step 5: Commit**

### Task 0.7：wipe.py

**Files:** Create `scripts/seedgen/wipe.py`、`tests/seedgen/test_wipe.py`

- [ ] **Step 1: 失敗測試**：`tables_to_wipe()` 回 list[str]，**包含** `students`/`employees`/`salary_records`/`attendances`/`classrooms`；**不含** 保留集 `alembic_version`/`permission_definitions`/`roles`；**不含** 跳過集 `jwt_blocklist`/`rate_limit_buckets`/任何 `_refresh_tokens`/`*_cache`/`scheduler_heartbeats`。table 全來自 `Base.metadata.sorted_tables`。
- [ ] **Step 2: 跑測試確認失敗**
- [ ] **Step 3: 實作 wipe.py**：`PRESERVE={...}`、`SKIP_SUBSTRINGS=[...]`（如 spec §4）；`tables_to_wipe()` = `Base.metadata` 全表名 − PRESERVE − 命中 SKIP；`wipe(session)`：`SET session_replication_role = replica`（或 `TRUNCATE ... RESTART IDENTITY CASCADE` 一次列出所有表）後復原，單一交易。
- [ ] **Step 4: 跑測試確認通過**
- [ ] **Step 5: Commit**

### Task 0.8：CLI 骨架 __main__.py（模組先 stub）

**Files:** Create `scripts/seedgen/__main__.py`、`scripts/seedgen/modules/__init__.py`、`scripts/seedgen/modules/m00_config.py`…`m14_audit_misc.py`（**每檔先放 `def seed(ctx): pass` stub**）

- [ ] **Step 1: 實作 __main__.py**：`argparse`（--year/--today(ISO)/--scale/--rng-seed/--wipe/--yes/--i-know-not-dev/--only）→ 建 `SeedConfig` → `guard.assert_dev_db(settings.core.database_url, settings.core.env, cfg.allow_non_dev)` → `session_scope()` 開 session → 建 `SeedContext` → 若 `--wipe`：印 `wipe.tables_to_wipe()` 並（有 `--yes`）執行 `wipe.wipe`；無 `--wipe`：dry-run 印計畫 → 依執行序 import 並跑各模組 `seed(ctx)`（`--only` 過濾）每跑完 commit → 末了跑 verify 印 summary。
- [ ] **Step 2: 冒煙**：`cd <wt>; .venv/bin/python -m scripts.seedgen --help` 應印參數；`... -m scripts.seedgen`（無 --wipe，dry-run，全 stub）應乾淨跑完印空 summary 不報錯、不寫 DB。
- [ ] **Step 3: Commit** `feat(seedgen): CLI 骨架 + 模組 stub + 安全護欄接線`

---

## Phase 1：設定型資料 m00_config（所有 domain 模組依賴）

### Task 1.1：m00_config.py

**Files:** Modify `scripts/seedgen/modules/m00_config.py`；Test：本模組以「灌後 DB 查詢」自我驗證（見 Step）。

先 Read 這些 model 取確切欄位：`models/classroom.py`（ClassGrade/Classroom）、`models/employee.py`（JobTitle）、`models/config.py`（AttendancePolicy/BonusConfig/GradeTarget/InsuranceRate/InsuranceBracket/PositionSalaryConfig/SystemConfig）、`models/salary.py`（DeductionRule/DeductionType/BonusType/BonusSetting）、`models/leave.py`（LeaveQuota）、`models/fees.py`（FeeTemplate）、`models/shift.py`（ShiftType）、`models/activity.py`（ActivityCourse/ActivitySupply/ActivityRegistrationSettings）、`models/event.py`（Holiday）、`models/consent.py`（PolicyVersion）、`models/approval.py`（ApprovalPolicy）。

- [ ] **Step 1: 實作 seed(ctx)**，建立（每項用 `ctx.rng`、`reference_data`）：
  - `class_grades`：依規模建年級（如 幼幼/小班/中班/大班）→ 存入 `ctx.class_grades`
  - `job_titles`：7 職稱（主管/行政/會計/班導/助教/才藝時薪/支援）→ `ctx.job_titles`
  - `attendance_policies`、`bonus_configs`+`grade_targets`、`insurance_rates`、`insurance_brackets`、`position_salary_configs`、`deduction_rules`、`system_configs`：**每個跨 config_year（2025 與 2026）各一套**（period-aware resolver 需要），值取自 `reference_data`
  - `leave_quotas` 模板值（員工建立後由 m01 綁，或此處建通用額度）、`fee_templates`（年級×學期×費目）、`shift_types`、`activity_courses`/`supplies`/`registration_settings`、`holidays`（學年內國定假日）、`policy_versions`、`approval_policies`
  - 每表 `ctx.log(table, n)`
- [ ] **Step 2: 自我驗證**（暫時性 script 或在 __main__ `--only m00` 後查 DB）：`.venv/bin/python -m scripts.seedgen --wipe --yes --only m00` 後，psql 確認 `insurance_brackets>0 AND position_salary_configs 有 2025+2026 兩組 AND class_grades>=4 AND fee_templates>0`。
- [ ] **Step 3: Commit** `feat(seedgen): m00 設定型 + 法定參考表（含 2025/2026 config_year）`

---

## Phase 2：核心實體 m01_org、m02_students

### Task 2.1：m01_org.py（employees → classrooms → users）

**Files:** Modify `scripts/seedgen/modules/m01_org.py`。先 Read `models/employee.py`（Employee 欄位：name/employee_id/job_title_id/employee_type/hire_date/salary 相關/insurance 相關/gender/email）、`models/classroom.py`（Classroom：name/class_grade_id/homeroom_teacher_id/assistant 等）、`models/auth.py`（User：username/password_hash/role/employee_id/permission_names）、`utils/permissions.py`（ROLE_TEMPLATES、密碼雜湊 helper）。

- [ ] **Step 1: 實作 seed(ctx)**：
  - 依 `scale_profile["employees"]` 建 Employee：角色配比（1 supervisor、1 admin、1 accountant、N homeroom=班級數、N assistant、~4 art(hourly `employee_type`)、~2 support）。`hire_date` 散佈於學年前數年（讓年資/年終 proration 有變化）。職稱底薪對齊 m00 `position_salary_configs`。存 `ctx.employees` + `ctx.employees_by_role`。
  - 建 Classroom（數量=`scale_profile["classrooms"]`，綁 class_grade、回填 homeroom_teacher_id=對應 homeroom employee、assistant）。存 `ctx.classrooms`。處理 employee↔classroom 循環：先 flush employees 取得 id，再建 classroom 回填，再（若 Employee.classroom_id 存在）回填班導的 classroom_id。
  - 建 User：admin/teacher/parent **已知帳號**（username+固定測試密碼，role 對應），permission_names 用 ROLE_TEMPLATES。存 `ctx.users`。每位有帳號需求的員工建 staff User。
  - `ctx.log(...)`
- [ ] **Step 2: 自我驗證**：`--only m00,m01` 後 psql 確認 `employees>=規模 AND classrooms=規模 AND 每 classroom.homeroom_teacher_id NOT NULL AND users 含 admin 角色一個`。
- [ ] **Step 3: Commit** `feat(seedgen): m01 員工/班級/登入帳號（含循環 FK 回填）`

### Task 2.2：m02_students.py（recruitment_visits → students → guardians）

**Files:** Modify `scripts/seedgen/modules/m02_students.py`。先 Read `models/recruitment.py`（RecruitmentVisit）、`models/classroom.py`（Student：欄位、lifecycle_status 值域）、`models/guardian.py`（Guardian）、`models/parent_binding.py`（GuardianBindingCode）、`utils/student_lifecycle.py`（set_lifecycle_status）。

- [ ] **Step 1: 實作 seed(ctx)**：建 `scale_profile["students"]` 名學生，分配到 classrooms（每班 ~24）。lifecycle 分佈：多數 `active`，少量 `enrolled`/`on_leave`/`prospect`/`withdrawn`/`graduated`（涵蓋各態，狀態變更走 `set_lifecycle_status`）。先建對應 `recruitment_visits`（學生 FK 來源）。每生建 1~2 `guardians`（含 PII：phone/email/name）與少量 `guardian_binding_codes`。存 `ctx.students`/`students_active`/`guardians`。
- [ ] **Step 2: 自我驗證**：`--only m00,m01,m02` 後確認 `students≈規模 AND lifecycle_status 至少 4 種值 AND guardians>students AND 每 active 學生有 classroom_id`。
- [ ] **Step 3: Commit** `feat(seedgen): m02 招生訪視/學生(lifecycle 狀態機)/監護人`

---

## Phase 3：營運／財務／功能模組（建檔互相獨立，可平行）

> 共同規則：逐月只生到 `ctx` 的 closed + in_progress 月份，**不生 future**（上限 `config.today`）。closed 月資料為「已完成/已核」；in_progress（2026-02）留部分 pending。每模組先 Read 對應 model 取欄位與 enum/CHECK 值域，照「共享契約」寫 registry 與 log。

### Task 3.1：m03_attendance.py
員工 `attendances`（逐月逐工作日，~88% normal、其餘 late/early_leave/leave，status enum 見 `models/attendance.py`）、`shift_assignments`/`daily_shifts`（`models/shift.py`）、學生每日 `student_attendances`（status 值域 `出席/缺席/病假/事假/遲到`，`models/classroom.py:312`）。in_progress 月只到 today。自我驗證：closed 月每員工出勤列數>0。Commit。

### Task 3.2：m04_leave_ot.py
`leave_records`（假別 enum `models/leave.py`，closed 月 approved、當月留 pending；含補休來源 overtime）、`overtime_records`（pending→approved）、`punch_correction_requests`。確保與 m03 考勤不衝突（請假日對應考勤 status=leave）。自我驗證：有 approved 與 pending 各>0。Commit。

### Task 3.3：m05_fees.py
`student_fee_records`（依 fee_templates×學生，status `unpaid/partial/paid` 三態，`models/fees.py:184`）、`student_fee_payments`、`student_fee_refunds`、`student_fee_adjustments`（減免）。closed 學期多 paid，當期有 unpaid/partial。自我驗證：三種 status 皆有。Commit。

### Task 3.4：m06_salary.py（跑真引擎）
**前置 preflight**：assert `insurance_brackets>0`、`position_salary_configs` 含當年 config_year、`bonus_configs>0`、closed 月有考勤——缺則 raise 明確訊息。對每個 `ctx.closed_months()`：先 `ctx.session.commit()`，再 `engine = RuntimeSalaryEngine(load_from_db=True)`（`from api.salary.calculate import RuntimeSalaryEngine`，或 `SalaryEngine(load_from_db=True, insurance_service=InsuranceService())`），`employee_ids=[e.id for e in ctx.employees if 月薪在職]`，呼叫 `engine.process_bulk_salary_calculation(employee_ids, year, month)`（自管 session+commit，寫 `salary_records`+`salary_snapshots`）。當月（in_progress）**不算**。**fallback**：若 bulk 對某月 raise，記錄並改用引擎純函式 `calculate_salary()` 寫單筆（見 spec §7.1 階梯）。自我驗證：closed 月 salary_records 筆數≈在職月薪人數 × 月數，net_pay>0。Commit。

### Task 3.5：m07_activities.py
`activity_registrations`/`registration_courses`（含 waitlist 候補，status default `enrolled`）/`registration_supplies`/`activity_payment_records`（POS，帶 idempotency_key）/`activity_pos_daily_close`/`activity_attendance`/`parent_inquiries`。Read `models/activity.py`。自我驗證：報名>0 且有候補列。Commit。

### Task 3.6：m08_portal.py
`announcements`(+`announcement_recipients`/`announcement_reads`/`announcement_parent_recipients`)、`student_contact_book_entries`(+ack/reply)、`student_dismissal_calls`、`meeting_records`、`school_events`、`workday_overrides`、`event_acknowledgments`。Read `models/event.py`/`contact_book.py`/`dismissal.py`。自我驗證：公告與聯絡簿>0。Commit。

### Task 3.7：m09_parent.py
`parent_message_threads`/`parent_messages`、`parent_notification_preferences`、`parent_consent_logs`（綁 m00 policy_versions）、`student_leave_requests`（家長送，類型 `病假/事假`，狀態含 pending/approved，`models/student_leave.py`）。自我驗證：訊息與家長請假>0。Commit。

### Task 3.8：m10_medical.py
`student_allergies`、`student_medication_orders`+`student_medication_logs`、`student_measurements`、`student_milestones`、`student_growth_reports`、`student_observations`、少量 `medical_access_log`。敏感欄位走既有加密（環境已設 `MEDICAL_FIELD_ENCRYPTION_KEY`）。Read `models/portfolio.py`/`medical_access_log.py`。自我驗證：用藥醫囑與給藥 log>0。Commit。

### Task 3.9：m11_special_ed.py
`student_iep_records`、`student_disability_documents`、`special_education_subsidies`、`monthly_enrollment_snapshots`、`enrollment_certificates`。Read `models/gov_moe.py`。自我驗證：IEP>0。Commit。

### Task 3.10：m12_appraisal.py
上學期 `appraisal_cycles`(CLOSED) + `appraisal_participants` + `appraisal_score_items`（綁 m00 catalog）+ `appraisal_summaries`（已評，grade/rate）；下學期 cycle OPEN（進行中、未評滿）。Read `models/appraisal.py`。自我驗證：上學期 summaries 數≈參與員工數。Commit。

### Task 3.11：m13_year_end.py（跑真引擎）
建 114 `year_end_cycles`（status OPEN）+ `org_year_settings` + `class_enrollment_targets`，`ctx.session.commit()`，呼叫 `build_settlements(ctx.session, academic_year=2025, included_resigned_ids=None, actor_id=<admin user id>, refresh_rates=True)`（學年 ROC 114→ build 用西元學年起 2025，對齊既有慣例；確認既有呼叫端傳的是哪個值再對齊），產生 `employee_year_end_snapshots`+`year_end_settlements`，再把 settlement status 設為 `SUPERVISOR_SIGNED`。金額落 ±100 萬 CHECK 內。自我驗證：settlements>0 且金額在界內。Commit。

### Task 3.12：m14_audit_misc.py
少量 `audit_logs`、`notification_log`、`vendor_payments`、`employee_offboarding_records`、`disciplinary_actions`、`employee_contracts`/`employee_education`/`employee_certificates`、`dsr_requests`。Read 對應 model。自我驗證：各表>0。Commit。

---

## Phase 4：驗證與端到端實灌

### Task 4.1：verify.py
**Files:** Create `scripts/seedgen/verify.py`。`summary(session)` 印每張被灌表的筆數；`check_consistency(session)`：① closed 月 salary_records.net_pay 全>0；② year_end_settlements 金額在 ±100 萬；③ students.lifecycle_status 全為合法值；④ 無孤兒 FK（抽查 students.classroom_id/guardians.student_id/salary_records.employee_id）。回傳問題清單，`--verify` 模式印出。Commit。

### Task 4.2：orchestrator 全串接
把 __main__.py 的模組 stub 改為真實 import 執行序（m00…m14 + verify），`--only` 仍可單跑。冒煙：`--help` 正常。Commit `feat(seedgen): orchestrator 全模組串接 + verify`。

### Task 4.3：dry-run 端到端（不 wipe，不寫）
`cd <wt>; .venv/bin/python -m scripts.seedgen`（無 --wipe）應印「將清哪些表 / 各模組將建概況」不報錯不寫 DB。修掉任何 import/契約錯。

### Task 4.4：實灌 dev DB + verify
- [ ] 備份保險：`pg_dump` 現有 dev DB 到 `.scratch/ivymanagement-before-seedgen-2026-06-14.sql`（保險，可回滾）。
- [ ] 執行：`cd <wt>; .venv/bin/python -m scripts.seedgen --year 114 --today 2026-02-16 --scale standard --wipe --yes`
- [ ] 跑 `--verify`，確認 consistency 全過、summary 各模組筆數合理。
- [ ] 高價值讀取閘抽查（沿用 spec §9.3）：起 `start.sh`，用 admin 帳號打幾個 API（員工/學生日點名/招生漏斗/考核/年終/聯絡簿）回 200 帶資料；或用既有 e2e/瀏覽器抽查。
- [ ] Commit 任何修補。回報 summary 給 user。

---

## Self-Review（撰寫者檢查，已完成）

- **Spec 覆蓋**：spec §3 套件佈局 14 模組 ↔ Task 1.1/2.x/3.x 一一對應；§4 wipe ↔ Task 0.7；§5 時間模型 ↔ Task 0.5；§6 決定論 ↔ Task 0.1/0.4；§7 引擎整合 ↔ Task 3.4/3.11；§9 驗證 ↔ Task 4.1/4.4；§3.3 護欄 ↔ Task 0.3。無遺漏。
- **Placeholder 掃描**：data 模組刻意以「Read model + 約束清單 + 自我驗證」描述（agent 有 repo 存取，逐欄硬編會使計畫脆裂且過長）；契約/infra 任務給完整程式。非 TODO/TBD。
- **型別一致**：`SeedContext` registry key（employees/employees_by_role/classrooms/users/students/students_active/guardians/class_grades/job_titles）全計畫統一；模組協定 `seed(ctx)` 一致；引擎入口 `process_bulk_salary_calculation` / `build_settlements` 簽名與勘查一致。

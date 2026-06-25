# 薪資設定單一事實來源 + 啟動完整性檢查 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除薪資設定的三處手抄重複（抽 `config_defaults` 單一來源 + 一致性測試守衛）、新增啟動時薪資 config 當年度完整性檢查、查證殘留舊查詢路徑是否繞過 fail-loud。

**Architecture:** 建立在 [`2026-06-05-period-aware-salary-config-resolver`](../specs/2026-06-05-period-aware-salary-config-resolver-design.md) 已落地的 resolver 之上。設定預設數值集中到新模組 `services/salary/config_defaults.py`（純資料、不 import 任何專案模組）；`constants.py` re-export、`seed.py` 索引引用；無法物理合併者（migration 快照、SQLAlchemy column `default=`）以一致性測試防漂移。啟動檢查沿用既有 `check_insurance_brackets_seeded` / `infra_check` 的 loud-not-block 模式。

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy / Alembic / pytest（SQLite 測試環境）

## Global Constraints

- 設計依據：`docs/superpowers/specs/2026-06-25-salary-config-single-source-design.md`（含 §2.0 漂移裁定）。
- **業主裁定**：節慶目標人數正確值 = DB 當前值。`config_defaults.TARGET_ENROLLMENT` 用 **27/25/23/14/13**（大班 2t=27/1t=14、中班 2t=25/1t=13、小班 2t=23/1t=12；`shared_assistant` 與幼幼班維持 20/18/16/12 與 15/7/12，全部 `OVERTIME_TARGET` 維持原值）。
- **fail-loud 邊界不變**：表空→fallback、表有料缺當年度→raise（既有行為，本計畫不改）。
- 零 schema 變更、零新 migration（`PositionSalaryConfig.config_year` 已存在）。
- `config_defaults.py` **不得 import** `models/`、`engine.py`、`constants.py`（避免循環 import；它是最低層純資料）。
- 測試關 coverage 加速：`pytest <path> -o addopts="" -v`。
- Commit：Conventional Commits、繁體中文、一 commit 一件事。BE `.py` Edit 後 PostToolUse 會自動 black。
- 全程在 main 分支併入（未 push，prod 部署由業主決定）；commit 前 `git branch --show-current` 驗證未被平行 session 切走。

---

### Task 1: 建立 `config_defaults.py` 單一來源（含修正後的節慶目標）

**Files:**
- Create: `services/salary/config_defaults.py`
- Test: `tests/test_config_single_source.py`

**Interfaces:**
- Produces: 模組常數 `FESTIVAL_BONUS_BASE`、`TARGET_ENROLLMENT`、`OVERTIME_TARGET`、`OVERTIME_BONUS_PER_PERSON`、`SUPERVISOR_DIVIDEND`、`SUPERVISOR_FESTIVAL_BONUS`、`OFFICE_FESTIVAL_BONUS_BASE`、`POSITION_GRADE_MAP`（型別與現行 `constants.py` 完全相同的巢狀 dict），其中 `TARGET_ENROLLMENT` 採修正值。

- [ ] **Step 1: 寫 config_defaults 模組（純資料，TARGET_ENROLLMENT 用修正值）**

```python
# services/salary/config_defaults.py
"""薪資設定預設值「單一事實來源」。

constants.py（fallback）、startup/seed.py（DB seed）、models/config.py（column default
經一致性測試）皆以此為準。本模組不得 import 任何專案模組（保持最低層純資料）。
"""

# 節慶獎金職位等級對應（A=幼兒園教師, B=教保員, C=助理教保員）
POSITION_GRADE_MAP = {"幼兒園教師": "A", "教保員": "B", "助理教保員": "C"}

# 節慶獎金基數（依職位等級與角色）
FESTIVAL_BONUS_BASE = {
    "head_teacher": {"A": 2000, "B": 2000, "C": 1500},
    "assistant_teacher": {"A": 1200, "B": 1200, "C": 1200},
    "art_teacher": {"A": 2000, "B": 2000, "C": 2000},
}

# 節慶獎金目標人數（2026-06-25 業主裁定：對齊 DB GradeTarget.festival_*）
TARGET_ENROLLMENT = {
    "大班": {"2_teachers": 27, "1_teacher": 14, "shared_assistant": 20},
    "中班": {"2_teachers": 25, "1_teacher": 13, "shared_assistant": 18},
    "小班": {"2_teachers": 23, "1_teacher": 12, "shared_assistant": 16},
    "幼幼班": {"2_teachers": 15, "1_teacher": 7, "shared_assistant": 12},
}

# 超額獎金目標人數（與節慶不同；兩端早已一致，維持原值）
OVERTIME_TARGET = {
    "大班": {"2_teachers": 25, "1_teacher": 13, "shared_assistant": 20},
    "中班": {"2_teachers": 23, "1_teacher": 12, "shared_assistant": 18},
    "小班": {"2_teachers": 21, "1_teacher": 11, "shared_assistant": 16},
    "幼幼班": {"2_teachers": 14, "1_teacher": 7, "shared_assistant": 12},
}

# 超額獎金每人金額（依角色與年級）
OVERTIME_BONUS_PER_PERSON = {
    "head_teacher": {"大班": 400, "中班": 400, "小班": 400, "幼幼班": 450},
    "assistant_teacher": {"大班": 100, "中班": 100, "小班": 100, "幼幼班": 150},
}

# 主管紅利 / 主管節慶 / 行政節慶
SUPERVISOR_DIVIDEND = {"園長": 5000, "主任": 4000, "組長": 3000, "副組長": 1500}
SUPERVISOR_FESTIVAL_BONUS = {"園長": 6500, "主任": 3500, "組長": 2000}
OFFICE_FESTIVAL_BONUS_BASE = {"司機": 1000, "美編": 1000, "行政": 2000}
```

- [ ] **Step 2: 寫測試驗證修正值已就位**

```python
# tests/test_config_single_source.py
from services.salary import config_defaults as cd


def test_target_enrollment_uses_corrected_db_values():
    # 2026-06-25 業主裁定值（對齊 DB GradeTarget）
    assert cd.TARGET_ENROLLMENT["大班"]["2_teachers"] == 27
    assert cd.TARGET_ENROLLMENT["大班"]["1_teacher"] == 14
    assert cd.TARGET_ENROLLMENT["中班"]["2_teachers"] == 25
    assert cd.TARGET_ENROLLMENT["中班"]["1_teacher"] == 13
    assert cd.TARGET_ENROLLMENT["小班"]["2_teachers"] == 23
```

- [ ] **Step 3: 跑測試確認通過**

Run: `pytest tests/test_config_single_source.py::test_target_enrollment_uses_corrected_db_values -o addopts="" -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add services/salary/config_defaults.py tests/test_config_single_source.py
git commit -m "feat(salary): 新增 config_defaults 單一事實來源（節慶目標對齊 DB 裁定值）"
```

---

### Task 2: `constants.py` 改 re-export config_defaults（不再各自硬寫）

**Files:**
- Modify: `services/salary/constants.py:46-108`（移除巢狀 dict 字面值，改 import）
- Test: `tests/test_config_single_source.py`

**Interfaces:**
- Consumes: Task 1 的 `config_defaults` 常數。
- Produces: `constants.py` 既有匯出名（`FESTIVAL_BONUS_BASE` 等）維持可 `from services.salary.constants import X`，但值來自 config_defaults（同一物件）。

- [ ] **Step 1: 寫測試斷言 constants 與 config_defaults 為同一物件**

```python
# tests/test_config_single_source.py 追加
from services.salary import constants


def test_constants_reexports_config_defaults():
    # 同一物件 → 單一來源；改 config_defaults 即改 constants
    assert constants.TARGET_ENROLLMENT is cd.TARGET_ENROLLMENT
    assert constants.FESTIVAL_BONUS_BASE is cd.FESTIVAL_BONUS_BASE
    assert constants.SUPERVISOR_DIVIDEND is cd.SUPERVISOR_DIVIDEND
    assert constants.OVERTIME_TARGET is cd.OVERTIME_TARGET
    assert constants.OVERTIME_BONUS_PER_PERSON is cd.OVERTIME_BONUS_PER_PERSON
    assert constants.SUPERVISOR_FESTIVAL_BONUS is cd.SUPERVISOR_FESTIVAL_BONUS
    assert constants.OFFICE_FESTIVAL_BONUS_BASE is cd.OFFICE_FESTIVAL_BONUS_BASE
    assert constants.POSITION_GRADE_MAP is cd.POSITION_GRADE_MAP
```

- [ ] **Step 2: 跑測試確認失敗（目前各自獨立物件）**

Run: `pytest tests/test_config_single_source.py::test_constants_reexports_config_defaults -o addopts="" -v`
Expected: FAIL（`is` 比對為 False，各自硬寫）

- [ ] **Step 3: 改 constants.py 用 re-export 取代字面值**

把 `constants.py` 第 46–108 行（`POSITION_GRADE_MAP`、`FESTIVAL_BONUS_BASE`、`TARGET_ENROLLMENT`、`OVERTIME_TARGET`、`OVERTIME_BONUS_PER_PERSON`、`SUPERVISOR_DIVIDEND`、`SUPERVISOR_FESTIVAL_BONUS`、`OFFICE_FESTIVAL_BONUS_BASE` 八個巢狀 dict 字面值）整段刪除，替換為：

```python
# 設定預設值統一由 config_defaults 提供（單一事實來源；本檔僅 re-export 供既有 import 路徑）
from services.salary.config_defaults import (  # noqa: F401
    POSITION_GRADE_MAP,
    FESTIVAL_BONUS_BASE,
    TARGET_ENROLLMENT,
    OVERTIME_TARGET,
    OVERTIME_BONUS_PER_PERSON,
    SUPERVISOR_DIVIDEND,
    SUPERVISOR_FESTIVAL_BONUS,
    OFFICE_FESTIVAL_BONUS_BASE,
)
```

（保留 constants.py 其餘常數：`MONTHLY_BASE_DAYS`、加班倍率、`LEAVE_DEDUCTION_RULES`、`DEFAULT_*` 等法規/扣款常數不動。）

- [ ] **Step 4: 跑測試確認通過 + 既有 constants 匯入未壞**

Run: `pytest tests/test_config_single_source.py -o addopts="" -v && pytest tests/test_salary_engine.py -o addopts="" -q`
Expected: PASS（若 `test_salary_engine.py` 不存在，改跑 `pytest tests/ -k salary -o addopts="" -q`）

- [ ] **Step 5: Commit**

```bash
git add services/salary/constants.py tests/test_config_single_source.py
git commit -m "refactor(salary): constants 改 re-export config_defaults，消除巢狀 dict 重複"
```

---

### Task 3: `seed.py` 改引用 config_defaults（消除 seed 硬寫數字）

**Files:**
- Modify: `startup/seed.py:108-218`（`seed_default_configs` 內 BonusConfig 與 GradeTarget 的字面數字）
- Test: `tests/test_config_single_source.py`

**Interfaces:**
- Consumes: Task 1 的 `config_defaults` 常數。
- Produces: seed 寫入 DB 的值 == config_defaults（同一來源）。

- [ ] **Step 1: 寫測試斷言 seed BonusConfig 值來自 config_defaults**

此測試以「seed 後查 DB」驗證；用既有測試 DB fixture（`test_db_session`）。

```python
# tests/test_config_single_source.py 追加
def test_seed_bonus_config_matches_defaults(test_db_session, monkeypatch):
    from startup import seed as seed_mod
    monkeypatch.setattr(seed_mod, "get_session", lambda: test_db_session)
    seed_mod.seed_default_configs()
    from models.config import BonusConfig, GradeTarget
    bc = test_db_session.query(BonusConfig).first()
    assert bc.head_teacher_ab == cd.FESTIVAL_BONUS_BASE["head_teacher"]["A"]
    assert bc.principal_dividend == cd.SUPERVISOR_DIVIDEND["園長"]
    assert bc.overtime_head_baby == cd.OVERTIME_BONUS_PER_PERSON["head_teacher"]["幼幼班"]
    # GradeTarget 節慶目標 == 修正後 TARGET_ENROLLMENT
    big = test_db_session.query(GradeTarget).filter_by(grade_name="大班").first()
    assert big.festival_two_teachers == cd.TARGET_ENROLLMENT["大班"]["2_teachers"]  # 27
    assert big.overtime_two_teachers == cd.OVERTIME_TARGET["大班"]["2_teachers"]    # 25
```

> 注意：若 `test_db_session` 的 monkeypatch 方式與本 repo 慣例不同，依 `tests/conftest.py` 既有 fixture 簽名調整；核心斷言不變。

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_config_single_source.py::test_seed_bonus_config_matches_defaults -o addopts="" -v`
Expected: FAIL（目前 seed 硬寫，且 GradeTarget 大班 festival 為 27 但若改前測試引用修正值仍會比中——主要失敗點為 seed 未引用 cd）

- [ ] **Step 3: 改 seed_default_configs 引用 config_defaults**

`startup/seed.py` 開頭加 `from services.salary import config_defaults as cd`。將 `seed_default_configs` 內 BonusConfig 與 grade_targets 的字面數字改為索引引用，例如：

```python
        if session.query(DBBonusConfig).count() == 0:
            fb = cd.FESTIVAL_BONUS_BASE
            sd = cd.SUPERVISOR_DIVIDEND
            sf = cd.SUPERVISOR_FESTIVAL_BONUS
            of = cd.OFFICE_FESTIVAL_BONUS_BASE
            op = cd.OVERTIME_BONUS_PER_PERSON
            config = DBBonusConfig(
                config_year=2026,
                head_teacher_ab=fb["head_teacher"]["A"],
                head_teacher_c=fb["head_teacher"]["C"],
                assistant_teacher_ab=fb["assistant_teacher"]["A"],
                assistant_teacher_c=fb["assistant_teacher"]["C"],
                principal_festival=sf["園長"],
                director_festival=sf["主任"],
                leader_festival=sf["組長"],
                driver_festival=of["司機"],
                designer_festival=of["美編"],
                admin_festival=of["行政"],
                principal_dividend=sd["園長"],
                director_dividend=sd["主任"],
                leader_dividend=sd["組長"],
                vice_leader_dividend=sd["副組長"],
                overtime_head_normal=op["head_teacher"]["大班"],
                overtime_head_baby=op["head_teacher"]["幼幼班"],
                overtime_assistant_normal=op["assistant_teacher"]["大班"],
                overtime_assistant_baby=op["assistant_teacher"]["幼幼班"],
                school_wide_target=160,
                is_active=True,
            )
            session.add(config)
```

grade_targets 改為從 `cd.TARGET_ENROLLMENT` / `cd.OVERTIME_TARGET` 生成（取代寫死的 27/25/23 list）：

```python
        if session.query(GradeTarget).count() == 0:
            for grade in ("大班", "中班", "小班", "幼幼班"):
                ft = cd.TARGET_ENROLLMENT[grade]
                ot = cd.OVERTIME_TARGET[grade]
                session.add(GradeTarget(
                    config_year=2026,
                    grade_name=grade,
                    festival_two_teachers=ft["2_teachers"],
                    festival_one_teacher=ft["1_teacher"],
                    festival_shared=ft["shared_assistant"],
                    overtime_two_teachers=ot["2_teachers"],
                    overtime_one_teacher=ot["1_teacher"],
                    overtime_shared=ot["shared_assistant"],
                ))
            logger.info("Seeded default grade targets.")
```

> ⚠ 行為注意：此舉把 prod seed 的大班 festival_two_teachers 從 27 維持 27（裁定值），中/小班 25/23 維持——**與現行 seed 值相同**，故對未來 fresh seed 無數字變動；僅是改為引用單一來源。

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_config_single_source.py -o addopts="" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add startup/seed.py tests/test_config_single_source.py
git commit -m "refactor(salary): seed_default_configs 改引用 config_defaults，消除 seed 硬寫"
```

---

### Task 4: 一致性測試守衛（model default / 級距兩份手抄）

**Files:**
- Test: `tests/test_config_single_source.py`

**Interfaces:**
- Consumes: `models.config.BonusConfig`、`services.salary.constants.INSURANCE_TABLE_2026`、migration `20260507_d9e0f1g2h3i4` 的 `_BRACKETS_2026`。

- [ ] **Step 1: 寫 model default 一致性測試（無法物理合併 → 測試守衛）**

SQLAlchemy column `default=` 為避免 model-load 期循環 import，保留字面值；改以測試鎖定它與 config_defaults 一致。

```python
# tests/test_config_single_source.py 追加
def test_model_default_matches_config_defaults():
    from models.config import BonusConfig
    cols = {c.name: c.default.arg for c in BonusConfig.__table__.columns if c.default is not None and not callable(c.default.arg)}
    assert cols["head_teacher_ab"] == cd.FESTIVAL_BONUS_BASE["head_teacher"]["A"]
    assert cols["head_teacher_c"] == cd.FESTIVAL_BONUS_BASE["head_teacher"]["C"]
    assert cols["principal_dividend"] == cd.SUPERVISOR_DIVIDEND["園長"]
    assert cols["director_dividend"] == cd.SUPERVISOR_DIVIDEND["主任"]
    assert cols["overtime_head_baby"] == cd.OVERTIME_BONUS_PER_PERSON["head_teacher"]["幼幼班"]
    assert cols["principal_festival"] == cd.SUPERVISOR_FESTIVAL_BONUS["園長"]
```

- [ ] **Step 2: 寫級距逐筆一致性測試（migration 快照 vs runtime 常數）**

migration 不可 import 活常數，改測試斷言兩份 82 筆逐筆相同。

```python
# tests/test_config_single_source.py 追加
import importlib.util, pathlib


def _load_migration_brackets():
    p = pathlib.Path("alembic/versions/20260507_d9e0f1g2h3i4_insurance_brackets_to_db.py")
    spec = importlib.util.spec_from_file_location("_mig_brackets", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._BRACKETS_2026


def test_insurance_brackets_constant_matches_migration():
    from services.salary.constants import INSURANCE_TABLE_2026  # or services.insurance_service
    mig = _load_migration_brackets()
    assert len(mig) == len(INSURANCE_TABLE_2026)
    # 逐筆比對關鍵欄（依實際 dict key 對齊：amount/insured_amount、labor、health、pension）
    for m, c in zip(sorted(mig, key=lambda r: r["insured_amount"]),
                    sorted(INSURANCE_TABLE_2026, key=lambda r: r["amount"])):
        assert m["insured_amount"] == c["amount"]
        assert m["labor_employee"] == c["labor"]
        assert m["health_employee"] == c["health"]
```

> ⚠ 實作第一步先 `python -c "from services.salary.constants import INSURANCE_TABLE_2026; print(INSURANCE_TABLE_2026[0])"` 與 migration `_BRACKETS_2026[0]` 印出**確認實際 dict key 名**（amount vs insured_amount、labor vs labor_employee…），再對齊上面的 key。若 import 路徑是 `services.insurance_service.INSURANCE_TABLE_2026` 則改之。

- [ ] **Step 3: 跑測試（預期通過；若紅代表抓到既有漂移）**

Run: `pytest tests/test_config_single_source.py -o addopts="" -v`
Expected: PASS。**若 `test_insurance_brackets_constant_matches_migration` 失敗 → 抓到第二處真實漂移，停下回報業主裁定（如同節慶目標），勿擅改級距值。**

- [ ] **Step 4: Commit**

```bash
git add tests/test_config_single_source.py
git commit -m "test(salary): 加 model default 與級距兩份手抄一致性守衛"
```

---

### Task 5: 節慶目標 fallback 修正的 gold 測試對賬

**Files:**
- Test: 既有薪資 gold 測試（grep 定位）

**Interfaces:**
- Consumes: Task 1 修正後的 `TARGET_ENROLLMENT`。

- [ ] **Step 1: 定位所有節慶獎金 gold/回歸測試並判定是否 seed GradeTarget**

Run: `grep -rln "festival\|節慶\|TARGET_ENROLLMENT\|GradeTarget" tests/ | sort -u`
然後對每個檔判定：測試是否在跑前 seed GradeTarget（DB 路徑）或靠 fallback。

- [ ] **Step 2: 跑全部薪資相關測試，蒐集因 24→27 位移而紅的案例**

Run: `pytest tests/ -k "salary or festival or bonus or 薪資 or 節慶" -o addopts="" -q`
Expected: 有 seed GradeTarget 的測試 PASS（零位移）；靠 fallback 且硬鎖舊節慶金額的測試 FAIL。

- [ ] **Step 3: 對每個 FAIL 的 fallback gold，重算期望值並重鎖**

對每個失敗測試：確認失敗原因確為節慶目標 24→27（而非真 bug），用新目標人數重算正確期望節慶獎金，更新斷言。在測試加註：`# 期望值依 2026-06-25 業主裁定節慶目標(27/25/23) 重鎖`。

- [ ] **Step 4: 跑測試確認全綠**

Run: `pytest tests/ -k "salary or festival or bonus or 薪資 or 節慶" -o addopts="" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test(salary): 節慶目標 fallback 修正(24→27)後重鎖受影響 gold 測試"
```

---

### Task 6: 查證 `_load_config_from_db_locked` 是否繞過 fail-loud

**Files:**
- Investigate: `services/salary/engine.py:747` 附近 `_load_config_from_db_locked`、`load_config_from_db`、`startup/bootstrap.py:153`、`main.py:193`
- Modify（依查證結果）：`engine.py` 該函式或加註解

**Interfaces:**
- Consumes: 既有 `resolve_config`（`services/salary/config_resolver.py`）。

- [ ] **Step 1: 追查 baseline 載入值是否進入任何金流計算結果**

判斷：實算路徑是否一律經 `config_for_month`（per-month resolver）覆蓋 `_load_config_from_db_locked` 設的 instance 值？檢查所有讀 `self._bonus_base`/`self._target_enrollment`/`self.insurance_service.table` 等 instance 狀態的計算點，是否都在 `config_for_month` context 內。重點查 simulate 路徑（`api/salary/simulate.py`）與任何不開 `config_for_month` 的計算入口。

- [ ] **Step 2A: 若證明為純 baseline（實算必被覆蓋）→ 加註解，不改邏輯**

在 `_load_config_from_db_locked` 上方加：

```python
        # 注意：此處用 is_active+id.desc() 載入「啟動 baseline」instance 狀態，僅供
        # 無 per-month context 的相容路徑與顯示；所有實際金流計算一律經 config_for_month
        # → resolve_config（period-aware + fail-loud）覆蓋此 baseline，不會用到這裡撿的值。
        # 收斂走 resolver 列為低優先 follow-up（見 2026-06-25 spec §3 / §8）。
```

- [ ] **Step 2B: 若發現任何金流路徑直接讀 baseline（後門）→ 收斂走 resolver**

把該函式內 InsuranceRate/AttendancePolicy/BonusConfig 的 `filter(is_active==True).order_by(id.desc())` 改為 `resolve_config(session, Model, year=<當前年度>, year_col=...)`，並補一條「baseline 缺當年度 → fail-loud」回歸測試（比照實算路徑）。

- [ ] **Step 3: 跑薪資測試確認無回歸**

Run: `pytest tests/ -k salary -o addopts="" -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add services/salary/engine.py tests/
git commit -m "refactor(salary): _load_config_from_db_locked baseline 路徑查證並收斂/註記"
```

---

### Task 7: 啟動薪資 config 當年度完整性檢查

**Files:**
- Create: `startup/salary_config_check.py`
- Modify: `main.py:247-252`（在既有 `check_insurance_brackets_seeded` 後追加呼叫）
- Test: `tests/test_salary_config_startup_check.py`

**Interfaces:**
- Consumes: `models.config.SALARY_CONFIG_YEAR_COLUMNS`（已存在於 `models/config.py:185`）、5 個 config model。
- Produces: `check_salary_configs_current_year(session, year: int) -> list[str]`（回傳缺當年度的表名清單；表空→不列入）。

- [ ] **Step 1: 寫測試（齊全→空 / 表空不報 / 缺年度入清單 / 非 PG 安全）**

```python
# tests/test_salary_config_startup_check.py
from startup.salary_config_check import check_salary_configs_current_year
from models.config import BonusConfig, InsuranceRate


def test_all_present_returns_empty(test_db_session):
    # seed 當年度 BonusConfig + InsuranceRate（其餘表略，視 fixture）
    test_db_session.add(BonusConfig(config_year=2026, is_active=True))
    test_db_session.add(InsuranceRate(rate_year=2026, is_active=True))
    test_db_session.commit()
    missing = check_salary_configs_current_year(test_db_session, 2026)
    assert "BonusConfig" not in missing
    assert "InsuranceRate" not in missing


def test_empty_table_not_reported(test_db_session):
    # 完全空表 → 不列入缺漏（dev/test/fresh by design）
    missing = check_salary_configs_current_year(test_db_session, 2026)
    assert "BonusConfig" not in missing


def test_has_data_but_missing_year_reported(test_db_session):
    test_db_session.add(BonusConfig(config_year=2025, is_active=True))
    test_db_session.commit()
    missing = check_salary_configs_current_year(test_db_session, 2026)
    assert "BonusConfig" in missing
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_salary_config_startup_check.py -o addopts="" -v`
Expected: FAIL（module not found）

- [ ] **Step 3: 寫 check 實作**

```python
# startup/salary_config_check.py
"""啟動時驗證各薪資 config 表「當年度」列齊全（read-only, loud-not-block）。

邊界對齊實算 fail-loud：表完全空 → 不報（dev/test/fresh 靠內建常數）；
表有料卻獨缺當年度 → 列入缺漏（prod 配置遺漏指紋）。缺漏推 Sentry + error log，
絕不 raise、絕不擋 boot。
"""
import logging

from models.config import (
    SALARY_CONFIG_YEAR_COLUMNS,
    BonusConfig, InsuranceRate, InsuranceBracket,
    PositionSalaryConfig, AttendancePolicy,
)

logger = logging.getLogger(__name__)

_MODELS = {
    "BonusConfig": BonusConfig,
    "InsuranceRate": InsuranceRate,
    "InsuranceBracket": InsuranceBracket,
    "PositionSalaryConfig": PositionSalaryConfig,
    "AttendancePolicy": AttendancePolicy,
}


def check_salary_configs_current_year(session, year: int) -> list[str]:
    missing: list[str] = []
    for name, model in _MODELS.items():
        col = getattr(model, SALARY_CONFIG_YEAR_COLUMNS[name])
        try:
            total = session.query(model).count()
            if total == 0:
                continue  # 表空：dev/test/fresh，靠內建常數，不報
            if session.query(model).filter(col == year).count() == 0:
                missing.append(name)  # 有料卻缺當年度
        except Exception:  # noqa: BLE001
            logger.exception("salary config check failed for %s", name)
    return missing
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_salary_config_startup_check.py -o addopts="" -v`
Expected: PASS

- [ ] **Step 5: 接上 main.py 啟動（loud + Sentry，不擋 boot）**

在 `main.py` 既有 `check_insurance_brackets_seeded` 呼叫區塊（約 :247-252）之後追加：

```python
        from startup.salary_config_check import check_salary_configs_current_year
        from utils.dates import today_taipei  # 若 main 已 import 則略
        try:
            _missing = check_salary_configs_current_year(_ins_session, today_taipei().year)
            if _missing:
                logger.error("薪資設定缺當年度列：%s（fallback 將用內建常數，請補設定）", _missing)
                try:
                    import sentry_sdk
                    sentry_sdk.capture_message(
                        f"salary config missing current-year rows: {_missing}", level="error")
                except ImportError:
                    pass
        except Exception:  # noqa: BLE001
            logger.exception("salary config startup check failed")
```

> 沿用既有 `_ins_session` 與 try/except 包裹（與 `check_insurance_brackets_seeded` 同段），確保任何例外都不擋 boot。實作時對齊 main.py:247 附近實際變數名。

- [ ] **Step 6: 跑啟動相關測試 + 全套薪資回歸**

Run: `pytest tests/test_salary_config_startup_check.py tests/ -k "salary or startup" -o addopts="" -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add startup/salary_config_check.py main.py tests/test_salary_config_startup_check.py
git commit -m "feat(salary): 啟動驗證薪資 config 當年度齊全（loud 不擋 boot）"
```

---

### Task 8: 全套件回歸 + 收尾

- [ ] **Step 1: 跑完整測試套件確認零回歸**

Run: `pytest tests/ -q`（CI 設定；本地可分段）
Expected: 全綠（除預期重鎖的 gold 已於 Task 5 處理）

- [ ] **Step 2: 確認 main 分支、commit 完整**

Run: `git branch --show-current && git log --oneline -8`
Expected: branch=main，8 個 commit 對應 Task 1–7。

- [ ] **Step 3: 提醒業主收尾**

回報：實作完成、全套件綠。**push 與 prod 部署由業主決定**（push 後端會觸發 Zeabur 部署）。本批無 migration、無 schema 變更，prod 風險限於 code 行為（節慶 fallback 修正不影響 prod DB 路徑）。

---

## Self-Review

**Spec coverage：**
- §2.0 漂移裁定 → Task 1（修正值）+ Task 5（gold 重鎖）✅
- §2.1 抽 config_defaults → Task 1/2/3 ✅
- §2.2 一致性守衛（級距 + 三處 defaults）→ Task 3（seed==defaults）+ Task 4（model default + 級距）✅
- §3 殘留路徑收斂查證 → Task 6 ✅
- §4 啟動完整性檢查 → Task 7 ✅
- §6 零 schema 變更 → 全程無 migration ✅

**Placeholder scan：** Task 6 為「查證後二擇一（2A/2B）」，非 placeholder——兩分支皆有完整代碼/條件。Task 4 Step 2 與 Task 5 Step 1 含「先 grep/印出確認實際 key/檔名」的明確前置動作，非含糊。

**Type consistency：** `check_salary_configs_current_year(session, year) -> list[str]` 在 Task 7 定義與測試一致；`config_defaults` 常數名在 Task 1 定義、Task 2/3/4 引用一致；`SALARY_CONFIG_YEAR_COLUMNS` 引用 `models/config.py:185` 既有定義。

**已知實作期需現場確認（已在步驟標注）：** ① INSURANCE_TABLE_2026 / _BRACKETS_2026 的實際 dict key 名（Task 4 Step 2 前置印出）② `test_db_session` fixture monkeypatch 慣例（Task 3 Step 1 注記）③ main.py:247 實際變數名（Task 7 Step 5 注記）。

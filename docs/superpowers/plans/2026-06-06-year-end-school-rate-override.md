# 年終全校達成率 HR 手動覆寫（Phase 1）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 HR 能手動覆寫年終每學期「全校達成率」，使原生 build 路徑的年終金額 100% 對上園所 Excel（系統自算值降為「建議值」）。

**Architecture:** `OrgYearSettings` 新增 nullable `school_achievement_rate_override` 欄與 `effective_school_achievement_rate` property（override 優先、否則自算）。`refresh_enrollment_rates` 不動（仍只寫自算欄）。settlement_builder 兩處 school-rate 讀取點改讀 effective，覆寫值即往下傳到 step3。既有 `POST /year_end/cycles/{cycle_id}/org_settings` 端點透過擴充 schema 自動支援；reset 時清回 None。前端 `YearEndConfigView.vue` 加輸入欄 + `gen:api`。

**Tech Stack:** FastAPI / SQLAlchemy 2.0（`Mapped`）/ Alembic / Pydantic v2 / pytest（in-memory SQLite）/ Vue 3 `<script setup lang="ts">` / Vitest。

**分支：** 後端 `feat/year-end-school-rate-override-be`（spec commit `f1fe209` 已在此分支）；前端另開 `feat/year-end-school-rate-override-fe`。後端一筆/前端一筆 commit。

---

### Task 1: 資料模型 — 新增 override 欄 + effective property

**Files:**
- Modify: `models/year_end.py`（`OrgYearSettings`，約 185-195 行 `school_achievement_rate` 之後）
- Test: `tests/test_year_end_school_rate_override.py`（新檔）

- [ ] **Step 1: 寫失敗測試（property）**

`tests/test_year_end_school_rate_override.py`：
```python
"""Phase1: 全校達成率 HR 覆寫 — property / resolver / 端到端金額。"""
from __future__ import annotations
import os, sys
from decimal import Decimal
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from models.year_end import OrgYearSettings


def test_effective_rate_uses_auto_when_no_override():
    o = OrgYearSettings(
        school_achievement_rate=Decimal("91.48"),
        school_achievement_rate_override=None,
    )
    assert o.effective_school_achievement_rate == Decimal("91.48")


def test_effective_rate_uses_override_when_set():
    o = OrgYearSettings(
        school_achievement_rate=Decimal("91.48"),
        school_achievement_rate_override=Decimal("91.5"),
    )
    assert o.effective_school_achievement_rate == Decimal("91.5")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_year_end_school_rate_override.py -q`
Expected: FAIL — `TypeError: 'school_achievement_rate_override' is an invalid keyword argument` 或 `AttributeError: ... effective_school_achievement_rate`。

- [ ] **Step 3: 加欄位 + property**

`models/year_end.py`，在 `school_achievement_rate` 欄位定義之後、`org_achievement_rate` 之前插入欄位：
```python
    school_achievement_rate_override: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 3),
        nullable=True,
        comment="HR 手動覆寫全校達成率；NULL=用自算 school_achievement_rate",
    )
```
並在 `OrgYearSettings` class 內（建議放 `updated_at` 之後、class 結尾）新增 property：
```python
    @property
    def effective_school_achievement_rate(self) -> Decimal:
        """HR 覆寫優先；未覆寫（NULL）則用自算 school_achievement_rate。"""
        if self.school_achievement_rate_override is not None:
            return self.school_achievement_rate_override
        return self.school_achievement_rate
```
（`Optional`、`Decimal`、`Mapped`、`mapped_column`、`Numeric` 在本檔已 import，沿用即可。）

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_year_end_school_rate_override.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add models/year_end.py tests/test_year_end_school_rate_override.py
git commit -m "feat(year-end): OrgYearSettings 加 school_achievement_rate_override 欄 + effective property"
```

---

### Task 2: Alembic migration（新增 nullable 欄）

**Files:**
- Create: `alembic/versions/20260606_yeschr01_org_year_settings_school_rate_override.py`

- [ ] **Step 1: 寫 migration**

```python
"""org_year_settings 加 school_achievement_rate_override（HR 手動覆寫全校達成率）

NULL=用自算 school_achievement_rate。純 nullable add column，零回填、可逆。

Revision ID: yeschr01
Revises: cfgyear01
Create Date: 2026-06-06
"""

from alembic import op
import sqlalchemy as sa

revision = "yeschr01"
down_revision = "cfgyear01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "org_year_settings",
        sa.Column(
            "school_achievement_rate_override",
            sa.Numeric(6, 3),
            nullable=True,
            comment="HR 手動覆寫全校達成率；NULL=用自算 school_achievement_rate",
        ),
    )


def downgrade():
    op.drop_column("org_year_settings", "school_achievement_rate_override")
```

- [ ] **Step 2: 確認單一 head**

Run: `cd ~/Desktop/ivy-backend && python -m alembic heads`
Expected: 只有 `yeschr01 (head)`（若出現多 head 代表平行 session 又加了 migration，需 `alembic merge` — 停下回報）。

- [ ] **Step 3: upgrade / downgrade round-trip（dev DB）**

Run:
```bash
python -m alembic upgrade head
python -m alembic downgrade -1
python -m alembic upgrade head
```
Expected: 三步皆無錯；最後停在 `yeschr01`。

- [ ] **Step 4: Commit**

```bash
git add alembic/versions/20260606_yeschr01_org_year_settings_school_rate_override.py
git commit -m "feat(year-end): migration yeschr01 — org_year_settings.school_achievement_rate_override"
```

---

### Task 3: Pydantic schema — 擴充 Create / Out

**Files:**
- Modify: `schemas/year_end.py`（`OrgYearSettingsCreate` 約 47-53、`OrgYearSettingsOut` 約 56-65）
- Test: `tests/test_year_end_school_rate_override.py`

- [ ] **Step 1: 寫失敗測試（schema 帶 override + effective）**

於 `tests/test_year_end_school_rate_override.py` 末尾追加：
```python
from schemas.year_end import OrgYearSettingsCreate, OrgYearSettingsOut


def test_create_schema_accepts_override():
    c = OrgYearSettingsCreate(
        semester_first=True, org_achievement_rate=Decimal("0"),
        school_achievement_rate_override=Decimal("91.5"),
    )
    assert c.school_achievement_rate_override == Decimal("91.5")


def test_create_schema_override_defaults_none():
    c = OrgYearSettingsCreate(semester_first=True, org_achievement_rate=Decimal("0"))
    assert c.school_achievement_rate_override is None


def test_out_schema_exposes_effective():
    o = OrgYearSettings(
        id=1, year_end_cycle_id=1, semester_first=True, enrollment_target=176,
        enrollment_actual=161, school_achievement_rate=Decimal("91.48"),
        school_achievement_rate_override=Decimal("91.5"),
        org_achievement_rate=Decimal("0"), meeting_absence_deduction=Decimal("1000"),
    )
    out = OrgYearSettingsOut.model_validate(o)
    assert out.school_achievement_rate_override == Decimal("91.5")
    assert out.effective_school_achievement_rate == Decimal("91.5")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_year_end_school_rate_override.py -q`
Expected: FAIL（Create 不認 override kwarg / Out 無 effective 欄）。

- [ ] **Step 3: 擴充 schema**

`schemas/year_end.py`，`OrgYearSettingsCreate` 加一欄（放 `school_achievement_rate` 之後）：
```python
    school_achievement_rate_override: Optional[Decimal] = None
```
`OrgYearSettingsOut` 加兩欄（放 `school_achievement_rate` 之後）：
```python
    school_achievement_rate_override: Optional[Decimal]
    effective_school_achievement_rate: Decimal
```
（`Optional` / `Decimal` 在本檔已 import；`OrgYearSettingsOut` 已是 `model_config = ConfigDict(from_attributes=True)`，會自動讀 model 的 `effective_school_achievement_rate` property。）

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_year_end_school_rate_override.py -q`
Expected: PASS（5 passed）。

- [ ] **Step 5: Commit**

```bash
git add schemas/year_end.py tests/test_year_end_school_rate_override.py
git commit -m "feat(year-end): OrgYearSettings schema 加 override + effective_school_achievement_rate"
```

---

### Task 4: Resolver — settlement_builder 兩處改讀 effective

**Files:**
- Modify: `services/year_end/settlement_builder.py`
  - `gather_performance_rates` standalone 路徑（約 504-512：`org_first.school_achievement_rate` / `org_second.school_achievement_rate`）
  - `_school_rates` 預查（約 779-794：`select(OrgYearSettings.school_achievement_rate)` 兩段）
- Test: `tests/test_year_end_settlement_builder.py`（重用既有 `session` fixture 與 `_emp` helper）

- [ ] **Step 1: 寫失敗測試（gather 讀 effective）**

於 `tests/test_year_end_settlement_builder.py` 末尾追加（重用該檔既有 `session` fixture 與 `_emp()` helper；`_emp()` 是非帶班 stub，使 class_* 走 None 分支）。`YearEndCycle` 必填欄位為 `academic_year / start_date / end_date / bonus_calc_date`（**非 `name`**）：
```python
from datetime import date as _date
from decimal import Decimal as _D
from models.year_end import OrgYearSettings as _Org, YearEndCycle as _Cycle


def _mk_cycle(session):
    cyc = _Cycle(academic_year=114, start_date=_date(2025, 2, 1),
                 end_date=_date(2026, 1, 31), bonus_calc_date=_date(2026, 1, 15))
    session.add(cyc); session.flush()
    return cyc


def test_gather_uses_override_school_rate(session):
    cyc = _mk_cycle(session)
    # 上學期(first)：自算 91.48、HR 覆寫 91.5
    session.add(_Org(year_end_cycle_id=cyc.id, semester_first=True,
                     enrollment_target=176, school_achievement_rate=_D("91.48"),
                     school_achievement_rate_override=_D("91.5"),
                     org_achievement_rate=_D("0")))
    # 下學期(second)：只有自算 75.6、無覆寫
    session.add(_Org(year_end_cycle_id=cyc.id, semester_first=False,
                     enrollment_target=160, school_achievement_rate=_D("75.6"),
                     school_achievement_rate_override=None,
                     org_achievement_rate=_D("0")))
    session.flush()
    rates = sb.gather_performance_rates(session, cyc, _emp(), school_rates=None)
    assert rates.school_rate_first == _D("91.5")    # 用覆寫
    assert rates.school_rate_second == _D("75.6")   # 無覆寫→自算


def test_refresh_enrollment_rates_does_not_clobber_override(session):
    """spec §3.6 #5：refresh 只回填自算欄，不可洗掉 HR 覆寫。"""
    cyc = _mk_cycle(session)
    org = _Org(year_end_cycle_id=cyc.id, semester_first=True, enrollment_target=176,
               school_achievement_rate=_D("91.48"),
               school_achievement_rate_override=_D("91.5"), org_achievement_rate=_D("0"))
    session.add(org); session.flush()
    sb.refresh_enrollment_rates(session, cyc)  # 無學生→自算欄會被重算（可能變 0）
    session.refresh(org)
    assert org.school_achievement_rate_override == _D("91.5")  # 覆寫不被動
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_year_end_settlement_builder.py::test_gather_uses_override_school_rate -q`
Expected: FAIL — `school_rate_first` 為 `91.48`（仍讀自算欄）而非 `91.5`。

- [ ] **Step 3: 改 gather standalone 路徑**

`services/year_end/settlement_builder.py`，`gather_performance_rates` 內 standalone 分支：
```python
        school_first = (
            Decimal(str(org_first.effective_school_achievement_rate)) if org_first else None
        )
        school_second = (
            Decimal(str(org_second.effective_school_achievement_rate)) if org_second else None
        )
```
（把兩處 `.school_achievement_rate` 換成 `.effective_school_achievement_rate`。）

- [ ] **Step 4: 改 `_school_rates` 預查改 select 整列**

把兩段 `select(OrgYearSettings.school_achievement_rate)` 改為 select 整列物件，並用 property：
```python
    _org_first = db.scalar(
        select(OrgYearSettings).where(
            OrgYearSettings.year_end_cycle_id == cycle.id,
            OrgYearSettings.semester_first == True,  # noqa: E712
        )
    )
    _org_second = db.scalar(
        select(OrgYearSettings).where(
            OrgYearSettings.year_end_cycle_id == cycle.id,
            OrgYearSettings.semester_first == False,  # noqa: E712
        )
    )
    _school_rates = (
        Decimal(str(_org_first.effective_school_achievement_rate))
        if _org_first is not None else None,
        Decimal(str(_org_second.effective_school_achievement_rate))
        if _org_second is not None else None,
    )
```

- [ ] **Step 5: 跑測試確認通過 + 不回歸**

Run: `python -m pytest tests/test_year_end_settlement_builder.py tests/test_year_end_enrollment_rates.py -q`
Expected: PASS（含新測試 + 既有全綠）。

- [ ] **Step 6: Commit**

```bash
git add services/year_end/settlement_builder.py tests/test_year_end_settlement_builder.py
git commit -m "feat(year-end): settlement_builder 全校達成率改讀 effective（HR 覆寫優先）"
```

---

### Task 5: 端點透傳 override + reset 清除

**Files:**
- Modify: `api/year_end/__init__.py`（reset 內 OrgYearSettings 複製，約 137-148）
- Test: `tests/test_year_end_grid_api.py`（重用 `client_with_db` fixture）

> `POST /cycles/{cycle_id}/org_settings`（`upsert_org_settings`）用 `payload.model_dump()` + setattr 全欄寫入，Task 3 schema 加欄後**已自動透傳 override**，端點本體不需改。本 task 補測試 + 修 reset。

- [ ] **Step 1: 寫失敗測試（upsert 透傳 override + clone 清除）**

於 `tests/test_year_end_grid_api.py` 末尾追加（鏡像該檔既有 B1 `test_upsert_class_target` 與 B2 `test_create_cycle_clone_previous` 範式：`client_with_db` 回 `(client, sf)`，用 `_seed_users` / `_seed_cycle_and_employee` / `_login` / `_seed_finalize_user`）：
```python
def test_org_settings_upsert_override_and_effective(client_with_db):
    client, sf = client_with_db
    _seed_users(sf)
    cycle_id, _ = _seed_cycle_and_employee(sf)  # 已種兩筆 org_settings
    _login(client)
    res = client.post(f"/api/year_end/cycles/{cycle_id}/org_settings", json={
        "semester_first": True, "enrollment_target": 176,
        "org_achievement_rate": "0", "school_achievement_rate_override": "91.5",
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert Decimal(str(body["school_achievement_rate_override"])) == Decimal("91.5")
    assert Decimal(str(body["effective_school_achievement_rate"])) == Decimal("91.5")
    got = client.get(f"/api/year_end/cycles/{cycle_id}/org_settings").json()
    first = [o for o in got if o["semester_first"]][0]
    assert Decimal(str(first["effective_school_achievement_rate"])) == Decimal("91.5")


def test_clone_clears_school_rate_override(client_with_db):
    client, sf = client_with_db
    _seed_users(sf); _seed_finalize_user(sf)
    cycle_id, _ = _seed_cycle_and_employee(sf)  # cycle 114
    _login(client)
    client.post(f"/api/year_end/cycles/{cycle_id}/org_settings", json={
        "semester_first": True, "enrollment_target": 176,
        "org_achievement_rate": "0", "school_achievement_rate_override": "91.5",
    })
    _login(client, "admin2")  # FINALIZE 權限才能建/clone cycle
    res = client.post("/api/year_end/cycles", json={
        "academic_year": 115, "start_date": "2026-08-01", "end_date": "2027-07-31",
        "bonus_calc_date": "2027-01-15", "clone_from_academic_year": ACADEMIC_YEAR,
    })
    assert res.status_code == 200, res.text
    new_id = res.json()["id"]
    org = client.get(f"/api/year_end/cycles/{new_id}/org_settings").json()
    first = [o for o in org if o["semester_first"]][0]
    assert first["school_achievement_rate_override"] is None  # clone 不沿用覆寫
```
> Pydantic `Decimal` JSON 序列化為字串，故用 `Decimal(str(...))` 比對；override 清除後 JSON 為 `null` → Python `None`。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest "tests/test_year_end_grid_api.py::test_org_settings_upsert_override_and_effective" "tests/test_year_end_grid_api.py::test_clone_clears_school_rate_override" -q`
Expected: FAIL — upsert 回應無 `effective_school_achievement_rate` 欄（Task 3 未跑時）或 clone 後 override 仍帶 91.5（reset 未清）。

- [ ] **Step 3: reset 複製時清 override**

`api/year_end/__init__.py`，reset 內 `OrgYearSettings(...)` 複製區塊（`school_achievement_rate=Decimal("0")` 那段）新增一行：
```python
                    school_achievement_rate=Decimal("0"),  # 重置
                    school_achievement_rate_override=None,  # 重置：HR 覆寫不沿用到新週期
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest "tests/test_year_end_grid_api.py::test_org_settings_upsert_override_and_effective" "tests/test_year_end_grid_api.py::test_clone_clears_school_rate_override" -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**

```bash
git add api/year_end/__init__.py tests/test_year_end_grid_api.py
git commit -m "feat(year-end): org_settings 端點透傳 override + reset 清除"
```

---

### Task 6: 端到端金額驗證 — HR 覆寫 → 呂麗珍 = Excel 38052.04

**Files:**
- Test: `tests/test_year_end_school_rate_override.py`

- [ ] **Step 1: 寫端到端測試（純計算層，釘住金額）**

於 `tests/test_year_end_school_rate_override.py` 末尾追加：
```python
from services.year_end.settlement_builder import resolve_org_achievement_rate
from services.year_end.engine import compute_gross_amount, compute_subtotal_amount


def test_override_propagates_to_excel_amount_lvyu_lijhen():
    # HR 覆寫：下學期 75.6 / 上學期 91.5（園所 Excel 值）
    org_rate = resolve_org_achievement_rate(
        Decimal("75.6"), Decimal("91.5"), worked_first=True, worked_second=True
    )
    assert org_rate == Decimal("83.6")  # _q1((75.6+91.5)/2)=83.55→83.6（vs 自算91.48→83.5）
    # 呂麗珍：base 44300 + 節慶 6500、平均績效 89.6%
    gross = compute_gross_amount(Decimal("44300"), Decimal("6500"), Decimal("89.6"))
    subtotal = compute_subtotal_amount(gross, org_rate)
    assert subtotal == Decimal("38052.04")  # ＝義華 Excel「年終獎金」呂麗珍小計
```

- [ ] **Step 2: 跑測試確認通過（此 task 不需先紅；驗證鏈路成立）**

Run: `python -m pytest tests/test_year_end_school_rate_override.py -q`
Expected: PASS（全檔通過）。若 `subtotal` 非 `38052.04` 代表上游 step2/step3 量化改變，停下回報。

- [ ] **Step 3: 後端全套件不回歸**

Run: `python -m pytest tests/ -q -k "year_end or settlement or enrollment_rate"`
Expected: 全綠（年終相關全部通過）。

- [ ] **Step 4: Commit**

```bash
git add tests/test_year_end_school_rate_override.py
git commit -m "test(year-end): 端到端釘住 HR 覆寫→呂麗珍年終小計 38052.04（=Excel）"
```

---

### Task 7: 前端 — 年終人工步驟加「全校達成率」覆寫欄 + gen:api

**Files:**
- Modify: `ivy-frontend/src/views/yearEnd/YearEndConfigView.vue`（全校設定/org_settings 編輯區）
- 重生型別：`ivy-frontend/src/api/_generated/schema.d.ts`（`npm run gen:api`）
- 分支：`feat/year-end-school-rate-override-fe`（從前端 main 切）

> `src/api/yearEnd.ts` 已有 `getOrgSettings` / `upsertOrgSettings`，後端 schema 加欄後 gen:api 自動帶型別，**api wrapper 不需改**。

- [ ] **Step 1: 重生 OpenAPI 型別**

Run:
```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
cd ~/Desktop/ivy-frontend && npm run gen:api
```
Expected: `src/api/_generated/schema.d.ts` 內 `OrgYearSettingsOut` 出現 `school_achievement_rate_override` 與 `effective_school_achievement_rate`，`OrgYearSettingsCreate` 出現 `school_achievement_rate_override`。

- [ ] **Step 2: 在 `YearEndConfigView.vue` 找到 org_settings 編輯區**

Run: `grep -n "org_settings\|OrgSettings\|school_achievement\|enrollment_target\|達成" src/views/yearEnd/YearEndConfigView.vue`
確認上/下學期 OrgYearSettings 的表單/輸入區位置（沿用該區既有 `el-form` / `el-input-number` 樣式）。

- [ ] **Step 3: 加「全校達成率（HR 覆寫）」輸入欄**

在上、下學期各自的設定區，新增一個 `el-input-number`（綁 `school_achievement_rate_override`，允許清空＝送 `null`），label「全校達成率（覆寫）」，並用 placeholder/旁註顯示系統建議值 `school_achievement_rate`，例如：
```vue
<el-form-item label="全校達成率（覆寫）">
  <el-input-number
    v-model="form.school_achievement_rate_override"
    :precision="3" :step="0.1" :min="0" :max="200" :controls="false"
    :placeholder="`系統建議 ${row.school_achievement_rate ?? '—'}`"
    clearable
  />
  <span class="hint">空白＝用系統自算 {{ row.school_achievement_rate ?? '—' }}</span>
</el-form-item>
```
> 依該 view 實際的資料綁定變數名調整（`form` / `row` 換成該區實際 reactive 物件）；送出 upsert 時，欄位為空要送 `null`（清除覆寫），不要送 `0`（`0` 會被當成「覆寫為 0%」）。

- [ ] **Step 4: typecheck + 既有測試不回歸**

Run:
```bash
cd ~/Desktop/ivy-frontend && npm run typecheck && npm run gen:api:check
```
Expected: typecheck exit 0；gen:api:check 無漂移（schema.d.ts 已是最新）。
若該 view 有 co-located vitest，補一個「空欄送 null、填值送該值」的測試並 `npx vitest run` 對應檔。

- [ ] **Step 5: Commit（前端分支）**

```bash
cd ~/Desktop/ivy-frontend
git add src/views/yearEnd/YearEndConfigView.vue src/api/_generated/schema.d.ts
git commit -m "feat(year-end): 全校達成率 HR 覆寫輸入欄（年終設定）"
```

---

## 收尾（人工驗證，非自動）

- [ ] `start.sh` 起兩端，年終設定頁填上/下學期達成率 75.6 / 91.5 → 重建 grid → 抽查呂麗珍年終小計 ≈ 38052.04。
- [ ] 清空覆寫欄 → 重建 → 數字回到系統自算（驗證 null＝回退）。
- [ ] push 前：後端先（含 migration `yeschr01`，prod 需 `alembic upgrade heads`）、前端後；`gen:api:check` 綠；確認 alembic 單一 head。
- [ ] Phase 2（系統自算預繳率）另開 spec，待業主定義口徑（見設計 spec §6）。

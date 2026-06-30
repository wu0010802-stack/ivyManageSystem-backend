# 薪資「額外加給」手填欄位 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在月薪明細新增「額外加給」手填欄位（金額 `extra_allowance` + 名目 `extra_allowance_label`），會計透過既有手動調整介面輸入，金額併入應領、隨實領進薪資轉帳名冊，薪資單顯示名目；不計入二代健保補充保費。

**Architecture:** 後端在 `SalaryRecord` 加兩欄 + migration；手填值進 gross 的關鍵點為 `totals.py:recompute_record_totals`（手動調整/重算路徑），`engine.py` gross 組裝同步；沿用既有 `manual_overrides` 機制保留手填值。前端在 `SalaryView.vue` 手動調整加金額欄 + 名目輸入。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（PostgreSQL；測試用 SQLite in-memory + `Base.metadata.create_all`）；Vue 3 `<script setup lang="ts">` + Element Plus + vitest。

**參考 spec：** `docs/superpowers/specs/2026-06-04-salary-extra-allowance-field-design.md`

**分支：** 後端 `feat/salary-extra-allowance-2026-06-04-be`、前端 `feat/salary-extra-allowance-2026-06-04-fe`。後端先（migration 先行）。所有 commit 用具名檔案（避免帶入 working tree 其他 WIP）。

---

## 後端

### Task 1: SalaryRecord + SalarySnapshot 加兩欄 + alembic migration

**Files:**
- Modify: `models/salary.py`（`SalaryRecord` 欄位區，`other_deduction`(行 232) 之後 / `gross_salary`(行 239) 之前；**以及** `SalarySnapshot` 對齊欄位區，`unused_leave_payout`(行 408) 之後 / `remark`(行 409) 之前）
- Create: `alembic/versions/xtraallw01_add_salary_extra_allowance.py`

> 已查核：`Money` = `Numeric(12, 2)`（`models/types.py`）；table 名 `salary_records` / `salary_snapshots`；`String` 已 import。SalarySnapshot 透過 `_payload_columns()`「兩表欄位交集」反射複製 → **兩個 model 都要加，否則快照遺失**。

- [ ] **Step 1: `SalaryRecord` 新增兩欄**（`other_deduction` 之後）

```python
    extra_allowance = Column(
        Money, default=0, comment="額外加給（值週/活動加班費等，手填）"
    )
    extra_allowance_label = Column(
        String(50), nullable=True, comment="額外加給名目"
    )
```

- [ ] **Step 2: `SalarySnapshot` 同步新增兩欄**（`unused_leave_payout` 之後、`remark` 之前；比照同區 `server_default="0", nullable=False` 模式）

```python
    extra_allowance = Column(
        Money, default=0, server_default="0", nullable=False
    )
    extra_allowance_label = Column(String(50), nullable=True)
```

- [ ] **Step 3: 建立 migration（兩表各加兩欄）**

`alembic/versions/xtraallw01_add_salary_extra_allowance.py`：

```python
"""add salary extra_allowance + extra_allowance_label (records + snapshots)

Revision ID: xtraallw01
Revises: allergyenc01
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = "xtraallw01"
down_revision = "allergyenc01"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("salary_records", "salary_snapshots"):
        op.add_column(
            table,
            sa.Column("extra_allowance", sa.Numeric(12, 2), nullable=False, server_default="0"),
        )
        op.add_column(
            table,
            sa.Column("extra_allowance_label", sa.String(length=50), nullable=True),
        )


def downgrade():
    for table in ("salary_records", "salary_snapshots"):
        op.drop_column(table, "extra_allowance_label")
        op.drop_column(table, "extra_allowance")
```

> `salary_records` 既有資料列會以 server_default '0' 回填，nullable=False 安全。

- [ ] **Step 3: 套用 migration 到 dev DB**

Run: `python3 -m alembic upgrade heads`
Expected: 無錯誤；`python3 -m alembic heads` 顯示 `xtraallw01 (head)`。

- [ ] **Step 4: Commit**

```bash
git add models/salary.py alembic/versions/xtraallw01_add_salary_extra_allowance.py
git commit -m "feat(salary): SalaryRecord 新增 extra_allowance + 名目欄位與 migration"
```

---

### Task 2: SalaryBreakdown 加欄位 + _fill_salary_record 保護

**Files:**
- Modify: `services/salary/breakdown.py`
- Modify: `services/salary/engine.py`（`_fill_salary_record`，約行 226-245）

- [ ] **Step 1: `breakdown.py` `SalaryBreakdown` 加欄位**

在 `birthday_bonus: float = 0  # 生日禮金` 之後加：

```python
    extra_allowance: float = 0  # 額外加給（值週/活動加班費等，手填；引擎不自動算）
    extra_allowance_label: "str | None" = None  # 額外加給名目
```

- [ ] **Step 2: `engine.py` `_fill_salary_record` 加 `_apply`**

在 `_apply("birthday_bonus", breakdown.birthday_bonus)` 之後加：

```python
    _apply("extra_allowance", breakdown.extra_allowance)
    _apply("extra_allowance_label", breakdown.extra_allowance_label)
```

> 這兩欄引擎恆為 0/None；加 `_apply` 是為了讓 `manual_overrides` 在其中時跳過覆寫（保留手填值），與其他手填欄位行為一致。

- [ ] **Step 3: py 編譯檢查**

Run: `python3 -c "import services.salary.breakdown, services.salary.engine"`
Expected: 無 ImportError。

- [ ] **Step 4: Commit**

```bash
git add services/salary/breakdown.py services/salary/engine.py
git commit -m "feat(salary): breakdown 加 extra_allowance 並納入 manual_overrides 保護"
```

---

### Task 3: extra_allowance 併入 gross（totals.py + engine.py）— TDD

**Files:**
- Test: `tests/test_salary_extra_allowance.py`（新建）
- Modify: `services/salary/totals.py`（`recompute_record_totals`）
- Modify: `services/salary/engine.py`（gross 組裝，約行 1892-1898）

- [ ] **Step 1: 寫失敗測試**

`tests/test_salary_extra_allowance.py`：

```python
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from types import SimpleNamespace
from services.salary.totals import recompute_record_totals


def _blank_record(**kw):
    """最小 SalaryRecord 替身：recompute_record_totals 只讀欄位、寫 totals。"""
    fields = dict(
        base_salary=30000, hourly_total=0, performance_bonus=0, special_bonus=0,
        supervisor_dividend=0, meeting_overtime_pay=0, birthday_bonus=0, overtime_pay=0,
        extra_allowance=0,
        labor_insurance_employee=0, health_insurance_employee=0, pension_employee=0,
        late_deduction=0, early_leave_deduction=0, missing_punch_deduction=0,
        leave_deduction=0, absence_deduction=0, other_deduction=0,
        festival_bonus=0, overtime_bonus=0,
        gross_salary=0, total_deduction=0, net_salary=0, bonus_amount=0, bonus_separate=False,
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_extra_allowance_included_in_gross():
    rec = _blank_record(base_salary=30000, extra_allowance=1241)
    recompute_record_totals(rec)
    assert rec.gross_salary == 31241
    assert rec.net_salary == 31241  # 無扣款


def test_extra_allowance_zero_no_effect():
    rec = _blank_record(base_salary=30000, extra_allowance=0)
    recompute_record_totals(rec)
    assert rec.gross_salary == 30000
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_salary_extra_allowance.py -v`
Expected: FAIL（`test_extra_allowance_included_in_gross` 期望 31241 但得 30000，因 totals 尚未含此欄）。

- [ ] **Step 3: `totals.py` gross 公式加入 extra_allowance**

在 `recompute_record_totals` 的 `record.gross_salary = round_half_up(...)` 內，於 `+ (record.overtime_pay or 0)` 之後加：

```python
        + (record.extra_allowance or 0)
```

- [ ] **Step 4: `engine.py` gross 組裝同步（保持兩路徑一致）**

在 `breakdown.gross_salary = ( ... + breakdown.birthday_bonus )` 區塊（約行 1892-1898），於 `+ breakdown.birthday_bonus` 之後加：

```python
                + breakdown.extra_allowance
```

- [ ] **Step 5: 跑測試確認通過**

Run: `python3 -m pytest tests/test_salary_extra_allowance.py -v`
Expected: PASS（2 passed）。

- [ ] **Step 6: Commit**

```bash
git add services/salary/totals.py services/salary/engine.py tests/test_salary_extra_allowance.py
git commit -m "feat(salary): extra_allowance 併入應領 gross（totals + engine 兩路徑）"
```

---

### Task 4: manual_adjust 支援 extra_allowance + 名目 — TDD

**Files:**
- Test: `tests/test_salary_extra_allowance.py`（沿用既有 manual_adjust 測試 fixture 模式，見 `tests/test_salary_manual_adjust_bounds.py`）
- Modify: `api/salary/manual_adjust.py`

- [ ] **Step 1: 寫失敗測試**（參照 `tests/test_salary_manual_adjust_bounds.py` 的 app/TestClient/auth override fixture，複用其 setup 建一筆 `SalaryRecord` 後呼叫端點）

新增測試函式（沿用該檔的 client/auth fixture；以下為核心斷言）：

```python
def test_manual_adjust_sets_extra_allowance_and_label(client, make_record):
    rec = make_record(base_salary=30000)  # 既有 fixture：建立未封存 SalaryRecord，回傳含 id/version
    resp = client.put(
        f"/salaries/{rec.id}/manual-adjust",
        json={
            "adjustment_reason": "補值週費",
            "extra_allowance": 1241,
            "extra_allowance_label": "值週",
        },
        headers={"If-Match": str(rec.version)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["extra_allowance"] == 1241
    assert body["extra_allowance_label"] == "值週"
    assert "extra_allowance" in body["manual_overrides"]
    assert "extra_allowance_label" in body["manual_overrides"]
    # gross/net 已含此項
    assert body["gross_salary"] == 31241


def test_extra_allowance_not_in_supplementary_ytd_fields():
    from services.salary.supplementary_premium import BONUS_FIELDS_FOR_YTD
    assert "extra_allowance" not in BONUS_FIELDS_FOR_YTD


def test_extra_allowance_label_max_length_422(client, make_record):
    rec = make_record(base_salary=30000)
    resp = client.put(
        f"/salaries/{rec.id}/manual-adjust",
        json={"adjustment_reason": "測試名目過長", "extra_allowance_label": "值" * 51},
        headers={"If-Match": str(rec.version)},
    )
    assert resp.status_code == 422
```

> 若 `make_record` fixture 不存在，依 `tests/test_salary_manual_adjust_bounds.py` 既有方式內聯建立 record；不要新造 auth 機制。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_salary_extra_allowance.py -v -k "manual_adjust or supplementary or label_max"`
Expected: FAIL（端點尚未認得 extra_allowance / label）。

- [ ] **Step 3: `manual_adjust.py` AdjustModel 加欄位**

在 `AdjustModel`（`SalaryManualAdjustRequest`）的 `other_deduction` 欄位之後加：

```python
    extra_allowance: Optional[float] = Field(None, ge=0, le=_MANUAL_ADJUST_FIELD_MAX)
    extra_allowance_label: Optional[str] = Field(None, max_length=50)
```

- [ ] **Step 4: `EDITABLE_SALARY_FIELDS` 加金額欄**

```python
    "extra_allowance": "額外加給",
```

（`extra_allowance_label` 為文字欄，**不**放入此 dict，避免進數值 round_half_up 迴圈。）

- [ ] **Step 5: 名目欄分流處理**

在數值迴圈（`for field, value in payload.items(): ...`）結束之後、`if not changed_parts:` 之前插入：

```python
        # 額外加給名目（文字欄，與數值欄分流）；設值時加入 manual_overrides 以利重算保留
        if "extra_allowance_label" in payload:
            new_label = (payload.get("extra_allowance_label") or "").strip() or None
            old_label = record.extra_allowance_label
            if new_label != old_label:
                record.extra_allowance_label = new_label
                changed_parts.append(
                    f"額外加給名目 {old_label or '（空）'}→{new_label or '（空）'}"
                )
                modified_fields.append("extra_allowance_label")
```

- [ ] **Step 6: 回應 dict 補兩欄**

找到端點回傳的 response dict（含 `"net_salary": record.net_salary ...`、`"manual_overrides": ...`），加入：

```python
            "extra_allowance": record.extra_allowance or 0,
            "extra_allowance_label": record.extra_allowance_label,
```

- [ ] **Step 7: 跑測試確認通過**

Run: `python3 -m pytest tests/test_salary_extra_allowance.py -v`
Expected: PASS（全部）。

- [ ] **Step 8: Commit**

```bash
git add api/salary/manual_adjust.py tests/test_salary_extra_allowance.py
git commit -m "feat(salary): 手動調整支援 extra_allowance 金額與名目（不課二代健保）"
```

---

### Task 5: detail.py 回應補兩欄

**Files:**
- Modify: `api/salary/detail.py`（約行 173-198 的回應 dict）

- [ ] **Step 1: 在回應 dict（`"overtime_pay": record.overtime_pay or 0,` 附近）加入**

```python
                "extra_allowance": record.extra_allowance or 0,
                "extra_allowance_label": record.extra_allowance_label,
```

- [ ] **Step 2: 確認既有相關測試仍綠**

Run: `python3 -m pytest tests/test_salary_display_fields.py -v`
Expected: PASS（若該檔斷言 response 欄位數，需同步更新預期）。

- [ ] **Step 3: Commit**

```bash
git add api/salary/detail.py
git commit -m "feat(salary): salary detail 回應補 extra_allowance 與名目"
```

---

### Task 6: 薪資單顯示（PDF + Excel）— TDD

**Files:**
- Test: `tests/test_salary_extra_allowance.py`
- Modify: `services/finance/salary_slip.py`（`_build_earnings_table`、`generate_salary_excel`）

- [ ] **Step 1: 寫失敗測試**（驗 earnings rows builder；若 `_build_earnings_table` 直接回傳 Table 不易斷言，改抽出 row 組裝或斷言 PDF bytes 非空 + 不丟例外）

```python
def test_salary_slip_includes_extra_allowance_row():
    from services.finance.salary_slip import _build_earnings_table
    from types import SimpleNamespace
    rec = SimpleNamespace(
        base_salary=30000, performance_bonus=0, special_bonus=0,
        supervisor_dividend=0, festival_bonus=0, overtime_bonus=0,
        bonus_separate=False, gross_salary=31241,
        extra_allowance=1241, extra_allowance_label="值週",
    )
    table = _build_earnings_table(rec, "Helvetica", lambda v: f"{v:,.0f}")
    # 攤平 table 內所有 cell 文字，確認名目與金額出現
    flat = [str(c) for row in table._cellvalues for c in row]
    assert any("值週" in c for c in flat)
    assert any("1,241" in c for c in flat)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_salary_extra_allowance.py::test_salary_slip_includes_extra_allowance_row -v`
Expected: FAIL（名目/金額未出現）。

- [ ] **Step 3: `_build_earnings_table` 加列**

在 `earn_data.append(["月薪應發合計", "", money_fmt(record.gross_salary)])` 之前插入：

```python
    extra_allowance_val = getattr(record, "extra_allowance", 0) or 0
    if extra_allowance_val:
        extra_label = (getattr(record, "extra_allowance_label", None) or "額外加給")
        earn_data.append([extra_label, "", money_fmt(extra_allowance_val)])
```

- [ ] **Step 4: `generate_salary_excel` 同步**

於 `generate_salary_excel` 輸出應領明細處，比照加入「`extra_allowance_label`／額外加給」一列（當 `extra_allowance > 0`）。讀該函式現有應領欄位寫法後對齊插入。

- [ ] **Step 5: 跑測試確認通過**

Run: `python3 -m pytest tests/test_salary_extra_allowance.py -v`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add services/finance/salary_slip.py tests/test_salary_extra_allowance.py
git commit -m "feat(salary): 薪資單(PDF/Excel) 顯示額外加給列與名目"
```

---

### Task 7: 後端全套件回歸 + dump openapi

- [ ] **Step 1: 跑薪資相關全測試**

Run: `python3 -m pytest tests/ -k "salary or supplementary or salary_slip or manual_adjust" -q`
Expected: 全綠，無新增 fail（與 main baseline 對照）。

- [ ] **Step 2: 產 openapi（供前端 codegen）**

Run: `python3 scripts/dump_openapi.py`
Expected: 產出 `openapi.json`（local-only，不 commit）。

---

## 前端（後端 merge 後再做）

分支 `feat/salary-extra-allowance-2026-06-04-fe`。

### Task 8: 重新產生 API 型別

**Files:**
- Modify: `src/api/_generated/schema.d.ts`（由 codegen 產生）

- [ ] **Step 1: regen**

Run: `cd ~/Desktop/ivy-frontend && npm run gen:api`
Expected: `schema.d.ts` 出現 `extra_allowance` / `extra_allowance_label`（manual-adjust body 與 salary detail response）。

- [ ] **Step 2: Commit**

```bash
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regen schema 帶入 extra_allowance 欄位"
```

---

### Task 9: SalaryView.vue 手動調整加金額欄 + 名目輸入

**Files:**
- Modify: `src/views/SalaryView.vue`

- [ ] **Step 1: 編輯表單 model 加欄位**

在手動調整表單 model（約行 109-112，含 `overtime_pay: 0` 等）加：

```ts
  extra_allowance: 0,
  extra_allowance_label: '',
```

- [ ] **Step 2: 可編輯欄位 config 加金額欄**

在欄位 config 陣列（約行 122-125，`{ key, label }` 形式）加：

```ts
  { key: 'extra_allowance', label: '額外加給' },
```

- [ ] **Step 3: payload 帶上兩欄**

在送出 payload 組裝處（約行 349-367）加：

```ts
        extra_allowance: updated.extra_allowance,
        extra_allowance_label: updated.extra_allowance_label,
```

- [ ] **Step 4: UI 加名目文字輸入**

在金額欄附近加一個 `<el-input v-model="...extra_allowance_label" maxlength="50" placeholder="名目（值週/活動加班費）" />`，並比照既有 `extra_allowance` 金額欄與 `isManualOverride` tooltip 模式呈現。

- [ ] **Step 5: typecheck**

Run: `npm run type-check`（或專案實際指令，見 package.json scripts）
Expected: 0 error。

- [ ] **Step 6: Commit**

```bash
git add src/views/SalaryView.vue
git commit -m "feat(salary): 薪資手動調整加額外加給金額與名目欄"
```

---

### Task 10: 前端測試 + 收尾

- [ ] **Step 1: 補/跑 vitest**

針對 SalaryView 手動調整 payload 含 `extra_allowance` / `extra_allowance_label` 加（或擴充既有）測試。
Run: `npm run test:unit -- SalaryView`（依專案實際指令）
Expected: PASS。

- [ ] **Step 2: typecheck + lint**

Run: `npm run type-check && npx eslint src/views/SalaryView.vue`
Expected: 0 error（含 no-explicit-any 棘輪）。

- [ ] **Step 3: Commit**

```bash
git add tests/ src/views/SalaryView.vue
git commit -m "test(salary): 額外加給手動調整前端測試"
```

---

## 自我審查結果（spec coverage）

- spec §2 資料模型 → Task 1 ✓
- spec §3 引擎納入 gross + 不課二代健保 → Task 2/3/4(supplementary 斷言) ✓
- spec §4 手填介面 → Task 4 ✓
- spec §5 顯示（薪資單 + detail 回應）→ Task 5/6 ✓
- spec §6 前端 → Task 8/9/10 ✓
- spec §7 測試 → Task 3/4/6/10 ✓
- spec §9 分開 commit → 後端/前端分支各一系列 commit ✓

無 placeholder；型別/欄位名一致（`extra_allowance` / `extra_allowance_label` 全程一致）。

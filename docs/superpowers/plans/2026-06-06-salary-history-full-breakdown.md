# 薪資歷史完整明細 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓管理端「薪資歷史」分頁每一列可展開，顯示該月完整薪條明細（進帳收入／另行轉帳／扣款三區 + 權威小計），數字一律來自 `SalaryRecord` persisted 欄位。

**Architecture:** 後端新增純函式 `build_history_breakdown(record)` 組出三區明細（小計取 persisted gross/total_deduction/net，零重算），擴充 `GET /salaries/history` response schema 帶巢狀 `payslip_detail`；前端歷史表加 `type="expand"` 列 + 新 `SalaryHistoryDetail.vue` 渲染三區。後端先、前端後，跑 `gen:api` 同步型別。

**Tech Stack:** FastAPI + Pydantic v2 + SQLAlchemy（後端）/ Vue 3 `<script setup lang="ts">` + Element Plus + Vitest（前端）。

參考 spec：`docs/superpowers/specs/2026-06-06-salary-history-full-breakdown-design.md`

---

## File Structure

**後端（ivy-backend）**
- Modify: `api/salary_fields.py` — 既有顯示 helper 模組，新增純函式 `build_history_breakdown`。
- Modify: `tests/test_salary_display_fields.py` — 既有純函式測試，新增 `build_history_breakdown` 測試類別。
- Modify: `schemas/salary_records.py` — 新增 `SalaryHistoryLineOut` / `SalaryHistoryBreakdownOut`，擴充 `SalaryHistoryItemOut`。
- Modify: `api/salary/records.py:286-347` — `get_salary_history` 填入新欄位。
- Create: `tests/test_salary_history_breakdown_endpoint.py` — 端點整合測試（SQLite，seed 一筆 SalaryRecord）。

**前端（ivy-frontend）**
- Regen: `src/api/_generated/schema.d.ts`（`npm run gen:api`）。
- Create: `src/views/salary/salaryHistoryDetail.ts` — 純展示 helper（過濾零值列等）+ 型別。
- Create: `src/views/salary/__tests__/salaryHistoryDetail.spec.ts` — helper 純函式測試。
- Create: `src/views/salary/SalaryHistoryDetail.vue` — 三區明細元件。
- Create: `src/views/salary/__tests__/SalaryHistoryDetail.spec.ts` — 元件 mount 測試。
- Modify: `src/views/salary/SalaryHistoryPanel.vue` — 加可展開列 + 摘要欄改 `in_gross_bonus`。

**分支**：後端 `feat/salary-history-breakdown-2026-06-06-be`、前端 `feat/salary-history-breakdown-2026-06-06-fe`，皆從 `origin/main` 開 worktree（見 §收尾）。spec 檔於後端分支首 commit 一併納入。

---

## 後端

### Task 1: 純函式 `build_history_breakdown`

**Files:**
- Modify: `api/salary_fields.py`
- Test: `tests/test_salary_display_fields.py`

- [ ] **Step 1: 在測試檔末尾新增失敗測試**

於 `tests/test_salary_display_fields.py` 末尾追加（檔頭已有 `from types import SimpleNamespace`）：

```python
from api.salary_fields import build_history_breakdown


class TestBuildHistoryBreakdown:
    def _record(self, **over):
        base = dict(
            base_salary=2950, hourly_total=0, performance_bonus=0, special_bonus=0,
            supervisor_dividend=5000, overtime_pay=0, meeting_overtime_pay=0,
            birthday_bonus=0, extra_allowance=0, extra_allowance_label=None,
            festival_bonus=26000, overtime_bonus=0, appraisal_year_end_bonus=0,
            unused_leave_payout=0,
            labor_insurance_employee=600, health_insurance_employee=800,
            supplementary_health_employee=0, pension_employee=0,
            late_deduction=0, early_leave_deduction=0, missing_punch_deduction=0,
            leave_deduction=900, absence_deduction=0, other_deduction=0,
            gross_salary=7950, total_deduction=4604, net_salary=3346,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_income_subtotal_is_persisted_gross(self):
        bd = build_history_breakdown(self._record())
        assert bd["income_subtotal"] == 7950

    def test_deduction_subtotal_is_persisted_total(self):
        bd = build_history_breakdown(self._record())
        assert bd["deduction_subtotal"] == 4604

    def test_net_equals_gross_minus_deduction(self):
        bd = build_history_breakdown(self._record())
        assert bd["net_salary"] == bd["income_subtotal"] - bd["deduction_subtotal"]

    def test_supervisor_dividend_in_income_not_separate(self):
        """釘住易錯點：主管紅利進實發（income），不在另行轉帳。"""
        bd = build_history_breakdown(self._record())
        income_keys = {l["key"] for l in bd["income"]}
        sep_keys = {l["key"] for l in bd["separate_transfer"]}
        assert "supervisor_dividend" in income_keys
        assert "supervisor_dividend" not in sep_keys

    def test_festival_overtime_in_separate_not_income(self):
        """釘住易錯點：節慶/超額為另行轉帳，不進 income。"""
        bd = build_history_breakdown(self._record())
        income_keys = {l["key"] for l in bd["income"]}
        sep_keys = {l["key"] for l in bd["separate_transfer"]}
        assert {"festival_bonus", "overtime_bonus"} <= sep_keys
        assert "festival_bonus" not in income_keys

    def test_income_lines_sum_to_subtotal_via_other(self):
        """seed 殘差：gross 比已知收入多 5000 → other_income 吸收，使收入區對得回應發。"""
        rec = self._record(
            supervisor_dividend=0, gross_salary=35000,
            base_salary=29500, birthday_bonus=500,
        )
        bd = build_history_breakdown(rec)
        assert sum(l["amount"] for l in bd["income"]) == bd["income_subtotal"]
        other = next(l for l in bd["income"] if l["key"] == "other_income")
        assert other["amount"] == 5000

    def test_supplementary_health_is_informational_child_not_double_counted(self):
        """補充保費為健保下 informational 子列，不另計、不改扣款合計。"""
        rec = self._record(supplementary_health_employee=200)
        bd = build_history_breakdown(rec)
        health = next(l for l in bd["deductions"] if l["key"] == "health_insurance_employee")
        assert health["children"][0]["informational"] is True
        assert health["children"][0]["amount"] == 200
        assert bd["deduction_subtotal"] == 4604
        assert all(l["key"] != "supplementary_health_employee" for l in bd["deductions"])

    def test_extra_allowance_label_as_note(self):
        rec = self._record(extra_allowance=1500, extra_allowance_label="值週")
        bd = build_history_breakdown(rec)
        extra = next(l for l in bd["income"] if l["key"] == "extra_allowance")
        assert extra["note"] == "值週"

    def test_none_values_coalesced_to_zero(self):
        rec = self._record(performance_bonus=None, late_deduction=None)
        bd = build_history_breakdown(rec)  # 不可拋例外
        assert isinstance(bd["income_subtotal"], float)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_salary_display_fields.py::TestBuildHistoryBreakdown -q`
Expected: FAIL — `ImportError: cannot import name 'build_history_breakdown'`

- [ ] **Step 3: 實作純函式**

於 `api/salary_fields.py` 末尾（`calculate_display_bonus_total` 之後）新增：

```python
def _coalesce_float(record, name) -> float:
    """讀欄位並 coalesce None→0、轉 float（避免 Decimal/float 混算 TypeError）。"""
    return float(getattr(record, name, 0) or 0)


# 進帳收入欄位（計入 gross_salary）；label 對齊官方薪條用語。
_HISTORY_INCOME_FIELDS = [
    ("base_salary", "底薪"),
    ("performance_bonus", "績效獎金"),
    ("special_bonus", "特別獎金"),
    ("supervisor_dividend", "主管紅利"),  # ⚠ 進實發；欄位註解「獨立轉帳」具誤導性
    ("overtime_pay", "加班費"),
    ("meeting_overtime_pay", "園務會議加班"),
    ("birthday_bonus", "生日禮金"),
    ("hourly_total", "時薪總計"),
    ("extra_allowance", "額外加給"),
]
# 另行轉帳欄位（不進 gross/net，獨立金流）。
_HISTORY_SEPARATE_FIELDS = [
    ("festival_bonus", "節慶獎金"),
    ("overtime_bonus", "超額獎金"),
    ("appraisal_year_end_bonus", "考核年終獎金"),
    ("unused_leave_payout", "特休未休折現"),
]
# 扣款欄位（合計 = total_deduction）。
_HISTORY_DEDUCTION_FIELDS = [
    ("labor_insurance_employee", "勞保"),
    ("health_insurance_employee", "健保"),
    ("pension_employee", "勞退自提"),
    ("late_deduction", "遲到扣款"),
    ("early_leave_deduction", "早退扣款"),
    ("missing_punch_deduction", "未打卡扣款"),
    ("leave_deduction", "請假扣款"),
    ("absence_deduction", "曠職扣款"),
    ("other_deduction", "其他扣款"),
]


def build_history_breakdown(record) -> dict:
    """從 SalaryRecord persisted 欄位組出歷史薪條三區明細（純展示，不重算）。

    正確性守衛：
    - income_subtotal/deduction_subtotal/net 一律取 persisted 值當權威。
    - income 區補「其他（未分類）」吸收 gross 與已知收入欄位差額（seed/邊角），
      使收入各項 + other == gross。
    - supplementary_health_employee 已併入 health_insurance_employee，僅作健保下
      informational 子列，不進扣款合計（避免 double-count）。
    - meeting_absence_deduction 已在 engine 內從 festival 扣抵，不另列。
    """
    gross = _coalesce_float(record, "gross_salary")
    total_deduction = _coalesce_float(record, "total_deduction")
    net = _coalesce_float(record, "net_salary")

    income = []
    known_income_sum = 0.0
    for key, label in _HISTORY_INCOME_FIELDS:
        amount = _coalesce_float(record, key)
        line = {"key": key, "label": label, "amount": amount}
        if key == "extra_allowance":
            note = getattr(record, "extra_allowance_label", None)
            if note:
                line["note"] = note
        income.append(line)
        known_income_sum += amount
    other_income = round(gross - known_income_sum, 2)
    income.append({"key": "other_income", "label": "其他（未分類）", "amount": other_income})

    separate_transfer = [
        {"key": key, "label": label, "amount": _coalesce_float(record, key)}
        for key, label in _HISTORY_SEPARATE_FIELDS
    ]
    separate_subtotal = round(sum(item["amount"] for item in separate_transfer), 2)

    deductions = []
    for key, label in _HISTORY_DEDUCTION_FIELDS:
        line = {"key": key, "label": label, "amount": _coalesce_float(record, key)}
        if key == "health_insurance_employee":
            supp = _coalesce_float(record, "supplementary_health_employee")
            if supp:
                line["children"] = [{
                    "key": "supplementary_health_employee",
                    "label": "其中：二代健保補充保費",
                    "amount": supp,
                    "informational": True,
                }]
        deductions.append(line)

    return {
        "income": income,
        "income_subtotal": gross,
        "separate_transfer": separate_transfer,
        "separate_subtotal": separate_subtotal,
        "deductions": deductions,
        "deduction_subtotal": total_deduction,
        "net_salary": net,
    }
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_salary_display_fields.py -q`
Expected: PASS（全部，含既有 `calculate_display_bonus_total` 測試）

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/salary_fields.py tests/test_salary_display_fields.py
git commit -m "feat(salary): 新增 build_history_breakdown 純函式組薪資歷史三區明細"
```

---

### Task 2: Response schema 擴充

**Files:**
- Modify: `schemas/salary_records.py:98`（`SalaryHistoryItemOut` 前後）

- [ ] **Step 1: 在 `SalaryHistoryItemOut` class 之前新增巢狀 model**

於 `schemas/salary_records.py` 中 `class SalaryHistoryItemOut(IvyBaseModel):` 定義**之前**插入：

```python
class SalaryHistoryLineOut(IvyBaseModel):
    """薪資歷史明細單列（收入/另行轉帳/扣款共用）。"""

    key: str
    label: str
    amount: float  # pii-allow: 明細金額
    note: Optional[str] = None  # 額外加給名目等
    informational: bool = False  # True=僅資訊列（如補充保費），不進小計
    children: Optional[list["SalaryHistoryLineOut"]] = None


SalaryHistoryLineOut.model_rebuild()  # 解析自我參照 children 之 forward ref


class SalaryHistoryBreakdownOut(IvyBaseModel):
    """單月薪條三區明細 + 權威小計（小計取 persisted gross/total_deduction/net）。"""

    income: list[SalaryHistoryLineOut]
    income_subtotal: float  # pii-allow: 應發合計（= persisted gross_salary）
    separate_transfer: list[SalaryHistoryLineOut]
    separate_subtotal: float  # pii-allow: 另行轉帳小計
    deductions: list[SalaryHistoryLineOut]
    deduction_subtotal: float  # pii-allow: 扣款合計（= persisted total_deduction）
    net_salary: float  # pii-allow: 實發（= persisted net_salary）
```

- [ ] **Step 2: 擴充 `SalaryHistoryItemOut` 欄位**

在 `SalaryHistoryItemOut` 內 `total_bonus: float  # pii-allow: 獎金合計` 之下新增三欄，並把 `total_bonus` 的註解改為 deprecated：

```python
    total_bonus: float  # pii-allow: 獎金合計（DEPRECATED：語意不對帳，前端歷史改用 in_gross_bonus）
    in_gross_bonus: float  # pii-allow: 進帳獎金合計（= gross − base − hourly，摘要列用）
    separate_transfer_total: float  # pii-allow: 另行轉帳合計（摘要列用）
    payslip_detail: SalaryHistoryBreakdownOut  # 三區明細（展開列用）
```

- [ ] **Step 3: 驗證 schema import 無誤**

Run: `cd ~/Desktop/ivy-backend && python -c "from schemas.salary_records import SalaryHistoryItemOut, SalaryHistoryBreakdownOut, SalaryHistoryLineOut; print('ok')"`
Expected: 印出 `ok`（無 forward-ref / 型別錯誤）

- [ ] **Step 4: Commit**

```bash
cd ~/Desktop/ivy-backend
git add schemas/salary_records.py
git commit -m "feat(salary): SalaryHistoryItemOut 加 payslip_detail 三區明細與摘要欄"
```

---

### Task 3: 端點 `get_salary_history` 填入明細

**Files:**
- Modify: `api/salary/records.py:304-331`
- Test: `tests/test_salary_history_breakdown_endpoint.py`（Create）

- [ ] **Step 1: 寫端點整合測試（失敗）**

Create `tests/test_salary_history_breakdown_endpoint.py`：

```python
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.base import Base
from models.employee import Employee
from models.user import User
from models.salary import SalaryRecord
from utils.auth import hash_password, create_access_token


@pytest.fixture
def client_and_emp(tmp_path):
    from api.salary.records import router as salary_records_router

    engine = create_engine(
        f"sqlite:///{tmp_path / 'hist-breakdown.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine, old_factory = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)

    session = session_factory()
    try:
        emp = Employee(employee_id="T99", name="王小明", is_active=True)
        session.add(emp)
        session.commit()
        emp_id = emp.id
        admin = User(
            username="admin", password_hash=hash_password("Admin1234"),
            role="admin", is_active=True, permission_names=["*"], employee_id=emp_id,
        )
        session.add(admin)
        session.commit()
        admin_id = admin.id
        # seed 一筆已知薪資：gross 7950 = base 2950 + supervisor 5000；festival 26000 另行轉帳
        session.add(SalaryRecord(
            employee_id=emp_id, salary_year=2026, salary_month=6,
            base_salary=2950, supervisor_dividend=5000, festival_bonus=26000,
            labor_insurance_employee=600, health_insurance_employee=800,
            leave_deduction=900, gross_salary=7950, total_deduction=4604, net_salary=3346,
        ))
        session.commit()
    finally:
        session.close()

    token = create_access_token({
        "user_id": admin_id, "employee_id": emp_id, "role": "admin",
        "name": "王小明", "permission_names": ["*"], "token_version": 0,
    })
    app = FastAPI()
    app.include_router(salary_records_router, prefix="/api")
    client = TestClient(app)
    client.cookies.set("access_token", token)
    yield client, emp_id

    base_module._engine, base_module._SessionFactory = old_engine, old_factory
    engine.dispose()


def test_history_returns_payslip_detail_three_regions(client_and_emp):
    client, emp_id = client_and_emp
    res = client.get(f"/api/salaries/history?employee_id={emp_id}")
    assert res.status_code == 200
    rows = res.json()
    assert len(rows) == 1
    row = rows[0]
    # 摘要欄
    assert row["in_gross_bonus"] == 5000  # gross 7950 − base 2950 − hourly 0
    assert row["separate_transfer_total"] == 26000
    # 三區
    detail = row["payslip_detail"]
    assert detail["income_subtotal"] == 7950
    assert detail["deduction_subtotal"] == 4604
    assert detail["net_salary"] == 3346
    income_keys = {l["key"] for l in detail["income"]}
    sep_keys = {l["key"] for l in detail["separate_transfer"]}
    assert "supervisor_dividend" in income_keys
    assert "festival_bonus" in sep_keys
    assert "festival_bonus" not in income_keys
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_salary_history_breakdown_endpoint.py -q`
Expected: FAIL — response 缺 `in_gross_bonus`/`payslip_detail`（ResponseValidationError 或 KeyError）

- [ ] **Step 3: 端點填入新欄位**

於 `api/salary/records.py`：① 確認檔頭已有 `from api.salary_fields import calculate_display_bonus_total`，改為同時 import：

```python
from api.salary_fields import calculate_display_bonus_total, build_history_breakdown
```

② 在 `get_salary_history` 迴圈內，於 `total_bonus = calculate_display_bonus_total(r)` 之後、`results.append({...})` 的 dict 內，新增三個 key（其餘 key 不動）：

```python
            total_bonus = calculate_display_bonus_total(r)
            payslip_detail = build_history_breakdown(r)
            results.append(
                {
                    "id": r.id,
                    "year": r.salary_year,
                    "month": r.salary_month,
                    "base_salary": r.base_salary,
                    "total_bonus": total_bonus,
                    "in_gross_bonus": round(
                        float(r.gross_salary or 0)
                        - float(r.base_salary or 0)
                        - float(r.hourly_total or 0),
                        2,
                    ),
                    "separate_transfer_total": payslip_detail["separate_subtotal"],
                    "payslip_detail": payslip_detail,
                    "labor_insurance": r.labor_insurance_employee,
                    "health_insurance": r.health_insurance_employee,
                    "supplementary_health_employee": (
                        r.supplementary_health_employee or 0
                    ),
                    "attendance_deduction": (
                        (r.late_deduction or 0)
                        + (r.early_leave_deduction or 0)
                        + (r.missing_punch_deduction or 0)
                    ),
                    "leave_deduction": r.leave_deduction or 0,
                    "gross_salary": r.gross_salary,
                    "total_deduction": r.total_deduction,
                    "total_deductions": r.total_deduction,
                    "net_salary": r.net_salary,
                    "net_pay": r.net_salary,
                }
            )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_salary_history_breakdown_endpoint.py tests/test_audit_sensitive_get.py -q`
Expected: PASS（新測試 + 既有 history audit 測試皆綠，確認沒破壞既有契約）

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-backend
git add api/salary/records.py tests/test_salary_history_breakdown_endpoint.py
git commit -m "feat(salary): /salaries/history 回傳 payslip_detail 三區明細與摘要欄"
```

---

### Task 4: 後端回歸 + 產 OpenAPI

**Files:** 無新檔（產物 `openapi.json` 為 dev-time artifact，`.gitignore` 擋，不 commit）

- [ ] **Step 1: 跑薪資相關測試套件確認無回歸**

Run: `cd ~/Desktop/ivy-backend && python -m pytest tests/test_salary_display_fields.py tests/test_salary_history_breakdown_endpoint.py tests/test_audit_sensitive_get.py -q`
Expected: PASS

- [ ] **Step 2: 產出 OpenAPI（供前端 gen:api 用）**

Run: `cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py`
Expected: 產出 `openapi.json`（無錯誤）；接著確認 history schema 含新欄位：

Run: `cd ~/Desktop/ivy-backend && python -c "import json; s=json.load(open('openapi.json')); print('SalaryHistoryBreakdownOut' in s['components']['schemas'])"`
Expected: 印出 `True`

> 此 Task 無 commit（無 tracked 檔變動）。`openapi.json` 留給前端 Task 5 使用。

---

## 前端

> 前置：前端 worktree 的 `node_modules` 若為 symlink 失效需先修（見記憶 `feedback_frontend_worktree_node_modules_symlink`）。本批次假設 `npm install` 已可用。

### Task 5: 同步 OpenAPI 型別

**Files:**
- Regen: `src/api/_generated/schema.d.ts`

- [ ] **Step 1: 重新產生型別**

Run: `cd ~/Desktop/ivy-frontend && npm run gen:api`
（讀後端剛產的 `~/Desktop/ivy-backend/openapi.json`；若 gen:api 指向他處，依 `package.json` script 路徑為準）
Expected: `src/api/_generated/schema.d.ts` 更新，diff 內出現 `SalaryHistoryBreakdownOut` / `payslip_detail` / `in_gross_bonus`。

- [ ] **Step 2: 確認型別含新欄位**

Run: `cd ~/Desktop/ivy-frontend && grep -c "payslip_detail" src/api/_generated/schema.d.ts`
Expected: ≥ 1

- [ ] **Step 3: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/api/_generated/schema.d.ts
git commit -m "chore(api): regen OpenAPI 型別含 salary history payslip_detail"
```

---

### Task 6: 純展示 helper + 型別

**Files:**
- Create: `src/views/salary/salaryHistoryDetail.ts`
- Test: `src/views/salary/__tests__/salaryHistoryDetail.spec.ts`

- [ ] **Step 1: 寫失敗測試**

Create `src/views/salary/__tests__/salaryHistoryDetail.spec.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { nonZeroLines, hasSeparateTransfer, type PayslipDetail } from '../salaryHistoryDetail'

const detail: PayslipDetail = {
  income: [
    { key: 'base_salary', label: '底薪', amount: 2950 },
    { key: 'supervisor_dividend', label: '主管紅利', amount: 5000 },
    { key: 'performance_bonus', label: '績效獎金', amount: 0 },
    { key: 'other_income', label: '其他（未分類）', amount: 0 },
  ],
  income_subtotal: 7950,
  separate_transfer: [
    { key: 'festival_bonus', label: '節慶獎金', amount: 26000 },
    { key: 'overtime_bonus', label: '超額獎金', amount: 0 },
  ],
  separate_subtotal: 26000,
  deductions: [{ key: 'labor_insurance_employee', label: '勞保', amount: 600 }],
  deduction_subtotal: 4604,
  net_salary: 3346,
}

describe('salaryHistoryDetail helper', () => {
  it('nonZeroLines 隱藏金額為 0 的列', () => {
    const keys = nonZeroLines(detail.income).map(l => l.key)
    expect(keys).toContain('supervisor_dividend')
    expect(keys).not.toContain('performance_bonus')
    expect(keys).not.toContain('other_income')
  })

  it('hasSeparateTransfer 在小計非零時為 true', () => {
    expect(hasSeparateTransfer(detail)).toBe(true)
  })

  it('hasSeparateTransfer 在小計為零時為 false', () => {
    expect(hasSeparateTransfer({ ...detail, separate_subtotal: 0 })).toBe(false)
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/views/salary/__tests__/salaryHistoryDetail.spec.ts`
Expected: FAIL — 找不到模組 `../salaryHistoryDetail`

- [ ] **Step 3: 實作 helper**

Create `src/views/salary/salaryHistoryDetail.ts`：

```ts
// 薪資歷史明細純展示 helper。型別 hand-define 對齊 payslip_detail（與 SalaryHistoryPanel
// 既有 HistoryRow hand-define 風格一致）；後端 build_history_breakdown 已組好結構。
export interface BreakdownLine {
  key: string
  label: string
  amount: number
  note?: string | null
  informational?: boolean
  children?: BreakdownLine[] | null
}

export interface PayslipDetail {
  income: BreakdownLine[]
  income_subtotal: number
  separate_transfer: BreakdownLine[]
  separate_subtotal: number
  deductions: BreakdownLine[]
  deduction_subtotal: number
  net_salary: number
}

/** 過濾掉金額為 0 的列（other_income、未發生的獎金/扣款不顯示，降噪）。 */
export function nonZeroLines(lines: BreakdownLine[]): BreakdownLine[] {
  return lines.filter(l => l.amount !== 0)
}

/** 另行轉帳整區是否顯示（小計為 0 則整區隱藏）。 */
export function hasSeparateTransfer(detail: PayslipDetail): boolean {
  return detail.separate_subtotal !== 0
}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/views/salary/__tests__/salaryHistoryDetail.spec.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/views/salary/salaryHistoryDetail.ts src/views/salary/__tests__/salaryHistoryDetail.spec.ts
git commit -m "feat(salary): 薪資歷史明細純展示 helper（過濾零值列）"
```

---

### Task 7: `SalaryHistoryDetail.vue` 三區元件

**Files:**
- Create: `src/views/salary/SalaryHistoryDetail.vue`
- Test: `src/views/salary/__tests__/SalaryHistoryDetail.spec.ts`

- [ ] **Step 1: 寫失敗的 mount 測試**

Create `src/views/salary/__tests__/SalaryHistoryDetail.spec.ts`：

```ts
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import SalaryHistoryDetail from '../SalaryHistoryDetail.vue'
import type { PayslipDetail } from '../salaryHistoryDetail'

const detail: PayslipDetail = {
  income: [
    { key: 'base_salary', label: '底薪', amount: 2950 },
    { key: 'supervisor_dividend', label: '主管紅利', amount: 5000 },
    { key: 'performance_bonus', label: '績效獎金', amount: 0 },
    { key: 'other_income', label: '其他（未分類）', amount: 0 },
  ],
  income_subtotal: 7950,
  separate_transfer: [
    { key: 'festival_bonus', label: '節慶獎金', amount: 26000 },
    { key: 'overtime_bonus', label: '超額獎金', amount: 0 },
  ],
  separate_subtotal: 26000,
  deductions: [
    {
      key: 'health_insurance_employee', label: '健保', amount: 800,
      children: [{ key: 'supplementary_health_employee', label: '其中：二代健保補充保費', amount: 200, informational: true }],
    },
  ],
  deduction_subtotal: 4604,
  net_salary: 3346,
}

describe('SalaryHistoryDetail.vue', () => {
  it('渲染三區與實發', () => {
    const text = mount(SalaryHistoryDetail, { props: { detail } }).text()
    expect(text).toContain('主管紅利')   // 進帳收入
    expect(text).toContain('節慶獎金')   // 另行轉帳
    expect(text).toContain('健保')       // 扣款
    expect(text).toContain('其中：二代健保補充保費')  // informational 子列
    expect(text).toContain('實發')
  })

  it('隱藏金額為 0 的列（超額獎金/績效/other_income）', () => {
    const text = mount(SalaryHistoryDetail, { props: { detail } }).text()
    expect(text).not.toContain('超額獎金')
    expect(text).not.toContain('其他（未分類）')
  })

  it('另行轉帳小計為 0 時整區隱藏', () => {
    const zero: PayslipDetail = { ...detail, separate_transfer: [], separate_subtotal: 0 }
    const text = mount(SalaryHistoryDetail, { props: { detail: zero } }).text()
    expect(text).not.toContain('另行轉帳')
  })
})
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/views/salary/__tests__/SalaryHistoryDetail.spec.ts`
Expected: FAIL — 找不到 `../SalaryHistoryDetail.vue`

- [ ] **Step 3: 實作元件（純 div 標記，免依賴 element-plus 便於測試）**

Create `src/views/salary/SalaryHistoryDetail.vue`：

```vue
<script setup lang="ts">
import { computed } from 'vue'
import { money } from '@/utils/format'
import { nonZeroLines, hasSeparateTransfer, type PayslipDetail } from './salaryHistoryDetail'

const props = defineProps<{ detail: PayslipDetail }>()
const income = computed(() => nonZeroLines(props.detail.income))
const separate = computed(() => nonZeroLines(props.detail.separate_transfer))
const deductions = computed(() => nonZeroLines(props.detail.deductions))
const showSeparate = computed(() => hasSeparateTransfer(props.detail))
</script>

<template>
  <div class="sh-detail">
    <section class="sh-region">
      <h4>進帳收入（計入應發/實發）</h4>
      <div v-for="line in income" :key="line.key" class="sh-line">
        <span>{{ line.label }}<small v-if="line.note">（{{ line.note }}）</small></span>
        <span>{{ money(line.amount) }}</span>
      </div>
      <div class="sh-line sh-subtotal">
        <span>應發合計</span><span>{{ money(detail.income_subtotal) }}</span>
      </div>
    </section>

    <section v-if="showSeparate" class="sh-region">
      <h4>另行轉帳（不進實發，另一條金流）</h4>
      <div v-for="line in separate" :key="line.key" class="sh-line">
        <span>{{ line.label }}</span><span>{{ money(line.amount) }}</span>
      </div>
      <div class="sh-line sh-subtotal">
        <span>另行轉帳小計</span><span>{{ money(detail.separate_subtotal) }}</span>
      </div>
    </section>

    <section class="sh-region">
      <h4>扣款</h4>
      <template v-for="line in deductions" :key="line.key">
        <div class="sh-line">
          <span>{{ line.label }}</span><span class="sh-neg">-{{ money(line.amount) }}</span>
        </div>
        <div v-for="child in (line.children || [])" :key="child.key" class="sh-line sh-child">
          <span>{{ child.label }}</span><span>-{{ money(child.amount) }}</span>
        </div>
      </template>
      <div class="sh-line sh-subtotal">
        <span>扣款合計</span><span class="sh-neg">-{{ money(detail.deduction_subtotal) }}</span>
      </div>
    </section>

    <div class="sh-line sh-net">
      <span>實發</span><span>{{ money(detail.net_salary) }}</span>
    </div>
  </div>
</template>

<style scoped>
.sh-detail { padding: 12px 16px; background: var(--el-fill-color-lighter); border-radius: 4px; display: grid; gap: 16px; }
.sh-region h4 { margin: 0 0 8px; font-size: 14px; color: var(--el-text-color-primary); }
.sh-line { display: flex; justify-content: space-between; padding: 2px 0; }
.sh-child { padding-left: 16px; font-size: 12px; color: var(--el-text-color-secondary); }
.sh-subtotal { font-weight: 600; border-top: 1px solid var(--el-border-color); margin-top: 4px; padding-top: 4px; }
.sh-neg { color: var(--el-color-danger); }
.sh-net { font-weight: 700; font-size: 16px; border-top: 2px solid var(--el-color-primary); padding-top: 6px; }
</style>
```

- [ ] **Step 4: 跑測試確認通過**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/views/salary/__tests__/SalaryHistoryDetail.spec.ts`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/views/salary/SalaryHistoryDetail.vue src/views/salary/__tests__/SalaryHistoryDetail.spec.ts
git commit -m "feat(salary): 薪資歷史可展開三區明細元件 SalaryHistoryDetail"
```

---

### Task 8: 接上 `SalaryHistoryPanel.vue`

**Files:**
- Modify: `src/views/salary/SalaryHistoryPanel.vue`

- [ ] **Step 1: 擴充 `HistoryRow` interface 與 import**

於 `SalaryHistoryPanel.vue` `<script setup>`：① import 新元件與型別（在既有 import 區）：

```ts
import SalaryHistoryDetail from './SalaryHistoryDetail.vue'
import type { PayslipDetail } from './salaryHistoryDetail'
```

② 在 `interface HistoryRow {` 內，於 `total_bonus: number` 之下新增兩欄：

```ts
  total_bonus: number
  in_gross_bonus: number
  payslip_detail: PayslipDetail
```

- [ ] **Step 2: 加可展開列 + 摘要「獎金合計」改用 `in_gross_bonus`**

於 `<el-table :data="historyData" ...>` 內，把**「獎金」欄**改名並改綁定，並在「年/月」欄**之前**插入展開欄。

把：

```vue
        <el-table-column label="獎金" width="100">
          <template #default="scope">{{ money(scope.row.total_bonus) }}</template>
        </el-table-column>
```

改為：

```vue
        <el-table-column label="獎金合計" width="110">
          <template #default="scope">{{ money(scope.row.in_gross_bonus) }}</template>
        </el-table-column>
```

並在 `<el-table-column label="年/月" width="90">` **之前**插入：

```vue
        <el-table-column type="expand">
          <template #default="scope">
            <SalaryHistoryDetail :detail="scope.row.payslip_detail" />
          </template>
        </el-table-column>
```

- [ ] **Step 3: typecheck 通過**

Run: `cd ~/Desktop/ivy-frontend && npm run typecheck`
Expected: exit 0（`in_gross_bonus`/`payslip_detail` 型別齊備，無 `any`）

- [ ] **Step 4: 跑相關前端測試**

Run: `cd ~/Desktop/ivy-frontend && npx vitest run src/views/salary/__tests__/ src/views/__tests__/SalaryView.appraisal-year-end-bonus.spec.ts`
Expected: PASS（新元件/helper 測試 + 既有 SalaryView 測試無回歸）

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/ivy-frontend
git add src/views/salary/SalaryHistoryPanel.vue
git commit -m "feat(salary): 薪資歷史表加可展開明細列、摘要改用對帳的獎金合計"
```

---

## 收尾（Definition of Done）

- [ ] **後端**：`feat/salary-history-breakdown-2026-06-06-be`（從 `origin/main` worktree）→ push → GitHub Actions 綠（含 `openapi-drift`）→ `git worktree remove`。
- [ ] **前端**：`feat/salary-history-breakdown-2026-06-06-fe` → 先在前端跑 `npm run gen:api:check`（確認 schema.d.ts 不漂移）+ `npm run typecheck` + `npx vitest run` → push → CI 綠 → `git worktree remove`。
- [ ] **整合驗證**：`./start.sh` 起兩端 → 「薪資設定/計算薪資」頁旁的「薪資歷史」分頁 → 選一員工 → 展開某月 → 對照「計算薪資」分頁同月數字，確認三區小計與實發一致。
- [ ] 跑 workspace `./scripts/finish-check.sh` 確認兩 repo 已收尾。

> push 後端會觸發 Zeabur（後端目前 SUSPENDED → 不部署）；前端 RUNNING → push 即上線。本變更無 migration、無破壞性 DDL，prod 安全。

---

## Self-Review

**1. Spec coverage：**
- 三區明細（進帳/另行轉帳/扣款）+ 權威小計 → Task 1（helper）+ Task 7（元件）✓
- 摘要「獎金合計」改對帳數字 `in_gross_bonus` → Task 3 + Task 8 ✓
- 主管紅利進實發 / 節慶超額另行轉帳 / 補充保費 informational 不重複 → Task 1 測試釘死 ✓
- other_income catch-all 吸收殘差 → Task 1 `test_income_lines_sum_to_subtotal_via_other` ✓
- response_model 變動 → gen:api → Task 4 + Task 5 + 收尾 `gen:api:check` ✓
- 無 migration / 無權限變更 → 計畫未含（正確）✓
- 非目標（不動 calculate_display_bonus_total / 不做 Portal / 不做 PDF）→ 計畫未含（正確）✓

**2. Placeholder scan：** 無 TBD/TODO；所有 step 含實際程式碼與指令。✓

**3. Type consistency：**
- 後端回傳 dict keys（income/income_subtotal/separate_transfer/separate_subtotal/deductions/deduction_subtotal/net_salary）在 Task 1 helper、Task 2 schema、Task 3 測試三處一致 ✓
- `payslip_detail` / `in_gross_bonus` / `separate_transfer_total` 在 schema（Task 2）、端點（Task 3）、前端 HistoryRow（Task 8）一致 ✓
- 前端 `PayslipDetail` / `BreakdownLine` 介面在 helper（Task 6）、元件（Task 7）、Panel（Task 8）一致 ✓
- helper 函式名 `nonZeroLines` / `hasSeparateTransfer` 在測試與元件一致 ✓

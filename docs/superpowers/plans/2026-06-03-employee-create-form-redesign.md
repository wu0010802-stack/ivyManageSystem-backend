# 新增員工表單重新設計（兩段式建檔）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將「新增員工」改為兩段式建檔（第一段基本、第二段薪資後補），新增 性別/Email/加保生效日 三欄、移除孤兒「部門」欄、並在列表提示「待補薪資」。

**Architecture:** 後端只新增 3 個 nullable 欄位 + schema 同步 + 1 支可逆 migration；持久化由既有 `Employee(**emp_data)` 自動帶入，讀回路徑（`EmployeeOut` + `_format_employee_response`）需手動補欄。兩段式為前端呈現切分：建檔對話框移除薪資區段，薪資沿用既有編輯模式高風險 tab。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（後端）、Vue 3 `<script setup lang="ts">` + Element Plus + Vitest（前端）。

**Spec:** `ivy-backend/docs/superpowers/specs/2026-06-03-employee-create-form-redesign-design.md`

**分支（執行時用 worktree，從 origin/main 開）:**
- 後端：`feat/employee-create-redesign-be`
- 前端：`feat/employee-create-redesign-fe`

---

## Part A — 後端（ivy-backend）

### Task A1: 新欄位 create + 讀回回歸測試（先寫失敗測試）

**Files:**
- Test: `tests/test_employees.py`

- [ ] **Step 1: 在 `tests/test_employees.py` 末尾新增測試**

```python
def test_create_employee_with_new_fields(client, admin_headers):
    """新增員工帶 gender / email / insurance_effective_date，能存能讀回。"""
    payload = {
        "name": "新欄位測試員",
        "employee_type": "regular",
        "gender": "女",
        "email": "newfield@example.com",
        "insurance_effective_date": "2026-07-01",
    }
    r = client.post("/api/employees", json=payload, headers=admin_headers)
    assert r.status_code == 201, r.text
    emp_id = r.json()["id"]

    detail = client.get(f"/api/employees/{emp_id}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["gender"] == "女"
    assert body["email"] == "newfield@example.com"
    assert body["insurance_effective_date"] == "2026-07-01"


def test_create_employee_without_salary_two_stage(client, admin_headers):
    """兩段式回歸：不帶任何薪資欄位仍可建檔成功。"""
    r = client.post(
        "/api/employees",
        json={"name": "只建人不填薪資", "employee_type": "regular"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
```

> 註：`client` / `admin_headers` fixture 沿用 `tests/test_employees.py` 既有慣例；若該檔 fixture 名稱不同（例如 `auth_headers`），改用該檔現有 fixture。先 `grep -n "def admin_headers\|def client\|headers=" tests/test_employees.py` 對齊。

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_employees.py::test_create_employee_with_new_fields -v`
Expected: FAIL —— 讀回 `body["gender"]` KeyError 或 422（schema 不認 gender）。

### Task A2: Employee model 加 3 欄

**Files:**
- Modify: `models/employee.py`

- [ ] **Step 1: 在 `birthday` 欄位後新增 gender / email；在 `pension_self_rate` 後新增 insurance_effective_date**

`models/employee.py` 找到 `birthday = Column(Date, nullable=True, comment="生日")`，其後加：

```python
    gender = Column(String(10), nullable=True, comment="性別：男/女/其他")
    email = Column(String(100), nullable=True, comment="電子郵件")
```

找到 `pension_self_rate = Column(Float, default=0, comment="勞退自提比例 (0~0.06)")`，其後加：

```python
    insurance_effective_date = Column(
        Date, nullable=True, comment="加保生效日（記錄用，不入薪資計算）"
    )
```

- [ ] **Step 2: 確認 import 已含 `Date` / `String`**

Run: `grep -nE "^\s+(Date|String),?" models/employee.py | head`
Expected: 兩者都已在 `from sqlalchemy import (...)` 內（現況已 import，無需改）。

### Task A3: schema 三處同步（Create / Update / Out）+ format + 日期解析 + PII

**Files:**
- Modify: `api/employees.py`（`EmployeeCreate`、`EmployeeUpdate`、`_format_employee_response`、`_DATE_FIELDS`）
- Modify: `schemas/employees.py`（`EmployeeOut`）

- [ ] **Step 1: `EmployeeCreate` 加 3 欄**

`api/employees.py` `class EmployeeCreate` 內，於 `teacher_cert_type` 後加：

```python
    gender: Optional[str] = Field(None, max_length=10)
    email: Optional[str] = Field(None, max_length=100)
    insurance_effective_date: Optional[str] = None
```

- [ ] **Step 2: `EmployeeUpdate` 加同 3 欄**

`class EmployeeUpdate` 內，於 `teacher_cert_type` 後加同上三行。

- [ ] **Step 3: `_DATE_FIELDS` 加入 insurance_effective_date**

把 `api/employees.py:54` 的
```python
_DATE_FIELDS = ("hire_date", "probation_end_date", "birthday")
```
改為
```python
_DATE_FIELDS = ("hire_date", "probation_end_date", "birthday", "insurance_effective_date")
```

- [ ] **Step 4: `_format_employee_response` 補讀回欄位**

找到 `"emergency_contact_phone": emp.emergency_contact_phone,`，其後加：

```python
        "gender": emp.gender,
        "email": emp.email,
        "insurance_effective_date": (
            emp.insurance_effective_date.isoformat()
            if emp.insurance_effective_date
            else None
        ),
```

- [ ] **Step 5: `EmployeeOut` 加 3 欄（email 標 pii-allow）**

`schemas/employees.py` `class EmployeeOut`，於聯絡資訊區（`emergency_contact_phone` 那行）後加：

```python
    gender: Optional[str] = None
    email: Optional[str] = None  # pii-allow: 員工聯絡 Email
    insurance_effective_date: Optional[str] = None
```

- [ ] **Step 6: 跑 PII schema 檢查腳本**

Run: `python3 scripts/check_pii_in_schemas.py`
Expected: PASS。若腳本對 `gender` 報未標記，於該行加 `# pii-allow: 性別（申報用，非敏感醫療）` 後重跑至 PASS。

- [ ] **Step 7: 跑 Task A1 測試確認通過**

Run: `python3 -m pytest tests/test_employees.py::test_create_employee_with_new_fields tests/test_employees.py::test_create_employee_without_salary_two_stage -v`
Expected: 2 passed。

- [ ] **Step 8: 跑員工相關測試全綠（無回歸）**

Run: `python3 -m pytest tests/test_employees.py tests/test_employees_self_edit.py tests/test_employee_special_status.py -q`
Expected: all passed。

### Task A4: 確認 Sentry PII denylist 涵蓋 email

**Files:**
- Read-only check: `utils/sentry_init.py`

- [ ] **Step 1: 確認 email 子字串已被遮罩、未被 exempt**

Run: `grep -niE "email|exempt" utils/sentry_init.py | head`
Expected: `_PII_KEY_SUBSTRINGS` 含 `email`（既有），且 `_PII_KEY_EXEMPT_SUBSTRINGS` 不含會誤放 `email` 的條目。若已涵蓋則無需改（前端同名單於 Part B 一併確認）。

### Task A5: Alembic migration（加 3 nullable 欄，可逆）

**Files:**
- Create: `alembic/versions/20260603_empnewcol01_employee_gender_email_insurance_date.py`

- [ ] **Step 1: 確認唯一 head**

Run: `python3 -m alembic heads`
Expected: `yeatpunch01 (head)`（單一；本 worktree off origin/main 的 head）。若非單一，停下回報。

- [ ] **Step 2: 建立 migration 檔**

```python
"""employee add gender / email / insurance_effective_date

Revision ID: empnewcol01
Revises: yeatpunch01
Create Date: 2026-06-03
"""
from alembic import op
import sqlalchemy as sa

revision = "empnewcol01"
down_revision = "yeatpunch01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("employees", sa.Column("gender", sa.String(length=10), nullable=True))
    op.add_column("employees", sa.Column("email", sa.String(length=100), nullable=True))
    op.add_column(
        "employees",
        sa.Column("insurance_effective_date", sa.Date(), nullable=True),
    )


def downgrade():
    op.drop_column("employees", "insurance_effective_date")
    op.drop_column("employees", "email")
    op.drop_column("employees", "gender")
```

- [ ] **Step 3: 套用 migration 到 dev DB**

Run: `python3 -m alembic upgrade heads`
Expected: 無錯；`python3 -m alembic heads` 顯示 `empnewcol01 (head)`。

- [ ] **Step 4: 驗證可逆（downgrade 再 upgrade）**

Run: `python3 -m alembic downgrade -1 && python3 -m alembic upgrade heads`
Expected: 皆成功，最終 head 為 `empnewcol01`。

### Task A6: 後端 commit

- [ ] **Step 1: Commit**

```bash
git add models/employee.py api/employees.py schemas/employees.py \
  alembic/versions/20260603_empnewcol01_employee_gender_email_insurance_date.py \
  tests/test_employees.py
git commit -m "feat(employees): 新增 gender/email/insurance_effective_date 欄位與讀回路徑

兩段式建檔 Phase 1 後端：model + EmployeeCreate/Update/Out + _format_employee_response
+ _DATE_FIELDS 日期解析 + alembic empnewcol01（可逆）。email 標 pii-allow。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Part B — 前端（ivy-frontend）

> 前置：Part A 已完成且後端程式碼可被 `dump_openapi.py` import。

### Task B0: 重新產生 OpenAPI 型別

**Files:**
- Modify: `src/api/_generated/schema.d.ts`（產生物）

- [ ] **Step 1: 從後端 dump openapi 後在前端 gen**

Run:
```bash
(cd ~/Desktop/ivy-backend && python3 scripts/dump_openapi.py)
cd ~/Desktop/ivy-frontend && npm run gen:api
```
Expected: `schema.d.ts` 內 employee 相關 schema 出現 `gender` / `email` / `insurance_effective_date`，不再因缺欄報型別錯。

- [ ] **Step 2: typecheck**

Run: `npm run typecheck`
Expected: PASS（此時 form 仍有 department，後續移除）。

### Task B1: 移除孤兒「部門」欄位

**Files:**
- Modify: `src/views/EmployeeView.vue`（form 初始化、resetForm、模板）
- Modify: `src/components/employee/EmployeeFormBasic.vue`（input + interface）
- Modify: `src/constants/employeeFields.ts`
- Modify: `src/constants/employeeFormSections.ts`

- [ ] **Step 1: 移除模板 input**

`EmployeeFormBasic.vue` 刪除整行：
```html
    <el-form-item label="部門"><el-input v-model="form.department" /></el-form-item>
```

- [ ] **Step 2: 移除 interface 欄位**

`EmployeeFormBasic.vue` `interface EmployeeFormBasicData` 內刪 `department?: string`。

- [ ] **Step 3: 移除 EmployeeView form 初始化與 resetForm 的 department**

`src/views/EmployeeView.vue` 刪除 `form` reactive 內的 `department: 'Teaching',`，以及 `resetForm` 內的 `form.department = 'Teaching'`。同時刪 `EmployeeForm` interface 內 `department` 宣告（若有）。

- [ ] **Step 4: 移除常數登記**

`src/constants/employeeFields.ts` 刪除 `'department',` 那行；
`src/constants/employeeFormSections.ts` 刪除 `department: 'jobDetail',` 那行。

- [ ] **Step 5: typecheck（確認無殘留引用）**

Run: `npm run typecheck`
Expected: PASS，無 `department` 未定義或未使用錯誤。

### Task B2: EmployeeFormBasic 新增 性別 / Email 輸入

**Files:**
- Modify: `src/components/employee/EmployeeFormBasic.vue`
- Modify: `src/constants/employeeFormSections.ts`
- Test: `tests/components/EmployeeFormBasic.test.ts`

- [ ] **Step 1: interface 加欄位**

`EmployeeFormBasicData` 加（若尚無）：`gender?: string`、`email?: string`（`email` 已存在於 interface 則略過）。

- [ ] **Step 2: 模板加「性別」到核心區（員工類型 el-form-item 後）**

```html
  <el-form-item label="性別">
    <el-select v-model="form.gender" clearable placeholder="請選擇" style="width:100%">
      <el-option label="男" value="男" />
      <el-option label="女" value="女" />
      <el-option label="其他" value="其他" />
    </el-select>
  </el-form-item>
```

- [ ] **Step 3: 模板加「Email」到個資聯絡區（聯絡電話 el-form-item 後）**

```html
    <el-form-item label="Email" prop="email">
      <el-input v-model="form.email" placeholder="example@mail.com" />
    </el-form-item>
```

- [ ] **Step 4: 區段登記**

`src/constants/employeeFormSections.ts` 的 `EMPLOYEE_FIELD_SECTION` 加：
```ts
  gender: 'core',
  email: 'personal',
  insurance_effective_date: 'salary',
```

- [ ] **Step 5: email 格式驗證 rule**

`src/views/EmployeeView.vue` 的 `const rules: FormRules = {` 內加：
```ts
  email: [{ type: 'email', message: 'Email 格式不正確', trigger: 'blur' }],
```

- [ ] **Step 6: 測試新欄渲染**

於 `tests/components/EmployeeFormBasic.test.ts` 加：
```ts
it('渲染性別與 Email 欄位，且不再有部門', () => {
  const wrapper = mountBasic()  // 沿用該檔既有 mount helper
  const text = wrapper.text()
  expect(text).toContain('性別')
  expect(text).toContain('Email')
  expect(text).not.toContain('部門')
})
```
> 若該檔無 `mountBasic` helper，仿照檔內既有 `mount(EmployeeFormBasic, {...})` 寫法。

- [ ] **Step 7: 跑測試**

Run: `npm run test:unit -- EmployeeFormBasic`
Expected: PASS。

### Task B3: EmployeeFormSalary 新增 加保生效日

**Files:**
- Modify: `src/components/employee/EmployeeFormSalary.vue`

- [ ] **Step 1: interface 加欄位**

`EmployeeFormSalaryData` 加 `insurance_effective_date?: string`。

- [ ] **Step 2: 模板於「投保級距」el-form-item 後加日期選擇**

```html
    <el-form-item label="加保生效日">
      <el-date-picker
        v-model="form.insurance_effective_date" type="date"
        placeholder="選擇日期" style="width:100%"
        value-format="YYYY-MM-DD" clearable
        :readonly="isReadonly"
      />
      <div class="hint" style="text-align:left">僅作記錄，不影響薪資計算</div>
    </el-form-item>
```

- [ ] **Step 3: typecheck**

Run: `npm run typecheck`
Expected: PASS。

### Task B4: 兩段式 — 建檔對話框移除薪資區段

**Files:**
- Modify: `src/views/EmployeeView.vue`

- [ ] **Step 1: 移除建檔分支的薪資 FormSection**

`src/views/EmployeeView.vue` 建檔模板 `<template v-if="!isEdit">` 內，刪除整個薪資區塊（`<FormSection ref="salarySectionRef" title="薪資・投保・銀行" ...>` 至對應 `</FormSection>`，含內部 `el-alert` 與 `<EmployeeFormSalary .../>`）。保留上方 `<EmployeeFormBasic .../>` 與 `required-legend`。

- [ ] **Step 2: 移除因移除而變成死碼的 refs（noUnusedLocals）**

刪除：
- `const salarySectionRef = ref<{ expand: () => void } | null>(null)`（約 line 57）
- `const salarySectionErrors = ref(0)`（約 line 58）
- `resetForm` 內 `salarySectionErrors.value = 0`
- `saveCreate` 內這三行（薪資欄在建檔已不存在）：
```ts
      const salaryProps = props.filter(p => sectionForField(p) === 'salary')
      salarySectionErrors.value = salaryProps.length
      if (salaryProps.length > 0) salarySectionRef.value?.expand()
```

- [ ] **Step 3: 清掉因此不再使用的 import（若有）**

確認 `sectionForField` 是否仍被其他處使用（`basicFormRef.value?.applyValidationErrors` 內部用的是元件自己的，View 層若僅 saveCreate 用到則移除該 import）。
Run: `grep -n "sectionForField" src/views/EmployeeView.vue`
若 0 筆使用，刪除其 import 行；否則保留。

- [ ] **Step 4: typecheck + EmployeeView 測試**

Run: `npm run typecheck && npm run test:unit -- EmployeeView`
Expected: PASS。若既有測試斷言建檔對話框含薪資欄，更新該斷言為「建檔不含薪資、編輯才有」。

### Task B5: 列表「待補薪資」提示 tag

**Files:**
- Modify: `src/views/EmployeeView.vue`

- [ ] **Step 1: 狀態欄加條件 tag**

`src/views/EmployeeView.vue` 狀態欄（`<el-table-column label="狀態" ...>`）的 `#default` 內，於既有狀態 `el-tag` 後加：

```html
            <el-tag
              v-if="scope.row.is_active && scope.row.employee_type === 'regular' && scope.row.base_salary === 0"
              type="warning" size="small" effect="plain" style="margin-left:4px"
            >待補薪資</el-tag>
```

> 用嚴格 `=== 0`：無薪資檢視權者該欄為 `null`（被遮罩），不會誤顯示。

- [ ] **Step 2: typecheck**

Run: `npm run typecheck`
Expected: PASS。

### Task B6: 前端全測 + commit

- [ ] **Step 1: 跑前端單元測試**

Run: `npm run test:unit -- employee EmployeeForm EmployeeView`
Expected: 相關測試全綠。

- [ ] **Step 2: Commit**

```bash
git add src/views/EmployeeView.vue src/components/employee/EmployeeFormBasic.vue \
  src/components/employee/EmployeeFormSalary.vue src/constants/employeeFields.ts \
  src/constants/employeeFormSections.ts src/api/_generated/schema.d.ts \
  tests/components/EmployeeFormBasic.test.ts
git commit -m "feat(employee): 兩段式建檔 + 新增性別/Email/加保生效日 + 移除部門孤兒欄

建檔對話框移除薪資區段（薪資改於編輯模式高風險 tab 補），列表加「待補薪資」提示。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Part C — 整合驗證

- [ ] **Step 1: 起兩端**

Run: `cd ~/Desktop/ivyManageSystem && ./start.sh`

- [ ] **Step 2: 手動走一次兩段式**

1. 新增員工 → 確認對話框只有基本欄位（無薪資/投保/銀行），有 性別、Email；無部門。
2. 只填姓名 + 類型「正職」儲存 → 列表出現該員工且顯示「待補薪資」tag。
3. 編輯該員工 → 薪資 tab 在 → 填底薪 → 觸發變更摘要確認 → 儲存 → tag 消失。
4. 編輯/新增填加保生效日 → 詳情讀回正確。

---

## Self-Review（計畫對照 spec）

- spec §4 增 gender/email/insurance_effective_date → Task A2/A3 + B2/B3 ✓
- spec §4 移除 department → Task B1（前端 5 處）✓
- spec §4 不動 title → 計畫未觸碰 title ✓
- spec §5 後端 model/schema/Out/format/_DATE_FIELDS/PII/migration → Task A2–A5 ✓
- spec §5.6 PII（email pii-allow + 腳本 + Sentry denylist）→ A3 Step5/6 + A4 ✓
- spec §6 前端兩段式 + 新欄 + 待補薪資 tag + codegen → B0/B2/B3/B4/B5 ✓
- spec §3 兩段式不需新後端端點（建檔本就允許無薪資）→ A1 第二測試驗證 ✓
- spec §7 後端先行、分開 commit → Part A 先於 Part B、各一 commit ✓

無 placeholder；型別/欄位名（gender/email/insurance_effective_date）跨任務一致。

# 預設角色庫 (a) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `utils/permissions.py` 擴充 principal/accountant 兩個 ROLE_TEMPLATE + ROLE_DESCRIPTIONS 字典，把 SettingsUsersTab.vue 從「下拉 + 56 條 checkbox 平面展開」重組為「7 角色卡片 + 進階微調 expander」。

**Architecture:** 純後端 in-code dict 擴充（零 DB schema 變動）+ 前端單一 Vue 元件重組（沿用既有 `onRoleChange` / `togglePermission` helper）。後端 `GET /permissions` response 純擴欄位（向後相容）。Spec 見 `docs/superpowers/specs/2026-05-25-permission-role-library-design.md`。

**Tech Stack:** FastAPI / SQLAlchemy / pytest（後端）；Vue 3 `<script setup lang="ts">` / Element Plus / vitest @vue/test-utils（前端）。

**Repo layout：** 後端任務在 `ivy-backend/`、前端任務在 `ivy-frontend/`。建議用 worktree 隔離：
- 後端：`feat/permission-role-library-2026-05-25-backend`
- 前端：`feat/permission-role-library-2026-05-25-frontend`

---

## Phase A：後端（`ivy-backend/`）

### Task 1: 加 principal 角色與 label

**Files:**
- Modify: `ivy-backend/utils/permissions.py:200-298`（ROLE_TEMPLATES）、`ivy-backend/utils/permissions.py:302-308`（ROLE_LABELS）
- Test: `ivy-backend/tests/test_permissions_unit.py`

- [ ] **Step 1.1: 在 `tests/test_permissions_unit.py` 末尾加 3 條 test（先失敗）**

```python
def test_role_templates_principal_inherits_supervisor():
    """principal 必須含 supervisor 全部 + 3 條額外。"""
    sup_set = set(ROLE_TEMPLATES["supervisor"])
    pri_set = set(ROLE_TEMPLATES["principal"])
    assert sup_set.issubset(pri_set)
    extras = pri_set - sup_set
    assert extras == {
        Permission.SALARY_READ.value,
        Permission.AUDIT_LOGS.value,
        Permission.GOV_REPORTS_EXPORT.value,
    }


def test_role_templates_principal_excludes_write_and_admin_permissions():
    """principal 不可含 SALARY_WRITE / USER_MANAGEMENT_* / SETTINGS_*。"""
    pri = ROLE_TEMPLATES["principal"]
    assert Permission.SALARY_WRITE.value not in pri
    assert Permission.USER_MANAGEMENT_READ.value not in pri
    assert Permission.USER_MANAGEMENT_WRITE.value not in pri
    assert Permission.SETTINGS_READ.value not in pri
    assert Permission.SETTINGS_WRITE.value not in pri


def test_role_labels_principal_zh():
    """principal 中文 label = 園長。"""
    from utils.permissions import ROLE_LABELS
    assert ROLE_LABELS["principal"] == "園長"
```

- [ ] **Step 1.2: 跑 test，預期 fail（principal key 不存在 → KeyError）**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "principal" -v
```

預期：3 條全 FAIL with `KeyError: 'principal'`。

- [ ] **Step 1.3: Edit `utils/permissions.py` 在 ROLE_TEMPLATES / ROLE_LABELS dict literal 之後 mutate 新增 principal**

採用「**字典定義後 mutate**」方案：既有 `ROLE_TEMPLATES = {...}` 與 `ROLE_LABELS = {...}` dict literal **一字不動**，在 `ROLE_LABELS` closing `}` 之後（約 `utils/permissions.py:309`）加：

```python

# principal 角色：supervisor 全部 + 薪資審視 + 稽核 + 政府報表匯出
ROLE_TEMPLATES["principal"] = ROLE_TEMPLATES["supervisor"] + [
    Permission.SALARY_READ.value,
    Permission.AUDIT_LOGS.value,
    Permission.GOV_REPORTS_EXPORT.value,
]
ROLE_LABELS["principal"] = "園長"
```

**為何採此方案**：(1) 既有 dict literal 結構完全不動，diff 最小；(2) `ROLE_TEMPLATES["supervisor"] + [...]` 在 module load 時一次性求值，runtime 無 overhead；(3) 後續 Task 2/3 也用相同 mutate 風格保持一致。

- [ ] **Step 1.4: 跑 test，預期 pass**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "principal" -v
```

預期：3 條 PASS。

---

### Task 2: 加 accountant 角色與 label

**Files:**
- Modify: `ivy-backend/utils/permissions.py`（同上）
- Test: `ivy-backend/tests/test_permissions_unit.py`

- [ ] **Step 2.1: 在 `tests/test_permissions_unit.py` 末尾加 3 條 test**

```python
def test_role_templates_accountant_pure_finance():
    """accountant 只含財務 + EMPLOYEES_READ；不可含 EMPLOYEES_WRITE / 考勤 / 學生 / 招生 / 政府匯出。"""
    acc = set(ROLE_TEMPLATES["accountant"])
    forbidden = {
        Permission.EMPLOYEES_WRITE.value,
        Permission.ATTENDANCE_READ.value,
        Permission.ATTENDANCE_WRITE.value,
        Permission.LEAVES_READ.value,
        Permission.STUDENTS_READ.value,
        Permission.RECRUITMENT_READ.value,
        Permission.GOV_REPORTS_EXPORT.value,
        Permission.YEAR_END_FINALIZE.value,
    }
    assert forbidden.isdisjoint(acc), f"accountant 不該含: {forbidden & acc}"


def test_role_templates_accountant_includes_finance_core():
    """accountant 必須含薪資/廠商/學費/年終讀寫 + APPRAISAL_ACCOUNTING。"""
    acc = set(ROLE_TEMPLATES["accountant"])
    required = {
        Permission.EMPLOYEES_READ.value,
        Permission.SALARY_READ.value,
        Permission.SALARY_WRITE.value,
        Permission.FEES_READ.value,
        Permission.FEES_WRITE.value,
        Permission.VENDOR_PAYMENT_READ.value,
        Permission.VENDOR_PAYMENT_WRITE.value,
        Permission.YEAR_END_READ.value,
        Permission.YEAR_END_WRITE.value,
        Permission.APPRAISAL_ACCOUNTING.value,
    }
    assert required.issubset(acc), f"accountant 缺: {required - acc}"


def test_role_labels_accountant_zh():
    from utils.permissions import ROLE_LABELS
    assert ROLE_LABELS["accountant"] == "會計"
```

- [ ] **Step 2.2: 跑 test，預期 fail**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "accountant" -v
```

預期：3 條 FAIL with `KeyError: 'accountant'`。

- [ ] **Step 2.3: Edit `utils/permissions.py` 在 ROLE_TEMPLATES dict 之後（與 Task 1 的 principal mutation 同區塊）加：**

```python
ROLE_TEMPLATES["accountant"] = [
    Permission.DASHBOARD.value,
    Permission.REPORTS.value,
    Permission.GOV_REPORTS_VIEW.value,
    Permission.EMPLOYEES_READ.value,        # 要看誰可申報薪資（不含 WRITE）
    Permission.SALARY_READ.value,
    Permission.SALARY_WRITE.value,
    Permission.FEES_READ.value,
    Permission.FEES_WRITE.value,
    Permission.VENDOR_PAYMENT_READ.value,
    Permission.VENDOR_PAYMENT_WRITE.value,
    Permission.YEAR_END_READ.value,
    Permission.YEAR_END_WRITE.value,        # 不含 FINALIZE（簽核屬 supervisor/principal）
    Permission.APPRAISAL_ACCOUNTING.value,  # 核考核獎金數字
]
ROLE_LABELS["accountant"] = "會計"
```

- [ ] **Step 2.4: 跑 test，預期 pass**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "accountant" -v
```

預期：3 條 PASS。

---

### Task 3: 加 ROLE_DESCRIPTIONS 字典

**Files:**
- Modify: `ivy-backend/utils/permissions.py`
- Test: `ivy-backend/tests/test_permissions_unit.py`

- [ ] **Step 3.1: 在 `tests/test_permissions_unit.py` 末尾加 2 條 test**

```python
def test_role_descriptions_complete():
    """每個 ROLE_TEMPLATES key 都有對應 ROLE_DESCRIPTIONS。"""
    from utils.permissions import ROLE_DESCRIPTIONS
    assert set(ROLE_TEMPLATES.keys()) == set(ROLE_DESCRIPTIONS.keys())


def test_role_descriptions_non_empty():
    """ROLE_DESCRIPTIONS 每個值非空字串。"""
    from utils.permissions import ROLE_DESCRIPTIONS
    for role, desc in ROLE_DESCRIPTIONS.items():
        assert isinstance(desc, str) and len(desc) > 0, f"{role} description 空"
```

- [ ] **Step 3.2: 跑 test，預期 fail**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "role_descriptions" -v
```

預期：FAIL with `ImportError: cannot import name 'ROLE_DESCRIPTIONS'`。

- [ ] **Step 3.3: Edit `utils/permissions.py` 在 ROLE_LABELS dict 之後加 ROLE_DESCRIPTIONS**

```python
# 角色說明（給前端 SettingsUsersTab 卡片顯示）
ROLE_DESCRIPTIONS: Dict[str, str] = {
    "admin": "唯一能改帳號、系統設定",
    "principal": "業務全包 + 薪資審視，不動帳號",
    "supervisor": "教務管理、招生轉換、考核全程",
    "hr": "員工資料、薪資發放、年終、廠商付款",
    "accountant": "純財務（薪資/學費/廠商/年終）",
    "teacher": "公告、考勤、放學接送、學生檔案",
    "parent": "家長端登入，無管理端權限",
}
```

- [ ] **Step 3.4: 跑 test，預期 pass**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "role_descriptions" -v
```

預期：2 條 PASS。

---

### Task 4: `get_permissions_definition()` 暴露 description

**Files:**
- Modify: `ivy-backend/utils/permissions.py:582-603`
- Test: `ivy-backend/tests/test_permissions_unit.py`

- [ ] **Step 4.1: 在 `tests/test_permissions_unit.py` 末尾加 1 條 test**

```python
def test_get_permissions_definition_includes_role_descriptions():
    """get_permissions_definition().roles[*] 應含 description 欄位。"""
    definition = get_permissions_definition()
    roles = definition["roles"]
    for role_key in ROLE_TEMPLATES.keys():
        assert role_key in roles, f"roles 缺 {role_key}"
        assert "description" in roles[role_key], f"{role_key} 缺 description"
        assert "label" in roles[role_key]
        assert "permissions" in roles[role_key]
```

- [ ] **Step 4.2: 跑 test，預期 fail**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "definition_includes_role_descriptions" -v
```

預期：FAIL with `AssertionError: admin 缺 description`。

- [ ] **Step 4.3: Edit `utils/permissions.py:591-597`**

把：

```python
    roles = {
        role: {
            "permissions": perms,
            "label": ROLE_LABELS.get(role, role),
        }
        for role, perms in ROLE_TEMPLATES.items()
    }
```

改為：

```python
    roles = {
        role: {
            "permissions": perms,
            "label": ROLE_LABELS.get(role, role),
            "description": ROLE_DESCRIPTIONS.get(role, ""),
        }
        for role, perms in ROLE_TEMPLATES.items()
    }
```

- [ ] **Step 4.4: 跑 test，預期 pass**

```bash
cd ivy-backend && pytest tests/test_permissions_unit.py -k "definition_includes_role_descriptions" -v
```

預期：PASS。

---

### Task 5: `GET /permissions` endpoint integration test

**Files:**
- Test: `ivy-backend/tests/test_auth.py`（在末尾追加）或新建 `tests/test_permissions_endpoint.py`

- [ ] **Step 5.1: 先確認 test_auth.py 是否已有 `/permissions` endpoint test**

```bash
cd ivy-backend && grep -n "/permissions\|get_permissions" tests/test_auth.py
```

若已有相關 fixture（admin_token / client），用既有 pattern；若無 endpoint test，續 5.2 新建。

- [ ] **Step 5.2: 新建 `tests/test_permissions_endpoint.py`**

```python
"""Integration test: GET /api/permissions returns extended role response."""

from fastapi.testclient import TestClient
import pytest


def test_get_permissions_endpoint_returns_role_descriptions(client: TestClient, admin_token: str):
    """GET /api/permissions 應回傳 7 個 role 且每個含 label/description/permissions。"""
    response = client.get(
        "/api/permissions",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "roles" in payload
    roles = payload["roles"]
    expected_roles = {"admin", "principal", "supervisor", "hr", "accountant", "teacher", "parent"}
    assert expected_roles == set(roles.keys()), f"角色不齊: 缺 {expected_roles - set(roles)}, 多 {set(roles) - expected_roles}"
    for role_key, role_data in roles.items():
        assert "label" in role_data
        assert "description" in role_data and len(role_data["description"]) > 0
        assert "permissions" in role_data


def test_get_permissions_endpoint_admin_uses_wildcard(client: TestClient, admin_token: str):
    """admin 的 permissions 是 ['*']。"""
    response = client.get(
        "/api/permissions",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    payload = response.json()
    assert payload["roles"]["admin"]["permissions"] == ["*"]
```

**注意**：`client` 與 `admin_token` fixtures 需要與既有 `tests/conftest.py` 提供的一致。先 grep 確認：

```bash
cd ivy-backend && grep -n "def client\|def admin_token" tests/conftest.py
```

若 fixture 名稱不同（例如 `admin_client` / `auth_headers`），改用對應名稱。

- [ ] **Step 5.3: 跑新測試**

```bash
cd ivy-backend && pytest tests/test_permissions_endpoint.py -v
```

預期：2 條 PASS。

---

### Task 6: 跑全套後端 pytest + commit

- [ ] **Step 6.1: 跑全套後端測試確認零回歸**

```bash
cd ivy-backend && pytest
```

預期：所有測試 PASS（含新增 9 條）。

- [ ] **Step 6.2: 跑 type/lint check（若 repo 有 mypy/ruff）**

```bash
cd ivy-backend && ruff check utils/permissions.py tests/test_permissions_unit.py tests/test_permissions_endpoint.py 2>/dev/null || true
```

- [ ] **Step 6.3: git commit**

```bash
cd ivy-backend
git add utils/permissions.py tests/test_permissions_unit.py tests/test_permissions_endpoint.py
git status  # 確認沒夾帶其他變更
git commit -m "$(cat <<'EOF'
feat(permissions): add principal/accountant role templates + role descriptions

- ROLE_TEMPLATES 新增 principal（supervisor + SALARY_READ + AUDIT_LOGS + GOV_REPORTS_EXPORT）
- ROLE_TEMPLATES 新增 accountant（純財務，13 條，不含 EMPLOYEES_WRITE）
- 新增 ROLE_DESCRIPTIONS 字典（7 個 role 中文說明）
- get_permissions_definition() 在 roles[*] 加 description 欄位（向後相容）

零 DB schema 變動；既有 admin/hr/supervisor/teacher/parent 完全不動。

Spec: docs/superpowers/specs/2026-05-25-permission-role-library-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase B：前端（`ivy-frontend/`）

### Task 7: 寫 SettingsUsersTab.test.ts 5 條（先失敗版）

**Files:**
- Create: `ivy-frontend/src/components/settings/__tests__/SettingsUsersTab.test.ts`

- [ ] **Step 7.1: 確認測試慣例（mock pattern）**

```bash
cd ivy-frontend && cat src/components/settings/__tests__/SettingsAcademicTermsTab.test.ts | head -30
```

參考既有 vi.mock 結構與 mount pattern。

- [ ] **Step 7.2: 建立新測試檔**

`ivy-frontend/src/components/settings/__tests__/SettingsUsersTab.test.ts`：

```ts
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'

const mockPermissionDefinition = {
  permissions: {
    EMPLOYEES_READ: { label: '員工檢視', value: 'EMPLOYEES_READ' },
    EMPLOYEES_WRITE: { label: '員工編輯', value: 'EMPLOYEES_WRITE' },
    SALARY_READ: { label: '薪資檢視', value: 'SALARY_READ' },
    SALARY_WRITE: { label: '薪資編輯', value: 'SALARY_WRITE' },
    DASHBOARD: { label: '儀表板', value: 'DASHBOARD' },
  },
  groups: [
    { name: '員工管理', permissions: [], split_permissions: [{ module: '員工', read: 'EMPLOYEES_READ', write: 'EMPLOYEES_WRITE' }] },
    { name: '薪資', permissions: [], split_permissions: [{ module: '薪資', read: 'SALARY_READ', write: 'SALARY_WRITE' }] },
    { name: '基礎', permissions: ['DASHBOARD'] },
  ],
  roles: {
    admin: { label: '系統管理員', description: '唯一能改帳號、系統設定', permissions: ['*'] },
    principal: { label: '園長', description: '業務全包 + 薪資審視，不動帳號', permissions: ['DASHBOARD', 'EMPLOYEES_READ', 'SALARY_READ'] },
    supervisor: { label: '主管', description: '教務管理、招生轉換、考核全程', permissions: ['DASHBOARD', 'EMPLOYEES_READ'] },
    hr: { label: '人事管理員', description: '員工資料、薪資發放、年終、廠商付款', permissions: ['DASHBOARD', 'EMPLOYEES_READ', 'EMPLOYEES_WRITE', 'SALARY_READ', 'SALARY_WRITE'] },
    accountant: { label: '會計', description: '純財務（薪資/學費/廠商/年終）', permissions: ['DASHBOARD', 'EMPLOYEES_READ', 'SALARY_READ', 'SALARY_WRITE'] },
    teacher: { label: '教師', description: '公告、考勤、放學接送、學生檔案', permissions: ['DASHBOARD'] },
    parent: { label: '家長', description: '家長端登入，無管理端權限', permissions: [] },
  },
}

vi.mock('@/api/auth', () => ({
  getUsers: vi.fn().mockResolvedValue({ data: [] }),
  getPermissions: vi.fn().mockResolvedValue({ data: mockPermissionDefinition }),
  createUser: vi.fn().mockResolvedValue({ data: {} }),
  updateUser: vi.fn().mockResolvedValue({ data: {} }),
  deleteUser: vi.fn().mockResolvedValue({ data: { ok: true } }),
  resetPassword: vi.fn().mockResolvedValue({ data: { ok: true } }),
}))

vi.mock('@/stores/employee', () => ({
  useEmployeeStore: () => ({
    fetchEmployees: vi.fn(),
    employees: { value: [] },
  }),
}))

import SettingsUsersTab from '../SettingsUsersTab.vue'

describe('SettingsUsersTab — role card UX', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  async function mountAndOpenAddDialog() {
    const wrapper = mount(SettingsUsersTab, { attachTo: document.body })
    await flushPromises()
    // 「新增帳號」按鈕在 .tab-header 內，與 dialog footer 按鈕區分
    const addBtn = wrapper.find('.tab-header button.el-button--primary')
    await addBtn.trigger('click')
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('renders 7 role cards in new-user dialog', async () => {
    const wrapper = await mountAndOpenAddDialog()
    const cards = document.querySelectorAll('.role-card')
    expect(cards.length).toBe(7)
    const roleKeys = Array.from(cards).map((el) => el.getAttribute('data-role'))
    expect(roleKeys.sort()).toEqual(['accountant', 'admin', 'hr', 'parent', 'principal', 'supervisor', 'teacher'])
  })

  it('clicking principal card fills form with role template and collapses expander', async () => {
    const wrapper = await mountAndOpenAddDialog()
    const principalCard = document.querySelector('.role-card[data-role="principal"]') as HTMLElement
    principalCard.click()
    await flushPromises()
    await nextTick()
    // expander collapsed (內部 v-show 或 v-if 控制)
    const expanderContent = document.querySelector('.advanced-tuning-content')
    expect(expanderContent === null || (expanderContent as HTMLElement).style.display === 'none').toBe(true)
    // badge 顯示「預設」
    const badge = document.querySelector('.deviation-badge')
    expect(badge?.textContent).toContain('預設')
  })

  it('toggling a checkbox auto-expands the expander and shows deviation badge', async () => {
    const wrapper = await mountAndOpenAddDialog()
    // 先選 principal 套用預設
    ;(document.querySelector('.role-card[data-role="principal"]') as HTMLElement).click()
    await flushPromises()
    await nextTick()
    // 手動展開 expander 才能 click checkbox
    const toggleBtn = document.querySelector('.advanced-tuning-toggle') as HTMLElement
    toggleBtn?.click()
    await nextTick()
    // toggle 一個 checkbox（從 principal 預設移除 SALARY_READ）
    const checkboxes = document.querySelectorAll('.permission-section input[type="checkbox"]')
    const salaryReadCheckbox = Array.from(checkboxes).find((el) => {
      const label = el.closest('.el-checkbox')?.textContent
      return label?.includes('薪資檢視')
    }) as HTMLInputElement
    salaryReadCheckbox.click()
    await flushPromises()
    await nextTick()
    // badge 顯示「已偏離 1 項」
    const badge = document.querySelector('.deviation-badge')
    expect(badge?.textContent).toContain('已偏離')
    // 還原預設 button 出現
    expect(document.querySelector('.restore-default-btn')).not.toBeNull()
  })

  it('clicking 還原預設 resets to role template', async () => {
    const wrapper = await mountAndOpenAddDialog()
    ;(document.querySelector('.role-card[data-role="principal"]') as HTMLElement).click()
    await flushPromises()
    await nextTick()
    // 製造偏離
    const toggleBtn = document.querySelector('.advanced-tuning-toggle') as HTMLElement
    toggleBtn?.click()
    await nextTick()
    const checkboxes = document.querySelectorAll('.permission-section input[type="checkbox"]')
    ;(checkboxes[0] as HTMLInputElement).click()
    await flushPromises()
    await nextTick()
    // 點還原
    const restoreBtn = document.querySelector('.restore-default-btn') as HTMLElement
    restoreBtn.click()
    await flushPromises()
    await nextTick()
    const badge = document.querySelector('.deviation-badge')
    expect(badge?.textContent).toContain('預設')
    expect(document.querySelector('.restore-default-btn')).toBeNull()
  })

  it('parent role card is disabled with tooltip', async () => {
    const wrapper = await mountAndOpenAddDialog()
    const parentCard = document.querySelector('.role-card[data-role="parent"]')
    expect(parentCard?.classList.contains('is-disabled')).toBe(true)
    expect(parentCard?.getAttribute('title') || parentCard?.querySelector('[role="tooltip"]')?.textContent).toContain('家長端 LIFF')
  })
})
```

- [ ] **Step 7.3: 跑 vitest，預期 5 條 FAIL**

```bash
cd ivy-frontend && npm test -- src/components/settings/__tests__/SettingsUsersTab.test.ts
```

預期：5 條 FAIL（沒有 .role-card / .advanced-tuning-toggle / .deviation-badge / .restore-default-btn / .is-disabled 等 selectors）。

---

### Task 8: 重組 SettingsUsersTab.vue 為「角色卡片 + 進階微調 expander」

**Files:**
- Modify: `ivy-frontend/src/components/settings/SettingsUsersTab.vue`（整個 template + script 重組；style 加新 class）

- [ ] **Step 8.1: 在 `<script setup lang="ts">` 區頂部加 ROLE_ICONS / ROLE_ORDER 與 expanded ref**

緊接在既有 import 之後加：

```ts
import { computed } from 'vue'

const ROLE_ICONS: Record<string, string> = {
  admin: '👑',
  principal: '🏫',
  supervisor: '📋',
  hr: '💼',
  accountant: '💰',
  teacher: '📚',
  parent: '👨‍👩‍👧',
}

const ROLE_ORDER = ['admin', 'principal', 'supervisor', 'hr', 'accountant', 'teacher', 'parent']

const advancedExpanded = ref<boolean>(false)
```

- [ ] **Step 8.2: 加 `deviationCount` computed 與 `restoreDefault` / `selectRoleCard` 兩個 method**

加在既有 `onRoleChange` 之後：

```ts
const _activeForm = computed<{ role: string; permission_names: string[] } | null>(() => {
  if (userDialogVisible.value) return userForm
  if (editUserDialogVisible.value) return editUserForm
  return null
})

const deviationCount = computed<number>(() => {
  const form = _activeForm.value
  if (!form) return 0
  const roleConfig = permissionDefinition.value.roles[form.role]
  if (!roleConfig) return 0
  const tpl = roleConfig.permissions
  if (form.permission_names.includes('*')) {
    return tpl.includes('*') ? 0 : Object.keys(permissionDefinition.value.permissions).length
  }
  if (tpl.includes('*')) {
    // role 預設是 wildcard 但 form 是顯式清單
    return Object.keys(permissionDefinition.value.permissions).length - form.permission_names.length
  }
  const tplSet = new Set(tpl)
  const formSet = new Set(form.permission_names)
  let count = 0
  for (const p of form.permission_names) if (!tplSet.has(p)) count++
  for (const p of tpl) if (!formSet.has(p)) count++
  return count
})

const selectRoleCard = (form: { role: string; permission_names: string[] }, roleKey: string) => {
  if (roleKey === 'parent') return  // disabled
  form.role = roleKey
  onRoleChange(form)
  advancedExpanded.value = false
}

const restoreDefault = (form: { role: string; permission_names: string[] }) => {
  onRoleChange(form)
  advancedExpanded.value = false
}

// 開啟編輯 dialog 時依偏離狀態決定 expander 初始
const _openEditExpander = () => {
  advancedExpanded.value = deviationCount.value > 0
}
```

- [ ] **Step 8.3: 在 `handleEditUser` 末尾呼叫 `_openEditExpander()`**

修改既有 `handleEditUser`：

```ts
const handleEditUser = (user: Record<string, unknown>) => {
  editUserForm.id = user.id as number
  editUserForm.username = user.username as string
  editUserForm.role = user.role as string
  editUserForm.permission_names = (user.permission_names as string[] | null) ?? ['*']
  editUserDialogVisible.value = true
  nextTick(() => _openEditExpander())
}
```

`handleAddUser` 也加 `advancedExpanded.value = false`：

```ts
const handleAddUser = () => {
  userForm.employee_id = null
  userForm.username = ''
  userForm.password = ''
  userForm.role = 'teacher'
  userForm.permission_names = ['*']
  advancedExpanded.value = false
  employeeStore.fetchEmployees()
  userDialogVisible.value = true
}
```

import nextTick：

```ts
import { ref, reactive, onMounted, computed, nextTick } from 'vue'
```

- [ ] **Step 8.4: 修改 `togglePermission` 加 auto-expand**

```ts
const togglePermission = (form: { permission_names: string[]; role: string }, permName: string) => {
  if (form.permission_names.includes('*')) {
    const allPerms = permissionsCombine([Object.keys(permissionDefinition.value.permissions)])
    form.permission_names = permissionsRemove(allPerms, permName)
  } else if (form.permission_names.includes(permName)) {
    form.permission_names = permissionsRemove(form.permission_names, permName)
  } else {
    form.permission_names = permissionsAdd(form.permission_names, permName)
  }
  // 若新狀態為偏離，強制展開
  if (deviationCount.value > 0) {
    advancedExpanded.value = true
  }
}
```

- [ ] **Step 8.5: 重寫新增 dialog 的「角色」與「權限」`<el-form-item>` 區塊**

把既有兩個 form-item（`<el-form-item label="角色">...` 與 `<el-form-item v-if="userForm.role !== 'teacher'" label="權限">...`）整段取代為：

```vue
        <el-form-item label="角色">
          <div class="role-cards-grid">
            <div
              v-for="roleKey in ROLE_ORDER"
              :key="roleKey"
              class="role-card"
              :data-role="roleKey"
              :class="{
                'role-card--active': userForm.role === roleKey,
                'is-disabled': roleKey === 'parent',
              }"
              :title="roleKey === 'parent' ? '家長帳號請從家長端 LIFF 綁定' : ''"
              @click="selectRoleCard(userForm, roleKey)"
            >
              <div class="role-card__icon">{{ ROLE_ICONS[roleKey] || '👤' }}</div>
              <div class="role-card__label">{{ permissionDefinition.roles[roleKey]?.label || roleKey }}</div>
              <div class="role-card__desc">{{ permissionDefinition.roles[roleKey]?.description || '' }}</div>
              <div class="role-card__count">
                <el-tag size="small" :type="roleKey === 'admin' ? 'danger' : 'info'">
                  {{ permissionDefinition.roles[roleKey]?.permissions?.includes('*') ? '全部' : `${permissionDefinition.roles[roleKey]?.permissions?.length ?? 0} 條` }}
                </el-tag>
              </div>
            </div>
          </div>
        </el-form-item>

        <el-form-item v-if="userForm.role !== 'teacher' && userForm.role !== 'parent'" label="權限">
          <div class="advanced-tuning">
            <div class="advanced-tuning__header">
              <button
                type="button"
                class="advanced-tuning-toggle"
                @click="advancedExpanded = !advancedExpanded"
              >
                <span>{{ advancedExpanded ? '▼' : '▶' }} 進階微調</span>
                <el-tag
                  class="deviation-badge"
                  :type="deviationCount > 0 ? 'warning' : 'info'"
                  size="small"
                >
                  {{ deviationCount > 0 ? `已偏離 ${deviationCount} 項` : '預設' }}
                </el-tag>
              </button>
              <el-button
                v-if="deviationCount > 0"
                class="restore-default-btn"
                link
                type="primary"
                size="small"
                @click="restoreDefault(userForm)"
              >
                ↻ 還原預設
              </el-button>
            </div>
            <div v-show="advancedExpanded" class="advanced-tuning-content">
              <div class="permission-section">
                <div class="permission-actions">
                  <el-button size="small" @click="selectAllPermissions(userForm)">全選</el-button>
                  <el-button size="small" @click="clearAllPermissions(userForm)">清除</el-button>
                </div>
                <div v-for="group in permissionDefinition.groups" :key="group.name" class="permission-group">
                  <div class="permission-group-title">{{ group.name }}</div>
                  <div class="permission-checkboxes">
                    <el-checkbox
                      v-for="perm in (group.permissions || [])"
                      :key="perm"
                      :model-value="isPermissionChecked(userForm, perm)"
                      @change="togglePermission(userForm, perm)"
                    >
                      {{ getPermissionLabel(perm) }}
                    </el-checkbox>
                  </div>
                  <div v-if="group.split_permissions" class="split-permission-list">
                    <div v-for="sp in group.split_permissions" :key="sp.read" class="split-permission-row">
                      <span class="split-permission-label">{{ sp.module }}</span>
                      <el-checkbox
                        :model-value="isPermissionChecked(userForm, sp.read)"
                        @change="togglePermission(userForm, sp.read)"
                      >檢視</el-checkbox>
                      <el-checkbox
                        :model-value="isPermissionChecked(userForm, sp.write)"
                        @change="togglePermission(userForm, sp.write)"
                      >編輯</el-checkbox>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </el-form-item>
```

- [ ] **Step 8.6: 對「編輯」dialog 同樣替換兩個 form-item（把上面整段複製、所有 `userForm` 改為 `editUserForm`）**

把編輯 dialog 內既有 `<el-form-item label="角色">` + `<el-form-item v-if="editUserForm.role !== 'teacher'" label="權限">` 整段以同上 template 取代，**只改變數名為 editUserForm**。

- [ ] **Step 8.7: 加 CSS（在 `<style scoped>` 內既有 .tab-header 之後）**

```css
.role-cards-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  width: 100%;
}

@media (max-width: 720px) {
  .role-cards-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

.role-card {
  padding: 12px;
  border: 2px solid var(--el-border-color-lighter);
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.15s ease;
  background: #fff;
  text-align: center;
}

.role-card:hover:not(.is-disabled) {
  border-color: var(--el-color-primary-light-5);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.role-card--active {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

.role-card.is-disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.role-card__icon {
  font-size: 24px;
  margin-bottom: 4px;
}

.role-card__label {
  font-weight: 600;
  font-size: 14px;
  color: var(--text-primary);
}

.role-card__desc {
  font-size: 12px;
  color: var(--text-tertiary);
  margin: 6px 0 8px;
  min-height: 28px;
  line-height: 1.3;
}

.role-card__count {
  display: flex;
  justify-content: center;
}

.advanced-tuning {
  width: 100%;
}

.advanced-tuning__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.advanced-tuning-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  background: none;
  border: none;
  padding: 4px 0;
  cursor: pointer;
  font-size: 14px;
  color: var(--text-primary);
}

.advanced-tuning-toggle:hover {
  color: var(--el-color-primary);
}
```

- [ ] **Step 8.8: 跑 vitest，預期 5 條 PASS**

```bash
cd ivy-frontend && npm test -- src/components/settings/__tests__/SettingsUsersTab.test.ts
```

預期：5 條 PASS。若 fail，依錯誤訊息調整 selector 或 wait 邏輯。

---

### Task 9: 跑全套 frontend 驗證

- [ ] **Step 9.1: 跑全套 vitest**

```bash
cd ivy-frontend && npm test
```

預期：所有測試 PASS（含新 5 條），零回歸。

- [ ] **Step 9.2: 跑 typecheck**

```bash
cd ivy-frontend && npm run typecheck
```

預期：0 error。常見漏：`computed` 或 `nextTick` import 漏、`form` 參數型別不明。

- [ ] **Step 9.3: 跑 build**

```bash
cd ivy-frontend && npm run build
```

預期：success。

---

### Task 10: 手動驗收（dev server）

- [ ] **Step 10.1: 啟動 workspace dev server**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
```

開 http://localhost:5173，用 admin 帳號登入。

- [ ] **Step 10.2: 走 spec §Rollout 驗收清單**

依序確認：

- [ ] 設定 → 使用者管理 → 「新增帳號」按鈕
- [ ] dialog 內看到 7 卡片 4-列 grid（admin/principal/supervisor/hr/accountant/teacher/parent）
- [ ] parent 卡片半透明、cursor 變 not-allowed
- [ ] 點 principal → 卡片邊框變主色、進階微調折疊、badge 顯示「預設」
- [ ] 點開「▶ 進階微調」展開 checkbox 區，toggle 任一 checkbox → badge 變「已偏離 1 項」、出現「↻ 還原預設」
- [ ] 點「↻ 還原預設」→ checkbox 重置、badge 變「預設」、expander 自動折回
- [ ] 切到 teacher → 整個進階微調 form-item 隱藏
- [ ] 編輯既有 supervisor 帳號 → dialog 開啟時 expander 折疊（因 supervisor 預設等於 user）
- [ ] 編輯已偏離的 user → dialog 開啟時 expander 自動展開且顯示「已偏離 N 項」
- [ ] 建立 principal 帳號儲存成功
- [ ] 列表顯示新 principal 帳號的「預設」tag

任一步驟異常，停下回頭 debug。

---

### Task 11: frontend commit

- [ ] **Step 11.1: git status 確認改檔範圍**

```bash
cd ivy-frontend && git status
```

應只有：
- `src/components/settings/SettingsUsersTab.vue`
- `src/components/settings/__tests__/SettingsUsersTab.test.ts`

- [ ] **Step 11.2: commit**

```bash
cd ivy-frontend
git add src/components/settings/SettingsUsersTab.vue src/components/settings/__tests__/SettingsUsersTab.test.ts
git commit -m "$(cat <<'EOF'
feat(settings): redesign user dialog as role-card grid + advanced-tuning expander

- 7 角色卡片 4-列 grid 取代下拉選單（含 emoji 圖示、中文 label、描述、權限數 tag）
- 進階微調 expander 包住既有 8 群 checkbox UI，預設折疊
- 偏離預設時 badge 顯示「已偏離 N 項」並出現「↻ 還原預設」按鈕
- 編輯模式 dialog 開啟自動依偏離數決定 expander 初始狀態
- parent 卡片 disabled（管理端不可建家長帳號，請走家長端 LIFF）

依賴 backend role descriptions API（commit <backend-sha>）

Spec: ivy-backend/docs/superpowers/specs/2026-05-25-permission-role-library-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

把 `<backend-sha>` 替換為 Phase A Task 6 的實際 commit short SHA。

---

## 驗收完成

整體驗收清單對齊 spec §Rollout：

- [ ] 後端 pytest 全綠（含新 9 條 unit test + 2 條 endpoint test）
- [ ] `GET /api/permissions` response 含 7 個 role 且每個含 description
- [ ] 前端 vitest 全綠（含新 5 條）
- [ ] 前端 typecheck + build 零錯
- [ ] dev server 手動驗收清單全勾
- [ ] 既有 admin/hr/supervisor/teacher 帳號編輯沒有行為變化（向後相容）

整個 plan 約 1.5 工作日（後端 0.5、前端 1.0）。回滾：兩 commit 各自 revert，零資料風險。

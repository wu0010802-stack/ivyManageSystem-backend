# 預設角色庫 (a) — Principal/Accountant 擴充與 SettingsUsersTab UX 重組

- 起草日期：2026-05-25
- 範圍：ivy-backend（`utils/permissions.py` / `api/auth.py` `GET /permissions` response / `tests/test_permissions.py`）+ ivy-frontend（`src/components/settings/SettingsUsersTab.vue` 重組、新 `SettingsUsersTab.test.ts`）
- 不在範圍：
  - 不動既有 admin/hr/supervisor/teacher/parent 五個 ROLE_TEMPLATES
  - 不做「家長代表 (parent_rep)」角色（定義未明，user defer）
  - 不做 DB-driven 自定義權限（屬 (b) 子專案，另開 spec）
  - 不寫資料 migration script（無 user 需要遷移；新角色純新增）
  - 不改 `require_permission` decorator、`has_permission` runtime、前端 `hasPermission` 任何呼叫點

## 問題

CLAUDE.md #1 起初寫的「Permission 位元 > 32-bit 需 BigInt」「bit 已到 1<<62 邊界」這兩個前提**已不成立** —— 2026-05-21 `permtxt01` migration 已把後端 `Permission` 改成 `str Enum`、`User.permission_names` 改成 `ARRAY(Text)`、前端 `hasPermission` 改成字串 `includes`，64-bit 上限與 BigInt 心智負擔早已消除。

但 user 提出的兩個 UX 層級痛點仍在：

1. **新角色無從加**：要支援園所實際職位（園長、會計），需要重啟改 `ROLE_TEMPLATES` —— 但這次至少可以「**一次補齊**」常見角色，無需動 schema。
2. **SettingsUsersTab 主視角錯位**：現況是「下拉選角色 → 56 條 checkbox 平面展開」，視覺上 checkbox 主導、角色退為次要選項。對 admin 新增帳號時要先在 56 條中辨認哪些屬於哪個角色，認知負擔高。

本 spec 解 (a)「預設角色庫 UX 改進」：擴充 2 個常見角色（principal、accountant）+ 把 SettingsUsersTab 重組為「角色卡片為主、checkbox 退為進階微調 expander」。

(b)「DB-driven 自定義權限」（admin runtime 加新權限名稱）為獨立子專案，待 (a) 收價值後另開 spec。

## 設計概要

1. **後端**：`utils/permissions.py` ROLE_TEMPLATES 新增 `principal` / `accountant`、ROLE_LABELS 新增中文 label、新增 `ROLE_DESCRIPTIONS` 字典；`api/auth.py` `GET /permissions` response 的 roles 物件擴充每項加 `description` 欄位。
2. **前端**：`SettingsUsersTab.vue` 新增/編輯 dialog 改為「7 角色卡片 grid + 折疊進階微調 expander」；既有 `onRoleChange` / `togglePermission` / `isUsingDefaultPermissions` 等邏輯完全保留。
3. **測試**：後端 pytest 5 條驗 ROLE_TEMPLATES 內容與 endpoint response；前端 vitest 5 條驗角色卡片互動、expander 自動展開、偏離 badge。
4. **Rollout**：純 Python + Vue code change，零 DB schema 變動；後端 PR 先合、前端 PR 後合。

> 起手 brainstorm 時 user 原本以「Permission 64-bit ceiling」為前提提兩個方案；探索後發現該前提已在 2026-05-21 解掉，重新校準為「純 UX + 角色擴充」，並把「自定義權限」拆出為 (b) 另開 spec。

## 角色定義

### principal（園長）

對標現行 `supervisor`（教務主任）加上「薪資審視 + 稽核紀錄 + 政府報表匯出」，**不含**帳號/系統設定、**不含** SALARY_WRITE。

```python
"principal": [
    *ROLE_TEMPLATES["supervisor"],  # 43 條，明示衍生
    Permission.SALARY_READ.value,        # 園長能查薪資但不能改
    Permission.AUDIT_LOGS.value,         # 園長能查稽核紀錄
    Permission.GOV_REPORTS_EXPORT.value, # 園長能匯出政府報表
],
```

**設計取捨**：
- 不含 SALARY_WRITE：薪資發放走 hr/accountant，職責分離。實務上若某園長即會計，可以個人 `permission_names` override 加 SALARY_WRITE。
- 不含 USER_MANAGEMENT_*、SETTINGS_*：帳號與系統設定僅 admin 能動。
- 含 AUDIT_LOGS：園長要查誰改過什麼。
- 含 GOV_REPORTS_EXPORT：園長簽核並對外送件。

權限總數：46 條（supervisor 43 + 3）。

### accountant（會計）

純財務，**完全不含** EMPLOYEES_WRITE / 考勤 / 請假 / 加班 / 學生 / 招生：

```python
"accountant": [
    Permission.DASHBOARD.value,
    Permission.REPORTS.value,
    Permission.GOV_REPORTS_VIEW.value,
    Permission.EMPLOYEES_READ.value,        # 要看誰可申報薪資
    Permission.SALARY_READ.value,
    Permission.SALARY_WRITE.value,
    Permission.FEES_READ.value,
    Permission.FEES_WRITE.value,
    Permission.VENDOR_PAYMENT_READ.value,
    Permission.VENDOR_PAYMENT_WRITE.value,
    Permission.YEAR_END_READ.value,
    Permission.YEAR_END_WRITE.value,
    Permission.APPRAISAL_ACCOUNTING.value,  # 核考核獎金數字
],
```

**設計取捨**：
- 含 EMPLOYEES_READ 不含 WRITE：會計只讀員工名單（決定誰可申報薪資），不能改基本資料/職位/到職日。
- 不含 GOV_REPORTS_EXPORT：政府報表匯出對外送件由 hr/principal/admin 行使，會計只看不送（避免「會計同時製單與送件」）。
- 不含 YEAR_END_FINALIZE：會計能編輯/匯出，但「核定」屬 supervisor/principal 簽核權，配合既有 supervisor ROLE_TEMPLATE。

權限總數：13 條。

### 既有角色完全不動（向後相容承諾）

`admin / hr / supervisor / teacher / parent` 五個 ROLE_TEMPLATES 一字不動。零既有 user 受影響、零資料 backfill 需要。

| 角色 | 現權限數 | 變動 |
|---|---|---|
| admin | `['*']` | 不動 |
| hr | 24 | 不動（仍含薪資+廠商付款+員工寫入；前端說明文字可標「兼採購行政」） |
| supervisor | 43 | 不動 |
| teacher | 13 | 不動 |
| parent | 0 | 不動 |

### 7 角色描述字典（給 UI 卡片用）

```python
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

每個 ROLE_TEMPLATES key 都必須有對應 ROLE_DESCRIPTIONS entry（pytest 斷言完整性）。

## API 變更（`GET /permissions`）

現行 response 形如：

```json
{
  "permissions": { "EMPLOYEES_READ": { "label": "員工檢視", "value": "EMPLOYEES_READ" }, ... },
  "groups": [ { "name": "員工管理", "permissions": [...], "split_permissions": [...] }, ... ],
  "roles": {
    "admin":      { "label": "系統管理員", "permissions": ["*"] },
    "supervisor": { "label": "主管",     "permissions": [...] },
    ...
  }
}
```

擴充後 `roles[*]` 加 `description` 欄位：

```json
"roles": {
  "admin":      { "label": "系統管理員", "description": "唯一能改帳號、系統設定", "permissions": ["*"] },
  "principal":  { "label": "園長",     "description": "業務全包 + 薪資審視，不動帳號", "permissions": [...] },
  "accountant": { "label": "會計",     "description": "純財務（薪資/學費/廠商/年終）", "permissions": [...] },
  ...
}
```

**向後相容**：欄位純新增、key 純新增、未動 permissions/groups/labels 區塊。舊前端讀新 response 不會壞（多出 description 欄位忽略）；新前端讀舊 response 會少 description（fallback 顯示 label）。

## 前端 UX 重組（`SettingsUsersTab.vue`）

### Dialog 結構

新增/編輯使用者 dialog 拆三區（上→下）：

1. **基本資料區**（既有）：員工選擇 / 帳號 / 密碼（新增時才有）
2. **角色卡片區**（新）：7 個角色卡片 4-列 grid，點選即觸發 `onRoleChange`
3. **進階微調 expander**（新）：折疊狀態包住既有 8 群 checkbox UI

### 角色卡片 spec

```
┌──────────────────────┐
│ 👑  系統管理員      │  ← 圖示 emoji + label
│                     │
│ 唯一能改帳號、      │  ← description（兩行截斷）
│ 系統設定           │
│                     │
│ ┌────┐             │
│ │全部│             │  ← 權限數 el-tag（admin 顯示「全部」）
│ └────┘             │
└──────────────────────┘
```

- **元件**：`<el-card shadow="hover">` + 點選態 `class="role-card--active"`（border 變主色）
- **圖示**：emoji（admin 👑 / principal 🏫 / supervisor 📋 / hr 💼 / accountant 💰 / teacher 📚 / parent 👨‍👩‍👧）—— 避免引入 icon 套件
- **權限數**：`role.permissions.length`（admin `['*']` 顯示「全部」字樣）
- **layout**：CSS Grid `grid-template-columns: repeat(4, 1fr)`，min-width 700px dialog 對齊
- **parent 卡片 disabled**：`cursor: not-allowed`、半透明、tooltip「家長帳號請從家長端 LIFF 綁定」
- **teacher 卡片選了**：自動隱藏整個進階微調 expander（沿用既有 `v-if="form.role !== 'teacher'"` 規則）

### 進階微調 expander spec

- **折疊狀態列**：`▶ 進階微調 ⓘ 預設`（藍色 info badge）/ `▶ 進階微調 ⚠ 已偏離 N 項`（黃色 warning badge）+ 偏離時右側出現「↻ 還原預設」link button
- **展開狀態列**：`▼ 進階微調`（同上 badge）+ 既有 `[全選] [清除]` 兩按鈕 + 8 群 checkbox UI（一字不改）
- **expanded state**：獨立 `ref<boolean>`（不從偏離數推導），讓使用者可手動點頭部折疊已偏離狀態。
- **自動展開時機**：
  - 開啟 dialog 時：`expanded = deviationCount > 0`（編輯模式 user 已偏離則直接展開）
  - 使用者 toggle checkbox 觸發 `togglePermission`：若該動作後 `deviationCount > 0`，強制 `expanded = true`（避免折疊狀態下偷偷改）
  - 點角色卡片觸發 `onRoleChange`：`expanded = false`（套用預設後折回）
  - 點「↻ 還原預設」：`expanded = false`

### 偏離計算

```ts
const deviationCount = computed(() => {
  if (form.permission_names.includes('*')) {
    // wildcard 對應 admin 預設視為 0 偏離；對應其他角色視為「全部偏離」
    const tpl = permissionDefinition.value.roles[form.role]?.permissions ?? []
    return tpl.includes('*') ? 0 : Object.keys(permissionDefinition.value.permissions).length
  }
  const tpl = permissionDefinition.value.roles[form.role]?.permissions ?? []
  const tplSet = new Set(tpl)
  const formSet = new Set(form.permission_names)
  const added = form.permission_names.filter(p => !tplSet.has(p)).length
  const removed = tpl.filter(p => !formSet.has(p)).length
  return added + removed
})
```

### 沿用既有邏輯（不重寫）

- `onRoleChange(form)`：點卡片即呼叫，灌入 role template permissions
- `togglePermission(form, perm)`：checkbox 變動，沿用 wildcard→展開 logic
- `isUsingDefaultPermissions(form)`：判斷是否偏離，用於 expander badge 與 row tag
- `permissionDefinition` `onMounted` 一次 fetch，含新增 description 欄位
- `getRoleTagType` 加 `principal: 'success'` / `accountant: 'warning'` 映射

### Row 顯示維持原樣

表格列的「全部 / 預設 / 自訂」三 tag 邏輯不動（既有 `isUsingRoleDefault` 已正確對所有 role 適用）。

## 測試

### 後端 (`ivy-backend/tests/test_permissions.py` 擴充)

```python
def test_role_templates_principal_inherits_supervisor():
    """principal 必須包含 supervisor 全部 + 3 條額外"""
    sup_set = set(ROLE_TEMPLATES["supervisor"])
    pri_set = set(ROLE_TEMPLATES["principal"])
    assert sup_set.issubset(pri_set)
    extras = pri_set - sup_set
    assert extras == {
        Permission.SALARY_READ.value,
        Permission.AUDIT_LOGS.value,
        Permission.GOV_REPORTS_EXPORT.value,
    }

def test_role_templates_principal_excludes_salary_write():
    """principal 不可含 SALARY_WRITE / USER_MANAGEMENT_* / SETTINGS_*"""
    pri = ROLE_TEMPLATES["principal"]
    assert Permission.SALARY_WRITE.value not in pri
    assert Permission.USER_MANAGEMENT_READ.value not in pri
    assert Permission.USER_MANAGEMENT_WRITE.value not in pri
    assert Permission.SETTINGS_READ.value not in pri
    assert Permission.SETTINGS_WRITE.value not in pri

def test_role_templates_accountant_pure_finance():
    """accountant 只含財務相關 + EMPLOYEES_READ；不可含 EMPLOYEES_WRITE / 考勤 / 學生 / 招生"""
    acc = set(ROLE_TEMPLATES["accountant"])
    forbidden = {
        Permission.EMPLOYEES_WRITE.value,
        Permission.ATTENDANCE_READ.value,
        Permission.STUDENTS_READ.value,
        Permission.RECRUITMENT_READ.value,
        Permission.GOV_REPORTS_EXPORT.value,
        Permission.YEAR_END_FINALIZE.value,
    }
    assert forbidden.isdisjoint(acc)
    assert Permission.EMPLOYEES_READ.value in acc
    assert Permission.SALARY_WRITE.value in acc
    assert Permission.VENDOR_PAYMENT_WRITE.value in acc

def test_role_descriptions_complete():
    """每個 ROLE_TEMPLATES key 都有對應 ROLE_DESCRIPTIONS"""
    assert set(ROLE_TEMPLATES.keys()) == set(ROLE_DESCRIPTIONS.keys())

def test_get_permissions_returns_role_descriptions(client, admin_token):
    """endpoint integration: roles[*] 含 description 欄位"""
    response = client.get("/api/permissions", headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200
    roles = response.json()["roles"]
    for role_key in ("admin", "principal", "supervisor", "hr", "accountant", "teacher", "parent"):
        assert role_key in roles
        assert "description" in roles[role_key]
        assert roles[role_key]["description"]  # 非空
```

### 前端 (`ivy-frontend/src/components/settings/SettingsUsersTab.test.ts` 新增)

```ts
describe('SettingsUsersTab role card UX', () => {
  it('renders 7 role cards in new-user dialog', async () => {
    // mount dialog, mock GET /permissions
    // assert 7 .role-card elements
  })

  it('clicking principal card fills form.permission_names with template', async () => {
    // click .role-card[data-role="principal"]
    // assert form.permission_names === ROLE_TEMPLATES.principal
    // assert expander collapsed, badge shows "預設"
  })

  it('toggling a checkbox auto-expands the expander and shows deviation badge', async () => {
    // select principal → expander collapsed
    // toggle one EMPLOYEES_READ checkbox off
    // assert expander expanded, badge shows "已偏離 1 項"
    // assert "還原預設" button visible
  })

  it('clicking 還原預設 resets to role template', async () => {
    // after deviation, click 還原預設
    // assert badge shows "預設", "還原預設" button hidden
  })

  it('parent role card is disabled with tooltip', async () => {
    // assert .role-card[data-role="parent"] has class is-disabled
    // assert tooltip "家長帳號請從家長端 LIFF 綁定"
  })
})
```

## Rollout

### Migration

**零 DB schema 變動。** ROLE_TEMPLATES 純新增 key、ROLE_LABELS / ROLE_DESCRIPTIONS 純新增 entry，既有 users.permission_names 完全不動（NULL 仍解析為原 role 預設、`['*']` 仍解析為全部）。

### PR 順序

1. **後端 PR**：`utils/permissions.py` ROLE_TEMPLATES + ROLE_LABELS + ROLE_DESCRIPTIONS + `api/auth.py` `GET /permissions` response include description + 5 條 pytest。
2. **前端 PR**：`SettingsUsersTab.vue` 卡片 + expander 重組 + 5 條 vitest。依賴後端 API 已 deploy。

兩 PR 各自一個 conventional commit，按 CLAUDE.md SOP 分開（後端先合、前端後合）。

### 驗收清單

- [ ] 後端 pytest 全綠（含新 5 條）
- [ ] `GET /api/permissions` 回傳 7 個 roles 且每項含 description
- [ ] 前端 vitest 全綠（含新 5 條）
- [ ] 前端 typecheck + build 零錯
- [ ] 開新增帳號 dialog 看到 7 卡片 4-列 grid、parent 卡片 disabled
- [ ] 選 principal → expander 折疊、badge「預設」
- [ ] toggle 一個 checkbox → expander 自動展開、badge「已偏離 1 項」、出現「還原預設」
- [ ] 點還原預設 → checkbox 重置、badge 變「預設」
- [ ] 選 teacher → expander 整段隱藏
- [ ] 既有 admin/hr/supervisor 帳號編輯沒有任何行為變化（向後相容）

### 回滾

純加角色（既有 admin/hr/supervisor/teacher/parent 未改），revert 兩 commit 即回原狀。**零資料風險、零 schema 風險。**

## 後續延伸（明確不在本 spec）

- **(b) DB-driven 自定義權限**：admin 在 UI 新增 permission_code / role_code，建 `permission_definitions` 與 `roles` 兩表，`require_permission` 查表，前端 hasPermission 吃動態清單。需 freeze 期 + migration backfill + 30+ router 同步。等 (a) 收價值後另開 spec。
- **「家長代表 (parent_rep)」角色**：user 提及但定義未明（家長會代表？觀察員？跨班觀察者？），釐清語意後可在 (a) 之後 inline 加入 ROLE_TEMPLATES。
- **角色 inline 移除**：UI 加「停用角色」開關（保留歷史不刪）—— 目前 7 角色全部都會被用到，無需求。

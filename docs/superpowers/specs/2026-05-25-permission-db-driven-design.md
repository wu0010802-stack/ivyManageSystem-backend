# DB-Driven 自訂權限/角色 (b) 設計

- 起草日期：2026-05-25
- 範圍：ivy-backend（新建 `models/permission_models.py`、`api/permissions_admin.py`、`alembic/versions/...rolesdb01...`；改 `utils/permissions.py` runtime 從 DB 拉）+ ivy-frontend（新建 `src/components/settings/SettingsPermissionsTab.vue` + 對應測試 + 設定頁加 tab）
- **前置依賴**：(a) 子專案 `feat/permission-role-library-2026-05-25-*`（已 merge local main，HEAD `70846c0` / `0346d3ac`），含 `ROLE_TEMPLATES["principal"]` `ROLE_TEMPLATES["accountant"]` `ROLE_DESCRIPTIONS`
- 不在範圍：
  - 不改 `require_permission` decorator runtime 路徑（hot path 仍純字串 `in` 比對，零 DB 查詢）
  - 不為自訂權限自動產生 router 守衛（自訂權限對 endpoint 無作用，僅角色組合 + 前端 v-if）
  - 不做版本歷史 / role 變更 audit trail（mutation 走既有 audit_logs middleware 即可）
  - 不做 `users.role` → `roles.id` FK normalization（仍保留 string 對應 `roles.code`）
  - 不改 `Permission` enum / 不改既有 router `@require_permission(Permission.XXX)` 引用

## 問題

(a) 預設角色庫已落地 principal/accountant 兩個角色 + 卡片 UX，但**新增角色/權限仍需改 Python 程式碼 + redeploy**：

1. **`ROLE_TEMPLATES` 是 in-code dict**（`utils/permissions.py:200`）：admin 想加「兼會計的園長」「只看招生不能轉換的招生主任」等組合角色，必須等開發者改 dict + alembic + redeploy。
2. **`PERMISSION_GROUPS` 是 in-code list**（`utils/permissions.py:426`）：分組結構不可動態調整。
3. **無 `roles` / `permission_definitions` 兩個 table**：(a) `ROLE_DESCRIPTIONS` 也是 in-code dict，無 audit / 無 admin self-serve。

本 spec 解 (b)「DB-driven 自訂權限/角色」：把 `ROLE_TEMPLATES` / `ROLE_DESCRIPTIONS` / `PERMISSION_GROUPS` / `PERMISSION_LABELS` 搬到 DB，admin UI runtime 新增/編輯權限定義與角色，零 redeploy。

## 設計概要

1. **2 個 DB table**：`permission_definitions`（取代 `PERMISSION_LABELS` + `PERMISSION_GROUPS`）、`roles`（取代 `ROLE_TEMPLATES` + `ROLE_DESCRIPTIONS`）。
2. **Seed migration `rolesdb01`**：把 in-code dict 全 seed 進兩 table 標 `is_core=true`，admin 不可刪 / 不可改 code。
3. **`get_permissions_definition()` 改從 DB 拉**：response shape 對齊現有 + 加 `is_core` 欄位（前端用以決定刪除按鈕 disable）。
4. **新 admin endpoint** `/api/permissions/definitions` 與 `/api/roles` 兩組 CRUD，守衛走新權限 `ROLES_MANAGE`（admin `['*']` 自動含）。
5. **新前端 `SettingsPermissionsTab.vue`**：兩個 sub-tab「角色管理」與「權限定義」，is_core 列 disabled「刪除」並警示。
6. **既有 in-code dict 保留**：runtime 不再讀，但仍作為 alembic seed 來源、IDE 自動完成、core 凍結快照（Python `Permission` enum 不動）。
7. **Hot path 零變動**：`require_permission` / `has_permission` 仍純字串 `in` 比對，零 DB hit。
8. **Cascade**：刪 `permission_definitions` 連動清 `roles.permissions[]` 與 `users.permission_names[]`；刪 `roles` 若仍有 user 引用拒 409。

> brainstorming 階段校準兩個 user 前提：(1) require_permission 已接受字串、permission_names 寫入無 whitelist（admin 可塞任意字串但 router 不認）；(2) 「自訂權限名對 router 無用」是根本限制 —— 自訂權限只能用於角色組合 + 前端 v-if，UX 須明確警示。

## 資料模型

### `permission_definitions` table

```sql
CREATE TABLE permission_definitions (
    id BIGSERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    description TEXT,
    group_name TEXT NOT NULL DEFAULT '自訂',
    is_core BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_permission_definitions_group ON permission_definitions(group_name);
```

| 欄位 | 用途 |
|---|---|
| `code` | 唯一識別字串，例 `"EMPLOYEES_READ"` / `"PARENT_SURVEY_WRITE"`，對應 `users.permission_names[]` 元素 |
| `label` | 中文顯示名（如「員工管理 (檢視)」），admin 可改 |
| `description` | 詳細說明 |
| `group_name` | 前端分組（既有 8 群 + admin 自訂分組）；對應 `PERMISSION_GROUPS` |
| `is_core` | core 為 56 個 Permission enum + ROLES_MANAGE = 57 條；admin 可改 label/description/group_name 但不可改 code、不可刪 |

### `roles` table

```sql
CREATE TABLE roles (
    id BIGSERIAL PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    label TEXT NOT NULL,
    description TEXT,
    permissions TEXT[] NOT NULL DEFAULT '{}',
    is_core BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);
```

| 欄位 | 用途 |
|---|---|
| `code` | 對應 `users.role` 字串，例 `"admin"` / `"custom_principal_w_salary"` |
| `label` | 中文顯示名（如「兼會計的園長」） |
| `description` | 適用對象 / 一句話 |
| `permissions` | 角色預設權限 `TEXT[]`；`['*']` = wildcard；與 `users.permission_names` 同 shape |
| `is_core` | core 為 (a) 之 7 個 ROLE_TEMPLATES（admin/principal/supervisor/hr/accountant/teacher/parent）；admin 可改 label/description 但不可改 code、不可改 permissions、不可刪 |

### 為何 `roles.permissions` 用 `TEXT[]` 而非 join table

- **與 `users.permission_names` 同 shape**：runtime helper 共用、查詢一致
- **零 JOIN 開銷**：role 載入單筆即拿全部 permissions
- **取捨**：無 FK 自動 cascade — 刪 `permission_definitions` 須 app 層手動 `UPDATE ... array_remove`（§6 處理）

### `users.role` 與 `roles.code` 的關聯

保留現有 string-link（不導入 FK normalization）：
- `users.role` 字串對應 `roles.code`，runtime resolve permissions 時 `SELECT roles.permissions FROM roles WHERE code = users.role`
- 刪 role 時若有 user `role = code` 拒 409（避免孤兒）
- 改 role.code 永遠拒（is_core 與 custom 都不可改 code，避免引用斷裂）

## 新權限定義：`ROLES_MANAGE`

加入 56 條 Permission enum 之外的第 57 條：

```python
# utils/permissions.py
class Permission(str, Enum):
    # ... 既有 56 條 ...
    ROLES_MANAGE = "ROLES_MANAGE"
```

**用途**：守衛 7 個 admin endpoint（角色 + 權限定義 CRUD）。

**seed**：alembic 同時 seed 進 `permission_definitions` (is_core=true, label="角色與權限管理", group_name="系統") + 加進 admin role（已 `['*']` 自動含 — 仍顯式列出讓資料一致）。

## API endpoint

新檔 `api/permissions_admin.py`，全部走 `require_permission(Permission.ROLES_MANAGE)` 守衛：

| Method | Path | 用途 / 行為 |
|---|---|---|
| `POST` | `/api/permissions/definitions` | 新增 permission_definition；body: `{code, label, description?, group_name?}`；自動 `is_core=false`；重複 code 422 |
| `PUT` | `/api/permissions/definitions/{code}` | 改 label/description/group_name；code 不可改；is_core=true 允許改 label/description/group_name |
| `DELETE` | `/api/permissions/definitions/{code}` | 刪自訂權限；is_core=true 拒 409；cascade 清 roles + users |
| `POST` | `/api/roles` | 新增角色；body: `{code, label, description?, permissions[]}`；自動 `is_core=false`；驗證 permissions[] 內每個 code 都存在於 permission_definitions（含 `*` wildcard） |
| `PUT` | `/api/roles/{code}` | 改 label/description/permissions[]；code 不可改；is_core=true 鎖 permissions[] 只允許改 label/description；permissions[] 驗證同 POST |
| `DELETE` | `/api/roles/{code}` | 刪自訂角色；is_core=true 拒 409；有 user 引用拒 409（提示先改 user role） |

既有 `GET /api/permissions` 不動 path 但實作改為從 DB 拉（§4），response 新增 `is_core` 欄位於每個 role 與 permission，前端用以決定刪除按鈕 disable。

### Schema (Pydantic)

```python
class PermissionDefinitionIn(BaseModel):
    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]*$", max_length=64)
    label: str = Field(..., min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    group_name: str = Field("自訂", max_length=40)

class PermissionDefinitionUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    group_name: Optional[str] = Field(None, max_length=40)

class RoleIn(BaseModel):
    code: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    label: str = Field(..., min_length=1, max_length=40)
    description: Optional[str] = Field(None, max_length=200)
    permissions: List[str] = Field(default_factory=list)

class RoleUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=40)
    description: Optional[str] = Field(None, max_length=200)
    permissions: Optional[List[str]] = None
```

**約束**：
- permission `code` 規則：uppercase + digit + underscore（對齊既有 Permission enum 命名）
- role `code` 規則：lowercase + digit + underscore（對齊既有 admin/hr/supervisor 命名）

## Runtime 改動（`utils/permissions.py`）

### `get_permissions_definition(session)` 改從 DB 拉

```python
def get_permissions_definition(session: Session) -> Dict:
    """從 DB 拉權限定義（取代 in-code dict）。"""
    perm_defs = session.query(PermissionDefinition).order_by(PermissionDefinition.group_name, PermissionDefinition.code).all()
    role_defs = session.query(Role).order_by(Role.is_core.desc(), Role.code).all()

    permissions = {
        p.code: {"value": p.code, "label": p.label, "is_core": p.is_core}
        for p in perm_defs
    }
    groups = _build_groups_from_definitions(perm_defs)  # 動態組分組，含 split_permissions 對 _READ/_WRITE 配對
    roles = {
        r.code: {
            "label": r.label,
            "description": r.description or "",
            "permissions": list(r.permissions),
            "is_core": r.is_core,
        }
        for r in role_defs
    }
    return {
        "permissions": permissions,
        "groups": groups,
        "roles": roles,
        "split_modules": SPLIT_MODULES,  # 暫保 in-code（read/write 配對為靜態邏輯）
    }
```

### `get_role_default_permissions(session, role_code)` 改從 DB 拉

```python
def get_role_default_permissions(session: Session, role_code: str) -> List[str]:
    role = session.query(Role).filter_by(code=role_code).first()
    if role is None:
        # fallback to teacher（既有行為）
        teacher_role = session.query(Role).filter_by(code="teacher").first()
        return list(teacher_role.permissions) if teacher_role else []
    return list(role.permissions)
```

**影響面**：`api/auth.py` 數處呼叫 `get_role_default_permissions(role)` 改為 `get_role_default_permissions(session, role)`（session 從 dependency 取）。

### Hot path 不變

- `has_permission(user_perms, name)` 純字串 `in` 比對，零 DB hit
- `require_permission(permission)` decorator 不動
- JWT 內仍夾 `permission_names`，user 登入時一次 resolve 寫入 token（既有行為）

### In-code dict 命運

`ROLE_TEMPLATES` / `ROLE_DESCRIPTIONS` / `PERMISSION_GROUPS` / `PERMISSION_LABELS` 保留，但加 module-level docstring：

```python
# 以下 in-code dict 在 rolesdb01 之後**僅供 alembic seed 與測試**使用，runtime 從 DB 拉。
# 新增 in-code 角色/權限定義無作用——須走 admin UI 或直接 INSERT 進 DB。
```

`Permission` enum 不動 — router 仍可用 `@require_permission(Permission.EMPLOYEES_READ)`（IDE 自動完成、type safety）。

## 前端 UX

### 新檔 `SettingsPermissionsTab.vue`

新 sub-tab「權限管理」加入 SettingsView，與 SettingsUsersTab 並列。內含 `<el-tabs>` 兩個區：

**Tab A：角色管理**
- `<el-table>` 列出全部 roles：
  - 欄位：`code` / `label` / `description` / 權限數 tag / `is_core` badge / 操作（編輯 / 刪除）
  - `is_core=true` 列：刪除按鈕 disabled + tooltip「核心角色不可刪除」；code 欄與 permissions 不可編輯但 label/description 可改
- 「新增角色」開 dialog：
  - 上方：code / label / description 三輸入欄
  - 下方：reuse (a) `SettingsUsersTab` 的進階微調 expander UI（直接渲染 PERMISSION_GROUPS checkbox 給 admin 勾）
  - 提交 `POST /api/roles`

**Tab B：權限定義**
- `<el-table>` 列出全部 permission_definitions：
  - 欄位：`code` / `label` / `description` / `group_name` / `is_core` badge / 操作
  - is_core 同樣 disable 刪除
- **頂部 warning callout**（`<el-alert type="warning" :closable="false">`）：
  > ⚠ 自訂權限僅可用於「角色組合」與「前端條件渲染」；後端 API 守衛仍是 hardcoded enum。新增權限不會自動為任何 endpoint 加守衛 — 若需後端守衛新模組，請開 issue 走開發流程。
- 「新增權限」開 dialog：code / label / description / group_name（下拉，既有 8 群 + 自訂可輸入新分組名）

### `SettingsUsersTab` 自動受益

(a) 已實作的「7 角色卡片 + expander」UI 是 driven by `permissionDefinition.value.roles`（從 API 動態渲染），DB 多 1 個自訂角色 → SettingsUsersTab 多 1 張卡片，**零 SettingsUsersTab 改動**。

### 設定頁 tab 入口

`SettingsView.vue`（或對應檔）加 tab：
```vue
<el-tab-pane label="權限管理" name="permissions">
  <SettingsPermissionsTab />
</el-tab-pane>
```

`SETTINGS_WRITE` 守衛該 tab 顯示（既有 router rule）；ROLES_MANAGE 守衛 tab 內所有 mutation。

## Cascade / 資料完整性

### 刪 permission_definition

```python
@router.delete("/permissions/definitions/{code}")
def delete_permission_definition(code: str, session: Session = Depends(get_session_dep), _: dict = Depends(require_permission(Permission.ROLES_MANAGE))):
    pd = session.query(PermissionDefinition).filter_by(code=code).first()
    if not pd:
        raise HTTPException(404, "權限定義不存在")
    if pd.is_core:
        raise HTTPException(409, "核心權限不可刪除")
    
    # Cascade clean：清掉所有 roles 與 users 引用此 code
    session.execute(text("UPDATE roles SET permissions = array_remove(permissions, :c), updated_at = NOW() WHERE :c = ANY(permissions)"), {"c": code})
    session.execute(text("UPDATE users SET permission_names = array_remove(permission_names, :c) WHERE :c = ANY(permission_names)"), {"c": code})
    session.delete(pd)
    session.commit()
    
    # token_version bump：因 users.permission_names 改動，所有 user 既有 token 應失效
    session.execute(text("UPDATE users SET token_version = token_version + 1 WHERE EXISTS (SELECT 1 FROM jsonb_array_elements_text(to_jsonb(permission_names)) e WHERE e.value = :c)"), {"c": code})
    # 注意：上句僅針對曾持有此 perm 的 user bump，但 array_remove 已先跑 → 改順序：先 bump 再 array_remove
    return {"ok": True}
```

實作順序：**先 bump token_version → 再 array_remove permissions → 最後 delete pd**（避免 array_remove 後找不到誰該 bump）。

### 刪 role

```python
@router.delete("/roles/{code}")
def delete_role(code: str, session: Session = Depends(get_session_dep), _: dict = Depends(require_permission(Permission.ROLES_MANAGE))):
    role = session.query(Role).filter_by(code=code).first()
    if not role:
        raise HTTPException(404, "角色不存在")
    if role.is_core:
        raise HTTPException(409, "核心角色不可刪除")
    
    user_count = session.query(User).filter_by(role=code).count()
    if user_count > 0:
        raise HTTPException(409, f"尚有 {user_count} 個帳號使用此角色，請先變更帳號角色再刪除")
    
    session.delete(role)
    session.commit()
    return {"ok": True}
```

### 改 role.permissions

```python
@router.put("/roles/{code}")
def update_role(code: str, payload: RoleUpdate, session, ...):
    role = session.query(Role).filter_by(code=code).first()
    if not role: raise HTTPException(404)
    
    if payload.permissions is not None:
        if role.is_core:
            raise HTTPException(409, "核心角色的權限不可修改（僅可改 label/description）")
        # Validate every code exists
        invalid = [c for c in payload.permissions if c != "*" and not session.query(PermissionDefinition.code).filter_by(code=c).first()]
        if invalid:
            raise HTTPException(422, f"以下 permission code 不存在：{invalid}")
        role.permissions = payload.permissions
    
    if payload.label is not None: role.label = payload.label
    if payload.description is not None: role.description = payload.description
    
    # 改 role.permissions → 所有 users.role == code 的 user 須 token_version bump
    session.execute(text("UPDATE users SET token_version = token_version + 1 WHERE role = :c AND permission_names IS NULL"), {"c": code})
    session.commit()
```

只 bump `permission_names IS NULL`（依角色預設）的 user — 有 explicit override 的 user 不受 role 改動影響。

## Alembic Migration

新檔 `alembic/versions/20260525_rolesdb01_roles_and_permission_definitions.py`：

```python
"""roles and permission_definitions tables; seed from in-code dicts.

Revision ID: rolesdb01
Revises: <current_head>
Create Date: 2026-05-25
"""

revision = "rolesdb01"
down_revision = "<current_head>"  # auto-fill at generation


def upgrade():
    from utils.permissions import (
        PERMISSION_LABELS,
        PERMISSION_GROUPS,
        ROLE_TEMPLATES,
        ROLE_LABELS,
        ROLE_DESCRIPTIONS,
    )
    
    op.create_table(
        "permission_definitions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.Text, nullable=False, unique=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("group_name", sa.Text, nullable=False, server_default="自訂"),
        sa.Column("is_core", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_permission_definitions_group", "permission_definitions", ["group_name"])
    
    op.create_table(
        "roles",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.Text, nullable=False, unique=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("permissions", sa.dialects.postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("is_core", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP, nullable=False, server_default=sa.func.now()),
    )
    
    # Seed permission_definitions（57 條 is_core=true）
    conn = op.get_bind()
    
    # 反查 PERMISSION_GROUPS 找每個 code 屬於哪一群
    group_lookup = _build_code_to_group_lookup(PERMISSION_GROUPS)
    
    perm_rows = []
    for code, label in PERMISSION_LABELS.items():
        perm_rows.append({
            "code": code,
            "label": label,
            "description": None,
            "group_name": group_lookup.get(code, "其他"),
            "is_core": True,
        })
    # 加 ROLES_MANAGE（第 57 條）
    perm_rows.append({
        "code": "ROLES_MANAGE",
        "label": "角色與權限管理",
        "description": "新增/編輯/刪除自訂角色與權限定義",
        "group_name": "系統",
        "is_core": True,
    })
    conn.execute(sa.text("INSERT INTO permission_definitions (code, label, description, group_name, is_core) VALUES (:code, :label, :description, :group_name, :is_core)"), perm_rows)
    
    # Seed roles（7 條 is_core=true）
    role_rows = []
    for code, perms in ROLE_TEMPLATES.items():
        role_rows.append({
            "code": code,
            "label": ROLE_LABELS.get(code, code),
            "description": ROLE_DESCRIPTIONS.get(code, ""),
            "permissions": perms,
            "is_core": True,
        })
    conn.execute(sa.text("INSERT INTO roles (code, label, description, permissions, is_core) VALUES (:code, :label, :description, :permissions, :is_core)"), role_rows)


def downgrade():
    # 注意：自訂角色與自訂權限資料將丟失，emergency rollback 接受
    op.drop_table("roles")
    op.drop_index("ix_permission_definitions_group", "permission_definitions")
    op.drop_table("permission_definitions")
```

## 測試

### 後端 `tests/test_permissions_admin.py`（新檔，約 25 條）

**Seed 驗證（5 條）：**
- migration 跑完後 permission_definitions 含 57 條 is_core=true
- 每個 PERMISSION_LABELS key 都在 permission_definitions
- ROLES_MANAGE 存在且 is_core=true
- migration 跑完後 roles 含 7 條 is_core=true
- 7 條 roles 的 permissions 與 ROLE_TEMPLATES 完全一致

**Permission CRUD（7 條）：**
- POST /permissions/definitions 成功（is_core=false）
- POST 重複 code 422
- POST code 格式違反 pattern 422
- PUT is_core 改 label 200
- PUT is_core 改 code 拒（無此欄位即 422）
- DELETE is_core 拒 409
- DELETE 自訂 cascade 清 roles + users + token_version bump

**Role CRUD（9 條）：**
- POST /roles 成功
- POST 帶不存在的 permission code 422
- POST 重複 code 422
- POST code 格式違反 pattern 422
- PUT is_core role 改 permissions 拒 409
- PUT is_core role 改 label 200
- PUT 自訂 role 改 permissions 連動 bump user.token_version（permission_names IS NULL 才 bump）
- DELETE is_core role 拒 409
- DELETE 有 user 引用拒 409，無引用 200

**Endpoint 守衛（4 條）：**
- 無 ROLES_MANAGE 的 user call CRUD endpoint 403
- admin（`['*']`）call 所有 endpoint 200
- get_permissions_definition() 從 DB 回 7+N roles / 57+N permissions
- get_role_default_permissions() 從 DB 回 — fallback to teacher 當 role 不存在

### 前端 `SettingsPermissionsTab.test.ts`（新檔，約 8 條）

- 兩個 tab 都渲染
- is_core 角色 row 顯示 disabled 刪除按鈕 + tooltip
- 點「新增角色」開 dialog
- 新增權限 dialog 顯示 warning callout
- DELETE call 成功後表格刷新
- code 輸入欄 disable 在編輯既有項目（不論 is_core 或 custom）
- 改 is_core 角色的 permissions 區 disable
- 提交 permissions 含未知 code 顯示 422 訊息

## Rollout

### 順序

1. **後端 PR**：
   - alembic `rolesdb01`（建兩表 + seed 57 perm + 7 role）
   - `models/permission_models.py`（SQLAlchemy `PermissionDefinition` + `Role`）
   - `api/permissions_admin.py`（7 endpoint + ROLES_MANAGE 守衛）
   - `utils/permissions.py` runtime 改：`get_permissions_definition(session)` + `get_role_default_permissions(session, code)` 從 DB 拉
   - `Permission` enum 加 `ROLES_MANAGE`
   - `api/auth.py` 呼叫 `get_role_default_permissions` 處改傳 session
   - 25 條 pytest

2. **前端 PR**（依賴後端 deploy）：
   - `SettingsPermissionsTab.vue` 新檔
   - `SettingsView.vue` 加 tab entry
   - `src/api/permissions_admin.ts` 新檔（7 endpoint wrapper）
   - 8 條 vitest

### 驗收清單

- [ ] alembic upgrade rolesdb01 後 DB 兩表存在、57 permission / 7 role seed 完成
- [ ] 後端 25 條 pytest 全綠
- [ ] `GET /api/permissions` 回 7+N roles / 57+N permissions，每個含 is_core
- [ ] 無 ROLES_MANAGE 帳號打 CRUD 403
- [ ] admin UI 設定頁多「權限管理」tab，含「角色管理」+「權限定義」兩區
- [ ] 新增「兼會計的園長」角色（principal + SALARY_WRITE）成功
- [ ] 新角色立即出現在 SettingsUsersTab 卡片區（無需 redeploy）
- [ ] 新增自訂權限「PARENT_SURVEY_WRITE」+ warning callout 顯示
- [ ] 刪除自訂權限連動清 roles 與 users 內引用
- [ ] 嘗試刪除 is_core 角色/權限拒 409
- [ ] 嘗試刪除有 user 引用的角色拒 409
- [ ] 前端 typecheck + build + vitest 全綠

### 回滾

`alembic downgrade rolesdb01-1` + revert code commit：自訂角色與自訂權限資料丟失（emergency rollback 接受）。runtime 改動會 break — 若 prod 已 deploy 須同時 revert 兩 PR。

## 後續延伸（明確不在 (b) 範圍）

- **角色變更 audit trail**：用既有 audit_logs middleware 自動 capture mutation（不另設 role_change_history 表）
- **per-user override UX 加強**：在 SettingsUsersTab 顯示「此 user 之前角色是 X 已改成 Y，permission_names 是 explicit 還是 inherit」（屬 (a) 的 minor follow-up，非本 spec）
- **「自訂前端模組」**：admin 可自訂報表/dashboard 並掛自訂權限做過濾——這是另一個 sub-project
- **`users.role` FK normalization**：把 string-link 改成 `users.role_id INTEGER FK roles.id`——本 spec 不做，保留 string 對齊既有 schema

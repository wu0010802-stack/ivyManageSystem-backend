# Permission IntFlag → text[] 重構設計

- 起草日期：2026-05-21
- 範圍：ivy-backend（`utils/permissions.py` / `models/auth.py` / `alembic/` / 所有 router 權限守衛）+ ivy-frontend（`src/utils/auth.ts` / `src/constants/permissions.ts` / 所有 `hasPermission` 呼叫點）
- 不在範圍：parent 端（家長無 Permission 位元，恆為空）、ROLE_TEMPLATES 進 DB（保留 in-code dict）、多對多 user_permissions 表（不導入正規 RBAC）

## 問題

`utils/permissions.py:10` 的 `Permission(IntFlag)` 已用到 `1 << 62`（`VENDOR_PAYMENT_WRITE`），63 個 enum 值（bit 0-62）耗盡 PostgreSQL `bigint`（signed 64-bit）容量：

- `1 << 63` 在 signed bigint 是 `INT64_MIN`（負數），不能作為一般 permission 位元使用。
- DB column `users.permissions` 是 `BigInteger`，無從擴張。
- 現有 sentinel `permissions = -1` 表示「全部權限」，與位元語意混在同一欄。
- 前端 `≥ 1 << 32` 已強迫所有運算走 `BigInt`，4 個 `permissionMask*` helper 不斷修補 32-bit 邊界。

下一個要新加的權限位元就會把這套系統撞牆——這是「在路上的 bug」，不是技術債。

## 設計概要

把 `users.permissions: bigint`（位元遮罩）改成 `users.permission_names: text[]`（權限名稱字串集合）：

1. **儲存層**：`text[]`，零容量上限；保留 `NULL`「依角色預設」語意；`-1` sentinel 改成 `['*']`。
2. **Enum 層**：`Permission` 改為 `str Enum`（`perm.value == "EMPLOYEES_READ"`）；位元值搬到 `LEGACY_PERMISSION_BITS` dict，僅 migration 用，runtime 不參考。
3. **Runtime**：後端 `has_permission(perms: list[str], required)` 走 set check；前端 `hasPermission(name)` 走 `includes`。所有 mask/BigInt 邏輯刪除。
4. **Migration**：單一 alembic 檔，inline `LEGACY_BITS` 拆 bit → name 做 backfill；downgrade 反向 round-trip。
5. **Rollout**：一次切換 PR + token_version bump 強制全員重登；前端啟動偵測舊 `userInfo` schema 自動清掉。

> 起手 brainstorm 時 user 原本建議「拆 `Permission.EMPLOYEE / FINANCE / PORTFOLIO` 三組 enum、前端合併」。討論後改成 `text[]`，因為：(a) 拆三 enum 仍受 bigint 容量限制，每組 enum 上限 63 位元，只是把問題推遲；(b) `text[]` 把 JS BigInt 心智負擔徹底移除；(c) 未來新增權限零 schema 改動。

## 資料模型

### `users.permission_names: text[]`

SQLAlchemy column 定義（`models/auth.py`）：

```python
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy import Text

permission_names = Column(
    ARRAY(Text),
    nullable=True,
    default=None,
    comment="權限名稱集合（NULL=依角色預設；['*']=全部；[]=無；其他=顯式 perm names）",
)
```

DDL：

```sql
ALTER TABLE users ADD COLUMN permission_names text[] NULL;
-- backfill ...
ALTER TABLE users DROP COLUMN permissions;
```

**儲存語意：**

| 值 | 意義 | 對應舊值 |
|----|------|----------|
| `NULL` | 使用角色預設（不 override） | `permissions IS NULL` |
| `ARRAY['*']` | 全部權限（admin） | `permissions = -1` |
| `ARRAY[]::text[]` | 0 個權限（parent） | `permissions = 0` |
| `ARRAY['EMPLOYEES_READ', 'SALARY_WRITE', ...]` | 顯式集合 | `permissions = bit OR ...` |

**為何保留 `NULL`「依角色預設」**：現行設計中，user 不 override 角色預設即留空；展開全部 mask 寫入 DB 會增加維護成本（改 ROLE_TEMPLATES 後需要 backfill 所有「未 override」的 user）。保持語意一致。

### 不加 GIN index

當前 `has_permission` 都在 app layer 算，沒有「SQL 篩有 X 權限的全部 user」查詢需求。若日後出現，再加：

```sql
CREATE INDEX idx_users_permission_names ON users USING GIN (permission_names);
```

## Enum 設計

```python
# utils/permissions.py
from enum import Enum

WILDCARD = "*"

class Permission(str, Enum):
    """權限識別字串。

    繼承 str 讓 perm.value == "EMPLOYEES_READ"，可直接 in list 比對。
    位元值已搬到 LEGACY_PERMISSION_BITS，僅 migration 與 downgrade 使用，
    runtime 不參考。
    """
    DASHBOARD = "DASHBOARD"
    APPROVALS = "APPROVALS"
    CALENDAR = "CALENDAR"
    SCHEDULE = "SCHEDULE"
    MEETINGS = "MEETINGS"
    REPORTS = "REPORTS"
    AUDIT_LOGS = "AUDIT_LOGS"
    ATTENDANCE_READ = "ATTENDANCE_READ"
    ATTENDANCE_WRITE = "ATTENDANCE_WRITE"
    # ... 全部 53 條
    VENDOR_PAYMENT_WRITE = "VENDOR_PAYMENT_WRITE"

# 位元值凍結快照——僅供 alembic upgrade()/downgrade() backfill 使用
# 一旦 migration 跑過 prod，請勿變更此表（保持歷史 migration 可重跑）
LEGACY_PERMISSION_BITS: dict[str, int] = {
    "DASHBOARD": 1 << 0,
    "APPROVALS": 1 << 1,
    # ... 全部 53 條
    "VENDOR_PAYMENT_WRITE": 1 << 62,
}
```

### 保留 / 改造的結構

- `ROLE_TEMPLATES`：值型別從 `int`（bitmask）改為 `list[str]`（permission names）；`admin` 從 `-1` 改為 `[WILDCARD]`。寫法統一用 `Permission.X.value`（雖然 `Permission` 繼承 `str` 兩者等價，但 `.value` 在 type checker 與 IDE 下行為一致，且 JSON serialization 無歧義）。
- `SPLIT_MODULES`：結構不變（read/write 對應字串）。
- `PERMISSION_GROUPS`：結構不變（前端 UI 用，已是字串）。
- `PERMISSION_LABELS`：結構不變（中文標籤）。
- `Permission.ALL`：移除（沒有意義；用 `WILDCARD` 表達）。

### 新 / 改的 helper

```python
def has_permission(user_perms: list[str] | None, required: Permission | str) -> bool:
    """單一權限檢查。

    user_perms 應為已 resolve 完的最終 list（NULL 已用 role 預設展開）。
    若 caller 傳 None，視為「無權限」回 False；不在 helper 內 fallback role。
    """
    if user_perms is None:
        return False
    if WILDCARD in user_perms:
        return True
    name = required.value if isinstance(required, Permission) else required
    return name in user_perms


def resolve_user_permissions(user: User) -> list[str]:
    """把 DB 欄位 + role 預設合成最終 permission 集合。

    user.permission_names 為 NULL → 用 ROLE_TEMPLATES[user.role]
    否則直接用 user.permission_names。
    """
    if user.permission_names is None:
        return list(ROLE_TEMPLATES.get(user.role, []))
    return list(user.permission_names)


def get_role_default_permissions(role: str) -> list[str]:
    return list(ROLE_TEMPLATES.get(role, ROLE_TEMPLATES["teacher"]))


def get_permission_list(user_perms: list[str] | None) -> list[str]:
    """供 audit log / debug 用：把使用者實際擁有的權限名稱展開。
    遇到 wildcard 展開為所有 enum names。
    """
    if user_perms is None:
        return []
    if WILDCARD in user_perms:
        return [p.value for p in Permission]
    return [p for p in user_perms if p in Permission.__members__]
```

**移除**：舊 `has_permission(user_permissions: int, required: Permission) -> bool`、`get_permission_value(name) -> int`、`Permission.ALL`。**不留 shim**——雙語意永久殘留會把這次重構的價值抵銷。

## Migration

### 檔案位置

`alembic/versions/<rev>_permissions_to_text_array.py`

### upgrade()

```python
def upgrade():
    bind = op.get_bind()

    # 1) 加新欄
    op.add_column(
        "users",
        sa.Column("permission_names", postgresql.ARRAY(sa.Text()), nullable=True),
    )

    # 2) backfill（inline LEGACY_BITS，避免 import utils.permissions 被未來改動影響）
    LEGACY_BITS = {
        "DASHBOARD": 1 << 0,
        # ... 全部 53 條，從 LEGACY_PERMISSION_BITS 複製
    }

    rows = bind.execute(sa.text("SELECT id, permissions FROM users")).fetchall()
    for r in rows:
        perm_val = r.permissions
        if perm_val is None:
            names = None
        elif perm_val == -1:
            names = ["*"]
        elif perm_val == 0:
            names = []
        else:
            # int → list[str]：把 bit 拆出
            names = [
                name for name, bit in LEGACY_BITS.items()
                if (perm_val & bit) == bit
            ]
        bind.execute(
            sa.text("UPDATE users SET permission_names = :names WHERE id = :id"),
            {"names": names, "id": r.id},
        )

    # 3) drop 舊欄
    op.drop_column("users", "permissions")
```

### downgrade()

```python
def downgrade():
    bind = op.get_bind()
    LEGACY_BITS = { ... }  # 同 upgrade 的 inline copy

    op.add_column(
        "users",
        sa.Column("permissions", sa.BigInteger(), nullable=True),
    )

    rows = bind.execute(sa.text("SELECT id, permission_names FROM users")).fetchall()
    for r in rows:
        names = r.permission_names
        if names is None:
            val = None
        elif "*" in names:
            val = -1
        else:
            # 對 LEGACY_BITS 不認得的字串：abort（避免 silently drop）
            unknown = [n for n in names if n not in LEGACY_BITS and n != "*"]
            if unknown:
                raise RuntimeError(
                    f"downgrade abort: user_id={r.id} 含 LEGACY_BITS 不認得的權限 {unknown}。"
                    "請手動處理或更新 LEGACY_BITS。"
                )
            val = 0
            for n in names:
                val |= LEGACY_BITS[n]
        bind.execute(
            sa.text("UPDATE users SET permissions = :val WHERE id = :id"),
            {"val": val, "id": r.id},
        )

    op.drop_column("users", "permission_names")
```

### 為什麼 inline LEGACY_BITS

- migration 跑歷史時 `utils/permissions.py` 可能已經改過（新增 perm、刪除 perm）；import 會抓到當下版本而非 migration 撰寫時的版本。
- inline 是「快照」，永遠對齊 migration 寫的那一刻。
- 重複代碼可接受（一次性 63 行）。

### Migration chain

依現有規則命名：`<8-char-slug>_permissions_to_text_array.py`。`down_revision` 指向 main 上最新一條 migration。需手動確認 chain：執行前跑 `alembic heads`。

## API 契約變化（BREAKING）

### 前端拿到的 user 物件（login / refresh / me）

```diff
{
  "id": 123,
  "username": "wu",
  "role": "admin",
- "permissions": 4611686018427387904,
+ "permission_names": ["EMPLOYEES_READ", "SALARY_WRITE", "..."],
  "is_active": true,
  ...
}
```

對於 admin：

```json
{ "permission_names": ["*"] }
```

### JWT payload claim

```diff
{
  "user_id": 123,
  "role": "admin",
- "permissions": 4611686018427387904,
+ "permission_names": ["*"],
  "token_version": 5,
  "exp": ...
}
```

### 影響清單（grep 確認）

- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST/PUT/PATCH /api/users`（user CRUD）
- `GET /api/users`（list）
- `GET /api/users/{id}`
- `GET /api/roles/permissions-definition`（前端設定頁的 schema endpoint，內部 `get_permissions_definition()` 回傳 shape 不變）

### `get_permissions_definition()` 回傳

外部 shape 保留——前端設定頁不需動。內部把 `permissions[name].value` 從 int 改為 string（與 name 相同）：

```diff
{
  "permissions": {
    "EMPLOYEES_READ": {
-     "value": 256,
+     "value": "EMPLOYEES_READ",
      "label": "員工管理 (檢視)"
    },
    ...
  },
  "groups": [...],
  "roles": {...},
  "split_modules": {...}
}
```

前端使用 `permissions[name].value` 的點需檢查（grep `\.value` near `permissions`）。

## 前端改動

### `src/constants/permissions.ts`

```ts
// 移除 PERMISSION_VALUES（number map）
// 新增 PERMISSION_NAMES（純 const string map，純 type-safety）
export const PERMISSION_NAMES = {
  DASHBOARD: 'DASHBOARD',
  APPROVALS: 'APPROVALS',
  // ... 53 條
  VENDOR_PAYMENT_WRITE: 'VENDOR_PAYMENT_WRITE',
} as const

export type PermissionName = typeof PERMISSION_NAMES[keyof typeof PERMISSION_NAMES]
```

`ROUTE_PERMISSION_RULES` 不動（已是字串）。

### `src/utils/auth.ts`

```ts
export function hasPermission(permissionName: string): boolean {
  const userInfo = getUserInfo()
  if (!userInfo) return false
  if (userInfo['role'] === 'teacher') return false

  const perms = userInfo['permission_names'] as string[] | null | undefined
  if (perms == null) return false  // resolve 在後端；前端 null = 無顯式權限
  if (perms.includes('*')) return true
  return perms.includes(permissionName)
}

export function hasWritePermission(moduleName: string): boolean {
  return hasPermission(`${moduleName}_WRITE`)
}
```

**移除**：`_toBig`、所有 BigInt 邏輯。

### Mask helpers 改名 + 改實作

| 舊 | 新 | 行為 |
|----|----|------|
| `permissionMaskHas(mask, value)` | `permissionsHave(perms, name)` | `perms.includes(name) \|\| perms.includes('*')` |
| `permissionMaskAdd(mask, value)` | `permissionsAdd(perms, name)` | `[...new Set([...perms, name])]` |
| `permissionMaskRemove(mask, value)` | `permissionsRemove(perms, name)` | `perms.filter(p => p !== name)` |
| `permissionMaskCombine(values)` | `permissionsCombine(arrays)` | `[...new Set(arrays.flat())]` |

### Call site

grep 抓改點：

```bash
grep -rE 'permissionMaskHas|permissionMaskAdd|permissionMaskRemove|permissionMaskCombine|PERMISSION_VALUES|userInfo\.permissions[^_]' src/
```

預期改點：user 設定頁（`SettingsUsersTab.vue`、`UserPermissionEditor.vue` 或類似 — 以 grep 結果為準）、`useDashboardSections.ts`、`AdminSidebar.vue` 等已知 import 點。所有 number 參數改為 `string[]`。

## Rollout

### 部署順序

```
T0  後端 PR merge to main + 前端 PR merge to main（同日同窗口）
T1  staging:
    a. alembic upgrade head
    b. backend deploy
    c. frontend deploy
    d. 手動 smoke：admin/hr/teacher 三角色登入 + 任一 protected route + user 設定頁勾權限
T2  staging 兩端驗證 1 小時內穩定 → prod:
    a. alembic upgrade head（migration 自帶 token_version bump）
    b. backend deploy
    c. frontend deploy
T3  全員第一次重整頁面 → 強制重登一次
```

### Token 失效策略

利用既有 `users.token_version` 機制——`verify_token()` 已經比對 `payload.token_version != user.token_version` 直接 reject 舊 token。

Migration 在 backfill 完 `permission_names` 後加一行：

```python
bind.execute(sa.text("UPDATE users SET token_version = COALESCE(token_version, 0) + 1"))
```

部署後第一次 token verify 就失效（舊 cookie token 帶舊 version），全員重登。不需引入新環境變數。

### 前端啟動嗅探

`src/utils/auth.ts` 在 `_readFromStorage()` 後加一次性 schema check：

```ts
const stored = JSON.parse(localStorage.getItem('userInfo') ?? 'null')
if (stored && 'permissions' in stored && !('permission_names' in stored)) {
  localStorage.removeItem('userInfo')
  // 走正常未登入流程；下次 navigation 會被 canAccessRoute 拒絕 → redirect /login
}
```

UX：deploy 後第一次開瀏覽器強制重登一次，沒了。

## 測試策略

### 後端

新增：

- `tests/test_permissions_unit.py`
  - `has_permission` 三情境：wildcard / 命中 / miss / None input
  - `resolve_user_permissions`：NULL → role 預設、明確值 → 直接用
  - 每個 `ROLE_TEMPLATES[role]` 預期擁有的權限子集（防止 ROLE_TEMPLATES 漏改）
  - `get_permission_list` 處理 wildcard 展開

- `tests/test_permission_migration_roundtrip.py`
  - 純函式測 backfill 邏輯（不跑真 alembic，直接 import 函式或 inline copy）
  - Round-trip 案例：`None ↔ None`、`-1 ↔ ['*']`、`0 ↔ []`、`(1<<8)|(1<<23) ↔ ['EMPLOYEES_READ', 'SALARY_WRITE']`
  - downgrade 遇到 unknown name 必 raise

修改：

- 全部 `tests/test_*_permission*.py` 與 router 守衛測試：呼叫處 `permissions=-1` / `permissions=1 << N` 改成 `permission_names=['*']` / `['EMPLOYEES_READ', ...]`。預期 ~30 處（以 grep 為準）。

驗收標準：全套 `pytest` 4486/0（與 main 對齊；3 條 pre-existing `test_audit_router` fail 不計）。

### 前端

新增：

- `tests/utils/auth.test.ts`
  - `hasPermission`：`['*']`、`[]`、命中、miss、teacher 永遠 false、null
- `tests/utils/permissions.test.ts`
  - 4 個改名 helper round-trip

修改：

- 舊 `permissionMaskHas` 相關測試一併改名 + 改 setup（穿 string[]）。

驗收標準：全套 vitest 2349/2349 全綠；typecheck 0 error；build 0 error。

### 手測 checklist（merge 前）

1. admin 登入 → 全部選單可見
2. hr 登入 → 只看到 ROLE_TEMPLATES['hr'] 內的選單
3. supervisor 登入 → ROLE_TEMPLATES['supervisor']
4. teacher 登入 → 自動進 /portal，admin 路由 hard redirect
5. parent 登入 → /parent 入口，admin 路由 hard redirect
6. user 設定頁勾選/取消權限 → save → 該 user 重登後 sidebar 立即反映
7. 從 main 切到本分支不清 localStorage 開瀏覽器 → 不卡白屏，redirect /login

## 風險與回滾

| 風險 | 機率 | 影響 | 對策 |
|------|------|------|------|
| Migration backfill 漏 enum bit | 低 | 部分 user 缺權限 | LEGACY_BITS 63 條對齊測試 + dry-run print diff |
| 前後端 deploy 不同步 | 中 | 全站 500 | staging 先驗 + 同窗口 deploy + migration token_version bump 確保 session reset |
| 漏改 call site（仍用 mask） | 中 | runtime error | typecheck blocking + pytest 全綠 + grep CI lint check |
| LEGACY_BITS 與 utils/permissions 漂移 | 低 | 未來改 enum 後跑歷史 migration 炸 | LEGACY_BITS 寫死在 migration 檔 + 註解「已凍結」 |
| Rollback 後新版簽出的 token 不被舊版接受 | 中 | 所有 user 強制重登一次 | 預期行為——舊版 verify 抓不到新 `permission_names` claim 自然 reject，user 重登拿回 bigint-style token |
| 未來新 perm 在 LEGACY_BITS 沒有的 string 撐到 downgrade | 低 | downgrade abort | downgrade 已加 abort 守衛（含 user_id 訊息） |

### Rollback 程序

最壞情境：上 prod 後發現嚴重 bug 需回滾。

1. 部署前一版前端
2. `alembic downgrade -1`（permission_names → permissions bigint）
3. 部署前一版後端
4. 全員瀏覽器 reload → 新版簽出的 cookie 含 `permission_names` claim，前一版的 verify 邏輯不認得 → 自然 redirect 重登一次

`token_version` 不需要回滾調整：新版 token 的 claim shape 不同，前一版 verify 直接失敗，與 token_version 無關。

### Rollback 不可逆的情境

- prod 已上線後 user 透過設定頁**新增**了 LEGACY_BITS 沒有的 perm 名稱（理論上不可能，因為 enum 範圍由前後端 const 寫死，UI 只能勾既有）。
- downgrade SQL 已加守衛：遇到不認得的 perm 名 abort，避免 silently drop。

## Out of Scope

- ROLE_TEMPLATES 進 DB（保留 in-code dict；user 已決定 scope）
- 多對多 user_permissions 表（不導入正規 RBAC）
- ABAC / 細粒度 row-level（家長端 parent RLS spike 為獨立軌）
- Permission audit log（現有 audit middleware 已記）
- Sentry / metrics 加觀測點（不需要）

## Followups（不列為本 spec 範圍）

- prod 跑穩後一條 followup commit：把 `LEGACY_PERMISSION_BITS` 從 `utils/permissions.py` 移除（migration 檔內 inline copy 保留）
- `Permission.__str__` / `__repr__` 印 name（讓 log 可讀）
- 若日後出現「SQL 篩有 X 權限的全部 user」需求，加 GIN index

## 變更檔案清單（預估）

### Backend

- `utils/permissions.py`（重寫）
- `models/auth.py`（`permissions` → `permission_names: ARRAY(Text)`）
- `api/auth.py`（login / me / refresh response）
- `utils/auth.py`（JWT issue claim 改名 `permissions` → `permission_names`；verify 邏輯不變）
- `api/users.py`（user CRUD）
- 所有 router 內 `has_permission(...)` 與 `permissions=` 引用點（~25 routers，逐個 grep）
- `alembic/versions/<rev>_permissions_to_text_array.py`（新檔）
- `tests/test_permissions_unit.py`（新檔）
- `tests/test_permission_migration_roundtrip.py`（新檔）
- 所有現有 permission 測試（簽章改）

### Frontend

- `src/utils/auth.ts`（重寫 hasPermission + 4 改名 helper + 移除 BigInt）
- `src/constants/permissions.ts`（PERMISSION_VALUES → PERMISSION_NAMES）
- 所有 `permissionMaskHas/Add/Remove/Combine` call site（grep）
- 所有 `userInfo.permissions` 引用點（→ `userInfo.permission_names`）
- 所有 `PERMISSION_VALUES` 引用點（→ `PERMISSION_NAMES`）
- `tests/utils/auth.test.ts`（新檔）
- `tests/utils/permissions.test.ts`（新檔）
- 現有 permission 相關測試（簽章改）

# SPEC-007：權限系統

| 欄位 | 值 |
|------|----|
| Version | v0.1 |
| Status | Draft |
| Scope | `utils/permissions.py`、`utils/auth.py`、`models/auth.py`、`models/permission_models.py`、`api/auth.py`、`api/permissions_admin.py` |
| Related | `docs/superpowers/specs/2026-05-21-permission-intflag-split-design.md`、`docs/superpowers/specs/2026-05-25-permission-db-driven-design.md`、`docs/superpowers/specs/2026-05-25-permission-role-library-design.md`、`docs/superpowers/specs/2026-05-21-jwt-secret-rotation-design.md`、`docs/superpowers/specs/2026-04-27-salary-permission-and-finalize-fixes-design.md` |

---

## Overview

本系統採 **RBAC + 顯式 override** 的權限模型，於 2026-05-21 自 64-bit `IntFlag` 容量限制中重構為 PostgreSQL `text[]` enum 字串集合，再於 2026-05-25 把權限／角色定義從 in-code dict 搬到 DB（`permission_definitions` + `roles` 兩張表），達成 admin runtime 即時改動。

關鍵設計：

- **`Permission` enum（`str` 子類）**：實際讀出共 **64 個**權限字串（任務描述中的「96 個」屬上游待校正資訊；本 SPEC 以實際程式碼讀出為準），命名規則 `<DOMAIN>_<ACTION>`，多數網域成對拆分為 `<DOMAIN>_READ`/`<DOMAIN>_WRITE`，部分含 `APPROVE`/`PUBLISH`/`ADMINISTER`/`REVIEW`/`ACCOUNTING`/`FINALIZE` 等特殊動作。
- **`WILDCARD = "*"`**：super-admin 通配；若使用者 `permission_names` 內含 `*`，則任何 `has_permission()` 檢查直接放行。
- **DB-driven 三層解析**：
  1. `users.permission_names` 為 `NULL` → 走 `users.role` → 從 `roles` 表拉預設權限模板；
  2. `users.permission_names` 為 `list[str]` → 直接使用（顯式 override 角色預設）；
  3. 出現 `*` → 視為全部權限。
- **核心 7 角色**（`is_core=True`，alembic seed）：`admin`、`principal`、`supervisor`、`hr`、`accountant`、`teacher`、`parent`；後續可由 admin 透過 `ROLES_MANAGE` 端點 CRUD 自訂角色／權限。
- **`LEGACY_PERMISSION_BITS`**：63 個位元值的凍結快照，**僅** alembic `permtxt01` 遷移時 backfill 使用，runtime 不參考。
- **守衛入口**：所有路由必須加 `Depends(require_permission(Permission.XXX))` 或 `require_staff_permission(Permission.XXX)`（後者額外擋 teacher／parent 撞管理端）。
- **JWT 整合**：登入發 access token 內含 `permission_names`；任何權限變更必同步遞增 `token_version`，使舊 token 在下次 `/refresh` 立即失效。

---

## Interface Definitions

### Permission Enum（`utils/permissions.py`）

實際 enum 共 **64 個** Permission（與 `LEGACY_PERMISSION_BITS` 63 條的差異為 2026-05-25 新增的 `ROLES_MANAGE`，僅存在於 enum，不入 legacy bits）。以下依 `PERMISSION_GROUPS` 邏輯分組整理。

#### 1. 首頁／工作台（2 條）

| 群組 | Permission | 用途簡述 |
|------|------------|----------|
| 首頁 | `DASHBOARD` | 儀表板 |
| 首頁 | `APPROVALS` | 審核工作台 |

#### 2. 考勤管理（9 條：3 單一 + 3 讀寫對）

| 群組 | Permission | 用途簡述 |
|------|------------|----------|
| 考勤 | `CALENDAR` | 行事曆 |
| 考勤 | `SCHEDULE` | 排班管理 |
| 考勤 | `MEETINGS` | 園務會議 |
| 考勤 | `ATTENDANCE_READ` | 出勤管理（檢視） |
| 考勤 | `ATTENDANCE_WRITE` | 出勤管理（編輯） |
| 考勤 | `LEAVES_READ` | 請假管理（檢視） |
| 考勤 | `LEAVES_WRITE` | 請假管理（編輯） |
| 考勤 | `OVERTIME_READ` | 加班管理（檢視） |
| 考勤 | `OVERTIME_WRITE` | 加班管理（編輯） |

#### 3. 人事教務（21 條：3 單一 + 9 讀寫對）

| 群組 | Permission | 用途簡述 |
|------|------------|----------|
| 人事教務 | `ACTIVITY_PAYMENT_APPROVE` | 才藝課收款簽核 |
| 人事教務 | `STUDENTS_LIFECYCLE_WRITE` | 學生生命週期（狀態轉移） |
| 人事教務 | `RECRUITMENT_CONVERT` | 招生轉化為學生 |
| 人事教務 | `EMPLOYEES_READ` | 員工管理（檢視） |
| 人事教務 | `EMPLOYEES_WRITE` | 員工管理（編輯） |
| 人事教務 | `STUDENTS_READ` | 學生管理（檢視） |
| 人事教務 | `STUDENTS_WRITE` | 學生管理（編輯） |
| 人事教務 | `GUARDIANS_READ` | 監護人資料（檢視） |
| 人事教務 | `GUARDIANS_WRITE` | 監護人資料（編輯） |
| 人事教務 | `CLASSROOMS_READ` | 班級管理（檢視） |
| 人事教務 | `CLASSROOMS_WRITE` | 班級管理（編輯） |
| 人事教務 | `SALARY_READ` | 薪資管理（檢視） |
| 人事教務 | `SALARY_WRITE` | 薪資管理（編輯） |
| 人事教務 | `ACTIVITY_READ` | 課後才藝（檢視） |
| 人事教務 | `ACTIVITY_WRITE` | 課後才藝（編輯） |
| 人事教務 | `DISMISSAL_CALLS_READ` | 接送通知（檢視） |
| 人事教務 | `DISMISSAL_CALLS_WRITE` | 接送通知（操作） |
| 人事教務 | `FEES_READ` | 學費管理（檢視） |
| 人事教務 | `FEES_WRITE` | 學費管理（編輯） |
| 人事教務 | `RECRUITMENT_READ` | 招生統計（檢視） |
| 人事教務 | `RECRUITMENT_WRITE` | 招生統計（編輯） |

#### 4. 教職員考核 / 年終（8 條：4 單一 + 2 讀寫對）

| 群組 | Permission | 用途簡述 |
|------|------------|----------|
| 考核 | `APPRAISAL_READ` | 考核資料（檢視） |
| 考核 | `APPRAISAL_EVENT_WRITE` | 考核事件（登錄） |
| 考核 | `APPRAISAL_REVIEW` | 考核簽核（主管第一階） |
| 考核 | `APPRAISAL_ACCOUNTING` | 考核核數字（會計第二階） |
| 考核 | `APPRAISAL_FINALIZE` | 考核核定（最高主管第三階） |
| 考核 | `APPRAISAL_RULE_WRITE` | 考核扣分規則設定（Phase 1 calibrate） |
| 年終 | `YEAR_END_READ` | 年終結算（檢視） |
| 年終 | `YEAR_END_WRITE` | 年終結算（編輯） |
| 年終 | `YEAR_END_FINALIZE` | 年終核定（最高主管） |

#### 5. 園務行政（8 條：6 單一 + 2 讀寫對）

| 群組 | Permission | 用途簡述 |
|------|------------|----------|
| 園務 | `REPORTS` | 報表統計 |
| 園務 | `AUDIT_LOGS` | 操作紀錄 |
| 園務 | `BUSINESS_ANALYTICS` | 經營分析（招生漏斗／流失預警） |
| 園務 | `PARENT_MESSAGES_WRITE` | 家長訊息（發送／回覆） |
| 園務 | `GOV_REPORTS_VIEW` | 政府申報資料（檢視） |
| 園務 | `GOV_REPORTS_EXPORT` | 政府申報匯出（執行） |
| 園務 | `ANNOUNCEMENTS_READ` | 公告管理（檢視） |
| 園務 | `ANNOUNCEMENTS_WRITE` | 公告管理（編輯） |
| 園務 | `VENDOR_PAYMENT_READ` | 廠商付款簽收（檢視） |
| 園務 | `VENDOR_PAYMENT_WRITE` | 廠商付款簽收（編輯／簽收） |

#### 6. 成長歷程 / 教務（8 條：2 單一 + 3 讀寫對）

| 群組 | Permission | 用途簡述 |
|------|------------|----------|
| 教務 | `PORTFOLIO_PUBLISH` | 學期報告（發佈） |
| 教務 | `STUDENTS_MEDICATION_ADMINISTER` | 餵藥執行與紀錄 |
| 教務 | `PORTFOLIO_READ` | 成長歷程（檢視） |
| 教務 | `PORTFOLIO_WRITE` | 成長歷程（編輯） |
| 教務 | `STUDENTS_HEALTH_READ` | 健康資訊（檢視） |
| 教務 | `STUDENTS_HEALTH_WRITE` | 健康資訊（編輯） |
| 教務 | `STUDENTS_SPECIAL_NEEDS_READ` | 特殊需求（檢視） |
| 教務 | `STUDENTS_SPECIAL_NEEDS_WRITE` | 特殊需求（編輯／IEP） |

#### 7. 系統（4 條：2 讀寫對）

| 群組 | Permission | 用途簡述 |
|------|------------|----------|
| 系統 | `SETTINGS_READ` | 系統設定（檢視） |
| 系統 | `SETTINGS_WRITE` | 系統設定（編輯） |
| 系統 | `USER_MANAGEMENT_READ` | 帳號管理（檢視） |
| 系統 | `USER_MANAGEMENT_WRITE` | 帳號管理（編輯） |

#### 8. DB-driven 自訂角色管理（1 條）

| 群組 | Permission | 用途簡述 |
|------|------------|----------|
| 系統 | `ROLES_MANAGE` | 角色與權限管理（DB-driven CRUD 守衛，僅存於 enum，**不在** `LEGACY_PERMISSION_BITS`） |

#### 永遠未綁定路由的位元（legacy only）

`LEGACY_PERMISSION_BITS` 中所有 63 個 key 與 enum 64 個值的對應已驗證一致；唯一例外是 `ROLES_MANAGE` 於 2026-05-25 新增，不再回寫 legacy bits（執行期不參考）。

---

### Python Guard API

| 函式 | 模組 | 用途簡述 |
|------|------|----------|
| `has_permission(user_perms, required) -> bool` | `utils/permissions.py` | 單一權限檢查；`user_perms` 已 resolve 完的 list；`None` 視為無權限；含 `WILDCARD "*"` 直接 True；`required` 可為 `Permission` enum 或字串。 |
| `resolve_user_permissions(user) -> List[str]` | `utils/permissions.py` | 從 `User.permission_names` 取出最終權限；`None` 則套 `ROLE_TEMPLATES[user.role]`；非 `None` 原樣回傳。 |
| `get_role_default_permissions(session, role_code) -> List[str]` | `utils/permissions.py` | 從 DB `roles` 表拉預設權限；未知 role fallback 至 `teacher`（既有行為）。 |
| `get_permission_list(user_perms) -> List[str]` | `utils/permissions.py` | 展開為合法權限名稱列表；`None` → `[]`；含 `*` → 全部 enum；否則過濾非法字串。**[needs review]** docstring 寫「全部 63 個」，實際 enum 為 64 個 `Permission`。 |
| `get_permissions_definition(session) -> Dict` | `utils/permissions.py` | 從 DB `permission_definitions` + `roles` 動態組出前端 UI 需要的 `permissions`／`groups`／`roles`／`split_modules`。 |
| `get_current_user(request) -> dict` | `utils/auth.py` | FastAPI dependency：從 `httpOnly cookie access_token` 或 `Authorization: Bearer` 取 token → `decode_token` → 檢 `is_token_revoked` → 檢 scope-restricted token → 檢 DB `is_active`／`token_version`／`must_change_password`；回傳 JWT payload + `username`、`must_change_password`。 |
| `require_permission(permission)` | `utils/auth.py` | FastAPI dependency factory；包一層 `get_current_user`，再呼叫 `has_permission(payload["permission_names"], permission)`，失敗 403。 |
| `require_staff_permission(permission)` | `utils/auth.py` | 在 `require_permission` 之上額外擋 `role == teacher` 與 `role == parent`，迫使教師走 portal、家長走家長端。 |
| `require_admin(current_user)` | `utils/auth.py` | 僅 `role == admin` 通過。 |
| `require_parent_role()` | `utils/auth.py` | 僅 `role == parent` 通過。 |
| `require_non_parent_role()` | `utils/auth.py` | 拒絕 `role == parent`，避免家長 token 撞員工/管理端。 |
| `verify_ws_token(token) -> dict` | `utils/auth.py` | WebSocket 連線專用；除 `decode_token` 外亦查 DB 確認 `is_active`／`token_version`／`must_change_password`，並擋 `is_token_revoked`。 |
| `is_token_revoked(jti) -> bool` | `utils/auth.py` | 查 `jwt_blocklist` 表；空 jti → False；DB 失敗 fail-open 並 log warning。 |
| `revoke_token(jti, expires_at, reason)` | `utils/auth.py` | logout 用；`INSERT ... ON CONFLICT DO NOTHING`；DB 失敗 log error 不擋登出。 |
| `cleanup_jwt_blocklist() -> int` | `utils/auth.py` | scheduler 每日呼叫；刪 `expires_at < now` 的 jti，回傳刪除筆數。 |
| `create_access_token(data, expires_delta=None)` | `utils/auth.py` | 簽發 access token；自動產生 `jti`／補 `iat`／`original_iat`；header 帶 `kid`（multi-key rotation）。 |
| `decode_token(token) -> dict` | `utils/auth.py` | 先 `_check_token_algorithm`（防 alg:none、algorithm confusion）→ `_decode_with_keys`（multi-kid 容忍）；不允許過期。 |
| `decode_token_allow_expired(token) -> dict` | `utils/auth.py` | 用於 `/refresh`、`/end-impersonate`、`/logout`；允許 `JWT_REFRESH_GRACE_HOURS=2` 寬限期內過期；仍檢 jti blocklist。 |
| `decode_token_for_audit(token) -> dict\|None` | `utils/auth.py` | 純為 audit log 抽 user_id／name；**不可用於授權判斷**；任何失敗回 `None`。 |
| `validate_password_strength(password)` | `utils/auth.py` | 至少 8 字元、含大寫、小寫、數字；失敗 400 與中文理由。 |
| `hash_password(password)` | `utils/auth.py` | PBKDF2-HMAC-SHA256，600,000 iterations；格式 `{iter}${salt}${hash}`。 |
| `verify_password(plain, hashed)` | `utils/auth.py` | 同時相容新格式（含 iterations）與舊格式（固定 100,000 次）；`hmac.compare_digest` 恆定時間；格式不合走 `_dummy_hash` 維持回應時間。 |
| `needs_rehash(hashed_password)` | `utils/auth.py` | 判斷是否需透明升級（舊格式或低 iter）。 |

---

### HTTP 端點

#### `api/auth.py`（`/api/auth/*`）

| 項目 | 描述 |
|------|------|
| Function | `POST /api/auth/login` |
| Permission | 公開（無 guard） |
| Request | `LoginRequest { username, password }`；無 token |
| Response | `200 { must_change_password, user }`，並 `Set-Cookie: access_token=...; HttpOnly`；錯誤 401／403／429 |
| 備註 | 雙層 rate limit（IP 5 分鐘 20 次 + 帳號失敗鎖 5 次/15 分鐘）；wrong-credentials 仍跑 dummy PBKDF2 防 timing；`role == teacher` 強制學校 WiFi；密碼舊格式無感升級 |

| 項目 | 描述 |
|------|------|
| Function | `POST /api/auth/refresh` |
| Permission | 公開（憑 token 自身） |
| Request | 取 cookie 或 `Bearer` header；允許 grace 期內過期 |
| Response | `200 { user }`，並重發 `access_token`；錯誤 401／429 |
| 備註 | 比對 `token_version`；S2 absolute lifetime（`JWT_ABSOLUTE_LIFETIME_HOURS`）從 `original_iat` 算起超出即拒；IP rate limit |

| 項目 | 描述 |
|------|------|
| Function | `POST /api/auth/logout` |
| Permission | 公開 |
| Request | 取 cookie／header token |
| Response | `200 { message }`；清 `access_token` + `admin_token` cookie；同時 bump `token_version`、將 `jti` 寫 blocklist；過期 token 在 grace 內亦處理 |

| 項目 | 描述 |
|------|------|
| Function | `POST /api/auth/impersonate` |
| Permission | `role == admin`（`get_current_user` + 角色檢查） |
| Request | `ImpersonateRequest { employee_id }` |
| Response | 簽 target 員工的 token 寫 `access_token` cookie，並保留原 admin token 至 `admin_token` cookie |

| 項目 | 描述 |
|------|------|
| Function | `POST /api/auth/end-impersonate` |
| Permission | 須持有有效 `admin_token` cookie |
| Request | 無 body |
| Response | 簽回 admin 自己的新 access token，清 `admin_token` |

| 項目 | 描述 |
|------|------|
| Function | `GET /api/auth/me` |
| Permission | `get_current_user`（任何登入者） |
| Request | 無 body |
| Response | `{ id, username, role, role_label, permission_names, employee_id, name, title }` |

| 項目 | 描述 |
|------|------|
| Function | `POST /api/auth/change-password` |
| Permission | `get_current_user`；white-listed 在 `_PASSWORD_CHANGE_ALLOWED_PATHS` 中，即使 `must_change_password=True` 也可呼叫 |
| Request | `ChangePasswordRequest { old_password, new_password }` |
| Response | `{ message }`；重發新 token（避免改完密碼立刻 401）；bump `token_version` |

| 項目 | 描述 |
|------|------|
| Function | `GET /api/auth/users` |
| Permission | `require_staff_permission(Permission.USER_MANAGEMENT_READ)` |
| Request | 無 body |
| Response | `[{ id, username, role, role_label, permission_names, is_active, employee_id, employee_name, last_login }]` |

| 項目 | 描述 |
|------|------|
| Function | `POST /api/auth/users` |
| Permission | `require_staff_permission(Permission.USER_MANAGEMENT_WRITE)` + `_assert_can_manage_user` |
| Request | `CreateUserRequest { employee_id?, username, password, role, permission_names? }` |
| Response | `201 { message, id }`；`must_change_password=True` 預設 |
| 備註 | 提權鏈防護：caller 須能授予的權限 ⊆ caller 自己的權限 |

| 項目 | 描述 |
|------|------|
| Function | `PUT /api/auth/users/{user_id}/reset-password` |
| Permission | `require_staff_permission(Permission.USER_MANAGEMENT_WRITE)` + `_assert_can_manage_user(target_user)` |
| Request | `ResetPasswordRequest { new_password }` |
| Response | `{ message }`；強制 target `must_change_password=True`、bump `token_version` |

| 項目 | 描述 |
|------|------|
| Function | `PUT /api/auth/users/{user_id}` |
| Permission | `require_staff_permission(Permission.USER_MANAGEMENT_WRITE)` + `_assert_can_manage_user` |
| Request | `UpdateUserRequest { role?, permission_names?, is_active? }` |
| Response | `{ message }`；禁止停用自己；角色／權限／停用任一變動 → bump `token_version`；停用語意視為 soft-delete |

| 項目 | 描述 |
|------|------|
| Function | `DELETE /api/auth/users/{user_id}` |
| Permission | `require_staff_permission(Permission.USER_MANAGEMENT_WRITE)` + `_assert_can_manage_user(target_user)` |
| Request | 無 body |
| Response | `{ message }`；禁止刪除自己 |

| 項目 | 描述 |
|------|------|
| Function | `GET /api/auth/permissions` |
| Permission | **無 guard**（公開）**[needs review]**：純讀權限定義給前端渲染 UI 用；不洩漏使用者資料，但確認是否需登入限制 |
| Request | 無 body |
| Response | `{ permissions, groups, roles, split_modules }`，由 `get_permissions_definition(session)` 動態組裝 |

#### `api/permissions_admin.py`（`/api/*`，6 端點）

| 項目 | 描述 |
|------|------|
| Function | `POST /api/permissions/definitions` |
| Permission | `require_permission(Permission.ROLES_MANAGE)` |
| Request | `PermissionDefinitionIn { code (`^[A-Z][A-Z0-9_]*$`), label, description?, group_name="自訂" }` |
| Response | 新增 `PermissionDefinition` 一筆；`is_core=False` |
| 錯誤 | `422` code 已存在 |

| 項目 | 描述 |
|------|------|
| Function | `PUT /api/permissions/definitions/{code}` |
| Permission | `require_permission(Permission.ROLES_MANAGE)` |
| Request | `PermissionDefinitionUpdate { label?, description?, group_name? }` |
| Response | 改 `label`／`description`／`group_name`（**不可改 code、不可改 is_core**） |
| 錯誤 | `404` 不存在 |

| 項目 | 描述 |
|------|------|
| Function | `DELETE /api/permissions/definitions/{code}` |
| Permission | `require_permission(Permission.ROLES_MANAGE)` |
| Request | 無 body |
| Response | `{ ok: true }`；**核心權限拒刪（`409`）** |
| 副作用 | bump 持有此 perm 的所有 user `token_version` → `array_remove` 從 `roles.permissions`／`users.permission_names` → `DELETE pd`；SQLite 走 app 層 fallback |

| 項目 | 描述 |
|------|------|
| Function | `POST /api/roles` |
| Permission | `require_permission(Permission.ROLES_MANAGE)` |
| Request | `RoleIn { code (`^[a-z][a-z0-9_]*$`), label, description?, permissions=[] }` |
| Response | 新增 `Role`；`is_core=False` |
| 錯誤 | `422` code 已存在；`422` 含未知 permission code（`*` 例外） |

| 項目 | 描述 |
|------|------|
| Function | `PUT /api/roles/{code}` |
| Permission | `require_permission(Permission.ROLES_MANAGE)` |
| Request | `RoleUpdate { label?, description?, permissions? }` |
| Response | 改 `label`／`description`／`permissions`；**核心角色不可改 permissions（`409`）**；其他 user 依此 role 預設者 bump `token_version` |
| 錯誤 | `404` 不存在；`409` 核心角色想改 permissions |

| 項目 | 描述 |
|------|------|
| Function | `DELETE /api/roles/{code}` |
| Permission | `require_permission(Permission.ROLES_MANAGE)` |
| Request | 無 body |
| Response | `{ ok: true }`；**核心角色拒刪（`409`）**；尚有使用者持有此 role 時拒刪（`409`，回傳人數） |

---

## DTO Definitions

### `User` 表權限欄位（`models/auth.py`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `Integer PK` | |
| `employee_id` | `ForeignKey("employees.id")`, unique, nullable | `NULL` 表純管理帳號 |
| `username` | `String(50)`, unique, NOT NULL | 登入帳號 |
| `password_hash` | `String(255)` | PBKDF2 格式 `{iter}${salt}${hash}` |
| `role` | `String(20)`, default `teacher` | `teacher`／`admin`／`hr`／`supervisor`／`principal`／`accountant`／`parent`（與 `roles.code` 對應） |
| `permission_names` | `JSON().with_variant(ARRAY(Text), "postgresql")`, nullable, default `None` | **`NULL`=依角色預設**；`["*"]`=全部；`[]`=無；其他=顯式 perm names |
| `is_active` | `Boolean`, default `True` | |
| `must_change_password` | `Boolean`, default `False` | 新帳號或管理員 reset 後為 True |
| `token_version` | `Integer`, default 0, NOT NULL | 帳號停用／權限變更／密碼變更時遞增；舊 token 在 `/refresh` 立即被拒 |
| `last_login` | `DateTime` | |
| `line_user_id` | `String(100)`, unique | LINE 綁定 |
| `line_follow_confirmed_at` | `DateTime` | |
| `display_name` | `String(100)` | |
| `created_at` / `updated_at` | `DateTime`（naive Taipei） | `default=now_taipei_naive` |

`__table_args__`：`Index("ix_user_emp_active", "employee_id", "is_active")`。

### `PermissionDefinition` 表（`models/permission_models.py`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `BigInteger PK` | |
| `code` | `Text`, unique, NOT NULL | 權限識別字串（如 `EMPLOYEES_READ`） |
| `label` | `Text`, NOT NULL | 中文顯示名 |
| `description` | `Text`, nullable | 詳細說明 |
| `group_name` | `Text`, server_default `"自訂"` | 前端分組 |
| `is_core` | `Boolean`, default `False` | core 為 alembic seed（model docstring 寫「**57 條**」**[needs review]**：與目前 enum 64、`PERMISSION_LABELS` 145 行內含 64 條不一致；可能 docstring 過時或 seed 排除部分非業務 enum） |
| `created_at` / `updated_at` | `TIMESTAMP`（DB-side `func.now()`） | |
| Index | `ix_permission_definitions_group` on `group_name` | |

### `Role` 表（`models/permission_models.py`）

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | `BigInteger PK` | |
| `code` | `Text`, unique, NOT NULL | 對應 `users.role` 字串 |
| `label` | `Text`, NOT NULL | 中文顯示名 |
| `description` | `Text`, nullable | 適用對象一句話 |
| `permissions` | `JSON().with_variant(ARRAY(Text), "postgresql")`, NOT NULL, default `[]` | 角色預設權限；`["*"]` = wildcard；與 `users.permission_names` 同 shape |
| `is_core` | `Boolean`, default `False` | core 為 alembic seed 7 個：`admin`／`principal`／`supervisor`／`hr`／`accountant`／`teacher`／`parent` |
| `created_at` / `updated_at` | `TIMESTAMP` | |

### Pydantic Schema

`api/auth.py`：

- `LoginRequest { username, password }`
- `ChangePasswordRequest { old_password, new_password }`
- `CreateUserRequest { employee_id?, username, password, role="teacher", permission_names? }`
- `UpdateUserRequest { role?, permission_names?, is_active? }`
- `ResetPasswordRequest { new_password }`
- `ImpersonateRequest { employee_id }`

`api/permissions_admin.py`：

- `PermissionDefinitionIn { code (pattern: `^[A-Z][A-Z0-9_]*$`, max_length=64), label (1..80), description? (..500), group_name="自訂" (..40) }`
- `PermissionDefinitionUpdate { label?, description?, group_name? }`
- `RoleIn { code (pattern: `^[a-z][a-z0-9_]*$`, max_length=40), label (1..40), description? (..200), permissions=[] }`
- `RoleUpdate { label?, description?, permissions? }`

---

## Business Rules

1. **READ / WRITE 拆分原則**：18 個業務網域成對拆分為 `<DOMAIN>_READ` + `<DOMAIN>_WRITE`（`SPLIT_MODULES` 18 條：`ATTENDANCE`／`LEAVES`／`OVERTIME`／`EMPLOYEES`／`STUDENTS`／`CLASSROOMS`／`SALARY`／`ANNOUNCEMENTS`／`SETTINGS`／`USER_MANAGEMENT`／`ACTIVITY`／`DISMISSAL_CALLS`／`FEES`／`RECRUITMENT`／`GUARDIANS`／`APPRAISAL`／`YEAR_END`／`VENDOR_PAYMENT`）。少數例外含 `APPROVE`（`ACTIVITY_PAYMENT_APPROVE`）、`PUBLISH`（`PORTFOLIO_PUBLISH`）、`ADMINISTER`（`STUDENTS_MEDICATION_ADMINISTER`）、`REVIEW`／`ACCOUNTING`／`FINALIZE`（考核三階）、`CONVERT`（招生→學生轉換）、`LIFECYCLE_WRITE`（學生狀態轉移）、單一動作（`DASHBOARD`／`APPROVALS`／`CALENDAR`／`SCHEDULE`／`MEETINGS`／`REPORTS`／`AUDIT_LOGS`／`BUSINESS_ANALYTICS`／`PARENT_MESSAGES_WRITE`／`GOV_REPORTS_VIEW`／`GOV_REPORTS_EXPORT`／`ROLES_MANAGE`）。

2. **WILDCARD `*` 語意（super-admin）**：`has_permission(user_perms, _)` 偵測 `WILDCARD in user_perms` 即直接 True，**不展開**為列出所有 Permission；`get_permission_list` 才會把 `*` 展開為全部 enum value，僅供前端 UI 顯示用。`admin` 角色 `ROLE_TEMPLATES["admin"] = [WILDCARD]`。

3. **DB-driven 權限解析三層優先序**：
   - User 表 `permission_names IS NULL` → 由 `resolve_user_permissions(user)` fallback 至 `ROLE_TEMPLATES[user.role]`（in-code 模板，runtime 不查 DB）；
   - User 表 `permission_names` 為 `list[str]` → 原樣使用（顯式 override）；
   - `get_role_default_permissions(session, role_code)` 則從 DB `roles` 表查（用於 `create_user`／`update_user` 套預設、`PUT /api/roles/{code}` 後 bump 預設用戶 `token_version`）。
   - **[needs review]**：`resolve_user_permissions` 走 in-code `ROLE_TEMPLATES`、`create_user`/`update_user` 走 DB `roles`，兩者來源不一致。若 admin 透過 `PUT /api/roles/admin` 改了 admin 角色 permissions，已登入的 admin（`permission_names IS NULL`）下次 token decode 拿到的仍是 in-code `[WILDCARD]`，需重登／refresh 後新 token 才會走 DB。

4. **核心 7 角色 in-code 模板**（`ROLE_TEMPLATES`，`ROLE_LABELS`）：
   - `admin`：`["*"]`（系統管理員：唯一能改帳號、系統設定）
   - `principal`：`supervisor` 全部 + `SALARY_READ` + `AUDIT_LOGS` + `GOV_REPORTS_EXPORT`（園長）
   - `supervisor`：教務管理、招生轉換、考核全程、廠商付款、家園訊息、Portfolio 發佈、健康／特殊需求／餵藥
   - `hr`：員工、薪資、考勤／請假／加班、報表、政府報表、考核（事件登錄＋會計核數字）、年終讀寫、廠商付款讀寫
   - `accountant`：純財務 13 條（含 `SALARY_WRITE`／`FEES_WRITE`／`VENDOR_PAYMENT_*`／`YEAR_END_WRITE`／`APPRAISAL_ACCOUNTING`，**不含** `EMPLOYEES_WRITE`／`YEAR_END_FINALIZE`）
   - `teacher`：公告檢視、接送通知讀寫、Portfolio 讀寫、健康／特殊需求檢視、餵藥執行、家長訊息發送、考核事件登錄；**不可** 撞管理端 API（`require_staff_permission` 結構性擋線）
   - `parent`：**恆無任何 Permission**；資源存取一律由 `user_id → guardians` 過濾。

5. **`_assert_can_manage_user` 提權鏈防護**（`api/auth.py:78`）：caller 非 `admin` 時：
   - 不可管理 `target.role == admin`；
   - 不可把任何帳號 role 指定為 `admin`；
   - target 既有權限 + 此次 payload 的最終權限（明示或 role 預設）皆須 **⊆ caller 自身權限**（caller 持 `*` 例外）；
   - 違反任一條 → 403 並寫 warning log。

6. **`token_version` 失效機制**：以下事件**必同步**遞增 `User.token_version`：
   - 管理員透過 `PUT /api/auth/users/{id}/reset-password` 重設密碼；
   - 管理員透過 `PUT /api/auth/users/{id}` 改 `role`／`permission_names`／停用；
   - 使用者自行 `POST /api/auth/change-password`；
   - `POST /api/auth/logout`；
   - `DELETE /api/permissions/definitions/{code}` cascade 清除時 → 所有受影響 user；
   - `PUT /api/roles/{code}` 改 permissions → 所有依此 role 預設（`permission_names IS NULL`）的 user。

7. **legacy IntFlag 位元僅供 alembic 使用**：`LEGACY_PERMISSION_BITS`（63 條，bits 0–62）為**凍結快照**，alembic `permtxt01` migration backfill 自帶完整 dict（**不**從 `utils.permissions` import，避免未來修改污染歷史 migration）。Runtime 端 `has_permission`／`resolve_user_permissions` 完全不參考位元值；`tests/test_permissions_unit.py` enforce「63 條、bit 唯一、無 gap、max bit = 1<<62」。

8. **JWT 黑名單（`jwt_blocklist` 表）**：
   - logout 時把當前 `jti` + `exp + grace` 寫入；
   - `get_current_user`／`verify_ws_token`／`decode_token_allow_expired` 三路徑都會在驗章後查 `is_token_revoked(jti)`；
   - DB 失敗時 fail-open（log warning，不擋使用者）；
   - `cleanup_jwt_blocklist()` 由 scheduler 每日清過期項。

9. **JWT multi-key rotation**：
   - `JWT_SECRET_KEY` 為 current（簽 + 驗第一順位）；
   - `JWT_SECRET_KEYS_OLDS`（JSON list of strings）為 accept-only secrets，rotation 過渡期用；
   - `kid` header = `sha256(secret)[:12]`，確定性且不洩漏 secret；
   - 過渡期 legacy token（無 `kid`）依序試 `_LEGACY_TRY_ORDER`；
   - 啟動時 log 所有 `kid` 供 runbook 對照（不洩漏 secret）。

10. **`require_staff_permission` 角色黑名單**：除一般 permission 檢查外，明文拒絕 `role in ("teacher", "parent")`；教師走 `api/portal/*` 自助、家長走 `api/parent_portal/*`，**禁止** 撞管理端 API。

11. **scope-restricted token 不可上管理端**：`get_current_user` 偵測 payload 中 `scope` 欄位（如 parent_portal bind temp token `scope=bind`）即 401，避免繞過 `require_non_parent_role` 黑名單。

12. **公開端點不可洩漏內部錯誤**：任何 `Exception` → `utils.errors.raise_safe_500(e, context=...)`；dev `detail=str(e)`，prod `detail="系統內部錯誤，請聯繫管理員"` 並 `logger.error(..., exc_info=True)`。`api/auth.py` 多處 `try / except Exception as e: raise_safe_500(e)` 已遵循。

13. **新帳號／reset 密碼後強制改密**：`create_user` 與 `reset_password` 一律設 `must_change_password=True`；`get_current_user` 偵測到此旗標 + 請求路徑不在 `_PASSWORD_CHANGE_ALLOWED_PATHS`（`/api/auth/change-password`、`/api/auth/logout`）即 403。

14. **登入雙層 rate limit**：IP 滑動視窗 5 分鐘 20 次（不分成敗）+ 帳號失敗鎖 5 次/15 分鐘；DB-backed counter（`rate_limit_buckets` 表）多 worker 一致；**DB 失敗時 auth 端點 fail-closed**（`count_recent_attempts(fail_closed=True)` 降級到 in-process backstop 計數，非歸零；RA-MED-2 修補，2026-06-04）；非 auth scope 仍 fail-open。

15. **教師 WiFi 限制**：`role == teacher` 且 client IP 不在 `SCHOOL_WIFI_IPS` 白名單 → 403；未設白名單則全部放行。

16. **PBKDF2 密碼透明升級**：login 成功後若 `needs_rehash(password_hash)`（舊格式 / 低 iter），即時以新參數 rehash 寫回，**不**強制改密。

17. **絕對 session lifetime（S2）**：`/refresh` 檢查 `(now - original_iat) > JWT_ABSOLUTE_LIFETIME_HOURS` 即拒；缺欄位的舊 token 過渡期不擋，本次 refresh 後新 token 帶上 `original_iat`。

18. **`get_permissions_definition` 動態組裝**：runtime 從 DB `permission_definitions` + `roles` 拉，自動依 `group_name` 分群，並把 `SPLIT_MODULES` 的 read/write 配對掛回各 group 的 `split_permissions`；`module` 名取 `PERMISSION_LABELS[read].replace(" (檢視)", "")`。

19. **使用點統計**（grep 結果，含 utils/permissions.py 自身）：
    - `require_permission` / `require_staff_permission` 引用：**711** 處（含 api/、portal/、parent_portal/、tests/、main.py 等）；
    - `Permission.XXX` 引用（排除 `utils/permissions.py` 自身定義）：**797** 處；
    - 僅 `api/` 子目錄下 `Permission.\|require_permission`：**648** 處。
    - **[unverified]**：未個別逐一驗證每處 require_permission 真的 wrap 在 router decorator 之下；可能含註解、字串 literal、tests。

---

## Changelog

| Version | Date | Changes |
|---------|------|---------|
| v0.1 | 2026-05-28 | Initial draft；以 commit `8e4f1a5` snapshot 為基線 |

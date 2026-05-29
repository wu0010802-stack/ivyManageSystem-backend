# 權限系統 Row-Level Scoping 設計（Phase A：Helper + scope qualifiers）

- 日期：2026-05-29
- 影響面：ivy-backend（`utils/permissions.py`、`services/scoping/*`、~10 routers、1 alembic migration）+ ivy-frontend（`SettingsPermissionsTab.vue`、`src/utils/permissions.ts`、~2 元件）+ workspace `e2e/`（1 spec + globalSetup）
- 不影響：parent 端、wildcard `*` 行為、其餘 ~40 條 binary 權限、router 路徑分流
- 前置：[`docs/superpowers/specs/2026-05-25-permission-db-driven-design.md`](2026-05-25-permission-db-driven-design.md)（(b) DB-driven 角色 / 權限）

---

## Why

目前「教師只能看自班學生」這類 row-level 規則散落在 ~8 個 router 用同一段 SQLAlchemy OR 條件 hard-code：

```python
or_(
    Classroom.head_teacher_id == emp_id,
    Classroom.assistant_teacher_id == emp_id,
    Classroom.art_teacher_id == emp_id,
)
```

出現在：`api/student_assessments.py` / `api/student_enrollment.py` / `api/student_incidents.py` /
`api/portal/profile.py` / `api/portal/attendance.py` / `api/portal/activity.py` /
`api/portal/_shared.py` / `api/portal/students.py`（多處）。

**痛點：**

1. **DRY 違反 → 隱私風險面**：任何新增的 router 漏掉這段條件即洩漏全園學生資料。Code review 不易抓。
2. **規則隱式而非顯式**：teacher 的 `permission_names` 是 `["STUDENTS_READ", ...]` 字面看不出 `:own_class` 範圍；範圍藏在 router code。
3. **自訂角色（2026-05-25 (b) DB-driven）無法表達 scope**：admin 想開「資深老師」角色看相鄰班、或開「實習老師」只看自班，只能 binary on/off。
4. **與 portal 端規則漂移**：`api/portal/*` 與 admin router 的 OR 條件偶有 subtle 差異（has/lacks `art_teacher_id` 之類），規則沒有 single source of truth。
5. **2026-05-29「才藝點名跨班」事件**正是因為規則改動需要動 N 處，每處都要記得改 → 改不徹底 → 才特地寫了 helper 抽出共用條件（但仍只覆蓋 3 個 endpoint）。

**Phase B / C（不在本 spec 範圍）**：

- Phase B：個別班指派（user_classroom_scope 中介表，超越自班）
- Phase C：policy engine（Casbin/OPA 等級的條件式規則）

---

## 設計概念

**1 個新欄位 + 1 個 wire format 約定 + 1 套 scope helper。**

### 資料模型

```
permission_definitions          → 加 scope_options TEXT[] NULL
                                  空/NULL = 該權限無 scope；
                                  ['own_class','all'] = 可選 scope

users.permission_names          → 仍 ARRAY(Text)，wire format 改複合鍵
roles.permissions               → 同上
                                  'STUDENTS_READ:own_class' = 帶 scope
                                  'STUDENTS_READ'           = bare（向後相容＝':all'）
                                  '*'                       = wildcard，視為全部 :all
                                  'DASHBOARD'               = 無 scope_options 的權限保持單 code
```

### Scope 層級

本 Phase 只支援兩個 scope value：

| scope | 語意 |
|---|---|
| `all` | 看全園 |
| `own_class` | 看 user.employee_id 任 head_teacher/assistant_teacher/art_teacher 三角之一所屬班級的 student |

`own_employee` / `own_campus` 等本期不引入（無多園區概念；員工自己的資料走 `/portal/*` 獨立路徑）。

### 涵蓋的 15 條 scope-aware 權限

| 類別 | 權限 code |
|---|---|
| 學生核心 + 註記 | `STUDENTS_READ` `STUDENTS_WRITE` `STUDENTS_LIFECYCLE_WRITE` `PORTFOLIO_READ` `PORTFOLIO_WRITE` `PORTFOLIO_PUBLISH` |
| 健康 / 特殊需求 / 投藥（法規敏感 PII） | `STUDENTS_HEALTH_READ` `STUDENTS_HEALTH_WRITE` `STUDENTS_SPECIAL_NEEDS_READ` `STUDENTS_SPECIAL_NEEDS_WRITE` `STUDENTS_MEDICATION_ADMINISTER` |
| 接送 | `DISMISSAL_CALLS_READ` `DISMISSAL_CALLS_WRITE` |
| 班級 / 考勤 | `CLASSROOMS_READ` `ATTENDANCE_READ` |

其餘 ~40 條（DASHBOARD / FEES_* / SALARY_* / SETTINGS_* …）保持 binary，`scope_options=NULL`。

---

## Component 設計

### 1. `utils/permissions.py` 擴充

```python
class PermissionGrant(NamedTuple):
    code: str            # "STUDENTS_READ"
    scope: str | None    # "all" | "own_class" | None (該 perm 無 scope_options)


def resolve_grant(user, code: str) -> PermissionGrant | None:
    """從 user.permission_names 解析該 code 的授權。

    - wildcard '*'              → PermissionGrant(code, 'all')
    - 'STUDENTS_READ:own_class' → PermissionGrant(code, 'own_class')
    - 'STUDENTS_READ'           → PermissionGrant(code, 'all')   # bare = 預設全 scope
    - 未持有                     → None
    """


def require_scoped_permission(code: Permission):
    """FastAPI dependency；同 require_permission 但回傳 (user, grant)。

    既有 require_permission 保留不動，未涉及 scope 的 router 不必改。
    """
    def dep(user=Depends(get_current_user)) -> tuple[User, PermissionGrant]:
        grant = resolve_grant(user, code.value)
        if grant is None:
            raise HTTPException(403, detail=f"missing permission: {code.value}")
        return user, grant
    return dep
```

### 2. `services/scoping/` 新模組（純函式）

```
services/scoping/
├── __init__.py            # re-export 3 helper
├── student_scope.py       # STUDENTS_* / PORTFOLIO_* / DISMISSAL_* / 學生相關
├── classroom_scope.py     # CLASSROOMS_READ
└── attendance_scope.py    # ATTENDANCE_READ（同班同事的員工考勤）
```

每個檔案 export `filter_clause(user, scope: str) -> ColumnElement | None`：

```python
# services/scoping/student_scope.py
def filter_clause(user, scope: str) -> ColumnElement | None:
    if scope == "all":
        return None
    if scope == "own_class":
        emp_id = user.employee_id
        if emp_id is None:
            raise ValueError("scope=own_class requires user.employee_id")
        return Student.classroom_id.in_(
            select(Classroom.id).where(
                or_(
                    Classroom.head_teacher_id == emp_id,
                    Classroom.assistant_teacher_id == emp_id,
                    Classroom.art_teacher_id == emp_id,
                )
            )
        )
    raise ValueError(f"unknown scope: {scope}")
```

`classroom_scope` 直接 filter `Classroom.id`；`attendance_scope` 用相同三角條件找出同班員工 IDs，filter `EmployeeAttendance.employee_id IN (...)`。

### 3. Router 改寫 pattern

**前：**

```python
@router.get("/students")
def list_students(
    db: Session = Depends(get_db),
    current_user=Depends(require_permission(Permission.STUDENTS_READ)),
):
    q = db.query(Student)
    if current_user.role == "teacher":
        emp_id = current_user.employee_id
        q = q.join(Classroom).filter(
            or_(Classroom.head_teacher_id == emp_id, ...)
        )
    return q.all()
```

**後：**

```python
@router.get("/students")
def list_students(
    db: Session = Depends(get_db),
    scoped=Depends(require_scoped_permission(Permission.STUDENTS_READ)),
):
    user, grant = scoped
    q = db.query(Student)
    clause = student_scope.filter_clause(user, grant.scope)
    if clause is not None:
        q = q.filter(clause)
    return q.all()
```

### 4. Portal 端 (`api/portal/*`)

Portal 端 by definition own_class（教師自助頁面），不查 grant，直接：

```python
clause = student_scope.filter_clause(current_user, "own_class")
q = q.filter(clause)
```

**目的**：portal 與 admin 共用同一個 `student_scope.filter_clause`，未來規則變動只改一處。

### 5. 防呆：runtime scope 驗證

`resolve_grant` 在 runtime（每個 request 解析時）查 `permission_definitions[code].scope_options` 確認 user 持有的 scope 在合法清單內，避免有人 DB 手動編輯塞 `STUDENTS_READ:owncampus` 之類錯字。**驗證失敗一律 raise 403 + log ERROR**，不要 silent 轉成 `:all`（fail-closed）。

同時 app **startup** 階段對 `Permission` enum 比對 `permission_definitions` seed：若 enum 名稱含 `STUDENTS_` / `PORTFOLIO_` / `DISMISSAL_CALLS_` / `CLASSROOMS_READ` / `ATTENDANCE_READ` 等 prefix 但該 row 的 `scope_options IS NULL`，log WARNING（**非 raise**，避免 dev 環境卡）— 防止日後加新權限忘記 seed `scope_options` 而 silent 退化為 binary。

### 6. 重複 grant 解析規則

若 `permission_names` 同時含 bare 與 scoped（如 `["STUDENTS_READ", "STUDENTS_READ:own_class"]`，可能是 admin 手動編輯失誤或 seed bug），`resolve_grant` **取較寬鬆者**（`all > own_class`）。理由：bare = `all` 通常是更高權限的覆寫；若 admin 真想降權，應把 bare 移除。Migration backfill 不會產生這種狀態，但 runtime 仍要明確規則。

### 7. Audit log（follow-up，本 spec 不實作）

在 `require_scoped_permission` 注入 audit context 紀錄 `grant.scope`，事後可問「誰在哪天用 own_class scope 看了多少 student」。列 follow-up。

---

## Frontend 設計

### 1. 類型重新生成

後端 `permission_definitions` schema 多 `scope_options: string[] | null` 後，跑：

```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
cd ~/Desktop/ivy-frontend && npm run gen:api
```

`User.permission_names` / `Role.permissions` 仍是 `string[]`，wire format 無 type 變動。

### 2. `src/utils/permissions.ts` 擴充

```ts
export function hasPermission(code: string): boolean {
  const names = userStore.permission_names || []
  if (names.includes('*')) return true
  if (names.includes(code)) return true                 // bare or no-scope
  return names.some(n => n.startsWith(`${code}:`))      // :own_class 也算
}

export function getPermissionScope(code: string): 'all' | 'own_class' | null {
  const names = userStore.permission_names || []
  if (names.includes('*')) return 'all'
  if (names.includes(code)) return 'all'                // bare = all（與後端一致）
  const scoped = names.find(n => n.startsWith(`${code}:`))
  return scoped ? (scoped.split(':')[1] as 'all' | 'own_class') : null
}
```

Router guard 與 sidebar 仍用 `hasPermission`，本 spec 不解 CLAUDE.md 提及「自訂權限對 router 無作用」（另一個方向）。

### 3. SettingsPermissionsTab.vue UX

每條權限是一列；對 `scope_options` 非空的權限，勾起來時下方縮排顯示 radio：

```
□ 學生管理 (檢視)             ← 未勾，radio 不渲染

☑ 學生管理 (檢視)
    ○ 全園學生
    ● 僅自班學生              ← 勾起來後縮排顯示 radio
```

UI 規則：

- 從 `permission_definitions[code].scope_options` 判斷該列是否要渲染 radio
- 序列化：`'STUDENTS_READ' + ':' + scope_value` → 進 `Role.permissions` / `User.permission_names`
- 反序列化：分割 `:` 還原成 checkbox 勾選狀態 + radio 選擇
- 預設值：第一次勾起來預設選 `own_class`（保守選項）
- 切換 role template 時：template 字串本身已含 scope（如 teacher 範本是 `STUDENTS_READ:own_class`），radio 跟著範本走

### 4. 元件可選用 `getPermissionScope`

少數元件可依 scope 收掉空 tab / 灰按鈕：

```ts
const scope = getPermissionScope('STUDENTS_READ')
const showAllStudentsTab = scope === 'all'   // own_class 隱藏「全園」分頁
```

多數元件不必動 — backend 已 filter，前端拿不到的 row 自然不渲染。

---

## Migration / Backfill

### 風險核心

現況：teacher 角色的 user 持有 bare `["STUDENTS_READ", ...]`，但 router hard-code `role == "teacher"` 強制 own_class → **隱式 = own_class**。

新規：bare 視為 `:all`。**若不 backfill 直接 ship，所有 teacher 從上線當下 suddenly 看到全園學生 → 隱私事故。**

→ 必須先做 data backfill migration，再 ship router 改寫，再撤掉 hard-code。3 階段嚴格順序。

### Alembic Migration（單一 head：`permscope01`）

```python
SCOPE_AWARE_CODES = (
    'STUDENTS_READ', 'STUDENTS_WRITE', 'STUDENTS_LIFECYCLE_WRITE',
    'PORTFOLIO_READ', 'PORTFOLIO_WRITE', 'PORTFOLIO_PUBLISH',
    'STUDENTS_HEALTH_READ', 'STUDENTS_HEALTH_WRITE',
    'STUDENTS_SPECIAL_NEEDS_READ', 'STUDENTS_SPECIAL_NEEDS_WRITE',
    'STUDENTS_MEDICATION_ADMINISTER',
    'DISMISSAL_CALLS_READ', 'DISMISSAL_CALLS_WRITE',
    'CLASSROOMS_READ', 'ATTENDANCE_READ',
)

def upgrade():
    # 1. schema：加欄
    op.add_column('permission_definitions',
        sa.Column('scope_options', ARRAY(sa.Text), nullable=True))

    # 2. seed：標 15 條為 scope-aware
    op.execute(f"""
        UPDATE permission_definitions
        SET scope_options = ARRAY['own_class','all']
        WHERE code = ANY(ARRAY{list(SCOPE_AWARE_CODES)!r})
    """)

    # 3. 內建 teacher role 預設權限 → bare 轉 :own_class
    op.execute(f"""
        UPDATE roles
        SET permissions = ARRAY(
          SELECT CASE
            WHEN p = ANY(ARRAY{list(SCOPE_AWARE_CODES)!r}) THEN p || ':own_class'
            ELSE p
          END
          FROM unnest(permissions) AS p
        )
        WHERE code = 'teacher' AND is_core = true
    """)

    # 4. 既有 teacher user → 同步
    op.execute(f"""
        UPDATE users
        SET permission_names = ARRAY(
          SELECT CASE
            WHEN p = ANY(ARRAY{list(SCOPE_AWARE_CODES)!r}) THEN p || ':own_class'
            ELSE p
          END
          FROM unnest(permission_names) AS p
        ),
        token_version = COALESCE(token_version, 0) + 1
        WHERE role = 'teacher'
          AND NOT ('*' = ANY(permission_names))
    """)

def downgrade():
    op.execute("""
        UPDATE users SET permission_names = ARRAY(
          SELECT split_part(p, ':', 1) FROM unnest(permission_names) AS p
        )
    """)
    op.execute("""
        UPDATE roles SET permissions = ARRAY(
          SELECT split_part(p, ':', 1) FROM unnest(permissions) AS p
        ) WHERE code = 'teacher' AND is_core = true
    """)
    op.drop_column('permission_definitions', 'scope_options')
```

### 部署順序（嚴格）

1. **Migration first**：`alembic upgrade head`。Router 仍 hard-code，行為不變。Teacher token_version bumped → 下次 request 自動刷新權限。
2. **Backend code ship**：`require_scoped_permission` + `services/scoping/*` 上 main，router 尚未改用。行為不變。
3. **Router 逐個改寫**：每改一個 PR，跑全套 pytest。
4. **驗收 grep**：確認 router 內無殘留三角 OR（allow-list：`services/scoping/`、`api/classrooms.py` CRUD 本身）。
5. **Frontend ship**：Settings UI radio + `getPermissionScope`。對既有 user 無破壞性。
6. **OpenAPI codegen + drift check 過 CI**。

### Rollback 計畫

若 prod 上線後發現 regression：

1. **App-level rollback only 不可行**：DB 內 `:own_class` 字串仍在，舊 code 純字串 includes 比對找不到 → 教師全部 403。
2. 必跑 `alembic downgrade -1` 剝掉 suffix（UPDATE 語句，無 schema lock，ETA ~1 分鐘）。
3. Release note 標 critical：app + DB 同步 rollback。

### Test fixture 影響

- admin fixture（`permission_names=["*"]`）不受影響
- teacher fixture：fixture 用 `Base.metadata.create_all` 不跑 alembic，沿用 bare code → 行為等同 `:all`，與 prod teacher（`:own_class`）不一致
- **對策**：新增 `make_teacher_user(permissions, scope='own_class')` fixture helper，明確標 scope；既有 fixture 視情況改寫，剩餘改不完的列 follow-up
- E2E：admin 用 `*`，不受影響

---

## Testing

### Unit — pure-function helpers（~25 test）

- `tests/test_permission_grant.py`：`resolve_grant` 各種 wire format / 防呆
- `tests/test_scoping_student.py` / `_classroom.py` / `_attendance.py`：scope=all → None / own_class 涵蓋三角 / 排除非三角 / `employee_id=None` raise

### Integration — router 行為回歸（每個改寫 router 1-3 test）

每個 router 加 `test_<router>_scope.py`：

- admin（`*`）打 list → 全園
- teacher（`:own_class`）打同 endpoint → 只自班
- teacher 想看 / 寫別班 student → 403 / 404（依 endpoint 慣例）

**Invariant**：每個改寫的 router 至少 1 個 test 證明 own_class 阻擋了該擋的 row。

### Migration test

`tests/test_alembic_permscope01.py`：

- `upgrade()` 後 teacher role 與 user `permissions/permission_names` 轉換正確、admin/hr/principal/accountant/parent 不受影響、token_version bumped
- `downgrade()` 完全回復

### Frontend（~12 vitest）

- `tests/utils/permissions.test.ts`：`hasPermission` 對 bare / `:own_class` / `:all` / wildcard 都 true；`getPermissionScope` 回對應值
- `tests/components/settings/SettingsPermissionsTab.test.ts`：有 `scope_options` 渲染 radio；無則只 checkbox；序列化 round-trip 不掉資料；切角色範本 radio 跟著走

### E2E（workspace `e2e/`）

新增 `e2e/specs/permission_scoping.spec.ts`：

- admin 登入 → `/students` → 看到 ≥ 2 個學生
- teacher 登入（**新 fixture：必須是某班 head_teacher**）→ `/students` → 看到 < 全園學生數
- teacher 點別班學生 detail URL → 403 / not-found

`globalSetup` 多備 `e2e_teacher` 帳號 + 至少 1 個非該 teacher 班的 student，fail-fast 預檢。

### CI grep gate

新增 `ci/permission_scoping_gate.sh`：

```bash
violations=$(grep -rn "Classroom\.head_teacher_id\s*==\|assistant_teacher_id\s*==.*art_teacher_id\s*==" \
    api/ --include="*.py" \
    | grep -v "services/scoping/" | grep -v "api/classrooms.py")
[ -z "$violations" ] || { echo "$violations"; exit 1; }
```

Allow-list：`services/scoping/*`（single source of truth）、`api/classrooms.py`（classroom CRUD 本身管理三角欄位）。

---

## Out of scope（明確不做）

- 多園區 / `:own_campus`（系統無此概念）
- 個別班指派（user_classroom_scope 中介表，留 Phase B）
- Policy engine（時間 / 多條件，留 Phase C）
- Parent 端（已走獨立 portal）
- admin / role hard-code 字串檢查（`role == "admin"` / `role == "teacher"` 等其他 use case；列 follow-up）
- Router guard 與自訂權限對齊（另一個方向：「sidebar/router 對 DB-driven 自訂權限無作用」CLAUDE.md 已記）
- Audit log 注入 scope context（列 follow-up，不擋 P0）

---

## 風險與不確定性

1. **行為穩定性依賴嚴格部署順序**：data backfill 必須先於 router 改寫；若 ops 失序執行，最差會發生 teacher 短暫看到全園。**緩解**：Release note 標 critical step order；rollback 計畫含 DB downgrade。
2. **Token bump 操作影響教師體驗**：所有 teacher 下次 request 會被踢到 login。**緩解**：Release note 通知 HR 與教師。
3. **`PermissionDefinition.scope_options` seed 漂移**：日後加新 scope-aware 權限若忘了 seed `scope_options`，會 silent 退化為 binary。**緩解**：startup 階段對 `Permission` enum 中名稱含 `STUDENTS_` / `PORTFOLIO_` / `DISMISSAL_CALLS_` / `CLASSROOMS_READ` / `ATTENDANCE_READ` 等 prefix 做 sanity check warning（非 raise，避免 dev 環境卡）。
4. **Custom role（is_core=false）中含 bare scope-aware code**：admin 已建立的自訂角色不會自動 backfill。**緩解**：migration 內 `LOG WARNING` 列出這類 role，admin 手動處理。
5. **Test fixture 與 prod 行為不一致**：fixture teacher 仍視為 bare → `:all`。**緩解**：建議寫 `make_teacher_user` helper；改不完的列 follow-up；不擋 ship。
6. **Performance**：`student_scope.filter_clause` 對 own_class 用 `IN (SELECT classroom_id FROM ...)` 子查詢。N+1 風險低（單一 query），但 prod 學生量大（< 1000）情況下無 perf 顧慮。

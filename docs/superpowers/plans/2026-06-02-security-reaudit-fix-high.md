# 資安 re-audit 3 個 High 修補計畫

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。每個 task TDD（先寫會失敗的回歸測試 → 修 → 綠 → commit）。Steps 用 `- [ ]`。

**Goal:** 修補 2026-06-02 re-audit 的 3 個 High：RA-HIGH-3（園長 guardian 越權）、RA-HIGH-1（scope fail-open，含前後端同步）、RA-HIGH-2 + RA-MED-2（限流可繞過 / fail-open）。

**Architecture:** 後端從 local main 開 worktree、前端從 local main 開 worktree（origin 落後過多，見 spec）。修完各自 merge 回 local main（non-ff），user 之後一起 push。純授權/限流邏輯修補，配回歸測試。

**Tech Stack:** FastAPI / SQLAlchemy / pytest（後端）、Vue3 + TS / vitest（前端）。

**對應**：`SECURITY_AUDIT.md` §2026-06-02 re-audit；spec `docs/superpowers/specs/2026-06-02-security-reaudit-design.md`。

**Branch base 決策（user 已確認）**：從 local main 開 worktree → merge 回 local main。修補前先 `git -C <worktree> status` 確認乾淨；用 `git -C <絕對路徑>` 強制 worktree（勿落 main）。後端 .py Edit 後 black hook 會重排，subagent 對既有檔用 `python3` string.replace surgical 改避免 cosmetic creep。

**已驗證的關鍵事實（implementer 可信用）：**
- 13 個 scope-aware code（DB `permission_definitions.scope_options` 非空）：`STUDENTS_READ` `STUDENTS_WRITE` `STUDENTS_HEALTH_READ` `STUDENTS_HEALTH_WRITE` `STUDENTS_LIFECYCLE_WRITE` `STUDENTS_MEDICATION_ADMINISTER` `STUDENTS_SPECIAL_NEEDS_READ` `STUDENTS_SPECIAL_NEEDS_WRITE` `PORTFOLIO_READ` `PORTFOLIO_WRITE` `PORTFOLIO_PUBLISH` `DISMISSAL_CALLS_READ` `DISMISSAL_CALLS_WRITE`。
- `_UNRESTRICTED_ROLES = {admin, hr, supervisor}`（不含 principal）；`ROLE_TEMPLATES["principal"]` 含 `GUARDIANS_WRITE`。
- `assert_student_access(session, current_user, student_id)` 在 `utils/portfolio_access.py:99`；對 unrestricted 放行、teacher/principal 限 `accessible_classroom_ids`。

---

## Task 1（RA-HIGH-3）：guardian 寫入端點補 `assert_student_access`

**Files:**
- Modify: `api/students.py`（`create_guardian`:1438 / `update_guardian`:1481 / `delete_guardian`:1534）
- Test: `tests/test_students_guardian_access.py`（新增）

設計：三端點對齊 READ 的 `assert_student_access`（保留 principal 對「自班學生」寫家長的能力，只擋跨班）。create 用 path `student_id`；update/delete 先撈 guardian 再用 `guardian.student_id`。

- [ ] **Step 1: 寫會失敗的回歸測試**

```python
# tests/test_students_guardian_access.py
"""RA-HIGH-3 回歸：principal/teacher 不可改非自班學生的 guardian。"""
import pytest
from fastapi.testclient import TestClient

# 依專案既有 conftest fixture 取得 client + 建立資料的 helper。
# 關鍵斷言：一個 principal（非 unrestricted、GUARDIANS_WRITE）對「不在其班級」的
# 學生 guardian 做 PATCH/DELETE/POST → 應 403（修補前為 200）。

def test_principal_cannot_update_other_class_guardian(client, principal_token, other_class_guardian):
    r = client.patch(
        f"/api/students/guardians/{other_class_guardian.id}",
        json={"phone": "0900000000"},
        headers={"Authorization": f"Bearer {principal_token}"},
    )
    assert r.status_code == 403

def test_principal_cannot_create_guardian_for_other_class_student(client, principal_token, other_class_student):
    r = client.post(
        f"/api/students/{other_class_student.id}/guardians",
        json={"name": "X", "relation": "父", "phone": "0900000001"},
        headers={"Authorization": f"Bearer {principal_token}"},
    )
    assert r.status_code == 403

def test_principal_cannot_delete_other_class_guardian(client, principal_token, other_class_guardian):
    r = client.delete(
        f"/api/students/guardians/{other_class_guardian.id}",
        headers={"Authorization": f"Bearer {principal_token}"},
    )
    assert r.status_code == 403
```

> 實作者：依 `tests/conftest.py` 既有 fixture 慣例補 `principal_token` / `other_class_*`（principal 不分配到該班；參考既有 guardian/student fixture）。若既有測試已有 principal-scope fixture，沿用。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd <worktree> && ./venv_sec/bin/python -m pytest tests/test_students_guardian_access.py -v`
Expected: 3 個皆 FAIL（目前回 200，無守衛）。

- [ ] **Step 3: 三端點加守衛**

`create_guardian`（1438）：在 `session.query(Student)...` 之後、操作前，把現有的 student 取得改為（或補上）：
```python
student = assert_student_access(session, current_user, student_id)
```
（取代原本 `session.query(Student).filter(Student.id == student_id).first()` + 404 區塊；`assert_student_access` 內已處理 404 與 403。）

`update_guardian`（1481）：撈到 `guardian` 後（None→404 之後）加：
```python
assert_student_access(session, current_user, guardian.student_id)
```

`delete_guardian`（1534）：撈到 `guardian` 後（None→404 之後）加：
```python
assert_student_access(session, current_user, guardian.student_id)
```

確認 `assert_student_access` 已 import（`from utils.portfolio_access import assert_student_access`；同檔 READ 端點已用，應已 import）。

- [ ] **Step 4: 跑測試確認綠 + 既有 students 測試無回歸**

Run: `./venv_sec/bin/python -m pytest tests/test_students_guardian_access.py tests/test_students.py -v`（students 測試檔名以實際為準）
Expected: 新測試 PASS；既有 students/guardian 測試無新增 fail。

- [ ] **Step 5: Commit**

```bash
git -C <worktree絕對路徑> add api/students.py tests/test_students_guardian_access.py
git -C <worktree絕對路徑> commit -m "fix(security): guardian 寫入端點補 assert_student_access（RA-HIGH-3）

園長(非 unrestricted 但有 GUARDIANS_WRITE)原可改全園任一家長 PII；
create/update/delete_guardian 對齊 READ 端點的 per-student scope 守衛。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2（RA-HIGH-1a）：`has_permission` 對非 scope-aware code 的 scope 後綴 fail-closed

**Files:**
- Modify: `utils/permissions.py`（`has_permission`:600-622；新增 `SCOPE_AWARE_CODES` 常數）
- Test: `tests/test_has_permission_scope.py`（新增）

- [ ] **Step 1: 寫會失敗的回歸測試**

```python
# tests/test_has_permission_scope.py
"""RA-HIGH-1：非 scope-aware code 的 :own_class 後綴不得被當全域放行。"""
from utils.permissions import has_permission

def test_non_scope_aware_own_class_is_fail_closed():
    # SALARY_READ 不在 scope_options → :own_class 後綴不應授予全域 SALARY_READ
    assert has_permission(["SALARY_READ:own_class"], "SALARY_READ") is False
    assert has_permission(["USER_MANAGEMENT_WRITE:own_class"], "USER_MANAGEMENT_WRITE") is False

def test_scope_aware_own_class_still_grants():
    # STUDENTS_READ 是 scope-aware → :own_class 仍視為持有（端點再做 row 過濾）
    assert has_permission(["STUDENTS_READ:own_class"], "STUDENTS_READ") is True

def test_bare_and_wildcard_unchanged():
    assert has_permission(["SALARY_READ"], "SALARY_READ") is True
    assert has_permission(["*"], "SALARY_READ") is True
    assert has_permission(None, "SALARY_READ") is False
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./venv_sec/bin/python -m pytest tests/test_has_permission_scope.py -v`
Expected: `test_non_scope_aware_own_class_is_fail_closed` FAIL（目前回 True）。

- [ ] **Step 3: 加 `SCOPE_AWARE_CODES` 常數 + 改 `has_permission`**

在 `utils/permissions.py` 靠近 `_SCOPE_AWARE_PREFIXES` 處新增（canonical in-code 集合，對齊 DB scope_options seed）：
```python
# Canonical scope-aware permission codes（對齊 DB permission_definitions.scope_options，
# permscope01-04 seed）。has_permission 只對這些 code 認 ":scope" 後綴；其餘 code 的
# scope 後綴 fail-closed（避免「想授本班、實際授全域」的 footgun，RA-HIGH-1）。
SCOPE_AWARE_CODES = frozenset({
    "STUDENTS_READ", "STUDENTS_WRITE", "STUDENTS_HEALTH_READ", "STUDENTS_HEALTH_WRITE",
    "STUDENTS_LIFECYCLE_WRITE", "STUDENTS_MEDICATION_ADMINISTER",
    "STUDENTS_SPECIAL_NEEDS_READ", "STUDENTS_SPECIAL_NEEDS_WRITE",
    "PORTFOLIO_READ", "PORTFOLIO_WRITE", "PORTFOLIO_PUBLISH",
    "DISMISSAL_CALLS_READ", "DISMISSAL_CALLS_WRITE",
})
```

把 `has_permission` 結尾兩行：
```python
    scope_prefix = f"{name}:"
    return any(p.startswith(scope_prefix) for p in user_perms)
```
改為：
```python
    if name in SCOPE_AWARE_CODES:
        scope_prefix = f"{name}:"
        return any(p.startswith(scope_prefix) for p in user_perms)
    return False
```

- [ ] **Step 4: 跑測試確認綠 + 既有權限測試無回歸**

Run: `./venv_sec/bin/python -m pytest tests/test_has_permission_scope.py -v && ./venv_sec/bin/python -m pytest -k "permission" -q`
Expected: 新測試 PASS；既有 permission 相關測試無新增 fail。

- [ ] **Step 5: 加 startup 一致性檢查（防 SCOPE_AWARE_CODES 與 DB 漂移）**

在 `check_scope_options_sanity` 內或啟動處補：若 DB scope_options 非空的 code 集合 ≠ `SCOPE_AWARE_CODES`，log WARNING（不擋啟動）。確保未來新增 scope-aware 權限時兩者同步。

- [ ] **Step 6: Commit**

```bash
git -C <worktree> add utils/permissions.py tests/test_has_permission_scope.py
git -C <worktree> commit -m "fix(security): has_permission 對非 scope-aware code 的 scope 後綴 fail-closed（RA-HIGH-1）

新增 canonical SCOPE_AWARE_CODES；SALARY_READ:own_class 類誤授不再放行全域。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3（RA-HIGH-1b）：`POST/PUT /api/auth/users` 驗證 permission_names 的 code/scope

**Files:**
- Modify: `api/auth.py`（user create/update 寫入 `permission_names` 前驗證；`_check_can_create_user` 附近 ~106-164）
- Test: `tests/test_user_permission_validation.py`（新增）

- [ ] **Step 1: 寫會失敗的回歸測試**

```python
# tests/test_user_permission_validation.py
"""RA-HIGH-1b：建立/更新 user 時，permission_names 的 scope 後綴只能掛在 scope-aware code 上。"""
# 以 admin token 呼叫 POST /api/auth/users：
#   permission_names=["SALARY_READ:own_class"] → 422（非 scope-aware code 不可帶 scope）
#   permission_names=["STUDENTS_READ:own_class"] → 200（合法）
#   permission_names=["NOT_A_CODE"] → 422（非法 code）
#   permission_names=["STUDENTS_READ:bogus"] → 422（scope 值非法）

def test_reject_scope_on_non_scope_aware_code(client, admin_token):
    r = client.post("/api/auth/users", headers={"Authorization": f"Bearer {admin_token}"},
                    json={"username":"u1","password":"x"*12,"role":"teacher",
                          "permission_names":["SALARY_READ:own_class"]})
    assert r.status_code == 422

def test_accept_scope_on_scope_aware_code(client, admin_token):
    r = client.post("/api/auth/users", headers={"Authorization": f"Bearer {admin_token}"},
                    json={"username":"u2","password":"x"*12,"role":"teacher",
                          "permission_names":["STUDENTS_READ:own_class"]})
    assert r.status_code in (200, 201)

def test_reject_unknown_code(client, admin_token):
    r = client.post("/api/auth/users", headers={"Authorization": f"Bearer {admin_token}"},
                    json={"username":"u3","password":"x"*12,"role":"teacher",
                          "permission_names":["NOT_A_CODE"]})
    assert r.status_code == 422
```

> 實作者：username/password/role 欄位與密碼長度限制以 `UserCreate` schema 實際為準調整；admin_token fixture 沿用既有。

- [ ] **Step 2: 跑測試確認失敗**

Run: `./venv_sec/bin/python -m pytest tests/test_user_permission_validation.py -v`
Expected: 至少 `test_reject_scope_on_non_scope_aware_code` 與 `test_reject_unknown_code` FAIL（目前原樣存）。

- [ ] **Step 3: 加驗證 helper + 在 create/update user 路徑呼叫**

在 `utils/permissions.py` 加純函式（便於單測 + 兩 caller 共用）：
```python
def validate_permission_names(names: list[str]) -> list[str]:
    """驗證 permission_names 每筆：base code 合法；scope 後綴只能掛 scope-aware code，
    scope 值 ∈ {own_class, all}。回傳非法項清單（空=全合法）。"""
    invalid: list[str] = []
    for n in names:
        if n == WILDCARD:
            continue
        base, _, scope = n.partition(":")
        if base not in Permission.__members__:
            invalid.append(n); continue
        if scope:
            if base not in SCOPE_AWARE_CODES or scope not in ("own_class", "all"):
                invalid.append(n)
    return invalid
```
在 `api/auth.py` 的 create_user 與 update_user 寫入 `permission_names` 前：
```python
if payload.permission_names is not None:
    bad = validate_permission_names(payload.permission_names)
    if bad:
        raise HTTPException(status_code=422, detail=f"非法權限項：{bad}")
```
（不破壞既有 `final_perms ⊆ caller_perms` 子集檢查；此為額外的格式/scope 驗證。）

- [ ] **Step 4: 跑測試確認綠 + 既有 auth 測試無回歸**

Run: `./venv_sec/bin/python -m pytest tests/test_user_permission_validation.py -v && ./venv_sec/bin/python -m pytest tests/test_auth.py -q`
Expected: 新測試 PASS；既有 auth 測試無新增 fail。

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add utils/permissions.py api/auth.py tests/test_user_permission_validation.py
git -C <worktree> commit -m "fix(security): user CRUD 驗證 permission_names code/scope（RA-HIGH-1b）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4（RA-HIGH-1c）：前端 `auth.ts` 同步 scope-aware 集合 + parity 測試

**Files（前端 worktree）:**
- Modify: `src/utils/auth.ts`（`hasPermission`:189 — scope 後綴只對 scope-aware code 認）
- Test: `tests/unit/utils/has-permission-scope.test.ts`（新增）；`tests/unit/utils/scope-aware-parity.test.ts`（新增，比對前後端集合）

設計：前端鏡像後端 `SCOPE_AWARE_CODES`，並補 parity 測試（對齊既有 PII denylist parity 模式），防未來單側漂移造成「UI 顯示有權、後端 403」。

- [ ] **Step 1: 寫會失敗的測試**

```typescript
// tests/unit/utils/has-permission-scope.test.ts
import { describe, it, expect, beforeEach } from 'vitest'
import { setUserInfo, hasPermission } from '@/utils/auth'

describe('hasPermission scope fail-closed (RA-HIGH-1c)', () => {
  it('non-scope-aware code with :own_class is NOT held', () => {
    setUserInfo({ permission_names: ['SALARY_READ:own_class'] } as any)
    expect(hasPermission('SALARY_READ')).toBe(false)
  })
  it('scope-aware code with :own_class IS held', () => {
    setUserInfo({ permission_names: ['STUDENTS_READ:own_class'] } as any)
    expect(hasPermission('STUDENTS_READ')).toBe(true)
  })
})
```

> setUserInfo 的實際 API 名稱以 `auth.ts` 既有 export 為準（檔頭有 reactive user info source）。

- [ ] **Step 2: 跑測試確認失敗**

Run: `cd <前端worktree> && npx vitest run tests/unit/utils/has-permission-scope.test.ts`
Expected: 第一個 it FAIL。

- [ ] **Step 3: 加 `SCOPE_AWARE_CODES` + 改 `hasPermission`**

在 `src/utils/auth.ts` 加（與後端同 13 筆）：
```typescript
// 與後端 utils/permissions.SCOPE_AWARE_CODES 同步；scope-aware-parity.test 守同步
export const SCOPE_AWARE_CODES: ReadonlySet<string> = new Set([
  'STUDENTS_READ','STUDENTS_WRITE','STUDENTS_HEALTH_READ','STUDENTS_HEALTH_WRITE',
  'STUDENTS_LIFECYCLE_WRITE','STUDENTS_MEDICATION_ADMINISTER',
  'STUDENTS_SPECIAL_NEEDS_READ','STUDENTS_SPECIAL_NEEDS_WRITE',
  'PORTFOLIO_READ','PORTFOLIO_WRITE','PORTFOLIO_PUBLISH',
  'DISMISSAL_CALLS_READ','DISMISSAL_CALLS_WRITE',
])
```
`hasPermission` 結尾的 `return perms.some((n) => n.startsWith(`${permissionName}:`))` 改為：
```typescript
  if (SCOPE_AWARE_CODES.has(permissionName)) {
    return perms.some((n) => n.startsWith(`${permissionName}:`))
  }
  return false
```

- [ ] **Step 4: 加 parity 測試**

```typescript
// tests/unit/utils/scope-aware-parity.test.ts
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { SCOPE_AWARE_CODES } from '@/utils/auth'

// 從後端 permissions.py 抓 SCOPE_AWARE_CODES 區塊比對（路徑相對 monorepo sibling）
describe('scope-aware code parity 前後端同步', () => {
  it('frontend SCOPE_AWARE_CODES 與後端一致', () => {
    const py = readFileSync(
      new URL('../../../../../ivy-backend/utils/permissions.py', import.meta.url), 'utf8')
    const block = py.match(/SCOPE_AWARE_CODES = frozenset\(\{([\s\S]*?)\}\)/)
    expect(block).not.toBeNull()
    const backend = new Set([...block![1].matchAll(/"([A-Z_]+)"/g)].map(m => m[1]))
    expect(new Set(SCOPE_AWARE_CODES)).toEqual(backend)
  })
})
```

> 路徑深度（`../`）以實際 test 檔位置調整；若 CI 環境無 sibling repo，改用 hardcoded 期望集合 + 註明手動同步（對齊 PII denylist parity 既有處理方式）。

- [ ] **Step 5: 跑測試 + typecheck**

Run: `npx vitest run tests/unit/utils/has-permission-scope.test.ts tests/unit/utils/scope-aware-parity.test.ts && npm run typecheck`
Expected: 全 PASS；typecheck 0 error。

- [ ] **Step 6: Commit（前端 worktree）**

```bash
git -C <前端worktree> add src/utils/auth.ts tests/unit/utils/has-permission-scope.test.ts tests/unit/utils/scope-aware-parity.test.ts
git -C <前端worktree> commit -m "fix(security): hasPermission 對非 scope-aware code fail-closed + parity 測試（RA-HIGH-1c）

對齊後端 SCOPE_AWARE_CODES，防修後端後前後端反向漂移。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5（RA-MED-2）：auth 限流 DB 失敗時不再全 fail-open

**Files:**
- Modify: `utils/rate_limit_db.py`（`count_recent_attempts`:105-140）
- Test: `tests/test_rate_limit_failclosed.py`（新增）

設計：`count_recent_attempts` 加 `fail_closed: bool = False` 參數。auth scope 的 caller（`api/auth.py` 登入 / 改密 / 重設、`api/parent_portal/auth.py` bind）傳 `fail_closed=True`：DB 失敗時改用 module 層 in-process backstop dict（per-worker，降級但非歸零），而非回 0。非 auth scope 維持 fail-open。

- [ ] **Step 1: 寫會失敗的測試**

```python
# tests/test_rate_limit_failclosed.py
"""RA-MED-2：DB 失敗時 auth scope 不應回 0（fail-open）。"""
from unittest.mock import patch
from utils import rate_limit_db

def test_auth_scope_fail_closed_on_db_error():
    # 模擬 DB 失敗：fail_closed=True 時應回 in-process backstop 計數（≥ 之前累積），非 0
    rate_limit_db.record_attempt_inproc("login_ip", "1.2.3.4")  # 先累積一筆（backstop）
    with patch.object(rate_limit_db, "_count_from_db", side_effect=Exception("db down")):
        n = rate_limit_db.count_recent_attempts("login_ip", "1.2.3.4", within_seconds=900, fail_closed=True)
    assert n >= 1  # 修補前為 0

def test_non_auth_scope_still_fail_open():
    with patch.object(rate_limit_db, "_count_from_db", side_effect=Exception("db down")):
        n = rate_limit_db.count_recent_attempts("public_register", "1.2.3.4", within_seconds=60)
    assert n == 0
```

> 實作者：依現有 `count_recent_attempts` 內部結構，把 DB 查詢抽成 `_count_from_db` 便於 mock；`record_attempt_inproc` 為新的 in-process backstop helper（復活 api/auth.py 既有的死碼 dict 概念，集中到 rate_limit_db）。scope 名稱以實際常數為準。

- [ ] **Step 2: 跑測試確認失敗** → `./venv_sec/bin/python -m pytest tests/test_rate_limit_failclosed.py -v`，Expected FAIL。

- [ ] **Step 3: 實作 in-process backstop + fail_closed 分支**

`count_recent_attempts` 的 `except` 區塊：`fail_closed=True` 時回 `_count_recent_inproc(scope, key, within_seconds)`；否則維持 `return 0`。新增 module 層 `_inproc: dict[str, list[float]]` + `record_attempt_inproc` / `_count_recent_inproc`（滑動視窗，沿用 `time.monotonic()`）。`record_attempt` 在 auth scope 同時寫 DB 與 in-process。auth scope 的 caller（api/auth.py `_check_ip_rate_limit` 等 + parent bind）傳 `fail_closed=True`。

- [ ] **Step 4: 跑測試確認綠 + 既有 rate limit 測試無回歸**

Run: `./venv_sec/bin/python -m pytest tests/test_rate_limit_failclosed.py tests/test_rate_limit_pg.py -v`
Expected: 新測試 PASS；既有無回歸。

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add utils/rate_limit_db.py api/auth.py tests/test_rate_limit_failclosed.py
git -C <worktree> commit -m "fix(security): auth 限流 DB 失敗改用 in-process backstop 不再歸零（RA-MED-2）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6（RA-HIGH-2）：`get_client_ip` 不信任未設定的 proxy（XFF 防偽造）

**Files:**
- Modify: `utils/request_ip.py`
- Test: `tests/test_get_client_ip.py`（新增或補充）

設計：先讀 `utils/request_ip.py` 現行 `get_client_ip` 與 `config/network.trusted_proxy_ips`（預設 `"*"`）。修法：當 `trusted_proxy_ips` 為 `"*"` 或空（未明設可信代理）時，**不信任 XFF**，直接回 `request.client.host`（直連 peer）；只有明設可信代理網段時才解析 XFF，且取「可信代理鏈外的最右一跳」。並在 `.env.example` 註明 prod 必設 `TRUSTED_PROXY_IPS` 為實際 edge 網段。

- [ ] **Step 1: 寫會失敗的測試**

```python
# tests/test_get_client_ip.py
"""RA-HIGH-2：未設可信代理時，偽造 X-Forwarded-For 不應被採信。"""
from utils.request_ip import get_client_ip

class _Req:
    def __init__(self, peer, xff=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}

def test_spoofed_xff_ignored_when_no_trusted_proxy(monkeypatch):
    # 預設 trusted_proxy_ips="*"（未明設）→ 應回直連 peer，不採信偽造 XFF
    req = _Req(peer="203.0.113.9", xff="1.1.1.1, 2.2.2.2")
    assert get_client_ip(req) == "203.0.113.9"
```

> 實作者：依 `get_client_ip` 實際簽章（接 Request 物件）調整 stub；若已有測試檔則補 case。

- [ ] **Step 2: 跑測試確認失敗** → Expected FAIL（目前會採信偽造 XFF 的某一跳）。

- [ ] **Step 3: 修 `get_client_ip`** — 未設可信代理（`"*"`/空）時忽略 XFF 回直連 peer；明設時取可信鏈外最右跳。`.env.example` 補 `TRUSTED_PROXY_IPS` 註解。

- [ ] **Step 4: 跑測試確認綠 + 既有 ip/限流測試無回歸**

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add utils/request_ip.py tests/test_get_client_ip.py .env.example
git -C <worktree> commit -m "fix(security): 未設可信代理時忽略 X-Forwarded-For 防偽造繞過限流（RA-HIGH-2）

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## 收尾（所有 task 完成後）

- [ ] 後端 worktree 跑 focused 套件確認零回歸：`./venv_sec/bin/python -m pytest tests/test_students_guardian_access.py tests/test_has_permission_scope.py tests/test_user_permission_validation.py tests/test_rate_limit_failclosed.py tests/test_get_client_ip.py -q` + 抽跑既有 `tests/test_auth.py`、`tests/test_students.py`。
- [ ] 前端 worktree：`npx vitest run` 相關檔 + `npm run typecheck`。
- [ ] 各自 merge 回 local main（non-ff）；`SECURITY_AUDIT.md` 對應 RA-HIGH-1/2/3 + RA-MED-2 標記「已修 + 對應檔案」。
- [ ] 向 user 報告：哪些已修、prod 待補項（設 `TRUSTED_PROXY_IPS`、`alembic` 無新 migration、scope grant prod 查核），等 user push。

## Self-Review（plan vs 報告覆蓋）
- RA-HIGH-3 → Task 1 ✅
- RA-HIGH-1（has_permission / user CRUD / 前端同步）→ Task 2/3/4 ✅
- RA-MED-2（auth 限流 fail-open）→ Task 5 ✅
- RA-HIGH-2（XFF 繞過）→ Task 6 ✅
- 跨端漂移風險（advisor flag）→ Task 4 parity 測試 ✅

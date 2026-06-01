# 教師端後台檢視重構：雙模式 + 稽核歸屬 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把後台「以老師身份進入教師端」從單一完整模擬，拆成「預覽（唯讀）/ 代為操作（可寫）」兩個受控模式，並讓模擬期間每筆寫入都能追回發起的 admin。

**Architecture:** 沿用現有 impersonation 機制（方案 1）。模擬 token 多帶 `impersonated_by` / `impersonated_by_name` / `impersonation_mode` 三個 claim；新增一支全站 middleware 在 `readonly` 模式擋掉所有寫入 method；audit 寫入點從 token 解出 impersonation 並寫進 `AuditLog` 兩個新欄位；`/impersonate` 端點依 mode 用新 Permission 守衛分流（admin 經 WILDCARD 通過兩者、園長僅 `PORTAL_PREVIEW`）；refresh 端點拒絕帶 impersonation claim 的 token 換發。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（PostgreSQL）後端；Vue 3 + Vite + TypeScript 前端；pytest / vitest。

**Spec:** `docs/superpowers/specs/2026-06-01-portal-impersonation-dual-mode-design.md`

---

## 執行環境須知（每個 task 都適用）

- **Worktree**：本計畫應在從 `origin/main` 開的 worktree 內執行（不要從 local main，避免把 user 並行 WIP commit 拉進來）。
- **後端 worktree commit**：subagent 一律用 `git -C /absolute/path/to/backend-worktree ...`，並在動工前先 `git -C <worktree> branch --show-current` 驗證分支。
- **黑魔法 black hook**：ivy-backend 對 `.py` 的 Edit/Write 會觸發 PostToolUse black 全檔重排，surgical 修既有檔案會產生 +60/-30 cosmetic creep。**修既有 .py 檔請用 Bash `python3` 做字串替換**（精準改、不觸發整檔重排）；新建檔案可正常 Write。
- **alembic head 漂移**：Task 2 寫 migration 前，先在 worktree 跑 `python -m alembic heads` 確認當前 head。本計畫撰寫時 head 為 `permscope01`；若已變動，`down_revision` 用實際 head。
- **前端 OpenAPI 型別**：後端端點/schema 改完後，前端 Task 8 需重新 regen：先在後端 worktree `python scripts/dump_openapi.py`，再在前端 `npm run gen:api`，只 commit 前端 `schema.d.ts`。
- **前後端分開 commit**：後端 commit 一批、前端 commit 一批（不同 repo）。

---

## File Structure

### 後端（ivy-backend）
| 檔案 | 動作 | 責任 |
|------|------|------|
| `utils/permissions.py` | 修改 | 新增 2 個 Permission enum + PERMISSION_LABELS + principal 模板 |
| `models/audit.py` | 修改 | `AuditLog` 新增 `impersonated_by` / `impersonated_by_name` 兩欄 |
| `alembic/versions/portalimp01_audit_impersonated_by.py` | 新建 | 兩欄 migration（nullable，無 backfill） |
| `utils/audit.py` | 修改 | 新增 `_extract_impersonation_from_header`；3 處寫入點補兩欄 |
| `api/auth.py` | 修改 | `/impersonate` 加 `mode` + 權限分流 + token claim；`/refresh` 拒絕 impersonation token |
| `utils/readonly_guard.py` | 新建 | `ReadonlyImpersonationMiddleware`：readonly 模式擋寫入 |
| `main.py` | 修改 | 註冊 `ReadonlyImpersonationMiddleware` |
| `tests/test_portal_impersonation_dual_mode.py` | 新建 | 端點 mode/權限/claim/防呆 測試 |
| `tests/test_readonly_impersonation_guard.py` | 新建 | 全站唯讀守衛測試 |
| `tests/test_audit_impersonation_attribution.py` | 新建 | audit 歸屬測試 |
| `tests/test_refresh_blocks_impersonation.py` | 新建 | refresh 拒絕 impersonation token 測試 |

### 前端（ivy-frontend）
| 檔案 | 動作 | 責任 |
|------|------|------|
| `src/api/_generated/schema.d.ts` | regen | OpenAPI 型別 |
| `src/api/auth.ts` | 修改 | `impersonate(employeeId, mode)` |
| `src/components/layout/AdminHeader.vue` | 修改 | 進入前台時選模式（園長只見預覽） |
| `src/layouts/PortalLayout.vue` | 修改 | 常駐橫幅 + 唯讀禁用寫入 |
| `src/views/.../AuditLogView.vue` | 修改 | 顯示「代操作：<admin>」 |
| 對應 `*.test.ts` | 新建/修改 | vitest |

---

## 後端

### Task 1：新增兩個 Permission + label + principal 模板

**Files:**
- Modify: `utils/permissions.py`（enum 約 `:95` ROLES_MANAGE 後、PERMISSION_LABELS 約 `:428`、principal 模板約 `:317`）
- Test: `tests/test_portal_impersonation_dual_mode.py`

- [ ] **Step 1: 寫失敗測試（權限模板分流）**

新建 `tests/test_portal_impersonation_dual_mode.py`，先放這段：

```python
from utils.permissions import (
    Permission,
    ROLE_TEMPLATES,
    PERMISSION_LABELS,
    has_permission,
)


def test_new_portal_permissions_exist():
    assert Permission.PORTAL_PREVIEW.value == "PORTAL_PREVIEW"
    assert Permission.PORTAL_IMPERSONATE.value == "PORTAL_IMPERSONATE"
    assert "PORTAL_PREVIEW" in PERMISSION_LABELS
    assert "PORTAL_IMPERSONATE" in PERMISSION_LABELS


def test_principal_has_preview_not_impersonate():
    principal_perms = ROLE_TEMPLATES["principal"]
    assert Permission.PORTAL_PREVIEW.value in principal_perms
    assert Permission.PORTAL_IMPERSONATE.value not in principal_perms


def test_admin_wildcard_passes_both():
    admin_perms = ROLE_TEMPLATES["admin"]  # ["*"]
    assert has_permission(admin_perms, Permission.PORTAL_PREVIEW)
    assert has_permission(admin_perms, Permission.PORTAL_IMPERSONATE)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_portal_impersonation_dual_mode.py -v`
Expected: FAIL（`AttributeError: PORTAL_PREVIEW`）

- [ ] **Step 3: 加 enum 值**（用 `python3` 字串替換避免 black 全檔重排）

在 `utils/permissions.py` 的 `Permission` enum 內，`ROLES_MANAGE = "ROLES_MANAGE"` 那行**後面**插入：

```python
    # 教師端後台檢視：預覽（唯讀）/ 代為操作（可寫）
    PORTAL_PREVIEW = "PORTAL_PREVIEW"
    PORTAL_IMPERSONATE = "PORTAL_IMPERSONATE"
```

- [ ] **Step 4: 加 PERMISSION_LABELS**

在 `PERMISSION_LABELS` 的 `"ROLES_MANAGE": "角色與權限管理",` 那行後插入：

```python
    "PORTAL_PREVIEW": "預覽教師端",
    "PORTAL_IMPERSONATE": "代為操作教師端",
```

- [ ] **Step 5: 加進 principal 模板**

把 `ROLE_TEMPLATES["principal"]` 區塊改為（在既有 list 末尾加一條，admin 不必動——其模板是 `["*"]` 已涵蓋）：

```python
ROLE_TEMPLATES["principal"] = ROLE_TEMPLATES["supervisor"] + [
    Permission.SALARY_READ.value,
    Permission.AUDIT_LOGS.value,
    Permission.GOV_REPORTS_EXPORT.value,
    Permission.PORTAL_PREVIEW.value,  # 園長可預覽老師教師端（唯讀）
]
```

- [ ] **Step 6: 跑測試確認通過**

Run: `pytest tests/test_portal_impersonation_dual_mode.py -v`
Expected: PASS（3 個 test）

- [ ] **Step 7: Commit**

```bash
git -C <worktree> add utils/permissions.py tests/test_portal_impersonation_dual_mode.py
git -C <worktree> commit -m "feat(perm): 新增 PORTAL_PREVIEW / PORTAL_IMPERSONATE 權限與園長模板"
```

---

### Task 2：AuditLog 兩個新欄位 + alembic migration

**Files:**
- Modify: `models/audit.py:46`（`user_agent_hash` 之前/後）
- Create: `alembic/versions/portalimp01_audit_impersonated_by.py`
- Test: `tests/test_audit_impersonation_attribution.py`

- [ ] **Step 1: 寫失敗測試（model 有欄位）**

新建 `tests/test_audit_impersonation_attribution.py`：

```python
from models.audit import AuditLog


def test_auditlog_has_impersonation_columns():
    cols = AuditLog.__table__.columns.keys()
    assert "impersonated_by" in cols
    assert "impersonated_by_name" in cols
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_audit_impersonation_attribution.py::test_auditlog_has_impersonation_columns -v`
Expected: FAIL

- [ ] **Step 3: model 加兩欄**

在 `models/audit.py` 的 `session_id` Column 之後（class 內最後）加：

```python
    impersonated_by = Column(
        Integer,
        nullable=True,
        comment="若為模擬寫入，發起的 admin user_id（actor 仍為 user_id 老師）",
    )
    impersonated_by_name = Column(
        Text,
        nullable=True,
        comment="若為模擬寫入，發起的 admin 顯示名",
    )
```

（`Integer` / `Text` 已在檔案頂 import，無需新增 import。）

- [ ] **Step 4: 確認當前 alembic head**

Run: `python3 -m alembic heads`
Expected: 單一 head。**worktree off origin/main 實測為 `eb0d4cf88f26`**（merge 11 heads after 5/26-5/29 batch）。若實測值不同，以實測為準作為 `down_revision`。

- [ ] **Step 5: 新建 migration 檔**

新建 `alembic/versions/portalimp01_audit_impersonated_by.py`（`down_revision` = Step 4 實測 head `eb0d4cf88f26`）：

```python
"""audit_logs 加 impersonated_by / impersonated_by_name

Revision ID: portalimp01
Revises: eb0d4cf88f26
Create Date: 2026-06-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision = "portalimp01"
down_revision: Union[str, Sequence[str], None] = "eb0d4cf88f26"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("impersonated_by", sa.Integer(), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("impersonated_by_name", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_logs", "impersonated_by_name")
    op.drop_column("audit_logs", "impersonated_by")
```

- [ ] **Step 6: 跑測試 + 確認單一 head**

Run: `pytest tests/test_audit_impersonation_attribution.py::test_auditlog_has_impersonation_columns -v`
Expected: PASS
Run: `python -m alembic heads`
Expected: 單一 head `portalimp01`（無多 head）

- [ ] **Step 7: Commit**

```bash
git -C <worktree> add models/audit.py alembic/versions/portalimp01_audit_impersonated_by.py tests/test_audit_impersonation_attribution.py
git -C <worktree> commit -m "feat(audit): AuditLog 加 impersonated_by / impersonated_by_name 欄位 + migration"
```

---

### Task 3：audit 寫入點補 impersonation 歸屬

**Files:**
- Modify: `utils/audit.py`（新增 helper；middleware payload `:810`、`write_audit_in_session` `:580`、`write_explicit_audit` `:641`）
- Test: `tests/test_audit_impersonation_attribution.py`

- [ ] **Step 1: 寫失敗測試（helper 解析）**

在 `tests/test_audit_impersonation_attribution.py` 追加：

```python
from starlette.requests import Request
from utils.auth import create_access_token
from utils.audit import _extract_impersonation_from_header


def _req_with_cookie(token: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(b"cookie", f"access_token={token}".encode())],
    }
    return Request(scope)


def test_extract_impersonation_from_token():
    token = create_access_token(
        {
            "user_id": 5,
            "employee_id": 5,
            "role": "teacher",
            "name": "老師A",
            "impersonated_by": 1,
            "impersonated_by_name": "王小明",
            "impersonation_mode": "write",
        }
    )
    by, name = _extract_impersonation_from_header(_req_with_cookie(token))
    assert by == 1
    assert name == "王小明"


def test_extract_impersonation_none_for_normal_token():
    token = create_access_token(
        {"user_id": 5, "employee_id": 5, "role": "teacher", "name": "老師A"}
    )
    by, name = _extract_impersonation_from_header(_req_with_cookie(token))
    assert by is None
    assert name is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_audit_impersonation_attribution.py -k extract -v`
Expected: FAIL（`_extract_impersonation_from_header` 不存在）

- [ ] **Step 3: 新增 helper**

在 `utils/audit.py` 的 `_extract_user_from_header` 函式之後新增：

```python
def _extract_impersonation_from_header(request: Request):
    """從 access_token 靜默解出模擬歸屬：(impersonated_by, impersonated_by_name)。

    一般登入 token 無此 claim → 回 (None, None)。走 decode_token_for_audit
    （verify_exp=False、multi-key 容忍），與 _extract_user_from_header 一致。
    """
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth.split(" ", 1)[1]
    if not token:
        return None, None

    from utils.auth import decode_token_for_audit

    payload = decode_token_for_audit(token) or {}
    return payload.get("impersonated_by"), payload.get("impersonated_by_name")
```

- [ ] **Step 4: 三處寫入點補欄位**

在以下三處的 `payload = dict(...)` / `AuditLog(...)` 加上兩個欄位。各處在取得 `user_id` 之後呼叫 helper：

(a) `AuditMiddleware.dispatch`（約 `:776` 取 user_id 之後）：

```python
            user_id, username = _extract_user_from_header(request)
            impersonated_by, impersonated_by_name = _extract_impersonation_from_header(request)
```
並在該段 `payload = dict(` 內加：
```python
                impersonated_by=impersonated_by,
                impersonated_by_name=impersonated_by_name,
```

(b) `write_audit_in_session`（約 `:562` 取 user_id 之後）：

```python
    user_id, username = _extract_user_from_header(request)
    impersonated_by, impersonated_by_name = _extract_impersonation_from_header(request)
```
並在 `AuditLog(` 建構子加：
```python
        impersonated_by=impersonated_by,
        impersonated_by_name=impersonated_by_name,
```

(c) `write_explicit_audit`（約 `:622` 取 user_id 之後）：同 (a) 取值，並在其 `payload = dict(` 加兩欄。

- [ ] **Step 5: 跑 helper 測試確認通過**

Run: `pytest tests/test_audit_impersonation_attribution.py -k extract -v`
Expected: PASS

- [ ] **Step 6: 端到端 audit 測試**

在 `tests/test_audit_impersonation_attribution.py` 追加（用 TestClient 模擬一個帶 impersonation claim 的 token 打一個會 audit 的寫入端點，斷言 DB 最新 audit row 的 `impersonated_by`）。實作時依既有 audit 整合測試慣例（參考 `tests/test_audit_*.py` 既有 fixture：`client`、登入取 cookie、`get_session` 查 `AuditLog`）：

```python
def test_write_under_impersonation_stamps_admin(client, db_session):
    # 構造帶 impersonation(write) claim 的 token，set 為 access_token cookie，
    # 打一個「會被 audit」的 portal 寫入端點。
    # ⚠ 必須用 ENTITY_PATTERNS 內有列的路徑，否則 _parse_entity_type 回 None、
    #    middleware 短路 → 完全不產生 AuditLog row（測試會因錯誤原因失敗）。
    #    已 audit 的 portal 寫入路徑：POST /api/portal/my-overtimes、
    #    POST /api/portal/my-leaves、/api/portal/swap-requests(對應 r"/api/portal/swap")、
    #    /api/portal/contact-book。本測試用 POST /api/portal/my-overtimes。
    # 然後查最新 AuditLog row：
    #   assert row.user_id == <老師 user_id>
    #   assert row.impersonated_by == <admin user_id>
    #   assert row.impersonated_by_name == "<admin name>"
    ...
```

> 註：此 test 與 Task 4（token 簽發）、Task 5（唯讀守衛放行 write）相依，可在 Task 5 後補齊斷言；先放 skeleton + `@pytest.mark.xfail(reason="待 Task 4/5")` 或留到 Task 5 一起綠。

- [ ] **Step 7: Commit**

```bash
git -C <worktree> add utils/audit.py tests/test_audit_impersonation_attribution.py
git -C <worktree> commit -m "feat(audit): 寫入時從 token 解出 impersonated_by 一併記錄"
```

---

### Task 4：`/impersonate` 端點 mode 參數 + 權限分流 + token claim

**Files:**
- Modify: `api/auth.py:360`（`ImpersonateRequest` schema 加 `mode`）、`:393-461`（端點權限 + token claim）
- Test: `tests/test_portal_impersonation_dual_mode.py`

- [ ] **Step 1: 寫失敗測試（mode 分流 + claim）**

在 `tests/test_portal_impersonation_dual_mode.py` 追加（依既有 auth 測試 fixture 慣例：`client`、admin 登入、`principal` 帳號 fixture、目標教師 fixture）：

```python
def test_admin_readonly_impersonate_sets_mode_claim(client, admin_login, target_teacher):
    resp = client.post("/api/auth/impersonate",
                       json={"employee_id": target_teacher.id, "mode": "readonly"})
    assert resp.status_code == 200
    # 解 access_token cookie，斷言 impersonation_mode == "readonly"、impersonated_by == admin.user_id

def test_admin_write_impersonate_allowed(client, admin_login, target_teacher):
    resp = client.post("/api/auth/impersonate",
                       json={"employee_id": target_teacher.id, "mode": "write"})
    assert resp.status_code == 200

def test_principal_cannot_write_impersonate(client, principal_login, target_teacher):
    resp = client.post("/api/auth/impersonate",
                       json={"employee_id": target_teacher.id, "mode": "write"})
    assert resp.status_code == 403

def test_principal_can_readonly_impersonate(client, principal_login, target_teacher):
    resp = client.post("/api/auth/impersonate",
                       json={"employee_id": target_teacher.id, "mode": "readonly"})
    assert resp.status_code == 200

def test_default_mode_is_readonly(client, admin_login, target_teacher):
    resp = client.post("/api/auth/impersonate", json={"employee_id": target_teacher.id})
    assert resp.status_code == 200
    # 斷言 token claim impersonation_mode == "readonly"

def test_cannot_impersonate_admin_preserved(client, admin_login, other_admin_employee):
    resp = client.post("/api/auth/impersonate",
                       json={"employee_id": other_admin_employee.id, "mode": "readonly"})
    assert resp.status_code == 403

def test_cannot_reimpersonate_while_impersonating(client, write_impersonation_cookie, target_teacher2):
    # 防巢狀模擬：access_token 已帶 impersonated_by（write 模式才繞得過唯讀守衛）→ 409
    resp = client.post("/api/auth/impersonate",
                       json={"employee_id": target_teacher2.id, "mode": "write"},
                       cookies={"access_token": write_impersonation_cookie})
    assert resp.status_code == 409
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_portal_impersonation_dual_mode.py -k impersonate -v`
Expected: FAIL（mode 未支援 / 園長未被擋）

- [ ] **Step 3: schema 加 mode**

`api/auth.py` 的 `ImpersonateRequest`（約 `:378`，現為 `employee_id: int`）加欄位：

```python
class ImpersonateRequest(BaseModel):
    employee_id: int
    mode: Literal["readonly", "write"] = "readonly"
```

（確認檔頂有 `from typing import Literal`；若無則加 import。）

- [ ] **Step 4: 端點權限分流 + claim**

在 `impersonate_user`（`:393`）內，把現有 `if current_user.get("role") != "admin": raise 403` 改為「防巢狀模擬 → 依 mode 用 Permission 守衛」。`current_user` 即進入當下 access_token 的持有者（採「退出再進入」後恆為發起 admin）：

```python
    from utils.permissions import Permission, has_permission

    # 防巢狀模擬：已在模擬中（access_token 帶 impersonated_by）要求先退出。
    # 置於權限閘之前，回 409 比 403 語意清楚（避免用被模擬老師的權限判斷）。
    if current_user.get("impersonated_by") is not None:
        raise HTTPException(status_code=409, detail="請先退出目前模擬再切換")

    user_perms = current_user.get("permission_names")
    required = (
        Permission.PORTAL_IMPERSONATE
        if data.mode == "write"
        else Permission.PORTAL_PREVIEW
    )
    if not has_permission(user_perms, required):
        raise HTTPException(status_code=403, detail="您沒有此功能的存取權限")
```

並把簽發 token 的 `create_access_token({...})`（`:452`）payload 加三個 claim：

```python
        target_token = create_access_token(
            {
                "user_id": target_user.id,
                "employee_id": target_user.employee_id,
                "role": target_user.role,
                "name": target_emp.name,
                "permission_names": permission_names,
                "token_version": target_user.token_version,
                "impersonated_by": current_user.get("user_id"),
                "impersonated_by_name": current_user.get("name"),
                "impersonation_mode": data.mode,
            }
        )
```

並把 audit summary（`:481`）改為含模式：

```python
        _mode_label = "代操作" if data.mode == "write" else "預覽"
        request.state.audit_summary = (
            f"[{_mode_label}] 操作者 {current_user.get('name')}"
            f"（user_id={current_user.get('user_id')}）"
            f" {'切換為' if data.mode == 'write' else '檢視'} {target_emp.name}"
            f"（user_id={target_user.id}）"
        )
```

> 既有防呆（不能模擬 admin / 停用 / 離職）保留不動。

- [ ] **Step 5: 跑測試確認通過**

Run: `pytest tests/test_portal_impersonation_dual_mode.py -k impersonate -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git -C <worktree> add api/auth.py tests/test_portal_impersonation_dual_mode.py
git -C <worktree> commit -m "feat(auth): impersonate 加 mode 參數、權限分流與 impersonation token claim"
```

---

### Task 5：全站唯讀守衛 middleware

**Files:**
- Create: `utils/readonly_guard.py`
- Modify: `main.py:985`（middleware 註冊區）
- Test: `tests/test_readonly_impersonation_guard.py`

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_readonly_impersonation_guard.py`：

```python
def test_readonly_blocks_portal_write(client, readonly_impersonation_cookie):
    # 帶 readonly impersonation token，打 portal 寫入端點（守衛在 routing 前攔截，
    # 用真實存在的寫入路徑 POST /api/portal/my-overtimes）
    resp = client.post("/api/portal/my-overtimes", json={...},
                       cookies={"access_token": readonly_impersonation_cookie})
    assert resp.status_code == 403
    assert "唯讀" in resp.json()["detail"]

def test_readonly_blocks_nonportal_write(client, readonly_impersonation_cookie):
    resp = client.post("/api/employees", json={...},
                       cookies={"access_token": readonly_impersonation_cookie})
    assert resp.status_code == 403

def test_readonly_allows_get(client, readonly_impersonation_cookie):
    resp = client.get("/api/portal/home/summary",
                      cookies={"access_token": readonly_impersonation_cookie})
    assert resp.status_code != 403

def test_readonly_allows_end_impersonate(client, readonly_impersonation_cookie):
    resp = client.post("/api/auth/end-impersonate",
                       cookies={"access_token": readonly_impersonation_cookie})
    assert resp.status_code != 403

def test_write_mode_not_blocked(client, write_impersonation_cookie):
    resp = client.get("/api/portal/home/summary",
                      cookies={"access_token": write_impersonation_cookie})
    assert resp.status_code != 403
```

（`readonly_impersonation_cookie` / `write_impersonation_cookie` fixture：用 `create_access_token` 構造帶對應 `impersonation_mode` claim 的 token。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_readonly_impersonation_guard.py -v`
Expected: FAIL（寫入未被擋 → 非 403）

- [ ] **Step 3: 新建 middleware**

新建 `utils/readonly_guard.py`：

```python
"""唯讀模擬守衛：impersonation_mode==readonly 的 token 不可寫入任何端點。"""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

# readonly 身份仍須能退出模擬 / 登出
_EXEMPT_PATHS = {"/api/auth/end-impersonate", "/api/auth/logout"}


class ReadonlyImpersonationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method.upper() not in _MUTATING:
            return await call_next(request)
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        token = request.cookies.get("access_token")
        if not token:
            auth = request.headers.get("authorization", "")
            if auth.startswith("Bearer "):
                token = auth.split(" ", 1)[1]
        if token:
            from utils.auth import decode_token_for_audit

            payload = decode_token_for_audit(token) or {}
            if payload.get("impersonation_mode") == "readonly":
                logger.info(
                    "唯讀模擬擋下寫入：impersonated_by=%s path=%s method=%s",
                    payload.get("impersonated_by"),
                    request.url.path,
                    request.method,
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "唯讀預覽模式不可寫入"},
                )
        return await call_next(request)
```

- [ ] **Step 4: 註冊 middleware**

在 `main.py` middleware 區（`:985` 附近，`AuditMiddleware` 註冊處），加入。

**順序與語意（Starlette：最後加入 = 最外層 = 最先執行）**：把唯讀守衛加在 `app.add_middleware(AuditMiddleware)` **之後一行** → 它是最外層、最先執行。被它擋下的寫入直接回 403，**不會** propagate 進 AuditMiddleware，因此不產生 audit row（守衛自己 `logger.info` 留痕即可）。這是刻意決策：唯讀預覽被擋的寫入是 UX 防呆，不需灌進 audit_logs 變 BLOCKED 噪音。

> 另注意：`main.py:988` 既有 KillSwitch 的「維護/唯讀模式」是**不同機制**（全站 503），與本守衛（針對 impersonation token 的 403）無關，命名上勿混淆。

```python
from utils.readonly_guard import ReadonlyImpersonationMiddleware

app.add_middleware(ReadonlyImpersonationMiddleware)
```

（放在 `app.add_middleware(AuditMiddleware)` 之後一行。）

- [ ] **Step 5: 跑測試確認通過**

Run: `pytest tests/test_readonly_impersonation_guard.py -v`
Expected: PASS（5 個 test）

- [ ] **Step 6: 回填 Task 3 端到端 audit 測試**

回到 `tests/test_audit_impersonation_attribution.py::test_write_under_impersonation_stamps_admin`，移除 xfail、補齊斷言（write-mode token 打 portal 寫入 → 最新 AuditLog row `impersonated_by` == admin user_id）。
Run: `pytest tests/test_audit_impersonation_attribution.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git -C <worktree> add utils/readonly_guard.py main.py tests/test_readonly_impersonation_guard.py tests/test_audit_impersonation_attribution.py
git -C <worktree> commit -m "feat(auth): 全站唯讀模擬守衛 middleware"
```

---

### Task 6：refresh 拒絕 impersonation token

**Files:**
- Modify: `api/auth.py:757`（fallback 路徑 decode 之後）
- Test: `tests/test_refresh_blocks_impersonation.py`

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_refresh_blocks_impersonation.py`：

```python
from utils.auth import create_access_token


def test_refresh_rejects_impersonation_token(client):
    token = create_access_token(
        {
            "user_id": 5, "employee_id": 5, "role": "teacher", "name": "老師A",
            "token_version": 0,
            "impersonated_by": 1, "impersonated_by_name": "王小明",
            "impersonation_mode": "readonly",
        }
    )
    resp = client.post("/api/auth/refresh", cookies={"access_token": token})
    assert resp.status_code == 401
    # 確認沒有發回新的乾淨 access_token（不會被升級）
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_refresh_blocks_impersonation.py -v`
Expected: FAIL（目前會重建乾淨 token 回 200）

- [ ] **Step 3: 加拒絕邏輯**

在 `api/auth.py` refresh fallback 路徑、`payload = decode_token_allow_expired(token)`（`:757`）成功之後、`original_iat` 檢查之前，插入：

```python
    # 模擬 session 不可經 refresh 升級或洗掉歸屬 → 直接拒絕，請 admin 重新進入模擬
    if payload.get("impersonated_by") is not None:
        write_login_audit(
            request,
            action="TOKEN_REFRESH_FAILED",
            username=payload.get("name"),
            user_id=payload.get("user_id"),
            extras={"reason": "impersonation_token_not_refreshable"},
        )
        raise HTTPException(
            status_code=401,
            detail="模擬工作階段不可刷新，請重新進入模擬",
        )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_refresh_blocks_impersonation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add api/auth.py tests/test_refresh_blocks_impersonation.py
git -C <worktree> commit -m "feat(auth): refresh 拒絕帶 impersonation claim 的 token（防升級）"
```

---

### Task 7：後端全套回歸

- [ ] **Step 1: 跑相關模組全套**

Run: `pytest tests/test_portal_impersonation_dual_mode.py tests/test_readonly_impersonation_guard.py tests/test_audit_impersonation_attribution.py tests/test_refresh_blocks_impersonation.py tests/test_auth*.py tests/test_audit*.py -v`
Expected: 全 PASS

- [ ] **Step 2: 跑既有 auth / portal / audit 回歸確認零新增 fail**

Run: `pytest tests/ -k "auth or portal or audit or impersonat" -q`
Expected: 相對 main 無新增 fail（既有 pre-existing fail 不算）

- [ ] **Step 3:（可選）全套**

Run: `pytest -q`
Expected: 無新增 fail。記錄 pre-existing fail 數對照 baseline。

---

## 前端

### Task 8：OpenAPI 型別 regen + `impersonate(employeeId, mode)`

**Files:**
- regen: `src/api/_generated/schema.d.ts`
- Modify: `src/api/auth.ts:24`
- Test: `src/api/__tests__/auth.test.ts`（或既有 auth api test）

- [ ] **Step 1: regen 型別**

```bash
cd <backend-worktree> && python scripts/dump_openapi.py
cd <frontend-worktree> && npm run gen:api
```
只 `git add src/api/_generated/schema.d.ts`。

- [ ] **Step 2: 寫失敗測試**

在 auth api 既有 test 追加：斷言 `impersonate(5, 'readonly')` 送出 `POST /auth/impersonate` body `{ employee_id: 5, mode: 'readonly' }`（依既有 axios mock 慣例）。

- [ ] **Step 3: 改 api**

`src/api/auth.ts`（`:24`）：

```typescript
export const impersonate = (employeeId: number, mode: 'readonly' | 'write' = 'readonly') =>
  api.post('/auth/impersonate', { employee_id: employeeId, mode })
```

- [ ] **Step 4: 跑測試 + typecheck**

Run: `npm run test -- auth` 與 `npm run typecheck`
Expected: PASS / 0 error

- [ ] **Step 5: Commit**

```bash
git -C <frontend-worktree> add src/api/auth.ts src/api/_generated/schema.d.ts <test>
git -C <frontend-worktree> commit -m "feat(api): impersonate 加 mode 參數 + schema regen"
```

---

### Task 9：AdminHeader 進入前台時選模式（園長只見預覽）

**Files:**
- Modify: `src/components/layout/AdminHeader.vue:154-181`
- Test: 對應 `*.test.ts`

- [ ] **Step 1: 寫失敗測試**

斷言：(a) 進入前台流程出現「預覽 / 代操作」選擇；(b) 無 `PORTAL_IMPERSONATE` 權限者（園長）只渲染「預覽」選項；(c) 選定後呼叫 `impersonate(empId, 選定mode)`。

- [ ] **Step 2: 跑測試確認失敗** — `npm run test -- AdminHeader`，Expected: FAIL

- [ ] **Step 3: 實作**

在 `doImpersonate`（`:171-181`）前插入模式選擇（沿用既有「選擇瀏覽身份」dialog pattern，加一個 mode 選擇；用 `hasPermission('PORTAL_IMPERSONATE')` 決定是否顯示「代操作」選項，預設選「預覽」）。呼叫改為 `impersonate(emp.id, chosenMode)`。

- [ ] **Step 4: 跑測試 + typecheck + build** — Expected: PASS / 0 / success

- [ ] **Step 5: Commit**

```bash
git -C <frontend-worktree> commit -am "feat(portal): 後台進入前台可選預覽/代操作（園長僅預覽）"
```

---

### Task 10：PortalLayout 常駐橫幅 + 唯讀禁用寫入 + 移除壞掉的切換器

**Files:**
- Modify: `src/layouts/PortalLayout.vue:231-289`
- Test: 對應 `*.test.ts`

> 背景：依 spec §1 problem 5，in-portal 直接切換器今天就是壞的（`handleSwitchUser` 打 `/impersonate` 與模擬中 `fetchEmployees` 都會 403）。決策「退出再進入」→ 本 task 一併移除它。

- [ ] **Step 1: 寫失敗測試**

斷言：(a) `impersonation_mode==='readonly'` 渲染藍色「預覽中（唯讀）— <老師>」橫幅；(b) `==='write'` 渲染紅/橘色「代操作中（操作會被記錄）— <老師>」；(c) readonly 時寫入控制（送假/補打卡按鈕等）被隱藏或 disabled；(d) 兩模式皆有退出鈕；(e) 模擬中**不再渲染 in-portal 老師切換器**（switcher dropdown 不存在）。

- [ ] **Step 2: 跑測試確認失敗** — Expected: FAIL

- [ ] **Step 3: 實作**

(i) 在 PortalLayout 頂部加常駐橫幅元件，依當前模式（來源：登入/impersonate 回應 `user.impersonation_mode` 或 store）切換顏色/文案/對象；readonly 時透過 provide/store flag 讓子頁隱藏寫入控制（最小範圍：橫幅 + layout 層 `isReadonlyImpersonation` flag，子頁讀此 flag）。真正防線在後端 Task 5，前端僅 UX 防呆。
(ii) **移除** `handleSwitchUser`、模擬狀態下的 `fetchEmployees` 呼叫、以及切換器 UI（`showSwitcher` 相關 dropdown）。保留 `goBackToAdmin`（退出）。`showBackToAdmin` 維持。員工清單改由 AdminHeader（Task 9，admin 端 token 有效）負責，PortalLayout 不再抓。

- [ ] **Step 4: 跑測試 + typecheck + build** — Expected: PASS / 0 / success

- [ ] **Step 5: Commit**

```bash
git -C <frontend-worktree> commit -am "feat(portal): 教師端常駐模式橫幅 + 唯讀禁用寫入"
```

---

### Task 11：AuditLogView 顯示代操作者

**Files:**
- Modify: `src/views/.../AuditLogView.vue`（搜尋 `AuditLogView` 確定路徑）
- Test: 對應 `*.test.ts`

- [ ] **Step 1: 寫失敗測試**

斷言：audit row 的 `impersonated_by_name` 非空時，操作者欄顯示「<老師>（代操作：<admin>）」。

- [ ] **Step 2: 跑測試確認失敗** — Expected: FAIL

- [ ] **Step 3: 實作** — 在操作者欄渲染邏輯加 `impersonated_by_name` 條件顯示。

- [ ] **Step 4: 跑測試 + typecheck** — Expected: PASS / 0

- [ ] **Step 5: Commit**

```bash
git -C <frontend-worktree> commit -am "feat(audit): 後台操作紀錄顯示代操作者"
```

---

### Task 12：前端全套回歸

- [ ] **Step 1:** `npm run test` — Expected: 無新增 fail
- [ ] **Step 2:** `npm run typecheck` — Expected: 0 error
- [ ] **Step 3:** `npm run build` — Expected: success

---

## 整合驗證（Task 13）

- [ ] `./start.sh` 起兩端
- [ ] admin 進入前台 → 選「預覽」→ 確認橫幅藍色、寫入按鈕禁用、嘗試任一寫入被擋（403）
- [ ] admin 進入前台 → 選「代操作」→ 送一筆（如加班申請）→ 後台操作紀錄該筆顯示「老師（代操作：admin）」
- [ ] 以園長帳號 → 進入前台只有「預覽」選項、無「代操作」
- [ ] 退出模擬正常回後台
- [ ] （可選）模擬中讓 access_token 過期觸發 refresh → 確認回 401 而非偷偷續期

---

## Self-Review（撰寫者已核對）

- **Spec 覆蓋**：4.1 token claim→T4；4.2 Permission→T1；4.3 端點 mode/權限/防巢狀→T4；4.4 唯讀守衛→T5；4.5 audit 歸屬→T2+T3；4.6 refresh 防升級→T6；4.7 前端→T8-T11；§1 problem 5（移除壞切換器、退出再進入）→T4（操作者恆為 current_user + 防巢狀）+T10（移除 switcher）；測試計畫→各 task + T7/T12；整合→T13。無遺漏。
- **型別一致**：`impersonation_mode` / `impersonated_by` / `impersonated_by_name` 三 claim 名稱全程一致；`mode` 值域 `'readonly'|'write'` 前後端一致。
- **advisor 三點已納入**：#1 操作者身份（採退出再進入，操作者恆為 current_user；防巢狀模擬 409；T10 移除壞切換器）；#2 audit 路徑（測試改用真實已 audit 的 `POST /api/portal/my-overtimes`）；#3 middleware 順序註解（最外層先執行、被擋 403 不進 audit，已澄清且與 KillSwitch 唯讀模式區隔）。
- **無 placeholder**：各 task 皆含實際程式碼/指令；前端 T9-T11 的 UI 細節依既有 dialog/layout pattern（執行時讀現檔對齊），已標明搜尋點與斷言內容。

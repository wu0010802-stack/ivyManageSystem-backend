# Audit Pattern Tightening + Navigation Restructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 給 audit_log 加上「軟刪/真刪」語意標記 + 高風險事件 ack 機制 + 前端工作台/報表 IA 重整與紅點，落地 P2 6.1 + 6.2。

**Architecture:** 後端 reuse 既有 `utils/audit.py` middleware infra（不動寫入機制），加 helper 函式（軟刪 endpoint 顯式呼叫）+ middleware 對 HTTP DELETE 自動加「(不可復原)」尾綴；新增 `audit_logs` 表 ack 欄位 + 3 個 endpoint（filter / ack / ack-all）+ service-layer SQL filter。前端 OpenAPI 拉新型別，新增「工作台」與「報表」兩個一級項目，「工作台」內含 2 sub-tab（待簽核既有 + 高風險新），紅點靠 60s polling composable。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + pytest（後端）；Vue 3 + TS + Pinia + Element Plus + Vitest（前端）。

**Spec reference:** `docs/superpowers/specs/2026-05-25-audit-pattern-and-navigation-design.md`

**Spec correction（implementation 階段發現）**：
- 表名是 `audit_logs`（複數），spec §3 寫 `audit_log` 是錯字，plan 與 migration 用 `audit_logs`
- 既有 `utils/audit.py` 已有 `ENTITY_LABELS` dict（50+ entry），不新增 `ENTITY_LABEL_ZH`，helper 直接 reuse
- `request.state.audit_summary` / `audit_entity_id` / `audit_changes` / `audit_skip` 是既有命名慣例；新增 `audit_delete_kind` 對齊

---

## File Structure

### 後端（ivy-backend）

| 檔案 | 動作 | 責任 |
|---|---|---|
| `utils/audit.py` | Modify | 加 `mark_soft_delete` / `mark_hard_delete` / `_decorate_delete_summary`；middleware 寫入前過 decorate |
| `models/audit.py` | Modify | `AuditLog` 加 `acknowledged_at` / `acknowledged_by` 2 欄 |
| `alembic/versions/20260525_audrsk01_audit_acknowledged.py` | Create | 加 2 欄 + FK + index；對 `audit_logs` 表（複數） |
| `services/audit_high_risk.py` | Create | `HIGH_RISK_ACTIONS` 集合 + `filter_high_risk` 純函式 + `classify_risk_kind` |
| `api/audit.py` | Modify | 加 `GET /audit-logs/high-risk`、`POST /audit-logs/{id}/ack`、`POST /audit-logs/ack-all` |
| `schemas/audit.py`（若不存在則 inline 在 api/audit.py） | Modify/Create | Pydantic `AuditLogHighRiskItem` / `HighRiskListResponse` |
| `api/attachments.py`、`api/students.py`、`api/contact_book.py`、`api/portal/contact_book.py` | Modify | `deleted_at` 軟刪點加 `mark_soft_delete` call |
| `api/employees.py`、`api/auth.py` | Modify | `is_active=False` 軟刪點加 `mark_soft_delete` call |
| `utils/student_lifecycle.py` 或 `services/student_lifecycle.py` | Modify | `set_lifecycle_status` 終態轉換時加 `mark_soft_delete` |
| `tests/test_audit_delete_decorator.py` | Create | middleware 自動加尾綴 + helper state 設定 |
| `tests/test_audit_soft_delete_integration.py` | Create | 軟刪 endpoint 寫出的 summary 含「軟刪」 |
| `tests/test_audit_high_risk.py` | Create | `filter_high_risk` 三類 + `classify_risk_kind` |
| `tests/test_audit_high_risk_router.py` | Create | 3 endpoint 權限/時間窗/ack idempotent |

### 前端（ivy-frontend）

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/api/_generated/schema.d.ts` | Regen | OpenAPI codegen 拉新 endpoint 型別 |
| `src/api/audit.ts` | Modify | 加 `getHighRiskAudits` / `ackAudit` / `ackAllAudits` |
| `src/composables/useHighRiskAuditCount.ts` | Create | 60s 輪詢 unack_count，visibilitychange 暫停 |
| `src/views/workbench/WorkbenchLayout.vue` | Create | 工作台外層 + 2 sub-tab nav |
| `src/views/workbench/WorkbenchApprovalsView.vue` | Move | 從 `src/views/ApprovalsView.vue` 搬位（內容不動） |
| `src/views/workbench/WorkbenchHighRiskView.vue` | Create | 高風險列表 + ack 按鈕 + 全部標已讀 |
| `src/router/index.ts` | Modify | `/approvals` redirect + 加 `/workbench/*` routes |
| `src/components/layout/AdminSidebar.vue` | Modify | IA 重整 9 一級項目 + `pendingHighRiskAudit` prop + `workbenchBadge` |
| `src/layouts/AdminLayout.vue`（或 App.vue） | Modify | 接 `useHighRiskAuditCount` 傳 prop 給 sidebar |
| `tests/views/workbench/*.test.ts` | Create | layout / approvals / high-risk view |
| `tests/composables/useHighRiskAuditCount.test.ts` | Create | 輪詢 / visibilitychange / dedupe |
| `tests/components/AdminSidebar.test.ts` | Modify | badge 計算 + 新結構 + 權限隱藏 |

---

## Task 1: 後端 audit helper + middleware 尾綴 decorator

**Branch（從 ivy-backend main 開）**：`feat/audit-pattern-and-ack-2026-05-25-backend`

**Files:**
- Modify: `utils/audit.py`（加 3 個函式 + middleware tweak）
- Create: `tests/test_audit_delete_decorator.py`

- [ ] **Step 1: Write failing test (`tests/test_audit_delete_decorator.py`)**

```python
"""Test audit middleware 軟刪/真刪 decorator 與 helpers"""
from types import SimpleNamespace

import pytest

from utils.audit import mark_soft_delete, mark_hard_delete, _decorate_delete_summary


def _fake_request(method: str, state: dict | None = None):
    state_ns = SimpleNamespace(**(state or {}))
    return SimpleNamespace(method=method, state=state_ns)


def test_mark_soft_delete_sets_summary_and_kind():
    req = _fake_request("PATCH")
    mark_soft_delete(req, "employee", "王小明")
    assert req.state.audit_summary == "軟刪 員工 王小明"
    assert req.state.audit_delete_kind == "soft"


def test_mark_soft_delete_falls_back_to_entity_type_when_unknown():
    req = _fake_request("PATCH")
    mark_soft_delete(req, "unknown_entity", "X-1")
    assert req.state.audit_summary == "軟刪 unknown_entity X-1"


def test_mark_hard_delete_appends_irreversible_marker():
    req = _fake_request("PATCH")
    mark_hard_delete(req, "vendor_payment", "#123")
    assert "(不可復原)" in req.state.audit_summary
    assert req.state.audit_summary == "真刪 廠商付款 #123 (不可復原)"
    assert req.state.audit_delete_kind == "hard"


def test_decorate_http_delete_auto_appends_marker():
    req = _fake_request("DELETE")
    assert _decorate_delete_summary(req, "刪除員工") == "刪除員工 (不可復原)"


def test_decorate_skips_when_delete_kind_already_set():
    req = _fake_request("DELETE", {"audit_delete_kind": "soft"})
    assert _decorate_delete_summary(req, "軟刪 員工 X") == "軟刪 員工 X"


def test_decorate_skips_non_delete_method():
    req = _fake_request("PATCH")
    assert _decorate_delete_summary(req, "修改員工") == "修改員工"


def test_decorate_handles_hard_delete_via_helper():
    """非 HTTP DELETE 但 endpoint 用 mark_hard_delete 標記的情境
    middleware decorate 不應重複加尾綴（因為 helper 已加）。"""
    req = _fake_request("PATCH", {"audit_delete_kind": "hard"})
    assert _decorate_delete_summary(req, "真刪 員工 X (不可復原)") == "真刪 員工 X (不可復原)"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_audit_delete_decorator.py -v
```

Expected: FAIL with `ImportError: cannot import name 'mark_soft_delete'`.

- [ ] **Step 3: Add helpers + decorator to `utils/audit.py`**

在 `utils/audit.py` 既有 `ENTITY_LABELS` 與 `_build_summary` 之間（約 line 282 附近）加：

```python
def mark_soft_delete(request, entity_type: str, entity_label: str) -> None:
    """軟刪 endpoint 顯式呼叫，summary 形如「軟刪 員工 王小明」。

    軟刪 = endpoint 內部 `deleted_at=now()` 或 `is_active=False`，
    對外是 PATCH/PUT 但業務語意是刪除。middleware 看 HTTP method 看不出來，
    必須 endpoint 顯式呼叫此 helper。
    """
    label = ENTITY_LABELS.get(entity_type, entity_type)
    request.state.audit_summary = f"軟刪 {label} {entity_label}"
    request.state.audit_delete_kind = "soft"


def mark_hard_delete(request, entity_type: str, entity_label: str) -> None:
    """真刪 helper — 用於非 HTTP DELETE 但內部 `session.delete()` 的情境
    （例如 PATCH 觸發 cascade 真刪）。HTTP DELETE 不需呼叫此 helper，
    middleware 會自動加「(不可復原)」尾綴。
    """
    label = ENTITY_LABELS.get(entity_type, entity_type)
    request.state.audit_summary = f"真刪 {label} {entity_label} (不可復原)"
    request.state.audit_delete_kind = "hard"


def _decorate_delete_summary(request, summary: str) -> str:
    """寫入前對 summary 補真刪尾綴：
    - HTTP DELETE 且 endpoint 未自行標 audit_delete_kind → 加「(不可復原)」
    - 其他情境（軟刪 / 已 hard / 非 DELETE）→ 維持原 summary
    """
    if request.method == "DELETE" and not getattr(request.state, "audit_delete_kind", None):
        return f"{summary} (不可復原)"
    return summary
```

- [ ] **Step 4: 在 middleware dispatch 中接 decorator**

`utils/audit.py` 約 line 669 的 summary 取得處：

找：
```python
            else:
                summary = getattr(
                    request.state, "audit_summary", None
                ) or _build_summary(method, path, entity_type)
```

改成：
```python
            else:
                summary = getattr(
                    request.state, "audit_summary", None
                ) or _build_summary(method, path, entity_type)
                summary = _decorate_delete_summary(request, summary)
```

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_audit_delete_decorator.py -v
```

Expected: 7 passed.

- [ ] **Step 6: Run full audit-related pre-existing tests for regression**

```bash
pytest tests/ -k "audit" -v
```

Expected: All pre-existing audit tests pass + 7 new ones; if 3 pre-existing `test_audit_router` fails appear (per MEMORY.md they are pre-existing), document but don't fix in this task.

- [ ] **Step 7: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git checkout -b feat/audit-pattern-and-ack-2026-05-25-backend
git add utils/audit.py tests/test_audit_delete_decorator.py
git commit -m "$(cat <<'EOF'
feat(audit): add soft/hard delete summary helpers + DELETE auto marker

middleware 對 HTTP DELETE 自動加「(不可復原)」尾綴；新增
mark_soft_delete / mark_hard_delete helpers 給 endpoint 顯式
標記軟刪 / cascade 真刪。reuse 既有 ENTITY_LABELS dict 取中文 label。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: 軟刪 endpoint 加 `mark_soft_delete`（deleted_at 類）

**Files:**
- Modify: `api/attachments.py`、`api/students.py`、`api/contact_book.py`、`api/portal/contact_book.py`
- Create: `tests/test_audit_soft_delete_integration.py`

- [ ] **Step 1: Write failing integration test**

```python
"""Test 軟刪 endpoint 寫出的 audit summary 含「軟刪」字眼"""
import pytest
from sqlalchemy import select

from models.audit import AuditLog


def _latest_audit(session, entity_type: str) -> AuditLog | None:
    return session.execute(
        select(AuditLog)
        .where(AuditLog.entity_type == entity_type)
        .order_by(AuditLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def test_attachment_soft_delete_audit_summary(client, session, admin_token, attachment_fixture):
    """DELETE /attachments/{id} 是軟刪 → summary 含「軟刪 附件」"""
    att = attachment_fixture()
    res = client.delete(
        f"/api/attachments/{att.id}",
        cookies={"access_token": admin_token},
    )
    assert res.status_code == 204
    session.expire_all()
    row = _latest_audit(session, "attachment")
    assert row is not None
    assert row.summary.startswith("軟刪")
    assert "(不可復原)" not in row.summary


def test_guardian_soft_delete_audit_summary(client, session, admin_token, guardian_fixture):
    """DELETE /students/{sid}/guardians/{gid} 是軟刪 → summary 含「軟刪」"""
    guardian = guardian_fixture()
    res = client.delete(
        f"/api/students/{guardian.student_id}/guardians/{guardian.id}",
        cookies={"access_token": admin_token},
    )
    assert res.status_code in (200, 204)
    session.expire_all()
    row = _latest_audit(session, "guardian") or _latest_audit(session, "student")
    assert row is not None
    assert row.summary.startswith("軟刪") or "軟刪" in row.summary
```

NOTE: 若 `attachment_fixture` / `guardian_fixture` 不存在，先 grep 既有 conftest 找對應 factory，或在 tests/conftest.py 加最小 fixture。

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_audit_soft_delete_integration.py -v
```

Expected: FAIL — summary 是預設的「刪除附件」沒有「軟刪」字眼。

- [ ] **Step 3: 在 `api/attachments.py` 軟刪點加 helper call**

Grep 找 `deleted_at = datetime.now()` 或 `deleted_at = datetime.utcnow()`（約 line 246）：

```python
# 既有
att.deleted_at = datetime.now()
session.commit()
```

改成：
```python
from utils.audit import mark_soft_delete  # 加 import (檔案上方)

# 在 deleted_at 設定後、session.commit() 前
att.deleted_at = datetime.now()
mark_soft_delete(request, "attachment", str(att.id))
session.commit()
```

NOTE: 若 endpoint signature 沒有 `request: Request` 參數，加上 `from fastapi import Request` 並在 dependency 列加 `request: Request`。

- [ ] **Step 4: 在 `api/students.py` guardian 軟刪點加 helper**

Grep `guardian.deleted_at = datetime.now()`（約 line 1456）。在該行後加：
```python
mark_soft_delete(request, "guardian", guardian.name or str(guardian.id))
```

- [ ] **Step 5: 在 `api/contact_book.py` 與 `api/portal/contact_book.py` 軟刪點加 helper**

兩檔分別 grep `deleted_at = datetime.now()`，在 commit 前加：
```python
mark_soft_delete(request, "contact_book_entry", str(entry.id))
```

- [ ] **Step 6: Run integration test to verify pass**

```bash
pytest tests/test_audit_soft_delete_integration.py -v
```

Expected: 2 passed（attachment + guardian）；若 contact_book 也加 case 則 3+。

- [ ] **Step 7: Commit**

```bash
git add api/attachments.py api/students.py api/contact_book.py api/portal/contact_book.py tests/test_audit_soft_delete_integration.py
git commit -m "$(cat <<'EOF'
feat(audit): mark soft-delete endpoints (deleted_at variants)

attachments / students-guardian / contact_book × 2 四個軟刪
endpoint 顯式呼叫 mark_soft_delete，audit summary 改為「軟刪 ...」
而非預設「刪除 ...」。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: 軟刪 endpoint 加 `mark_soft_delete`（is_active 類）

**Files:**
- Modify: `api/employees.py`、`api/auth.py`
- Append: `tests/test_audit_soft_delete_integration.py`

- [ ] **Step 1: Append failing test**

```python
def test_employee_deactivate_audit_summary(client, session, admin_token, employee_fixture):
    """PATCH /employees/{id} with is_active=False 是軟刪 → summary 含「軟刪 員工」"""
    emp = employee_fixture()
    res = client.patch(
        f"/api/employees/{emp.id}",
        json={"is_active": False},
        cookies={"access_token": admin_token},
    )
    assert res.status_code == 200
    session.expire_all()
    row = _latest_audit(session, "employee")
    assert row is not None
    assert row.summary.startswith("軟刪")


def test_user_deactivate_audit_summary(client, session, admin_token, user_fixture):
    user = user_fixture()
    res = client.patch(
        f"/api/users/{user.id}",
        json={"is_active": False},
        cookies={"access_token": admin_token},
    )
    assert res.status_code == 200
    session.expire_all()
    row = _latest_audit(session, "user")
    assert row is not None
    assert row.summary.startswith("軟刪")
```

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_audit_soft_delete_integration.py::test_employee_deactivate_audit_summary -v
```

Expected: FAIL — summary 是「修改員工」沒有「軟刪」。

- [ ] **Step 3: 在 `api/employees.py` 加 helper**

Grep 員工停用點（is_active 從 True → False 或 `is_active = False`），約 line 719。修改 endpoint：

```python
# 既有
if payload.is_active is False:
    emp.is_active = False
# ... 

# 改：在 is_active=False 路徑加 helper
if payload.is_active is False and emp.is_active:
    emp.is_active = False
    mark_soft_delete(request, "employee", emp.name or f"#{emp.id}")
```

- [ ] **Step 4: 在 `api/auth.py` 加 helper**

Grep users 停用點（PATCH /users/{id} 或專屬 deactivate endpoint）。在 `is_active=False` 寫入點加：
```python
mark_soft_delete(request, "user", user.username or f"#{user.id}")
```

- [ ] **Step 5: Run test to verify pass**

```bash
pytest tests/test_audit_soft_delete_integration.py -v
```

Expected: 4 passed total.

- [ ] **Step 6: Commit**

```bash
git add api/employees.py api/auth.py tests/test_audit_soft_delete_integration.py
git commit -m "$(cat <<'EOF'
feat(audit): mark soft-delete endpoints (is_active variants)

employees / users 停用流程 (is_active=False) 顯式呼叫 mark_soft_delete，
audit summary 改為「軟刪 員工 王小明」/「軟刪 使用者帳號 jdoe」。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: lifecycle 中央化 + ENTITY_LABELS sanity coverage

**Files:**
- Modify: `utils/student_lifecycle.py` 或 `services/student_lifecycle.py`（依現況）
- Create: `tests/test_audit_entity_labels_coverage.py`

- [ ] **Step 1: 確認 lifecycle 中央化位置**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
grep -rn "set_lifecycle_status\|def transition" utils/student_lifecycle.py services/student_lifecycle.py 2>/dev/null | head -5
```

定位 `set_lifecycle_status` 或 `transition()` 函式。

- [ ] **Step 2: 在終態 transition 處加 helper call**

找學生進入終態（`graduated` / `withdrawn` / `archived`）的路徑。加 helper：

```python
# 終態 lifecycle_status 變更 = 業務語意上的軟刪
TERMINAL_STATUSES = {"graduated", "withdrawn", "archived"}

def transition(student, target_status, *, request=None, ...):
    # ... 既有邏輯
    student.lifecycle_status = target_status
    if request is not None and target_status in TERMINAL_STATUSES:
        from utils.audit import mark_soft_delete
        mark_soft_delete(request, "student", student.chinese_name or f"#{student.id}")
```

NOTE: `request` 必須由 caller 傳入；若 `set_lifecycle_status` 簽章沒有 request，加 keyword-only optional 參數。若 caller 在背景 task（非 HTTP request 上下文）則略過 mark_soft_delete。

- [ ] **Step 3: Write sanity test for `ENTITY_LABELS` coverage**

```python
"""Sanity: 確保 ENTITY_LABELS 與 ENTITY_PATTERNS keys 對齊；
軟刪 helper 使用的 entity_type 都有中文 label。"""
from utils.audit import ENTITY_LABELS, ENTITY_PATTERNS


def test_entity_labels_cover_all_patterns():
    """每個 ENTITY_PATTERNS 的 entity_type 都要在 ENTITY_LABELS 有中文 label，
    否則軟刪/真刪 summary 會回退顯示英文 key。"""
    pattern_keys = set(ENTITY_PATTERNS.keys()) if isinstance(ENTITY_PATTERNS, dict) else {
        p["entity_type"] for p in ENTITY_PATTERNS
    }
    label_keys = set(ENTITY_LABELS.keys())
    missing = pattern_keys - label_keys
    assert not missing, f"ENTITY_LABELS missing entries: {sorted(missing)}"


def test_soft_delete_marker_entity_types_have_labels():
    """軟刪 helper 慣用的 entity_type 都要有中文 label。"""
    used_types = {
        "attachment", "guardian", "contact_book_entry",
        "employee", "user", "student",
    }
    for et in used_types:
        assert et in ENTITY_LABELS, f"{et} 無中文 label，軟刪 summary 會顯示英文"
```

NOTE: 看 ENTITY_PATTERNS 實際結構（dict vs list of dict）對應改 test。

- [ ] **Step 4: Run sanity test**

```bash
pytest tests/test_audit_entity_labels_coverage.py -v
```

Expected: 若失敗顯示 missing entries → 在 `utils/audit.py:ENTITY_LABELS` 補上對應中文 label（例如「`"guardian": "監護人"`」若漏）。

- [ ] **Step 5: 補齊 ENTITY_LABELS missing entries（若 step 4 報缺）**

依 step 4 報缺清單，在 `utils/audit.py` ENTITY_LABELS 加對應 entry。重跑 step 4 確認 pass。

- [ ] **Step 6: Run full audit tests for regression**

```bash
pytest tests/ -k "audit" -v
```

Expected: All previously-green tests still green + new ones pass.

- [ ] **Step 7: Commit**

```bash
git add utils/student_lifecycle.py services/student_lifecycle.py utils/audit.py tests/test_audit_entity_labels_coverage.py
git commit -m "$(cat <<'EOF'
feat(audit): mark soft-delete on student lifecycle terminal transition

學生進入 graduated / withdrawn / archived 終態時，從中央化
set_lifecycle_status 顯式呼叫 mark_soft_delete (request 由 caller 傳入)，
覆蓋所有 student 軟刪業務路徑。同時加 ENTITY_LABELS coverage sanity
test 強制軟刪 helper 使用的 entity_type 都有中文 label。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: alembic migration + AuditLog model 加 ack 欄位

**Files:**
- Create: `alembic/versions/20260525_audrsk01_audit_acknowledged.py`
- Modify: `models/audit.py`
- Create: `tests/test_alembic_audrsk01.py`

- [ ] **Step 1: 確認當前 alembic head**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
alembic heads
```

Expected: `rolesdb01 (head)`. 新 migration `down_revision = "rolesdb01"`.

- [ ] **Step 2: Write migration file**

`alembic/versions/20260525_audrsk01_audit_acknowledged.py`:

```python
"""audit_logs add acknowledged_at / acknowledged_by

Revision ID: audrsk01
Revises: rolesdb01
Create Date: 2026-05-25

紅點機制：高風險 audit 事件需要 ack（標已讀）。
新增 2 nullable 欄位 + FK + index。Postgres 11+ nullable column add 為
metadata-only operation，無鎖風險。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "audrsk01"
down_revision: Union[str, None] = "rolesdb01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "audit_logs",
        sa.Column("acknowledged_by", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_audit_logs_acknowledged_by",
        "audit_logs",
        "users",
        ["acknowledged_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_audit_logs_ack_created",
        "audit_logs",
        ["acknowledged_at", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_ack_created", table_name="audit_logs")
    op.drop_constraint("fk_audit_logs_acknowledged_by", "audit_logs", type_="foreignkey")
    op.drop_column("audit_logs", "acknowledged_by")
    op.drop_column("audit_logs", "acknowledged_at")
```

- [ ] **Step 3: 套用 migration 到 dev DB**

```bash
alembic upgrade heads
```

Expected: `Running upgrade rolesdb01 -> audrsk01, ...`. 無錯誤。

確認 schema：
```bash
psql -U yilunwu -d ivymanagement -c "\d audit_logs" | grep -E "acknowledged|created_at"
```

Expected: 看到 acknowledged_at + acknowledged_by 兩欄位 + ix_audit_logs_ack_created index.

- [ ] **Step 4: Modify `models/audit.py`**

在 `AuditLog` class 內加：

```python
from sqlalchemy import Column, Integer, String, DateTime, Text, Index, ForeignKey

class AuditLog(Base):
    # ... existing columns
    acknowledged_at = Column(DateTime(timezone=True), nullable=True, comment="ack 時間")
    acknowledged_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="ack 操作者")
```

並在 `__table_args__` 加 index（為 ORM-side 對齊；alembic 已建實體 index）：
```python
__table_args__ = (
    Index("ix_audit_created", "created_at"),
    Index("ix_audit_entity", "entity_type", "entity_id"),
    Index("ix_audit_user", "user_id"),
    Index("ix_audit_logs_ack_created", "acknowledged_at", "created_at"),
)
```

- [ ] **Step 5: Write migration round-trip test**

`tests/test_alembic_audrsk01.py`:

```python
"""Test audrsk01 migration upgrade/downgrade reversible"""
import pytest
from sqlalchemy import inspect

from models.base import engine, Base
from models.audit import AuditLog


def test_audit_logs_has_ack_columns():
    """套完 migration 後 audit_logs 表應有 acknowledged_at / acknowledged_by"""
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("audit_logs")}
    assert "acknowledged_at" in cols
    assert "acknowledged_by" in cols


def test_audit_logs_ack_index_exists():
    inspector = inspect(engine)
    indexes = inspector.get_indexes("audit_logs")
    names = {ix["name"] for ix in indexes}
    assert "ix_audit_logs_ack_created" in names


def test_audit_log_model_can_set_ack_fields(session):
    """ORM 層可以寫 acknowledged_at / acknowledged_by"""
    from datetime import datetime, timezone
    row = AuditLog(
        action="DELETE",
        entity_type="employee",
        summary="刪除員工 (不可復原)",
        username="admin",
        acknowledged_at=datetime.now(timezone.utc),
        acknowledged_by=1,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    assert row.acknowledged_at is not None
    assert row.acknowledged_by == 1
```

- [ ] **Step 6: Run test**

```bash
pytest tests/test_alembic_audrsk01.py -v
```

Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/20260525_audrsk01_audit_acknowledged.py models/audit.py tests/test_alembic_audrsk01.py
git commit -m "$(cat <<'EOF'
feat(audit): add acknowledged_at / acknowledged_by to audit_logs

新增 audrsk01 migration 給 audit_logs 表加 ack 兩欄位 +
FK ON DELETE SET NULL + (acknowledged_at, created_at) 複合 index
給紅點 query 用。Postgres 11+ nullable column add 為 metadata-only。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: services/audit_high_risk.py — filter + classify

**Files:**
- Create: `services/audit_high_risk.py`
- Create: `tests/test_audit_high_risk.py`

- [ ] **Step 1: Write failing test (`tests/test_audit_high_risk.py`)**

```python
"""Test audit_high_risk service: filter_high_risk + classify_risk_kind"""
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from models.audit import AuditLog
from services.audit_high_risk import (
    HIGH_RISK_ACTIONS,
    filter_high_risk,
    classify_risk_kind,
)


# ============== classify_risk_kind ==============

def test_classify_http_delete_as_hard_delete():
    row = AuditLog(action="DELETE", entity_type="employee", summary="刪除員工 X (不可復原)")
    assert classify_risk_kind(row) == "hard_delete"


def test_classify_marker_only_hard_delete():
    """非 HTTP DELETE 但 summary 含 (不可復原) 也算 hard_delete"""
    row = AuditLog(action="UPDATE", entity_type="employee", summary="真刪 員工 X (不可復原)")
    assert classify_risk_kind(row) == "hard_delete"


def test_classify_blocked_action():
    row = AuditLog(action="BLOCKED_DELETE", entity_type="employee", summary="拒絕")
    assert classify_risk_kind(row) == "blocked"


def test_classify_blocked_create():
    row = AuditLog(action="BLOCKED_CREATE", entity_type="user", summary="拒絕")
    assert classify_risk_kind(row) == "blocked"


def test_classify_permission_change_fallback():
    row = AuditLog(action="UPDATE", entity_type="user", summary="修改使用者 jdoe (role: hr → admin)")
    assert classify_risk_kind(row) == "permission_change"


# ============== filter_high_risk ==============

def _make_row(session, *, action, entity_type="employee", summary="x", acked=False, created_offset=timedelta()):
    row = AuditLog(
        action=action,
        entity_type=entity_type,
        summary=summary,
        username="admin",
        created_at=datetime.now(timezone.utc) + created_offset,
    )
    if acked:
        row.acknowledged_at = datetime.now(timezone.utc)
        row.acknowledged_by = 1
    session.add(row)
    session.commit()
    return row


def test_filter_includes_http_delete(session):
    row = _make_row(session, action="DELETE", summary="刪除員工 X (不可復原)")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    query = filter_high_risk(sa.select(AuditLog), since=since)
    results = session.execute(query).scalars().all()
    assert row.id in [r.id for r in results]


def test_filter_includes_blocked(session):
    row = _make_row(session, action="BLOCKED_DELETE")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = session.execute(filter_high_risk(sa.select(AuditLog), since=since)).scalars().all()
    assert row.id in [r.id for r in results]


def test_filter_includes_marker_only_hard_delete(session):
    """非 HTTP DELETE 但 summary 含「(不可復原)」要被 catch"""
    row = _make_row(session, action="UPDATE", summary="真刪 員工 X (不可復原)")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = session.execute(filter_high_risk(sa.select(AuditLog), since=since)).scalars().all()
    assert row.id in [r.id for r in results]


def test_filter_includes_permission_change(session):
    """user entity + summary 含 role/permission 字眼"""
    row = _make_row(session, action="UPDATE", entity_type="user", summary="修改使用者 (role: hr → admin)")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = session.execute(filter_high_risk(sa.select(AuditLog), since=since)).scalars().all()
    assert row.id in [r.id for r in results]


def test_filter_excludes_normal_update(session):
    """普通 UPDATE 不該被 catch"""
    row = _make_row(session, action="UPDATE", entity_type="employee", summary="修改員工資料")
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = session.execute(filter_high_risk(sa.select(AuditLog), since=since)).scalars().all()
    assert row.id not in [r.id for r in results]


def test_filter_excludes_acked_when_unack_only(session):
    """acked 已標 + only_unack=True → 不該回傳"""
    row = _make_row(session, action="DELETE", summary="x (不可復原)", acked=True)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = session.execute(
        filter_high_risk(sa.select(AuditLog), since=since, only_unack=True)
    ).scalars().all()
    assert row.id not in [r.id for r in results]


def test_filter_includes_acked_when_only_unack_false(session):
    row = _make_row(session, action="DELETE", summary="x (不可復原)", acked=True)
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = session.execute(
        filter_high_risk(sa.select(AuditLog), since=since, only_unack=False)
    ).scalars().all()
    assert row.id in [r.id for r in results]


def test_filter_respects_time_window(session):
    """8 天前的 row 不該被 7 天窗 catch"""
    row = _make_row(session, action="DELETE", summary="x (不可復原)", created_offset=timedelta(days=-8))
    since = datetime.now(timezone.utc) - timedelta(days=7)
    results = session.execute(filter_high_risk(sa.select(AuditLog), since=since)).scalars().all()
    assert row.id not in [r.id for r in results]


def test_high_risk_actions_constant():
    assert HIGH_RISK_ACTIONS == {"DELETE", "BLOCKED_CREATE", "BLOCKED_UPDATE", "BLOCKED_DELETE"}
```

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_audit_high_risk.py -v
```

Expected: ImportError — services/audit_high_risk 不存在.

- [ ] **Step 3: Create `services/audit_high_risk.py`**

```python
"""High-risk audit event 過濾與分類。

「高風險」定義（與 spec §3.2 對齊）：
- HTTP DELETE：action='DELETE'
- BLOCKED_*：身份驗證 / 權限伺服器擋下的 403 嘗試
- 非 HTTP DELETE 但 summary 含「(不可復原)」（§2 mark_hard_delete 標的）
- 權限變更：entity_type='user' AND summary 含 role/permission/角色/權限

False positive 緩解：權限變更 LIKE 只在 entity_type='user' AND action='UPDATE'
組合下生效；若實際 false positive 多再加 marker（spec §3.2 預留方案）。
"""
from typing import Literal

import sqlalchemy as sa

from models.audit import AuditLog


HIGH_RISK_ACTIONS = {
    "DELETE",
    "BLOCKED_CREATE",
    "BLOCKED_UPDATE",
    "BLOCKED_DELETE",
}


def filter_high_risk(query, *, since, only_unack: bool = True):
    """套用 high-risk filter 到 SQLAlchemy query。

    Args:
        query: sa.select(AuditLog) base query
        since: datetime — 只看 created_at >= since 的 row
        only_unack: 預設 True，只回未 ack 的 row

    Returns:
        加上 filter / order 的 query（caller 自行 execute）
    """
    cond = sa.or_(
        AuditLog.action.in_(HIGH_RISK_ACTIONS),
        AuditLog.summary.like("%(不可復原)%"),
        sa.and_(
            AuditLog.entity_type == "user",
            AuditLog.action == "UPDATE",
            sa.or_(
                AuditLog.summary.like("%role%"),
                AuditLog.summary.like("%permission%"),
                AuditLog.summary.like("%角色%"),
                AuditLog.summary.like("%權限%"),
            ),
        ),
    )
    query = query.filter(AuditLog.created_at >= since).filter(cond)
    if only_unack:
        query = query.filter(AuditLog.acknowledged_at.is_(None))
    return query.order_by(AuditLog.created_at.desc())


def classify_risk_kind(row: AuditLog) -> Literal["hard_delete", "blocked", "permission_change"]:
    """分類單筆 row 的 risk kind（response shape 用）。"""
    if row.action in {"BLOCKED_CREATE", "BLOCKED_UPDATE", "BLOCKED_DELETE"}:
        return "blocked"
    if row.action == "DELETE":
        return "hard_delete"
    if row.summary and "(不可復原)" in row.summary:
        return "hard_delete"
    return "permission_change"
```

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_audit_high_risk.py -v
```

Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add services/audit_high_risk.py tests/test_audit_high_risk.py
git commit -m "$(cat <<'EOF'
feat(audit): add high-risk filter + classify service

services/audit_high_risk.py 純函式：filter_high_risk 套 SQL filter
(DELETE / BLOCKED_* / 「(不可復原)」marker / user 權限變更 LIKE)；
classify_risk_kind 派分三類給 response shape 用。
權限變更 LIKE 限制在 entity_type='user' AND action='UPDATE' 降低
false positive。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: api/audit.py 加 3 個高風險 endpoint

**Files:**
- Modify: `api/audit.py`（加 schemas + 3 endpoint）
- Create: `tests/test_audit_high_risk_router.py`

- [ ] **Step 1: Write failing router tests**

```python
"""Test /audit-logs/high-risk + ack endpoints"""
from datetime import datetime, timedelta, timezone

import pytest

from models.audit import AuditLog


# ============== GET /audit-logs/high-risk ==============

def test_get_high_risk_requires_audit_logs_permission(client, viewer_token):
    """無 AUDIT_LOGS 權限 → 403"""
    res = client.get("/api/audit-logs/high-risk", cookies={"access_token": viewer_token})
    assert res.status_code == 403


def test_get_high_risk_returns_unack_only_by_default(client, admin_token, session):
    """預設 unack_only=True，已 ack 不回傳"""
    unacked = AuditLog(action="DELETE", entity_type="employee", summary="刪除 X (不可復原)", username="admin")
    acked = AuditLog(
        action="DELETE",
        entity_type="employee",
        summary="刪除 Y (不可復原)",
        username="admin",
        acknowledged_at=datetime.now(timezone.utc),
        acknowledged_by=1,
    )
    session.add_all([unacked, acked])
    session.commit()

    res = client.get("/api/audit-logs/high-risk", cookies={"access_token": admin_token})
    assert res.status_code == 200
    data = res.json()
    ids = {item["id"] for item in data["items"]}
    assert unacked.id in ids
    assert acked.id not in ids
    assert data["unack_count"] >= 1


def test_get_high_risk_with_unack_only_false(client, admin_token, session):
    """unack_only=false → 包含已 ack"""
    res = client.get(
        "/api/audit-logs/high-risk?unack_only=false",
        cookies={"access_token": admin_token},
    )
    assert res.status_code == 200


def test_get_high_risk_respects_days_param(client, admin_token, session):
    """8 天前的 row 在 days=7 不該回傳"""
    old_row = AuditLog(
        action="DELETE",
        entity_type="employee",
        summary="刪除老的 (不可復原)",
        username="admin",
        created_at=datetime.now(timezone.utc) - timedelta(days=8),
    )
    session.add(old_row)
    session.commit()
    res = client.get("/api/audit-logs/high-risk?days=7", cookies={"access_token": admin_token})
    data = res.json()
    assert old_row.id not in {item["id"] for item in data["items"]}


def test_get_high_risk_classifies_risk_kind(client, admin_token, session):
    """item.risk_kind 三類各一筆能正確派分"""
    hd = AuditLog(action="DELETE", entity_type="employee", summary="刪 (不可復原)", username="a")
    bl = AuditLog(action="BLOCKED_DELETE", entity_type="employee", summary="拒絕刪", username="a")
    pc = AuditLog(action="UPDATE", entity_type="user", summary="改 role: hr → admin", username="a")
    session.add_all([hd, bl, pc])
    session.commit()

    res = client.get("/api/audit-logs/high-risk?days=7", cookies={"access_token": admin_token})
    items = {item["id"]: item["risk_kind"] for item in res.json()["items"]}
    assert items[hd.id] == "hard_delete"
    assert items[bl.id] == "blocked"
    assert items[pc.id] == "permission_change"


# ============== POST /audit-logs/{id}/ack ==============

def test_ack_single_marks_acknowledged(client, admin_token, session, admin_user_id):
    row = AuditLog(action="DELETE", entity_type="employee", summary="刪 (不可復原)", username="admin")
    session.add(row)
    session.commit()
    res = client.post(f"/api/audit-logs/{row.id}/ack", cookies={"access_token": admin_token})
    assert res.status_code == 200
    session.expire_all()
    session.refresh(row)
    assert row.acknowledged_at is not None
    assert row.acknowledged_by == admin_user_id


def test_ack_returns_404_for_missing(client, admin_token):
    res = client.post("/api/audit-logs/999999/ack", cookies={"access_token": admin_token})
    assert res.status_code == 404


def test_ack_is_idempotent(client, admin_token, session, admin_user_id):
    """重複 ack 同筆 → timestamp / user 維持第一次"""
    row = AuditLog(action="DELETE", entity_type="employee", summary="x (不可復原)", username="admin")
    session.add(row)
    session.commit()

    client.post(f"/api/audit-logs/{row.id}/ack", cookies={"access_token": admin_token})
    session.expire_all()
    session.refresh(row)
    first_at = row.acknowledged_at
    first_by = row.acknowledged_by

    client.post(f"/api/audit-logs/{row.id}/ack", cookies={"access_token": admin_token})
    session.expire_all()
    session.refresh(row)
    assert row.acknowledged_at == first_at
    assert row.acknowledged_by == first_by


def test_ack_does_not_create_audit_log(client, admin_token, session):
    """ack 動作本身不寫新 audit log"""
    row = AuditLog(action="DELETE", entity_type="employee", summary="x (不可復原)", username="admin")
    session.add(row)
    session.commit()
    before = session.query(AuditLog).count()
    client.post(f"/api/audit-logs/{row.id}/ack", cookies={"access_token": admin_token})
    session.expire_all()
    after = session.query(AuditLog).count()
    assert after == before, "ack endpoint 不該自己產生 audit log"


def test_ack_requires_audit_logs_permission(client, viewer_token, session):
    row = AuditLog(action="DELETE", entity_type="employee", summary="x (不可復原)", username="admin")
    session.add(row)
    session.commit()
    res = client.post(f"/api/audit-logs/{row.id}/ack", cookies={"access_token": viewer_token})
    assert res.status_code == 403


# ============== POST /audit-logs/ack-all ==============

def test_ack_all_marks_only_unack_in_window(client, admin_token, session):
    unacked = AuditLog(action="DELETE", entity_type="employee", summary="a (不可復原)", username="x")
    already_acked = AuditLog(
        action="DELETE",
        entity_type="employee",
        summary="b (不可復原)",
        username="x",
        acknowledged_at=datetime.now(timezone.utc),
        acknowledged_by=99,
    )
    session.add_all([unacked, already_acked])
    session.commit()

    original_already_ack_at = already_acked.acknowledged_at
    res = client.post("/api/audit-logs/ack-all?days=7", cookies={"access_token": admin_token})
    assert res.status_code == 200
    session.expire_all()
    session.refresh(unacked)
    session.refresh(already_acked)
    assert unacked.acknowledged_at is not None
    assert already_acked.acknowledged_at == original_already_ack_at, "已 ack 不該被重寫"


def test_ack_all_returns_count(client, admin_token, session):
    unacked_a = AuditLog(action="DELETE", entity_type="employee", summary="a (不可復原)", username="x")
    unacked_b = AuditLog(action="BLOCKED_DELETE", entity_type="user", summary="拒絕", username="x")
    session.add_all([unacked_a, unacked_b])
    session.commit()

    res = client.post("/api/audit-logs/ack-all?days=7", cookies={"access_token": admin_token})
    assert res.status_code == 200
    body = res.json()
    assert body["acknowledged_count"] >= 2
```

NOTE: `viewer_token` / `admin_user_id` fixture 若不存在，先 grep `tests/conftest.py` 找等價 fixture 或加最小 fixture。

- [ ] **Step 2: Run to verify fail**

```bash
pytest tests/test_audit_high_risk_router.py -v
```

Expected: 404s — endpoints 還沒接.

- [ ] **Step 3: Add Pydantic schemas + endpoints in `api/audit.py`**

在 `api/audit.py` 既有 router 上方加 schemas（若已有 `schemas/audit.py` 集中檔則加在那）：

```python
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import update

from services.audit_high_risk import filter_high_risk, classify_risk_kind


class AuditLogHighRiskItem(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: str | None
    summary: str
    username: str
    created_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by: int | None
    risk_kind: Literal["hard_delete", "blocked", "permission_change"]

    class Config:
        from_attributes = True


class HighRiskListResponse(BaseModel):
    items: list[AuditLogHighRiskItem]
    unack_count: int
    total: int


class AckAllResponse(BaseModel):
    acknowledged_count: int
```

加 3 endpoint（接續既有 `router = APIRouter(...)`）：

```python
@router.get(
    "/high-risk",
    response_model=HighRiskListResponse,
    summary="高風險 audit 事件列表（紅點用）",
)
def get_high_risk_audits(
    days: int = 7,
    unack_only: bool = True,
    limit: int = 50,
    current_user=Depends(require_permission(Permission.AUDIT_LOGS)),
    session=Depends(get_session_dep),
):
    """回傳高風險 audit 事件清單。

    - 高風險 = HTTP DELETE / BLOCKED_* / 含「(不可復原)」/ user 權限變更
    - 預設 unack_only=True 只回未 ack 的（紅點用）
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    query = filter_high_risk(
        sa.select(AuditLog), since=since, only_unack=unack_only,
    ).limit(limit)
    rows = session.execute(query).scalars().all()

    # 派分 risk_kind
    items = []
    for row in rows:
        item = AuditLogHighRiskItem.model_validate(row).model_copy(
            update={"risk_kind": classify_risk_kind(row)}
        )
        items.append(item)

    # unack_count（永遠以 only_unack=True 算紅點數字）
    unack_count = session.execute(
        sa.select(sa.func.count())
        .select_from(filter_high_risk(sa.select(AuditLog), since=since, only_unack=True).subquery())
    ).scalar() or 0

    total = session.execute(
        sa.select(sa.func.count())
        .select_from(filter_high_risk(sa.select(AuditLog), since=since, only_unack=False).subquery())
    ).scalar() or 0

    return HighRiskListResponse(items=items, unack_count=unack_count, total=total)


@router.post(
    "/{audit_id}/ack",
    summary="標記單筆 audit 為已 ack",
)
def ack_audit(
    audit_id: int,
    current_user=Depends(require_permission(Permission.AUDIT_LOGS)),
    session=Depends(get_session_dep),
    request: Request = None,
):
    row = session.get(AuditLog, audit_id)
    if row is None:
        raise HTTPException(status_code=404, detail="audit log not found")
    if row.acknowledged_at is None:  # idempotent
        row.acknowledged_at = datetime.now(timezone.utc)
        row.acknowledged_by = current_user.get("user_id") if isinstance(current_user, dict) else current_user.id
        session.commit()

    # ack 動作本身不寫 audit log
    if request is not None:
        request.state.audit_skip = True

    return {"ok": True, "id": audit_id, "acknowledged_at": row.acknowledged_at}


@router.post(
    "/ack-all",
    response_model=AckAllResponse,
    summary="標記所有高風險未 ack 為已 ack",
)
def ack_all_audits(
    days: int = 7,
    current_user=Depends(require_permission(Permission.AUDIT_LOGS)),
    session=Depends(get_session_dep),
    request: Request = None,
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    target_ids_q = filter_high_risk(
        sa.select(AuditLog.id), since=since, only_unack=True,
    )
    target_ids = [row[0] for row in session.execute(target_ids_q).all()]

    if target_ids:
        user_id = current_user.get("user_id") if isinstance(current_user, dict) else current_user.id
        session.execute(
            update(AuditLog)
            .where(AuditLog.id.in_(target_ids))
            .values(acknowledged_at=datetime.now(timezone.utc), acknowledged_by=user_id)
        )
        session.commit()

    if request is not None:
        request.state.audit_skip = True

    return AckAllResponse(acknowledged_count=len(target_ids))
```

NOTE：依既有 router import 慣例補 `from datetime import datetime, timezone, timedelta`、`import sqlalchemy as sa`、`from fastapi import Request, HTTPException, Depends`、`from models.audit import AuditLog`、`from utils.permissions import require_permission, Permission`、`from utils.db import get_session_dep`。

- [ ] **Step 4: Run tests to verify pass**

```bash
pytest tests/test_audit_high_risk_router.py -v
```

Expected: 12 passed.

- [ ] **Step 5: Run full audit suite for regression**

```bash
pytest tests/ -k "audit" -v
```

Expected: 全部新 + 既有 audit test 通過（3 pre-existing test_audit_router fail 為已知 MEMORY.md flagged，不修）。

- [ ] **Step 6: Commit**

```bash
git add api/audit.py tests/test_audit_high_risk_router.py
git commit -m "$(cat <<'EOF'
feat(audit): add high-risk listing + ack endpoints

新增 GET /audit-logs/high-risk (filter+pagination+unack_count)、
POST /audit-logs/{id}/ack (idempotent)、POST /audit-logs/ack-all
(批次標已讀)。三個 endpoint 都用 AUDIT_LOGS 守衛；ack 動作本身
透過 request.state.audit_skip=True 不寫新 audit log，避免遞迴。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 7: Push BE branch + open PR draft**

```bash
git push -u origin feat/audit-pattern-and-ack-2026-05-25-backend
gh pr create --draft --title "feat(audit): pattern tightening + ack mechanism" --body "$(cat <<'EOF'
Spec: docs/superpowers/specs/2026-05-25-audit-pattern-and-navigation-design.md
Plan: docs/superpowers/plans/2026-05-25-audit-pattern-and-navigation.md

## 改動
- §2 軟刪/真刪 explicit audit_summary (helper + middleware decorator)
- §3 audit_logs 加 ack 欄位 + 3 個高風險 endpoint + service-layer filter

## Test plan
- [ ] pytest tests/ -k "audit" -v 全綠
- [ ] alembic upgrade heads / downgrade audrsk01 round-trip 正常
- [ ] 手測軟刪一筆 attachment → AuditLog summary 含「軟刪」

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Task 8: 前端 OpenAPI regen + `api/audit.ts` 加 3 函式

**Pre-condition**: BE PR Task 1–7 已合併並 deploy 到 dev backend（或至少 dev server 重啟跑新 endpoint）。

**Branch（從 ivy-frontend main 開）**：`feat/audit-pattern-and-ack-2026-05-25-frontend`

**Files:**
- Modify: `src/api/_generated/schema.d.ts`（OpenAPI codegen output）
- Modify: `src/api/audit.ts`
- Create: `tests/api/audit.test.ts`（若既有 audit test 已存在則 append）

- [ ] **Step 1: Regenerate OpenAPI schema**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
python scripts/dump_openapi.py
cd /Users/yilunwu/Desktop/ivy-frontend
npm run gen:api
```

Expected: `src/api/_generated/schema.d.ts` 更新；diff 應該只含 `/audit-logs/high-risk` / `/audit-logs/{audit_id}/ack` / `/audit-logs/ack-all` 三條 path + 對應 schemas。

- [ ] **Step 2: Verify drift check**

```bash
npm run gen:api:check
```

Expected: clean (committed schema.d.ts 與 regen 一致).

- [ ] **Step 3: Modify `src/api/audit.ts`**

既有 `src/api/audit.ts` 應有基本 audit CRUD 函式。Append 3 新函式：

```typescript
import api from "./index";
import type { ApiResponse, ApiQuery, AxiosResp } from "./_generated/typed";

export async function getHighRiskAudits(params?: {
  days?: number;
  unack_only?: boolean;
  limit?: number;
}): AxiosResp<"/audit-logs/high-risk", "get"> {
  return api.get("/audit-logs/high-risk", { params });
}

export async function ackAudit(auditId: number): Promise<unknown> {
  return api.post(`/audit-logs/${auditId}/ack`);
}

export async function ackAllAudits(params?: { days?: number }): AxiosResp<"/audit-logs/ack-all", "post"> {
  return api.post("/audit-logs/ack-all", null, { params });
}
```

NOTE：若 schema.d.ts 對應 path 是 unknown 型別，先 grep 確認後端 `response_model=HighRiskListResponse` 有寫；若沒寫則回 BE 補。

- [ ] **Step 4: Write failing api test**

`tests/api/audit.test.ts`：

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import api from "@/api";
import { getHighRiskAudits, ackAudit, ackAllAudits } from "@/api/audit";

vi.mock("@/api");

describe("api/audit high-risk endpoints", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("getHighRiskAudits calls /audit-logs/high-risk with params", async () => {
    const mockGet = vi.mocked(api.get).mockResolvedValue({ data: { items: [], unack_count: 0, total: 0 } } as any);
    await getHighRiskAudits({ days: 7, unack_only: true, limit: 50 });
    expect(mockGet).toHaveBeenCalledWith("/audit-logs/high-risk", { params: { days: 7, unack_only: true, limit: 50 } });
  });

  it("ackAudit calls POST /audit-logs/{id}/ack", async () => {
    const mockPost = vi.mocked(api.post).mockResolvedValue({ data: { ok: true } } as any);
    await ackAudit(42);
    expect(mockPost).toHaveBeenCalledWith("/audit-logs/42/ack");
  });

  it("ackAllAudits calls POST /audit-logs/ack-all", async () => {
    const mockPost = vi.mocked(api.post).mockResolvedValue({ data: { acknowledged_count: 5 } } as any);
    await ackAllAudits({ days: 7 });
    expect(mockPost).toHaveBeenCalledWith("/audit-logs/ack-all", null, { params: { days: 7 } });
  });
});
```

- [ ] **Step 5: Run test to verify pass**

```bash
npm test -- tests/api/audit.test.ts
```

Expected: 3 passed.

- [ ] **Step 6: Run typecheck + full vitest**

```bash
npm run typecheck
npm test
```

Expected: 0 type error；vitest 全綠（既有 2522 + 3 新）.

- [ ] **Step 7: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git checkout -b feat/audit-pattern-and-ack-2026-05-25-frontend
git add src/api/_generated/schema.d.ts src/api/audit.ts tests/api/audit.test.ts
git commit -m "$(cat <<'EOF'
feat(audit): add high-risk + ack API client

regen OpenAPI schema.d.ts；api/audit.ts 加 getHighRiskAudits /
ackAudit / ackAllAudits 三個函式。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: `useHighRiskAuditCount` composable

**Files:**
- Create: `src/composables/useHighRiskAuditCount.ts`
- Create: `tests/composables/useHighRiskAuditCount.test.ts`

- [ ] **Step 1: Write failing test**

```typescript
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ref, nextTick } from "vue";
import { mount } from "@vue/test-utils";

vi.mock("@/api/audit", () => ({
  getHighRiskAudits: vi.fn(),
}));

import { getHighRiskAudits } from "@/api/audit";
import { useHighRiskAuditCount } from "@/composables/useHighRiskAuditCount";

describe("useHighRiskAuditCount", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(getHighRiskAudits).mockResolvedValue({ data: { unack_count: 3, items: [], total: 3 } } as any);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  it("初次 mount 拉一次資料", async () => {
    const TestComp = {
      setup() {
        return useHighRiskAuditCount();
      },
      template: "<div>{{ unackCount }}</div>",
    };
    const wrapper = mount(TestComp);
    await vi.runOnlyPendingTimersAsync();
    await nextTick();
    expect(getHighRiskAudits).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toBe("3");
    wrapper.unmount();
  });

  it("每 60 秒輪詢一次", async () => {
    const TestComp = {
      setup() {
        return useHighRiskAuditCount();
      },
      template: "<div></div>",
    };
    const wrapper = mount(TestComp);
    await vi.runOnlyPendingTimersAsync();
    expect(getHighRiskAudits).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(60_000);
    await vi.runOnlyPendingTimersAsync();
    expect(getHighRiskAudits).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });

  it("unmount 後停止輪詢", async () => {
    const TestComp = {
      setup() {
        return useHighRiskAuditCount();
      },
      template: "<div></div>",
    };
    const wrapper = mount(TestComp);
    await vi.runOnlyPendingTimersAsync();
    wrapper.unmount();
    const callsBefore = vi.mocked(getHighRiskAudits).mock.calls.length;
    vi.advanceTimersByTime(120_000);
    await vi.runOnlyPendingTimersAsync();
    expect(vi.mocked(getHighRiskAudits).mock.calls.length).toBe(callsBefore);
  });

  it("refresh() 立即拉一次", async () => {
    const TestComp = {
      setup() {
        return useHighRiskAuditCount();
      },
      template: "<div></div>",
    };
    const wrapper = mount(TestComp);
    await vi.runOnlyPendingTimersAsync();
    const { refresh } = (wrapper.vm as any).$.setupState;
    await refresh();
    expect(getHighRiskAudits).toHaveBeenCalledTimes(2);
    wrapper.unmount();
  });
});
```

- [ ] **Step 2: Run to verify fail**

```bash
npm test -- tests/composables/useHighRiskAuditCount.test.ts
```

Expected: ImportError.

- [ ] **Step 3: Create `src/composables/useHighRiskAuditCount.ts`**

```typescript
import { ref, onMounted, onUnmounted } from "vue";

import { getHighRiskAudits } from "@/api/audit";

const POLL_INTERVAL_MS = 60_000;

export function useHighRiskAuditCount() {
  const unackCount = ref(0);
  const loading = ref(false);
  let timerId: ReturnType<typeof setInterval> | null = null;

  async function refresh(): Promise<void> {
    if (typeof document !== "undefined" && document.hidden) {
      return; // hidden tab 跳過，省 quota
    }
    loading.value = true;
    try {
      const res = await getHighRiskAudits({ days: 7, limit: 1 });
      unackCount.value = (res as any).data?.unack_count ?? 0;
    } catch (e) {
      // 靜默失敗：紅點消失優於假數字
      unackCount.value = 0;
    } finally {
      loading.value = false;
    }
  }

  function stop(): void {
    if (timerId) {
      clearInterval(timerId);
      timerId = null;
    }
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", onVisibility);
    }
  }

  function onVisibility(): void {
    if (!document.hidden) {
      refresh();
    }
  }

  onMounted(() => {
    refresh();
    timerId = setInterval(refresh, POLL_INTERVAL_MS);
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibility);
    }
  });

  onUnmounted(stop);

  return { unackCount, loading, refresh, stop };
}
```

- [ ] **Step 4: Run tests to verify pass**

```bash
npm test -- tests/composables/useHighRiskAuditCount.test.ts
```

Expected: 4 passed.

- [ ] **Step 5: typecheck**

```bash
npm run typecheck
```

Expected: 0 error.

- [ ] **Step 6: Commit**

```bash
git add src/composables/useHighRiskAuditCount.ts tests/composables/useHighRiskAuditCount.test.ts
git commit -m "$(cat <<'EOF'
feat(workbench): useHighRiskAuditCount composable

每 60s 輪詢 GET /audit-logs/high-risk 拉 unack_count 給紅點用。
visibilitychange 隱藏 tab 暫停；onUnmounted cleanup。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: `WorkbenchLayout` + 搬 ApprovalsView + routes

**Files:**
- Create: `src/views/workbench/WorkbenchLayout.vue`
- Move: `src/views/ApprovalsView.vue` → `src/views/workbench/WorkbenchApprovalsView.vue`（內容 byte-identical）
- Modify: `src/router/index.ts`

- [ ] **Step 1: `git mv` ApprovalsView**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
mkdir -p src/views/workbench
git mv src/views/ApprovalsView.vue src/views/workbench/WorkbenchApprovalsView.vue
```

NOTE: 元件 `<script setup>` 內部不改；只是檔位置變。

- [ ] **Step 2: Update router**

`src/router/index.ts` 找既有：

```ts
{
  path: "/approvals",
  component: () => import("@/views/ApprovalsView.vue"),
  // ...
}
```

改成：

```ts
{
  path: "/approvals",
  redirect: "/workbench/approvals",
},
{
  path: "/workbench",
  component: () => import("@/views/workbench/WorkbenchLayout.vue"),
  redirect: "/workbench/approvals",
  meta: { requiresAuth: true },
  children: [
    {
      path: "approvals",
      name: "WorkbenchApprovals",
      component: () => import("@/views/workbench/WorkbenchApprovalsView.vue"),
      meta: { requiresAuth: true, permission: "APPROVALS" },
    },
    {
      path: "high-risk",
      name: "WorkbenchHighRisk",
      component: () => import("@/views/workbench/WorkbenchHighRiskView.vue"),
      meta: { requiresAuth: true, permission: "AUDIT_LOGS" },
    },
  ],
},
```

NOTE: `permission` meta 用既有 router guard 慣例；若 router meta 用其他鍵名（grep `meta.permission` 確認），照原慣例。

- [ ] **Step 3: Create `WorkbenchLayout.vue`**

```vue
<script setup lang="ts">
import { useRoute } from "vue-router";
import { computed } from "vue";

import { useHighRiskAuditCount } from "@/composables/useHighRiskAuditCount";
import { hasPermission } from "@/utils/auth";

const route = useRoute();
const { unackCount } = useHighRiskAuditCount();

const canSeeHighRisk = computed(() => hasPermission("AUDIT_LOGS"));
const activeTab = computed(() => {
  if (route.path.endsWith("/high-risk")) return "high-risk";
  return "approvals";
});
</script>

<template>
  <div class="workbench-layout">
    <el-tabs v-model="activeTab" class="workbench-tabs">
      <el-tab-pane label="待簽核" name="approvals">
        <router-link to="/workbench/approvals" custom v-slot="{ navigate }">
          <span @click="navigate">點此</span>
        </router-link>
      </el-tab-pane>
      <el-tab-pane v-if="canSeeHighRisk" name="high-risk">
        <template #label>
          <span>高風險事件</span>
          <el-badge v-if="unackCount > 0" :value="unackCount" class="ml-1" />
        </template>
        <router-link to="/workbench/high-risk" custom v-slot="{ navigate }">
          <span @click="navigate">點此</span>
        </router-link>
      </el-tab-pane>
    </el-tabs>
    <router-view />
  </div>
</template>

<style scoped>
.workbench-layout {
  padding: 16px;
}
.workbench-tabs {
  margin-bottom: 16px;
}
</style>
```

NOTE: 上面 template 用 router-link + 自訂 slot 是因為 el-tabs name 與 router 同步可能要用 watch；若既有 sidebar 一級項目已負責點擊跳 sub-route，可改用 nav links 顯示樣式但實際 nav 由 sidebar 負責，layout 只負責 sub-tab badge 顯示。實作 階段視既有 AdminLayout 慣例調整。

- [ ] **Step 4: Write layout test**

`tests/views/workbench/WorkbenchLayout.test.ts`：

```typescript
import { describe, it, expect, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createRouter, createWebHistory } from "vue-router";
import ElementPlus from "element-plus";

vi.mock("@/api/audit", () => ({ getHighRiskAudits: vi.fn().mockResolvedValue({ data: { unack_count: 5, items: [], total: 5 } }) }));
vi.mock("@/utils/auth", () => ({ hasPermission: vi.fn(() => true) }));

import WorkbenchLayout from "@/views/workbench/WorkbenchLayout.vue";

describe("WorkbenchLayout", () => {
  function makeRouter() {
    return createRouter({
      history: createWebHistory(),
      routes: [
        { path: "/workbench/approvals", component: { template: "<div>approvals</div>" } },
        { path: "/workbench/high-risk", component: { template: "<div>high-risk</div>" } },
      ],
    });
  }

  it("有 AUDIT_LOGS 權限時顯示「高風險事件」tab + badge", async () => {
    const router = makeRouter();
    const wrapper = mount(WorkbenchLayout, {
      global: { plugins: [router, ElementPlus] },
    });
    await router.isReady();
    expect(wrapper.text()).toContain("高風險事件");
    expect(wrapper.find(".el-badge").exists()).toBe(true);
  });

  it("無 AUDIT_LOGS 權限隱藏「高風險事件」tab", async () => {
    const auth = await import("@/utils/auth");
    vi.mocked(auth.hasPermission).mockReturnValue(false);
    const router = makeRouter();
    const wrapper = mount(WorkbenchLayout, {
      global: { plugins: [router, ElementPlus] },
    });
    await router.isReady();
    expect(wrapper.text()).not.toContain("高風險事件");
  });
});
```

- [ ] **Step 5: Run tests**

```bash
npm test -- tests/views/workbench/WorkbenchLayout.test.ts
npm run typecheck
```

Expected: 2 passed, typecheck 0 error.

- [ ] **Step 6: Verify `/approvals` redirect 與 `/workbench/approvals` 渲染**

```bash
npm run dev
```

手測：
- 開 `http://localhost:5173/approvals` → 自動跳 `/workbench/approvals` → 看到既有簽核內容
- 開 `http://localhost:5173/workbench/high-risk` → 載入 WorkbenchHighRiskView（會錯，因為 Task 11 還沒做；先用 placeholder 確認 routing）

如果還沒做 Task 11，可暫時將 WorkbenchHighRiskView 路徑指向 `WorkbenchApprovalsView.vue` 占位確認 routing。

- [ ] **Step 7: Commit**

```bash
git add src/views/workbench/ src/router/index.ts tests/views/workbench/WorkbenchLayout.test.ts
git commit -m "$(cat <<'EOF'
feat(workbench): scaffold WorkbenchLayout + move ApprovalsView

新增 src/views/workbench/ 群組：WorkbenchLayout (含 2 sub-tab)、
搬 ApprovalsView → WorkbenchApprovalsView (內容不動)。
router /approvals → /workbench/approvals redirect，書籤兼容。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 11: `WorkbenchHighRiskView` 高風險列表 + ack

**Files:**
- Create: `src/views/workbench/WorkbenchHighRiskView.vue`
- Create: `tests/views/workbench/WorkbenchHighRiskView.test.ts`

- [ ] **Step 1: Write failing test**

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import ElementPlus from "element-plus";

vi.mock("@/api/audit", () => ({
  getHighRiskAudits: vi.fn(),
  ackAudit: vi.fn().mockResolvedValue({ data: { ok: true } }),
  ackAllAudits: vi.fn().mockResolvedValue({ data: { acknowledged_count: 3 } }),
}));

import { getHighRiskAudits, ackAudit, ackAllAudits } from "@/api/audit";
import WorkbenchHighRiskView from "@/views/workbench/WorkbenchHighRiskView.vue";

describe("WorkbenchHighRiskView", () => {
  beforeEach(() => {
    vi.mocked(getHighRiskAudits).mockResolvedValue({
      data: {
        items: [
          { id: 1, action: "DELETE", entity_type: "employee", entity_id: "10", summary: "刪除 員工 王小明 (不可復原)", username: "admin", created_at: "2026-05-25T10:00:00Z", acknowledged_at: null, acknowledged_by: null, risk_kind: "hard_delete" },
          { id: 2, action: "BLOCKED_DELETE", entity_type: "user", entity_id: "5", summary: "拒絕刪除使用者", username: "jdoe", created_at: "2026-05-25T11:00:00Z", acknowledged_at: null, acknowledged_by: null, risk_kind: "blocked" },
          { id: 3, action: "UPDATE", entity_type: "user", entity_id: "7", summary: "修改使用者 (role: hr → admin)", username: "admin", created_at: "2026-05-25T12:00:00Z", acknowledged_at: null, acknowledged_by: null, risk_kind: "permission_change" },
        ],
        unack_count: 3,
        total: 3,
      },
    } as any);
  });

  it("渲染 3 種 risk_kind 各一筆", async () => {
    const wrapper = mount(WorkbenchHighRiskView, { global: { plugins: [ElementPlus] } });
    await flushPromises();
    expect(wrapper.text()).toContain("王小明");
    expect(wrapper.text()).toContain("拒絕刪除");
    expect(wrapper.text()).toContain("role");
    expect(wrapper.findAll(".risk-tag").length).toBe(3);
  });

  it("單筆 ack 按鈕呼叫 ackAudit", async () => {
    const wrapper = mount(WorkbenchHighRiskView, { global: { plugins: [ElementPlus] } });
    await flushPromises();
    const ackButtons = wrapper.findAll("[data-test='ack-btn']");
    await ackButtons[0].trigger("click");
    await flushPromises();
    expect(ackAudit).toHaveBeenCalledWith(1);
  });

  it("「全部標已讀」按鈕呼叫 ackAllAudits", async () => {
    const wrapper = mount(WorkbenchHighRiskView, { global: { plugins: [ElementPlus] } });
    await flushPromises();
    const ackAllBtn = wrapper.find("[data-test='ack-all-btn']");
    await ackAllBtn.trigger("click");
    await flushPromises();
    expect(ackAllAudits).toHaveBeenCalled();
  });

  it("empty state 顯示", async () => {
    vi.mocked(getHighRiskAudits).mockResolvedValueOnce({
      data: { items: [], unack_count: 0, total: 0 },
    } as any);
    const wrapper = mount(WorkbenchHighRiskView, { global: { plugins: [ElementPlus] } });
    await flushPromises();
    expect(wrapper.text()).toMatch(/沒有|無高風險|空/);
  });
});
```

- [ ] **Step 2: Run to verify fail**

```bash
npm test -- tests/views/workbench/WorkbenchHighRiskView.test.ts
```

Expected: ImportError.

- [ ] **Step 3: Create `WorkbenchHighRiskView.vue`**

```vue
<script setup lang="ts">
import { ref, onMounted, computed } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { getHighRiskAudits, ackAudit, ackAllAudits } from "@/api/audit";

interface Item {
  id: number;
  action: string;
  entity_type: string;
  entity_id: string | null;
  summary: string;
  username: string;
  created_at: string;
  acknowledged_at: string | null;
  acknowledged_by: number | null;
  risk_kind: "hard_delete" | "blocked" | "permission_change";
}

const items = ref<Item[]>([]);
const loading = ref(false);
const unackOnly = ref(true);

const RISK_TAG_LABEL: Record<Item["risk_kind"], string> = {
  hard_delete: "真刪",
  blocked: "被擋",
  permission_change: "權限變更",
};

const RISK_TAG_TYPE: Record<Item["risk_kind"], string> = {
  hard_delete: "danger",
  blocked: "warning",
  permission_change: "info",
};

async function load(): Promise<void> {
  loading.value = true;
  try {
    const res = await getHighRiskAudits({
      days: 7,
      unack_only: unackOnly.value,
      limit: 100,
    });
    items.value = ((res as any).data?.items ?? []) as Item[];
  } catch (e) {
    ElMessage.error("讀取高風險事件失敗");
  } finally {
    loading.value = false;
  }
}

async function onAck(id: number): Promise<void> {
  await ackAudit(id);
  ElMessage.success("已標為已讀");
  await load();
}

async function onAckAll(): Promise<void> {
  try {
    await ElMessageBox.confirm("確定把所有 7 天內高風險事件標為已讀？", "確認", { type: "warning" });
    const res = await ackAllAudits({ days: 7 });
    const count = (res as any).data?.acknowledged_count ?? 0;
    ElMessage.success(`已標 ${count} 筆為已讀`);
    await load();
  } catch (e) {
    // cancel 不處理
  }
}

onMounted(load);
</script>

<template>
  <div class="high-risk-view">
    <div class="header">
      <h2>高風險事件（近 7 天）</h2>
      <div class="actions">
        <el-checkbox v-model="unackOnly" @change="load">只看未讀</el-checkbox>
        <el-button type="primary" data-test="ack-all-btn" @click="onAckAll">全部標已讀</el-button>
      </div>
    </div>

    <el-empty v-if="!loading && items.length === 0" description="目前沒有高風險事件" />

    <el-table v-else :data="items" v-loading="loading" stripe>
      <el-table-column label="類型" width="120">
        <template #default="{ row }">
          <el-tag :type="RISK_TAG_TYPE[row.risk_kind] as any" class="risk-tag">
            {{ RISK_TAG_LABEL[row.risk_kind] }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="summary" label="摘要" min-width="300" />
      <el-table-column prop="username" label="操作者" width="120" />
      <el-table-column prop="created_at" label="時間" width="180" />
      <el-table-column label="動作" width="120">
        <template #default="{ row }">
          <el-button
            v-if="!row.acknowledged_at"
            size="small"
            data-test="ack-btn"
            @click="onAck(row.id)"
          >
            標已讀
          </el-button>
          <span v-else class="acked-label">已讀</span>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.high-risk-view {
  padding: 16px;
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.acked-label {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
```

- [ ] **Step 4: Run tests**

```bash
npm test -- tests/views/workbench/WorkbenchHighRiskView.test.ts
npm run typecheck
```

Expected: 4 passed, typecheck 0.

- [ ] **Step 5: Commit**

```bash
git add src/views/workbench/WorkbenchHighRiskView.vue tests/views/workbench/WorkbenchHighRiskView.test.ts
git commit -m "$(cat <<'EOF'
feat(workbench): WorkbenchHighRiskView with ack + ack-all

新 view 顯示近 7 天高風險事件（3 種 risk_kind tag）+ 單筆 ack +
全部標已讀；empty state；ack 後自動 reload 更新紅點。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 12: AdminSidebar IA 重整 + 接 composable

**Files:**
- Modify: `src/components/layout/AdminSidebar.vue`
- Modify: `src/layouts/AdminLayout.vue`（或父層接 composable）
- Modify: `tests/components/AdminSidebar.test.ts`

- [ ] **Step 1: Append failing tests to existing `AdminSidebar.test.ts`**

```typescript
// 在既有 describe block 內 append：

it("「工作台」一級項目存在", async () => {
  const wrapper = mount(AdminSidebar, {
    props: { pendingApprovals: 0, pendingHighRiskAudit: 0 },
    global: { plugins: [router, ElementPlus] },
  });
  expect(wrapper.text()).toContain("工作台");
});

it("「報表」一級項目存在且含操作紀錄子項", async () => {
  const wrapper = mount(AdminSidebar, {
    props: {},
    global: { plugins: [router, ElementPlus] },
  });
  expect(wrapper.text()).toContain("報表");
  expect(wrapper.text()).toContain("操作紀錄");
});

it("workbenchBadge = pendingApprovals + pendingHighRiskAudit", async () => {
  const wrapper = mount(AdminSidebar, {
    props: { pendingApprovals: 3, pendingHighRiskAudit: 2 },
    global: { plugins: [router, ElementPlus] },
  });
  // sidebar 應渲染 5 在工作台一級
  expect(wrapper.html()).toContain("5");
});

it("無 AUDIT_LOGS 權限不顯示「操作紀錄」連結", async () => {
  const hasPerm = vi.fn((p: string) => p !== "AUDIT_LOGS");
  vi.doMock("@/utils/auth", () => ({ hasPermission: hasPerm }));
  const wrapper = mount(AdminSidebar, { global: { plugins: [router, ElementPlus] } });
  expect(wrapper.find('a[href="/audit-logs"]').exists()).toBe(false);
});
```

- [ ] **Step 2: Run to verify fail**

```bash
npm test -- tests/components/AdminSidebar.test.ts
```

Expected: 新 case 失敗（無「工作台」一級、無「報表」一級）。

- [ ] **Step 3: Modify `AdminSidebar.vue` props**

加 prop：

```ts
const props = defineProps<{
  pendingApprovals?: number;
  pendingActivityInquiries?: number;
  pendingHighRiskAudit?: number;  // 新
}>();

const workbenchBadge = computed(() =>
  (props.pendingApprovals ?? 0) + (props.pendingHighRiskAudit ?? 0)
);
```

- [ ] **Step 4: 重整 template IA**

完整新 sidebar 結構（spec §4.2）：

```vue
<template>
  <el-menu :default-active="$route.path" router class="sidebar">
    <!-- 1. 儀表板 -->
    <el-menu-item index="/" v-if="hasPermission('DASHBOARD')">
      <el-icon><Odometer /></el-icon>
      <span>儀表板</span>
    </el-menu-item>

    <!-- 2. 工作台（新合併）-->
    <el-sub-menu index="workbench" v-if="hasPermission('APPROVALS') || hasPermission('AUDIT_LOGS')">
      <template #title>
        <el-icon><Tickets /></el-icon>
        <span>工作台</span>
        <el-badge v-if="workbenchBadge > 0" :value="workbenchBadge" class="menu-badge" />
      </template>
      <el-menu-item index="/workbench/approvals" v-if="hasPermission('APPROVALS')">
        待簽核
        <el-badge v-if="pendingApprovals" :value="pendingApprovals" class="ml-1" />
      </el-menu-item>
      <el-menu-item index="/workbench/high-risk" v-if="hasPermission('AUDIT_LOGS')">
        高風險事件
        <el-badge v-if="pendingHighRiskAudit" :value="pendingHighRiskAudit" class="ml-1" />
      </el-menu-item>
    </el-sub-menu>

    <!-- 3. 人事薪資（移除考核管理） -->
    <el-sub-menu index="hr" v-if="...">
      <!-- ... 員工管理 / 薪資管理 / 年終獎金 / 年終 payout / 出勤 / 請假 / 加班 / 排班 -->
    </el-sub-menu>

    <!-- 4. 學生與班級（不動）-->
    <!-- 5. 園務統計（移除報表統計、經營分析）-->
    <!-- 6. 園務行政（不動）-->
    <!-- 7. 課後才藝（移除修改紀錄、報名時間設定）-->

    <!-- 8. 報表（新一級）-->
    <el-sub-menu index="reports" v-if="hasPermission('AUDIT_LOGS') || hasPermission('REPORTS') || hasPermission('SALARY_READ') || hasPermission('BUSINESS_ANALYTICS') || hasPermission('ACTIVITY_READ')">
      <template #title>
        <el-icon><DataLine /></el-icon>
        <span>報表</span>
      </template>
      <el-menu-item index="/audit-logs" v-if="hasPermission('AUDIT_LOGS')">操作紀錄</el-menu-item>
      <el-menu-item index="/activity/changes" v-if="hasPermission('ACTIVITY_READ')">修改紀錄</el-menu-item>
      <el-menu-item index="/admin/gov-reports/monthly" v-if="hasPermission('SALARY_READ')">月度月報</el-menu-item>
      <el-menu-item index="/gov-reports" v-if="hasPermission('SALARY_READ')">政府申報匯出</el-menu-item>
      <el-menu-item index="/reports" v-if="hasPermission('REPORTS')">報表統計</el-menu-item>
      <el-menu-item index="/analytics" v-if="hasPermission('BUSINESS_ANALYTICS')">經營分析</el-menu-item>
    </el-sub-menu>

    <!-- 9. 系統設定（保留一般設定 + 遷入考核管理、報名時間設定，移除操作紀錄）-->
    <el-sub-menu index="settings" v-if="hasPermission('SETTINGS_READ') || hasPermission('ACTIVITY_WRITE')">
      <template #title>
        <el-icon><Setting /></el-icon>
        <span>系統設定</span>
      </template>
      <el-menu-item index="/settings" v-if="hasPermission('SETTINGS_READ')">一般設定</el-menu-item>
      <el-menu-item index="/appraisal-management" v-if="hasPermission('SETTINGS_READ')">考核管理</el-menu-item>
      <el-menu-item index="/activity/settings" v-if="hasPermission('ACTIVITY_WRITE')">報名時間設定</el-menu-item>
    </el-sub-menu>
  </el-menu>
</template>
```

NOTE: 既有 sidebar 細節（icon、`v-if` permission、route 命名）依現況保留；只調動分群歸屬。完整 9 一級結構參考 spec §4.2。

- [ ] **Step 5: Modify `AdminLayout.vue` 接 composable**

找父層元件（AdminLayout 或 App.vue 内 admin 區段）的 sidebar 渲染處，加 composable：

```vue
<script setup lang="ts">
import { useHighRiskAuditCount } from "@/composables/useHighRiskAuditCount";
import { useApprovalsStore } from "@/stores/approvals"; // 假設既有

const approvalsStore = useApprovalsStore();
const { unackCount } = useHighRiskAuditCount();
</script>

<template>
  <AdminSidebar
    :pending-approvals="approvalsStore.pendingCount"
    :pending-high-risk-audit="unackCount"
    :pending-activity-inquiries="..."
  />
  <!-- ... -->
</template>
```

- [ ] **Step 6: Run tests**

```bash
npm test -- tests/components/AdminSidebar.test.ts
npm run typecheck
```

Expected: 既有 + 新 case 全綠.

- [ ] **Step 7: Smoke test in browser**

```bash
npm run dev
```

手測 spec §5.4 checklist 前半（sidebar 結構 + 紅點顯示）。

- [ ] **Step 8: Commit**

```bash
git add src/components/layout/AdminSidebar.vue src/layouts/AdminLayout.vue tests/components/AdminSidebar.test.ts
git commit -m "$(cat <<'EOF'
feat(workbench): restructure admin sidebar + wire high-risk badge

AdminSidebar 重整為 9 一級：新增「工作台」(合併簽核+高風險) 與
「報表」(收 audit/changes/月報/政府/reports/analytics)。
pendingHighRiskAudit prop 由父層 useHighRiskAuditCount 注入。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 13: 整合驗收 + push FE PR

**Files:**
- 無 code 改動，純驗證。

- [ ] **Step 1: Full BE pytest**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/ -q 2>&1 | tail -20
```

Expected: 全綠（除 3 pre-existing `test_audit_router` failures 已知 per MEMORY.md）.

- [ ] **Step 2: Full FE vitest**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
npm test 2>&1 | tail -10
```

Expected: 2522 pre-existing + ~25 new = ~2547 passed, 0 failed.

- [ ] **Step 3: Typecheck + build**

```bash
npm run typecheck
npm run build
```

Expected: 0 type error; build success.

- [ ] **Step 4: OpenAPI drift check**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
python scripts/dump_openapi.py
cd /Users/yilunwu/Desktop/ivy-frontend
npm run gen:api:check
```

Expected: porcelain clean（schema.d.ts 與後端 OpenAPI 一致）.

- [ ] **Step 5: Dev server smoke per spec §5.4 checklist**

```bash
cd /Users/yilunwu/Desktop/ivyManageSystem
./start.sh
```

手動驗 8 條：

- [ ] 軟刪一個 attachment → AuditLog（/audit-logs）看到「軟刪 附件 ...」、action=UPDATE
- [ ] 真刪一個 vendor payment → AuditLog 看到「刪除 廠商付款 #N (不可復原)」、action=DELETE
- [ ] 改一個 user 的 role → 工作台/高風險事件看到、kind=permission_change
- [ ] 點某筆「標已讀」→ 該筆灰顯、紅點 -1
- [ ] 「全部標已讀」→ 紅點歸零
- [ ] sidebar 9 個一級結構正確、`/approvals` 自動跳 `/workbench/approvals`
- [ ] 用無 AUDIT_LOGS 權限的 admin 帳號登入 → 看不到「高風險事件」sub-tab + 看不到「報表」群內的「操作紀錄」連結
- [ ] sidebar `pendingHighRiskAudit` badge 與「工作台」一級紅點數字一致

- [ ] **Step 6: Push FE branch + open PR**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git push -u origin feat/audit-pattern-and-ack-2026-05-25-frontend
gh pr create --draft --title "feat(workbench): high-risk audit ack + sidebar IA restructure" --body "$(cat <<'EOF'
Spec: ivy-backend/docs/superpowers/specs/2026-05-25-audit-pattern-and-navigation-design.md
Plan: ivy-backend/docs/superpowers/plans/2026-05-25-audit-pattern-and-navigation.md

依賴 BE PR feat/audit-pattern-and-ack-2026-05-25-backend 先合併並 deploy。

## 改動
- §4.2 sidebar 重整為 9 一級結構（新增「工作台」「報表」）
- §4.3 紅點：pendingHighRiskAudit + useHighRiskAuditCount 60s 輪詢
- §4.4 新元件：WorkbenchLayout / WorkbenchHighRiskView
- §4.5 `/approvals` → `/workbench/approvals` 301 redirect

## Test plan
- [x] vitest 全綠（新增 ~25 case）
- [x] typecheck + build 通過
- [x] OpenAPI drift clean
- [ ] 手測 spec §5.4 checklist 8 條（依 plan Task 13 Step 5）

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Plan 完成

13 task 對應 spec §2–§5：

| Spec section | Plan tasks |
|---|---|
| §2 audit_summary 語意化 | Task 1 (helper + middleware) / Task 2-4 (軟刪 endpoint) |
| §3 ack 機制 | Task 5 (migration) / Task 6 (filter service) / Task 7 (3 endpoint) |
| §4 IA + 紅點 | Task 8 (api/audit.ts) / Task 9 (composable) / Task 10 (layout + routes) / Task 11 (high-risk view) / Task 12 (sidebar 重整) |
| §5 驗收 | Task 13 |

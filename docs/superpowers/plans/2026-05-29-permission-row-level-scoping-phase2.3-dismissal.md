# 權限 Row-Level Scoping Phase 2.3 DISMISSAL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 2 條 DISMISSAL 權限（`DISMISSAL_CALLS_READ` `DISMISSAL_CALLS_WRITE`）納入 row-level scoping，啟用「資深老師看跨班放學叫車」的自訂角色情境。

**Architecture:** `api/portal/_shared.py:_get_teacher_classroom_ids` 是 portal 專用的本地 helper（**非** `utils/portfolio_access.py` 那套）。最小變更：保留 `_get_teacher_classroom_ids` 不動，在 `api/portal/dismissal_calls.py` 4 個 endpoint 的「classroom_ids 決策」前面加一層 `is_unrestricted(current_user, code=...)` gate — True 則跳過 classroom_id filter（走 :all scope），False 則沿用既有 `_get_teacher_classroom_ids` 邏輯。

**Tech Stack:** Python 3.13/3.14 + FastAPI + SQLAlchemy + Alembic + pytest；PostgreSQL prod / SQLite test。

**Pre-flight：必先**
- 確認 Phase 2.2 已 ship 或開新 worktree from origin/main（Phase 2.3 與 2.2 無強耦合可平行 ship，但 migration 必須在 permscope03 之後 → `down_revision = "permscope03"`）
- 對既有 `.py` 改動全用 `python3` string.replace 繞 black PostToolUse hook

---

## File Structure

| File | 改動類型 | 估計 LOC |
|------|---------|---------|
| `alembic/versions/20260530_permscope04_dismissal.py` | seed 2 perm `scope_options` + backfill teacher | +120 |
| `tests/test_alembic_permscope04.py` | upgrade/downgrade test | +60 |
| `api/portal/dismissal_calls.py` | 4 endpoint 加 `is_unrestricted(code=)` gate | +20/-0 |
| `tests/test_permscope_dismissal.py` | integration tests for 3 角色 × 4 endpoint | +180 |

無新增 helper、無 frontend 變動、無 schema 結構變動（只 seed scope_options 字串）。

---

## Task 1: Migration `permscope04_dismissal` seed + backfill

**Files:**
- Create: `alembic/versions/20260530_permscope04_dismissal.py`
- Test: `tests/test_alembic_permscope04.py`

仿 `permscope01` / `permscope03` 結構，差別只在 SCOPE_AWARE_CODES：

```python
revision = "permscope04"
down_revision = "permscope03"  # Phase 2.2 head；若 2.2 未 ship 改 "permscope01"

SCOPE_AWARE_CODES = (
    "DISMISSAL_CALLS_READ",
    "DISMISSAL_CALLS_WRITE",
)
```

`downgrade()` 必須限定僅剝 DISMISSAL_CALLS_* 後綴，不要動其他 family。

- [ ] **Step 1: pre-flight 確認 alembic head**

```bash
cd /abs/path/worktree && alembic heads
```

- [ ] **Step 2: 寫 alembic test（FAIL）** — 仿 `tests/test_alembic_permscope03.py`
- [ ] **Step 3: 建 migration file** — 仿 permscope01
- [ ] **Step 4: Run test PASS + alembic single head**
- [ ] **Step 5: Commit**

```bash
git commit -m "feat(alembic): permscope04 seed DISMISSAL_CALLS scope_options + backfill teacher"
```

---

## Task 2: `api/portal/dismissal_calls.py` — 4 endpoint 加 `is_unrestricted(code=)` gate

**Files:**
- Modify: `api/portal/dismissal_calls.py:65-95` `:98-126` `:129-183`

### Current pattern (L65-95 READ list endpoint)

```python
@router.get("/dismissal-calls")
def portal_list_dismissal_calls(
    current_user: dict = Depends(require_permission(Permission.DISMISSAL_CALLS_READ)),
):
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)
        if not classroom_ids:
            return []
        # ... SQL filter by classroom_ids ...
```

### Target pattern

```python
@router.get("/dismissal-calls")
def portal_list_dismissal_calls(
    current_user: dict = Depends(require_permission(Permission.DISMISSAL_CALLS_READ)),
):
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        # :all scope → 跳過 classroom filter（看全校放學叫車）
        unrestricted = is_unrestricted(
            current_user, code=Permission.DISMISSAL_CALLS_READ.value
        )
        if not unrestricted:
            classroom_ids = _get_teacher_classroom_ids(session, emp.id)
            if not classroom_ids:
                return []
        else:
            classroom_ids = None  # sentinel：不加 classroom filter

        today = today_taipei()
        day_start = datetime.combine(today, _DAY_START)
        day_end = datetime.combine(today, _DAY_END)

        q = session.query(StudentDismissalCall).filter(
            StudentDismissalCall.status.in_(["pending", "acknowledged"]),
            StudentDismissalCall.requested_at >= day_start,
            StudentDismissalCall.requested_at <= day_end,
        )
        if classroom_ids is not None:
            q = q.filter(StudentDismissalCall.classroom_id.in_(classroom_ids))
        calls = q.order_by(StudentDismissalCall.requested_at.desc()).all()

        return _build_calls_out_bulk(calls, session)
    finally:
        session.close()
```

對 `pending-count` (L98-126) 同樣 pattern。

對 `_db_transition_call` (L129-183) — 這個 helper 由 acknowledge / complete 共用，加 `is_unrestricted(current_user, code=Permission.DISMISSAL_CALLS_WRITE.value)` gate，若 True 則跳過 L154 的 `call.classroom_id not in classroom_ids` 檢查（仍保留「call 不存在」分支）。

### Implementation steps

- [ ] **Step 1: pre-flight 確認 import 與 helper 位置**

```bash
grep -n "^from\|^import" /abs/path/worktree/api/portal/dismissal_calls.py | head -10
grep -n "is_unrestricted" /abs/path/worktree/api/portal/dismissal_calls.py
```

預期：原本沒 import `is_unrestricted`，需從 `utils.portfolio_access` 加。

- [ ] **Step 2: 寫 integration test（FAIL）**

```python
# tests/test_permscope_dismissal.py
"""Phase 2.3 integration tests：3 角色 × dismissal_calls 4 endpoint。"""

def test_admin_wildcard_sees_all_dismissal_calls(
    client, admin_user, dismissal_call_class_a, dismissal_call_class_b
):
    res = client.get("/portal/dismissal-calls", headers=admin_user.auth)
    assert res.status_code == 200
    ids = {c["id"] for c in res.json()}
    assert dismissal_call_class_a.id in ids
    assert dismissal_call_class_b.id in ids


def test_teacher_own_class_sees_own_class_only(
    client, teacher_class_a_own_class_scope,
    dismissal_call_class_a, dismissal_call_class_b
):
    res = client.get("/portal/dismissal-calls", headers=teacher_class_a_own_class_scope.auth)
    ids = {c["id"] for c in res.json()}
    assert dismissal_call_class_a.id in ids
    assert dismissal_call_class_b.id not in ids


def test_teacher_all_scope_sees_all_classes(
    client, teacher_class_a_all_scope,
    dismissal_call_class_a, dismissal_call_class_b
):
    # teacher 有 DISMISSAL_CALLS_READ:all（自訂角色）
    res = client.get("/portal/dismissal-calls", headers=teacher_class_a_all_scope.auth)
    ids = {c["id"] for c in res.json()}
    assert dismissal_call_class_a.id in ids
    assert dismissal_call_class_b.id in ids


def test_acknowledge_teacher_own_class_can_acknowledge_own(
    client, teacher_class_a_own_class_scope, dismissal_call_class_a_pending
):
    res = client.post(
        f"/portal/dismissal-calls/{dismissal_call_class_a_pending.id}/acknowledge",
        headers=teacher_class_a_own_class_scope.auth,
    )
    assert res.status_code == 200


def test_acknowledge_teacher_own_class_cannot_acknowledge_other(
    client, teacher_class_a_own_class_scope, dismissal_call_class_b_pending
):
    res = client.post(
        f"/portal/dismissal-calls/{dismissal_call_class_b_pending.id}/acknowledge",
        headers=teacher_class_a_own_class_scope.auth,
    )
    assert res.status_code == 403  # F-006 generic 403


def test_acknowledge_teacher_all_scope_can_acknowledge_any(
    client, teacher_class_a_all_scope, dismissal_call_class_b_pending
):
    res = client.post(
        f"/portal/dismissal-calls/{dismissal_call_class_b_pending.id}/acknowledge",
        headers=teacher_class_a_all_scope.auth,
    )
    assert res.status_code == 200  # :all 跨班通過


def test_pending_count_teacher_own_class_counts_own(
    client, teacher_class_a_own_class_scope,
    dismissal_call_class_a_pending, dismissal_call_class_b_pending
):
    res = client.get("/portal/dismissal-calls/pending-count", headers=teacher_class_a_own_class_scope.auth)
    assert res.json()["count"] == 1


def test_pending_count_teacher_all_scope_counts_all(
    client, teacher_class_a_all_scope,
    dismissal_call_class_a_pending, dismissal_call_class_b_pending
):
    res = client.get("/portal/dismissal-calls/pending-count", headers=teacher_class_a_all_scope.auth)
    assert res.json()["count"] == 2
```

Run: `pytest tests/test_permscope_dismissal.py -v`
Expected: `:all` scope 相關 test FAIL（current behavior 沒讀 scope）。

- [ ] **Step 3: 用 python3 surgical edit 加 4 個 gate**

```bash
python3 - <<'EOF'
import pathlib
p = pathlib.Path("/abs/path/worktree/api/portal/dismissal_calls.py")
text = p.read_text()

# 1. 加 import
old_import = "from utils.permissions import Permission, require_permission"  # 確認原文
new_import = "from utils.permissions import Permission, require_permission\nfrom utils.portfolio_access import is_unrestricted"
assert old_import in text or "from utils.permissions import" in text
# 視原文修正 — pre-flight 確認後寫實際 replace

# 2. READ list endpoint L65-95
old_read = '''        emp = _get_employee(session, current_user)
        classroom_ids = _get_teacher_classroom_ids(session, emp.id)
        if not classroom_ids:
            return []

        today = today_taipei()
        day_start = datetime.combine(today, _DAY_START)
        day_end = datetime.combine(today, _DAY_END)

        calls = (
            session.query(StudentDismissalCall)
            .filter(
                StudentDismissalCall.classroom_id.in_(classroom_ids),'''
new_read = '''        emp = _get_employee(session, current_user)
        unrestricted = is_unrestricted(
            current_user, code=Permission.DISMISSAL_CALLS_READ.value
        )
        classroom_ids = None
        if not unrestricted:
            classroom_ids = _get_teacher_classroom_ids(session, emp.id)
            if not classroom_ids:
                return []

        today = today_taipei()
        day_start = datetime.combine(today, _DAY_START)
        day_end = datetime.combine(today, _DAY_END)

        q = session.query(StudentDismissalCall).filter('''
# ... 完整 replace 含 SQL filter 條件式 if classroom_ids is not None
EOF
```

實作時必須用 line-precise edit（grep 確認原文後做完整 string match）；上面 snippet 只示意。

- [ ] **Step 4: Run test PASS + 既有 dismissal test 零 regression**

```bash
pytest tests/test_permscope_dismissal.py tests/test_portal_dismissal_calls.py -v 2>&1 | tail -30
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(portal/dismissal_calls): 4 endpoint 加 is_unrestricted(code=) gate 支援 :all scope

啟用自訂「資深老師」角色透過 DISMISSAL_CALLS_READ:all / :WRITE:all 跨班看
與處理放學叫車。teacher :own_class 與 admin 行為不變。"
```

---

## Task 3: Final integration verification

- [ ] **Step 1: Run focused suite**

```bash
pytest tests/test_permscope_dismissal.py tests/test_alembic_permscope04.py -v
```

- [ ] **Step 2: Run regression — portal/dismissal_calls + admin dismissal_calls**

```bash
pytest tests/test_portal_dismissal_calls.py tests/test_dismissal_calls.py -v 2>&1 | tail -20
```

- [ ] **Step 3: Run full backend pytest**

```bash
pytest 2>&1 | tail -10
```

Expected: baseline 持平。

- [ ] **Step 4: Frontend OpenAPI 漂移檢查** — 無 schema 變動，skip。

- [ ] **Step 5: Push origin**

```bash
git push -u origin feat/permission-row-level-scoping-phase2.3-dismissal-2026-05-30-backend
```

---

## Out of scope

- Admin `api/dismissal_calls.py` 不變動（admin role 已透過 `is_unrestricted(role-based)` 預設放行；無「資深 admin 看跨園」需求）
- Frontend — `getPermissionScope` 已通用 helper，無 family-specific 改動
- `_get_teacher_classroom_ids` 本身保留不動（portal-local helper，重構成本不划算；未來若多個 family 都用可考慮抽到 portfolio_access）

# 權限 Row-Level Scoping Phase 2.1 PORTFOLIO Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate 8 router/service files (`api/portfolio/*.py` 7 files + `api/attachments.py`) to use permission-aware `accessible_classroom_ids(session, user, code=)` for PORTFOLIO_READ/WRITE/PUBLISH scope. Plus alembic migration seeding `scope_options` and backfilling teacher role.

**Architecture:** No new infrastructure — reuses Phase 1's `portfolio_access` bridge. Each `accessible_classroom_ids(session, user)` call site gains a `code=Permission.PORTFOLIO_xxx.value` argument matching the endpoint's `require_permission(...)` perm. Migration follows Phase 1 `permscope01` pattern: PG + SQLite dual path, teacher role permissions transform bare → `:own_class`, teacher users' permission_names bumped + token_version + 1.

**Tech Stack:** Python 3.13 + FastAPI + SQLAlchemy + Alembic + PostgreSQL (test path uses SQLite).

**Spec reference:** `docs/superpowers/specs/2026-05-29-permission-row-level-scoping-phase2-design.md`

**Precondition:** Phase 1 must be merged and `alembic upgrade head` run on local DB. Verify before starting:
```bash
cd /Users/yilunwu/Desktop/ivy-backend && alembic heads
# expected: permscope01 (head)
```

---

## File Map

### Backend create
- `alembic/versions/20260530_permscope02_portfolio_seed_and_backfill.py` — seed scope_options for 3 PORTFOLIO codes + teacher role/user backfill + token_version bump
- `tests/test_alembic_permscope02.py` — migration tests
- `tests/test_portfolio_scope_permission_aware.py` — integration tests for migrated routers

### Backend modify (8 files, surgical `code=` additions only)
- `api/portfolio/auto_milestone.py` — 2 calls
- `api/portfolio/measurements.py` — 6 calls
- `api/portfolio/milestones.py` — 5 calls
- `api/portfolio/observations.py` — 5 calls
- `api/portfolio/reports.py` — 7 calls
- `api/portfolio/student_attachments.py` — 2 calls
- `api/portfolio/timeline.py` — 2 calls
- `api/attachments.py` — 6 calls

### Out of scope
- `api/portal/students.py` `api/portal/class_hub.py` `api/portal/contact_book*.py` `api/contact_book_ws.py` (also reference PORTFOLIO_READ) — investigate per-endpoint case; tentatively in Phase 2.2 (cross-cutting)
- Frontend — no changes (Phase 1 already shipped `getPermissionScope`)

---

## Task 1: Migration permscope02 — Portfolio seed + teacher backfill

**Files:**
- Create: `alembic/versions/20260530_permscope02_portfolio_seed_and_backfill.py`
- Test: `tests/test_alembic_permscope02.py`

- [ ] **Step 1: Pre-flight checks**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
alembic heads
# Must show: permscope01 (head)

ls alembic/versions/ | grep -i permscope02
# Must be empty (no existing file)
```

If heads ≠ permscope01, escalate.

- [ ] **Step 2: Write failing migration test**

```python
# tests/test_alembic_permscope02.py
# Adapt Task 1 (Phase 1) pattern from test_alembic_permscope01.py
# - test_upgrade_seeds_three_portfolio_codes_with_scope_options
#   verify PORTFOLIO_READ, PORTFOLIO_WRITE, PORTFOLIO_PUBLISH have scope_options=['own_class','all']
# - test_upgrade_other_codes_unchanged
#   verify e.g., DASHBOARD still has scope_options=NULL
# - test_upgrade_teacher_role_portfolio_permissions_get_own_class_suffix
#   teacher.permissions had ['PORTFOLIO_READ', ...] → ['PORTFOLIO_READ:own_class', ...]
# - test_upgrade_admin_role_unchanged
#   admin role permissions untouched
# - test_upgrade_existing_teacher_user_backfilled
#   teacher user with permission_names=['PORTFOLIO_READ','STUDENTS_HEALTH_READ']
#   → ['PORTFOLIO_READ:own_class', 'STUDENTS_HEALTH_READ'] (STUDENTS_HEALTH_READ unchanged, not in Phase 2.1)
#   AND token_version bumped
# - test_upgrade_skips_wildcard_teacher_user
#   teacher with ['*'] → unchanged, token_version unchanged
# - test_downgrade_restores_bare_codes_and_bumps_token_version
#   downgrade strips :own_class suffix from PORTFOLIO codes, leaves other codes' suffix alone
#   AND bumps token_version (per Phase 1 review fix)
```

Use existing `_AlembicOpStub` pattern from `tests/test_alembic_permscope01.py` — adapt the `SCOPE_AWARE_CODES` to the 3 PORTFOLIO codes.

- [ ] **Step 3: Run test — verify FAIL**

```bash
pytest tests/test_alembic_permscope02.py -v
# Expected: FAIL (migration file doesn't exist)
```

- [ ] **Step 4: Write migration**

Copy `alembic/versions/20260529_permscope01_permission_scope_options.py` as a starting point. Key changes:

```python
revision = "permscope02"
down_revision = "permscope01"
# ... (NO branch_labels, NO depends_on)

SCOPE_AWARE_CODES = (
    "PORTFOLIO_READ",
    "PORTFOLIO_WRITE",
    "PORTFOLIO_PUBLISH",
)

def upgrade() -> None:
    # 1. NO column add (permscope01 already added scope_options)
    # 2. seed scope_options for 3 PORTFOLIO codes (PG + SQLite paths)
    # 3. teacher role: bare PORTFOLIO_* → :own_class
    # 4. teacher users: same transform + token_version bump
    # ... (copy pattern from permscope01 upgrade())

def downgrade() -> None:
    # ONLY strip :own_class from PORTFOLIO_* codes (not all codes)
    # bump token_version
    # ... (copy pattern but constrain to SCOPE_AWARE_CODES via WHERE clause)
```

**Critical:** the downgrade must NOT strip `:own_class` from STUDENTS_* codes (those belong to Phase 1's migration permscope01). Use this approach:

```python
def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # strip ONLY PORTFOLIO_* suffixes
        op.execute(f"""
            UPDATE users
            SET permission_names = ARRAY(
                SELECT CASE
                    WHEN p IN ({sql_list_of_scoped_portfolio_codes}) THEN split_part(p, ':', 1)
                    ELSE p
                END
                FROM unnest(permission_names) AS p
            ),
            token_version = COALESCE(token_version, 0) + 1
            WHERE role = 'teacher'
        """)
        # similarly for roles table
        # unseed scope_options on PORTFOLIO_* codes (set back to NULL)
        op.execute(f"""
            UPDATE permission_definitions
            SET scope_options = NULL
            WHERE code IN ({sql_list_of_portfolio_codes})
        """)
    else:
        # SQLite path: load, transform (split only matching codes), write back
        # ... (mirror SQLite logic from permscope01 but filter by SCOPE_AWARE_CODES)
```

For SQLite path explicitly check `if base_code in SCOPE_AWARE_CODES` when stripping, rather than universal `split_part(p, ':', 1)`.

- [ ] **Step 5: Run test — verify PASS**

```bash
pytest tests/test_alembic_permscope02.py -v
# Expected: all 7 PASS
```

- [ ] **Step 6: Verify single head**

```bash
alembic heads
# Expected: permscope02 (head)
```

- [ ] **Step 7: Commit**

```bash
git add alembic/versions/20260530_permscope02_portfolio_seed_and_backfill.py tests/test_alembic_permscope02.py
git commit -m "feat(permissions): alembic permscope02 — PORTFOLIO_* scope_options seed + teacher backfill"
```

---

## Task 2: Migrate api/portfolio/timeline.py (smallest, prove the pattern)

**Files:**
- Modify: `api/portfolio/timeline.py` (2 calls to portfolio_access)
- Test: `tests/test_portfolio_scope_permission_aware.py` (new file, will grow per task)

- [ ] **Step 1: Survey current state**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
grep -n "accessible_classroom_ids\|is_unrestricted\|require_permission\|@router\." api/portfolio/timeline.py
```

Expect: 1-2 endpoints each with `require_permission(Permission.PORTFOLIO_READ)` + 1-2 `accessible_classroom_ids(session, user)` calls.

- [ ] **Step 2: Write failing test**

```python
# tests/test_portfolio_scope_permission_aware.py
import pytest
from utils.portfolio_access import is_unrestricted, accessible_classroom_ids

def test_timeline_endpoint_uses_portfolio_read_code():
    """Regression: ensure api/portfolio/timeline.py passes code=Permission.PORTFOLIO_READ.value"""
    import api.portfolio.timeline as mod
    import inspect

    source = inspect.getsource(mod)
    # Must contain code= argument with PORTFOLIO_READ
    assert "code=Permission.PORTFOLIO_READ" in source or \
           'code="PORTFOLIO_READ"' in source, \
           "api/portfolio/timeline.py should pass code= to accessible_classroom_ids"
```

Also add an integration test fixture if practical (verify a teacher with `:own_class` sees only own-class timeline entries).

- [ ] **Step 3: Run test — verify FAIL**

```bash
pytest tests/test_portfolio_scope_permission_aware.py -v
# Expected: FAIL (code= not present yet)
```

- [ ] **Step 4: Apply surgical edit via python3**

```bash
python3 << 'PYEOF'
import re
path = 'api/portfolio/timeline.py'
with open(path) as f: src = f.read()

# Find each accessible_classroom_ids(session, current_user) call and add code=
# Use the per-endpoint required permission code. For timeline.py both endpoints
# likely require PORTFOLIO_READ.
new_src = re.sub(
    r'accessible_classroom_ids\(session,\s*current_user\)',
    'accessible_classroom_ids(session, current_user, code=Permission.PORTFOLIO_READ.value)',
    src,
)
new_src = re.sub(
    r'is_unrestricted\(current_user\)(?!\s*,)',
    'is_unrestricted(current_user, code=Permission.PORTFOLIO_READ.value)',
    new_src,
)
with open(path, 'w') as f: f.write(new_src)
print('done')
PYEOF
```

Verify no cosmetic churn:
```bash
git diff api/portfolio/timeline.py
# Expected: 2-3 line edits, no unrelated reflow
```

If endpoints in this file require *different* perms (e.g., one requires PORTFOLIO_READ, another PORTFOLIO_WRITE), manual edit needed per-endpoint — don't blindly regex.

- [ ] **Step 5: Run test — verify PASS**

```bash
pytest tests/test_portfolio_scope_permission_aware.py -v -k timeline
pytest tests/ -k portfolio --tb=no -q 2>&1 | tail -10
# Expected: new test PASS, no regression
```

- [ ] **Step 6: Commit**

```bash
git add api/portfolio/timeline.py tests/test_portfolio_scope_permission_aware.py
git commit -m "feat(api/portfolio/timeline): permission-aware scope (PORTFOLIO_READ)"
```

---

## Task 3: Migrate api/portfolio/auto_milestone.py + student_attachments.py

**Pattern is identical to Task 2.** Each file has 2 portfolio_access calls. Bundle into one commit since both are minimal.

- [ ] **Step 1: Survey** — check each file's endpoint perms (likely PORTFOLIO_WRITE for milestone updates, PORTFOLIO_READ for read; attachments may need PUBLISH)

- [ ] **Step 2: Add regression test in `tests/test_portfolio_scope_permission_aware.py`** — one test per file confirming `code=` is present

- [ ] **Step 3: Run, verify FAIL**

- [ ] **Step 4: Surgical python3 edits per file** — match each call to its endpoint's required perm

- [ ] **Step 5: Run, verify PASS + regression smoke**

```bash
pytest tests/ -k "portfolio or attachments" --tb=no -q 2>&1 | tail -5
```

- [ ] **Step 6: Commit**

```bash
git add api/portfolio/auto_milestone.py api/portfolio/student_attachments.py tests/test_portfolio_scope_permission_aware.py
git commit -m "feat(api/portfolio): permission-aware scope for auto_milestone + student_attachments"
```

---

## Task 4: Migrate api/portfolio/milestones.py + observations.py

Same pattern (5 calls each).

- [ ] Steps 1-6 mirror Task 3, scaled for ~5 calls per file
- [ ] Commit message: `feat(api/portfolio): permission-aware scope for milestones + observations`

---

## Task 5: Migrate api/portfolio/measurements.py

Larger file (6 calls, 366 lines). Standalone commit.

- [ ] Steps 1-6 mirror Task 2
- [ ] **Special check:** measurements may require multiple perms (PORTFOLIO_READ for read, PORTFOLIO_WRITE for create). Per-endpoint code= mapping is critical.
- [ ] Commit: `feat(api/portfolio/measurements): permission-aware scope (READ + WRITE per endpoint)`

---

## Task 6: Migrate api/portfolio/reports.py

Biggest file (7 calls, 785 lines). Highest review scrutiny.

- [ ] Steps 1-6 mirror Task 2
- [ ] **Special checks:**
  - Reports may aggregate across classes — does `code=PORTFOLIO_READ` correctly express the intent for aggregate views?
  - Some endpoints may require PORTFOLIO_PUBLISH (for publishing growth reports). Check `require_permission` per endpoint.
- [ ] Commit: `feat(api/portfolio/reports): permission-aware scope per endpoint perm`

---

## Task 7: Migrate api/attachments.py

Cross-cutting file (used by multiple modules, 6 calls, 324 lines).

- [ ] Steps 1-6 mirror Task 2
- [ ] **Special check:** attachments serves multiple endpoint types — verify each endpoint's `require_permission` and map `code=` accordingly. May not all be PORTFOLIO_*.
- [ ] Commit: `feat(api/attachments): permission-aware scope per endpoint perm`

---

## Task 8: Final verification

- [ ] **Step 1: Run full portfolio + attachments suite**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/ -k "portfolio or attachments" -v 2>&1 | tail -20
# Expected: 0 NEW failures vs Phase 1 baseline
```

- [ ] **Step 2: Run permission grant + permscope migration tests**

```bash
pytest tests/test_permission_grant.py tests/test_alembic_permscope01.py tests/test_alembic_permscope02.py tests/test_portfolio_scope_permission_aware.py tests/test_portfolio_access_permission_aware.py -v 2>&1 | tail -20
# Expected: all PASS
```

- [ ] **Step 3: Verify alembic single head**

```bash
alembic heads
# Expected: permscope02 (head)
```

- [ ] **Step 4: Grep verify no orphan calls**

```bash
grep -rn "accessible_classroom_ids(session, current_user)\b" api/portfolio/ api/attachments.py
# Expected: empty output (all calls now have code= argument)
```

- [ ] **Step 5: Manual smoke**

`./start.sh`, login as admin with wildcard, hit /portfolio/timeline → see all entries. Login as a non-admin user with PORTFOLIO_READ:own_class (manually craft via DB or create via Settings UI) → see only own-class entries.

- [ ] **Step 6: (no commit if clean)**

```bash
git status
# Expected: nothing to commit; working tree clean
```

---

## Spec Coverage Self-Review

| Spec section | Covered by |
|---|---|
| 涵蓋的 3 條 PORTFOLIO 權限 | Task 1 migration |
| ~40 call sites 加 code= | Tasks 2-7 |
| Migration teacher role/user backfill | Task 1 |
| Migration downgrade NUR strip PORTFOLIO_* | Task 1 (constraint) |
| Token_version bump on upgrade + downgrade | Task 1 (per Phase 1 review fix) |
| Per-endpoint code= mapping correctness | Tasks 2-7 each Step 1 survey + Step 4 manual edit |
| No cosmetic churn (black hook) | Tasks 2-7 use python3 surgical edits |
| Alembic single head invariant | Task 1 Step 6 + Task 8 Step 3 |
| 既有 30+ files NOT touched | Out of scope — these files don't need to be touched in Phase 2.1 |

## Out of scope（明確不做）

- `api/portal/students.py` `api/portal/class_hub.py` `api/portal/contact_book*.py` `api/contact_book_ws.py` (they reference PORTFOLIO_READ as gate but may not have row-level scope filter to migrate — investigate in Phase 2.2 plan)
- Other family permissions (HEALTH/MEDICATION/DISMISSAL/CLASSROOM/ATTENDANCE) — Phases 2.2-2.4
- Frontend changes — none needed
- Tests against actual integration with PermissionGrant `:all` granting — covered in Phase 1 test_portfolio_access_permission_aware.py

## Pre-merge checklist

- [ ] All 8 file migrations have integration test in `tests/test_portfolio_scope_permission_aware.py`
- [ ] Migration `permscope02` upgrade + downgrade tested with `_AlembicOpStub`
- [ ] `alembic heads` shows single head
- [ ] `git -C <worktree> log --oneline origin/main..HEAD` shows ~8 commits (1 migration + 6-7 file migrations + 1 follow-up if needed)
- [ ] Manual smoke completed
- [ ] Spec PR description references Phase 2 spec + this plan

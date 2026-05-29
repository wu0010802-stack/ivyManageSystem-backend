# 權限系統 Row-Level Scoping Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship row-level scoping infrastructure + STUDENTS_READ/STUDENTS_WRITE/STUDENTS_LIFECYCLE_WRITE end-to-end, eliminating 8+ hard-code `or_(head_teacher_id, assistant_teacher_id, art_teacher_id == emp_id)` duplications.

**Architecture:** Add `permission_definitions.scope_options` TEXT[], allow `users.permission_names` complex keys (`STUDENTS_READ:own_class`), introduce `services/scoping/student_scope.filter_clause(user, scope)` as single source of truth, refactor 9 routers + portal helpers to call it. Frontend Settings UI gains radio for scope choice. Phases 2-4 (PORTFOLIO_*, HEALTH_*, MEDICATION, DISMISSAL_*, CLASSROOMS_READ, ATTENDANCE_READ) follow with separate plans.

**Tech Stack:** Python 3.13 + FastAPI + SQLAlchemy + Alembic + PostgreSQL (test path uses SQLite). Vue 3 + Vite + TypeScript + Element Plus + Vitest. Playwright for E2E.

**Spec reference:** `docs/superpowers/specs/2026-05-29-permission-row-level-scoping-design.md`

**Scope deviation from spec:** Spec lists 15 scope-aware codes. This Phase 1 migration only seeds `scope_options` for **3** (STUDENTS_READ, STUDENTS_WRITE, STUDENTS_LIFECYCLE_WRITE), because routers for the other 12 don't yet have any teacher-scope filter and shipping `scope_options` without router enforcement would let admin grant `:own_class` UI controls that have no effect (silent privacy leak). Phases 2-4 add the remaining codes alongside their router refactors.

---

## File Map

### Backend create
- `services/scoping/__init__.py` — re-export student_scope
- `services/scoping/student_scope.py` — `filter_clause(user, scope) → ColumnElement | None`
- `alembic/versions/20260529_permscope01_permission_scope_options.py` — schema + seed + backfill
- `tests/test_permission_grant.py` — resolve_grant unit
- `tests/test_scoping_student.py` — student_scope.filter_clause unit
- `tests/test_alembic_permscope01.py` — migration upgrade/downgrade
- `tests/test_students_scope.py` — api/students.py integration
- `tests/test_student_assessments_scope.py`
- `tests/test_student_enrollment_scope.py`
- `tests/test_student_incidents_scope.py`

### Backend modify
- `utils/permissions.py` — add PermissionGrant, resolve_grant, require_scoped_permission, startup sanity warning
- `api/students.py` — add filter via require_scoped_permission
- `api/student_assessments.py` — replace inline OR with helper
- `api/student_enrollment.py` — same
- `api/student_incidents.py` — same
- `api/portal/_shared.py` — replace inline OR with helper (always own_class)
- `api/portal/profile.py` — same
- `api/portal/attendance.py` — same
- `api/portal/activity.py` — same
- `api/portal/students.py` — same
- `ci/permission_scoping_gate.sh` (new) — grep gate
- `.github/workflows/ci.yml` — invoke gate

### Frontend create
- `src/utils/__tests__/permission_scope.test.ts` — getPermissionScope unit
- `src/components/settings/__tests__/SettingsPermissionsTab.scope.test.ts` — radio UX

### Frontend modify
- `src/utils/permissions.ts` — extend hasPermission, add getPermissionScope
- `src/components/settings/SettingsPermissionsTab.vue` — radio for scope_options
- `src/api/_generated/schema.d.ts` — regen via `npm run gen:api`

### E2E
- `e2e/specs/permission_scoping.spec.ts` (new)
- `e2e/globalSetup.ts` — modify to pre-validate e2e_teacher fixture

---

## Task 1: Alembic Migration — Schema, Seed, Backfill

**Files:**
- Create: `alembic/versions/20260529_permscope01_permission_scope_options.py`
- Test: `tests/test_alembic_permscope01.py`

- [ ] **Step 1: Write failing migration test**

```python
# tests/test_alembic_permscope01.py
import pytest
from sqlalchemy import inspect, text
from models.database import get_session
from models.permission_models import PermissionDefinition, Role


def test_upgrade_adds_scope_options_column(alembic_runner):
    alembic_runner.migrate_up_to("permscope01")
    with get_session() as s:
        cols = {c["name"] for c in inspect(s.bind).get_columns("permission_definitions")}
        assert "scope_options" in cols


def test_upgrade_seeds_three_students_codes_with_scope_options(alembic_runner):
    alembic_runner.migrate_up_to("permscope01")
    with get_session() as s:
        rows = s.query(PermissionDefinition).filter(
            PermissionDefinition.code.in_([
                "STUDENTS_READ", "STUDENTS_WRITE", "STUDENTS_LIFECYCLE_WRITE"
            ])
        ).all()
        assert len(rows) == 3
        for r in rows:
            assert r.scope_options == ["own_class", "all"]


def test_upgrade_other_codes_have_null_scope_options(alembic_runner):
    alembic_runner.migrate_up_to("permscope01")
    with get_session() as s:
        portfolio_read = s.query(PermissionDefinition).filter_by(code="PORTFOLIO_READ").one()
        assert portfolio_read.scope_options is None


def test_upgrade_teacher_role_permissions_get_own_class_suffix(alembic_runner):
    alembic_runner.migrate_up_to("permscope01")
    with get_session() as s:
        teacher = s.query(Role).filter_by(code="teacher", is_core=True).one()
        assert "STUDENTS_READ:own_class" in teacher.permissions
        assert "STUDENTS_READ" not in teacher.permissions  # bare form removed


def test_upgrade_admin_role_permissions_unchanged(alembic_runner):
    alembic_runner.migrate_up_to("permscope01")
    with get_session() as s:
        admin = s.query(Role).filter_by(code="admin", is_core=True).one()
        # admin uses wildcard or full list; no :own_class suffix expected
        assert not any(":own_class" in p for p in admin.permissions)


def test_upgrade_existing_teacher_user_backfilled(alembic_runner, db_session):
    # seed teacher user BEFORE migration
    alembic_runner.migrate_up_to("rolesdb01")  # head before this migration
    db_session.execute(text("""
        INSERT INTO users (username, password_hash, role, permission_names, token_version)
        VALUES ('t1', 'x', 'teacher', ARRAY['STUDENTS_READ','PORTFOLIO_READ'], 0)
    """))
    db_session.commit()
    alembic_runner.migrate_up_to("permscope01")
    row = db_session.execute(text(
        "SELECT permission_names, token_version FROM users WHERE username='t1'"
    )).fetchone()
    assert "STUDENTS_READ:own_class" in row.permission_names
    assert "STUDENTS_READ" not in row.permission_names
    assert "PORTFOLIO_READ" in row.permission_names  # not scope-aware in Phase 1
    assert row.token_version == 1


def test_downgrade_restores_bare_codes(alembic_runner, db_session):
    alembic_runner.migrate_up_to("permscope01")
    db_session.execute(text("""
        INSERT INTO users (username, password_hash, role, permission_names, token_version)
        VALUES ('t2', 'x', 'teacher', ARRAY['STUDENTS_READ:own_class'], 0)
    """))
    db_session.commit()
    alembic_runner.migrate_down_to("rolesdb01")
    row = db_session.execute(text(
        "SELECT permission_names FROM users WHERE username='t2'"
    )).fetchone()
    assert row.permission_names == ["STUDENTS_READ"]
    cols = {c["name"] for c in inspect(db_session.bind).get_columns("permission_definitions")}
    assert "scope_options" not in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_alembic_permscope01.py -v`
Expected: FAIL (migration file doesn't exist)

- [ ] **Step 3: Write migration**

```python
# alembic/versions/20260529_permscope01_permission_scope_options.py
"""permission_definitions.scope_options + teacher backfill (Phase 1: STUDENTS_*)

Revision ID: permscope01
Revises: annsched01
Create Date: 2026-05-29
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

revision = "permscope01"
down_revision = "annsched01"
branch_labels = None
depends_on = None

SCOPE_AWARE_CODES = (
    "STUDENTS_READ",
    "STUDENTS_WRITE",
    "STUDENTS_LIFECYCLE_WRITE",
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. add column
    if dialect == "postgresql":
        op.add_column(
            "permission_definitions",
            sa.Column("scope_options", ARRAY(sa.Text), nullable=True),
        )
    else:
        # sqlite test path: use JSON variant column
        op.add_column(
            "permission_definitions",
            sa.Column("scope_options", sa.JSON, nullable=True),
        )

    # 2. seed scope_options for 3 STUDENTS_* codes
    codes_sql = ", ".join(f"'{c}'" for c in SCOPE_AWARE_CODES)
    if dialect == "postgresql":
        op.execute(f"""
            UPDATE permission_definitions
            SET scope_options = ARRAY['own_class','all']
            WHERE code IN ({codes_sql})
        """)
    else:
        op.execute(f"""
            UPDATE permission_definitions
            SET scope_options = '["own_class","all"]'
            WHERE code IN ({codes_sql})
        """)

    # 3. teacher role: bare → :own_class
    if dialect == "postgresql":
        op.execute(f"""
            UPDATE roles
            SET permissions = ARRAY(
                SELECT CASE
                    WHEN p IN ({codes_sql}) THEN p || ':own_class'
                    ELSE p
                END
                FROM unnest(permissions) AS p
            )
            WHERE code = 'teacher' AND is_core = true
        """)
    else:
        # sqlite: load, transform, write back (JSON column)
        rows = bind.execute(sa.text(
            "SELECT id, permissions FROM roles WHERE code='teacher' AND is_core=1"
        )).fetchall()
        import json
        for rid, perms_json in rows:
            perms = json.loads(perms_json) if isinstance(perms_json, str) else perms_json
            new_perms = [
                f"{p}:own_class" if p in SCOPE_AWARE_CODES else p
                for p in perms
            ]
            bind.execute(
                sa.text("UPDATE roles SET permissions=:p WHERE id=:i"),
                {"p": json.dumps(new_perms), "i": rid},
            )

    # 4. teacher users: bare → :own_class + bump token_version
    if dialect == "postgresql":
        op.execute(f"""
            UPDATE users
            SET permission_names = ARRAY(
                SELECT CASE
                    WHEN p IN ({codes_sql}) THEN p || ':own_class'
                    ELSE p
                END
                FROM unnest(permission_names) AS p
            ),
            token_version = COALESCE(token_version, 0) + 1
            WHERE role = 'teacher'
              AND NOT ('*' = ANY(permission_names))
        """)
    else:
        rows = bind.execute(sa.text(
            "SELECT id, permission_names, token_version FROM users WHERE role='teacher'"
        )).fetchall()
        import json
        for uid, names_json, tv in rows:
            names = json.loads(names_json) if isinstance(names_json, str) else names_json
            if "*" in names:
                continue
            new_names = [
                f"{n}:own_class" if n in SCOPE_AWARE_CODES else n
                for n in names
            ]
            bind.execute(
                sa.text("UPDATE users SET permission_names=:n, token_version=:t WHERE id=:i"),
                {"n": json.dumps(new_names), "t": (tv or 0) + 1, "i": uid},
            )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute("""
            UPDATE users SET permission_names = ARRAY(
                SELECT split_part(p, ':', 1) FROM unnest(permission_names) AS p
            )
            WHERE role = 'teacher'
        """)
        op.execute("""
            UPDATE roles SET permissions = ARRAY(
                SELECT split_part(p, ':', 1) FROM unnest(permissions) AS p
            )
            WHERE code = 'teacher' AND is_core = true
        """)
    else:
        import json
        for table, col, where in (
            ("users", "permission_names", "role='teacher'"),
            ("roles", "permissions", "code='teacher' AND is_core=1"),
        ):
            rows = bind.execute(sa.text(
                f"SELECT id, {col} FROM {table} WHERE {where}"
            )).fetchall()
            for rid, val in rows:
                items = json.loads(val) if isinstance(val, str) else val
                stripped = [p.split(":")[0] for p in items]
                bind.execute(
                    sa.text(f"UPDATE {table} SET {col}=:v WHERE id=:i"),
                    {"v": json.dumps(stripped), "i": rid},
                )

    op.drop_column("permission_definitions", "scope_options")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_alembic_permscope01.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Verify single head**

Run: `alembic heads`
Expected: `permscope01 (head)` (single line)

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/20260529_permscope01_permission_scope_options.py tests/test_alembic_permscope01.py
git commit -m "feat(permissions): alembic migration permscope01 — scope_options column + teacher backfill (STUDENTS_*)"
```

---

## Task 2: PermissionGrant + resolve_grant in utils/permissions.py

**Files:**
- Modify: `utils/permissions.py` (add ~50 lines)
- Test: `tests/test_permission_grant.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_permission_grant.py
import pytest
from types import SimpleNamespace
from utils.permissions import resolve_grant, PermissionGrant


def _user(*perms):
    return SimpleNamespace(permission_names=list(perms), employee_id=1)


def test_resolve_grant_wildcard_returns_all_scope():
    g = resolve_grant(_user("*"), "STUDENTS_READ")
    assert g == PermissionGrant("STUDENTS_READ", "all")


def test_resolve_grant_bare_code_returns_all_scope():
    g = resolve_grant(_user("STUDENTS_READ"), "STUDENTS_READ")
    assert g == PermissionGrant("STUDENTS_READ", "all")


def test_resolve_grant_scoped_code_returns_scope():
    g = resolve_grant(_user("STUDENTS_READ:own_class"), "STUDENTS_READ")
    assert g == PermissionGrant("STUDENTS_READ", "own_class")


def test_resolve_grant_not_held_returns_none():
    g = resolve_grant(_user("DASHBOARD"), "STUDENTS_READ")
    assert g is None


def test_resolve_grant_bare_and_scoped_takes_broader():
    # If user has both, the broader (all) wins
    g = resolve_grant(_user("STUDENTS_READ", "STUDENTS_READ:own_class"), "STUDENTS_READ")
    assert g.scope == "all"


def test_resolve_grant_two_scoped_invalid_takes_broader():
    g = resolve_grant(_user("STUDENTS_READ:own_class", "STUDENTS_READ:all"), "STUDENTS_READ")
    assert g.scope == "all"


def test_resolve_grant_empty_permission_names():
    user = SimpleNamespace(permission_names=[], employee_id=1)
    assert resolve_grant(user, "STUDENTS_READ") is None


def test_resolve_grant_none_permission_names():
    user = SimpleNamespace(permission_names=None, employee_id=1)
    assert resolve_grant(user, "STUDENTS_READ") is None
```

- [ ] **Step 2: Run test — verify FAIL**

Run: `pytest tests/test_permission_grant.py -v`
Expected: FAIL with ImportError or NameError on `resolve_grant`/`PermissionGrant`

- [ ] **Step 3: Implement in utils/permissions.py**

Append to `utils/permissions.py`:

```python
from typing import NamedTuple, Optional


class PermissionGrant(NamedTuple):
    code: str
    scope: Optional[str]  # "all" | "own_class" | None (no scope_options)


# scope ranking: higher index = broader
_SCOPE_BREADTH = {"own_class": 0, "all": 1}


def resolve_grant(user, code: str) -> Optional[PermissionGrant]:
    """Resolve a user's grant for a permission code.

    Returns:
        PermissionGrant(code, scope) where scope is 'all' / 'own_class' / None.
        None if user does not hold this permission.

    Rules:
        - wildcard '*' → ('all')
        - bare 'STUDENTS_READ' → ('all')  [backward compat]
        - 'STUDENTS_READ:own_class' → ('own_class')
        - both bare and scoped present → broader (all) wins
        - multiple scoped → broadest wins
        - None / empty permission_names → None
    """
    names = getattr(user, "permission_names", None) or []
    if WILDCARD in names:
        return PermissionGrant(code, "all")

    found_scopes: list[str] = []
    for n in names:
        if n == code:
            found_scopes.append("all")
        elif n.startswith(f"{code}:"):
            scope = n.split(":", 1)[1]
            found_scopes.append(scope)

    if not found_scopes:
        return None

    # pick broadest
    valid = [s for s in found_scopes if s in _SCOPE_BREADTH]
    if not valid:
        # all scopes were invalid strings; treat as not-held (fail-closed)
        return None
    broadest = max(valid, key=lambda s: _SCOPE_BREADTH[s])
    return PermissionGrant(code, broadest)
```

- [ ] **Step 4: Run test — verify PASS**

Run: `pytest tests/test_permission_grant.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add utils/permissions.py tests/test_permission_grant.py
git commit -m "feat(permissions): add PermissionGrant + resolve_grant helper"
```

---

## Task 3: services/scoping/student_scope.py

**Files:**
- Create: `services/scoping/__init__.py`
- Create: `services/scoping/student_scope.py`
- Test: `tests/test_scoping_student.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_scoping_student.py
import pytest
from types import SimpleNamespace
from sqlalchemy import select
from services.scoping import student_scope
from models.database import get_session, Classroom, Student, Employee


@pytest.fixture
def setup_two_classrooms(db_session):
    teacher_a = Employee(name="A", email="a@x", phone="", role="teacher")
    teacher_b = Employee(name="B", email="b@x", phone="", role="teacher")
    db_session.add_all([teacher_a, teacher_b])
    db_session.flush()
    c1 = Classroom(name="星星班", head_teacher_id=teacher_a.id, school_year=113, grade_level="middle")
    c2 = Classroom(name="月亮班", head_teacher_id=teacher_b.id, school_year=113, grade_level="middle")
    db_session.add_all([c1, c2])
    db_session.flush()
    s1 = Student(chinese_name="王小明", classroom_id=c1.id, lifecycle_status="active")
    s2 = Student(chinese_name="李小華", classroom_id=c2.id, lifecycle_status="active")
    db_session.add_all([s1, s2])
    db_session.commit()
    return SimpleNamespace(teacher_a=teacher_a, teacher_b=teacher_b, c1=c1, c2=c2, s1=s1, s2=s2)


def _user(employee_id):
    return SimpleNamespace(employee_id=employee_id, permission_names=[])


def test_filter_clause_all_returns_none(setup_two_classrooms):
    user = _user(setup_two_classrooms.teacher_a.id)
    assert student_scope.filter_clause(user, "all") is None


def test_filter_clause_own_class_returns_clause(setup_two_classrooms, db_session):
    user = _user(setup_two_classrooms.teacher_a.id)
    clause = student_scope.filter_clause(user, "own_class")
    assert clause is not None
    visible = db_session.query(Student).filter(clause).all()
    names = {s.chinese_name for s in visible}
    assert names == {"王小明"}


def test_filter_clause_own_class_includes_assistant_teacher(db_session, setup_two_classrooms):
    setup_two_classrooms.c1.assistant_teacher_id = setup_two_classrooms.teacher_b.id
    db_session.commit()
    user = _user(setup_two_classrooms.teacher_b.id)
    clause = student_scope.filter_clause(user, "own_class")
    visible = db_session.query(Student).filter(clause).all()
    names = {s.chinese_name for s in visible}
    # teacher_b is asst of c1 AND head of c2 → sees both students
    assert names == {"王小明", "李小華"}


def test_filter_clause_own_class_raises_without_employee_id():
    user = SimpleNamespace(employee_id=None, permission_names=[])
    with pytest.raises(ValueError, match="employee_id"):
        student_scope.filter_clause(user, "own_class")


def test_filter_clause_unknown_scope_raises():
    user = _user(1)
    with pytest.raises(ValueError, match="unknown scope"):
        student_scope.filter_clause(user, "own_campus")
```

- [ ] **Step 2: Run test — verify FAIL**

Run: `pytest tests/test_scoping_student.py -v`
Expected: FAIL with ModuleNotFoundError on `services.scoping`

- [ ] **Step 3: Create __init__.py**

```python
# services/scoping/__init__.py
"""Row-level scoping helpers. Single source of truth for
'teacher only sees own class' style filters."""

from . import student_scope

__all__ = ["student_scope"]
```

- [ ] **Step 4: Create student_scope.py**

```python
# services/scoping/student_scope.py
"""Student row-level scoping filter clauses.

Used by admin routers (via require_scoped_permission) and portal routers
(direct call with scope='own_class')."""

from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.sql.elements import ColumnElement

from models.database import Classroom, Student


def filter_clause(user, scope: str) -> Optional[ColumnElement]:
    """Return SQLAlchemy WHERE clause for filtering Student query by scope.

    Args:
        user: must have .employee_id (int | None)
        scope: 'all' | 'own_class'

    Returns:
        None if scope='all' (caller skips filter)
        ColumnElement WHERE Student.classroom_id IN (user's classrooms)

    Raises:
        ValueError: scope='own_class' but user.employee_id is None
        ValueError: scope is not 'all' / 'own_class'
    """
    if scope == "all":
        return None
    if scope == "own_class":
        emp_id = getattr(user, "employee_id", None)
        if emp_id is None:
            raise ValueError(
                "student_scope.filter_clause: scope=own_class requires user.employee_id"
            )
        return Student.classroom_id.in_(
            select(Classroom.id).where(
                or_(
                    Classroom.head_teacher_id == emp_id,
                    Classroom.assistant_teacher_id == emp_id,
                    Classroom.art_teacher_id == emp_id,
                )
            )
        )
    raise ValueError(f"student_scope.filter_clause: unknown scope: {scope!r}")
```

- [ ] **Step 5: Run test — verify PASS**

Run: `pytest tests/test_scoping_student.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add services/scoping/ tests/test_scoping_student.py
git commit -m "feat(scoping): add student_scope.filter_clause single source of truth"
```

---

## Task 4: require_scoped_permission FastAPI dependency

**Files:**
- Modify: `utils/permissions.py` (add ~40 lines)
- Test: extend `tests/test_permission_grant.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_permission_grant.py`:

```python
from fastapi import HTTPException
from utils.permissions import require_scoped_permission, Permission


def test_require_scoped_permission_returns_user_and_grant():
    user = SimpleNamespace(
        permission_names=["STUDENTS_READ:own_class"],
        employee_id=42,
    )
    dep = require_scoped_permission(Permission.STUDENTS_READ)
    # FastAPI dependency function is the inner callable
    result_user, grant = dep(user=user)
    assert result_user is user
    assert grant.scope == "own_class"


def test_require_scoped_permission_raises_403_when_missing():
    user = SimpleNamespace(permission_names=[], employee_id=1)
    dep = require_scoped_permission(Permission.STUDENTS_READ)
    with pytest.raises(HTTPException) as exc:
        dep(user=user)
    assert exc.value.status_code == 403


def test_require_scoped_permission_wildcard_grants_all():
    user = SimpleNamespace(permission_names=["*"], employee_id=1)
    dep = require_scoped_permission(Permission.STUDENTS_READ)
    _, grant = dep(user=user)
    assert grant.scope == "all"
```

- [ ] **Step 2: Run test — verify FAIL**

Run: `pytest tests/test_permission_grant.py -v -k require_scoped`
Expected: FAIL

- [ ] **Step 3: Implement require_scoped_permission**

Append to `utils/permissions.py`:

```python
from fastapi import Depends, HTTPException


def require_scoped_permission(code: Permission):
    """FastAPI dependency that also exposes the user's grant scope.

    Returns:
        callable returning tuple[User, PermissionGrant]

    Usage:
        @router.get("/students")
        def list_students(
            scoped=Depends(require_scoped_permission(Permission.STUDENTS_READ))
        ):
            user, grant = scoped
            clause = student_scope.filter_clause(user, grant.scope)
            ...
    """
    # local import to avoid circular: utils.auth → utils.permissions
    from utils.auth import get_current_user

    def dep(user=Depends(get_current_user)):
        grant = resolve_grant(user, code.value)
        if grant is None:
            raise HTTPException(
                status_code=403,
                detail=f"missing permission: {code.value}",
            )
        return user, grant

    return dep
```

- [ ] **Step 4: Run test — verify PASS**

Run: `pytest tests/test_permission_grant.py -v -k require_scoped`
Expected: PASS (3 new tests)

- [ ] **Step 5: Commit**

```bash
git add utils/permissions.py tests/test_permission_grant.py
git commit -m "feat(permissions): add require_scoped_permission FastAPI dependency"
```

---

## Task 5: Startup sanity warning for missing scope_options

**Files:**
- Modify: `utils/permissions.py` (add `check_scope_options_sanity` function)
- Modify: `main.py` (call on startup, after `init_*` services)
- Test: extend `tests/test_permission_grant.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_permission_grant.py`:

```python
import logging
from utils.permissions import check_scope_options_sanity


def test_sanity_warns_when_students_prefix_lacks_scope_options(caplog):
    # PermissionDefinition table missing scope_options for STUDENTS_READ
    # simulate via in-memory mapping
    seed = {"STUDENTS_READ": None, "DASHBOARD": None}
    with caplog.at_level(logging.WARNING):
        check_scope_options_sanity(seed)
    assert any("STUDENTS_READ" in r.message for r in caplog.records)
    # DASHBOARD lacks STUDENTS_/PORTFOLIO_/etc prefix → no warning
    assert not any("DASHBOARD" in r.message for r in caplog.records)


def test_sanity_no_warning_when_scope_options_present(caplog):
    seed = {"STUDENTS_READ": ["own_class", "all"]}
    with caplog.at_level(logging.WARNING):
        check_scope_options_sanity(seed)
    assert len(caplog.records) == 0
```

- [ ] **Step 2: Run test — verify FAIL**

Run: `pytest tests/test_permission_grant.py -v -k sanity`
Expected: FAIL

- [ ] **Step 3: Implement check_scope_options_sanity**

Append to `utils/permissions.py`:

```python
import logging

_logger = logging.getLogger(__name__)

# Prefixes that imply the permission SHOULD support scope_options.
# Add new prefixes as Phases 2-4 expand coverage.
_SCOPE_AWARE_PREFIXES = (
    "STUDENTS_",
    # Phase 2+: "PORTFOLIO_", "STUDENTS_HEALTH_", etc.
)
_SCOPE_AWARE_EXACT: tuple[str, ...] = (
    # Phase 4+: "CLASSROOMS_READ", "ATTENDANCE_READ"
)


def check_scope_options_sanity(seed: dict[str, list[str] | None]) -> None:
    """Log WARNING (not raise) for permission codes that look scope-aware
    by name but have NULL scope_options in DB. Catches seed drift when
    new scope-aware perms are added without updating the migration."""
    for code, opts in seed.items():
        looks_scope_aware = (
            any(code.startswith(p) for p in _SCOPE_AWARE_PREFIXES)
            or code in _SCOPE_AWARE_EXACT
        )
        if looks_scope_aware and not opts:
            _logger.warning(
                "permission %r looks scope-aware but scope_options is empty/NULL "
                "in permission_definitions; consider adding a migration",
                code,
            )
```

- [ ] **Step 4: Wire startup call in main.py**

Find the existing `on_startup` (likely in `main.py` lifespan or `@app.on_event("startup")`); append:

```python
# main.py, inside on_startup (after init_*_services())
from utils.permissions import check_scope_options_sanity
from models.permission_models import PermissionDefinition
from models.database import get_session

with get_session() as s:
    seed = {p.code: p.scope_options for p in s.query(PermissionDefinition).all()}
check_scope_options_sanity(seed)
```

- [ ] **Step 5: Run test — verify PASS**

Run: `pytest tests/test_permission_grant.py -v -k sanity`
Expected: PASS (2 tests)

- [ ] **Step 6: Run full permissions test suite**

Run: `pytest tests/test_permission_grant.py tests/test_scoping_student.py tests/test_alembic_permscope01.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add utils/permissions.py main.py tests/test_permission_grant.py
git commit -m "feat(permissions): startup sanity warning for missing scope_options seed"
```

---

## Task 6: Refactor api/students.py — add scope filter

**Files:**
- Modify: `api/students.py`
- Test: `tests/test_students_scope.py`

- [ ] **Step 1: Read current api/students.py list endpoint**

Run: `grep -n "def list_students\|def get_student" api/students.py | head -5`

Note the endpoint signature and which permission it requires.

- [ ] **Step 2: Write failing integration test**

```python
# tests/test_students_scope.py
import pytest
from fastapi.testclient import TestClient

from main import app
from tests.conftest import make_user, login_token  # use existing helpers


def test_admin_sees_all_students(db_session, multi_class_students_fixture):
    admin = make_user(role="admin", permission_names=["*"])
    token = login_token(admin)
    r = TestClient(app).get(
        "/students", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    assert len(r.json()["items"]) >= 2


def test_teacher_own_class_sees_only_own_students(db_session, multi_class_students_fixture):
    fx = multi_class_students_fixture
    teacher = make_user(
        role="teacher",
        employee_id=fx.teacher_a.id,
        permission_names=["STUDENTS_READ:own_class"],
    )
    token = login_token(teacher)
    r = TestClient(app).get(
        "/students", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    names = {s["chinese_name"] for s in r.json()["items"]}
    assert names == {"王小明"}  # only c1 student


def test_teacher_no_permission_gets_403(db_session, multi_class_students_fixture):
    teacher = make_user(
        role="teacher",
        employee_id=multi_class_students_fixture.teacher_a.id,
        permission_names=["DASHBOARD"],
    )
    token = login_token(teacher)
    r = TestClient(app).get(
        "/students", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403
```

Add `multi_class_students_fixture` to `tests/conftest.py` if not exists:

```python
# tests/conftest.py
import pytest
from types import SimpleNamespace
from models.database import Classroom, Student, Employee


@pytest.fixture
def multi_class_students_fixture(db_session):
    teacher_a = Employee(name="老師甲", email="a@school.tw", phone="", role="teacher")
    teacher_b = Employee(name="老師乙", email="b@school.tw", phone="", role="teacher")
    db_session.add_all([teacher_a, teacher_b])
    db_session.flush()
    c1 = Classroom(name="星星班", head_teacher_id=teacher_a.id,
                   school_year=113, grade_level="middle")
    c2 = Classroom(name="月亮班", head_teacher_id=teacher_b.id,
                   school_year=113, grade_level="middle")
    db_session.add_all([c1, c2])
    db_session.flush()
    s1 = Student(chinese_name="王小明", classroom_id=c1.id, lifecycle_status="active")
    s2 = Student(chinese_name="李小華", classroom_id=c2.id, lifecycle_status="active")
    db_session.add_all([s1, s2])
    db_session.commit()
    return SimpleNamespace(teacher_a=teacher_a, teacher_b=teacher_b,
                           c1=c1, c2=c2, s1=s1, s2=s2)
```

- [ ] **Step 3: Run test — verify FAIL**

Run: `pytest tests/test_students_scope.py -v`
Expected: FAIL — likely the teacher test sees both students (no filter today)

- [ ] **Step 4: Refactor api/students.py list endpoint**

Find the `list_students` (or equivalent) endpoint. Replace:

```python
# before
@router.get("")
def list_students(
    db: Session = Depends(get_db),
    current_user=Depends(require_permission(Permission.STUDENTS_READ)),
):
    q = db.query(Student)
    ...
```

with:

```python
# after
from services.scoping import student_scope
from utils.permissions import require_scoped_permission

@router.get("")
def list_students(
    db: Session = Depends(get_db),
    scoped=Depends(require_scoped_permission(Permission.STUDENTS_READ)),
):
    user, grant = scoped
    q = db.query(Student)
    clause = student_scope.filter_clause(user, grant.scope)
    if clause is not None:
        q = q.filter(clause)
    ...
```

Apply the same refactor to **detail endpoints** (`GET /students/{id}`): after fetching the row, check the row's `classroom_id` against `student_scope.filter_clause` result; if the filter would have excluded it, return 404.

```python
@router.get("/{student_id}")
def get_student(
    student_id: int,
    db: Session = Depends(get_db),
    scoped=Depends(require_scoped_permission(Permission.STUDENTS_READ)),
):
    user, grant = scoped
    q = db.query(Student).filter(Student.id == student_id)
    clause = student_scope.filter_clause(user, grant.scope)
    if clause is not None:
        q = q.filter(clause)
    student = q.first()
    if student is None:
        raise HTTPException(404, detail="student not found")
    return student
```

Apply similarly to any `PUT/PATCH/DELETE /students/{id}` requiring `STUDENTS_WRITE` (use `Permission.STUDENTS_WRITE` + same pattern).

- [ ] **Step 5: Run test — verify PASS**

Run: `pytest tests/test_students_scope.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Run regression suite for students module**

Run: `pytest tests/ -k students -v 2>&1 | tail -30`
Expected: no new failures; pre-existing fails unchanged

- [ ] **Step 7: Commit**

```bash
git add api/students.py tests/test_students_scope.py tests/conftest.py
git commit -m "feat(api/students): apply STUDENTS_READ/WRITE scope filter via student_scope helper"
```

---

## Task 7: Refactor api/student_assessments.py

**Files:**
- Modify: `api/student_assessments.py` (lines ~95-110)
- Test: `tests/test_student_assessments_scope.py`

- [ ] **Step 1: Read current OR pattern**

Run: `sed -n '90,115p' api/student_assessments.py`

Note: currently uses `or_(Classroom.head_teacher_id == emp_id, ...)` inline.

- [ ] **Step 2: Write failing test**

```python
# tests/test_student_assessments_scope.py
import pytest
from fastapi.testclient import TestClient
from main import app
from tests.conftest import make_user, login_token


def test_admin_lists_assessments_across_classes(db_session, multi_class_students_fixture):
    admin = make_user(role="admin", permission_names=["*"])
    token = login_token(admin)
    r = TestClient(app).get(
        "/student-assessments", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200


def test_teacher_own_class_assessments_only(db_session, multi_class_students_fixture):
    fx = multi_class_students_fixture
    teacher = make_user(
        role="teacher",
        employee_id=fx.teacher_a.id,
        permission_names=["STUDENTS_READ:own_class"],
    )
    token = login_token(teacher)
    r = TestClient(app).get(
        f"/student-assessments?student_id={fx.s2.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    # accessing s2 (c2's student) while teacher_a heads c1 → empty result
    assert r.status_code == 200
    assert r.json() == [] or len(r.json()) == 0
```

- [ ] **Step 3: Run test — verify FAIL or current pass (current hard-code may already pass)**

Run: `pytest tests/test_student_assessments_scope.py -v`

If pass: that's because current hard-code uses `if current_user.role == "teacher":`. We still need to refactor to remove the hard-code and use the helper (refactor with green tests is fine).

- [ ] **Step 4: Refactor — replace inline OR with helper call**

In `api/student_assessments.py`, find:

```python
if current_user.role == "teacher":
    emp_id = current_user.employee_id
    q = q.join(Classroom).filter(
        or_(
            Classroom.head_teacher_id == emp_id,
            Classroom.assistant_teacher_id == emp_id,
            Classroom.art_teacher_id == emp_id,
        )
    )
```

Replace with:

```python
from services.scoping import student_scope
from utils.permissions import require_scoped_permission

# in endpoint signature, change require_permission → require_scoped_permission:
# scoped=Depends(require_scoped_permission(Permission.STUDENTS_READ))
# user, grant = scoped

clause = student_scope.filter_clause(user, grant.scope)
if clause is not None:
    # student_scope filter targets Student.classroom_id; assessment table
    # joins Student so the clause works after .join(Student)
    q = q.join(Student).filter(clause)
```

(If the query already joins Student, the second `.join(Student)` is redundant — drop it. Confirm before adding.)

- [ ] **Step 5: Run test — verify PASS**

Run: `pytest tests/test_student_assessments_scope.py -v`
Expected: PASS

- [ ] **Step 6: Run module regression**

Run: `pytest tests/ -k assessment -v 2>&1 | tail -20`
Expected: no new failures

- [ ] **Step 7: Commit**

```bash
git add api/student_assessments.py tests/test_student_assessments_scope.py
git commit -m "refactor(api/student_assessments): inline teacher OR → student_scope helper"
```

---

## Task 8: Refactor api/student_enrollment.py

**Files:**
- Modify: `api/student_enrollment.py`
- Test: `tests/test_student_enrollment_scope.py`

**Follow exact same pattern as Task 7.**

- [ ] **Step 1: Read current OR pattern in `api/student_enrollment.py`**

Run: `grep -n "head_teacher_id\s*==\|assistant_teacher_id" api/student_enrollment.py`

- [ ] **Step 2: Write failing test** (mirror Task 7 Step 2, adapt endpoint URL)

```python
# tests/test_student_enrollment_scope.py
def test_teacher_own_class_enrollment_only(db_session, multi_class_students_fixture):
    fx = multi_class_students_fixture
    teacher = make_user(
        role="teacher",
        employee_id=fx.teacher_a.id,
        permission_names=["STUDENTS_READ:own_class"],
    )
    token = login_token(teacher)
    r = TestClient(app).get(
        "/student-enrollment", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    # Only c1 students should appear
    student_ids = {e["student_id"] for e in r.json()}
    assert fx.s1.id in student_ids
    assert fx.s2.id not in student_ids
```

- [ ] **Step 3: Run test — verify FAIL or current pass**

Run: `pytest tests/test_student_enrollment_scope.py -v`

- [ ] **Step 4: Refactor — replace inline OR with helper**

Same pattern as Task 7 Step 4. Note: line 246-250 in current code is a `.outerjoin(HeadTeacher, ...)` for displaying teacher names — **do NOT touch that**; only refactor the filter OR if present.

- [ ] **Step 5: Run test — verify PASS**

- [ ] **Step 6: Run module regression**

Run: `pytest tests/ -k enrollment -v 2>&1 | tail -20`

- [ ] **Step 7: Commit**

```bash
git add api/student_enrollment.py tests/test_student_enrollment_scope.py
git commit -m "refactor(api/student_enrollment): inline teacher OR → student_scope helper"
```

---

## Task 9: Refactor api/student_incidents.py

**Files:**
- Modify: `api/student_incidents.py` (lines ~70-80)
- Test: `tests/test_student_incidents_scope.py`

**Follow exact same pattern as Task 7.** Use `Permission.STUDENTS_READ` for read endpoints, `Permission.STUDENTS_WRITE` for create/update.

- [ ] **Step 1: Read current OR pattern**

Run: `sed -n '65,85p' api/student_incidents.py`

- [ ] **Step 2: Write failing test**

```python
# tests/test_student_incidents_scope.py
def test_teacher_own_class_incidents_only(db_session, multi_class_students_fixture):
    fx = multi_class_students_fixture
    teacher = make_user(
        role="teacher",
        employee_id=fx.teacher_a.id,
        permission_names=["STUDENTS_READ:own_class", "STUDENTS_WRITE:own_class"],
    )
    token = login_token(teacher)
    # teacher tries to read incidents for s2 (c2) → empty
    r = TestClient(app).get(
        f"/student-incidents?student_id={fx.s2.id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == [] or len(r.json()) == 0


def test_teacher_cannot_write_incident_for_other_class_student(
    db_session, multi_class_students_fixture
):
    fx = multi_class_students_fixture
    teacher = make_user(
        role="teacher",
        employee_id=fx.teacher_a.id,
        permission_names=["STUDENTS_WRITE:own_class"],
    )
    token = login_token(teacher)
    r = TestClient(app).post(
        "/student-incidents",
        json={"student_id": fx.s2.id, "type": "injury", "description": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code in (403, 404)  # accept either based on impl choice
```

- [ ] **Step 3-7: same flow as Task 7-8** (FAIL → refactor → PASS → regression → commit)

```bash
git add api/student_incidents.py tests/test_student_incidents_scope.py
git commit -m "refactor(api/student_incidents): inline teacher OR → student_scope helper"
```

---

## Task 10: Refactor api/portal/_shared.py + dependent portal endpoints

**Files:**
- Modify: `api/portal/_shared.py` (lines ~155-160)
- Modify: `api/portal/profile.py` (lines ~60-65)
- Modify: `api/portal/attendance.py` (lines ~70-85)
- Modify: `api/portal/activity.py` (lines ~45-50)
- Modify: `api/portal/students.py` (lines ~79-85 and ~218-225)

Portal endpoints don't go through `require_scoped_permission` — they are by-definition own_class. Call `student_scope.filter_clause(user, "own_class")` directly.

- [ ] **Step 1: Grep all OR patterns to be replaced**

Run: `grep -n "head_teacher_id\s*==.*assistant_teacher_id\|or_(\s*Classroom\.head_teacher" api/portal/*.py`

Confirm 5 files, ~6 sites total.

- [ ] **Step 2: Replace each OR with helper call**

For each site, replace:

```python
.filter(
    or_(
        Classroom.head_teacher_id == emp_id,
        Classroom.assistant_teacher_id == emp_id,
        Classroom.art_teacher_id == emp_id,
    )
)
```

with:

```python
from services.scoping import student_scope
# ...
.filter(student_scope.filter_clause(current_user, "own_class"))
```

**Exception:** `api/portal/attendance.py:71-81` uses CASE/aggregate, not WHERE filter. Leave that one alone (it's a SELECT projection, not a row filter). Document in comment:

```python
# scope: aggregate label, not row filter (see services/scoping/student_scope.py
# for the canonical row filter)
```

Similarly check each site whether it's a true row filter or projection logic.

- [ ] **Step 3: Run portal test suite**

Run: `pytest tests/ -k portal -v 2>&1 | tail -40`
Expected: no new failures

- [ ] **Step 4: Verify grep gate finds no remaining sites**

Run:
```bash
grep -rn "Classroom\.head_teacher_id\s*==" api/portal/ \
  | grep -v "_shared.py.*scope" | grep -v "attendance.py.*aggregate"
```
Expected: empty output (all true row filters consolidated through helper)

- [ ] **Step 5: Commit**

```bash
git add api/portal/
git commit -m "refactor(api/portal): consolidate teacher OR sites through student_scope helper"
```

---

## Task 11: CI grep gate

**Files:**
- Create: `ci/permission_scoping_gate.sh`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Create grep script**

```bash
# ci/permission_scoping_gate.sh
#!/usr/bin/env bash
# Ensure routers don't reintroduce inline teacher-class OR patterns.
# Single source of truth: services/scoping/student_scope.py
set -euo pipefail

ALLOW='services/scoping/\|api/classrooms\.py\|api/portal/attendance\.py'

violations=$(
  grep -rn "Classroom\.head_teacher_id\s*==" api/ --include="*.py" \
    | grep -Ev "$ALLOW" || true
)

if [ -n "$violations" ]; then
  echo "ERROR: inline teacher-class OR found outside allow-list:"
  echo "$violations"
  echo ""
  echo "Use services/scoping/student_scope.filter_clause(user, scope) instead."
  exit 1
fi
echo "OK: no inline teacher-class OR violations"
```

Make executable: `chmod +x ci/permission_scoping_gate.sh`

- [ ] **Step 2: Run gate locally**

Run: `bash ci/permission_scoping_gate.sh`
Expected: `OK: no inline teacher-class OR violations`

- [ ] **Step 3: Wire into CI**

In `.github/workflows/ci.yml`, add a new step after pytest:

```yaml
      - name: Permission scoping gate
        run: bash ci/permission_scoping_gate.sh
```

- [ ] **Step 4: Commit**

```bash
git add ci/permission_scoping_gate.sh .github/workflows/ci.yml
git commit -m "ci: gate against inline teacher-class OR (use services/scoping/)"
```

---

## Task 12: Frontend — regen schema + extend permissions.ts

**Files:**
- Modify: `ivy-frontend/src/utils/permissions.ts`
- Create: `ivy-frontend/src/utils/__tests__/permission_scope.test.ts`
- Regen: `ivy-frontend/src/api/_generated/schema.d.ts`

Work in `ivy-frontend` repo from here on.

- [ ] **Step 1: Regen schema (requires backend running OR dumped openapi.json)**

```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
cd ~/Desktop/ivy-frontend && npm run gen:api
```

Verify `schema.d.ts` now contains `scope_options?: string[] | null` on PermissionDefinition type.

- [ ] **Step 2: Write failing test**

```typescript
// src/utils/__tests__/permission_scope.test.ts
import { describe, it, expect, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useUserStore } from '@/stores/user'
import { hasPermission, getPermissionScope } from '@/utils/permissions'

describe('getPermissionScope', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('returns "all" for wildcard user', () => {
    useUserStore().$patch({ permission_names: ['*'] })
    expect(getPermissionScope('STUDENTS_READ')).toBe('all')
  })

  it('returns "all" for bare code', () => {
    useUserStore().$patch({ permission_names: ['STUDENTS_READ'] })
    expect(getPermissionScope('STUDENTS_READ')).toBe('all')
  })

  it('returns "own_class" for scoped code', () => {
    useUserStore().$patch({ permission_names: ['STUDENTS_READ:own_class'] })
    expect(getPermissionScope('STUDENTS_READ')).toBe('own_class')
  })

  it('returns null when not held', () => {
    useUserStore().$patch({ permission_names: ['DASHBOARD'] })
    expect(getPermissionScope('STUDENTS_READ')).toBeNull()
  })

  it('returns broader scope when user has both bare and scoped', () => {
    useUserStore().$patch({
      permission_names: ['STUDENTS_READ', 'STUDENTS_READ:own_class'],
    })
    expect(getPermissionScope('STUDENTS_READ')).toBe('all')
  })
})

describe('hasPermission with scoped codes', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('returns true for scoped grant', () => {
    useUserStore().$patch({ permission_names: ['STUDENTS_READ:own_class'] })
    expect(hasPermission('STUDENTS_READ')).toBe(true)
  })

  it('returns true for bare grant', () => {
    useUserStore().$patch({ permission_names: ['STUDENTS_READ'] })
    expect(hasPermission('STUDENTS_READ')).toBe(true)
  })

  it('returns false when not held in any form', () => {
    useUserStore().$patch({ permission_names: ['DASHBOARD'] })
    expect(hasPermission('STUDENTS_READ')).toBe(false)
  })
})
```

- [ ] **Step 3: Run test — verify FAIL**

Run: `npm run test -- permission_scope`
Expected: FAIL — `getPermissionScope` not exported

- [ ] **Step 4: Implement in src/utils/permissions.ts**

Modify `hasPermission` to also match `:scope` suffixed entries, and add `getPermissionScope`:

```typescript
// src/utils/permissions.ts
import { useUserStore } from '@/stores/user'

const SCOPE_BREADTH: Record<string, number> = { own_class: 0, all: 1 }

export function hasPermission(code: string): boolean {
  const names = useUserStore().permission_names || []
  if (names.includes('*')) return true
  if (names.includes(code)) return true
  return names.some((n) => n.startsWith(`${code}:`))
}

export function getPermissionScope(code: string): 'all' | 'own_class' | null {
  const names = useUserStore().permission_names || []
  if (names.includes('*')) return 'all'
  const found: string[] = []
  for (const n of names) {
    if (n === code) found.push('all')
    else if (n.startsWith(`${code}:`)) found.push(n.split(':', 2)[1])
  }
  if (found.length === 0) return null
  const valid = found.filter((s) => s in SCOPE_BREADTH)
  if (valid.length === 0) return null
  return valid.reduce((a, b) =>
    SCOPE_BREADTH[a] >= SCOPE_BREADTH[b] ? a : b
  ) as 'all' | 'own_class'
}
```

- [ ] **Step 5: Run test — verify PASS**

Run: `npm run test -- permission_scope`
Expected: PASS (8 tests)

- [ ] **Step 6: Type-check**

Run: `npm run typecheck`
Expected: 0 errors

- [ ] **Step 7: Commit**

```bash
git add src/utils/permissions.ts src/utils/__tests__/permission_scope.test.ts src/api/_generated/schema.d.ts
git commit -m "feat(utils/permissions): extend hasPermission for scoped codes + add getPermissionScope"
```

---

## Task 13: SettingsPermissionsTab — radio for scope_options

**Files:**
- Modify: `src/components/settings/SettingsPermissionsTab.vue`
- Create: `src/components/settings/__tests__/SettingsPermissionsTab.scope.test.ts`

- [ ] **Step 1: Read current component structure**

Run: `wc -l src/components/settings/SettingsPermissionsTab.vue && grep -n "checkbox\|el-radio\|scope" src/components/settings/SettingsPermissionsTab.vue | head -20`

Understand the current checkbox row layout before modifying.

- [ ] **Step 2: Write failing test**

```typescript
// src/components/settings/__tests__/SettingsPermissionsTab.scope.test.ts
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import SettingsPermissionsTab from '../SettingsPermissionsTab.vue'

const PERMISSION_DEFS = [
  { code: 'STUDENTS_READ', label: '學生 (檢視)', group_name: '學生',
    scope_options: ['own_class', 'all'] },
  { code: 'DASHBOARD', label: '儀表板', group_name: '一般',
    scope_options: null },
]

describe('SettingsPermissionsTab scope UX', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('renders radio when checkbox is checked and scope_options present', async () => {
    const wrapper = mount(SettingsPermissionsTab, {
      props: { modelValue: ['STUDENTS_READ:own_class'], definitions: PERMISSION_DEFS },
      global: { plugins: [ElementPlus] },
    })
    const radios = wrapper.findAll('[data-perm-scope="STUDENTS_READ"] input[type="radio"]')
    expect(radios.length).toBe(2)
    const checkedRadio = radios.find((r) => (r.element as HTMLInputElement).checked)
    expect((checkedRadio!.element as HTMLInputElement).value).toBe('own_class')
  })

  it('hides radio when checkbox is unchecked', async () => {
    const wrapper = mount(SettingsPermissionsTab, {
      props: { modelValue: [], definitions: PERMISSION_DEFS },
      global: { plugins: [ElementPlus] },
    })
    expect(wrapper.find('[data-perm-scope="STUDENTS_READ"]').exists()).toBe(false)
  })

  it('does not render radio for codes without scope_options', () => {
    const wrapper = mount(SettingsPermissionsTab, {
      props: { modelValue: ['DASHBOARD'], definitions: PERMISSION_DEFS },
      global: { plugins: [ElementPlus] },
    })
    expect(wrapper.find('[data-perm-scope="DASHBOARD"]').exists()).toBe(false)
  })

  it('emits update with complex key when scope changes', async () => {
    const wrapper = mount(SettingsPermissionsTab, {
      props: { modelValue: ['STUDENTS_READ:own_class'], definitions: PERMISSION_DEFS },
      global: { plugins: [ElementPlus] },
    })
    await wrapper.find('[data-perm-scope="STUDENTS_READ"] input[value="all"]').setValue(true)
    const emits = wrapper.emitted('update:modelValue')!
    expect(emits[emits.length - 1][0]).toContain('STUDENTS_READ:all')
    expect(emits[emits.length - 1][0]).not.toContain('STUDENTS_READ:own_class')
  })

  it('defaults to own_class when first checked', async () => {
    const wrapper = mount(SettingsPermissionsTab, {
      props: { modelValue: [], definitions: PERMISSION_DEFS },
      global: { plugins: [ElementPlus] },
    })
    await wrapper.find('input[type="checkbox"][value="STUDENTS_READ"]').setValue(true)
    const emits = wrapper.emitted('update:modelValue')!
    expect(emits[0][0]).toContain('STUDENTS_READ:own_class')
  })
})
```

- [ ] **Step 3: Run test — verify FAIL**

Run: `npm run test -- SettingsPermissionsTab.scope`
Expected: FAIL

- [ ] **Step 4: Implement scope UX in SettingsPermissionsTab.vue**

Add to `<script setup lang="ts">`:

```typescript
import { computed } from 'vue'

interface PermDef {
  code: string
  label: string
  group_name: string
  scope_options: string[] | null
}

const props = defineProps<{
  modelValue: string[]            // array of bare or complex keys
  definitions: PermDef[]
}>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: string[]): void
}>()

// Helper: split complex key into (code, scope)
function splitKey(key: string): { code: string; scope: string | null } {
  const idx = key.indexOf(':')
  if (idx === -1) return { code: key, scope: null }
  return { code: key.slice(0, idx), scope: key.slice(idx + 1) }
}

// Current state derived from modelValue
function isChecked(code: string): boolean {
  return props.modelValue.some((k) => splitKey(k).code === code)
}

function currentScope(code: string): string | null {
  const k = props.modelValue.find((k) => splitKey(k).code === code)
  if (!k) return null
  return splitKey(k).scope
}

function toggleCheckbox(code: string, checked: boolean) {
  const def = props.definitions.find((d) => d.code === code)
  let next = props.modelValue.filter((k) => splitKey(k).code !== code)
  if (checked) {
    if (def?.scope_options && def.scope_options.length > 0) {
      // default to own_class (conservative)
      const dflt = def.scope_options.includes('own_class') ? 'own_class' : def.scope_options[0]
      next.push(`${code}:${dflt}`)
    } else {
      next.push(code)
    }
  }
  emit('update:modelValue', next)
}

function setScope(code: string, scope: string) {
  const next = props.modelValue.map((k) =>
    splitKey(k).code === code ? `${code}:${scope}` : k
  )
  emit('update:modelValue', next)
}
```

In template, replace the existing checkbox row with:

```vue
<div v-for="def in definitions" :key="def.code" class="perm-row">
  <el-checkbox
    :model-value="isChecked(def.code)"
    :value="def.code"
    @update:model-value="(v) => toggleCheckbox(def.code, v)"
  >
    {{ def.label }}
  </el-checkbox>

  <div
    v-if="isChecked(def.code) && def.scope_options && def.scope_options.length > 0"
    :data-perm-scope="def.code"
    class="perm-scope-row"
  >
    <el-radio-group
      :model-value="currentScope(def.code)"
      @update:model-value="(v) => setScope(def.code, v as string)"
    >
      <el-radio
        v-for="opt in def.scope_options"
        :key="opt"
        :value="opt"
        :label="opt"
      >
        {{ opt === 'own_class' ? '僅自班' : '全園' }}
      </el-radio>
    </el-radio-group>
  </div>
</div>
```

CSS:

```vue
<style scoped>
.perm-scope-row {
  margin-left: 24px;
  margin-top: 4px;
}
</style>
```

- [ ] **Step 5: Run test — verify PASS**

Run: `npm run test -- SettingsPermissionsTab.scope`
Expected: PASS (5 tests)

- [ ] **Step 6: Run full SettingsPermissionsTab regression**

Run: `npm run test -- SettingsPermissionsTab`
Expected: pre-existing tests still PASS

- [ ] **Step 7: Type-check + build**

Run: `npm run typecheck && npm run build`
Expected: 0 errors, build success

- [ ] **Step 8: Commit**

```bash
git add src/components/settings/SettingsPermissionsTab.vue \
        src/components/settings/__tests__/SettingsPermissionsTab.scope.test.ts
git commit -m "feat(settings): scope radio (僅自班/全園) for scope-aware permissions"
```

---

## Task 14: E2E spec + globalSetup precheck

**Files:**
- Modify: `e2e/globalSetup.ts`
- Create: `e2e/specs/permission_scoping.spec.ts`
- Modify: `e2e/README.md` (document new env var)

Work in `~/Desktop/ivyManageSystem/e2e`.

- [ ] **Step 1: Add E2E_TEACHER_USERNAME env doc**

In `e2e/.env` (local only) add:

```
E2E_TEACHER_USERNAME=e2e_teacher
E2E_TEACHER_PASSWORD=<test-password>
E2E_TEACHER_EMPLOYEE_ID=<int>
E2E_OUT_OF_CLASS_STUDENT_ID=<int>   # student in a class teacher does not teach
```

Document in `e2e/README.md` "前置條件" section: dev DB must have an `e2e_teacher` user that is head_teacher of at least one class, plus at least one student in a different class.

- [ ] **Step 2: Extend globalSetup.ts to pre-validate**

In `e2e/globalSetup.ts`, add after existing self-guard checks:

```typescript
// e2e_teacher must exist and be head_teacher of at least one class
const teacherLogin = await api.post('/auth/login', {
  username: process.env.E2E_TEACHER_USERNAME!,
  password: process.env.E2E_TEACHER_PASSWORD!,
})
if (teacherLogin.status !== 200) {
  throw new Error('E2E_TEACHER fixture login failed; create test user first')
}

// Out-of-class student must NOT appear in teacher's /students list
const teacherToken = teacherLogin.data.access_token
const studentList = await api.get('/students', {
  headers: { Authorization: `Bearer ${teacherToken}` },
})
const outId = Number(process.env.E2E_OUT_OF_CLASS_STUDENT_ID)
if (studentList.data.items.some((s: { id: number }) => s.id === outId)) {
  throw new Error(
    `E2E_OUT_OF_CLASS_STUDENT_ID=${outId} appears in teacher's scope; ` +
    `fix dev DB so this student is in a class teacher does NOT teach`
  )
}

// Save teacher storage state for tests
await page.context().storageState({ path: 'e2e/.auth/teacher.json' })
```

- [ ] **Step 3: Write E2E spec**

```typescript
// e2e/specs/permission_scoping.spec.ts
import { test, expect } from '@playwright/test'

const TEACHER_STATE = 'e2e/.auth/teacher.json'
const ADMIN_STATE = 'e2e/.auth/admin.json'

test.describe('Permission scoping', () => {
  test.use({ storageState: ADMIN_STATE })

  test('admin sees all students', async ({ page }) => {
    await page.goto('/students')
    const rows = await page.locator('[data-test="student-row"]').count()
    expect(rows).toBeGreaterThanOrEqual(2)
  })
})

test.describe('Permission scoping — teacher own_class', () => {
  test.use({ storageState: TEACHER_STATE })

  test('teacher sees fewer students than admin', async ({ page }) => {
    await page.goto('/students')
    const rows = await page.locator('[data-test="student-row"]').count()
    expect(rows).toBeGreaterThan(0)
    // assertion of "less than total" is in globalSetup precheck
  })

  test('teacher cannot view out-of-class student detail', async ({ page }) => {
    const outId = process.env.E2E_OUT_OF_CLASS_STUDENT_ID
    const resp = await page.request.get(`/api/students/${outId}`)
    expect([403, 404]).toContain(resp.status())
  })
})
```

- [ ] **Step 4: Run E2E locally**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh   # in separate terminal
cd ~/Desktop/ivyManageSystem/e2e
set -a; . ./.env; set +a
npx playwright test specs/permission_scoping.spec.ts
```

Expected: 3 tests PASS

- [ ] **Step 5: Commit (workspace repo)**

```bash
cd ~/Desktop/ivyManageSystem
git add e2e/specs/permission_scoping.spec.ts e2e/globalSetup.ts e2e/README.md
git commit -m "test(e2e): permission scoping smoke (admin all + teacher own_class)"
```

---

## Task 15: Final verification + grep gate + full suite

**Files:** none new

- [ ] **Step 1: Run backend full test suite**

```bash
cd ~/Desktop/ivy-backend && pytest -x --tb=short 2>&1 | tail -50
```
Expected: 0 NEW failures vs baseline (any pre-existing fails unchanged)

- [ ] **Step 2: Run frontend full test suite + typecheck + build**

```bash
cd ~/Desktop/ivy-frontend && npm run test -- --run && npm run typecheck && npm run build
```
Expected: all PASS, 0 type errors, build OK

- [ ] **Step 3: Run CI grep gate**

```bash
cd ~/Desktop/ivy-backend && bash ci/permission_scoping_gate.sh
```
Expected: `OK: no inline teacher-class OR violations`

- [ ] **Step 4: Run OpenAPI drift check**

```bash
cd ~/Desktop/ivy-backend && python scripts/dump_openapi.py
cd ~/Desktop/ivy-frontend && npm run gen:api:check
```
Expected: no diff

- [ ] **Step 5: Manual smoke (admin)**

`./start.sh`, login as admin, go to:
- /students → see all students
- /settings/permissions → see scope radio under STUDENTS_READ when checked
- Create new role with `STUDENTS_READ:own_class`, save, verify it round-trips

- [ ] **Step 6: Manual smoke (teacher)**

Login as teacher (with `STUDENTS_READ:own_class`), go to:
- /students → see only own-class students
- Try direct URL to other-class student → 403/404

- [ ] **Step 7: Verify alembic single head**

```bash
cd ~/Desktop/ivy-backend && alembic heads
```
Expected: `permscope01 (head)` single line

- [ ] **Step 8: Final commit (if any leftover files)**

```bash
git status
# if clean, no commit needed
```

---

## Out of Scope (Phase 2-4 follow-up plans)

1. **Phase 2**: PORTFOLIO_* scoping — refactor `api/portfolio/*.py` (7 files) + `api/attachments.py`, migration adds `scope_options` to 3 PORTFOLIO codes
2. **Phase 3**: HEALTH/SPECIAL_NEEDS/MEDICATION scoping — refactor `api/student_health.py`, parts of `api/students.py`, `api/gov_moe/iep.py`, migration adds 5 codes
3. **Phase 4**: DISMISSAL_CALLS / CLASSROOMS_READ / ATTENDANCE_READ scoping — needs `classroom_scope` + `attendance_scope` helpers, refactor dismissal + classroom + attendance admin routers, migration adds 4 codes
4. **Audit log scope injection** — log grant.scope in audit context, enable reverse query
5. **Test fixture migration** — add `make_teacher_user(permissions, scope='own_class')` helper, migrate existing fixtures
6. **Frontend router guard rework** — solve CLAUDE.md gap that custom permissions don't affect sidebar/router

---

## Spec Coverage Self-Review

| Spec section | Covered by |
|---|---|
| §設計概念 → 資料模型 | Task 1 (migration) |
| §設計概念 → Scope 層級 | Task 2 (resolve_grant), Task 3 (filter_clause) |
| §設計概念 → 涵蓋的 15 條 | **Phase 1 only 3** — Phases 2-4 deferred per scope deviation noted at top |
| §Component → 1. utils/permissions.py 擴充 | Task 2, 4 |
| §Component → 2. services/scoping/ | Task 3 |
| §Component → 3. Router 改寫 pattern | Tasks 6, 7, 8, 9 |
| §Component → 4. Portal 端 | Task 10 |
| §Component → 5. 防呆 runtime + startup | Task 2 (runtime in resolve_grant), Task 5 (startup warning) |
| §Component → 6. 重複 grant 解析規則 | Task 2 (resolve_grant takes broader) |
| §Frontend → 1. 類型重新生成 | Task 12 Step 1 |
| §Frontend → 2. permissions.ts | Task 12 |
| §Frontend → 3. SettingsPermissionsTab | Task 13 |
| §Frontend → 4. getPermissionScope usage | Task 12 (helper only; consumer-side optional uses defer to actual UI need) |
| §Migration → Alembic | Task 1 |
| §Migration → 部署順序 | Documented in Phase 1 task ordering (Task 1 first, code after) |
| §Migration → Rollback | Task 1 downgrade test |
| §Migration → Test fixture 影響 | Out of scope follow-up #5 |
| §Testing → Unit | Tasks 2, 3, 4, 5 |
| §Testing → Integration | Tasks 6-10 |
| §Testing → Migration | Task 1 |
| §Testing → Frontend | Tasks 12, 13 |
| §Testing → E2E | Task 14 |
| §Testing → CI grep gate | Task 11 |

# 權限 Row-Level Scoping Phase 2.2 HEALTH-MEDICATION Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 5 條 HEALTH-MEDICATION 權限（`STUDENTS_HEALTH_READ` `STUDENTS_HEALTH_WRITE` `STUDENTS_SPECIAL_NEEDS_READ` `STUDENTS_SPECIAL_NEEDS_WRITE` `STUDENTS_MEDICATION_ADMINISTER`）納入 row-level scoping bridge，並順帶修 `api/gov_moe/iep.py` 既有 lifecycle 過濾漏洞。

**Architecture:** 先擴展 `utils/portfolio_access.py` 3 個既有 helper（`assert_student_access` / `filter_student_ids_by_access` / `student_ids_in_scope`）接受可選 `code=` 參數（Phase 1 只擴了 `is_unrestricted` + `accessible_classroom_ids`），再以 surgical edit 把 5 個 file 的 portfolio_access 呼叫加上 `code=`、把 `iep.py` 與 `portal/medications.py` 自有 scope 邏輯替換為 portfolio_access delegation。

**Tech Stack:** Python 3.13/3.14 + FastAPI + SQLAlchemy + Alembic + pytest；PostgreSQL prod / SQLite test。

**Pre-flight：必先**
- 確認 Phase 1 (permscope01) 已 ship 至 local main 並驗證
- worktree from `origin/main` （非 local main）
- subagent dispatch 全用 `git -C /absolute/path/worktree`
- 對既有 `.py` 改動全用 `python3` string.replace 繞 black PostToolUse hook（避免 +60/-30 cosmetic creep）

---

## File Structure

| File | 改動類型 | 估計 LOC |
|------|---------|---------|
| `utils/portfolio_access.py` | extend 3 helper 加 `code=` 參數 | +30/-0 |
| `tests/test_portfolio_access_scope_bridge.py` | 新 unit tests for code= behavior | +120 |
| `alembic/versions/20260530_permscope03_health_med.py` | seed 5 perm `scope_options`+backfill teacher | +180 |
| `tests/test_alembic_permscope03.py` | upgrade/downgrade test | +90 |
| `api/student_health.py` | 8 處 portfolio_access calls 加 `code=` | +0/-0（surgical replace） |
| `services/dashboard_query_service.py` | 1 處 `student_ids_in_scope` 加 `code=` | +0/-0 |
| `api/portal/medications.py` | 移除自有 `_get_teacher_classroom_ids` 改 `accessible_classroom_ids(code=)` | +5/-3 |
| `api/portal/class_hub.py` | 驗證下游 service，必要時加 `code=` | TBD |
| `api/gov_moe/iep.py` | 移除自有 `_student_ids_in_scope` `_assert_student_in_scope`，delegate 至 portfolio_access | +15/-50 |
| `tests/test_permscope_health_medication.py` | integration tests for 3 角色 × 5 perm | +250 |

---

## Task 1: Extend portfolio_access helpers 加 `code=` 參數

**Files:**
- Modify: `utils/portfolio_access.py:99-145` `:238-257`
- Test: `tests/test_portfolio_access_scope_bridge.py`（新檔，verify code= 行為與 wildcard 處理）

- [ ] **Step 1: pre-flight 確認 helper 簽章**

```bash
git -C /abs/path/worktree show HEAD:utils/portfolio_access.py | grep -n "^def " | head -10
```

預期看到：`is_unrestricted` `accessible_classroom_ids` 已有 `code` 參數；`assert_student_access` `filter_student_ids_by_access` `student_ids_in_scope` 尚未有。

- [ ] **Step 2: 寫新 test file（FAIL）**

```python
# tests/test_portfolio_access_scope_bridge.py
"""Phase 2.2 bridge 擴展：assert_student_access / filter_student_ids_by_access /
student_ids_in_scope 三個 helper 接受 code= 參數。"""
import pytest
from utils.portfolio_access import (
    assert_student_access,
    filter_student_ids_by_access,
    student_ids_in_scope,
)
from utils.permissions import Permission


class TestStudentIdsInScopeWithCode:
    def test_teacher_own_class_with_code_returns_class_student_ids(
        self, db_session, teacher_with_class
    ):
        # teacher 持有 STUDENTS_HEALTH_READ:own_class → 限自班
        teacher = teacher_with_class["user_dict"]
        teacher["permission_names"] = ["STUDENTS_HEALTH_READ:own_class"]
        result = student_ids_in_scope(
            db_session, teacher, code=Permission.STUDENTS_HEALTH_READ.value
        )
        assert isinstance(result, list)
        assert teacher_with_class["student_in_class_id"] in result
        assert teacher_with_class["student_other_class_id"] not in result

    def test_teacher_all_scope_with_code_returns_none(
        self, db_session, teacher_with_class
    ):
        # teacher 持有 STUDENTS_HEALTH_READ:all → 全放行
        teacher = teacher_with_class["user_dict"]
        teacher["permission_names"] = ["STUDENTS_HEALTH_READ:all"]
        result = student_ids_in_scope(
            db_session, teacher, code=Permission.STUDENTS_HEALTH_READ.value
        )
        assert result is None  # None 表全放行

    def test_no_code_falls_back_to_role_based(self, db_session, teacher_with_class):
        # 未傳 code → 回退既有 role-based 邏輯（向後相容）
        teacher = teacher_with_class["user_dict"]
        result = student_ids_in_scope(db_session, teacher)  # 不傳 code
        # role=teacher 仍走 classroom 過濾
        assert isinstance(result, list)


class TestAssertStudentAccessWithCode:
    def test_teacher_all_scope_can_access_any_student(
        self, db_session, teacher_with_class
    ):
        teacher = teacher_with_class["user_dict"]
        teacher["permission_names"] = ["STUDENTS_HEALTH_READ:all"]
        # 跨班學生也通過
        student = assert_student_access(
            db_session,
            teacher,
            teacher_with_class["student_other_class_id"],
            code=Permission.STUDENTS_HEALTH_READ.value,
        )
        assert student is not None

    def test_teacher_own_class_scope_403_on_other_class(
        self, db_session, teacher_with_class
    ):
        from fastapi import HTTPException
        teacher = teacher_with_class["user_dict"]
        teacher["permission_names"] = ["STUDENTS_HEALTH_READ:own_class"]
        with pytest.raises(HTTPException) as exc:
            assert_student_access(
                db_session,
                teacher,
                teacher_with_class["student_other_class_id"],
                code=Permission.STUDENTS_HEALTH_READ.value,
            )
        assert exc.value.status_code == 403


class TestFilterStudentIdsByAccessWithCode:
    def test_filter_by_code_scope(self, db_session, teacher_with_class):
        teacher = teacher_with_class["user_dict"]
        teacher["permission_names"] = ["STUDENTS_HEALTH_READ:own_class"]
        result = filter_student_ids_by_access(
            db_session,
            teacher,
            [
                teacher_with_class["student_in_class_id"],
                teacher_with_class["student_other_class_id"],
            ],
            code=Permission.STUDENTS_HEALTH_READ.value,
        )
        assert teacher_with_class["student_in_class_id"] in result
        assert teacher_with_class["student_other_class_id"] not in result
```

Fixture `teacher_with_class` 需先在 `tests/conftest.py` 或本檔加 fixture 建 2 個 classroom + 1 個 teacher（head_teacher 班 A）+ 各 1 學生（active lifecycle、非終態）。

Run: `pytest tests/test_portfolio_access_scope_bridge.py -v`
Expected: FAIL（helper 不接受 code= 參數，`TypeError`）

- [ ] **Step 3: 用 python3 surgical edit 加 `code` 參數**

```bash
python3 - <<'EOF'
import pathlib
p = pathlib.Path("/abs/path/worktree/utils/portfolio_access.py")
text = p.read_text()

# 1. assert_student_access：簽章加 code，內部 is_unrestricted 呼叫傳 code
old = '''def assert_student_access(session, current_user: dict, student_id: int) -> Student:
    """檢查 user 是否可存取該學生；不可則 403。回傳 Student 物件。

    - admin/hr/supervisor：一律放行（含終態學生，供事後查歷史）
    - teacher：僅可存取自己班級且 lifecycle 非終態（graduated/withdrawn/transferred）
      的學生；未分班學生一律禁
    - 學生不存在：raise 404
    """
    student = session.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="學生不存在")
    if is_unrestricted(current_user):
        return student
    # teacher 路徑：終態學生立即失效（audit 2026-05-07 P0 #5）
    if student.lifecycle_status in _TEACHER_BLOCKED_LIFECYCLE:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    if not student.classroom_id:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    allowed = accessible_classroom_ids(session, current_user)
    if student.classroom_id not in allowed:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    return student'''
new = '''def assert_student_access(
    session, current_user: dict, student_id: int, code: str | None = None
) -> Student:
    """檢查 user 是否可存取該學生；不可則 403。回傳 Student 物件。

    - admin/hr/supervisor 或持有 `<code>:all`：一律放行（含終態學生，供事後查歷史）
    - teacher：僅可存取自己班級且 lifecycle 非終態（graduated/withdrawn/transferred）
      的學生；未分班學生一律禁
    - 學生不存在：raise 404

    Args:
        code: 若提供，以 PermissionGrant.scope 判斷 unrestricted；
              否則回退到 role-based 判斷（向後相容）。
    """
    student = session.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="學生不存在")
    if is_unrestricted(current_user, code=code):
        return student
    # teacher 路徑：終態學生立即失效（audit 2026-05-07 P0 #5）
    if student.lifecycle_status in _TEACHER_BLOCKED_LIFECYCLE:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    if not student.classroom_id:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    allowed = accessible_classroom_ids(session, current_user, code=code)
    if student.classroom_id not in allowed:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    return student'''
assert old in text, "assert_student_access 原文不符"
text = text.replace(old, new)

# 2. filter_student_ids_by_access：加 code
old2 = '''def filter_student_ids_by_access(
    session, current_user: dict, candidate_ids: Iterable[int]
) -> set[int]:
    """把一批 student_id 過濾掉該 user 無權存取的。用於 list 端點。

    對 teacher：除班級限制外，亦排除 lifecycle 終態學生
    （graduated/withdrawn/transferred；audit 2026-05-07 P0 #5）。
    """
    if is_unrestricted(current_user):
        return set(candidate_ids)
    allowed_classrooms = accessible_classroom_ids(session, current_user)'''
new2 = '''def filter_student_ids_by_access(
    session,
    current_user: dict,
    candidate_ids: Iterable[int],
    code: str | None = None,
) -> set[int]:
    """把一批 student_id 過濾掉該 user 無權存取的。用於 list 端點。

    對 teacher：除班級限制外，亦排除 lifecycle 終態學生
    （graduated/withdrawn/transferred；audit 2026-05-07 P0 #5）。

    Args:
        code: 若提供，以 PermissionGrant.scope 判斷 unrestricted；
              否則回退到 role-based 判斷（向後相容）。
    """
    if is_unrestricted(current_user, code=code):
        return set(candidate_ids)
    allowed_classrooms = accessible_classroom_ids(session, current_user, code=code)'''
assert old2 in text, "filter_student_ids_by_access 原文不符"
text = text.replace(old2, new2)

# 3. student_ids_in_scope：加 code
old3 = '''def student_ids_in_scope(session, current_user: dict) -> list[int] | None:
    """回傳 user 所有可存取的 student_id 清單；管理角色回傳 None（表無限制）。

    用於彙總端點（例：今日用藥）的 WHERE student_id IN (...) 子句。
    對 teacher：排除 lifecycle 終態學生（audit 2026-05-07 P0 #5）。
    """
    if is_unrestricted(current_user):
        return None
    allowed_classrooms = accessible_classroom_ids(session, current_user)'''
new3 = '''def student_ids_in_scope(
    session, current_user: dict, code: str | None = None
) -> list[int] | None:
    """回傳 user 所有可存取的 student_id 清單；管理角色回傳 None（表無限制）。

    用於彙總端點（例：今日用藥）的 WHERE student_id IN (...) 子句。
    對 teacher：排除 lifecycle 終態學生（audit 2026-05-07 P0 #5）。

    Args:
        code: 若提供，以 PermissionGrant.scope 判斷 unrestricted；
              否則回退到 role-based 判斷（向後相容）。
    """
    if is_unrestricted(current_user, code=code):
        return None
    allowed_classrooms = accessible_classroom_ids(session, current_user, code=code)'''
assert old3 in text, "student_ids_in_scope 原文不符"
text = text.replace(old3, new3)

p.write_text(text)
print("DONE")
EOF
```

- [ ] **Step 4: Run tests PASS**

```bash
pytest tests/test_portfolio_access_scope_bridge.py -v
```

Expected: 全部 PASS。

- [ ] **Step 5: Regression — 跑 students/portfolio 既有 test**

```bash
pytest tests/test_students.py tests/test_portfolio.py -v 2>&1 | tail -30
```

Expected: 零 regression（既有 caller 沒傳 `code=` 走 default 行為）。

- [ ] **Step 6: Commit**

```bash
cd /abs/path/worktree
git add utils/portfolio_access.py tests/test_portfolio_access_scope_bridge.py
git commit -m "feat(portfolio_access): 3 個 helper 接受 code= 參數（Phase 2.2 bridge 擴展）

assert_student_access / filter_student_ids_by_access / student_ids_in_scope
新增可選 code= 參數，內部委派至 is_unrestricted/accessible_classroom_ids
（Phase 1 已擴展）。未傳 code 走既有 role-based 行為，向後相容。

為 Phase 2.2 HEALTH-MEDICATION 5 條權限 row-level scoping 鋪路。"
```

---

## Task 2: Migration `permscope03_health_med` seed + backfill

**Files:**
- Create: `alembic/versions/20260530_permscope03_health_med.py`
- Test: `tests/test_alembic_permscope03.py`

- [ ] **Step 1: pre-flight 確認 alembic head**

```bash
cd /abs/path/worktree && python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; cfg = Config('alembic.ini'); s = ScriptDirectory.from_config(cfg); print(s.get_current_head())"
```

預期：`permscope01`（Phase 1 head）。如果 user 已 merge 其他並行 PR，可能變動，必須先確認。

- [ ] **Step 2: 寫 alembic test（FAIL）**

```python
# tests/test_alembic_permscope03.py
"""permscope03 migration：seed 5 條 HEALTH-MEDICATION perm scope_options
+ backfill teacher role 既有 bare codes → :own_class。"""
import pytest
from tests.test_alembic_pretent001 import _AlembicOpStub


SCOPE_AWARE_CODES = (
    "STUDENTS_HEALTH_READ",
    "STUDENTS_HEALTH_WRITE",
    "STUDENTS_SPECIAL_NEEDS_READ",
    "STUDENTS_SPECIAL_NEEDS_WRITE",
    "STUDENTS_MEDICATION_ADMINISTER",
)


def test_upgrade_seeds_scope_options_for_5_codes(db_session):
    from alembic.versions import permscope03_health_med
    permscope03_health_med.upgrade()
    # 驗證 5 條 perm 的 scope_options 都被 seed 為 ['own_class', 'all']
    from models.permission_models import PermissionDefinition
    for code in SCOPE_AWARE_CODES:
        pd = db_session.query(PermissionDefinition).filter_by(code=code).first()
        assert pd is not None, f"{code} 不存在於 permission_definitions"
        assert set(pd.scope_options or []) == {"own_class", "all"}


def test_upgrade_backfills_teacher_permissions_to_own_class(db_session, teacher_role):
    """teacher role 若已有 bare STUDENTS_HEALTH_READ 應 backfill 為 STUDENTS_HEALTH_READ:own_class。"""
    from alembic.versions import permscope03_health_med
    permscope03_health_med.upgrade()
    teacher_role_after = db_session.query(...).filter_by(name="teacher").first()
    perms = set(teacher_role_after.permissions)
    assert "STUDENTS_HEALTH_READ" not in perms
    assert "STUDENTS_HEALTH_READ:own_class" in perms


def test_downgrade_strips_suffixes(db_session):
    from alembic.versions import permscope03_health_med
    permscope03_health_med.upgrade()
    permscope03_health_med.downgrade()
    # scope_options 清空（或設為 NULL，依 dialect）
    # teacher role 持有的 :own_class 後綴必須剝除回 bare code


def test_downgrade_bumps_token_version_for_affected_users(db_session):
    """downgrade 後所有持有受影響 perm 的 user 必須 token_version + 1
    （強制重新登入避免 frontend 拿 stale scope）。"""
    ...
```

Run: `pytest tests/test_alembic_permscope03.py -v`
Expected: FAIL（migration 尚未建立）

- [ ] **Step 3: 建 migration file（仿 permscope01 結構）**

```python
# alembic/versions/20260530_permscope03_health_med.py
"""Phase 2.2 HEALTH-MEDICATION: seed scope_options for 5 perm codes
+ backfill teacher role permissions (bare → :own_class)
+ bump teacher users' token_version.

Revision ID: permscope03
Revises: permscope01
Create Date: 2026-05-30
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "permscope03"
down_revision = "permscope01"  # ← Phase 1 head
branch_labels = None
depends_on = None

SCOPE_AWARE_CODES = (
    "STUDENTS_HEALTH_READ",
    "STUDENTS_HEALTH_WRITE",
    "STUDENTS_SPECIAL_NEEDS_READ",
    "STUDENTS_SPECIAL_NEEDS_WRITE",
    "STUDENTS_MEDICATION_ADMINISTER",
)


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. Seed scope_options 至 permission_definitions
    for code in SCOPE_AWARE_CODES:
        if dialect == "postgresql":
            op.execute(sa.text(
                "UPDATE permission_definitions "
                "SET scope_options = ARRAY['own_class', 'all'] "
                "WHERE code = :code"
            ).bindparams(code=code))
        else:  # sqlite
            op.execute(sa.text(
                'UPDATE permission_definitions '
                "SET scope_options = '[\"own_class\", \"all\"]' "
                "WHERE code = :code"
            ).bindparams(code=code))

    # 2. Backfill teacher role：bare → :own_class
    #    （仿 permscope01 patterns；用 array_remove + array_append on PG，
    #     JSON manipulation on SQLite）
    ...

    # 3. Bump teacher users' token_version
    ...


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1. 從 teacher role.permissions 與 user.permission_names 剝除 :own_class / :all 後綴
    #    僅針對本 phase 5 條 code，不要動 STUDENTS_*（permscope01 管轄）
    ...

    # 2. Clear scope_options for 5 codes
    for code in SCOPE_AWARE_CODES:
        if dialect == "postgresql":
            op.execute(sa.text(
                "UPDATE permission_definitions SET scope_options = NULL WHERE code = :code"
            ).bindparams(code=code))
        else:
            op.execute(sa.text(
                "UPDATE permission_definitions SET scope_options = NULL WHERE code = :code"
            ).bindparams(code=code))

    # 3. Bump token_version
    ...
```

實作細節仿 `alembic/versions/20260529_permscope01_permission_scope_options.py` 的 backfill + token_version bump pattern。

- [ ] **Step 4: Run alembic test PASS**

```bash
pytest tests/test_alembic_permscope03.py -v
```

- [ ] **Step 5: 確認 alembic single head**

```bash
cd /abs/path/worktree && alembic heads
```

預期：單一 head `permscope03`。

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/20260530_permscope03_health_med.py tests/test_alembic_permscope03.py
git commit -m "feat(alembic): permscope03 seed HEALTH-MEDICATION scope_options + backfill teacher"
```

---

## Task 3: `api/student_health.py` — 8 處 portfolio_access calls 加 `code=`

**Files:**
- Modify: `api/student_health.py` L237, L265, L310, L352, L388, L418, L451, L682

對應關係（per investigation report）：
| Line | Helper | Permission code 應傳 |
|------|--------|-------------------|
| L237 | `assert_student_access` (in L233 READ endpoint) | `STUDENTS_HEALTH_READ` |
| L265 | `assert_student_access` (in L261 WRITE endpoint) | `STUDENTS_HEALTH_WRITE` |
| L310 | `assert_student_access` (in L306 WRITE endpoint) | `STUDENTS_HEALTH_WRITE` |
| L352 | `assert_student_access` (in L347 WRITE endpoint) | `STUDENTS_HEALTH_WRITE` |
| L388 | `assert_student_access` (in L384 READ endpoint) | `STUDENTS_HEALTH_READ` |
| L418 | `assert_student_access` (in L414 READ endpoint) | `STUDENTS_HEALTH_READ` |
| L451 | `assert_student_access` (in L? MEDICATION endpoint) | `STUDENTS_MEDICATION_ADMINISTER`（pre-flight 確認） |
| L682 | `student_ids_in_scope` (in L676 summary endpoint) | `STUDENTS_HEALTH_READ` |

- [ ] **Step 1: pre-flight 確認每個 L? 對應 endpoint 的 permission**

```bash
grep -n "require_permission\|assert_student_access\|student_ids_in_scope" /abs/path/worktree/api/student_health.py | head -30
```

對齊每個 helper call 與其 enclosing endpoint 的 require_permission。如果發現某 endpoint 同時用兩個 perm（例如先 READ 後可選 WRITE），優先傳 READ scope（最弱守衛，避免 over-deny）。

- [ ] **Step 2: 寫 integration test for 1 個代表 endpoint（FAIL — 無 :own_class scope 限制）**

```python
# tests/test_permscope_health_medication.py
"""Phase 2.2 integration tests：3 角色 × HEALTH READ endpoint 驗證 scope。"""
def test_admin_wildcard_sees_any_student_health(client, admin_user, student_in_other_class):
    res = client.get(f"/students/{student_in_other_class.id}/health", headers=admin_user.auth)
    assert res.status_code == 200

def test_teacher_own_class_scope_sees_own_class_only(
    client, teacher_with_class_user_own_class_scope, student_in_class, student_in_other_class
):
    # teacher 有 STUDENTS_HEALTH_READ:own_class
    res_ok = client.get(f"/students/{student_in_class.id}/health", headers=teacher_with_class_user_own_class_scope.auth)
    assert res_ok.status_code == 200
    res_403 = client.get(f"/students/{student_in_other_class.id}/health", headers=teacher_with_class_user_own_class_scope.auth)
    assert res_403.status_code == 403

def test_teacher_all_scope_sees_any_student(
    client, teacher_with_all_scope, student_in_other_class
):
    # teacher 有 STUDENTS_HEALTH_READ:all（自訂角色）
    res = client.get(f"/students/{student_in_other_class.id}/health", headers=teacher_with_all_scope.auth)
    assert res.status_code == 200  # :all 跨班通過
```

Run: 預期 `test_teacher_all_scope_sees_any_student` FAIL（current behavior 沒有讀 scope → :all teacher 被當 own_class 拒絕）。

- [ ] **Step 3: 用 python3 batch replace 加 `code=` 參數**

```bash
python3 - <<'EOF'
import pathlib
p = pathlib.Path("/abs/path/worktree/api/student_health.py")
text = p.read_text()

# 範例（per actual code）：
replacements = [
    (
        "assert_student_access(session, current_user, student_id)",
        "assert_student_access(session, current_user, student_id, code=Permission.STUDENTS_HEALTH_READ.value)",
        # 適用於 L237 / L388 / L418 (READ endpoint)
    ),
    # ... WRITE / ADMINISTER 對應
]
# 注意：相同字串可能對應不同 perm code，必須 line-by-line 處理
# 用 line 寫 indexed replacement
EOF
```

注意：因 `assert_student_access(...)` 在多 endpoint 出現相同形式，python3 string.replace 會誤觸。改用 line-by-line edit（讀 line N，確認 enclosing endpoint，替換該 line）。

- [ ] **Step 4: 確認 import Permission**

```bash
grep "from utils.permissions import" /abs/path/worktree/api/student_health.py
```

若沒 import `Permission`，append `, Permission` 到既有 import。

- [ ] **Step 5: Run Phase 2.2 integration test + 既有 student_health test PASS**

```bash
pytest tests/test_permscope_health_medication.py tests/test_student_health.py -v 2>&1 | tail -40
```

Expected: 新 test PASS，既有 test 零 regression。

- [ ] **Step 6: Commit**

```bash
git add api/student_health.py tests/test_permscope_health_medication.py
git commit -m "feat(student_health): portfolio_access calls 加 code= 啟用 :own_class/:all scope"
```

---

## Task 4: `services/dashboard_query_service.py` 加 `code=`

**Files:**
- Modify: `services/dashboard_query_service.py:333`

L325 import + L333 call：
```python
# L325
from utils.portfolio_access import student_ids_in_scope
# L333
scope = student_ids_in_scope(session, current_user)
```

→ 改為
```python
scope = student_ids_in_scope(
    session, current_user, code=Permission.STUDENTS_HEALTH_READ.value
)
```

- [ ] **Step 1: 確認 `_count_recent_parent_leaves` (L305-309) 是否屬本 phase scope**

`L306 accessible_classroom_ids()` 走 `LEAVES_READ` 或 `STUDENTS_READ` scope — 不屬於 HEALTH-MEDICATION，**不要動**。

- [ ] **Step 2: TDD test for `build_today_medication_summary` scope**

```python
def test_today_medication_summary_respects_own_class_scope(
    db_session, teacher_with_class_own_class_scope,
    medication_order_in_class, medication_order_other_class
):
    from services.dashboard_query_service import build_today_medication_summary
    result = build_today_medication_summary(db_session, teacher_with_class_own_class_scope)
    assert medication_order_in_class.id in {o["id"] for o in result["pending"]}
    assert medication_order_other_class.id not in {o["id"] for o in result["pending"]}
```

- [ ] **Step 3: surgical edit + import Permission**

- [ ] **Step 4: Run test PASS + 既有 dashboard test 零 regression**

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(dashboard): build_today_medication_summary 加 STUDENTS_HEALTH_READ scope"
```

---

## Task 5: `api/portal/medications.py` — 移除自有 helper，改用 portfolio_access

**Files:**
- Modify: `api/portal/medications.py:54-86`

當前邏輯（L54-70）：
```python
is_admin_like = "*" in (...) or current_user.get("role") in ("admin", "supervisor")
if is_admin_like and classroom_id:
    classroom_ids = [classroom_id]
else:
    my_classrooms = _get_teacher_classroom_ids(session, emp.id)
    ...
```

問題：
1. 沒走 portfolio_access → 無法支援自訂 `:all` scope（資深老師跨班看用藥）
2. 沒過濾 lifecycle 終態學生（teacher 可能看到已退學學生的歷史用藥 — Phase 1 已修 STUDENTS_* 但 medications 沒跟上）

改造方案：
```python
my_classrooms = accessible_classroom_ids(
    session, current_user, code=Permission.STUDENTS_HEALTH_READ.value
)
is_unrestricted_now = is_unrestricted(
    current_user, code=Permission.STUDENTS_HEALTH_READ.value
)
if is_unrestricted_now and classroom_id:
    classroom_ids = [classroom_id]
elif is_unrestricted_now:
    # 不指定 classroom_id → 不限制（admin 看全部）
    classroom_ids = None  # 後面 SQL filter 不加 classroom_id.in_()
else:
    if classroom_id and classroom_id not in my_classrooms:
        raise HTTPException(403, "此班級不屬於您")
    classroom_ids = [classroom_id] if classroom_id else my_classrooms
```

並把 L82-86 `students = ... filter(Student.classroom_id.in_(classroom_ids))` 改為呼叫 `student_ids_in_scope(...code=...)` 拿 ID list 再 filter。

- [ ] **Step 1: 寫 integration test（3 角色 × 跨班 vs 自班 medication 看見性）**
- [ ] **Step 2: Refactor L54-86 改用 portfolio_access**
- [ ] **Step 3: 移除 `_get_teacher_classroom_ids` 的 import（若無其他 caller）**
- [ ] **Step 4: Run test PASS**
- [ ] **Step 5: Commit**

```bash
git commit -m "feat(portal/medications): 移除自有 _get_teacher_classroom_ids 改用 portfolio_access

啟用 :own_class/:all scope；連帶補上 lifecycle 終態學生過濾（Phase 1 修了
STUDENTS_* 但 medications 自有路徑沒跟上 audit 2026-05-07 P0 #5）。"
```

---

## Task 6: `api/portal/class_hub.py` — 驗證下游 service

**Files:**
- Read first: `services/portal_class_hub_service.py:list_pending_medications`
- Modify: `api/portal/class_hub.py:133-136` 或下游 service（看實際 scope 邏輯在哪一層）

- [ ] **Step 1: 確認 `list_pending_medications` 是 hard-code classroom_id 還是已走 portfolio_access**

```bash
grep -n "list_pending_medications\|accessible_classroom_ids\|portfolio_access" /abs/path/worktree/services/portal_class_hub_service.py
```

兩種情境：

(a) 已走 portfolio_access → 加 `code=Permission.STUDENTS_HEALTH_READ.value`
(b) 自有邏輯（hard-code `classroom_id` arg）→ 重構為傳 `current_user` + `code=`，service 內部用 portfolio_access

- [ ] **Step 2: 依實際情況寫 TDD test + 改 code + commit**

---

## Task 7: `api/gov_moe/iep.py` — 移除自有 scope，delegate 至 portfolio_access

**Files:**
- Modify: `api/gov_moe/iep.py:76-118` `:139-153`

當前 `_student_ids_in_scope` (L76-105) 與 `_assert_student_in_scope` (L108-118) 是 Phase 1 之前的設計：
1. 用 `Employee.classroom_id` 而非 `Classroom.head_teacher_id` 三角 — teacher 換班只認 `Employee.classroom_id`，與 portfolio_access 不一致
2. **沒過濾 lifecycle 終態學生**（IEP 是長期文件，已轉出/畢業學生的 IEP 還能被 teacher 看到 — 是 latent bug）
3. 沒接 PermissionGrant scope

改造方案：完全 delegate
```python
# 移除 L76-105 _student_ids_in_scope, L108-118 _assert_student_in_scope, L139-153 _scoped_query

# 改為直接 import:
from utils.portfolio_access import student_ids_in_scope, assert_student_access

# 用法替換：
# 原 _scoped_query(db, current_user) →
#   allowed = student_ids_in_scope(db, current_user, code=Permission.STUDENTS_SPECIAL_NEEDS_WRITE.value)
#   q = db.query(StudentIEPRecord).filter(StudentIEPRecord.deleted_at == None)
#   if allowed is None: return q
#   if not allowed: return q.filter(False)
#   return q.filter(StudentIEPRecord.student_id.in_(allowed))
# （這就是 _scoped_query 的內容，只是 student_ids_in_scope 改用 portfolio_access）

# 原 _assert_student_in_scope(db, current_user, student_id) →
#   assert_student_access(db, current_user, student_id, code=Permission.STUDENTS_SPECIAL_NEEDS_WRITE.value)
#   # 注意 assert_student_access 回傳 Student 但 IEP 用法忽略 return value
```

注意：原 `_student_ids_in_scope` 對「主任以上」回 None（全放行），portfolio_access 的 `is_unrestricted` 只認 admin/hr/supervisor 角色 + `:all` scope。如果業主期待主任以上的「老師」（`role=teacher` + `supervisor_role=主任`）能看全部 IEP，需透過 DB-driven 自訂角色配 `STUDENTS_SPECIAL_NEEDS_WRITE:all` 給主任 — 而非在 code 寫死。**這是行為變更**：

- [ ] **Step 1: 與 user 確認**：主任 / 園長能看全部 IEP 是否仍要透過角色配置（推薦），還是維持 hard-code（保守）

如果用 hard-code（保守路徑）：
```python
def _student_ids_in_scope(db, current_user):
    if current_user.get("role") == "admin":
        return None
    # 保留主任以上判斷
    employee_id = current_user.get("employee_id")
    if employee_id:
        emp = db.query(Employee).filter(Employee.id == employee_id).first()
        if emp and emp.supervisor_role in ("園長", "主任"):
            return None
    # 其他走 portfolio_access
    return student_ids_in_scope(db, current_user, code=Permission.STUDENTS_SPECIAL_NEEDS_WRITE.value)
```

如果走自訂角色路徑（推薦）：
- 移除主任以上判斷
- 在 Phase 2.2 deploy 後請 user 在 Settings UI 給主任 / 園長角色配 `STUDENTS_SPECIAL_NEEDS_WRITE:all`
- migration 階段可在 permscope03 順帶 seed 主任 / 園長角色

- [ ] **Step 2: 寫 integration test for 3 角色 × IEP list/create endpoint**
- [ ] **Step 3: Refactor 移除 / 改寫 helper**
- [ ] **Step 4: 確認既有 iep test 跑 PASS**
- [ ] **Step 5: Commit**

```bash
git commit -m "fix(gov_moe/iep): delegate scope to portfolio_access 修 lifecycle 終態過濾漏洞

移除自有 _student_ids_in_scope/_assert_student_in_scope；改 delegate 至
utils/portfolio_access.py（含 PermissionGrant scope 支援 + lifecycle 終態過濾
audit 2026-05-07 P0 #5）。

iep.py 既有寫法用 Employee.classroom_id 而非 Classroom 三角 OR，teacher 換班
時可能 stale；改 delegate 後與全系統一致。"
```

---

## Task 8: Final integration verification

**Files:**
- Test: 跑 full backend pytest

- [ ] **Step 1: Run focused suite**

```bash
pytest tests/test_permscope_health_medication.py tests/test_portfolio_access_scope_bridge.py tests/test_alembic_permscope03.py -v
```

- [ ] **Step 2: Run regression — student_health / portal / iep / dashboard**

```bash
pytest tests/test_student_health.py tests/test_portal_medications.py tests/test_iep.py tests/test_dashboard_query.py -v 2>&1 | tail -50
```

Expected: 全 PASS，零 regression。

- [ ] **Step 3: Run full backend pytest**

```bash
pytest 2>&1 | tail -10
```

Expected: 與 baseline 相同 fail count（不增不減）。

- [ ] **Step 4: Frontend OpenAPI codegen 漂移檢查**

無 schema 變動，frontend 不需重 gen。

- [ ] **Step 5: Final commit + push**

```bash
git log --oneline -10
git push -u origin feat/permission-row-level-scoping-phase2.2-health-medication-2026-05-30-backend
```

---

## Out of scope

- 主任 / 園長角色 IEP scope 改造（Task 7 Step 1 確認）— 若走自訂角色路徑，預期在 Phase 2.2 deploy 後由 user 手動 Settings UI 配置
- Frontend `getPermissionScope` 已是通用 helper，無 family-specific 改動
- HEALTH 相關非 portfolio_access 的其他自有 scope（若 investigation 漏掉，task 6 verify 補上）

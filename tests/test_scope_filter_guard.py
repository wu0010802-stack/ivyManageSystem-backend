"""Tests for scripts/lint_scope_filter_guard.py（scope-filter 守衛）。

涵蓋兩層：
  1. fixture：驗證 lint 邏輯（違規偵測 / helper 合規 / 遞迴 delegate / 豁免）。
  2. 真實 codebase：把守衛接進 pytest，作為防漂移 gate——新增 scope-aware 端點
     漏套 filter、或修補 baseline 端點後忘了清 KNOWN_UNSCOPED，都會在此 fail。
"""

import sys
import textwrap
from pathlib import Path

# 讓 scripts/ 可被 import（對齊 tests/test_alembic_symmetry_lint.py 的慣例）。
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from lint_scope_filter_guard import (  # noqa: E402
    _default_api_root,
    lint_api_dir,
    lint_source,
    partition_violations,
)


def _keys(violations):
    return {key for key, _ in violations}


def test_lint_scope_aware_perms_is_single_source_of_truth():
    """SCOPE-5：lint 的 SCOPE_AWARE_PERMS 須直接來自 utils.permissions.SCOPE_AWARE_CODES
    （同一物件），消除 BE↔lint 各自手抄漂移。若有人重新硬抄一份 frozenset，identity
    比對會 fail。"""
    import lint_scope_filter_guard as lint
    from utils.permissions import SCOPE_AWARE_CODES

    assert lint.SCOPE_AWARE_PERMS is SCOPE_AWARE_CODES, (
        "lint.SCOPE_AWARE_PERMS 應 import 自 utils.permissions.SCOPE_AWARE_CODES，"
        "不可在 lint 內另立硬抄副本（會與 BE has_permission 漂移）"
    )


def test_scope_aware_codes_count_canary():
    """SCOPE-5 canary：scope-aware 權限數量改變時提醒同步前端 SCOPE_AWARE_CODES
    與 permscope alembic seed（對應前端 scope-aware-parity.test.ts 的 size canary）。"""
    from utils.permissions import SCOPE_AWARE_CODES

    assert len(SCOPE_AWARE_CODES) == 13, (
        f"SCOPE_AWARE_CODES 數量為 {len(SCOPE_AWARE_CODES)}（預期 13）；"
        "新增/移除 scope-aware 權限時，請同步前端 auth.ts SCOPE_AWARE_CODES、"
        "前端 scope-aware-parity.test.ts 的 EXPECTED、以及 permscope alembic seed，"
        "再更新此 canary 數字。"
    )


def test_scope_aware_gate_without_filter_is_flagged():
    src = textwrap.dedent("""
        @router.get("/x")
        def list_x(
            current_user: dict = Depends(
                require_staff_permission(Permission.STUDENTS_READ)
            ),
        ):
            return session.query(Student).all()
        """)
    assert _keys(lint_source(src, "m")) == {"m:list_x"}


def test_filter_via_scope_helper_passes():
    src = textwrap.dedent("""
        @router.get("/x")
        def list_x(
            current_user: dict = Depends(
                require_staff_permission(Permission.STUDENTS_READ)
            ),
        ):
            ids = student_ids_in_scope(session, current_user, code="STUDENTS_READ")
            return ids
        """)
    assert lint_source(src, "m") == []


def test_all_scope_lock_passes():
    src = textwrap.dedent("""
        @router.get("/x")
        def export_x(
            current_user: dict = Depends(
                require_staff_permission(Permission.STUDENTS_READ)
            ),
        ):
            assert_all_scope(current_user, "STUDENTS_READ")
            return session.query(Student).all()
        """)
    assert lint_source(src, "m") == []


def test_recursive_delegate_chain_passes():
    """endpoint → _scoped_query → _inner → student_ids_in_scope（多層 delegate）。"""
    src = textwrap.dedent("""
        def _inner(db, user):
            return student_ids_in_scope(db, user)

        def _scoped_query(db, user):
            return _inner(db, user)

        @router.get("/x")
        def list_x(
            current_user: dict = Depends(
                require_staff_permission(Permission.STUDENTS_READ)
            ),
        ):
            return _scoped_query(db, current_user).all()
        """)
    assert lint_source(src, "m") == []


def test_non_scope_aware_gate_is_ignored():
    src = textwrap.dedent("""
        @router.get("/x")
        def list_x(
            current_user: dict = Depends(
                require_staff_permission(Permission.SALARY_READ)
            ),
        ):
            return session.query(Salary).all()
        """)
    assert lint_source(src, "m") == []


def test_non_endpoint_function_is_ignored():
    """沒有 @router decorator 的 function 不檢查（即使參數帶 scope-aware gate）。"""
    src = textwrap.dedent("""
        def helper(
            current_user: dict = Depends(
                require_staff_permission(Permission.STUDENTS_READ)
            ),
        ):
            return session.query(Student).all()
        """)
    assert lint_source(src, "m") == []


def test_real_codebase_no_new_failopen_and_no_stale_baseline():
    """真實 codebase 防漂移 gate：
    - active 為空 → 沒有「新增、未登記」的潛在 fail-open 端點。
    - stale 為空 → KNOWN_UNSCOPED baseline 沒有「已修補卻沒移除」的腐爛條目。
    """
    active, stale = partition_violations(lint_api_dir(_default_api_root()))
    assert active == [], f"新增未過濾的 scope-aware 端點：{active}"
    assert stale == [], f"KNOWN_UNSCOPED 有過時條目（已修補，請移除）：{stale}"

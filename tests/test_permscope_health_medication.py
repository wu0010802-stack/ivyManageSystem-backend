"""Phase 2.2 router migration regression tests.

驗證 api/student_health.py 與 services/dashboard_query_service.py
在呼叫 portfolio_access helper (assert_student_access / student_ids_in_scope)
時，皆已帶入對應的 ``code=Permission.<X>.value``，
讓 row-level scope 能依端點 perm 各自走 :scope variant。
"""

import inspect


def test_student_health_endpoints_use_health_codes():
    import api.student_health as mod

    source = inspect.getsource(mod)
    assert "code=Permission.STUDENTS_HEALTH_READ" in source
    assert "code=Permission.STUDENTS_HEALTH_WRITE" in source
    assert "code=Permission.STUDENTS_MEDICATION_ADMINISTER" in source


def test_student_health_no_bare_assert_student_access():
    """所有 assert_student_access 呼叫都應帶 code=（防止漏改 regression）。"""
    import api.student_health as mod

    source = inspect.getsource(mod)
    # 把所有 `assert_student_access(` 出現的後續片段抓出，每處都應含 ``code=``
    # 不用 regex 跨行（python source 簡單以 line 切）
    lines = source.splitlines()
    offenders: list[str] = []
    for i, line in enumerate(lines):
        if "assert_student_access(" in line and "def assert_student_access" not in line:
            # 抓整個 call site：往下掃到括號平衡為止（black 可能把參數展開成
            # 逐行、code= 落在第 5+ 行，固定行數窗會誤報）
            depth = 0
            snippet_lines: list[str] = []
            for cont in lines[i:]:
                snippet_lines.append(cont)
                depth += cont.count("(") - cont.count(")")
                if depth <= 0:
                    break
            snippet = "\n".join(snippet_lines)
            if "code=" not in snippet:
                offenders.append(f"line {i + 1}: {line.strip()}")
    assert not offenders, "bare assert_student_access calls: " + "; ".join(offenders)


def test_student_health_today_medication_uses_health_read():
    import api.student_health as mod

    source = inspect.getsource(mod)
    # today_medication_summary endpoint 內 student_ids_in_scope 必帶 STUDENTS_HEALTH_READ
    # 同檔僅此一處呼叫 student_ids_in_scope
    assert source.count("student_ids_in_scope(") == 1
    idx = source.index("student_ids_in_scope(")
    snippet = source[idx : idx + 200]
    assert "code=Permission.STUDENTS_HEALTH_READ" in snippet


def test_dashboard_today_medication_summary_uses_health_read_code():
    import services.dashboard_query_service as mod

    source = inspect.getsource(mod)
    # build_today_medication_summary 必傳 code= STUDENTS_HEALTH_READ
    # （L305 _count_recent_parent_leaves 走 LEAVES/STUDENTS_READ，本 phase 不動）
    assert "code=Permission.STUDENTS_HEALTH_READ" in source


def test_portal_medications_today_uses_health_read_scope():
    """api/portal/medications.py 必須改用 portfolio_access bridge（accessible_classroom_ids
    + is_unrestricted）並帶 ``code=Permission.STUDENTS_HEALTH_READ``，
    取代既有自有 `_get_teacher_classroom_ids` + role-based `is_admin_like` 判斷。
    """
    import api.portal.medications as mod

    source = inspect.getsource(mod)
    assert (
        "code=Permission.STUDENTS_HEALTH_READ" in source
    ), "list_today_medications 應帶 code=Permission.STUDENTS_HEALTH_READ"
    assert (
        "accessible_classroom_ids" in source
    ), "應 import 並使用 accessible_classroom_ids"
    assert "is_unrestricted" in source, "應 import 並使用 is_unrestricted"
    # 確認移除舊邏輯
    assert (
        "_get_teacher_classroom_ids(session, emp.id)" not in source
    ), "應改用 accessible_classroom_ids(code=) 取代 _get_teacher_classroom_ids"
    assert (
        "is_admin_like" not in source
    ), "應改用 is_unrestricted(code=) 取代自有 is_admin_like role/wildcard 判斷"


def test_portal_class_hub_medications_gated_by_health_read():
    """api/portal/class_hub.py 設計為單班教師工作台（resolve_teacher_classroom 取得單一班），
    與 ClassHubTodayResponse(classroom_id) 單班 schema 綁定，無法表達跨班 `:all` 語意。
    確認 medication 來源走 scope-aware `has_permission`（Task 2.5 後 base code 自動匹配
    `:all`/`:own_class`），無需 router/service 層額外傳 `code=`。
    """
    import api.portal.class_hub as mod

    source = inspect.getsource(mod)
    # has(Permission.STUDENTS_HEALTH_READ) gate 仍在
    assert "Permission.STUDENTS_HEALTH_READ" in source
    # has_permission 來源確認（Task 2.5 已改成 scope-aware）
    assert "from utils.permissions import" in source
    assert "has_permission" in source


# ---------------------------------------------------------------------------
# Task 7 regression: api/gov_moe/iep.py delegate to portfolio_access
# ---------------------------------------------------------------------------


def test_iep_delegates_scope_to_portfolio_access():
    """iep.py 必移除自有 _student_ids_in_scope 邏輯，改 delegate 至
    portfolio_access（含 lifecycle 終態學生過濾 audit 2026-05-07 P0 #5 +
    PermissionGrant scope）。"""
    import api.gov_moe.iep as mod

    source = inspect.getsource(mod)
    # 必 import portfolio_access helper
    assert "from utils.portfolio_access import" in source
    # _student_ids_in_scope 與 _assert_student_in_scope 必帶 code=
    assert "code=Permission.STUDENTS_SPECIAL_NEEDS_WRITE" in source
    # 既有 Employee.classroom_id 自有路徑必移除（teacher 換班 stale 風險）
    assert (
        "emp.classroom_id" not in source
    ), "iep.py 不應再用 Employee.classroom_id；改走 portfolio_access 三角 OR"
    # supervisor_role hard-code 必移除（改靠 PermissionGrant :all scope）
    # 注意：_is_supervisor_or_above 用於 approve/close，仍保留 supervisor_role 判斷
    # 此處只檢查 _student_ids_in_scope 函式不再硬編
    src_lines = source.splitlines()
    in_scope_helper = False
    scope_helper_lines: list[str] = []
    for line in src_lines:
        if line.startswith("def _student_ids_in_scope"):
            in_scope_helper = True
            continue
        if in_scope_helper:
            if line.startswith("def ") or (line and not line.startswith((" ", "\t"))):
                break
            scope_helper_lines.append(line)
    helper_src = "\n".join(scope_helper_lines)
    assert (
        "supervisor_role" not in helper_src
    ), "_student_ids_in_scope 內不應再 hard-code supervisor_role；改走 PermissionGrant :all"


def test_iep_no_bare_assert_student_in_scope():
    """所有 _assert_student_in_scope 呼叫間接走 portfolio_access；
    確保 assert_student_access call site 都帶 code=（防止 phase 2.2 漏改 regression）。"""
    import api.gov_moe.iep as mod

    source = inspect.getsource(mod)
    lines = source.splitlines()
    offenders: list[str] = []
    for i, line in enumerate(lines):
        if "assert_student_access(" in line and "def assert_student_access" not in line:
            # 取後續 7 行以涵蓋多行 call site（含 trailing keyword arg + paren）
            snippet = "\n".join(lines[i : i + 7])
            if "code=" not in snippet:
                offenders.append(f"line {i + 1}: {line.strip()}")
    assert not offenders, "bare assert_student_access calls: " + "; ".join(offenders)

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
            # 抓 call site（多行 call 需往下看一兩行）
            snippet = "\n".join(lines[i : i + 3])
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

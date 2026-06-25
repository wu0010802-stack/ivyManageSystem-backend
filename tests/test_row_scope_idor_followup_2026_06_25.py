"""Row-scope IDOR 後續修補（2026-06-25 嚴重度門檻稽核 follow-up）。

P2-1 IDOR 窄修（is_row_unrestricted + _ROW_SCOPED_DATA_CODES allowlist）漏掉 3 個
row-level caller，bare scope-aware 碼自訂角色（非 admin/hr/supervisor）仍越權：

  #1 api/portal/medications.py — list_today_medications 用 is_unrestricted（端點測試
     見 test_portal_medications.py::test_bare_code_non_management_role_no_all_school）
  #2 api/activity/_shared.resolve_student_pii_scope — 用 is_unrestricted（本檔）
  #3 utils/portfolio_access._ROW_SCOPED_DATA_CODES — 漏 PORTFOLIO_PUBLISH（本檔）

修法沿用 P2-1 Option C 窄修：row-level helper 改用 is_row_unrestricted；
PORTFOLIO_PUBLISH 補進 _ROW_SCOPED_DATA_CODES（與 PORTFOLIO_READ/WRITE 同家族、
逐筆學生操作）。本檔 unit 層直接驗 helper / scope 解析行為。
"""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Classroom, Student
from models.employee import Employee
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access, is_row_unrestricted

PUBLISH = Permission.PORTFOLIO_PUBLISH.value
STUDENTS_READ = Permission.STUDENTS_READ.value


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "row_scope_idor_followup.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    s = sf()
    yield s
    s.close()
    base_module._engine, base_module._SessionFactory = old_engine, old_sf
    engine.dispose()


def _seed_other_class_student(s):
    """A 班 + A 班學生；回傳 (classroom_id, student_id)。caller 不擔任此班導師。"""
    cls = Classroom(name="A班", school_year=113, semester=2, is_active=True)
    s.add(cls)
    s.flush()
    stu = Student(
        student_id="S900",
        name="他班學生",
        classroom_id=cls.id,
        is_active=True,
        lifecycle_status="active",
    )
    s.add(stu)
    s.commit()
    return cls.id, stu.id


def _custom_role_user(perms):
    """非管理角色（principal，∉ {admin,hr,supervisor}）、無擔任班級。"""
    return {"role": "principal", "employee_id": None, "permission_names": perms}


# ── #2 才藝端 PII 遮罩範圍：resolve_student_pii_scope ─────────────────


def test_pii_scope_bare_students_read_scopes_to_own_class(db_session):
    """bare STUDENTS_READ 非管理角色 → (True, set) 限自班，非 (True, None) 全園。

    修前：resolve_student_pii_scope 以 is_unrestricted(bare=all) → (True, None)
    全園不遮罩，自訂 staff 角色經才藝 registrations/POS/點名端點看全校學生 + 家長 PII。
    """
    from api.activity._shared import resolve_student_pii_scope

    visible, allowed = resolve_student_pii_scope(
        db_session, _custom_role_user([STUDENTS_READ])
    )
    assert visible is True
    assert (
        allowed == set()
    ), "bare 碼非管理角色應限自班（此處無班級＝空集），不得回 None(全園)"


def test_pii_scope_management_bare_still_unrestricted(db_session):
    """管理角色 bare STUDENTS_READ → (True, None) 全園不變（不被波及）。"""
    from api.activity._shared import resolve_student_pii_scope

    user = {
        "role": "supervisor",
        "employee_id": None,
        "permission_names": [STUDENTS_READ],
    }
    assert resolve_student_pii_scope(db_session, user) == (True, None)


def test_pii_scope_explicit_all_still_unrestricted(db_session):
    """顯式 STUDENTS_READ:all 自訂角色 → (True, None) 全園（明示跨班，不受影響）。"""
    from api.activity._shared import resolve_student_pii_scope

    user = _custom_role_user([f"{STUDENTS_READ}:all"])
    assert resolve_student_pii_scope(db_session, user) == (True, None)


def test_pii_scope_no_students_read_invisible(db_session):
    """無 STUDENTS_READ → (False, None)（契約不變）。"""
    from api.activity._shared import resolve_student_pii_scope

    assert resolve_student_pii_scope(db_session, _custom_role_user([])) == (False, None)


# ── #3 成長報告發佈：PORTFOLIO_PUBLISH 收斂 ──────────────────────────


def test_portfolio_publish_bare_blocks_cross_class(db_session):
    """bare PORTFOLIO_PUBLISH 非管理角色 → assert_student_access 他班 403。

    修前：PUBLISH 不在 _ROW_SCOPED_DATA_CODES → is_row_unrestricted delegate 回
    is_unrestricted(bare=all) → 放行任一他班學生（可跨班建立/刪除/發 LINE 成長報告）。
    """
    _, stu_id = _seed_other_class_student(db_session)
    with pytest.raises(HTTPException) as exc:
        assert_student_access(
            db_session, _custom_role_user([PUBLISH]), stu_id, code=PUBLISH
        )
    assert exc.value.status_code == 403


def test_portfolio_publish_row_scoped_policy():
    """政策鎖：PORTFOLIO_PUBLISH 屬逐筆學生資料碼，bare 非管理角色收斂；
    管理角色（supervisor 模板含 bare PORTFOLIO_PUBLISH）與顯式 :all 不受影響。"""
    # 非管理角色 bare → 收斂（False）
    assert is_row_unrestricted(_custom_role_user([PUBLISH]), PUBLISH) is False
    # 管理角色 bare → True（supervisor 模板靠此取全校）
    assert (
        is_row_unrestricted(
            {"role": "supervisor", "permission_names": [PUBLISH]}, PUBLISH
        )
        is True
    )
    # 顯式 :all → True（自訂角色明示跨班）
    assert is_row_unrestricted(_custom_role_user([f"{PUBLISH}:all"]), PUBLISH) is True


def test_portfolio_publish_explicit_all_grants_cross_class(db_session):
    """顯式 PORTFOLIO_PUBLISH:all 自訂角色 → 仍可逐筆全園（明示跨班，不受影響）。"""
    _, stu_id = _seed_other_class_student(db_session)
    user = _custom_role_user([f"{PUBLISH}:all"])
    assert assert_student_access(db_session, user, stu_id, code=PUBLISH).id == stu_id


def test_portfolio_publish_management_role_unrestricted(db_session):
    """supervisor（管理角色）bare PORTFOLIO_PUBLISH → 逐筆全園不受影響。"""
    _, stu_id = _seed_other_class_student(db_session)
    user = {"role": "supervisor", "employee_id": None, "permission_names": [PUBLISH]}
    assert assert_student_access(db_session, user, stu_id, code=PUBLISH).id == stu_id

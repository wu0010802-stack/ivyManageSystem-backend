"""P2-1：bare scope-aware 權限不應自動給「逐筆學生」全園存取（IDOR）。

稽核 finding：resolve_grant 把 bare ``PORTFOLIO_READ``（無 ``:scope``）解析成
scope=='all'（向後相容），而 row-level helper（assert_student_access /
accessible_classroom_ids / filter_student_ids_by_access）以 is_unrestricted()
判斷是否全放行 → 自訂角色（非 admin/hr/supervisor）若被授予 **bare**
``PORTFOLIO_READ``，會被當成全園 unrestricted → 可讀任一他班學生的作品集（IDOR）。

決策 Option C（外科式）：
- ``assert_all_scope`` / ``is_unrestricted``（全園彙總/匯出語意）**保留 bare=all 零行為變更**。
- row-level helper 改用 ``is_row_unrestricted``：只有 **管理角色 / wildcard / 顯式
  ``<code>:all``** 才全放行；**bare ``<code>`` 不算** → 落回 own_class scoping。

本檔以 unit 層直接驗 helper 行為（不經端點）。
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
from utils.portfolio_access import (
    assert_all_scope,
    assert_student_access,
    filter_student_ids_by_access,
)

PORTFOLIO_READ = Permission.PORTFOLIO_READ.value


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "p2_1_row_scope.sqlite"
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
    """A 班 + A 班學生；回傳 (classroom_id, student_id)。teacher 由各測試自配。"""
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


# ── 核心 IDOR（RED）──────────────────────────────────────────────


def test_bare_scope_aware_code_does_not_grant_cross_class_row_access(db_session):
    """自訂角色持 bare PORTFOLIO_READ → 不得逐筆存取他班學生（修前 bare=all 會放行）。"""
    s = db_session
    _, stu_id = _seed_other_class_student(s)
    # principal 角色（非 admin/hr/supervisor），無任何擔任班級，持 bare 碼。
    principal = Employee(employee_id="P1", name="園長")
    s.add(principal)
    s.flush()
    s.commit()
    user = {
        "role": "principal",
        "employee_id": principal.id,
        "permission_names": [PORTFOLIO_READ],  # bare，無 :scope
    }
    with pytest.raises(HTTPException) as exc:
        assert_student_access(s, user, stu_id, code=PORTFOLIO_READ)
    assert exc.value.status_code == 403


def test_bare_scope_aware_code_filters_out_cross_class_in_batch(db_session):
    """filter_student_ids_by_access：bare 碼自訂角色批次過濾掉他班學生（非全放行）。"""
    s = db_session
    _, stu_id = _seed_other_class_student(s)
    user = {
        "role": "principal",
        "employee_id": None,
        "permission_names": [PORTFOLIO_READ],
    }
    allowed = filter_student_ids_by_access(s, user, [stu_id], code=PORTFOLIO_READ)
    assert allowed == set(), "bare 碼不應使他班學生全數放行"


# ── 必須保留的契約（綠）──────────────────────────────────────────


def test_explicit_all_scope_still_grants_row_access(db_session):
    """顯式 PORTFOLIO_READ:all 的自訂角色 → 仍可逐筆全園存取（不受影響）。"""
    s = db_session
    _, stu_id = _seed_other_class_student(s)
    user = {
        "role": "principal",
        "employee_id": None,
        "permission_names": [f"{PORTFOLIO_READ}:all"],
    }
    # 不應 raise
    got = assert_student_access(s, user, stu_id, code=PORTFOLIO_READ)
    assert got.id == stu_id


def test_management_role_still_unrestricted_row_access(db_session):
    """admin/supervisor（管理角色）→ 逐筆全園存取不受影響（即使只持 bare 碼）。"""
    s = db_session
    _, stu_id = _seed_other_class_student(s)
    for role in ("admin", "supervisor", "hr"):
        user = {
            "role": role,
            "employee_id": None,
            "permission_names": [PORTFOLIO_READ],
        }
        got = assert_student_access(s, user, stu_id, code=PORTFOLIO_READ)
        assert got.id == stu_id, f"{role} 應維持全園存取"


def test_own_class_role_scoped_to_own_class(db_session):
    """持 PORTFOLIO_READ:own_class 的教師 → 自班可、他班 403（既有 scoping 不變）。"""
    s = db_session
    other_cls_id, other_stu_id = _seed_other_class_student(s)
    teacher = Employee(employee_id="T1", name="導師")
    s.add(teacher)
    s.flush()
    own = Classroom(
        name="B班",
        school_year=113,
        semester=2,
        head_teacher_id=teacher.id,
        is_active=True,
    )
    s.add(own)
    s.flush()
    own_stu = Student(
        student_id="S901",
        name="自班學生",
        classroom_id=own.id,
        is_active=True,
        lifecycle_status="active",
    )
    s.add(own_stu)
    s.commit()
    user = {
        "role": "teacher",
        "employee_id": teacher.id,
        "permission_names": [f"{PORTFOLIO_READ}:own_class"],
    }
    # 自班可
    assert (
        assert_student_access(s, user, own_stu.id, code=PORTFOLIO_READ).id == own_stu.id
    )
    # 他班 403
    with pytest.raises(HTTPException) as exc:
        assert_student_access(s, user, other_stu_id, code=PORTFOLIO_READ)
    assert exc.value.status_code == 403


def test_allowlist_membership_controls_bare_de_escalation():
    """verified-safe 窄修：只有 _ROW_SCOPED_DATA_CODES 內的 code bare 才收斂；
    集合外（如 STUDENTS_IEP_APPROVE 全校批核）bare 維持 =all（不破壞主任批核）。"""
    from utils.portfolio_access import is_row_unrestricted

    custom = lambda perms: {  # noqa: E731
        "role": "principal",
        "employee_id": None,
        "permission_names": perms,
    }
    # in-set bare → 收斂（False）
    assert is_row_unrestricted(custom([PORTFOLIO_READ]), PORTFOLIO_READ) is False
    assert (
        is_row_unrestricted(
            custom([Permission.DISMISSAL_CALLS_READ.value]),
            Permission.DISMISSAL_CALLS_READ.value,
        )
        is False
    )
    # out-of-set bare → 維持 all（True，delegate 回 is_unrestricted）
    assert (
        is_row_unrestricted(
            custom([Permission.STUDENTS_IEP_APPROVE.value]),
            Permission.STUDENTS_IEP_APPROVE.value,
        )
        is True
    )
    # in-set 顯式 :all → True
    assert (
        is_row_unrestricted(custom([f"{PORTFOLIO_READ}:all"]), PORTFOLIO_READ) is True
    )
    # 管理角色 → True（不論 code）
    assert (
        is_row_unrestricted(
            {"role": "supervisor", "permission_names": [PORTFOLIO_READ]}, PORTFOLIO_READ
        )
        is True
    )


def test_students_data_codes_de_escalate_but_workflow_codes_keep_all():
    """2026-06-25 擴充：STUDENTS_* 逐筆資料碼（teacher 模板 :own_class）bare→收斂；
    全校 workflow/管理職碼（IEP_APPROVE/SPECIAL_NEEDS_WRITE/HEALTH_WRITE/LIFECYCLE_WRITE）
    維持 bare=all（主任靠 bare 取全校為正當設計）。"""
    from utils.portfolio_access import is_row_unrestricted

    P = Permission

    def custom(code):
        return {"role": "principal", "employee_id": None, "permission_names": [code]}

    # 逐筆資料碼 in-set → bare 收斂（False）
    for code in (
        P.STUDENTS_READ.value,
        P.STUDENTS_WRITE.value,
        P.STUDENTS_HEALTH_READ.value,
        P.STUDENTS_MEDICATION_ADMINISTER.value,
        P.STUDENTS_SPECIAL_NEEDS_READ.value,
    ):
        assert (
            is_row_unrestricted(custom(code), code) is False
        ), f"{code} bare 應收斂 own_class"
    # 全校 workflow/管理職碼 out-of-set → bare 維持 all（True）
    for code in (
        P.STUDENTS_IEP_APPROVE.value,
        P.STUDENTS_SPECIAL_NEEDS_WRITE.value,
        P.STUDENTS_HEALTH_WRITE.value,
        P.STUDENTS_LIFECYCLE_WRITE.value,
    ):
        assert (
            is_row_unrestricted(custom(code), code) is True
        ), f"{code} bare 應維持全校（管理職正當）"


def test_explicit_own_class_overrides_management_role():
    """管理角色被顯式授 ``<code>:own_class`` → 仍收斂自班（顯式 scope 覆蓋角色預設）。

    回歸：test_search 用 supervisor + STUDENTS_READ:own_class 期望限自班；
    is_row_unrestricted 早期 role-first 短路會誤判全校 → 修為顯式 scope 先判。
    """
    from utils.portfolio_access import is_row_unrestricted

    code = Permission.STUDENTS_READ.value
    # 管理角色 + 顯式 :own_class → 收斂（False）
    assert (
        is_row_unrestricted(
            {"role": "supervisor", "permission_names": [f"{code}:own_class"]}, code
        )
        is False
    )
    # 管理角色 + bare（無顯式 scope）→ 全校（True，角色預設）
    assert (
        is_row_unrestricted({"role": "supervisor", "permission_names": [code]}, code)
        is True
    )


def test_assert_all_scope_still_treats_bare_as_all(db_session):
    """全園彙總守衛 assert_all_scope：bare 碼仍視為 all（零行為變更，不被本修法波及）。"""
    user = {
        "role": "principal",
        "employee_id": None,
        "permission_names": [PORTFOLIO_READ],  # bare
    }
    # 不應 raise（bare 對全園匯出端點維持 all）
    assert_all_scope(user, PORTFOLIO_READ)

"""驗證學生 scope + lifecycle 終態守衛。

audit 2026-05-07 P0 重新評估後的實際修補面：

#1-#4 主端點（/api/students 列表/單筆/PUT/DELETE/bulk-transfer）：
    既有 require_staff_permission 已擋 teacher/parent 撞管理端 API，
    audit 對「STUDENTS_READ teacher 可拉全園 PII」的指摘其實已守住。
    本批 require_unrestricted_role 是 defense-in-depth：把「角色限定」從
    middleware 層（教師/家長拒絕）下沉到 endpoint 層，給出更具體的 403 訊息。

#5 utils/portfolio_access.py / parent_portal/_shared.py：
    assert_student_access / filter_student_ids_by_access / student_ids_in_scope
    對 teacher 角色額外排除 lifecycle 終態（graduated/withdrawn/transferred）。
    這條真正影響 teacher portal 的 sub-resource 端點（health/medication/
    incident/assessment）— teacher 不再對自班已退學/畢業/轉出學生有讀寫權。

#6 parent_portal._assert_student_owned(for_write=True)：
    家長對自己退學/畢業子女寫入動作（投藥單/簽收/請假/活動報名/藥袋照片）→ 403；
    讀路徑（成長紀錄、歷史聯絡簿）保留。
"""

import os
import sys

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.students import router as students_router
from models.classroom import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)
from models.database import Base, Classroom, Employee, Guardian, Student, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def students_client(tmp_path):
    db_path = tmp_path / "students_scope.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(students_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def isolated_db(tmp_path):
    """純 DB session（不需 client）— 用於 unit test helper 函式。"""
    db_path = tmp_path / "scope_helper.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Sess = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    yield Sess
    engine.dispose()


def _create_emp(session, *, name="教師", emp_no="E001"):
    e = Employee(employee_id=emp_no, name=name, base_salary=36000, is_active=True)
    session.add(e)
    session.flush()
    return e


def _create_user(session, *, username, role, permissions, employee_id=None):
    u = User(
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
        employee_id=employee_id,
    )
    session.add(u)
    session.flush()
    return u


def _create_classroom(session, *, name="大班A", head_teacher_id=None):
    c = Classroom(name=name, is_active=True, head_teacher_id=head_teacher_id)
    session.add(c)
    session.flush()
    return c


def _create_student(
    session,
    *,
    name="學生",
    student_id="S001",
    classroom_id=None,
    lifecycle_status=LIFECYCLE_ACTIVE,
    is_active=True,
):
    s = Student(
        student_id=student_id,
        name=name,
        classroom_id=classroom_id,
        lifecycle_status=lifecycle_status,
        is_active=is_active,
    )
    session.add(s)
    session.flush()
    return s


def _login(client, username):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


READ_PERMS = int(Permission.STUDENTS_READ)
WRITE_PERMS = int(Permission.STUDENTS_WRITE) | int(Permission.STUDENTS_READ)


# ── /api/students 主端點：admin 不變，teacher 由 require_staff_permission 擋 ──


class TestAdminCanWriteAndSeeTerminal:
    """admin 對 /api/students 主端點寫入仍正常，含終態學生。"""

    def test_admin_can_put_student(self, students_client):
        client, sf = students_client
        with sf() as s:
            _create_user(s, username="adm", role="admin", permissions=WRITE_PERMS)
            cls_a = _create_classroom(s)
            stu = _create_student(s, classroom_id=cls_a.id)
            s.commit()
            sid = stu.id

        assert _login(client, "adm").status_code == 200
        res = client.put(
            f"/api/students/{sid}",
            json={"parent_phone": "0922-111-222"},
        )
        assert res.status_code == 200, res.text

    def test_supervisor_can_put_student(self, students_client):
        client, sf = students_client
        with sf() as s:
            _create_user(s, username="sup", role="supervisor", permissions=WRITE_PERMS)
            cls_a = _create_classroom(s)
            stu = _create_student(s, classroom_id=cls_a.id)
            s.commit()
            sid = stu.id

        assert _login(client, "sup").status_code == 200
        res = client.put(
            f"/api/students/{sid}",
            json={"address": "新地址"},
        )
        assert res.status_code == 200, res.text

    def test_admin_can_get_graduated_student(self, students_client):
        """admin 看終態學生不受影響（事後查歷史用）。"""
        client, sf = students_client
        with sf() as s:
            _create_user(s, username="adm2", role="admin", permissions=READ_PERMS)
            cls_a = _create_classroom(s)
            stu = _create_student(
                s, classroom_id=cls_a.id, lifecycle_status=LIFECYCLE_GRADUATED
            )
            s.commit()
            sid = stu.id

        assert _login(client, "adm2").status_code == 200
        res = client.get(f"/api/students/{sid}")
        assert res.status_code == 200

    def test_admin_can_bulk_transfer(self, students_client):
        client, sf = students_client
        with sf() as s:
            _create_user(s, username="adm3", role="admin", permissions=WRITE_PERMS)
            cls_a = _create_classroom(s, name="A")
            cls_b = _create_classroom(s, name="B")
            stu = _create_student(s, classroom_id=cls_a.id)
            s.commit()
            sid = stu.id
            target_id = cls_b.id

        assert _login(client, "adm3").status_code == 200
        res = client.post(
            "/api/students/bulk-transfer",
            json={"student_ids": [sid], "target_classroom_id": target_id},
        )
        assert res.status_code == 200, res.text


# ── utils/portfolio_access.assert_student_access lifecycle 過濾 ──


class TestAssertStudentAccessLifecycle:
    """teacher 對自班終態學生 → 403；admin/hr/supervisor 不受 lifecycle 限制。"""

    def _make_setup(self, session, lifecycle):
        emp = _create_emp(session, emp_no="E_T")
        cls_a = _create_classroom(session, head_teacher_id=emp.id)
        stu = _create_student(
            session, classroom_id=cls_a.id, lifecycle_status=lifecycle
        )
        return emp, cls_a, stu

    def test_teacher_active_own_class_passes(self, isolated_db):
        from utils.portfolio_access import assert_student_access

        with isolated_db() as s:
            emp, _, stu = self._make_setup(s, LIFECYCLE_ACTIVE)
            s.commit()
            current_user = {"role": "teacher", "employee_id": emp.id}
            sid = stu.id
        with isolated_db() as s:
            student = assert_student_access(s, current_user, sid)
            assert student.id == sid

    @pytest.mark.parametrize(
        "lifecycle",
        [LIFECYCLE_GRADUATED, LIFECYCLE_WITHDRAWN, LIFECYCLE_TRANSFERRED],
    )
    def test_teacher_terminal_own_class_blocked(self, isolated_db, lifecycle):
        from utils.portfolio_access import assert_student_access

        with isolated_db() as s:
            emp, _, stu = self._make_setup(s, lifecycle)
            s.commit()
            current_user = {"role": "teacher", "employee_id": emp.id}
            sid = stu.id
        with isolated_db() as s:
            with pytest.raises(HTTPException) as exc:
                assert_student_access(s, current_user, sid)
            assert exc.value.status_code == 403

    @pytest.mark.parametrize(
        "lifecycle",
        [LIFECYCLE_GRADUATED, LIFECYCLE_WITHDRAWN, LIFECYCLE_TRANSFERRED],
    )
    def test_admin_terminal_passes(self, isolated_db, lifecycle):
        """admin 看終態學生不受限。"""
        from utils.portfolio_access import assert_student_access

        with isolated_db() as s:
            cls_a = _create_classroom(s)
            stu = _create_student(s, classroom_id=cls_a.id, lifecycle_status=lifecycle)
            s.commit()
            current_user = {"role": "admin"}
            sid = stu.id
        with isolated_db() as s:
            student = assert_student_access(s, current_user, sid)
            assert student.id == sid


class TestStudentIdsInScopeLifecycle:
    """teacher 視野 student_ids_in_scope 排除終態；admin 全放行。"""

    def test_teacher_excludes_terminal(self, isolated_db):
        from utils.portfolio_access import student_ids_in_scope

        with isolated_db() as s:
            emp = _create_emp(s, emp_no="E_T2")
            cls_a = _create_classroom(s, head_teacher_id=emp.id)
            active = _create_student(
                s, name="A1", student_id="A1", classroom_id=cls_a.id
            )
            graduated = _create_student(
                s,
                name="G1",
                student_id="G1",
                classroom_id=cls_a.id,
                lifecycle_status=LIFECYCLE_GRADUATED,
            )
            withdrawn = _create_student(
                s,
                name="W1",
                student_id="W1",
                classroom_id=cls_a.id,
                lifecycle_status=LIFECYCLE_WITHDRAWN,
            )
            s.commit()
            current_user = {"role": "teacher", "employee_id": emp.id}
            active_id = active.id
            graduated_id = graduated.id
            withdrawn_id = withdrawn.id
        with isolated_db() as s:
            ids = student_ids_in_scope(s, current_user)
            assert ids == [active_id]
            assert graduated_id not in ids
            assert withdrawn_id not in ids

    def test_admin_returns_none(self, isolated_db):
        """admin 不受限 → 回 None（caller 跳過 filter）。"""
        from utils.portfolio_access import student_ids_in_scope

        with isolated_db() as s:
            current_user = {"role": "admin"}
        with isolated_db() as s:
            assert student_ids_in_scope(s, current_user) is None


class TestFilterStudentIdsLifecycle:
    """filter_student_ids_by_access 對 teacher 角色排除終態。"""

    def test_teacher_filters_out_terminal(self, isolated_db):
        from utils.portfolio_access import filter_student_ids_by_access

        with isolated_db() as s:
            emp = _create_emp(s, emp_no="E_T3")
            cls_a = _create_classroom(s, head_teacher_id=emp.id)
            active = _create_student(
                s, name="A2", student_id="A2", classroom_id=cls_a.id
            )
            graduated = _create_student(
                s,
                name="G2",
                student_id="G2",
                classroom_id=cls_a.id,
                lifecycle_status=LIFECYCLE_GRADUATED,
            )
            s.commit()
            current_user = {"role": "teacher", "employee_id": emp.id}
            ids = [active.id, graduated.id]
        with isolated_db() as s:
            result = filter_student_ids_by_access(s, current_user, ids)
            assert result == {active.id}


# ── parent_portal _assert_student_owned for_write ─────────────────


class TestParentAssertStudentOwnedForWrite:
    """家長對自己退學/畢業子女寫入路徑被擋；讀路徑保留。"""

    def _setup(self, session, lifecycle):
        user = User(
            username=f"parent_{lifecycle}",
            password_hash=hash_password("Passw0rd!"),
            role="parent",
            permissions=0,
            is_active=True,
            must_change_password=False,
        )
        session.add(user)
        session.flush()
        student = _create_student(
            session,
            student_id=f"P_{lifecycle}",
            lifecycle_status=lifecycle,
        )
        g = Guardian(
            user_id=user.id,
            student_id=student.id,
            name="家長",
            relation="father",
            is_primary=True,
        )
        session.add(g)
        return user, student

    def test_active_student_read_and_write_ok(self, isolated_db):
        from api.parent_portal._shared import _assert_student_owned

        with isolated_db() as s:
            user, student = self._setup(s, LIFECYCLE_ACTIVE)
            s.commit()
            uid = user.id
            sid = student.id
        with isolated_db() as s:
            _assert_student_owned(s, uid, sid, for_write=False)
            _assert_student_owned(s, uid, sid, for_write=True)

    @pytest.mark.parametrize(
        "lifecycle",
        [LIFECYCLE_GRADUATED, LIFECYCLE_WITHDRAWN, LIFECYCLE_TRANSFERRED],
    )
    def test_terminal_student_read_ok_write_blocked(self, isolated_db, lifecycle):
        from api.parent_portal._shared import _assert_student_owned

        with isolated_db() as s:
            user, student = self._setup(s, lifecycle)
            s.commit()
            uid = user.id
            sid = student.id
        with isolated_db() as s:
            # 讀仍可（家長可看歷史）
            _assert_student_owned(s, uid, sid, for_write=False)
            # 寫被擋（不能送新投藥/簽收/請假等）
            with pytest.raises(HTTPException) as exc:
                _assert_student_owned(s, uid, sid, for_write=True)
            assert exc.value.status_code == 403
            assert "已離校" in exc.value.detail

    def test_not_owned_blocked_regardless_of_for_write(self, isolated_db):
        """非自己小孩 → 無論讀寫一律 403（既有 IDOR 行為不變）。"""
        from api.parent_portal._shared import _assert_student_owned

        with isolated_db() as s:
            user, _ = self._setup(s, LIFECYCLE_ACTIVE)
            other = _create_student(s, student_id="OTHER", classroom_id=None)
            s.commit()
            uid = user.id
            other_id = other.id
        with isolated_db() as s:
            with pytest.raises(HTTPException) as exc:
                _assert_student_owned(s, uid, other_id, for_write=False)
            assert exc.value.status_code == 403
            with pytest.raises(HTTPException) as exc:
                _assert_student_owned(s, uid, other_id, for_write=True)
            assert exc.value.status_code == 403


# ── require_unrestricted_role helper ─────────────────────────────


class TestRequireUnrestrictedRole:
    @pytest.mark.parametrize("role", ["admin", "hr", "supervisor"])
    def test_passes_for_unrestricted_roles(self, role):
        from utils.portfolio_access import require_unrestricted_role

        require_unrestricted_role({"role": role})  # 不應 raise

    @pytest.mark.parametrize("role", ["teacher", "parent", "", "weird_role"])
    def test_blocks_other_roles(self, role):
        from utils.portfolio_access import require_unrestricted_role

        with pytest.raises(HTTPException) as exc:
            require_unrestricted_role({"role": role})
        assert exc.value.status_code == 403

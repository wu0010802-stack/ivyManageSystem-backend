"""POST /academic-terms/{id}/set-current 整合測試。

涵蓋 spec §9.2 的 11 個整合 scenario。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.academic_terms import router as academic_terms_router
from models.database import (
    Base,
    Classroom,
    Employee,
    LeaveQuota,
    LeaveRecord,
    Student,
    User,
)
from models.academic_term import AcademicTerm
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def term_test(tmp_path):
    """整合測試 fixture：TestClient + session factory + admin login headers。

    Returns:
        (client, session_factory, admin_headers) tuple
    """
    db_path = tmp_path / "term_integration.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(academic_terms_router)

    # 確保 subscriber 已 import 並以 register_handler 顯式重新註冊
    # （不能依賴 import side-effect：module cache 導致 @on_term_changed decorator
    # 只跑一次，若前面的 test suite 已跑過 reset_handlers_for_tests，bare import 不重跑）
    from utils.term_events import reset_handlers_for_tests, register_handler
    from services.term_subscribers.classroom_carry_over import handle as cco_handle
    from services.term_subscribers.leave_quota_cutover import handle as lqc_handle
    from services.term_subscribers.activity_semester_tag import handle as ast_handle

    reset_handlers_for_tests()
    register_handler("classroom_carry_over", cco_handle)
    register_handler("leave_quota_cutover", lqc_handle)
    register_handler("activity_semester_tag_reset", ast_handle)

    # 建 admin user
    with session_factory() as s:
        admin = User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["SETTINGS_READ", "SETTINGS_WRITE"],
            is_active=True,
        )
        s.add(admin)
        s.commit()

    # raise_server_exceptions=False：讓 handler RuntimeError 回 500 而非 re-raise，
    # 以便 test_handler_raise_rolls_back_entire_transaction 可 assert r.status_code == 500
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "TempPass123"},
        )
        assert resp.status_code == 200, resp.text
        # login 回 HttpOnly cookie，JSON body 沒有 access_token；
        # get_current_user 同時支援 cookie 和 Authorization Bearer header。
        token = resp.cookies.get("access_token")
        assert token, "login 未回 access_token cookie"
        admin_headers = {"Authorization": f"Bearer {token}"}

        yield client, session_factory, admin_headers

    _ip_attempts.clear()
    _account_failures.clear()
    reset_handlers_for_tests()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def client(term_test):
    return term_test[0]


@pytest.fixture
def db_session(term_test):
    """每個 test 內取得 fresh session（與 TestClient 共用 sqlite engine）。"""
    _, session_factory, _ = term_test
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def admin_headers(term_test):
    return term_test[2]


def _seed_term(session, *, school_year, semester, start_date, end_date):
    """Helper：直接 INSERT term row（繞過 /academic-terms POST 簡化 setup）。"""
    t = AcademicTerm(
        school_year=school_year,
        semester=semester,
        start_date=start_date,
        end_date=end_date,
    )
    session.add(t)
    session.flush()
    return t


def _seed_classroom(session, sy, sem, name="ABC"):
    cls = Classroom(name=name, school_year=sy, semester=sem, capacity=30)
    session.add(cls)
    session.flush()
    return cls


def _seed_student(session, classroom_id, student_id):
    s = Student(
        student_id=student_id,
        name=f"S{student_id}",
        gender="M",
        birthday=date(2020, 1, 1),
        classroom_id=classroom_id,
        is_active=True,
    )
    session.add(s)
    session.flush()
    return s


_emp_counter = 0


def _seed_emp(session, hire_date=date(2020, 9, 1)):
    global _emp_counter
    _emp_counter += 1
    e = Employee(
        employee_id=f"E{_emp_counter:03d}",
        name="員工",
        hire_date=hire_date,
        is_active=True,
    )
    session.add(e)
    session.flush()
    return e


class TestTermChangeIntegration:
    def test_initial_set_current_no_subscribers_run(
        self, client, db_session, admin_headers
    ):
        """old=None 時 3 subscriber 全 no-op。"""
        t = _seed_term(
            db_session,
            school_year=115,
            semester=1,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )
        db_session.commit()
        r = client.post(
            f"/api/academic-terms/{t.id}/set-current", headers=admin_headers
        )
        assert r.status_code == 200
        assert r.json()["is_current"] is True
        # 沒 classroom 被建、沒 quota 被建
        db_session.expire_all()
        assert db_session.query(Classroom).count() == 0
        assert db_session.query(LeaveQuota).count() == 0

    def test_same_year_1_to_2_classroom_carry_over(
        self, client, db_session, admin_headers
    ):
        """114-1 → 114-2：classroom 複製、學生遷移、quota 不動。"""
        old_t = _seed_term(
            db_session,
            school_year=114,
            semester=1,
            start_date=date(2025, 8, 1),
            end_date=date(2026, 1, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=114,
            semester=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 7, 31),
        )
        old_cls = _seed_classroom(db_session, 114, 1, name="星星班")
        s = _seed_student(db_session, old_cls.id, "114-A-01")
        db_session.commit()

        r = client.post(
            f"/api/academic-terms/{new_t.id}/set-current", headers=admin_headers
        )
        assert r.status_code == 200

        db_session.expire_all()
        new_cls = (
            db_session.query(Classroom)
            .filter(Classroom.school_year == 114, Classroom.semester == 2)
            .first()
        )
        assert new_cls is not None
        assert new_cls.name == "星星班"
        db_session.refresh(s)
        assert s.classroom_id == new_cls.id
        assert db_session.query(LeaveQuota).count() == 0

    def test_cross_year_2_to_1_leave_quota_cutover(
        self, client, db_session, admin_headers
    ):
        """114-2 → 115-1：classroom 不動、每員工生 new quota row。"""
        old_t = _seed_term(
            db_session,
            school_year=114,
            semester=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115,
            semester=1,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )
        emp = _seed_emp(db_session)
        db_session.commit()

        r = client.post(
            f"/api/academic-terms/{new_t.id}/set-current", headers=admin_headers
        )
        assert r.status_code == 200

        db_session.expire_all()
        rows = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
            )
            .all()
        )
        assert len(rows) == 6  # 5 QUOTA_LEAVE_TYPES + compensatory

    def test_cross_year_quota_compensatory_balance_carry_over(
        self, client, db_session, admin_headers
    ):
        """補休結餘 carry-over：舊 row 8h、used 2h → 新 row 6h。"""
        from models.leave import LeaveRecord

        old_t = _seed_term(
            db_session,
            school_year=114,
            semester=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115,
            semester=1,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )
        emp = _seed_emp(db_session)
        # 舊學年 compensatory quota 8h
        db_session.add(
            LeaveQuota(
                employee_id=emp.id,
                year=2026,
                school_year=114,
                leave_type="compensatory",
                total_hours=8.0,
            )
        )
        # 已用 2h
        db_session.add(
            LeaveRecord(
                employee_id=emp.id,
                leave_type="compensatory",
                start_date=date(2026, 3, 10),
                end_date=date(2026, 3, 10),
                leave_hours=2.0,
                status="approved",
            )
        )
        db_session.commit()

        r = client.post(
            f"/api/academic-terms/{new_t.id}/set-current", headers=admin_headers
        )
        assert r.status_code == 200

        db_session.expire_all()
        new_comp = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "compensatory",
            )
            .first()
        )
        assert new_comp.total_hours == pytest.approx(6.0)

    def test_cross_year_annual_uses_new_term_start_date_as_ref(
        self, client, db_session, admin_headers
    ):
        """特休年資 reference = new.start_date。"""
        old_t = _seed_term(
            db_session,
            school_year=114,
            semester=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115,
            semester=1,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )
        emp = _seed_emp(db_session, hire_date=date(2020, 9, 1))
        db_session.commit()

        client.post(
            f"/api/academic-terms/{new_t.id}/set-current", headers=admin_headers
        )

        db_session.expire_all()
        annual = (
            db_session.query(LeaveQuota)
            .filter(
                LeaveQuota.employee_id == emp.id,
                LeaveQuota.school_year == 115,
                LeaveQuota.leave_type == "annual",
            )
            .first()
        )
        # 2020/9/1 → 2026/8/1 = 5 完整年 → 120 小時
        assert annual.total_hours == 120.0
        assert "2026-08-01" in (annual.note or "")

    def test_set_current_to_same_term_returns_409(
        self, client, db_session, admin_headers
    ):
        t = _seed_term(
            db_session,
            school_year=115,
            semester=1,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )
        t.is_current = True
        db_session.commit()

        r = client.post(
            f"/api/academic-terms/{t.id}/set-current", headers=admin_headers
        )
        assert r.status_code == 409
        assert "已是目前學期" in r.json()["detail"]

    def test_set_current_to_nonexistent_returns_404(self, client, admin_headers):
        r = client.post("/api/academic-terms/99999/set-current", headers=admin_headers)
        assert r.status_code == 404

    def test_handler_raise_rolls_back_entire_transaction(
        self, client, db_session, admin_headers
    ):
        """leave_quota_cutover handler raise → is_current 不變、quota 不建立。

        實作策略：直接 swap _HANDLERS 內 leave_quota_cutover 的 reference 為
        raising stub。@on_term_changed 在 import time 把原 handler 函式 reference
        存進 _HANDLERS list、不靠 lqc.handle 屬性查找，所以 patch.object(lqc, "handle")
        無效（_HANDLERS 仍持有原 function object）；必須直接改 _HANDLERS list
        才能 intercept。
        """
        from utils.term_events import (
            _HANDLERS,
            register_handler,
            reset_handlers_for_tests,
        )

        old_t = _seed_term(
            db_session,
            school_year=114,
            semester=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115,
            semester=1,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )
        _seed_emp(db_session)
        db_session.commit()

        # Positive assertion：boom 真的被呼叫，避免 rollback 是因為 handler
        # 根本沒跑（false negative）
        boom_called = []

        def boom(*, old, new, session):
            boom_called.append(True)
            raise RuntimeError("simulated subscriber failure")

        # Snapshot 原本 handlers 後 swap leave_quota_cutover
        original = list(_HANDLERS)
        assert any(
            n == "leave_quota_cutover" for n, _ in original
        ), "leave_quota_cutover not registered; fixture broken"

        reset_handlers_for_tests()
        for name, fn in original:
            if name == "leave_quota_cutover":
                register_handler(name, boom)
            else:
                register_handler(name, fn)

        try:
            r = client.post(
                f"/api/academic-terms/{new_t.id}/set-current",
                headers=admin_headers,
            )
            # FastAPI 對未捕捉 RuntimeError 預設回 500
            assert r.status_code == 500
            assert boom_called == [True], "boom handler 沒被呼叫 — registry swap 沒生效"
        finally:
            # 必還原 registry，否則污染後續 test
            reset_handlers_for_tests()
            for name, fn in original:
                register_handler(name, fn)

        # is_current 不應變更（rollback 成功的 invariant）
        db_session.expire_all()
        old_after = (
            db_session.query(AcademicTerm).filter(AcademicTerm.id == old_t.id).first()
        )
        new_after = (
            db_session.query(AcademicTerm).filter(AcademicTerm.id == new_t.id).first()
        )
        assert old_after.is_current is True
        assert new_after.is_current is False
        # quota 沒被寫入
        assert db_session.query(LeaveQuota).count() == 0
        # classroom_carry_over 在 boom 前執行；即便有寫入也要被 rollback
        assert (
            db_session.query(Classroom).filter(Classroom.school_year == 115).count()
            == 0
        )

    def test_idempotent_toggle_does_not_double_insert_quotas(
        self, client, db_session, admin_headers
    ):
        """連按兩次同方向跨學年 → quota row 只有一份。

        現實上第二次按會 409（is_current 已是 new_t），但 handler 內 idempotent
        guard 仍須保證即便 raw call 也不會 double-insert。此處用直接 raw call
        leave_quota_cutover.handle 驗證。
        """
        from services.term_subscribers.leave_quota_cutover import handle as lqc_handle

        old_t = _seed_term(
            db_session,
            school_year=114,
            semester=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 7, 31),
        )
        new_t = _seed_term(
            db_session,
            school_year=115,
            semester=1,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )
        _seed_emp(db_session)
        db_session.commit()

        lqc_handle(old=old_t, new=new_t, session=db_session)
        db_session.flush()
        first_count = (
            db_session.query(LeaveQuota).filter(LeaveQuota.school_year == 115).count()
        )
        lqc_handle(old=old_t, new=new_t, session=db_session)
        db_session.flush()
        second_count = (
            db_session.query(LeaveQuota).filter(LeaveQuota.school_year == 115).count()
        )
        assert first_count == second_count == 6

    def test_atypical_jump_113_2_to_115_1_logs_warning_no_op(
        self, client, db_session, admin_headers, caplog
    ):
        """跳級切換 113-2 → 115-1：classroom no-op + warning；quota no-op + info。"""
        import logging

        old_t = _seed_term(
            db_session,
            school_year=113,
            semester=2,
            start_date=date(2025, 2, 1),
            end_date=date(2025, 7, 31),
        )
        old_t.is_current = True
        new_t = _seed_term(
            db_session,
            school_year=115,
            semester=1,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )
        _seed_classroom(db_session, 113, 2)
        db_session.commit()

        with caplog.at_level(logging.WARNING):
            r = client.post(
                f"/api/academic-terms/{new_t.id}/set-current", headers=admin_headers
            )
        assert r.status_code == 200
        # classroom 不被複製
        db_session.expire_all()
        assert (
            db_session.query(Classroom).filter(Classroom.school_year == 115).count()
            == 0
        )
        # quota 不被建立
        assert db_session.query(LeaveQuota).count() == 0
        # 有 warning 出現
        assert any("非典型切換" in r.message for r in caplog.records)

    def test_read_path_prefers_school_year_falls_back_to_year(self, db_session):
        """_resolve_quota_row：school_year row 存在優先、缺則 fallback 西元年。"""
        from api.leaves_quota import _resolve_quota_row

        # 建一筆 is_current term
        t = _seed_term(
            db_session,
            school_year=115,
            semester=1,
            start_date=date(2026, 8, 1),
            end_date=date(2027, 1, 31),
        )
        t.is_current = True
        emp = _seed_emp(db_session)

        # 同時建 school_year=115 row 跟 legacy year=2026 row
        new_row = LeaveQuota(
            employee_id=emp.id,
            year=2026,
            school_year=115,
            leave_type="annual",
            total_hours=120.0,
        )
        legacy_row = LeaveQuota(
            employee_id=emp.id,
            year=2026,
            school_year=None,
            leave_type="annual",
            total_hours=100.0,
        )
        db_session.add_all([new_row, legacy_row])
        db_session.flush()

        found = _resolve_quota_row(db_session, emp.id, "annual")
        assert found.id == new_row.id  # 學年優先

        # 刪掉 school_year row → fallback 西元年
        db_session.delete(new_row)
        db_session.flush()
        fallback = _resolve_quota_row(db_session, emp.id, "annual")
        assert fallback.id == legacy_row.id

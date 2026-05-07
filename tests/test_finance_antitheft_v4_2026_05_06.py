"""tests/test_finance_antitheft_v4_2026_05_06.py — 2026-05-06 金流 A 錢守衛 v4 回歸。

涵蓋 3 條 finding:
- #6: portal /salary-preview 草稿/重算中不回傳薪資細節(對齊 LINE「我的薪資」)
- #7: AttendancePolicyUpdate.festival_bonus_months 加 le=24 上限
- #5: pay_fee_record 本次入帳 delta > FEE_PAYMENT_APPROVAL_THRESHOLD 需 ACTIVITY_PAYMENT_APPROVE

每組固定:
- 違規請求 → 403/422
- 合規請求 → 200
- 設定/狀態未被誤動
"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api import config as config_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.config import router as config_router
from api.fees import FEE_PAYMENT_APPROVAL_THRESHOLD, router as fees_router
from api.portal.salary import router as portal_salary_router
from models.base import Base
from models.classroom import Classroom, Student
from models.database import AttendancePolicy, Employee, SalaryRecord, User
from models.fees import FeeItem, StudentFeeRecord
from utils.auth import hash_password
from utils.permissions import Permission

# ─────────────────────────────────────────────────────────────────────────────
# 共用 fixture
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(tmp_path):
    """整合 auth + config + fees + portal_salary 的測試 app。"""
    db_path = tmp_path / "antitheft_v4.sqlite"
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

    # config update_attendance_policy 會呼叫 _salary_engine.load_config_from_db()
    # 用 mock 注入,避免測試啟動真正的 SalaryEngine
    config_module.init_config_services(salary_engine=MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(config_router)
    app.include_router(fees_router)
    app.include_router(portal_salary_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(
    session,
    *,
    username: str,
    password: str = "Temp123456",
    role: str = "admin",
    permissions: int = 0,
    employee_id: int | None = None,
) -> User:
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _create_employee(session, employee_id="E001", name="測試員工") -> Employee:
    emp = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        hire_date=None,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _login(client: TestClient, username: str, password: str = "Temp123456"):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


def _seed_fee_record(session, *, amount_due, amount_paid=0, status="unpaid"):
    cls = Classroom(name="大班", school_year=2025, semester=1)
    session.add(cls)
    session.flush()
    st = Student(
        student_id="S00001", name="王小明", is_active=True, classroom_id=cls.id
    )
    session.add(st)
    session.flush()
    item = FeeItem(name="學費", amount=amount_due, period="2025-1", is_active=True)
    session.add(item)
    session.flush()
    rec = StudentFeeRecord(
        student_id=st.id,
        student_name=st.name,
        classroom_name=cls.name,
        fee_item_id=item.id,
        fee_item_name=item.name,
        amount_due=amount_due,
        amount_paid=amount_paid,
        status=status,
        period=item.period,
    )
    session.add(rec)
    session.flush()
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# Finding #6: portal salary-preview 草稿/重算中不洩漏薪資細節
# ─────────────────────────────────────────────────────────────────────────────


class TestPortalSalaryDraftLeak:
    """portal /api/portal/salary-preview 對齊 LINE「我的薪資」:
    is_finalized=False 或 needs_recalc=True 時 salary 欄位回傳 None,
    並透過 salary_status 標示狀態(draft/recalc_pending/finalized/none)。
    """

    def _seed_self_employee_user(self, session):
        emp = _create_employee(session, "P001", "教師甲")
        _create_user(
            session,
            username="portal_self",
            role="teacher",
            permissions=0,
            employee_id=emp.id,
        )
        return emp

    def test_draft_salary_does_not_expose_amounts(self, app_client):
        """未封存薪資 → salary=None, salary_status='draft'。"""
        client, session_factory = app_client
        with session_factory() as s:
            emp = self._seed_self_employee_user(s)
            s.add(
                SalaryRecord(
                    employee_id=emp.id,
                    salary_year=2026,
                    salary_month=4,
                    base_salary=30000,
                    gross_salary=35000,
                    net_salary=32000,
                    is_finalized=False,
                    needs_recalc=False,
                )
            )
            s.commit()

        assert _login(client, "portal_self").status_code == 200
        res = client.get(
            "/api/portal/salary-preview", params={"year": 2026, "month": 4}
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["salary"] is None
        assert body["salary_status"] == "draft"
        # attendance_stats 仍應正常回傳(草稿不影響員工查當月考勤統計)
        assert "attendance_stats" in body

    def test_recalc_pending_salary_does_not_expose_amounts(self, app_client):
        """已封存但 needs_recalc=True → 視為待重算,不顯示金額。"""
        client, session_factory = app_client
        with session_factory() as s:
            emp = self._seed_self_employee_user(s)
            s.add(
                SalaryRecord(
                    employee_id=emp.id,
                    salary_year=2026,
                    salary_month=4,
                    base_salary=30000,
                    is_finalized=True,
                    needs_recalc=True,
                )
            )
            s.commit()

        assert _login(client, "portal_self").status_code == 200
        res = client.get(
            "/api/portal/salary-preview", params={"year": 2026, "month": 4}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["salary"] is None
        assert body["salary_status"] == "recalc_pending"

    def test_finalized_salary_returns_full_details(self, app_client):
        """已封存且非 stale → 完整薪資細節。"""
        client, session_factory = app_client
        with session_factory() as s:
            emp = self._seed_self_employee_user(s)
            s.add(
                SalaryRecord(
                    employee_id=emp.id,
                    salary_year=2026,
                    salary_month=4,
                    base_salary=30000,
                    gross_salary=35000,
                    net_salary=32000,
                    is_finalized=True,
                    needs_recalc=False,
                )
            )
            s.commit()

        assert _login(client, "portal_self").status_code == 200
        res = client.get(
            "/api/portal/salary-preview", params={"year": 2026, "month": 4}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["salary_status"] == "finalized"
        assert body["salary"] is not None
        assert body["salary"]["base_salary"] == 30000
        assert body["salary"]["gross_salary"] == 35000
        assert body["salary"]["net_salary"] == 32000

    def test_no_record_returns_none_status(self, app_client):
        """沒有 SalaryRecord → salary=None, salary_status='none'。"""
        client, session_factory = app_client
        with session_factory() as s:
            self._seed_self_employee_user(s)
            s.commit()

        assert _login(client, "portal_self").status_code == 200
        res = client.get(
            "/api/portal/salary-preview", params={"year": 2026, "month": 4}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["salary"] is None
        assert body["salary_status"] == "none"


# ─────────────────────────────────────────────────────────────────────────────
# Finding #7: AttendancePolicyUpdate.festival_bonus_months 上限 24
# ─────────────────────────────────────────────────────────────────────────────


class TestFestivalBonusMonthsCap:
    def test_above_cap_rejected(self, app_client):
        """festival_bonus_months > 24 → 422。"""
        client, session_factory = app_client
        with session_factory() as s:
            _create_user(
                session=s,
                username="cfg_admin",
                role="admin",
                permissions=Permission.SETTINGS_READ | Permission.SETTINGS_WRITE,
            )
            s.commit()

        assert _login(client, "cfg_admin").status_code == 200
        res = client.put(
            "/api/config/attendance-policy",
            json={"festival_bonus_months": 25},
        )
        assert res.status_code == 422

        # 設定不應被建立(無新版 AttendancePolicy)
        with session_factory() as s:
            assert s.query(AttendancePolicy).count() == 0

    def test_at_cap_accepted(self, app_client):
        """festival_bonus_months = 24 → 200。"""
        client, session_factory = app_client
        with session_factory() as s:
            _create_user(
                session=s,
                username="cfg_admin",
                role="admin",
                permissions=Permission.SETTINGS_READ | Permission.SETTINGS_WRITE,
            )
            s.commit()

        assert _login(client, "cfg_admin").status_code == 200
        res = client.put(
            "/api/config/attendance-policy",
            json={"festival_bonus_months": 24},
        )
        assert res.status_code == 200, res.text

    def test_zero_still_accepted(self, app_client):
        """0 仍合法(下限 ge=0 不變)。"""
        client, session_factory = app_client
        with session_factory() as s:
            _create_user(
                session=s,
                username="cfg_admin",
                role="admin",
                permissions=Permission.SETTINGS_READ | Permission.SETTINGS_WRITE,
            )
            s.commit()

        assert _login(client, "cfg_admin").status_code == 200
        res = client.put(
            "/api/config/attendance-policy",
            json={"festival_bonus_months": 0},
        )
        assert res.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# Finding #5: pay_fee_record 本次 delta 大額需 ACTIVITY_PAYMENT_APPROVE
# ─────────────────────────────────────────────────────────────────────────────


class TestFeePaymentLargeApproval:
    def test_small_delta_passes_without_approver(self, app_client):
        """delta = 20,000 (< 50,000) 無 ACTIVITY_PAYMENT_APPROVE → 200。
        確保日常月費收款流程不被簽核擋住。
        """
        client, session_factory = app_client
        with session_factory() as s:
            _create_user(
                session=s,
                username="fee_clerk",
                role="admin",
                permissions=Permission.FEES_READ | Permission.FEES_WRITE,
            )
            rec = _seed_fee_record(s, amount_due=200_000)
            s.commit()
            rec_id = rec.id

        assert _login(client, "fee_clerk").status_code == 200
        res = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 20_000,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 200, res.text
        with session_factory() as s:
            r = s.query(StudentFeeRecord).filter_by(id=rec_id).one()
            assert r.amount_paid == 20_000

    def test_large_delta_blocked_without_approver(self, app_client):
        """delta = 60,000 (> 50,000) 無 ACTIVITY_PAYMENT_APPROVE → 403。
        DB 不應被改動。
        """
        client, session_factory = app_client
        with session_factory() as s:
            _create_user(
                session=s,
                username="fee_clerk",
                role="admin",
                permissions=Permission.FEES_READ | Permission.FEES_WRITE,
            )
            rec = _seed_fee_record(s, amount_due=200_000)
            s.commit()
            rec_id = rec.id

        assert _login(client, "fee_clerk").status_code == 200
        res = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 60_000,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 403
        assert (
            "金流簽核" in res.json()["detail"]
            or "ACTIVITY_PAYMENT_APPROVE" in res.json()["detail"]
        )

        with session_factory() as s:
            r = s.query(StudentFeeRecord).filter_by(id=rec_id).one()
            assert r.amount_paid == 0
            assert r.status == "unpaid"

    def test_large_delta_passes_with_approver(self, app_client):
        """delta = 60,000 持 ACTIVITY_PAYMENT_APPROVE → 200。"""
        client, session_factory = app_client
        with session_factory() as s:
            _create_user(
                session=s,
                username="fee_boss",
                role="admin",
                permissions=(
                    Permission.FEES_READ
                    | Permission.FEES_WRITE
                    | Permission.ACTIVITY_PAYMENT_APPROVE
                ),
            )
            rec = _seed_fee_record(s, amount_due=200_000)
            s.commit()
            rec_id = rec.id

        assert _login(client, "fee_boss").status_code == 200
        res = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 60_000,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 200, res.text
        with session_factory() as s:
            r = s.query(StudentFeeRecord).filter_by(id=rec_id).one()
            assert r.amount_paid == 60_000
            assert r.status == "partial"

    def test_threshold_constant_is_50k(self):
        """確保常數值未被誤改;若調整需同步更新本測試。"""
        assert FEE_PAYMENT_APPROVAL_THRESHOLD == 50_000

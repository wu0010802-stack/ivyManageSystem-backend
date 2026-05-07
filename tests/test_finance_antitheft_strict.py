"""tests/test_finance_antitheft_strict.py — 拆單/拆欄/未來日繞過守衛回歸（2026-04-27）。

涵蓋本次「最嚴格」收緊：
- #1 手動調薪：本次所有欄位 |delta| 合計 > 閾值需簽核（拆欄繞過）
- #2 學費 PayRequest.payment_date：禁未來日 + 30 天回補上限
- #3 學費退款：以「同 record 累積已退 + 本次」對門檻（拆次繞過）
- #4 活動退費：以「同 registration 未作廢退費 + 本次」對門檻（拆次繞過）
- #5 force 封存薪資：未填 force_reason → 422；無 ACTIVITY_PAYMENT_APPROVE → 403
"""

import os
import sys
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.fees import router as fees_router
from api.salary import router as salary_router
from models.base import Base
from models.classroom import Classroom, Student
from models.database import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    Employee,
    RegistrationCourse,
    SalaryRecord,
    User,
)
from models.fees import FeeItem, StudentFeeRecord, StudentFeeRefund
from utils.auth import hash_password
from utils.permissions import Permission

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def strict_client(tmp_path):
    db_path = tmp_path / "antitheft_strict.sqlite"
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

    # 清空 activity 模組 limiter 計數，避免跨測試污染
    from api.activity import public as public_mod
    from api.activity import registrations as reg_mod
    from api.activity import pos as pos_mod

    for mod in (public_mod, reg_mod, pos_mod):
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if hasattr(obj, "_timestamps"):
                obj._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)
    app.include_router(fees_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_user(
    session, *, username, permissions, employee_id=None, role="admin"
) -> User:
    user = User(
        username=username,
        password_hash=hash_password("Temp123456"),
        role=role,
        permissions=permissions,
        employee_id=employee_id,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


# ══════════════════════════════════════════════════════════════════════
# #1 手動調薪：本次所有欄位 |delta| 合計門檻
# ══════════════════════════════════════════════════════════════════════


def _seed_salary_record(session, *, base_salary=30000) -> SalaryRecord:
    emp = Employee(
        employee_id="E_strict",
        name="調薪測試",
        base_salary=base_salary,
        employee_type="regular",
        is_active=True,
    )
    session.add(emp)
    session.flush()
    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=4,
        base_salary=base_salary,
        gross_salary=base_salary,
        total_deduction=0,
        net_salary=base_salary,
        is_finalized=False,
    )
    session.add(rec)
    session.flush()
    return rec


class TestManualAdjustCumulativeDelta:
    def test_split_fields_each_under_threshold_blocked_by_sum(self, strict_client):
        """同筆請求調 4 個欄位各 999（單欄位都未過門檻），合計 3996 應被擋下。"""
        client, sf = strict_client
        with sf() as s:
            rec = _seed_salary_record(s)
            _make_user(
                s,
                username="hr_writer",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
                employee_id=None,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "hr_writer").status_code == 200
        res = client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={
                "adjustment_reason": "拆欄繞過測試",
                "performance_bonus": 999,
                "special_bonus": 999,
                "supervisor_dividend": 999,
                "meeting_overtime_pay": 999,
            },
        )
        assert res.status_code == 403, res.text
        assert "審批閾值" in res.json()["detail"]

    def test_split_fields_total_under_threshold_allowed(self, strict_client):
        """合計 ≤ 1000 仍允許（不影響合法小額調整）。"""
        client, sf = strict_client
        with sf() as s:
            rec = _seed_salary_record(s)
            _make_user(
                s,
                username="hr_writer",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
                employee_id=None,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "hr_writer").status_code == 200
        res = client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={
                "adjustment_reason": "小額多欄微調",
                "performance_bonus": 300,
                "special_bonus": 300,
                "supervisor_dividend": 300,  # 合計 900
            },
        )
        assert res.status_code == 200, res.text


# ══════════════════════════════════════════════════════════════════════
# #2 學費 PayRequest.payment_date 守衛（禁未來日 + 30 天回補上限）
# ══════════════════════════════════════════════════════════════════════


def _seed_fee_record(session, *, amount_due=5000, amount_paid=0) -> StudentFeeRecord:
    cls = Classroom(name="日期測試班", school_year=2025, semester=1)
    session.add(cls)
    session.flush()
    st = Student(
        student_id="S_DATE",
        name="陳小華",
        is_active=True,
        classroom_id=cls.id,
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
        status="unpaid" if amount_paid == 0 else "paid",
        period=item.period,
    )
    session.add(rec)
    session.flush()
    return rec


class TestFeePaymentDateGuard:
    def test_future_payment_date_rejected(self, strict_client):
        """payment_date 為未來日 → 422（Pydantic validator）。"""
        client, sf = strict_client
        with sf() as s:
            rec = _seed_fee_record(s)
            _make_user(
                s,
                username="fees_user",
                permissions=Permission.FEES_READ | Permission.FEES_WRITE,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "fees_user").status_code == 200
        res = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": (date.today() + timedelta(days=1)).isoformat(),
                "amount_paid": 500,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 422, res.text

    def test_too_old_payment_date_rejected(self, strict_client):
        """payment_date 超過 90 天回補上限 → 422（學費跨月分期合法，故較活動寬鬆）。"""
        client, sf = strict_client
        with sf() as s:
            rec = _seed_fee_record(s)
            _make_user(
                s,
                username="fees_user",
                permissions=Permission.FEES_READ | Permission.FEES_WRITE,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "fees_user").status_code == 200
        res = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": (date.today() - timedelta(days=120)).isoformat(),
                "amount_paid": 500,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 422, res.text

    def test_today_payment_date_allowed(self, strict_client):
        """payment_date 為今天 → 通過。"""
        client, sf = strict_client
        with sf() as s:
            rec = _seed_fee_record(s)
            _make_user(
                s,
                username="fees_user",
                permissions=Permission.FEES_READ | Permission.FEES_WRITE,
            )
            s.commit()
            rec_id = rec.id

        assert _login(client, "fees_user").status_code == 200
        res = client.put(
            f"/api/fees/records/{rec_id}/pay",
            json={
                "payment_date": date.today().isoformat(),
                "amount_paid": 500,
                "payment_method": "現金",
            },
        )
        assert res.status_code == 200, res.text


# ══════════════════════════════════════════════════════════════════════
# #3 學費退款累積簽核
# ══════════════════════════════════════════════════════════════════════


class TestFeeRefundCumulative:
    def test_third_small_refund_pushed_over_threshold_blocked(self, strict_client):
        """第三筆 NT$500 讓累積跨 1000 閾值 → 應被擋下要求 ACTIVITY_PAYMENT_APPROVE。"""
        client, sf = strict_client
        with sf() as s:
            rec = _seed_fee_record(s, amount_due=5000, amount_paid=5000)
            _make_user(
                s,
                username="fees_writer",
                permissions=Permission.FEES_READ | Permission.FEES_WRITE,
            )
            # 預先放兩筆累積 1000 的退款（門檻含等號方向：> 1000 才擋）
            s.add(
                StudentFeeRefund(
                    record_id=rec.id,
                    amount=500,
                    reason="第一筆已退",
                    notes="",
                    refunded_by="prev_op",
                )
            )
            s.add(
                StudentFeeRefund(
                    record_id=rec.id,
                    amount=500,
                    reason="第二筆已退",
                    notes="",
                    refunded_by="prev_op",
                )
            )
            # 已退款後同步把 record.amount_paid 扣回（與 handler 行為一致）
            rec.amount_paid = 4000
            s.commit()
            rec_id = rec.id

        assert _login(client, "fees_writer").status_code == 200
        res = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 500, "reason": "第三筆讓累積超 1000 要簽核"},
        )
        # 累積 1500 > 1000，且 user 無 ACTIVITY_PAYMENT_APPROVE
        assert res.status_code == 403, res.text
        assert "審批閾值" in res.json()["detail"]

    def test_cumulative_with_approve_permission_allowed(self, strict_client):
        """具 ACTIVITY_PAYMENT_APPROVE 者可在累積跨閾值後仍執行。"""
        client, sf = strict_client
        with sf() as s:
            rec = _seed_fee_record(s, amount_due=5000, amount_paid=5000)
            _make_user(
                s,
                username="finance_boss",
                permissions=Permission.FEES_READ
                | Permission.FEES_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            s.add(
                StudentFeeRefund(
                    record_id=rec.id,
                    amount=900,
                    reason="先前已退一筆",
                    notes="",
                    refunded_by="prev_op",
                )
            )
            rec.amount_paid = 4100
            s.commit()
            rec_id = rec.id

        assert _login(client, "finance_boss").status_code == 200
        res = client.post(
            f"/api/fees/records/{rec_id}/refund",
            json={"amount": 200, "reason": "簽核後追加退款"},
        )
        assert res.status_code == 201, res.text


# ══════════════════════════════════════════════════════════════════════
# #4 活動退費累積簽核
# ══════════════════════════════════════════════════════════════════════


def _seed_activity_registration(
    session, *, paid_amount=2000, course_price=2000
) -> ActivityRegistration:
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = ActivityCourse(
        name="活動拆退測試",
        price=course_price,
        capacity=30,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name="林小芬",
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=paid_amount,
        is_active=True,
        match_status="matched",
        school_year=sy,
        semester=sem,
    )
    session.add(reg)
    session.flush()
    session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="enrolled",
            price_snapshot=course_price,
        )
    )
    session.flush()
    return reg


class TestActivityRefundCumulative:
    def test_split_refunds_blocked_when_cumulative_over_threshold(self, strict_client):
        """活動已存兩筆 500 退費（type=refund），第三筆 200 讓累積 = 1200 > 1000 → 403。"""
        client, sf = strict_client
        with sf() as s:
            reg = _seed_activity_registration(s, paid_amount=2000)
            _make_user(
                s,
                username="act_writer",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            for _ in range(2):
                s.add(
                    ActivityPaymentRecord(
                        registration_id=reg.id,
                        type="refund",
                        amount=500,
                        payment_date=date.today(),
                        payment_method="現金",
                        notes="先前退費",
                        operator="prev_op",
                    )
                )
            reg.paid_amount = 1000
            s.commit()
            reg_id = reg.id

        assert _login(client, "act_writer").status_code == 200
        res = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "refund",
                "amount": 200,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "notes": "第三筆讓累積過 1000 應被擋下",
            },
        )
        assert res.status_code == 403, res.text
        assert "審批閾值" in res.json()["detail"]

    def test_voided_refunds_excluded_from_cumulative(self, strict_client):
        """已 voided 的退費不計入累積（避免封過刪過的歷史誤殺合法後續操作）。"""
        client, sf = strict_client
        with sf() as s:
            reg = _seed_activity_registration(s, paid_amount=5000)
            _make_user(
                s,
                username="act_writer",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            from datetime import datetime as _dt

            # 一筆已作廢的 2000 退費 + 一筆有效 500 退費；本次再退 400 → 累積 = 500+400 = 900 ≤ 1000
            s.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="refund",
                    amount=2000,
                    payment_date=date.today(),
                    payment_method="現金",
                    notes="此筆已作廢",
                    operator="prev_op",
                    voided_at=_dt.now(),
                    voided_by="admin",
                    void_reason="誤刷",
                )
            )
            s.add(
                ActivityPaymentRecord(
                    registration_id=reg.id,
                    type="refund",
                    amount=500,
                    payment_date=date.today(),
                    payment_method="現金",
                    notes="有效退費",
                    operator="prev_op",
                )
            )
            reg.paid_amount = 4500
            s.commit()
            reg_id = reg.id

        assert _login(client, "act_writer").status_code == 200
        res = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "refund",
                "amount": 400,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "notes": "累積 900 未過閾值之單筆退費",
            },
        )
        assert res.status_code in (200, 201), res.text


# ══════════════════════════════════════════════════════════════════════
# #5 force 封存薪資：必填原因 + 必須具備 ACTIVITY_PAYMENT_APPROVE
# ══════════════════════════════════════════════════════════════════════


def _seed_two_employees_one_with_record(session) -> int:
    """造一個「有薪資」員工 + 一個「在職但無薪資」員工 → finalize 預設應 409，force=True 才能過。"""
    have = Employee(
        employee_id="E_have",
        name="有薪資",
        base_salary=30000,
        employee_type="regular",
        is_active=True,
        hire_date=date(2020, 1, 1),
    )
    miss = Employee(
        employee_id="E_miss",
        name="缺薪資",
        base_salary=28000,
        employee_type="regular",
        is_active=True,
        hire_date=date(2020, 1, 1),
    )
    session.add_all([have, miss])
    session.flush()
    rec = SalaryRecord(
        employee_id=have.id,
        salary_year=2026,
        salary_month=4,
        base_salary=30000,
        gross_salary=30000,
        total_deduction=0,
        net_salary=30000,
        is_finalized=False,
    )
    session.add(rec)
    session.flush()
    return rec.id


class TestForceFinalizeRequiresApprove:
    def test_force_without_reason_422(self, strict_client):
        """force=True 但未帶 force_reason → Pydantic 422。"""
        client, sf = strict_client
        with sf() as s:
            _seed_two_employees_one_with_record(s)
            _make_user(
                s,
                username="finance_boss",
                permissions=Permission.SALARY_READ
                | Permission.SALARY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            s.commit()

        assert _login(client, "finance_boss").status_code == 200
        res = client.post(
            "/api/salaries/finalize-month",
            json={"year": 2026, "month": 4, "force": True},
        )
        assert res.status_code == 422, res.text

    def test_force_short_reason_422(self, strict_client):
        """force_reason 少於 10 字 → 422。"""
        client, sf = strict_client
        with sf() as s:
            _seed_two_employees_one_with_record(s)
            _make_user(
                s,
                username="finance_boss",
                permissions=Permission.SALARY_READ
                | Permission.SALARY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            s.commit()

        assert _login(client, "finance_boss").status_code == 200
        res = client.post(
            "/api/salaries/finalize-month",
            json={
                "year": 2026,
                "month": 4,
                "force": True,
                "force_reason": "太短",
            },
        )
        assert res.status_code == 422, res.text

    def test_force_without_approve_permission_403(self, strict_client):
        """force=True + 帶 reason，但無 ACTIVITY_PAYMENT_APPROVE → 403。"""
        client, sf = strict_client
        with sf() as s:
            _seed_two_employees_one_with_record(s)
            _make_user(
                s,
                username="hr_only",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
            )
            s.commit()

        assert _login(client, "hr_only").status_code == 200
        res = client.post(
            "/api/salaries/finalize-month",
            json={
                "year": 2026,
                "month": 4,
                "force": True,
                "force_reason": "已與會計確認漏發風險可接受，先封存以結帳",
            },
        )
        assert res.status_code == 403, res.text
        assert "金流簽核" in res.json()["detail"]

    def test_force_with_reason_and_approve_succeeds_and_records_skipped(
        self, strict_client
    ):
        """force=True + reason + ACTIVITY_PAYMENT_APPROVE → 200 並回傳被略過清單。"""
        client, sf = strict_client
        with sf() as s:
            rec_id = _seed_two_employees_one_with_record(s)
            _make_user(
                s,
                username="finance_boss",
                permissions=Permission.SALARY_READ
                | Permission.SALARY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            s.commit()

        assert _login(client, "finance_boss").status_code == 200
        res = client.post(
            "/api/salaries/finalize-month",
            json={
                "year": 2026,
                "month": 4,
                "force": True,
                "force_reason": "已與會計確認漏發風險可接受，先封存以結帳",
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["force"] is True
        # 「缺薪資」員工應出現在 skipped_missing
        skipped_names = {m["name"] for m in body["skipped_missing"]}
        assert "缺薪資" in skipped_names

        # remark 應有 FORCE 痕跡（含 force_reason 與略過清單）
        with sf() as s:
            rec = s.query(SalaryRecord).filter(SalaryRecord.id == rec_id).first()
            assert rec is not None
            assert "FORCE 封存" in (rec.remark or "")
            assert "已與會計確認漏發風險可接受" in (rec.remark or "")
            assert "缺薪資" in (rec.remark or "")

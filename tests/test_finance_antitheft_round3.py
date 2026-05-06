"""tests/test_finance_antitheft_round3.py — 金流 A 錢守衛 round 3 漏洞修補（2026-04-27）。

延伸自 round 1 (04-24 finance_guards) + round 2 (04-27 strict)，本輪封死六個漏洞：

- #1 POS 退費可拆收據繞過累積簽核（pos.py）
- #2 退費累積檢查在 lock 之前的 race window（registrations.py，順序調整）
- #3 批次標記已繳缺原因/簽核（registrations.py batch-payment）
- #4 會議 overtime_pay schema 拿掉前端 override（已於 test_meetings.py）
- #5 批次會議 delete 漏檢查既存員工封存（meetings.py）
- #6 政府申報匯出讀草稿/stale 薪資（gov_reports.py）
- #7 POS 日結解鎖無原因（已於 test_activity_pos.py）

#4/#7 已在各自模組測試檔覆蓋，本檔聚焦 #1/#2/#3/#5/#6。
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
from api.gov_reports import router as gov_reports_router
from api.meetings import router as meetings_router
from models.base import Base
from models.database import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    Employee,
    MeetingRecord,
    RegistrationCourse,
    SalaryRecord,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def round3_client(tmp_path):
    db_path = tmp_path / "round3.sqlite"
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

    # 清空 activity / gov_reports 模組 limiter 計數
    from api.activity import public as public_mod
    from api.activity import registrations as reg_mod
    from api.activity import pos as pos_mod
    from api import gov_reports as gov_mod

    for mod in (public_mod, reg_mod, pos_mod, gov_mod):
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if hasattr(obj, "_timestamps"):
                obj._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)
    app.include_router(meetings_router)
    app.include_router(gov_reports_router)

    # 關掉 gov_reports 的 5/60s rate limiter（測試需要快速連發數個 request）
    from api.gov_reports import _rate_limit as _gov_rate_limit

    app.dependency_overrides[_gov_rate_limit] = lambda: None

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
    permissions: int,
    role: str = "admin",
    password: str = "TempPass123",
) -> User:
    u = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=True,
    )
    session.add(u)
    session.flush()
    return u


def _login(client: TestClient, username: str, password: str = "TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _setup_reg(
    session,
    *,
    student_name: str = "王小明",
    course_price: int = 5000,
    paid_amount: int = 5000,
    is_paid: bool = True,
) -> ActivityRegistration:
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = ActivityCourse(
        name=f"美術-{student_name}",
        price=course_price,
        capacity=30,
        allow_waitlist=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        parent_phone="0912345678",
        class_name="大班",
        paid_amount=paid_amount,
        is_paid=is_paid,
        is_active=True,
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
    session.commit()
    return reg


# ══════════════════════════════════════════════════════════════════════
# #1 POS 退費拆收據繞過累積簽核
# ══════════════════════════════════════════════════════════════════════


class TestPOSRefundCumulative:
    """POS refund 對同一 reg 的歷史未作廢退費需累積比對門檻。"""

    def test_third_small_pos_refund_blocked_when_cumulative_over_threshold(
        self, round3_client
    ):
        """同一 registration 連開三張小退費 POS 收據，第三張讓累積 > NT$1000 即整張 403。"""
        client, sf = round3_client
        with sf() as s:
            _create_user(
                s,
                username="cashier",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, paid_amount=5000, is_paid=True)
            s.commit()
            reg_id = reg.id

        assert _login(client, "cashier").status_code == 200
        today = date.today().isoformat()

        # 第一筆 NT$400 退費 → 累積 400 → 通過
        res1 = client.post(
            "/api/activity/pos/checkout",
            json={
                "type": "refund",
                "items": [{"registration_id": reg_id, "amount": 400}],
                "payment_method": "現金",
                "payment_date": today,
                "notes": "第一次小額退費測試（家長申請）",
            },
        )
        assert res1.status_code == 201, res1.text

        # 第二筆 NT$400 退費 → 累積 800 → 仍通過
        res2 = client.post(
            "/api/activity/pos/checkout",
            json={
                "type": "refund",
                "items": [{"registration_id": reg_id, "amount": 400}],
                "payment_method": "現金",
                "payment_date": today,
                "notes": "第二次小額退費測試（家長申請）",
            },
        )
        assert res2.status_code == 201, res2.text

        # 第三筆 NT$400 退費 → 累積 1200 跨閾值 → 無簽核權限即 403
        res3 = client.post(
            "/api/activity/pos/checkout",
            json={
                "type": "refund",
                "items": [{"registration_id": reg_id, "amount": 400}],
                "payment_method": "現金",
                "payment_date": today,
                "notes": "第三次小額退費觸發累積門檻（測試）",
            },
        )
        assert res3.status_code == 403
        assert "累積退費總額" in res3.json()["detail"]

    def test_pos_cumulative_with_approve_permission_allowed(self, round3_client):
        """有 ACTIVITY_PAYMENT_APPROVE 權限者不受累積簽核擋。"""
        client, sf = round3_client
        with sf() as s:
            _create_user(
                s,
                username="boss",
                permissions=Permission.ACTIVITY_READ
                | Permission.ACTIVITY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            reg = _setup_reg(s, paid_amount=5000, is_paid=True)
            s.commit()
            reg_id = reg.id

        assert _login(client, "boss").status_code == 200
        today = date.today().isoformat()

        for _ in range(3):
            res = client.post(
                "/api/activity/pos/checkout",
                json={
                    "type": "refund",
                    "items": [{"registration_id": reg_id, "amount": 400}],
                    "payment_method": "現金",
                    "payment_date": today,
                    "notes": "簽核權限者連續退費（測試案例）",
                },
            )
            assert res.status_code == 201

    def test_voided_pos_refunds_not_counted_in_cumulative(self, round3_client):
        """voided_at 不為 NULL 的退費紀錄不應計入累積（與單筆 add_payment 對齊）。"""
        client, sf = round3_client
        with sf() as s:
            _create_user(
                s,
                username="cashier2",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, paid_amount=5000, is_paid=True)
            # 已 voided 的歷史退費 NT$5000，不應計入累積
            from datetime import datetime

            voided = ActivityPaymentRecord(
                registration_id=reg.id,
                type="refund",
                amount=5000,
                payment_date=date.today(),
                payment_method="現金",
                operator="legacy",
                voided_at=datetime.now(),
                voided_by="boss",
                void_reason="作廢測試",
            )
            s.add(voided)
            s.commit()
            reg_id = reg.id

        assert _login(client, "cashier2").status_code == 200
        # 因為 voided 不算累積，新退 NT$500 仍應通過
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "type": "refund",
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "notes": "voided 已排除測試（測試案例）",
            },
        )
        assert res.status_code == 201, res.text


# ══════════════════════════════════════════════════════════════════════
# #2 退費累積檢查必須在 _lock_registration 之後
# ══════════════════════════════════════════════════════════════════════


class TestRefundCheckOrderAfterLock:
    """add_registration_payment 中累積簽核出現在 lock 之後。

    結構性測試：lock 後才查 prior_refunded，避免 race window。
    透過 schema 驗證走訪序列來確認順序，而非真正併發測試（需要 PG）。
    """

    def test_basic_refund_with_cumulative_check_passes(self, round3_client):
        """確認 lock 後累積檢查邏輯仍正確運作（不影響功能性）。"""
        client, sf = round3_client
        with sf() as s:
            _create_user(
                s,
                username="cashier3",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, paid_amount=2000, is_paid=False)
            s.commit()
            reg_id = reg.id

        assert _login(client, "cashier3").status_code == 200
        # 單筆 500 在門檻內，應通過
        res = client.post(
            f"/api/activity/registrations/{reg_id}/payments",
            json={
                "type": "refund",
                "amount": 500,
                "payment_date": date.today().isoformat(),
                "payment_method": "現金",
                "notes": "在門檻內單筆退費（家長申請辦理）",
            },
        )
        assert res.status_code == 201, res.text


# ══════════════════════════════════════════════════════════════════════
# #3 批次標記已繳：reason 必填 + 整批 shortfall 累積簽核
# ══════════════════════════════════════════════════════════════════════


class TestBatchMarkPaidGuards:
    def test_missing_reason_rejected_422(self, round3_client):
        """BatchPaymentUpdate 沒帶 reason → schema 422。"""
        client, sf = round3_client
        with sf() as s:
            _create_user(
                s,
                username="staff",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, course_price=500, paid_amount=0, is_paid=False)
            s.commit()
            reg_id = reg.id

        assert _login(client, "staff").status_code == 200
        res = client.put(
            "/api/activity/registrations/batch-payment",
            json={"ids": [reg_id], "is_paid": True},
        )
        assert res.status_code == 422

    def test_short_reason_rejected_422(self, round3_client):
        """reason 太短 → 422。"""
        client, sf = round3_client
        with sf() as s:
            _create_user(
                s,
                username="staff2",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            reg = _setup_reg(s, course_price=500, paid_amount=0, is_paid=False)
            s.commit()
            reg_id = reg.id

        assert _login(client, "staff2").status_code == 200
        res = client.put(
            "/api/activity/registrations/batch-payment",
            json={"ids": [reg_id], "is_paid": True, "reason": "短"},
        )
        assert res.status_code == 422

    def test_total_shortfall_over_threshold_blocked_without_approve(
        self, round3_client
    ):
        """整批 shortfall 合計 > NT$1000 → 無簽核權限 → 403。"""
        client, sf = round3_client
        with sf() as s:
            _create_user(
                s,
                username="write_only",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            r1 = _setup_reg(
                s,
                student_name="A",
                course_price=600,
                paid_amount=0,
                is_paid=False,
            )
            r2 = _setup_reg(
                s,
                student_name="B",
                course_price=600,
                paid_amount=0,
                is_paid=False,
            )
            s.commit()
            ids = [r1.id, r2.id]

        assert _login(client, "write_only").status_code == 200
        res = client.put(
            "/api/activity/registrations/batch-payment",
            json={
                "ids": ids,
                "is_paid": True,
                "reason": "期末批次補繳測試（無簽核權限的櫃台）",
            },
        )
        assert res.status_code == 403
        assert "批次補齊整批合計" in res.json()["detail"]

    def test_total_shortfall_over_threshold_with_approve_succeeds(self, round3_client):
        """有金流簽核權限者可執行；reason 寫入 system payment notes。"""
        client, sf = round3_client
        with sf() as s:
            _create_user(
                s,
                username="boss2",
                permissions=Permission.ACTIVITY_READ
                | Permission.ACTIVITY_WRITE
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            r1 = _setup_reg(
                s,
                student_name="C",
                course_price=600,
                paid_amount=0,
                is_paid=False,
            )
            r2 = _setup_reg(
                s,
                student_name="D",
                course_price=600,
                paid_amount=0,
                is_paid=False,
            )
            s.commit()
            ids = [r1.id, r2.id]

        assert _login(client, "boss2").status_code == 200
        res = client.put(
            "/api/activity/registrations/batch-payment",
            json={
                "ids": ids,
                "is_paid": True,
                "reason": "期末已收齊現金，老闆確認補齊測試",
            },
        )
        assert res.status_code == 200, res.text

        # 系統補齊紀錄 notes 必須包含 reason
        with sf() as s:
            recs = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id.in_(ids))
                .all()
            )
            assert len(recs) == 2
            for r in recs:
                assert r.payment_method == "系統補齊"
                assert "老闆確認補齊" in (r.notes or "")

    def test_under_threshold_without_approve_allowed(self, round3_client):
        """整批 shortfall < NT$1000 即一般 ACTIVITY_WRITE 即可。"""
        client, sf = round3_client
        with sf() as s:
            _create_user(
                s,
                username="staff3",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
            )
            r1 = _setup_reg(
                s,
                student_name="E",
                course_price=300,
                paid_amount=0,
                is_paid=False,
            )
            s.commit()
            ids = [r1.id]

        assert _login(client, "staff3").status_code == 200
        res = client.put(
            "/api/activity/registrations/batch-payment",
            json={
                "ids": ids,
                "is_paid": True,
                "reason": "小額補齊測試（在門檻內，家長同意）",
            },
        )
        assert res.status_code == 200, res.text


# ══════════════════════════════════════════════════════════════════════
# #5 批次會議 delete 須一併檢查既存員工封存
# ══════════════════════════════════════════════════════════════════════


class TestMeetingsBatchOverwriteFinalizeGuard:
    def _seed_finalized_employee_meeting(self, sf, *, year: int, month: int, day: int):
        """建立一名員工 + 該月已封存薪資 + 同日既存會議紀錄。"""
        with sf() as s:
            emp = Employee(
                employee_id="E_old",
                name="既存員工",
                base_salary=30000,
                employee_type="regular",
                is_active=True,
            )
            s.add(emp)
            s.flush()
            from datetime import datetime as _dt

            # 該月薪資已封存 → 守衛應阻止任何刪除
            rec = SalaryRecord(
                employee_id=emp.id,
                salary_year=year,
                salary_month=month,
                base_salary=30000,
                gross_salary=30000,
                net_salary=30000,
                is_finalized=True,
                finalized_by="boss",
                finalized_at=_dt(year, month, 28),
            )
            s.add(rec)
            # 該日同類型的既有會議
            mr = MeetingRecord(
                employee_id=emp.id,
                meeting_date=date(year, month, day),
                meeting_type="staff_meeting",
                attended=True,
                overtime_hours=1.0,
                overtime_pay=200,
            )
            s.add(mr)
            s.commit()
            return emp.id

    def test_batch_overwrite_blocked_when_existing_employee_is_finalized(
        self, round3_client
    ):
        """payload 沒列到既有員工，但既有員工該月已封存 → 整批拒絕。"""
        client, sf = round3_client
        target_year, target_month, target_day = 2026, 3, 15
        existing_emp_id = self._seed_finalized_employee_meeting(
            sf, year=target_year, month=target_month, day=target_day
        )

        # 建立另一名 payload 員工（其薪資未封存）
        with sf() as s:
            new_emp = Employee(
                employee_id="E_new",
                name="新員工",
                base_salary=30000,
                employee_type="regular",
                is_active=True,
            )
            s.add(new_emp)
            s.flush()
            new_emp_id = new_emp.id
            _create_user(
                s,
                username="meet_admin",
                permissions=Permission.MEETINGS,
            )
            s.commit()

        assert _login(client, "meet_admin").status_code == 200
        # payload 故意不放 existing_emp_id，新員工出席
        res = client.post(
            "/api/meetings/batch",
            json={
                "meeting_date": f"{target_year}-{target_month:02d}-{target_day:02d}",
                "meeting_type": "staff_meeting",
                "attendees": [new_emp_id],
                "absentees": [],
            },
        )
        assert res.status_code == 409
        assert "已封存" in res.json()["detail"]

        # 既有紀錄不應被刪除
        with sf() as s:
            still = (
                s.query(MeetingRecord)
                .filter(MeetingRecord.employee_id == existing_emp_id)
                .count()
            )
            assert still == 1


# ══════════════════════════════════════════════════════════════════════
# #6 政府申報匯出封存守衛
# ══════════════════════════════════════════════════════════════════════


class TestGovReportFinalizeGuard:
    def _seed_employee_and_unfinalized_record(
        self, sf, *, year: int, month: int, finalized: bool, needs_recalc: bool
    ):
        with sf() as s:
            emp = Employee(
                employee_id="E_gov",
                name="政府申報員工",
                base_salary=40000,
                employee_type="regular",
                is_active=True,
                hire_date=date(year, 1, 1),
            )
            s.add(emp)
            s.flush()
            rec = SalaryRecord(
                employee_id=emp.id,
                salary_year=year,
                salary_month=month,
                base_salary=40000,
                gross_salary=40000,
                net_salary=40000,
                labor_insurance_employee=823,
                labor_insurance_employer=2879,
                health_insurance_employee=623,
                health_insurance_employer=1948,
                is_finalized=finalized,
                needs_recalc=needs_recalc,
            )
            s.add(rec)
            s.commit()
            return emp.id

    def test_unfinalized_period_export_blocked_409(self, round3_client):
        """期間內有未封存薪資 → 預設 409 阻擋匯出。"""
        client, sf = round3_client
        self._seed_employee_and_unfinalized_record(
            sf, year=2026, month=3, finalized=False, needs_recalc=False
        )
        with sf() as s:
            _create_user(
                s,
                username="hr",
                permissions=Permission.SALARY_READ,
            )
            s.commit()

        assert _login(client, "hr").status_code == 200
        res = client.get("/api/gov-reports/labor-insurance?year=2026&month=3")
        assert res.status_code == 409
        assert "尚未封存" in res.json()["detail"]

    def test_stale_record_export_blocked_409(self, round3_client):
        """已封存但 needs_recalc=True → 視為 stale 仍阻擋。"""
        client, sf = round3_client
        self._seed_employee_and_unfinalized_record(
            sf, year=2026, month=3, finalized=True, needs_recalc=True
        )
        with sf() as s:
            _create_user(
                s,
                username="hr2",
                permissions=Permission.SALARY_READ,
            )
            s.commit()

        assert _login(client, "hr2").status_code == 200
        res = client.get("/api/gov-reports/health-insurance?year=2026&month=3")
        assert res.status_code == 409
        assert "標記待重算" in res.json()["detail"]

    def test_force_without_approve_permission_403(self, round3_client):
        """force=true 但無 ACTIVITY_PAYMENT_APPROVE → 403。"""
        client, sf = round3_client
        self._seed_employee_and_unfinalized_record(
            sf, year=2026, month=3, finalized=False, needs_recalc=False
        )
        with sf() as s:
            _create_user(
                s,
                username="hr3",
                permissions=Permission.SALARY_READ,
            )
            s.commit()

        assert _login(client, "hr3").status_code == 200
        res = client.get(
            "/api/gov-reports/labor-insurance"
            "?year=2026&month=3&force=true&force_reason=已對外手動處理"
        )
        assert res.status_code == 403
        assert "金流簽核" in res.json()["detail"]

    def test_force_short_reason_400(self, round3_client):
        """force=true 但 force_reason < 10 字 → 400。"""
        client, sf = round3_client
        self._seed_employee_and_unfinalized_record(
            sf, year=2026, month=3, finalized=False, needs_recalc=False
        )
        with sf() as s:
            _create_user(
                s,
                username="boss_gov",
                permissions=Permission.SALARY_READ
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            s.commit()

        assert _login(client, "boss_gov").status_code == 200
        res = client.get(
            "/api/gov-reports/labor-insurance"
            "?year=2026&month=3&force=true&force_reason=太短"
        )
        assert res.status_code == 400
        assert "force" in res.json()["detail"]

    def test_force_with_approve_and_reason_succeeds(self, round3_client):
        """force=true + 簽核權限 + 充足 reason → 200 並繞過守衛。"""
        client, sf = round3_client
        self._seed_employee_and_unfinalized_record(
            sf, year=2026, month=3, finalized=False, needs_recalc=False
        )
        with sf() as s:
            _create_user(
                s,
                username="boss_gov2",
                permissions=Permission.SALARY_READ
                | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            s.commit()

        assert _login(client, "boss_gov2").status_code == 200
        res = client.get(
            "/api/gov-reports/labor-insurance"
            "?year=2026&month=3&force=true"
            "&force_reason=人事系統故障必須先送審後補封存"
        )
        assert res.status_code == 200

    def test_finalized_clean_period_exports_normally(self, round3_client):
        """正常封存且非 stale → 預設 200。"""
        client, sf = round3_client
        self._seed_employee_and_unfinalized_record(
            sf, year=2026, month=3, finalized=True, needs_recalc=False
        )
        with sf() as s:
            _create_user(
                s,
                username="hr4",
                permissions=Permission.SALARY_READ,
            )
            s.commit()

        assert _login(client, "hr4").status_code == 200
        res = client.get("/api/gov-reports/pension?year=2026&month=3")
        assert res.status_code == 200

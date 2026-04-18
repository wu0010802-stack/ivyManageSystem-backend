"""tests/test_activity_pos.py — 才藝 POS 收銀端點測試。

涵蓋：
- POS checkout 原子性（一筆失敗整批 rollback）
- 多筆結帳成功後的 paid_amount / is_paid / receipt_no
- 找零計算 / 退費上限 / 付款方式驗證
- outstanding-by-student 聚合邏輯（同名不同生日、只含未結清）
- daily-summary 的日期/類型/付款方式分組
- 權限：ACTIVITY_READ 無法 POST
"""

import os
import sys
from datetime import date, datetime, timedelta

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
from models.database import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityPosDailyClose,
    ActivityRegistration,
    ActivitySupply,
    ApprovalLog,
    Base,
    Classroom,
    RegistrationCourse,
    RegistrationSupply,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def pos_client(tmp_path):
    db_path = tmp_path / "pos.sqlite"
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
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_admin(
    session,
    username: str = "pos_admin",
    password: str = "TempPass123",
    permissions: int = Permission.ACTIVITY_READ | Permission.ACTIVITY_WRITE,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permissions=permissions,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(
    client: TestClient, username: str = "pos_admin", password: str = "TempPass123"
):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _setup_reg(
    session,
    *,
    student_name: str = "王小明",
    birthday: str = "2020-01-01",
    class_name: str = "大班",
    course_price: int = 1500,
    supply_price: int = 500,
    paid_amount: int = 0,
    is_paid: bool = False,
    course_name: str = "美術",
    supply_name: str = "畫具包",
) -> ActivityRegistration:
    """建立含一門課程 + 一個用品的報名，total = course_price + supply_price。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = (
        session.query(ActivityCourse)
        .filter(
            ActivityCourse.name == course_name,
            ActivityCourse.school_year == sy,
            ActivityCourse.semester == sem,
        )
        .first()
    )
    if not course:
        course = ActivityCourse(
            name=course_name,
            price=course_price,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
        )
        session.add(course)
        session.flush()
    supply = (
        session.query(ActivitySupply)
        .filter(
            ActivitySupply.name == supply_name,
            ActivitySupply.school_year == sy,
            ActivitySupply.semester == sem,
        )
        .first()
    )
    if not supply:
        supply = ActivitySupply(
            name=supply_name, price=supply_price, school_year=sy, semester=sem
        )
        session.add(supply)
        session.flush()
    reg = ActivityRegistration(
        student_name=student_name,
        birthday=birthday,
        class_name=class_name,
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
    session.add(
        RegistrationSupply(
            registration_id=reg.id,
            supply_id=supply.id,
            price_snapshot=supply_price,
        )
    )
    session.flush()
    return reg


# ── POS Checkout 原子性（最關鍵） ──────────────────────────────────────


class TestPOSCheckoutAtomicity:
    def test_success_multi_items_updates_all(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            r1 = _setup_reg(
                s, student_name="王小明", course_name="美術", supply_name="畫具包"
            )
            r2 = _setup_reg(
                s,
                student_name="王小明",
                course_name="勞作",
                supply_name="剪刀組",
                course_price=1000,
                supply_price=300,
            )
            s.commit()
            r1_id, r2_id = r1.id, r2.id

        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [
                    {"registration_id": r1_id, "amount": 2000},
                    {"registration_id": r2_id, "amount": 1300},
                ],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "tendered": 4000,
                "notes": "現場收訖",
            },
        )
        assert res.status_code == 201, res.text
        data = res.json()
        assert data["total"] == 3300
        assert data["change"] == 700
        assert data["receipt_no"].startswith("POS-")
        assert len(data["items"]) == 2

        with sf() as s:
            recs = (
                s.query(ActivityPaymentRecord).order_by(ActivityPaymentRecord.id).all()
            )
            assert len(recs) == 2
            assert all("[POS-" in (r.notes or "") for r in recs)

            reg1 = s.query(ActivityRegistration).filter_by(id=r1_id).one()
            reg2 = s.query(ActivityRegistration).filter_by(id=r2_id).one()
            assert reg1.paid_amount == 2000
            assert reg1.is_paid is True  # total=2000
            assert reg2.paid_amount == 1300
            assert reg2.is_paid is True  # total=1300

    def test_one_invalid_reg_rolls_back_all(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            r1 = _setup_reg(s, student_name="王小明")
            s.commit()
            r1_id = r1.id

        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [
                    {"registration_id": r1_id, "amount": 500},
                    {"registration_id": 999999, "amount": 500},
                ],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
            },
        )
        assert res.status_code == 400

        with sf() as s:
            assert s.query(ActivityPaymentRecord).count() == 0
            reg = s.query(ActivityRegistration).filter_by(id=r1_id).one()
            assert reg.paid_amount == 0  # 未被寫入

    def test_refund_cannot_exceed_paid(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明", paid_amount=500, is_paid=False)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 1000}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "type": "refund",
            },
        )
        assert res.status_code == 400

        with sf() as s:
            assert s.query(ActivityPaymentRecord).count() == 0
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.paid_amount == 500  # 未變動

    def test_refund_reduces_paid_amount(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明", paid_amount=2000, is_paid=True)
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "type": "refund",
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert data["type"] == "refund"
        assert data["total"] == 500

        with sf() as s:
            reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg.paid_amount == 1500

    def test_change_ignored_for_transfer(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 2000}],
                "payment_method": "轉帳",
                "payment_date": date.today().isoformat(),
                "tendered": 5000,
            },
        )
        assert res.status_code == 201
        data = res.json()
        assert data["change"] is None
        assert data["tendered"] is None

    def test_tendered_less_than_total_rejected(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 2000}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "tendered": 1500,
            },
        )
        assert res.status_code == 400

    def test_receipt_no_embedded_in_notes(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 2000}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "notes": "測試備註",
            },
        )
        assert res.status_code == 201
        receipt_no = res.json()["receipt_no"]

        with sf() as s:
            rec = s.query(ActivityPaymentRecord).first()
            assert rec is not None
            assert receipt_no in (rec.notes or "")
            assert "測試備註" in (rec.notes or "")

    def test_duplicate_registration_ids_rejected(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [
                    {"registration_id": reg_id, "amount": 500},
                    {"registration_id": reg_id, "amount": 500},
                ],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
            },
        )
        assert res.status_code == 400

        with sf() as s:
            assert s.query(ActivityPaymentRecord).count() == 0


# ── Outstanding by Student ────────────────────────────────────────────


class TestOutstandingByStudent:
    def test_groups_by_name_and_birthday(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            _setup_reg(
                s, student_name="王小明", birthday="2020-01-01", course_name="美術"
            )
            _setup_reg(
                s, student_name="王小明", birthday="2019-05-05", course_name="勞作"
            )
            s.commit()
        assert _login(client).status_code == 200

        res = client.get("/api/activity/pos/outstanding-by-student?q=王小明")
        assert res.status_code == 200
        groups = res.json()["groups"]
        assert len(groups) == 2
        keys = {g["student_key"] for g in groups}
        assert "王小明|2020-01-01" in keys
        assert "王小明|2019-05-05" in keys

    def test_only_outstanding_included(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            _setup_reg(s, student_name="甲生", paid_amount=2000, is_paid=True)  # 已全繳
            _setup_reg(s, student_name="乙生", paid_amount=0)  # 未繳
            _setup_reg(s, student_name="丙生", paid_amount=500)  # 部分
            s.commit()
        assert _login(client).status_code == 200

        res = client.get("/api/activity/pos/outstanding-by-student?q=生")
        assert res.status_code == 200
        names = [g["student_name"] for g in res.json()["groups"]]
        assert "甲生" not in names
        assert "乙生" in names
        assert "丙生" in names

    def test_includes_courses_and_supplies(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            _setup_reg(
                s,
                student_name="王小明",
                course_name="美術",
                supply_name="畫具包",
                course_price=1500,
                supply_price=500,
            )
            s.commit()
        assert _login(client).status_code == 200

        res = client.get("/api/activity/pos/outstanding-by-student?q=王")
        group = res.json()["groups"][0]
        reg = group["registrations"][0]
        assert reg["total_amount"] == 2000
        assert reg["owed"] == 2000
        assert any(c["name"] == "美術" for c in reg["courses"])
        assert any(sp["name"] == "畫具包" for sp in reg["supplies"])

    def test_empty_query_lists_all_outstanding(self, pos_client):
        """空 q 時列出該學期所有未結清報名（預設瀏覽模式）。"""
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            _setup_reg(s, student_name="王小明", birthday="2020-01-01")
            _setup_reg(s, student_name="李小美", birthday="2019-07-07")
            s.commit()
        assert _login(client).status_code == 200
        res = client.get("/api/activity/pos/outstanding-by-student")
        assert res.status_code == 200
        names = {g["student_name"] for g in res.json()["groups"]}
        assert names == {"王小明", "李小美"}

    def test_search_by_parent_phone(self, pos_client):
        """關鍵字可比對 parent_phone。"""
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            reg.parent_phone = "0912345678"
            s.commit()
        assert _login(client).status_code == 200
        res = client.get("/api/activity/pos/outstanding-by-student?q=5678")
        names = {g["student_name"] for g in res.json()["groups"]}
        assert "王小明" in names

    def test_classroom_filter_exact_match(self, pos_client):
        """classroom 參數僅回傳精確符合班級的報名。"""
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            _setup_reg(s, student_name="王小明", class_name="大班")
            _setup_reg(
                s, student_name="李小美", birthday="2019-07-07", class_name="中班"
            )
            s.commit()
        assert _login(client).status_code == 200
        res = client.get(
            "/api/activity/pos/outstanding-by-student",
            params={"classroom": "大班"},
        )
        names = {g["student_name"] for g in res.json()["groups"]}
        assert names == {"王小明"}

    def test_overdue_only_filter(self, pos_client):
        """overdue_only 只列 created_at 早於 14 天前的項目。"""
        from datetime import datetime, timedelta

        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            old_reg = _setup_reg(s, student_name="舊生")
            old_reg.created_at = datetime.now() - timedelta(days=20)
            _setup_reg(s, student_name="新生", birthday="2019-07-07")
            s.commit()
        assert _login(client).status_code == 200
        res = client.get(
            "/api/activity/pos/outstanding-by-student",
            params={"overdue_only": "true"},
        )
        names = {g["student_name"] for g in res.json()["groups"]}
        assert names == {"舊生"}


# ── Daily Summary ──────────────────────────────────────────────────────


class TestDailySummary:
    def _create_payment(self, s, reg_id, type_, amount, method, day: date):
        rec = ActivityPaymentRecord(
            registration_id=reg_id,
            type=type_,
            amount=amount,
            payment_date=day,
            payment_method=method,
            notes="[TEST]",
            operator="tester",
        )
        s.add(rec)
        s.flush()

    def test_groups_by_method_and_type(self, pos_client):
        client, sf = pos_client
        today = date.today()
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="甲")
            self._create_payment(s, reg.id, "payment", 1000, "現金", today)
            self._create_payment(s, reg.id, "payment", 500, "現金", today)
            self._create_payment(s, reg.id, "payment", 2000, "轉帳", today)
            self._create_payment(s, reg.id, "refund", 300, "現金", today)
            s.commit()
        assert _login(client).status_code == 200

        res = client.get(f"/api/activity/pos/daily-summary?date={today.isoformat()}")
        assert res.status_code == 200
        data = res.json()
        assert data["payment_total"] == 3500
        assert data["refund_total"] == 300
        assert data["net"] == 3200
        assert data["payment_count"] == 3
        assert data["refund_count"] == 1

        methods = {m["method"]: m for m in data["by_method"]}
        assert methods["現金"]["payment"] == 1500
        assert methods["現金"]["refund"] == 300
        assert methods["轉帳"]["payment"] == 2000

    def test_payment_date_respected_not_created_at(self, pos_client):
        client, sf = pos_client
        today = date.today()
        yesterday = today - timedelta(days=1)
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="甲")
            self._create_payment(s, reg.id, "payment", 999, "現金", yesterday)
            s.commit()
        assert _login(client).status_code == 200

        today_res = client.get(
            f"/api/activity/pos/daily-summary?date={today.isoformat()}"
        )
        assert today_res.json()["payment_total"] == 0

        y_res = client.get(
            f"/api/activity/pos/daily-summary?date={yesterday.isoformat()}"
        )
        assert y_res.json()["payment_total"] == 999

    def test_invalid_date_format(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            s.commit()
        assert _login(client).status_code == 200
        res = client.get("/api/activity/pos/daily-summary?date=bad-date")
        assert res.status_code == 400


# ── 權限 ────────────────────────────────────────────────────────────────


class TestPOSPermissions:
    def test_read_only_cannot_checkout(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s, username="viewer", permissions=Permission.ACTIVITY_READ)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id

        assert _login(client, username="viewer").status_code == 200
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 1000}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
            },
        )
        assert res.status_code == 403

    def test_read_only_can_view_summary(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s, username="viewer", permissions=Permission.ACTIVITY_READ)
            s.commit()
        assert _login(client, username="viewer").status_code == 200
        res = client.get("/api/activity/pos/daily-summary")
        assert res.status_code == 200


# ── 邊界驗證（金額上限、日期範圍、notes 長度） ─────────────────────


class TestPOSInputValidation:
    def test_amount_over_limit_rejected(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 1_000_000}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
            },
        )
        assert res.status_code == 422

    def test_tendered_over_limit_rejected(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 2000}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "tendered": 100_000_000,
            },
        )
        assert res.status_code == 422

    def test_payment_date_future_rejected(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        future = (date.today() + timedelta(days=1)).isoformat()
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": future,
            },
        )
        assert res.status_code == 422

    def test_payment_date_too_far_past_rejected(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        far = (date.today() - timedelta(days=60)).isoformat()
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": far,
            },
        )
        assert res.status_code == 422

    def test_notes_over_length_rejected(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "notes": "x" * 300,
            },
        )
        assert res.status_code == 422

    def test_invalid_idempotency_key_rejected(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "idempotency_key": "x",  # 太短
            },
        )
        assert res.status_code == 422

    def test_payment_date_within_30_days_accepted(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        past = (date.today() - timedelta(days=15)).isoformat()
        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": past,
            },
        )
        assert res.status_code == 201


# ── 冪等性（idempotency_key） ──────────────────────────────────────


class TestPOSIdempotency:
    def test_same_key_returns_cached_result(self, pos_client):
        """同一 idempotency_key 兩次送出，只產生一筆 ActivityPaymentRecord。"""
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        idk = "test-idempotency-key-12345"
        payload = {
            "items": [{"registration_id": reg_id, "amount": 1000}],
            "payment_method": "現金",
            "payment_date": date.today().isoformat(),
            "idempotency_key": idk,
        }
        first = client.post("/api/activity/pos/checkout", json=payload)
        assert first.status_code == 201
        receipt_no = first.json()["receipt_no"]

        # 重送相同 key
        second = client.post("/api/activity/pos/checkout", json=payload)
        assert second.status_code == 201
        replay = second.json()
        assert replay["receipt_no"] == receipt_no
        assert replay.get("idempotent_replay") is True

        # DB 只有一筆 ActivityPaymentRecord
        with sf() as s:
            assert s.query(ActivityPaymentRecord).count() == 1
            reg_after = s.query(ActivityRegistration).filter_by(id=reg_id).one()
            assert reg_after.paid_amount == 1000

    def test_different_keys_create_separate_records(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        def _send(key):
            return client.post(
                "/api/activity/pos/checkout",
                json={
                    "items": [{"registration_id": reg_id, "amount": 500}],
                    "payment_method": "現金",
                    "payment_date": date.today().isoformat(),
                    "idempotency_key": key,
                },
            )

        r1 = _send("key-first-12345")
        r2 = _send("key-second-67890")
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["receipt_no"] != r2.json()["receipt_no"]

        with sf() as s:
            assert s.query(ActivityPaymentRecord).count() == 2


# ── 收據編號 ───────────────────────────────────────────────────────


class TestReceiptNumber:
    def test_receipt_no_has_12_hex_suffix(self, pos_client):
        """收據編號使用 12 字元 hex，降低碰撞率。"""
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
            },
        )
        assert res.status_code == 201
        receipt_no = res.json()["receipt_no"]
        # 格式：POS-YYYYMMDD-XXXXXXXXXXXX（12 hex）
        parts = receipt_no.split("-")
        assert len(parts) == 3
        assert parts[0] == "POS"
        assert len(parts[1]) == 8
        assert len(parts[2]) == 12

    def test_recent_transactions_lists_today_receipts(self, pos_client):
        """新 endpoint /pos/recent-transactions 聚合同收據的多筆記錄。"""
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            r1 = _setup_reg(s, student_name="王小明", course_name="美術")
            r2 = _setup_reg(
                s,
                student_name="王小明",
                course_name="勞作",
                supply_name="剪刀組",
                course_price=800,
                supply_price=200,
            )
            s.commit()
            r1_id, r2_id = r1.id, r2.id
        assert _login(client).status_code == 200

        # 送一次 multi-item checkout
        resp = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [
                    {"registration_id": r1_id, "amount": 2000},
                    {"registration_id": r2_id, "amount": 1000},
                ],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "tendered": 3000,
            },
        )
        assert resp.status_code == 201
        receipt_no = resp.json()["receipt_no"]

        # 查今日交易
        res = client.get("/api/activity/pos/recent-transactions")
        assert res.status_code == 200
        data = res.json()
        assert len(data["transactions"]) == 1  # 同張收據聚合成 1 筆
        tx = data["transactions"][0]
        assert tx["receipt_no"] == receipt_no
        assert tx["total"] == 3000
        assert tx["type"] == "payment"
        assert len(tx["items"]) == 2
        assert set(tx["student_names"]) == {"王小明"}
        assert tx["payment_method"] == "現金"

    def test_outstanding_filter_refundable(self, pos_client):
        """filter=refundable 只撈已繳金額 > 0 的報名。"""
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            # 未繳
            _setup_reg(s, student_name="甲", paid_amount=0)
            # 部分繳費
            _setup_reg(s, student_name="乙", paid_amount=500)
            # 全繳
            _setup_reg(s, student_name="丙", paid_amount=2000, is_paid=True)
            s.commit()
        assert _login(client).status_code == 200

        res = client.get(
            "/api/activity/pos/outstanding-by-student",
            params={"q": "甲", "filter": "refundable"},
        )
        assert res.json()["groups"] == []  # 甲未繳，不進退費列表

        res = client.get(
            "/api/activity/pos/outstanding-by-student",
            params={"q": "乙", "filter": "refundable"},
        )
        assert len(res.json()["groups"]) == 1

        res = client.get(
            "/api/activity/pos/outstanding-by-student",
            params={"q": "丙", "filter": "refundable"},
        )
        assert len(res.json()["groups"]) == 1  # 已繳仍可退費

    def test_idempotency_key_stored_in_column(self, pos_client):
        """冪等鍵已改存獨立欄位（非 notes），notes 只保留 [receipt_no] + 使用者備註。"""
        client, sf = pos_client
        with sf() as s:
            _create_admin(s)
            reg = _setup_reg(s, student_name="王小明")
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200

        res = client.post(
            "/api/activity/pos/checkout",
            json={
                "items": [{"registration_id": reg_id, "amount": 500}],
                "payment_method": "現金",
                "payment_date": date.today().isoformat(),
                "idempotency_key": "my-key-abc123",
                "notes": "Hello",
            },
        )
        assert res.status_code == 201

        with sf() as s:
            rec = s.query(ActivityPaymentRecord).first()
            assert rec.idempotency_key == "my-key-abc123"
            # notes 內不再含 IDK 標記，但仍有 receipt_no 與使用者備註
            assert "[IDK:" not in (rec.notes or "")
            assert "Hello" in (rec.notes or "")


# ── POS 日結簽核 ─────────────────────────────────────────────────────────


def _make_reg_minimal(session, student_name: str = "test") -> ActivityRegistration:
    """建立極簡 reg 作為 payment FK 母體；不建 Course/Supply 等雜訊。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    reg = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="A",
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    session.add(reg)
    session.flush()
    return reg


def _add_payment(
    session,
    reg_id: int,
    *,
    type_: str,
    amount: int,
    method: str,
    day: date,
    operator: str = "tester",
) -> ActivityPaymentRecord:
    rec = ActivityPaymentRecord(
        registration_id=reg_id,
        type=type_,
        amount=amount,
        payment_date=day,
        payment_method=method,
        notes="[T]",
        operator=operator,
    )
    session.add(rec)
    session.flush()
    return rec


class TestPosDailyClose:
    """POS 每日收款簽核：老闆核對流水、凍結 snapshot、解鎖重簽、對帳。"""

    APPROVE_PERMS = (
        Permission.ACTIVITY_READ
        | Permission.ACTIVITY_WRITE
        | Permission.ACTIVITY_PAYMENT_APPROVE
    )

    # ── A. Pending 列表 ────────────────────────────────────────────────

    def test_pending_lists_only_unapproved_dates(self, pos_client):
        """建立多日交易並簽核其中一日，pending 只含未簽核的日子。"""
        client, sf = pos_client
        today = date.today()
        d1 = today - timedelta(days=3)
        d2 = today - timedelta(days=2)
        d3 = today - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="A")
            for d in (d1, d2, d3):
                _add_payment(
                    s, reg.id, type_="payment", amount=500, method="現金", day=d
                )
            # 簽核 d2
            s.add(
                ActivityPosDailyClose(
                    close_date=d2,
                    approver_username="pos_admin",
                    approved_at=datetime(2026, 4, 18, 10, 0, 0),
                    note=None,
                    payment_total=500,
                    refund_total=0,
                    net_total=500,
                    transaction_count=1,
                    by_method_json='{"現金": 500}',
                )
            )
            s.commit()

        assert _login(client).status_code == 200
        res = client.get("/api/activity/pos/daily-close/pending")
        assert res.status_code == 200
        body = res.json()
        dates = {item["date"] for item in body["pending"]}
        assert d1.isoformat() in dates
        assert d3.isoformat() in dates
        assert d2.isoformat() not in dates

    def test_pending_includes_refund_only_day(self, pos_client):
        """某日只 refund 無 payment，仍視為有金流，應列入 pending。"""
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="R")
            _add_payment(
                s, reg.id, type_="refund", amount=800, method="現金", day=target
            )
            s.commit()
        assert _login(client).status_code == 200
        body = client.get("/api/activity/pos/daily-close/pending").json()
        row = next(x for x in body["pending"] if x["date"] == target.isoformat())
        assert row["payment_total"] == 0
        assert row["refund_total"] == 800
        assert row["net_total"] == -800
        assert row["transaction_count"] == 1

    def test_pending_rejects_range_over_92_days(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            s.commit()
        assert _login(client).status_code == 200
        res = client.get(
            "/api/activity/pos/daily-close/pending",
            params={"start_date": "2025-01-01", "end_date": "2026-01-01"},
        )
        assert res.status_code == 400

    # ── B. 查狀態 GET ──────────────────────────────────────────────────

    def test_get_status_unapproved_returns_live_preview(self, pos_client):
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="B")
            _add_payment(
                s, reg.id, type_="payment", amount=1200, method="現金", day=target
            )
            _add_payment(
                s, reg.id, type_="payment", amount=300, method="轉帳", day=target
            )
            s.commit()
        assert _login(client).status_code == 200
        body = client.get(f"/api/activity/pos/daily-close/{target.isoformat()}").json()
        assert body["is_approved"] is False
        assert body["approver_username"] is None
        assert body["payment_total"] == 1500
        assert body["refund_total"] == 0
        assert body["net_total"] == 1500
        assert body["transaction_count"] == 2
        assert body["by_method"] == {"現金": 1200, "轉帳": 300}

    def test_get_status_approved_returns_snapshot(self, pos_client):
        """簽核後即便補新交易，GET 仍回 snapshot（不重算）。"""
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="C")
            _add_payment(
                s, reg.id, type_="payment", amount=1000, method="現金", day=target
            )
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200
        approve = client.post(
            f"/api/activity/pos/daily-close/{target.isoformat()}", json={}
        )
        assert approve.status_code == 201

        # 事後補一筆交易
        with sf() as s:
            _add_payment(
                s, reg_id, type_="payment", amount=999, method="現金", day=target
            )
            s.commit()

        body = client.get(f"/api/activity/pos/daily-close/{target.isoformat()}").json()
        assert body["is_approved"] is True
        assert body["payment_total"] == 1000  # 仍為簽核當下 snapshot，未加 999
        assert body["by_method"] == {"現金": 1000}

    # ── C. 簽核 POST ───────────────────────────────────────────────────

    def test_approve_freezes_snapshot_and_writes_approval_log(self, pos_client):
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="D")
            _add_payment(
                s, reg.id, type_="payment", amount=2000, method="現金", day=target
            )
            _add_payment(
                s, reg.id, type_="refund", amount=200, method="現金", day=target
            )
            s.commit()
        assert _login(client).status_code == 200
        res = client.post(
            f"/api/activity/pos/daily-close/{target.isoformat()}",
            json={"note": "已核對"},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["is_approved"] is True
        assert data["approver_username"] == "pos_admin"
        assert data["payment_total"] == 2000
        assert data["refund_total"] == 200
        assert data["net_total"] == 1800
        assert data["transaction_count"] == 2
        assert data["by_method"] == {"現金": 1800}

        with sf() as s:
            row = (
                s.query(ActivityPosDailyClose)
                .filter(ActivityPosDailyClose.close_date == target)
                .one()
            )
            assert row.note == "已核對"
            log = (
                s.query(ApprovalLog)
                .filter(
                    ApprovalLog.doc_type == "activity_pos_daily",
                    ApprovalLog.action == "approved",
                )
                .one()
            )
            assert log.doc_id == int(target.strftime("%Y%m%d"))
            assert log.approver_username == "pos_admin"
            assert log.approver_role == "admin"
            assert log.comment == "已核對"

    def test_approve_with_cash_count_computes_variance(self, pos_client):
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="E")
            _add_payment(
                s, reg.id, type_="payment", amount=1500, method="現金", day=target
            )
            s.commit()
        assert _login(client).status_code == 200
        res = client.post(
            f"/api/activity/pos/daily-close/{target.isoformat()}",
            json={"actual_cash_count": 1480},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["actual_cash_count"] == 1480
        assert data["cash_variance"] == -20  # 1480 - 1500

    def test_approve_with_zero_actual_cash_count_sets_negative_variance(self, pos_client):
        """actual_cash_count=0 視為有效盤點（非 None），variance = -1000。"""
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="F")
            _add_payment(
                s, reg.id, type_="payment", amount=1000, method="現金", day=target
            )
            s.commit()
        assert _login(client).status_code == 200
        res = client.post(
            f"/api/activity/pos/daily-close/{target.isoformat()}",
            json={"actual_cash_count": 0},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["actual_cash_count"] == 0
        assert data["cash_variance"] == -1000

    def test_approve_with_actual_cash_but_no_cash_tx(self, pos_client):
        """當日無現金交易；老闆報 500 → variance = 500 - 0 = 500。"""
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="G")
            _add_payment(
                s, reg.id, type_="payment", amount=700, method="轉帳", day=target
            )
            s.commit()
        assert _login(client).status_code == 200
        res = client.post(
            f"/api/activity/pos/daily-close/{target.isoformat()}",
            json={"actual_cash_count": 500},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["by_method"] == {"轉帳": 700}
        assert data["actual_cash_count"] == 500
        assert data["cash_variance"] == 500

    def test_approve_rejects_duplicate_with_409(self, pos_client):
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="H")
            _add_payment(
                s, reg.id, type_="payment", amount=100, method="現金", day=target
            )
            s.commit()
        assert _login(client).status_code == 200
        first = client.post(f"/api/activity/pos/daily-close/{target.isoformat()}", json={})
        assert first.status_code == 201
        second = client.post(f"/api/activity/pos/daily-close/{target.isoformat()}", json={})
        assert second.status_code == 409

    def test_approve_rejects_future_date_with_400(self, pos_client):
        client, sf = pos_client
        future = date.today() + timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            s.commit()
        assert _login(client).status_code == 200
        res = client.post(f"/api/activity/pos/daily-close/{future.isoformat()}", json={})
        assert res.status_code == 400

    def test_approve_rejects_bad_date_format(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            s.commit()
        assert _login(client).status_code == 200
        res = client.post("/api/activity/pos/daily-close/bad-date", json={})
        assert res.status_code == 400

    def test_approve_zero_transaction_day_succeeds_currently(self, pos_client):
        """釘住當前行為：即使當日完全無交易，簽核依然成功（snapshot 全 0）。"""
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            s.commit()
        assert _login(client).status_code == 200
        res = client.post(f"/api/activity/pos/daily-close/{target.isoformat()}", json={})
        assert res.status_code == 201
        data = res.json()
        assert data["payment_total"] == 0
        assert data["refund_total"] == 0
        assert data["net_total"] == 0
        assert data["transaction_count"] == 0
        assert data["by_method"] == {}

    def test_approve_note_over_length_rejected(self, pos_client):
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            s.commit()
        assert _login(client).status_code == 200
        res = client.post(
            f"/api/activity/pos/daily-close/{target.isoformat()}",
            json={"note": "x" * 600},
        )
        assert res.status_code == 422

    # ── D. 解鎖 DELETE ─────────────────────────────────────────────────

    def test_unlock_deletes_row_and_writes_cancel_log(self, pos_client):
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="I")
            _add_payment(
                s, reg.id, type_="payment", amount=100, method="現金", day=target
            )
            s.commit()
        assert _login(client).status_code == 200
        assert (
            client.post(
                f"/api/activity/pos/daily-close/{target.isoformat()}", json={}
            ).status_code
            == 201
        )
        res = client.delete(f"/api/activity/pos/daily-close/{target.isoformat()}")
        assert res.status_code == 204
        assert res.content == b""

        with sf() as s:
            assert (
                s.query(ActivityPosDailyClose)
                .filter(ActivityPosDailyClose.close_date == target)
                .first()
                is None
            )
            cancel_log = (
                s.query(ApprovalLog)
                .filter(
                    ApprovalLog.doc_type == "activity_pos_daily",
                    ApprovalLog.action == "cancelled",
                )
                .one()
            )
            assert cancel_log.doc_id == int(target.strftime("%Y%m%d"))
            assert "解鎖" in (cancel_log.comment or "")
            assert "原簽核人 pos_admin" in (cancel_log.comment or "")

    def test_unlock_nonexistent_returns_404(self, pos_client):
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            s.commit()
        assert _login(client).status_code == 200
        res = client.delete(f"/api/activity/pos/daily-close/{target.isoformat()}")
        assert res.status_code == 404

    def test_full_cycle_approve_unlock_reapprove_captures_new_tx(self, pos_client):
        """核心循環：approve → 補新交易 → DELETE → 再 approve 應吃到新交易。"""
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="J")
            _add_payment(
                s, reg.id, type_="payment", amount=1000, method="現金", day=target
            )
            s.commit()
            reg_id = reg.id

        assert _login(client).status_code == 200
        first = client.post(
            f"/api/activity/pos/daily-close/{target.isoformat()}", json={}
        )
        assert first.status_code == 201
        assert first.json()["payment_total"] == 1000

        with sf() as s:
            _add_payment(
                s, reg_id, type_="payment", amount=500, method="現金", day=target
            )
            s.commit()

        assert (
            client.delete(
                f"/api/activity/pos/daily-close/{target.isoformat()}"
            ).status_code
            == 204
        )
        second = client.post(
            f"/api/activity/pos/daily-close/{target.isoformat()}", json={}
        )
        assert second.status_code == 201
        assert second.json()["payment_total"] == 1500  # 解鎖後重算吃到新交易

        with sf() as s:
            logs = (
                s.query(ApprovalLog)
                .filter(
                    ApprovalLog.doc_type == "activity_pos_daily",
                    ApprovalLog.doc_id == int(target.strftime("%Y%m%d")),
                )
                .order_by(ApprovalLog.id)
                .all()
            )
            actions = [log.action for log in logs]
            assert actions == ["approved", "cancelled", "approved"]

    # ── E. 權限 ────────────────────────────────────────────────────────

    def test_permission_read_cannot_approve_or_unlock(self, pos_client):
        """僅 ACTIVITY_READ 可 GET，但 POST/DELETE 應回 403。"""
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(
                s,
                username="viewer",
                password="Viewer12345",
                permissions=Permission.ACTIVITY_READ,
            )
            s.commit()
        assert _login(client, username="viewer", password="Viewer12345").status_code == 200

        assert client.get(f"/api/activity/pos/daily-close/{target.isoformat()}").status_code == 200
        assert (
            client.post(
                f"/api/activity/pos/daily-close/{target.isoformat()}", json={}
            ).status_code
            == 403
        )
        assert (
            client.delete(
                f"/api/activity/pos/daily-close/{target.isoformat()}"
            ).status_code
            == 403
        )

    def test_permission_approve_role_can_approve(self, pos_client):
        client, sf = pos_client
        target = date.today() - timedelta(days=1)
        with sf() as s:
            _create_admin(
                s,
                username="boss",
                password="BossPass1234",
                permissions=Permission.ACTIVITY_READ | Permission.ACTIVITY_PAYMENT_APPROVE,
            )
            s.commit()
        assert _login(client, username="boss", password="BossPass1234").status_code == 200
        res = client.post(f"/api/activity/pos/daily-close/{target.isoformat()}", json={})
        assert res.status_code == 201
        assert res.json()["approver_username"] == "boss"

    # ── F. 對帳 ────────────────────────────────────────────────────────

    def test_reconciliation_mixes_snapshot_and_live(self, pos_client):
        """已簽核日用 snapshot（補交易後仍為凍結值），未簽核日即時計算。"""
        client, sf = pos_client
        today = date.today()
        d_approved = today - timedelta(days=2)
        d_live = today - timedelta(days=1)
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            reg = _make_reg_minimal(s, student_name="K")
            _add_payment(
                s, reg.id, type_="payment", amount=1000, method="現金", day=d_approved
            )
            _add_payment(
                s, reg.id, type_="payment", amount=2000, method="轉帳", day=d_live
            )
            s.commit()
            reg_id = reg.id
        assert _login(client).status_code == 200
        # 簽核 d_approved
        assert (
            client.post(
                f"/api/activity/pos/daily-close/{d_approved.isoformat()}",
                json={"actual_cash_count": 1000},
            ).status_code
            == 201
        )
        # 簽核後補交易（不該影響 d_approved 的 snapshot）
        with sf() as s:
            _add_payment(
                s, reg_id, type_="payment", amount=9999, method="現金", day=d_approved
            )
            s.commit()

        res = client.get(
            "/api/activity/pos/reconciliation",
            params={
                "start_date": (today - timedelta(days=3)).isoformat(),
                "end_date": today.isoformat(),
            },
        )
        assert res.status_code == 200
        body = res.json()
        items = {item["date"]: item for item in body["items"]}
        assert items[d_approved.isoformat()]["is_approved"] is True
        assert items[d_approved.isoformat()]["payment_total"] == 1000  # 凍結
        assert items[d_approved.isoformat()]["actual_cash"] == 1000
        assert items[d_approved.isoformat()]["variance"] == 0

        assert items[d_live.isoformat()]["is_approved"] is False
        assert items[d_live.isoformat()]["payment_total"] == 2000
        assert items[d_live.isoformat()]["actual_cash"] is None
        assert items[d_live.isoformat()]["variance"] is None

    def test_reconciliation_max_days_guard(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _create_admin(s, permissions=self.APPROVE_PERMS)
            s.commit()
        assert _login(client).status_code == 200
        res = client.get(
            "/api/activity/pos/reconciliation",
            params={"start_date": "2025-01-01", "end_date": "2026-01-01"},
        )
        assert res.status_code == 400

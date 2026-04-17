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
from models.database import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    ActivitySupply,
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

    def test_idempotency_key_stored_in_notes(self, pos_client):
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
            assert "[IDK:my-key-abc123]" in (rec.notes or "")
            assert "Hello" in (rec.notes or "")

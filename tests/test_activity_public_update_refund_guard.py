"""tests/test_activity_public_update_refund_guard.py

公開 /public/update 端點的金流守衛回歸測試（2026-04-28）。

對應審計 finding A 系列：
- A:   家長前台 update 觸發 paid_amount > new_total 時應 409 拒絕；不寫
       「系統補齊」退費紀錄、不扣 paid_amount。原本會繞過所有金流守衛
       （無金額閘門、無原因記錄、無 admin 即時通知）。
- A.1: update 移除已被點名的課程時，對應 ActivityAttendance 應被清除
       （對齊管理端 withdraw_course 行為），避免出席率統計納入退課孤兒。
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
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    ActivitySession,
    Base,
    Classroom,
    RegistrationCourse,
)


@pytest.fixture
def public_update_client(tmp_path):
    db_path = tmp_path / "public-update-guard.sqlite"
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
    _public_register_limiter_instance._timestamps.clear()

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


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _setup(
    session,
    *,
    course_name: str = "圍棋",
    course_price: int = 5000,
    student_name: str = "王小明",
    parent_phone: str = "0912345678",
    paid_amount: int = 0,
    is_paid: bool = False,
):
    """建立 classroom + course + active registration（含 enrolled RC）。

    回傳 (registration, course)。
    """
    sy, sem = _term()
    classroom = Classroom(name="海豚班", is_active=True)
    session.add(classroom)
    session.flush()

    course = ActivityCourse(
        name=course_name,
        price=course_price,
        capacity=30,
        allow_waitlist=True,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()

    reg = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="海豚班",
        parent_phone=parent_phone,
        paid_amount=paid_amount,
        is_paid=is_paid,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(reg)
    session.flush()

    rc = RegistrationCourse(
        registration_id=reg.id,
        course_id=course.id,
        status="enrolled",
        price_snapshot=course_price,
    )
    session.add(rc)
    session.commit()
    return reg, course


# ═══════════════════════════════════════════════════════════════════════
# A：超繳退費守衛
# ═══════════════════════════════════════════════════════════════════════


class TestPublicUpdateRefundGuard:
    def test_overpaid_update_is_rejected_and_paid_amount_intact(
        self, public_update_client
    ):
        """家長 update 把已繳費課程移除 → 409，paid_amount 不變、不寫退費紀錄。"""
        client, sf = public_update_client
        with sf() as s:
            reg, _course = _setup(s, paid_amount=5000, is_paid=True)
            reg_id = reg.id

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": reg_id,
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "海豚班",
                "parent_phone": "0912345678",
                "courses": [],
                "supplies": [],
                "remark": "",
            },
        )
        assert res.status_code == 409, res.text
        assert "退費" in res.json()["detail"]

        with sf() as s:
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 5000, "拒絕後不可扣 paid_amount"
            assert reg.is_paid is True, "拒絕後 is_paid 旗標不可被改"

            refunds = (
                s.query(ActivityPaymentRecord)
                .filter(ActivityPaymentRecord.registration_id == reg_id)
                .all()
            )
            assert refunds == [], "拒絕路徑不可寫入任何 payment_record"

            # 課程未被移除（rollback 後保留）
            rc_count = (
                s.query(RegistrationCourse)
                .filter(RegistrationCourse.registration_id == reg_id)
                .count()
            )
            assert rc_count == 1, "拒絕後原 RegistrationCourse 應保留"

    def test_partial_overpaid_swap_is_rejected(self, public_update_client):
        """已繳 5000、改為較便宜課程使應繳變 1000（產生 4000 退費）→ 同樣 409。"""
        client, sf = public_update_client
        with sf() as s:
            reg, _course = _setup(s, paid_amount=5000, is_paid=True)
            sy, sem = _term()
            cheap = ActivityCourse(
                name="珠心算",
                price=1000,
                capacity=30,
                allow_waitlist=True,
                is_active=True,
                school_year=sy,
                semester=sem,
            )
            s.add(cheap)
            s.commit()
            reg_id = reg.id

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": reg_id,
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "海豚班",
                "parent_phone": "0912345678",
                "courses": [{"name": "珠心算"}],
                "supplies": [],
                "remark": "",
            },
        )
        assert res.status_code == 409, res.text

        with sf() as s:
            reg = s.query(ActivityRegistration).get(reg_id)
            assert reg.paid_amount == 5000

    def test_balanced_update_still_succeeds(self, public_update_client):
        """同價課程互換（不產生退費）路徑仍可成功。"""
        client, sf = public_update_client
        with sf() as s:
            reg, _course = _setup(s, paid_amount=5000, is_paid=True)
            sy, sem = _term()
            equal = ActivityCourse(
                name="珠心算",
                price=5000,
                capacity=30,
                allow_waitlist=True,
                is_active=True,
                school_year=sy,
                semester=sem,
            )
            s.add(equal)
            s.commit()
            reg_id = reg.id

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": reg_id,
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "海豚班",
                "parent_phone": "0912345678",
                "courses": [{"name": "珠心算"}],
                "supplies": [],
                "remark": "",
            },
        )
        assert res.status_code == 200, res.text


# ═══════════════════════════════════════════════════════════════════════
# A.1：移除已點名課程清除 attendance 孤兒
# ═══════════════════════════════════════════════════════════════════════


class TestPublicUpdateClearsAttendance:
    def test_removed_course_attendance_is_cleared(self, public_update_client):
        """家長 update 移除已被點名的課程 → 該課程 attendance 應被清。"""
        client, sf = public_update_client
        with sf() as s:
            sy, sem = _term()
            reg, course = _setup(s, paid_amount=0, is_paid=False)
            other = ActivityCourse(
                name="珠心算",
                price=0,
                capacity=30,
                allow_waitlist=True,
                is_active=True,
                school_year=sy,
                semester=sem,
            )
            s.add(other)
            s.flush()
            sess = ActivitySession(
                course_id=course.id,
                session_date=date.today(),
            )
            s.add(sess)
            s.flush()
            att = ActivityAttendance(
                session_id=sess.id,
                registration_id=reg.id,
                is_present=True,
            )
            s.add(att)
            s.commit()
            reg_id = reg.id
            course_id = course.id
            sess_id = sess.id

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": reg_id,
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "海豚班",
                "parent_phone": "0912345678",
                "courses": [{"name": "珠心算"}],
                "supplies": [],
                "remark": "",
            },
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            remaining = (
                s.query(ActivityAttendance)
                .filter(
                    ActivityAttendance.registration_id == reg_id,
                    ActivityAttendance.session_id == sess_id,
                )
                .count()
            )
            assert remaining == 0, "移除課程後該課程的 attendance 應被清除"
            # 確認 RC 也真的被移除了（沒退回原狀）
            rc_course_ids = {
                cid
                for (cid,) in s.query(RegistrationCourse.course_id)
                .filter(RegistrationCourse.registration_id == reg_id)
                .all()
            }
            assert course_id not in rc_course_ids

    def test_kept_course_attendance_is_not_cleared(self, public_update_client):
        """更新後仍報名同課程 → 該課程的歷史 attendance 不應被誤刪。"""
        client, sf = public_update_client
        with sf() as s:
            reg, course = _setup(s, paid_amount=0, is_paid=False)
            sess = ActivitySession(
                course_id=course.id,
                session_date=date.today(),
            )
            s.add(sess)
            s.flush()
            att = ActivityAttendance(
                session_id=sess.id,
                registration_id=reg.id,
                is_present=True,
            )
            s.add(att)
            s.commit()
            reg_id = reg.id
            sess_id = sess.id

        res = client.post(
            "/api/activity/public/update",
            json={
                "id": reg_id,
                "name": "王小明",
                "birthday": "2020-01-01",
                "class": "海豚班",
                "parent_phone": "0912345678",
                # 仍報名「圍棋」（與原本相同）
                "courses": [{"name": "圍棋"}],
                "supplies": [],
                "remark": "",
            },
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            remaining = (
                s.query(ActivityAttendance)
                .filter(
                    ActivityAttendance.registration_id == reg_id,
                    ActivityAttendance.session_id == sess_id,
                )
                .count()
            )
            assert remaining == 1, "未移除的課程其歷史 attendance 不可被刪"

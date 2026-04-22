"""本次才藝系統邏輯漏洞修復的回歸測試（2026-04-22）

覆蓋：
- M1: 同家長同學生同學期 is_active=TRUE 的 partial unique index
- M2: 學生離園自動沖帳會寫 RegistrationChange 軌跡
- L3: 生日格式/範圍驗證（Pydantic 層）
- L4: /public/update 換手機號時擋住與其他報名衝突
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationChange,
    Student,
)
from utils.academic import resolve_current_academic_term


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "logic-holes.sqlite"
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
    _public_register_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_term():
    return resolve_current_academic_term()


def _seed_basic(session, sy, sem, *, classroom_active=True):
    classroom = Classroom(
        name="大象班", is_active=classroom_active, school_year=sy, semester=sem
    )
    session.add(classroom)
    session.flush()
    session.add(
        ActivityCourse(
            name="圍棋",
            price=1200,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
    )
    session.commit()
    return classroom


def _public_register_payload(
    *,
    name="王小明",
    birthday="2020-05-10",
    phone="0912345678",
    class_="大象班",
    course_name="圍棋",
):
    return {
        "name": name,
        "birthday": birthday,
        "parent_phone": phone,
        "class": class_,
        "courses": [{"name": course_name, "price": "1"}],
        "supplies": [],
    }


# ═══════════════════════════════════════════════════════════════════════════
# M1: partial unique index（防併發重複報名）
# ═══════════════════════════════════════════════════════════════════════════


class TestActiveRegistrationUniqueIndex:
    def test_db_blocks_duplicate_active_registration_same_family(self, client):
        """同家長同學生同學期直接 INSERT 兩筆 is_active=TRUE → DB 擋下第二筆。"""
        _, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            s.add_all(
                [
                    ActivityRegistration(
                        student_name="王小明",
                        birthday="2020-05-10",
                        class_name="大象班",
                        parent_phone="0912345678",
                        school_year=sy,
                        semester=sem,
                        is_active=True,
                    ),
                    ActivityRegistration(
                        student_name="王小明",
                        birthday="2020-05-10",
                        class_name="大象班",
                        parent_phone="0912345678",
                        school_year=sy,
                        semester=sem,
                        is_active=True,
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                s.commit()

    def test_db_allows_same_name_birthday_with_different_parent_phone(self, client):
        """不同家長但同姓同生日的兩個小孩（極端少見）在 DB 層仍可並存。"""
        _, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            s.add_all(
                [
                    ActivityRegistration(
                        student_name="王小明",
                        birthday="2020-05-10",
                        class_name="大象班",
                        parent_phone="0911111111",
                        school_year=sy,
                        semester=sem,
                        is_active=True,
                    ),
                    ActivityRegistration(
                        student_name="王小明",
                        birthday="2020-05-10",
                        class_name="大象班",
                        parent_phone="0922222222",
                        school_year=sy,
                        semester=sem,
                        is_active=True,
                    ),
                ]
            )
            s.commit()
            assert (
                s.query(ActivityRegistration)
                .filter(ActivityRegistration.is_active.is_(True))
                .count()
                == 2
            )

    def test_db_allows_reregister_after_soft_delete(self, client):
        """軟刪除後同家長可再建立新的有效報名（partial index WHERE is_active=1）。"""
        _, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            s.add(
                ActivityRegistration(
                    student_name="王小明",
                    birthday="2020-05-10",
                    class_name="大象班",
                    parent_phone="0912345678",
                    school_year=sy,
                    semester=sem,
                    is_active=False,  # 已軟刪
                )
            )
            s.add(
                ActivityRegistration(
                    student_name="王小明",
                    birthday="2020-05-10",
                    class_name="大象班",
                    parent_phone="0912345678",
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.commit()  # 不應拋 IntegrityError

    def test_public_register_second_submit_returns_400(self, client):
        """應用層：家長連送兩次相同資料，第二次回 400 + 可辨識訊息。"""
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
        r1 = c.post("/api/activity/public/register", json=_public_register_payload())
        assert r1.status_code == 201, r1.text
        r2 = c.post("/api/activity/public/register", json=_public_register_payload())
        assert r2.status_code == 400
        assert "已有有效報名" in r2.json()["detail"]


# ═══════════════════════════════════════════════════════════════════════════
# M2: 學生離園自動沖帳 → log_change 軌跡
# ═══════════════════════════════════════════════════════════════════════════


class TestDeactivateAutoRefundLogged:
    def test_auto_refund_writes_registration_change(self, client):
        """sync_registrations_on_student_deactivate 自動寫退費時會留 RegistrationChange。"""
        _, sf = client
        sy, sem = _seed_term()
        from api.activity._shared import sync_registrations_on_student_deactivate

        with sf() as s:
            student = Student(
                student_id="S001",
                name="王小明",
                birthday=date(2020, 5, 10),
                is_active=True,
            )
            s.add(student)
            s.flush()
            s.add(
                ActivityRegistration(
                    student_id=student.id,
                    student_name="王小明",
                    birthday="2020-05-10",
                    class_name="大象班",
                    parent_phone="0912345678",
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                    paid_amount=1200,
                    is_paid=True,
                )
            )
            s.commit()

            sync_registrations_on_student_deactivate(s, student.id)
            s.commit()

            changes = (
                s.query(RegistrationChange)
                .filter(RegistrationChange.change_type == "學生離園自動沖帳")
                .all()
            )
            assert len(changes) == 1
            assert "NT$1200" in changes[0].description


# ═══════════════════════════════════════════════════════════════════════════
# L3: 生日格式/範圍
# ═══════════════════════════════════════════════════════════════════════════


class TestBirthdayRangeValidation:
    def test_rejects_future_birthday(self, client):
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
        r = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(birthday="2099-01-01"),
        )
        assert r.status_code == 422
        assert "未來" in r.text

    def test_rejects_too_old_birthday(self, client):
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
        r = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(birthday="1900-01-01"),
        )
        assert r.status_code == 422
        assert "合理範圍" in r.text

    def test_rejects_malformed_birthday(self, client):
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
        r = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(birthday="2020/05/10"),
        )
        assert r.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
# L4: /public/update 換手機號衝突檢查
# ═══════════════════════════════════════════════════════════════════════════


class TestPublicUpdatePhoneConflict:
    def test_cannot_change_to_phone_used_by_another_active_registration(self, client):
        """若 new_parent_phone 已被同學期另一筆 active 報名使用 → 409。"""
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)
            s.add(
                ActivityCourse(
                    name="繪畫",
                    price=1500,
                    school_year=sy,
                    semester=sem,
                    is_active=True,
                )
            )
            s.commit()

        # 家長 A 先報名
        r_a = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(phone="0911111111"),
        )
        assert r_a.status_code == 201, r_a.text
        reg_a_id = r_a.json()["id"]

        # 家長 B（不同姓名）報名
        r_b = c.post(
            "/api/activity/public/register",
            json=_public_register_payload(name="林小美", phone="0922222222"),
        )
        assert r_b.status_code == 201, r_b.text

        # 家長 A 試圖把手機改成家長 B 的號碼
        r_upd = c.post(
            "/api/activity/public/update",
            json={
                "id": reg_a_id,
                "name": "王小明",
                "birthday": "2020-05-10",
                "parent_phone": "0911111111",
                "new_parent_phone": "0922222222",
                "class": "大象班",
                "courses": [{"name": "圍棋", "price": "1"}],
                "supplies": [],
            },
        )
        assert r_upd.status_code == 409
        assert "已被其他報名" in r_upd.json()["detail"]

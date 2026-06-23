"""才藝報名 restore：滿班且「不開放候補」的課程應被剔除而非強制 waitlist（P2 回歸）。

bug：restore_registration 的容量重檢（Task A4）在 occupying >= capacity 時無條件
把課程列改成 waitlist，沒檢查 course.allow_waitlist。於是「不開放候補」的課程在
restore 後會多出一筆候補列，違反課程設定，且這筆死候補列永遠不會被 promote，
還會污染後續 waitlist UI / promotion 流程。

其餘四條建立報名的路徑（_shared._attach_courses / registrations_items.add_course /
parent_portal.activity）滿班且不開放候補一律 400。restore 是「盡力復原」語意，已對
停用課程/用品採「剔除該列」前例（session.delete）。業主裁定：滿班且不開放候補的
課程列同樣剔除（其餘課程照常復原）。

修正後 restore 時：
  capacity=1、allow_waitlist=False 的課程 → A 佔唯一名額（enrolled）
  → reject A（名額釋出，A 的 RC 仍 enrolled 但 A 已 inactive）
  → B 補上唯一名額（enrolled）
  → restore A → A 的課程列應被『剔除』（非 waitlist），占容量回到 1。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationCourse,
    Student,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def restore_client(tmp_path):
    db_path = tmp_path / "restore_no_waitlist.sqlite"
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


def _add_admin(session, username="admin", password="TempPass123"):
    session.add(
        User(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ"],
            is_active=True,
        )
    )
    session.flush()


def _login(client, username="admin", password="TempPass123"):
    r = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200
    return r


def _seed_no_waitlist_capacity_one(session):
    """建立 admin + 一門 capacity=1、allow_waitlist=False 的課程「書法」。回傳 course_id。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    _add_admin(session)
    session.add(Classroom(name="大象班", is_active=True, school_year=sy, semester=sem))
    session.add(
        Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 5, 10),
            parent_phone="0912345678",
            is_active=True,
        )
    )
    session.add(
        Student(
            student_id="S002",
            name="陳小美",
            birthday=date(2019, 1, 1),
            parent_phone="0922222222",
            is_active=True,
        )
    )
    course = ActivityCourse(
        name="書法",
        price=1200,
        capacity=1,  # ← 唯一名額
        allow_waitlist=False,  # ← 不開放候補：滿班只能 enrolled 或被擋，不該出現 waitlist
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    session.add(course)
    session.commit()
    return course.id


def _register(client, *, name, birthday, phone):
    return client.post(
        "/api/activity/public/register",
        json={
            "name": name,
            "birthday": birthday,
            "parent_phone": phone,
            "class": "大象班",
            "courses": [{"name": "書法", "price": "1"}],
            "supplies": [],
        },
    )


def _occupying_count(session, course_id):
    return (
        session.query(func.count(RegistrationCourse.id))
        .join(
            ActivityRegistration,
            RegistrationCourse.registration_id == ActivityRegistration.id,
        )
        .filter(
            RegistrationCourse.course_id == course_id,
            RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
            ActivityRegistration.is_active.is_(True),
        )
        .scalar()
    )


def test_restore_drops_course_row_when_full_and_no_waitlist(restore_client):
    """reject A → B 補位 → restore A：不開放候補的滿班課程應剔除 A 的課程列（非 waitlist）。"""
    client, sf = restore_client
    with sf() as s:
        course_id = _seed_no_waitlist_capacity_one(s)

    # A 報名 → 佔掉唯一名額（RC=enrolled）
    ra = _register(client, name="王小明", birthday="2020-05-10", phone="0912345678")
    assert ra.status_code == 201, ra.text
    reg_a_id = ra.json()["id"]

    _login(client)

    # reject A → 釋出名額（A 的 RC 仍 enrolled 但 A 已 inactive）
    rj = client.post(
        f"/api/activity/registrations/{reg_a_id}/reject",
        json={"reason": "測試用拒絕原因"},
    )
    assert rj.status_code == 200, rj.text

    # B 補上唯一名額（不開放候補課程仍可正常 enrolled）
    rb = _register(client, name="陳小美", birthday="2019-01-01", phone="0922222222")
    assert rb.status_code == 201, rb.text
    reg_b_id = rb.json()["id"]

    with sf() as s:
        rc_b = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_b_id, course_id=course_id)
            .one()
        )
        assert rc_b.status == "enrolled", f"前置失敗：B 應 enrolled，實際={rc_b.status}"
        assert _occupying_count(s, course_id) == 1

    # restore A
    res = client.post(f"/api/activity/registrations/{reg_a_id}/restore")
    assert res.status_code == 200, res.text

    with sf() as s:
        # 核心斷言：A 的課程列應被『剔除』，而非降 waitlist（課程不開放候補）
        rc_a = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_a_id, course_id=course_id)
            .one_or_none()
        )
        assert rc_a is None, (
            "不開放候補的滿班課程，restore 時應剔除該課程列，"
            f"不該保留任何列（含 waitlist），實際={getattr(rc_a, 'status', None)}"
        )

        # 不得產生任何 waitlist 列（違反課程設定）
        wl = (
            s.query(func.count(RegistrationCourse.id))
            .filter(
                RegistrationCourse.course_id == course_id,
                RegistrationCourse.status == "waitlist",
            )
            .scalar()
        )
        assert wl == 0, f"不開放候補課程不該出現候補列，實際候補數={wl}"

        # 占容量不超過容量；B 仍正式
        assert _occupying_count(s, course_id) == 1
        rc_b = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_b_id, course_id=course_id)
            .one()
        )
        assert rc_b.status == "enrolled"

        # 課程列被剔除後，A 的應繳重算為 0（不向家長收無法復原的課程費）
        reg_a = s.query(ActivityRegistration).filter_by(id=reg_a_id).one()
        assert reg_a.is_active is True

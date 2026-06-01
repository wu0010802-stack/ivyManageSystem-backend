"""才藝報名「reject → 名額被遞補 → restore 超賣」回歸測試（Task A4）。

bug：reject_registration 只翻 is_active=False，不清被拒報名的 RegistrationCourse
列（仍掛 enrolled），也不遞補；restore_registration 翻回 is_active=True 時只做
同名同生日 dedup 守衛，不重新檢查容量。於是：

  容量=1 的課程 → A 報名佔掉唯一名額（RC=enrolled）
  → reject A（名額釋出但 A 的 RC 仍 enrolled）
  → B 報名補上唯一名額（RC=enrolled）
  → restore A → A 翻回 active，A+B 兩列同時 enrolled → 超賣（占容量 2 > 1）

修正後 restore 時重數占位，超出容量者降為 waitlist，A 應被降 waitlist，
占容量回到 1（≤ capacity）。
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
    db_path = tmp_path / "restore_cap.sqlite"
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


def _seed_capacity_one(session):
    """建立 admin + 一門 capacity=1 的課程「圍棋」。回傳 course_id。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    _add_admin(session)
    # 兩名學生都建好對應 Student（讓 public register 走 matched 路徑，不進待審核），
    # name+birthday 刻意不同 → 避開 restore 的同名同生日 dedup 守衛。
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
        name="圍棋",
        price=1200,
        capacity=1,  # ← 唯一名額，超賣才看得出來
        allow_waitlist=True,
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
            "courses": [{"name": "圍棋", "price": "1"}],
            "supplies": [],
        },
    )


def _occupying_count(session, course_id):
    """生產定義的占容量數：is_active 報名 + RC.status in (enrolled, promoted_pending)。"""
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


def test_restore_does_not_oversell_when_slot_was_backfilled(restore_client):
    """reject A → B 補位 → restore A：A 的課程列應被降為 waitlist，占容量 ≤ 容量。"""
    client, sf = restore_client
    with sf() as s:
        course_id = _seed_capacity_one(s)

    # A 報名 → 佔掉唯一名額（RC=enrolled）
    ra = _register(client, name="王小明", birthday="2020-05-10", phone="0912345678")
    assert ra.status_code == 201, ra.text
    reg_a_id = ra.json()["id"]

    _login(client)

    # reject A → 釋出名額（但 A 的 RC 在修前仍為 enrolled）
    rj = client.post(
        f"/api/activity/registrations/{reg_a_id}/reject",
        json={"reason": "測試用拒絕原因"},
    )
    assert rj.status_code == 200, rj.text

    # B（不同 name+birthday，避開 dedup 守衛）報名 → 補上唯一名額
    rb = _register(client, name="陳小美", birthday="2019-01-01", phone="0922222222")
    assert rb.status_code == 201, rb.text
    reg_b_id = rb.json()["id"]

    # 前置條件：B 必須是 enrolled（占住唯一名額），否則無法形成超賣。
    with sf() as s:
        rc_b = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_b_id, course_id=course_id)
            .one()
        )
        assert (
            rc_b.status == "enrolled"
        ), f"前置失敗：B 應 enrolled 佔住名額，實際={rc_b.status}"
        # 此刻占容量 = 1（只有 B），A 已 inactive
        assert _occupying_count(s, course_id) == 1

    # restore A
    res = client.post(f"/api/activity/registrations/{reg_a_id}/restore")
    assert res.status_code == 200, res.text

    with sf() as s:
        # 核心斷言：占容量不得超過容量（修前 = 2 > 1 超賣，會 FAIL）
        occupying = _occupying_count(s, course_id)
        course = s.query(ActivityCourse).filter_by(id=course_id).one()
        assert (
            occupying <= course.capacity
        ), f"超賣！占容量={occupying} > 容量={course.capacity}"

        # A 的課程列應被降為 waitlist（B 已佔住唯一名額）
        rc_a = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_a_id, course_id=course_id)
            .one()
        )
        assert (
            rc_a.status == "waitlist"
        ), f"A 的課程列應降為 waitlist，實際={rc_a.status}"

        # B 仍正式
        rc_b = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_b_id, course_id=course_id)
            .one()
        )
        assert rc_b.status == "enrolled"

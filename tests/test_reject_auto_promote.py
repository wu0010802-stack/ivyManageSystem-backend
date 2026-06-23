"""
tests/test_reject_auto_promote.py — reject 後自動遞補候補（P3 修補回歸測試）

Bug：reject_registration 設 is_active=False 後直接 commit，不呼叫
_auto_promote_first_waitlist，導致釋出的名額不遞補候補。

修正後行為：reject 執行 flush（使 is_active=False 生效）後，對每門被拒報名
原本佔位（enrolled / promoted_pending）的課程呼叫 _auto_promote_first_waitlist。

測試使用 TestClient（對齊 test_activity_restore_capacity.py 模式），以覆蓋
完整 router → session commit 路徑。
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

# ------------------------------------------------------------------ #
# Fixture：獨立 SQLite DB + TestClient
# ------------------------------------------------------------------ #


@pytest.fixture
def reject_promo_client(tmp_path):
    db_path = tmp_path / "reject_promo.sqlite"
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


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _add_admin(session):
    session.add(
        User(
            username="admin",
            password_hash=hash_password("TempPass123"),
            role="admin",
            permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE", "STUDENTS_READ"],
            is_active=True,
        )
    )
    session.flush()


def _login(client):
    r = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "TempPass123"},
    )
    assert r.status_code == 200
    return r


def _seed_one_slot_course(session):
    """建立 admin、班級、兩名學生、一門 capacity=1 的課程。回傳 course_id。"""
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
        name="圍棋",
        price=1200,
        capacity=1,
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


# ------------------------------------------------------------------ #
# 核心測試：reject 後候補應自動遞補
# ------------------------------------------------------------------ #


def test_reject_promotes_waitlist(reject_promo_client):
    """容量=1，A 已 enrolled（佔滿），B 在候補。
    reject A 後 B 應自動升為 promoted_pending 並有 confirm_deadline。
    """
    client, sf = reject_promo_client
    with sf() as s:
        course_id = _seed_one_slot_course(s)

    # A 報名 → enrolled（唯一名額）
    ra = _register(client, name="王小明", birthday="2020-05-10", phone="0912345678")
    assert ra.status_code == 201, ra.text
    reg_a_id = ra.json()["id"]

    # B 報名 → waitlist（容量已滿）
    rb = _register(client, name="陳小美", birthday="2019-01-01", phone="0922222222")
    assert rb.status_code == 201, rb.text
    reg_b_id = rb.json()["id"]

    # 確認初始狀態：A=enrolled、B=waitlist
    with sf() as s:
        rc_a = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_a_id, course_id=course_id)
            .one()
        )
        rc_b = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_b_id, course_id=course_id)
            .one()
        )
        assert rc_a.status == "enrolled", f"前置失敗：A 應 enrolled，實際={rc_a.status}"
        assert rc_b.status == "waitlist", f"前置失敗：B 應 waitlist，實際={rc_b.status}"

    _login(client)

    # reject A
    rj = client.post(
        f"/api/activity/registrations/{reg_a_id}/reject",
        json={"reason": "非本園學生"},
    )
    assert rj.status_code == 200, rj.text

    # 核心斷言：B 應升為 promoted_pending 且有 confirm_deadline
    with sf() as s:
        rc_b = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_b_id, course_id=course_id)
            .one()
        )
        assert (
            rc_b.status == "promoted_pending"
        ), f"reject 後 B 應升為 promoted_pending，實際={rc_b.status}"
        assert rc_b.confirm_deadline is not None, "B 應有 confirm_deadline"
        assert rc_b.promoted_at is not None, "B 應有 promoted_at"


# ------------------------------------------------------------------ #
# 防迴歸：無候補時 reject 正常完成（不報錯）
# ------------------------------------------------------------------ #


def test_reject_no_waitlist_succeeds(reject_promo_client):
    """無候補時 reject 應正常完成，回傳 200，不拋例外。"""
    client, sf = reject_promo_client
    with sf() as s:
        _seed_one_slot_course(s)

    # 只有 A 報名，沒有候補
    ra = _register(client, name="王小明", birthday="2020-05-10", phone="0912345678")
    assert ra.status_code == 201, ra.text
    reg_a_id = ra.json()["id"]

    _login(client)

    rj = client.post(
        f"/api/activity/registrations/{reg_a_id}/reject",
        json={"reason": "測試無候補"},
    )
    assert rj.status_code == 200, rj.text
    assert rj.json()["registration_id"] == reg_a_id

"""tests/test_activity_public_update_lock_order_2026_06_24.py

Finding（2026-06-24 code review / P2）：public_update_registration 的鎖序與
confirm_waitlist_promotion / decline_waitlist_promotion 相反 → PostgreSQL 鎖順序
反轉（ABBA）死鎖。

- public_update_registration（家長改報名）：原本先鎖 ActivityRegistration 列、
  再鎖 ActivityCourse 列。
- confirm/decline_waitlist_promotion（2026-06-23 已統一）：先鎖 ActivityCourse、
  再鎖 RegistrationCourse + ActivityRegistration。

家長改報名與候補確認/放棄同時處理同一 (reg, course) 時形成循環等待。修正：
public_update 對齊全域 canonical 階層「advisory → ActivityCourse → ActivityRegistration」，
即先鎖 ActivityCourse 再鎖 ActivityRegistration。

測法沿用 test_activity_waitlist_confirm_lock_order_2026_06_23.py：spy
sqlalchemy Query.with_for_update 的呼叫順序，斷言 ActivityCourse 先於
ActivityRegistration 被鎖。SQLite 下 with_for_update 為 no-op 但仍會被呼叫，
故可驗證鎖『取得順序』。
"""

import os
import sys

import pytest
import sqlalchemy.orm
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
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationCourse,
)


@pytest.fixture
def public_update_client(tmp_path):
    db_path = tmp_path / "public-update-lock-order.sqlite"
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


def _setup(session):
    """建立 classroom + course + active matched registration（含 enrolled RC）。"""
    sy, sem = _term()
    classroom = Classroom(name="海豚班", is_active=True)
    session.add(classroom)
    session.flush()

    course = ActivityCourse(
        name="圍棋",
        price=5000,
        capacity=30,
        allow_waitlist=True,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()

    reg = ActivityRegistration(
        student_name="王小明",
        birthday="2020-01-01",
        class_name="海豚班",
        parent_phone="0912345678",
        paid_amount=0,
        is_paid=False,
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
            price_snapshot=5000,
        )
    )
    session.commit()
    return reg.id, course.id


def test_public_update_locks_course_before_registration(
    public_update_client, monkeypatch
):
    """家長改報名須先鎖 ActivityCourse 再鎖 ActivityRegistration（對齊
    confirm/decline_waitlist_promotion，消除 ABBA 鎖序反轉死鎖）。"""
    client, sf = public_update_client
    with sf() as s:
        reg_id, _course_id = _setup(s)

    recorded = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds:
            recorded.append(cds[0]["entity"])
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)

    res = client.post(
        "/api/activity/public/update",
        json={
            "id": reg_id,
            "name": "王小明",
            "birthday": "2020-01-01",
            "class": "海豚班",
            "parent_phone": "0912345678",
            "courses": [{"name": "圍棋"}],
            "supplies": [],
            "remark": "",
        },
    )
    assert res.status_code == 200, res.text

    locks = [e for e in recorded if e in (ActivityCourse, ActivityRegistration)]
    assert ActivityCourse in locks, f"未鎖 ActivityCourse；recorded={recorded}"
    assert (
        ActivityRegistration in locks
    ), f"未鎖 ActivityRegistration；recorded={recorded}"
    assert locks.index(ActivityCourse) < locks.index(
        ActivityRegistration
    ), f"鎖序錯誤：應先鎖 ActivityCourse 再鎖 ActivityRegistration，實際 {locks}"


def test_public_update_still_succeeds_after_reorder(public_update_client):
    """反回歸：重排鎖序後改報名本身仍正確（同價課程互換 → 200、RC 已換）。"""
    client, sf = public_update_client
    with sf() as s:
        reg_id, _course_id = _setup(s)
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
        course_names = {
            n
            for (n,) in s.query(ActivityCourse.name)
            .join(
                RegistrationCourse,
                RegistrationCourse.course_id == ActivityCourse.id,
            )
            .filter(RegistrationCourse.registration_id == reg_id)
            .all()
        }
        assert course_names == {"珠心算"}, f"課程未換成功：{course_names}"


def test_public_update_wrong_identity_rejected_without_locks(
    public_update_client, monkeypatch
):
    """身分不符（錯誤電話）須在取得任何列鎖前就回 403（預檢，避免持鎖）。"""
    client, sf = public_update_client
    with sf() as s:
        reg_id, _course_id = _setup(s)

    recorded = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds:
            recorded.append(cds[0]["entity"])
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)

    res = client.post(
        "/api/activity/public/update",
        json={
            "id": reg_id,
            "name": "王小明",
            "birthday": "2020-01-01",
            "class": "海豚班",
            "parent_phone": "0900000000",  # 錯誤電話
            "courses": [{"name": "圍棋"}],
            "supplies": [],
            "remark": "",
        },
    )
    assert res.status_code == 403, res.text
    locks = [e for e in recorded if e in (ActivityCourse, ActivityRegistration)]
    assert locks == [], f"身分不符不應持有任何列鎖，實際 {locks}"

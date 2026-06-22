"""tests/test_activity_waitlist_lock_order_2026_06_22.py

Finding 3（code review）：候補升位的兩條路徑鎖序不一致 →
PostgreSQL 鎖順序反轉死鎖。
- 手動升位 promote_waitlist：先鎖 RegistrationCourse(+ActivityRegistration)、
  再鎖 ActivityCourse。
- 自動遞補 _auto_promote_first_waitlist：先鎖 ActivityCourse、再鎖
  RegistrationCourse。
兩者同時處理首位候補時形成循環等待。

修正：所有路徑統一「course → registration_course」順序（手動端對齊自動端）。

測法：spy sqlalchemy Query.with_for_update 的呼叫順序（記錄被鎖的主 entity），
斷言 ActivityCourse 先於 RegistrationCourse 被鎖。SQLite 下 with_for_update 為
no-op，但仍會被呼叫，故可驗證鎖『取得順序』。
"""

import os
import sys

import pytest
import sqlalchemy.orm
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    RegistrationCourse,
)
from services.activity_service import activity_service
from utils.academic import resolve_current_academic_term


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'lock_order.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    s = sf()
    yield s
    s.close()
    engine.dispose()


def _seed_waitlist(s):
    sy, sem = resolve_current_academic_term()
    course = ActivityCourse(
        name="圍棋",
        price=1000,
        sessions=10,
        capacity=30,
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    s.add(course)
    s.flush()
    reg = ActivityRegistration(
        student_name="王小明",
        birthday="2020-01-01",
        class_name="大班",
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    s.add(reg)
    s.flush()
    s.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="waitlist",
            price_snapshot=1000,
        )
    )
    s.flush()
    return reg.id, course.id


def test_promote_waitlist_locks_course_before_registration_course(
    db_session, monkeypatch
):
    """手動升位須先鎖 ActivityCourse 再鎖 RegistrationCourse（對齊自動遞補，
    消除 ABBA 鎖序反轉死鎖）。"""
    reg_id, course_id = _seed_waitlist(db_session)

    recorded = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds:
            recorded.append(cds[0]["entity"])
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)

    activity_service.promote_waitlist(db_session, reg_id, course_id)

    # 只看這兩個 entity 的取得順序
    locks = [e for e in recorded if e in (ActivityCourse, RegistrationCourse)]
    assert ActivityCourse in locks, f"未鎖 ActivityCourse；recorded={recorded}"
    assert RegistrationCourse in locks, f"未鎖 RegistrationCourse；recorded={recorded}"
    assert locks.index(ActivityCourse) < locks.index(
        RegistrationCourse
    ), f"鎖序錯誤：應先鎖 ActivityCourse 再鎖 RegistrationCourse，實際 {locks}"


def test_promote_waitlist_still_promotes(db_session):
    """反回歸：重排鎖序後升位本身仍正確（waitlist → enrolled）。"""
    reg_id, course_id = _seed_waitlist(db_session)
    student_name, course_name = activity_service.promote_waitlist(
        db_session, reg_id, course_id
    )
    assert student_name == "王小明"
    assert course_name == "圍棋"
    rc = (
        db_session.query(RegistrationCourse)
        .filter(
            RegistrationCourse.registration_id == reg_id,
            RegistrationCourse.course_id == course_id,
        )
        .first()
    )
    assert rc.status == "enrolled"
    assert rc.confirm_deadline is None

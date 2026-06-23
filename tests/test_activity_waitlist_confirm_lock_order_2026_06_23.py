"""tests/test_activity_waitlist_confirm_lock_order_2026_06_23.py

Finding（2026-06-23 audit / P2）：confirm_waitlist_promotion 的鎖序與
promote_waitlist / decline_waitlist_promotion / _auto_promote_first_waitlist
相反 → PostgreSQL 鎖順序反轉（ABBA）死鎖。

- confirm_waitlist_promotion（家長確認）：先鎖 RegistrationCourse、再鎖
  ActivityCourse。
- 其餘三條路徑（2026-06-22 已統一）：先鎖 ActivityCourse、再鎖
  RegistrationCourse。

家長確認與管理員手動升位（promote_waitlist）同時處理同一 (reg, course) 時形成
循環等待。修正：confirm 對齊「course → registration_course」順序。

測法沿用 test_activity_waitlist_lock_order_2026_06_22.py：spy
sqlalchemy Query.with_for_update 的呼叫順序，斷言 ActivityCourse 先於
RegistrationCourse 被鎖。SQLite 下 with_for_update 為 no-op 但仍會被呼叫，
故可驗證鎖『取得順序』。
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
        f"sqlite:///{tmp_path / 'confirm_lock_order.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine)
    s = sf()
    yield s
    s.close()
    engine.dispose()


def _seed_promoted_pending(s):
    """建立一筆 promoted_pending（候補轉正待確認）的報名課程。"""
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
            status="promoted_pending",
            price_snapshot=1000,
        )
    )
    s.flush()
    return reg.id, course.id


def test_confirm_waitlist_promotion_locks_course_before_registration_course(
    db_session, monkeypatch
):
    """家長確認轉正須先鎖 ActivityCourse 再鎖 RegistrationCourse（對齊
    promote_waitlist / decline / _auto_promote，消除 ABBA 鎖序反轉死鎖）。"""
    reg_id, course_id = _seed_promoted_pending(db_session)

    recorded = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds:
            recorded.append(cds[0]["entity"])
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)

    activity_service.confirm_waitlist_promotion(db_session, reg_id, course_id)

    locks = [e for e in recorded if e in (ActivityCourse, RegistrationCourse)]
    assert ActivityCourse in locks, f"未鎖 ActivityCourse；recorded={recorded}"
    assert RegistrationCourse in locks, f"未鎖 RegistrationCourse；recorded={recorded}"
    assert locks.index(ActivityCourse) < locks.index(
        RegistrationCourse
    ), f"鎖序錯誤：應先鎖 ActivityCourse 再鎖 RegistrationCourse，實際 {locks}"


def test_confirm_waitlist_promotion_still_confirms(db_session):
    """反回歸：重排鎖序後確認本身仍正確（promoted_pending → enrolled）。"""
    reg_id, course_id = _seed_promoted_pending(db_session)
    student_name, course_name = activity_service.confirm_waitlist_promotion(
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

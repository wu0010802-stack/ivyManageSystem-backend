"""tests/test_activity_auto_promote_lock_scope_2026_07_01.py

A-1（2026-07-01 才藝 bug hunt）：`ActivityService._auto_promote_first_waitlist`
的候補選取查詢以 join 上的裸 `FOR UPDATE`（無 of=）鎖定，PostgreSQL 會連同候補者的
`activity_registrations` 列一起鎖。這把 AR 列鎖不在既有 canonical 鎖序協議
（advisory → activity_courses → registration_courses）內；跨課交叉候補的並發
刪除/退課（各自 `_auto_promote` 對方課程）可在兩張 AR 列上形成 A↔B 循環等待 →
ABBA 死鎖（PG 40P01 abort 其一）。

修法：`with_for_update(of=RegistrationCourse)` 收斂鎖範圍到 registration_courses
（AR 欄位僅供讀取，不需鎖）。

SQLite 的 `with_for_update` 為 no-op、無法重現行鎖，故本測以 PostgreSQL dialect
編譯候補選取語句，斷言其 FOR UPDATE 子句帶 `OF registration_courses`（而非裸
FOR UPDATE 連鎖 activity_registrations）。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    RegistrationCourse,
)
from services.activity_service import activity_service


@pytest.fixture
def promote_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'promote-lock.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        base_module._engine = old_engine
        base_module._SessionFactory = old_sf
        engine.dispose()


def _pg_sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_auto_promote_locks_only_registration_course_not_registration(
    promote_session,
):
    session = promote_session
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    # capacity=1、0 佔位、1 位候補 → 通過容量檢查後會跑候補選取（發出 FOR UPDATE）。
    course = ActivityCourse(
        name="圍棋",
        price=0,
        capacity=1,
        allow_waitlist=True,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course)
    session.flush()
    reg = ActivityRegistration(
        student_name="候補生",
        birthday="2020-01-01",
        class_name="大班",
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
            status="waitlist",
            price_snapshot=0,
        )
    )
    session.commit()

    captured: list[str] = []

    @event.listens_for(session, "do_orm_execute")
    def _capture(state):
        try:
            sql = _pg_sql(state.statement).lower()
        except Exception:
            return
        if "from registration_courses" in sql and "for update" in sql:
            captured.append(sql)

    activity_service._auto_promote_first_waitlist(session, course.id)

    assert captured, "應對候補 registration_courses 列發出 FOR UPDATE 選取"
    lock_sql = captured[-1]
    assert "for update of registration_courses" in lock_sql, (
        "候補選取的 FOR UPDATE 須以 of= 收斂到 registration_courses，避免連鎖鎖定 "
        "activity_registrations 列（ABBA 死鎖來源）。實際 SQL：\n" + lock_sql
    )

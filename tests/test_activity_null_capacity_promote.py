"""
tests/test_activity_null_capacity_promote.py — 容量 NULL 口徑漂移回歸測試

Bug: capacity=NULL 時，候補升正式的兩條路徑（_auto_promote_first_waitlist /
promote_waitlist）把 NULL 視為「無上限」，但報名端五處一律用 30 作為預設值。
這導致報名端在第 31 人後壓成候補，但任何釋位/sweep 觸發升正式時無上限地把
所有候補升上去，實際佔位遠超 30。

修正：兩處對齊報名端 inline idiom：
    capacity = course.capacity if course.capacity is not None else 30

使用 SQLite in-memory，不依賴 dev DB。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)
from services.activity_service import ActivityService

# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def svc():
    return ActivityService()


def _add_course(
    session, name="測試課程", capacity=None, allow_waitlist=True
) -> ActivityCourse:
    """建立課程，capacity 預設為 None（NULL）。

    注意：ActivityCourse.capacity 欄位有 SQLAlchemy column-level default=30，
    因此必須在 flush 之後再明確設為 None，否則預設值會覆蓋 None。
    """
    c = ActivityCourse(name=name, price=1000, allow_waitlist=allow_waitlist)
    session.add(c)
    session.flush()
    # 在 flush 後明確設 capacity，繞過 column default
    c.capacity = capacity
    session.flush()
    return c


def _add_reg(
    session, student_name="測試學生", parent_phone="0912345678"
) -> ActivityRegistration:
    r = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name="大班",
        parent_phone=parent_phone,
        is_paid=False,
        is_active=True,
    )
    session.add(r)
    session.flush()
    return r


def _enroll(session, reg_id, course_id, status="enrolled") -> RegistrationCourse:
    rc = RegistrationCourse(
        registration_id=reg_id,
        course_id=course_id,
        status=status,
        price_snapshot=1000,
    )
    session.add(rc)
    session.flush()
    return rc


# ------------------------------------------------------------------ #
# (a) _auto_promote_first_waitlist：capacity=NULL 時應視為上限 30
# ------------------------------------------------------------------ #


class TestAutoPromoteNullCapacity:
    def test_auto_promote_does_not_exceed_30_when_capacity_is_null(self, session, svc):
        """
        capacity=NULL 的課程，已有 30 筆 OCCUPYING（enrolled + promoted_pending），
        尚有 waitlist 時，_auto_promote_first_waitlist 不應把第 31 筆升上去。

        修前：NULL capacity 跳過閘，第 31 筆被升成 promoted_pending。
        修後：NULL 視為 30，occupying(30) >= 30 → early-return，waitlist 維持不動。
        """
        course = _add_course(session, capacity=None)

        # 建立 30 筆佔位（enrolled）
        for i in range(30):
            reg = _add_reg(session, student_name=f"佔位{i}")
            _enroll(session, reg.id, course.id, status="enrolled")

        # 第 31 位掛候補
        reg_waitlist = _add_reg(session, student_name="候補第一位")
        rc_wait = _enroll(session, reg_waitlist.id, course.id, status="waitlist")

        # 觸發自動升位
        svc._auto_promote_first_waitlist(session, course.id)
        session.flush()

        # 候補應維持 waitlist，不能被升為 promoted_pending
        assert rc_wait.status == "waitlist", (
            f"capacity=NULL 時 _auto_promote_first_waitlist 未正確限制容量上限 30，"
            f"第 31 筆被錯誤升為 {rc_wait.status}"
        )

        # 確認佔位總數仍為 30
        occupying_count = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.course_id == course.id,
                RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
            )
            .count()
        )
        assert occupying_count == 30, f"佔位數應維持 30，實際為 {occupying_count}"

    def test_auto_promote_still_works_when_below_30_null_capacity(self, session, svc):
        """
        capacity=NULL 的課，已有 29 筆佔位時，_auto_promote_first_waitlist
        應正常把第一位候補升為 promoted_pending（不超過 30 上限）。
        """
        course = _add_course(session, capacity=None)

        # 29 筆佔位
        for i in range(29):
            reg = _add_reg(session, student_name=f"佔位{i}")
            _enroll(session, reg.id, course.id, status="enrolled")

        reg_waitlist = _add_reg(session, student_name="候補應被升")
        rc_wait = _enroll(session, reg_waitlist.id, course.id, status="waitlist")

        svc._auto_promote_first_waitlist(session, course.id)
        session.flush()

        # 29 < 30，應可升位
        assert rc_wait.status == "promoted_pending", (
            f"capacity=NULL 且已有 29 佔位時，候補應被升為 promoted_pending，"
            f"但狀態為 {rc_wait.status}"
        )


# ------------------------------------------------------------------ #
# (b) promote_waitlist：capacity=NULL 時應視為上限 30
# ------------------------------------------------------------------ #


class TestPromoteWaitlistNullCapacity:
    def test_promote_waitlist_raises_when_occupying_others_reaches_30_null_capacity(
        self, session, svc
    ):
        """
        capacity=NULL 的課程，其他人（非此列）佔位數已達 30 時，
        對 waitlist 呼叫 promote_waitlist 應 raise ValueError（容量已滿）。

        修前：NULL capacity 跳過閘，waitlist 被錯誤升為 enrolled。
        修後：NULL 視為 30，occupying_others(30) >= 30 → raise ValueError。
        """
        course = _add_course(session, capacity=None)

        # 30 筆其他佔位（enrolled）
        for i in range(30):
            reg = _add_reg(session, student_name=f"佔位{i}")
            _enroll(session, reg.id, course.id, status="enrolled")

        # 第 31 位掛候補
        reg_waitlist = _add_reg(session, student_name="候補欲升")
        rc_wait = _enroll(session, reg_waitlist.id, course.id, status="waitlist")

        with pytest.raises(ValueError, match="容量已滿"):
            svc.promote_waitlist(session, reg_waitlist.id, course.id)

        # 確認狀態未被改動
        session.refresh(rc_wait)
        assert (
            rc_wait.status == "waitlist"
        ), f"promote_waitlist 應拋 ValueError 且不改狀態，但狀態變為 {rc_wait.status}"

    def test_promote_waitlist_succeeds_when_occupying_others_below_30_null_capacity(
        self, session, svc
    ):
        """
        capacity=NULL 的課程，其他佔位 29 時，promote_waitlist 應正常升位（不超過 30）。
        """
        course = _add_course(session, capacity=None)

        # 29 筆其他佔位
        for i in range(29):
            reg = _add_reg(session, student_name=f"佔位{i}")
            _enroll(session, reg.id, course.id, status="enrolled")

        reg_waitlist = _add_reg(session, student_name="候補應成功升")
        rc_wait = _enroll(session, reg_waitlist.id, course.id, status="waitlist")

        # 不應拋例外
        svc.promote_waitlist(session, reg_waitlist.id, course.id)
        session.flush()

        session.refresh(rc_wait)
        assert rc_wait.status == "enrolled", (
            f"capacity=NULL 且 occupying_others=29 時應成功升為 enrolled，"
            f"但狀態為 {rc_wait.status}"
        )

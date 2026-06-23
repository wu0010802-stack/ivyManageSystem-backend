"""sync_registrations_on_student_deactivate SAVEPOINT 回歸測試。

當批次軟刪過程中某筆失敗時：
- 不應汙染 SQLAlchemy session（後續筆仍可寫入）
- 失敗那筆的 is_active 必須維持原值（SAVEPOINT rollback）
- 其他筆正常完成軟刪
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def sqlite_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models.database import Base
    from models.academic_term import (
        AcademicTerm,
    )  # 註冊到 Base.metadata 以建 academic_terms 表  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    session = SessionFactory()
    try:
        yield engine, session
    finally:
        session.close()


def _seed_three_regs(session):
    from models.database import ActivityRegistration, Classroom, Student

    classroom = Classroom(name="班A", is_active=True)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id="S100",
        name="王小明",
        birthday=date(2020, 5, 10),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    regs = []
    for i in range(3):
        r = ActivityRegistration(
            student_name=f"王小明{i}",
            class_name="班A",
            classroom_id=classroom.id,
            school_year=sy,
            semester=sem,
            student_id=student.id,
            is_active=True,
            paid_amount=0,
            match_status="matched",
            pending_review=False,
        )
        session.add(r)
        regs.append(r)
    session.commit()
    return student.id, [r.id for r in regs]


class TestPartialFailureSavepoint:
    def test_failure_on_one_reg_does_not_corrupt_others(
        self, monkeypatch, sqlite_session
    ):
        from services import activity_student_sync as ass
        from models.database import ActivityRegistration

        _engine, session = sqlite_session
        student_id, reg_ids = _seed_three_regs(session)
        failing_id = reg_ids[1]

        original = ass._soft_delete_single_registration

        def patched(session, reg, **kwargs):
            if reg.id == failing_id:
                raise RuntimeError("人為失敗")
            return original(session, reg, **kwargs)

        monkeypatch.setattr(ass, "_soft_delete_single_registration", patched)

        deleted = ass.sync_registrations_on_student_deactivate(session, student_id)

        # 兩筆成功軟刪
        assert deleted == 2, f"預期 2 筆成功，實際 {deleted}"

        # 重撈確認狀態
        session.expire_all()
        statuses = {
            r.id: r.is_active
            for r in session.query(ActivityRegistration)
            .filter(ActivityRegistration.id.in_(reg_ids))
            .all()
        }
        assert statuses[reg_ids[0]] is False
        assert (
            statuses[failing_id] is True
        ), "SAVEPOINT 未生效：失敗那筆的 is_active 應該維持 True"
        assert statuses[reg_ids[2]] is False

    def test_all_success_returns_full_count(self, sqlite_session):
        from services import activity_student_sync as ass

        _engine, session = sqlite_session
        student_id, _ = _seed_three_regs(session)

        deleted = ass.sync_registrations_on_student_deactivate(session, student_id)
        assert deleted == 3


def _seed_one_student_with_course_reg(session, student_name, course_id, status):
    """為某學生建一筆當學期啟用報名，並掛一筆指定狀態的 RegistrationCourse。

    回傳 (student_id, registration_id, registration_course_id)。
    """
    from models.database import (
        ActivityRegistration,
        Classroom,
        RegistrationCourse,
        Student,
    )
    from utils.academic import resolve_current_academic_term

    classroom = session.query(Classroom).filter(Classroom.name == "班A").first()
    if classroom is None:
        classroom = Classroom(name="班A", is_active=True)
        session.add(classroom)
        session.flush()

    student = Student(
        student_id=f"S-{student_name}",
        name=student_name,
        birthday=date(2020, 5, 10),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    sy, sem = resolve_current_academic_term()
    reg = ActivityRegistration(
        student_name=student_name,
        class_name="班A",
        classroom_id=classroom.id,
        school_year=sy,
        semester=sem,
        student_id=student.id,
        is_active=True,
        paid_amount=0,
        match_status="matched",
        pending_review=False,
    )
    session.add(reg)
    session.flush()

    rc = RegistrationCourse(
        registration_id=reg.id,
        course_id=course_id,
        status=status,
        price_snapshot=1000,
    )
    session.add(rc)
    session.flush()
    return student.id, reg.id, rc.id


class TestDeactivateTriggersWaitlistPromotion:
    """學生離園/退學軟刪報名後，釋出的名額應自動遞補候補。

    對比 delete_registration：軟刪佔位報名後須對每門課呼叫
    _auto_promote_first_waitlist，否則名額空出但候補卡死。
    """

    def test_deactivate_enrolled_promotes_waitlist(self, sqlite_session):
        from models.database import ActivityCourse, RegistrationCourse
        from services import activity_student_sync as ass

        _engine, session = sqlite_session

        # 一門 capacity=1 課程
        course = ActivityCourse(
            name="美術", price=1000, capacity=1, allow_waitlist=True
        )
        session.add(course)
        session.flush()

        # student_a enrolled（佔位）、student_b waitlist（候補）
        student_a_id, _reg_a_id, _rc_a_id = _seed_one_student_with_course_reg(
            session, "甲生", course.id, "enrolled"
        )
        _student_b_id, _reg_b_id, rc_b_id = _seed_one_student_with_course_reg(
            session, "乙生", course.id, "waitlist"
        )
        session.commit()

        # 甲生離園 → 軟刪其報名，應遞補乙生候補
        ass.sync_registrations_on_student_deactivate(session, student_a_id)
        session.commit()

        session.expire_all()
        rc_b = (
            session.query(RegistrationCourse)
            .filter(RegistrationCourse.id == rc_b_id)
            .first()
        )
        assert (
            rc_b.status == "promoted_pending"
        ), "甲生離園軟刪後，名額應自動遞補乙生候補（修前仍為 waitlist）"
        assert rc_b.promoted_at is not None
        assert rc_b.confirm_deadline is not None


def _seed_regs_across_terms(session):
    """同一學生在 過去 / 當前 / 未來 學期各一筆 active 報名。

    回 (student_id, {"past": id, "current": id, "future": id})。
    past = (當前學年-1)，future = (當前學年+1)，與 sem 值無關皆嚴格早於/晚於當前。
    """
    from models.database import ActivityRegistration, Classroom, Student
    from utils.academic import resolve_current_academic_term

    classroom = Classroom(name="班T", is_active=True)
    session.add(classroom)
    session.flush()
    student = Student(
        student_id="ST-term",
        name="期生",
        birthday=date(2020, 1, 1),
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    sy, sem = resolve_current_academic_term()
    terms = {"past": (sy - 1, sem), "current": (sy, sem), "future": (sy + 1, sem)}
    ids: dict[str, int] = {}
    for label, (y, s) in terms.items():
        r = ActivityRegistration(
            student_name="期生",
            class_name="班T",
            classroom_id=classroom.id,
            school_year=y,
            semester=s,
            student_id=student.id,
            is_active=True,
            paid_amount=0,
            match_status="matched",
            pending_review=False,
        )
        session.add(r)
        session.flush()
        ids[label] = r.id
    session.commit()
    return student.id, ids


class TestDeactivateTermScope:
    """學生離園應取消「當前學期及之後」的 active 報名，歷史學期保留供追溯。"""

    def test_deactivate_cancels_current_and_future_keeps_past(self, sqlite_session):
        from models.database import ActivityRegistration
        from services import activity_student_sync as ass

        _engine, session = sqlite_session
        student_id, ids = _seed_regs_across_terms(session)

        ass.sync_registrations_on_student_deactivate(session, student_id)
        session.commit()
        session.expire_all()

        active = {
            r.id: r.is_active
            for r in session.query(ActivityRegistration)
            .filter(ActivityRegistration.id.in_(list(ids.values())))
            .all()
        }
        assert active[ids["current"]] is False, "當前學期應被軟刪"
        assert (
            active[ids["future"]] is False
        ), "未來學期 active 報名應一併軟刪（修前漏刪）"
        assert active[ids["past"]] is True, "歷史學期報名應保留供追溯"

    def test_deactivate_cancels_null_term_active_reg(self, sqlite_session):
        """異常資料：NULL-term（school_year/semester 為 NULL）的 active 報名也應在
        離園時取消，避免幽靈名額/未沖帳金額殘留（修前被學期條件靜默排除）。"""
        from models.database import ActivityRegistration, Classroom, Student
        from services import activity_student_sync as ass

        _engine, session = sqlite_session
        classroom = Classroom(name="班N", is_active=True)
        session.add(classroom)
        session.flush()
        student = Student(
            student_id="ST-null",
            name="無期生",
            birthday=date(2020, 1, 1),
            classroom_id=classroom.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        reg = ActivityRegistration(
            student_name="無期生",
            class_name="班N",
            classroom_id=classroom.id,
            school_year=None,
            semester=None,
            student_id=student.id,
            is_active=True,
            paid_amount=0,
            match_status="matched",
            pending_review=False,
        )
        session.add(reg)
        session.flush()
        reg_id = reg.id
        session.commit()

        ass.sync_registrations_on_student_deactivate(session, student.id)
        session.commit()
        session.expire_all()
        after = session.query(ActivityRegistration).get(reg_id)
        assert after.is_active is False, "NULL-term active 報名應被軟刪（修前漏刪）"

    def test_deactivate_cancels_school_year_set_but_semester_null(self, sqlite_session):
        """partial-null：school_year 有值但 semester 為 NULL 的 active 報名也應軟刪。

        歷史 migration（20260417 add_activity_academic_term）回填用的是
        `school_year IS NULL OR semester IS NULL`，故 legacy/修復中資料可能只有
        semester 為 NULL。修前 _deactivate_term_filter 只含 school_year IS NULL，
        漏掉「sy 有值、sem NULL」這型 → 學生離園後仍 active，殘留幽靈名額/金額。
        """
        from models.database import ActivityRegistration, Classroom, Student
        from services import activity_student_sync as ass
        from utils.academic import resolve_current_academic_term

        _engine, session = sqlite_session
        sy, _sem = resolve_current_academic_term()
        classroom = Classroom(name="班P", is_active=True)
        session.add(classroom)
        session.flush()
        student = Student(
            student_id="ST-partial",
            name="半期生",
            birthday=date(2020, 1, 1),
            classroom_id=classroom.id,
            is_active=True,
        )
        session.add(student)
        session.flush()
        reg = ActivityRegistration(
            student_name="半期生",
            class_name="班P",
            classroom_id=classroom.id,
            school_year=sy,  # 有值（當前學年）
            semester=None,  # 但 semester 為 NULL（partial-null）
            student_id=student.id,
            is_active=True,
            paid_amount=0,
            match_status="matched",
            pending_review=False,
        )
        session.add(reg)
        session.flush()
        reg_id = reg.id
        session.commit()

        ass.sync_registrations_on_student_deactivate(session, student.id)
        session.commit()
        session.expire_all()
        after = session.query(ActivityRegistration).get(reg_id)
        assert (
            after.is_active is False
        ), "school_year 有值但 semester NULL 的 active 報名應被軟刪（修前漏刪）"


class TestDeactivateRereadsPaidUnderLock:
    """離園同步自動沖帳必須用『鎖內重讀』的 paid_amount，而非同步流程早先讀到的舊值。

    模擬並發 POS 收款：同步流程先把 reg 載入 session（paid=0），之後 DB 被外帶
    更新為 3000（raw SQL，不同步 ORM 物件 → identity-map 物件維持 stale 0）。
    修前：deactivate 用 stale 0 → 不寫沖帳，幽靈付款 3000 留存。
    修後：populate_existing 重讀 → 寫 3000 自動沖帳。
    """

    def test_deactivate_uses_fresh_paid_amount(self, sqlite_session):
        from sqlalchemy import text

        from models.database import ActivityPaymentRecord, ActivityRegistration
        from services import activity_student_sync as ass

        _engine, session = sqlite_session
        student_id, reg_ids = _seed_three_regs(session)
        target = reg_ids[0]

        # 同步流程「先讀到 paid=0」：載入 identity map
        reg = session.get(ActivityRegistration, target)
        assert (reg.paid_amount or 0) == 0

        # 並發 POS 收款：DB 外帶更新（raw SQL 不同步 ORM；物件仍 stale）
        session.execute(
            text("UPDATE activity_registrations SET paid_amount=3000 WHERE id=:i"),
            {"i": target},
        )
        assert (reg.paid_amount or 0) == 0, "前置條件：identity-map 物件仍為舊值"

        ass.sync_registrations_on_student_deactivate(session, student_id)
        session.flush()

        refund = (
            session.query(ActivityPaymentRecord)
            .filter(
                ActivityPaymentRecord.registration_id == target,
                ActivityPaymentRecord.type == "refund",
            )
            .first()
        )
        assert (
            refund is not None
        ), "鎖內應重讀到 3000 並寫自動沖帳退費（修前用 stale 0 不寫）"
        assert refund.amount == 3000
        session.expire_all()
        reg_after = session.get(ActivityRegistration, target)
        assert (reg_after.paid_amount or 0) == 0, "沖帳後 paid_amount 應歸零"

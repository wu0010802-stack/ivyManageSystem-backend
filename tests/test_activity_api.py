"""
tests/test_activity_api.py — 才藝系統 API 測試（SQLite in-memory）

涵蓋：
- TestDerivePaymentStatus：純邏輯，四態衍生
- TestBatchCalcTotalAmounts：N+1 修正的批次計算
- TestBatchPayment：批次標記已/未繳費
- TestSinglePayment：新增繳費、退費、刪除記錄後重新計算
- TestRegistrationList：分頁 + payment_status 篩選
- TestCourseAPI：重複名稱 400 / 刪除有報名者 409
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
    ActivityRegistrationSettings,
    ActivityPaymentRecord,
    ActivitySupply,
    ActivitySession,
    ActivityAttendance,
    RegistrationCourse,
    RegistrationSupply,
    RegistrationChange,
)
from models.database import Classroom
from api.activity._shared import (
    _derive_payment_status,
    _batch_calc_total_amounts,
    _check_registration_open,
    _attach_courses,
    _attach_supplies,
    _build_session_detail_response,
    PublicCourseItem,
    PublicSupplyItem,
)

# ────────────────────────────────────────────────────────────────── #
# Fixtures
# ────────────────────────────────────────────────────────────────── #


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


def _add_course(session, name="美術", price=1000, capacity=30) -> ActivityCourse:
    c = ActivityCourse(name=name, price=price, capacity=capacity, allow_waitlist=True)
    session.add(c)
    session.flush()
    return c


def _add_reg(session, student="王小明", cls="大班") -> ActivityRegistration:
    r = ActivityRegistration(
        student_name=student,
        birthday="2020-01-01",
        class_name=cls,
        is_paid=False,
        paid_amount=0,
        is_active=True,
    )
    session.add(r)
    session.flush()
    return r


def _enroll(
    session, reg_id: int, course_id: int, price: int = 1000, status: str = "enrolled"
) -> RegistrationCourse:
    rc = RegistrationCourse(
        registration_id=reg_id,
        course_id=course_id,
        status=status,
        price_snapshot=price,
    )
    session.add(rc)
    session.flush()
    return rc


def _add_payment_record(
    session, reg_id: int, type_: str, amount: int
) -> ActivityPaymentRecord:
    from datetime import date

    rec = ActivityPaymentRecord(
        registration_id=reg_id,
        type=type_,
        amount=amount,
        payment_date=date.today(),
        payment_method="現金",
        notes="",
        operator="test",
    )
    session.add(rec)
    session.flush()
    return rec


# ────────────────────────────────────────────────────────────────── #
# P7-1：TestDerivePaymentStatus（純邏輯，無 DB）
# ────────────────────────────────────────────────────────────────── #


class TestDerivePaymentStatus:
    def test_unpaid_when_paid_amount_zero(self):
        assert _derive_payment_status(0, 1000) == "unpaid"

    def test_partial_when_paid_less_than_total(self):
        assert _derive_payment_status(500, 1000) == "partial"

    def test_paid_when_amounts_equal(self):
        assert _derive_payment_status(1000, 1000) == "paid"

    def test_overpaid_when_paid_exceeds_total(self):
        assert _derive_payment_status(1200, 1000) == "overpaid"

    def test_paid_when_both_zero(self):
        """total=0 且 paid=0 應視為已繳費（無需繳費）"""
        assert _derive_payment_status(0, 0) == "paid"

    def test_overpaid_when_total_zero_but_paid_nonzero(self):
        """total=0 但 paid>0，視為超繳（退費後仍有餘額）"""
        assert _derive_payment_status(100, 0) == "overpaid"


# ────────────────────────────────────────────────────────────────── #
# P7-1b：TestBatchCalcTotalAmounts（P3 N+1 修正）
# ────────────────────────────────────────────────────────────────── #


class TestBatchCalcTotalAmounts:
    def test_returns_correct_totals(self, session):
        """批次計算多筆報名應繳金額正確"""
        course = _add_course(session, price=500)
        reg1 = _add_reg(session, "學生A")
        reg2 = _add_reg(session, "學生B")
        _enroll(session, reg1.id, course.id, price=500)
        _enroll(session, reg2.id, course.id, price=500)
        session.commit()

        result = _batch_calc_total_amounts(session, [reg1.id, reg2.id])
        assert result[reg1.id] == 500
        assert result[reg2.id] == 500

    def test_waitlist_not_counted(self, session):
        """候補課程不計入應繳金額"""
        course = _add_course(session, price=800)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, price=800, status="waitlist")
        session.commit()

        result = _batch_calc_total_amounts(session, [reg.id])
        assert result[reg.id] == 0

    def test_empty_reg_ids_returns_empty(self, session):
        """空清單回傳空 dict"""
        result = _batch_calc_total_amounts(session, [])
        assert result == {}

    def test_includes_supply_amount(self, session):
        """包含用品金額"""
        from models.activity import ActivitySupply, RegistrationSupply

        course = _add_course(session, price=500)
        supply = ActivitySupply(name="畫筆", price=200)
        session.add(supply)
        session.flush()
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, price=500)
        rs = RegistrationSupply(
            registration_id=reg.id,
            supply_id=supply.id,
            price_snapshot=200,
        )
        session.add(rs)
        session.commit()

        result = _batch_calc_total_amounts(session, [reg.id])
        assert result[reg.id] == 700  # 課程 500 + 用品 200


# ────────────────────────────────────────────────────────────────── #
# P7-2：TestBatchPayment（整合，模擬 batch_update_payment 邏輯）
# ────────────────────────────────────────────────────────────────── #


class TestBatchPayment:
    def test_batch_mark_paid_updates_paid_amount(self, session):
        """批次標記已繳費後 paid_amount 應補齊至 total_amount"""
        course = _add_course(session, price=1000)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, price=1000)
        session.commit()

        # 模擬 batch_update_payment 邏輯
        from api.activity._shared import _batch_calc_total_amounts

        total_map = _batch_calc_total_amounts(session, [reg.id])
        total_amount = total_map[reg.id]
        shortfall = total_amount - (reg.paid_amount or 0)
        assert shortfall == 1000

        rec = _add_payment_record(session, reg.id, "payment", shortfall)
        reg.paid_amount = total_amount
        reg.is_paid = True
        session.commit()

        session.refresh(reg)
        assert reg.paid_amount == 1000
        assert reg.is_paid is True

    # 舊的 test_batch_mark_unpaid_clears_paid_amount 已移除：
    # 原測試用 raw DELETE 模擬「標記未繳費清空記錄」的過時行為；實際端點已改為
    # 寫 refund 沖帳保留歷史。端點級回歸測試見
    # tests/test_activity_fee_fixes.py::TestMarkUnpaidWritesRefund。


# ────────────────────────────────────────────────────────────────── #
# P7-3：TestSinglePayment（整合，模擬 add/delete payment 邏輯）
# ────────────────────────────────────────────────────────────────── #


class TestSinglePayment:
    def test_add_payment_increases_paid_amount(self, session):
        """新增繳費後 paid_amount 增加"""
        course = _add_course(session, price=1000)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, price=1000)
        session.commit()

        _add_payment_record(session, reg.id, "payment", 500)
        reg.paid_amount = (reg.paid_amount or 0) + 500
        session.commit()

        session.refresh(reg)
        assert reg.paid_amount == 500

    def test_add_refund_decreases_paid_amount(self, session):
        """新增退費後 paid_amount 減少"""
        course = _add_course(session, price=1000)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, price=1000)
        _add_payment_record(session, reg.id, "payment", 1000)
        reg.paid_amount = 1000
        reg.is_paid = True
        session.commit()

        _add_payment_record(session, reg.id, "refund", 300)
        reg.paid_amount = max(0, reg.paid_amount - 300)
        session.commit()

        session.refresh(reg)
        assert reg.paid_amount == 700

    def test_delete_payment_recalculates(self, session):
        """刪除繳費記錄後重新計算正確"""
        from sqlalchemy import func

        course = _add_course(session, price=1000)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, price=1000)
        rec1 = _add_payment_record(session, reg.id, "payment", 600)
        rec2 = _add_payment_record(session, reg.id, "payment", 400)
        reg.paid_amount = 1000
        reg.is_paid = True
        session.commit()

        # 刪除第一筆
        session.delete(rec1)
        session.flush()

        totals = (
            session.query(
                ActivityPaymentRecord.type, func.sum(ActivityPaymentRecord.amount)
            )
            .filter(ActivityPaymentRecord.registration_id == reg.id)
            .group_by(ActivityPaymentRecord.type)
            .all()
        )
        amount_map = {t: s for t, s in totals}
        new_paid = (amount_map.get("payment") or 0) - (amount_map.get("refund") or 0)
        reg.paid_amount = max(0, new_paid)
        session.commit()

        session.refresh(reg)
        assert reg.paid_amount == 400


# ────────────────────────────────────────────────────────────────── #
# P7-4：TestRegistrationList（查詢篩選邏輯）
# ────────────────────────────────────────────────────────────────── #


class TestRegistrationList:
    def test_active_only_returned(self, session):
        """只回傳 is_active=True 的報名"""
        reg_active = _add_reg(session, "在籍")
        reg_deleted = _add_reg(session, "已刪除")
        reg_deleted.is_active = False
        session.commit()

        q = session.query(ActivityRegistration).filter(
            ActivityRegistration.is_active.is_(True)
        )
        assert q.count() == 1
        assert q.first().student_name == "在籍"

    def test_payment_status_unpaid_filter(self, session):
        """payment_status=unpaid 篩選：paid_amount=0"""
        reg_paid = _add_reg(session, "已繳費")
        reg_unpaid = _add_reg(session, "未繳費")
        reg_paid.paid_amount = 1000
        reg_paid.is_paid = True
        session.commit()

        q = session.query(ActivityRegistration).filter(
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.paid_amount == 0,
        )
        assert q.count() == 1
        assert q.first().student_name == "未繳費"

    def test_payment_status_partial_filter(self, session):
        """payment_status=partial 篩選：paid_amount>0 且 is_paid=False"""
        reg_partial = _add_reg(session, "部分繳費")
        reg_other = _add_reg(session, "未繳費")
        reg_partial.paid_amount = 500
        reg_partial.is_paid = False
        session.commit()

        q = session.query(ActivityRegistration).filter(
            ActivityRegistration.is_active.is_(True),
            ActivityRegistration.paid_amount > 0,
            ActivityRegistration.is_paid.is_(False),
        )
        assert q.count() == 1
        assert q.first().student_name == "部分繳費"

    def test_total_count_correct(self, session):
        """total 回傳正確筆數"""
        for i in range(5):
            _add_reg(session, f"學生{i}")
        session.commit()

        total = (
            session.query(ActivityRegistration)
            .filter(ActivityRegistration.is_active.is_(True))
            .count()
        )
        assert total == 5

    def test_student_id_filter(self, session):
        """student_id 篩選：僅回傳指定學生的報名紀錄（跨學期）"""
        from api.activity._shared import _build_registration_filter_query

        reg_a = _add_reg(session, "學生A")
        reg_a.student_id = 101
        reg_a.school_year = 114
        reg_a.semester = 1
        reg_b = _add_reg(session, "學生A_下學期")
        reg_b.student_id = 101
        reg_b.school_year = 114
        reg_b.semester = 2
        reg_c = _add_reg(session, "學生B")
        reg_c.student_id = 202
        session.commit()

        q = _build_registration_filter_query(session, student_id=101)
        names = sorted([r.student_name for r in q.all()])
        assert names == ["學生A", "學生A_下學期"]

        q2 = _build_registration_filter_query(session, student_id=202)
        assert q2.count() == 1
        assert q2.first().student_name == "學生B"


# ────────────────────────────────────────────────────────────────── #
# P7-5：TestCourseAPI（課程 CRUD 邏輯）
# ────────────────────────────────────────────────────────────────── #


class TestCourseAPI:
    def test_duplicate_course_name_detected(self, session):
        """重複課程名稱應可被偵測"""
        _add_course(session, name="圍棋")
        session.commit()

        existing = (
            session.query(ActivityCourse).filter(ActivityCourse.name == "圍棋").first()
        )
        assert existing is not None, "重複名稱應被偵測到"

    def test_course_with_registrations_cannot_be_deleted(self, session):
        """有報名記錄的課程刪除前應先檢查"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        course = _add_course(session, name="芭蕾")
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id)
        session.commit()

        count = svc.count_active_course_registrations(session, course.id)
        assert count > 0, "應偵測到有報名記錄，應回 409"

    def test_course_without_registrations_can_be_deleted(self, session):
        """無報名記錄的課程可以停用"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        course = _add_course(session, name="游泳")
        session.commit()

        count = svc.count_active_course_registrations(session, course.id)
        assert count == 0, "無報名記錄，允許停用"


def _add_classroom(session, name="大班") -> Classroom:
    c = Classroom(name=name, is_active=True)
    session.add(c)
    session.flush()
    return c


def _add_settings(session, is_open=True, open_at=None, close_at=None):
    s = ActivityRegistrationSettings(
        is_open=is_open, open_at=open_at, close_at=close_at
    )
    session.add(s)
    session.flush()
    return s


def _make_course_item(name: str) -> PublicCourseItem:
    return PublicCourseItem(name=name)


def _make_supply_item(name: str) -> PublicSupplyItem:
    return PublicSupplyItem(name=name)


# ────────────────────────────────────────────────────────────────── #
# TestCheckRegistrationOpen
# ────────────────────────────────────────────────────────────────── #


class TestCheckRegistrationOpen:
    def test_no_settings_passes(self, session):
        """無設定時不拋例外（允許報名）"""
        _check_registration_open(session)  # should not raise

    def test_open_flag_true_passes(self, session):
        """is_open=True 且時間範圍內不拋例外"""
        _add_settings(session, is_open=True)
        _check_registration_open(session)  # should not raise

    def test_open_flag_false_raises(self, session):
        """is_open=False 應拋 400"""
        from fastapi import HTTPException

        _add_settings(session, is_open=False)
        with pytest.raises(HTTPException) as exc_info:
            _check_registration_open(session)
        assert exc_info.value.status_code == 400
        assert "尚未開放" in exc_info.value.detail

    def test_before_open_time_raises(self, session):
        """早於 open_at 應拋 400"""
        from fastapi import HTTPException

        _add_settings(session, is_open=True, open_at="2099-01-01T00:00")
        with pytest.raises(HTTPException) as exc_info:
            _check_registration_open(session)
        assert exc_info.value.status_code == 400
        assert "尚未開始" in exc_info.value.detail

    def test_after_close_time_raises(self, session):
        """晚於 close_at 應拋 400"""
        from fastapi import HTTPException

        _add_settings(session, is_open=True, close_at="2000-01-01T00:00")
        with pytest.raises(HTTPException) as exc_info:
            _check_registration_open(session)
        assert exc_info.value.status_code == 400
        assert "已截止" in exc_info.value.detail


# ────────────────────────────────────────────────────────────────── #
# TestAttachCourses
# ────────────────────────────────────────────────────────────────── #


class TestAttachCourses:
    def test_enroll_when_capacity_available(self, session):
        """有名額時應建立 enrolled 記錄"""
        course = _add_course(session, name="鋼琴", price=1000, capacity=30)
        reg = _add_reg(session)
        session.commit()

        courses_by_name = {course.name: course}
        enrolled_count_map = {course.id: 0}
        has_waitlist, wl_names = _attach_courses(
            session,
            reg.id,
            [_make_course_item("鋼琴")],
            courses_by_name,
            enrolled_count_map,
        )

        assert not has_waitlist
        rc = (
            session.query(RegistrationCourse)
            .filter(RegistrationCourse.registration_id == reg.id)
            .first()
        )
        assert rc is not None
        assert rc.status == "enrolled"
        assert rc.price_snapshot == 1000

    def test_waitlist_when_full_and_allowed(self, session):
        """課程額滿且 allow_waitlist=True 時建立 waitlist 記錄"""
        course = ActivityCourse(name="芭蕾", price=800, capacity=1, allow_waitlist=True)
        session.add(course)
        session.flush()
        reg = _add_reg(session)
        session.commit()

        courses_by_name = {course.name: course}
        enrolled_count_map = {course.id: 1}  # 已滿
        has_waitlist, wl_names = _attach_courses(
            session,
            reg.id,
            [_make_course_item("芭蕾")],
            courses_by_name,
            enrolled_count_map,
        )

        assert has_waitlist
        assert "芭蕾" in wl_names
        rc = (
            session.query(RegistrationCourse)
            .filter(RegistrationCourse.registration_id == reg.id)
            .first()
        )
        assert rc.status == "waitlist"

    def test_full_no_waitlist_raises(self, session):
        """課程額滿且 allow_waitlist=False 應拋 400"""
        from fastapi import HTTPException

        course = ActivityCourse(
            name="游泳", price=500, capacity=1, allow_waitlist=False
        )
        session.add(course)
        session.flush()
        reg = _add_reg(session)
        session.commit()

        courses_by_name = {course.name: course}
        enrolled_count_map = {course.id: 1}  # 已滿
        with pytest.raises(HTTPException) as exc_info:
            _attach_courses(
                session,
                reg.id,
                [_make_course_item("游泳")],
                courses_by_name,
                enrolled_count_map,
            )
        assert exc_info.value.status_code == 400
        assert "不開放候補" in exc_info.value.detail

    def test_course_not_found_raises(self, session):
        """找不到課程應拋 400"""
        from fastapi import HTTPException

        reg = _add_reg(session)
        session.commit()

        with pytest.raises(HTTPException) as exc_info:
            _attach_courses(session, reg.id, [_make_course_item("不存在的課")], {}, {})
        assert exc_info.value.status_code == 400


# ────────────────────────────────────────────────────────────────── #
# TestAttachSupplies
# ────────────────────────────────────────────────────────────────── #


class TestAttachSupplies:
    def test_supply_attached_correctly(self, session):
        """用品正確建立 RegistrationSupply 記錄"""
        supply = ActivitySupply(name="畫筆", price=150, is_active=True)
        session.add(supply)
        session.flush()
        reg = _add_reg(session)
        session.commit()

        supplies_by_name = {supply.name: supply}
        _attach_supplies(session, reg.id, [_make_supply_item("畫筆")], supplies_by_name)

        rs = (
            session.query(RegistrationSupply)
            .filter(RegistrationSupply.registration_id == reg.id)
            .first()
        )
        assert rs is not None
        assert rs.price_snapshot == 150

    def test_supply_not_found_raises(self, session):
        """找不到用品應拋 400"""
        from fastapi import HTTPException

        reg = _add_reg(session)
        session.commit()

        with pytest.raises(HTTPException) as exc_info:
            _attach_supplies(session, reg.id, [_make_supply_item("不存在")], {})
        assert exc_info.value.status_code == 400


# ────────────────────────────────────────────────────────────────── #
# TestDuplicateRegistrationGuard
# ────────────────────────────────────────────────────────────────── #


class TestDuplicateRegistrationGuard:
    def test_duplicate_detected_via_query(self, session):
        """同學生姓名+生日+is_active=True 應能偵測重複"""
        reg = ActivityRegistration(
            student_name="王小明",
            birthday="2020-01-01",
            class_name="大班",
            is_active=True,
        )
        session.add(reg)
        session.commit()

        existing = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_name == "王小明",
                ActivityRegistration.birthday == "2020-01-01",
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        assert existing is not None

    def test_inactive_registration_not_detected_as_duplicate(self, session):
        """is_active=False 的報名不應觸發重複防護"""
        reg = ActivityRegistration(
            student_name="王小明",
            birthday="2020-01-01",
            class_name="大班",
            is_active=False,
        )
        session.add(reg)
        session.commit()

        existing = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_name == "王小明",
                ActivityRegistration.birthday == "2020-01-01",
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        assert existing is None

    def test_different_student_no_conflict(self, session):
        """不同姓名或生日不觸發重複防護"""
        reg = ActivityRegistration(
            student_name="王小明",
            birthday="2020-01-01",
            class_name="大班",
            is_active=True,
        )
        session.add(reg)
        session.commit()

        existing = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_name == "李小華",
                ActivityRegistration.birthday == "2020-01-01",
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        assert existing is None


# ────────────────────────────────────────────────────────────────── #
# Phase 4-B：TestPromoteWaitlist（候補升正式邏輯）
# ────────────────────────────────────────────────────────────────── #


class TestPromoteWaitlist:
    def test_promote_waitlist_success(self, session):
        """capacity=2、1 enrolled、1 waitlist → 升位後 status 變 enrolled"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        course = _add_course(session, name="鋼琴", capacity=2)
        reg_enrolled = _add_reg(session, "學生A")
        reg_waitlist = _add_reg(session, "學生B")
        _enroll(session, reg_enrolled.id, course.id, status="enrolled")
        _enroll(session, reg_waitlist.id, course.id, status="waitlist")
        session.commit()

        student_name, course_name = svc.promote_waitlist(
            session, reg_waitlist.id, course.id
        )
        session.flush()

        assert student_name == "學生B"
        assert course_name == "鋼琴"
        rc = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg_waitlist.id,
                RegistrationCourse.course_id == course.id,
            )
            .first()
        )
        assert rc.status == "enrolled"

    def test_promote_waitlist_not_found_raises(self, session):
        """報名項目不存在或非候補狀態時拋 ValueError"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        course = _add_course(session)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, status="enrolled")  # 已是正式，非候補
        session.commit()

        with pytest.raises(ValueError, match="不存在或非候補"):
            svc.promote_waitlist(session, reg.id, course.id)

    def test_promote_waitlist_full_raises(self, session):
        """capacity=1 且已有 1 enrolled，再嘗試升位應拋 ValueError 含「容量已滿」"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        course = _add_course(session, name="芭蕾", capacity=1)
        reg_enrolled = _add_reg(session, "學生X")
        reg_waitlist = _add_reg(session, "學生Y")
        _enroll(session, reg_enrolled.id, course.id, status="enrolled")
        _enroll(session, reg_waitlist.id, course.id, status="waitlist")
        session.commit()

        with pytest.raises(ValueError, match="容量已滿"):
            svc.promote_waitlist(session, reg_waitlist.id, course.id)

    def test_promote_waitlist_course_not_exist_raises(self, session):
        """course_id 不存在時拋 ValueError"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        reg = _add_reg(session)
        # 先建立一個 waitlist 記錄（指向不存在的 course_id=9999）
        rc = RegistrationCourse(
            registration_id=reg.id,
            course_id=9999,
            status="waitlist",
            price_snapshot=0,
        )
        session.add(rc)
        session.commit()

        with pytest.raises(ValueError, match="課程不存在"):
            svc.promote_waitlist(session, reg.id, 9999)


# ────────────────────────────────────────────────────────────────── #
# Phase 4-B：TestDeleteRegistration（刪除報名邏輯）
# ────────────────────────────────────────────────────────────────── #


class TestDeleteRegistration:
    def test_delete_sets_inactive(self, session):
        """呼叫 delete_registration 後 is_active 變 False"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        reg = _add_reg(session, "王小明")
        session.commit()

        svc.delete_registration(session, reg.id, "admin")
        session.flush()

        session.refresh(reg)
        assert reg.is_active is False

    def test_delete_auto_promotes_waitlist_to_pending(self, session):
        """刪除 enrolled 報名後，候補第一位自動升 promoted_pending（24h 確認窗）"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        course = _add_course(session, name="圍棋", capacity=2)
        reg_enrolled = _add_reg(session, "在籍生")
        reg_waitlist = _add_reg(session, "候補生")
        _enroll(session, reg_enrolled.id, course.id, status="enrolled")
        _enroll(session, reg_waitlist.id, course.id, status="waitlist")
        session.commit()

        svc.delete_registration(session, reg_enrolled.id, "admin")
        session.flush()

        rc = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg_waitlist.id,
                RegistrationCourse.course_id == course.id,
            )
            .first()
        )
        assert rc.status == "promoted_pending"
        assert rc.confirm_deadline is not None

    def test_delete_no_waitlist_no_error(self, session):
        """課程無候補時刪除不拋錯"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        course = _add_course(session)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, status="enrolled")
        session.commit()

        svc.delete_registration(session, reg.id, "admin")  # 不應拋錯
        session.flush()

        session.refresh(reg)
        assert reg.is_active is False

    def test_delete_not_found_raises(self, session):
        """傳入不存在的 ID 拋 ValueError"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        with pytest.raises(ValueError, match="找不到報名資料"):
            svc.delete_registration(session, 99999, "admin")


# ────────────────────────────────────────────────────────────────── #
# Phase 4-B：TestGetRegistrationDetail（詳情相關邏輯）
# ────────────────────────────────────────────────────────────────── #


class TestGetRegistrationDetail:
    def test_total_amount_excludes_waitlist(self, session):
        """enrolled 課程計入 total_amount，waitlist 不計入"""
        from api.activity._shared import _batch_calc_total_amounts

        course = _add_course(session, price=800)
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, price=800, status="enrolled")
        _enroll(session, reg.id, course.id + 100, price=500, status="waitlist")
        # 第二個 course_id 不存在沒關係，只要 price_snapshot 在 DB 裡
        # 改用兩門不同課程
        course2 = _add_course(session, name="游泳", price=500, capacity=1)
        rc2 = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg.id,
                RegistrationCourse.course_id != course.id,
            )
            .first()
        )
        if rc2:
            session.delete(rc2)
        _enroll(session, reg.id, course2.id, price=500, status="waitlist")
        session.commit()

        result = _batch_calc_total_amounts(session, [reg.id])
        assert result[reg.id] == 800  # 只計 enrolled 的 800，不含 waitlist 的 500

    def test_total_amount_includes_supplies(self, session):
        """用品金額計入 total_amount"""
        from api.activity._shared import _batch_calc_total_amounts

        course = _add_course(session, price=600)
        supply = ActivitySupply(name="畫筆", price=200, is_active=True)
        session.add(supply)
        session.flush()
        reg = _add_reg(session)
        _enroll(session, reg.id, course.id, price=600)
        rs = RegistrationSupply(
            registration_id=reg.id,
            supply_id=supply.id,
            price_snapshot=200,
        )
        session.add(rs)
        session.commit()

        result = _batch_calc_total_amounts(session, [reg.id])
        assert result[reg.id] == 800  # 課程 600 + 用品 200

    def test_change_log_query_pattern(self, session):
        """RegistrationChange .limit(20) 有效截斷超過 20 筆的記錄"""
        reg = _add_reg(session)
        session.commit()

        for i in range(25):
            entry = RegistrationChange(
                registration_id=reg.id,
                student_name=reg.student_name,
                change_type="測試",
                description=f"第 {i+1} 筆",
                changed_by="test",
            )
            session.add(entry)
        session.commit()

        changes = (
            session.query(RegistrationChange)
            .filter(RegistrationChange.registration_id == reg.id)
            .order_by(RegistrationChange.created_at.desc())
            .limit(20)
            .all()
        )
        assert len(changes) == 20


# ────────────────────────────────────────────────────────────────── #
# TestBuildSessionDetailResponse（點名詳情計數邏輯）
# ────────────────────────────────────────────────────────────────── #


def _add_session(session, course_id: int) -> ActivitySession:
    from datetime import date

    s = ActivitySession(
        course_id=course_id, session_date=date.today(), created_by="test"
    )
    session.add(s)
    session.flush()
    return s


def _add_attendance(
    session, session_id: int, registration_id: int, is_present: bool
) -> ActivityAttendance:
    a = ActivityAttendance(
        session_id=session_id,
        registration_id=registration_id,
        is_present=is_present,
        notes="",
        recorded_by="test",
    )
    session.add(a)
    session.flush()
    return a


class TestBuildSessionDetailResponse:
    def test_present_count_correct(self, session):
        """3 enrolled（2 出席、1 缺席）→ present_count=2, absent_count=1"""
        course = _add_course(session, name="圍棋")
        reg1 = _add_reg(session, "學生A")
        reg2 = _add_reg(session, "學生B")
        reg3 = _add_reg(session, "學生C")
        _enroll(session, reg1.id, course.id)
        _enroll(session, reg2.id, course.id)
        _enroll(session, reg3.id, course.id)
        sess = _add_session(session, course.id)
        _add_attendance(session, sess.id, reg1.id, is_present=True)
        _add_attendance(session, sess.id, reg2.id, is_present=True)
        _add_attendance(session, sess.id, reg3.id, is_present=False)
        session.commit()

        result = _build_session_detail_response(session, sess)

        assert result["present_count"] == 2
        assert result["absent_count"] == 1
        assert result["total"] == 3

    def test_unrecorded_not_counted(self, session):
        """未點名學生（is_present=None）不計入 present 或 absent"""
        course = _add_course(session, name="鋼琴")
        reg1 = _add_reg(session, "有點名")
        reg2 = _add_reg(session, "未點名")
        _enroll(session, reg1.id, course.id)
        _enroll(session, reg2.id, course.id)
        sess = _add_session(session, course.id)
        _add_attendance(session, sess.id, reg1.id, is_present=True)
        # reg2 未點名，不建立 ActivityAttendance
        session.commit()

        result = _build_session_detail_response(session, sess)

        assert result["present_count"] == 1
        assert result["absent_count"] == 0
        assert result["total"] == 2

    def test_all_absent(self, session):
        """全員缺席 → present_count=0"""
        course = _add_course(session, name="舞蹈")
        reg1 = _add_reg(session, "缺席生A")
        reg2 = _add_reg(session, "缺席生B")
        _enroll(session, reg1.id, course.id)
        _enroll(session, reg2.id, course.id)
        sess = _add_session(session, course.id)
        _add_attendance(session, sess.id, reg1.id, is_present=False)
        _add_attendance(session, sess.id, reg2.id, is_present=False)
        session.commit()

        result = _build_session_detail_response(session, sess)

        assert result["present_count"] == 0
        assert result["absent_count"] == 2


# ────────────────────────────────────────────────────────────────── #
# TestAttendanceStatsIsActiveFilter（停用課程不計入統計）
# ────────────────────────────────────────────────────────────────── #


class TestAttendanceStatsIsActiveFilter:
    def test_inactive_course_excluded(self, session):
        """停用課程（is_active=False）的點名記錄不計入 get_attendance_stats"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        active_course = _add_course(session, name="主動課程")
        inactive_course = ActivityCourse(
            name="停用課程", price=500, capacity=10, is_active=False
        )
        session.add(inactive_course)
        session.flush()

        reg = _add_reg(session)
        _enroll(session, reg.id, active_course.id)
        _enroll(session, reg.id, inactive_course.id)

        active_sess = _add_session(session, active_course.id)
        inactive_sess = _add_session(session, inactive_course.id)

        _add_attendance(session, active_sess.id, reg.id, is_present=True)
        _add_attendance(session, inactive_sess.id, reg.id, is_present=True)
        session.commit()

        result = svc.get_attendance_stats(session)
        course_names = [c["course_name"] for c in result["by_course"]]

        assert "主動課程" in course_names
        assert "停用課程" not in course_names

    def test_only_active_courses_in_stats(self, session):
        """無活躍課程時，by_course 應為空"""
        from services.activity_service import ActivityService

        svc = ActivityService()

        inactive_course = ActivityCourse(
            name="已停用", price=500, capacity=10, is_active=False
        )
        session.add(inactive_course)
        session.flush()
        reg = _add_reg(session)
        _enroll(session, reg.id, inactive_course.id)
        inactive_sess = _add_session(session, inactive_course.id)
        _add_attendance(session, inactive_sess.id, reg.id, is_present=True)
        session.commit()

        result = svc.get_attendance_stats(session)
        assert result["by_course"] == []


# ────────────────────────────────────────────────────────────────── #
# Pydantic 批次上限防禦
# ────────────────────────────────────────────────────────────────── #


class TestBatchInputLimits:
    def test_batch_payment_update_max_length(self):
        """BatchPaymentUpdate.ids 超過 500 筆應拋 ValidationError"""
        from pydantic import ValidationError
        from api.activity._shared import BatchPaymentUpdate

        with pytest.raises(ValidationError):
            BatchPaymentUpdate(
                ids=list(range(501)), is_paid=True, reason="期末批次補繳"
            )

    def test_batch_payment_update_min_length(self):
        """BatchPaymentUpdate.ids 空列表應拋 ValidationError"""
        from pydantic import ValidationError
        from api.activity._shared import BatchPaymentUpdate

        with pytest.raises(ValidationError):
            BatchPaymentUpdate(ids=[], is_paid=True, reason="期末批次補繳")

    def test_batch_payment_update_valid(self):
        """BatchPaymentUpdate.ids 正常 500 筆不應拋例外（只允許 is_paid=True）。
        reason 最小長度與 MIN_REFUND_REASON_LENGTH 對齊（cash-only 後為 15 字）。"""
        from api.activity._shared import BatchPaymentUpdate

        obj = BatchPaymentUpdate(
            ids=list(range(500)),
            is_paid=True,
            reason="期末批次補繳家長已現金繳清完畢",
        )
        assert len(obj.ids) == 500

    def test_batch_payment_update_rejects_unpaid(self):
        """BatchPaymentUpdate 不再允許 is_paid=False（誤操作全額沖帳風險收緊）。"""
        from pydantic import ValidationError
        from api.activity._shared import BatchPaymentUpdate

        with pytest.raises(ValidationError):
            BatchPaymentUpdate(ids=[1], is_paid=False, reason="期末批次補繳")

    def test_batch_payment_update_requires_reason(self):
        """BatchPaymentUpdate 必填 reason ≥ 5 字（防止無稽核軌跡的批次補齊）。"""
        from pydantic import ValidationError
        from api.activity._shared import BatchPaymentUpdate

        with pytest.raises(ValidationError):
            BatchPaymentUpdate(ids=[1], is_paid=True)
        with pytest.raises(ValidationError):
            BatchPaymentUpdate(ids=[1], is_paid=True, reason="短")

    def test_public_registration_courses_max_length(self):
        """PublicRegistrationPayload.courses 超過 20 筆應拋 ValidationError"""
        from pydantic import ValidationError
        from api.activity._shared import PublicRegistrationPayload, PublicCourseItem

        courses = [PublicCourseItem(name=f"課程{i}") for i in range(21)]
        with pytest.raises(ValidationError):
            PublicRegistrationPayload(
                name="王小明",
                birthday="2020-01-01",
                parent_phone="0912345678",
                **{"class": "大班A"},
                courses=courses,
            )

    def test_public_registration_supplies_max_length(self):
        """PublicRegistrationPayload.supplies 超過 20 筆應拋 ValidationError"""
        from pydantic import ValidationError
        from api.activity._shared import (
            PublicRegistrationPayload,
            PublicCourseItem,
            PublicSupplyItem,
        )

        supplies = [PublicSupplyItem(name=f"用品{i}") for i in range(21)]
        with pytest.raises(ValidationError):
            PublicRegistrationPayload(
                name="王小明",
                birthday="2020-01-01",
                parent_phone="0912345678",
                **{"class": "大班A"},
                courses=[PublicCourseItem(name="美術", price="1000")],
                supplies=supplies,
            )

    def test_public_registration_accepts_remark(self):
        """前端家長備註以 remark 欄位傳入，應正確接收並保留原字串"""
        from api.activity._shared import PublicRegistrationPayload, PublicCourseItem

        payload = PublicRegistrationPayload(
            name="王小明",
            birthday="2020-01-01",
            parent_phone="0912345678",
            **{"class": "中班"},
            courses=[PublicCourseItem(name="美術", price="1000")],
            remark="不吃蛋與花生",
        )
        assert payload.remark == "不吃蛋與花生"
        assert payload.class_ == "中班"

    def test_public_registration_remark_defaults_empty(self):
        """remark 未傳時預設為空字串，保持向後相容"""
        from api.activity._shared import PublicRegistrationPayload, PublicCourseItem

        payload = PublicRegistrationPayload(
            name="王小明",
            birthday="2020-01-01",
            parent_phone="0912345678",
            **{"class": "中班"},
            courses=[PublicCourseItem(name="美術", price="1000")],
        )
        assert payload.remark == ""

    def test_public_registration_rejects_legacy_class_name_key(self):
        """前端若誤送 class_name（舊欄位）應拋 ValidationError，防止 422 回歸"""
        from pydantic import ValidationError
        from api.activity._shared import PublicRegistrationPayload, PublicCourseItem

        with pytest.raises(ValidationError):
            PublicRegistrationPayload(
                name="王小明",
                birthday="2020-01-01",
                parent_phone="0912345678",
                class_name="中班",  # 錯誤欄位名：應為 "class"
                courses=[PublicCourseItem(name="美術", price="1000")],
            )

    def test_public_registration_rejects_courses_as_string_list(self):
        """前端若誤送 courses=['課程A']（字串陣列）應拋 ValidationError，防止 422 回歸"""
        from pydantic import ValidationError
        from api.activity._shared import PublicRegistrationPayload

        with pytest.raises(ValidationError):
            PublicRegistrationPayload(
                name="王小明",
                birthday="2020-01-01",
                parent_phone="0912345678",
                **{"class": "中班"},
                courses=["美術", "音樂"],
            )

    def test_batch_attendance_update_max_length(self):
        """BatchAttendanceUpdate.records 超過 500 筆應拋 ValidationError"""
        from pydantic import ValidationError
        from api.activity.attendance import BatchAttendanceUpdate, AttendanceRecordItem

        records = [
            AttendanceRecordItem(registration_id=i, is_present=True) for i in range(501)
        ]
        with pytest.raises(ValidationError):
            BatchAttendanceUpdate(records=records)

    def test_batch_attendance_update_valid(self):
        """BatchAttendanceUpdate.records 正常 1 筆不應拋例外"""
        from api.activity.attendance import BatchAttendanceUpdate, AttendanceRecordItem

        obj = BatchAttendanceUpdate(
            records=[AttendanceRecordItem(registration_id=1, is_present=True)]
        )
        assert len(obj.records) == 1

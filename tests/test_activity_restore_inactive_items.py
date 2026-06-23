"""tests/test_activity_restore_inactive_items.py

Restore 可重新啟用已停用課程／用品並繼續計費（code review #2，High）。

問題：課程/用品的停用守衛只計 active 報名（_active_course_query /
delete_supply 皆 filter ActivityRegistration.is_active=True）。報名被拒
（is_active=False）期間其課程/用品可被停用。restore 把報名翻回 active 時：
- 課程迴圈只 `if not course: continue`，不檢查 course.is_active；
- 完全不碰 RegistrationSupply；
而 _calc_total_amount 仍加總 enrolled 課程 + 全部用品（不論 is_active）→ 家長被
收已下架課程/用品費用。

修正口徑（業主裁定）：restore 時剔除已停用課程列與已停用用品列（對齊
withdraw_course 的 session.delete），重算 total / is_paid，只收仍上架項目。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import date

from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    ActivitySupply,
    RegistrationCourse,
    RegistrationSupply,
)
from utils.academic import resolve_current_academic_term
from tests.test_activity_restore_capacity import (  # noqa: F401
    restore_client,
    _add_admin,
    _login,
)


def _seed_rejected_with_items(sf):
    """直接造一筆 rejected 報名：含 active 課程 A(300) + 將被停用課程 B(1000)
    + 將被停用用品 S(500)。回傳 (reg_id, course_a_id, course_b_id, supply_id)。"""
    sy, sem = resolve_current_academic_term()
    with sf() as s:
        _add_admin(s)
        course_a = ActivityCourse(
            name="圍棋",
            price=300,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        course_b = ActivityCourse(
            name="畫畫",
            price=1000,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        supply = ActivitySupply(
            name="畫具組",
            price=500,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        s.add_all([course_a, course_b, supply])
        s.flush()

        reg = ActivityRegistration(
            student_name="林小華",
            birthday="2020-03-03",
            parent_phone="0911111111",
            school_year=sy,
            semester=sem,
            is_active=False,
            match_status="rejected",
            pending_review=False,
            paid_amount=0,
            is_paid=False,
        )
        s.add(reg)
        s.flush()
        s.add_all(
            [
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course_a.id,
                    status="enrolled",
                    price_snapshot=300,
                ),
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course_b.id,
                    status="enrolled",
                    price_snapshot=1000,
                ),
                RegistrationSupply(
                    registration_id=reg.id,
                    supply_id=supply.id,
                    price_snapshot=500,
                ),
            ]
        )
        # 停用課程 B 與用品 S（模擬被拒期間後台下架；停用守衛只計 active 報名故放行）
        course_b.is_active = False
        supply.is_active = False
        s.commit()
        return reg.id, course_a.id, course_b.id, supply.id


def test_restore_drops_inactive_course_and_supply_and_recomputes_total(restore_client):
    client, sf = restore_client
    reg_id, course_a_id, course_b_id, supply_id = _seed_rejected_with_items(sf)

    _login(client)
    res = client.post(f"/api/activity/registrations/{reg_id}/restore")
    assert res.status_code == 200, res.text

    with sf() as s:
        # 已停用課程 B 的報名列被剔除
        rc_b = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_id, course_id=course_b_id)
            .first()
        )
        assert rc_b is None, "已停用課程的 RegistrationCourse 應被剔除（不再計費）"

        # 已停用用品 S 的報名列被剔除
        rs = (
            s.query(RegistrationSupply)
            .filter_by(registration_id=reg_id, supply_id=supply_id)
            .first()
        )
        assert rs is None, "已停用用品的 RegistrationSupply 應被剔除（不再計費）"

        # active 課程 A 保留且仍 enrolled
        rc_a = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=reg_id, course_id=course_a_id)
            .one()
        )
        assert rc_a.status == "enrolled", "仍上架課程不應受影響"

        # total 只剩 A 的 300；is_paid 重算（paid=0 → False）
        reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
        from api.activity._shared import _calc_total_amount

        assert _calc_total_amount(s, reg_id) == 300, "應只收仍上架課程，停用項目不計"
        assert reg.is_paid is False


def _seed_rejected_with_attendance(sf):
    """造一筆 rejected 報名，其已停用課程 B 已有一場次 + 一筆點名紀錄。
    回傳 (reg_id, course_b_id, attendance_id)。"""
    sy, sem = resolve_current_academic_term()
    with sf() as s:
        _add_admin(s)
        course_b = ActivityCourse(
            name="畫畫",
            price=1000,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        s.add(course_b)
        s.flush()
        reg = ActivityRegistration(
            student_name="林小華",
            birthday="2020-03-03",
            parent_phone="0911111111",
            school_year=sy,
            semester=sem,
            is_active=False,
            match_status="rejected",
            pending_review=False,
            paid_amount=0,
            is_paid=False,
        )
        s.add(reg)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course_b.id,
                status="enrolled",
                price_snapshot=1000,
            )
        )
        sess = ActivitySession(course_id=course_b.id, session_date=date(2026, 3, 1))
        s.add(sess)
        s.flush()
        att = ActivityAttendance(
            session_id=sess.id,
            registration_id=reg.id,
            is_present=True,
        )
        s.add(att)
        # 點名後才下架課程（停用守衛只計 active 報名，被拒報名故放行）
        course_b.is_active = False
        s.commit()
        return reg.id, course_b.id, att.id


def test_restore_clears_attendance_of_dropped_inactive_course(restore_client):
    """剔除已停用課程列時，須一併清該課考勤（對齊 withdraw_course），避免孤兒
    點名污染統計、未來重報撞 uq_activity_attendance_session_reg。"""
    client, sf = restore_client
    reg_id, course_b_id, att_id = _seed_rejected_with_attendance(sf)

    _login(client)
    res = client.post(f"/api/activity/registrations/{reg_id}/restore")
    assert res.status_code == 200, res.text

    with sf() as s:
        att = s.query(ActivityAttendance).filter_by(id=att_id).first()
        assert att is None, "剔除已停用課程後，其點名紀錄應一併清除（不留孤兒）"

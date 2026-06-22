"""tests/test_activity_restore_recompute.py — restore_registration 的兩個 P2 bug 回歸測試。

Bug 1（registrations_pending.py:861）：restore 後降為 waitlist 的課程降低了應繳金額，
但 restore 通篇沒有重算 is_paid / total_amount，導致 reg.is_paid 停在舊 True，
帳面出現幽靈超繳。

Bug 2（registrations_pending.py:826）：restore 時容量仍足的 promoted_pending 課程，
其 confirm_deadline（拒絕當下的過去時間）未被清為 None；下一輪
sweep_expired_pending_promotions 會立刻把它當逾期刪掉，家長名額被靜默踢掉。
"""

import os
import sys
from datetime import date, timedelta

import pytest
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
from api.activity._shared import _compute_is_paid, _calc_total_amount
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
from utils.taipei_time import now_taipei_naive as _now

# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def restore_recompute_client(tmp_path):
    db_path = tmp_path / "restore_recompute.sqlite"
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


def _seed_course(session, name="圍棋", price=1200, capacity=1):
    """建立 admin + 一門課，回傳 course_id。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    _add_admin(session)
    session.add(Classroom(name="大象班", is_active=True, school_year=sy, semester=sem))
    # 主測試學生（會先報名後被拒）
    session.add(
        Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 5, 10),
            parent_phone="0912345678",
            is_active=True,
        )
    )
    # 遞補學生（B 在 A 被拒後補位）
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
        name=name,
        price=price,
        capacity=capacity,
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
# Bug 1 — restore 後 waitlist 降級應重算 is_paid
# ------------------------------------------------------------------ #


def test_restore_recomputes_is_paid_after_waitlist_demotion(restore_recompute_client):
    """A enrolled 後繳費（is_paid=True, paid_amount>0）→ 被拒 → B 補位 →
    restore A（容量滿：A 降 waitlist）→ 應繳 total 下降 → is_paid 須重算為 False。

    bug 修前：restore 不重算 is_paid，reg.is_paid 仍停在 True（幽靈超繳）。
    """
    client, sf = restore_recompute_client
    with sf() as s:
        course_id = _seed_course(s, capacity=1, price=1200)

    # A 報名（公開端點，enrolled）
    ra = _register(client, name="王小明", birthday="2020-05-10", phone="0912345678")
    assert ra.status_code == 201, ra.text
    reg_a_id = ra.json()["id"]

    _login(client)

    # 模擬 A 已繳費：直接寫 DB（paid_amount=1200, is_paid=True）
    with sf() as s:
        reg_a = s.query(ActivityRegistration).filter_by(id=reg_a_id).one()
        reg_a.paid_amount = 1200
        reg_a.is_paid = True
        s.commit()

    # reject A
    rj = client.post(
        f"/api/activity/registrations/{reg_a_id}/reject",
        json={"reason": "測試拒絕"},
    )
    assert rj.status_code == 200, rj.text

    # B 補位（佔住唯一名額）
    rb = _register(client, name="陳小美", birthday="2019-01-01", phone="0922222222")
    assert rb.status_code == 201, rb.text

    # 前置確認：B enrolled 且 A inactive
    with sf() as s:
        reg_a_db = s.query(ActivityRegistration).filter_by(id=reg_a_id).one()
        assert not reg_a_db.is_active, "前置失敗：A 應已 inactive"
        assert (
            reg_a_db.is_paid is True
        ), "前置失敗：A 應有 is_paid=True（代表舊繳費狀態）"

    # restore A（容量 1 已滿，A 降 waitlist）
    res = client.post(f"/api/activity/registrations/{reg_a_id}/restore")
    assert res.status_code == 200, res.text

    # 核心斷言：restore 後 is_paid 須重算為 False（enrolled 課程 total=0，
    # waitlist 不計入 _calc_total_amount，paid_amount 雖仍 1200 但 total=0 → is_paid=False）
    with sf() as s:
        reg_a_db = s.query(ActivityRegistration).filter_by(id=reg_a_id).one()
        paid_amount = reg_a_db.paid_amount or 0
        total_amount = _calc_total_amount(s, reg_a_id)
        expected_is_paid = _compute_is_paid(paid_amount, total_amount)

        assert reg_a_db.is_paid == expected_is_paid, (
            f"is_paid 未重算：實際={reg_a_db.is_paid}，期望={expected_is_paid} "
            f"（paid={paid_amount}, total={total_amount}）"
        )
        # 更直白：total=0 時 is_paid 必須為 False
        assert (
            reg_a_db.is_paid is False
        ), f"waitlist 降級後 total=0，is_paid 應為 False，實際={reg_a_db.is_paid}"


# ------------------------------------------------------------------ #
# Bug 2 — restore 後 promoted_pending 課程應清 confirm_deadline
# ------------------------------------------------------------------ #


def test_restore_clears_confirm_deadline_on_promoted_pending(restore_recompute_client):
    """reg 有一門 promoted_pending 課（confirm_deadline=過去），被拒後 restore。
    容量仍足，維持 promoted_pending，但 confirm_deadline 必須清為 None（停錶）。

    bug 修前：restore 不清 confirm_deadline，過去時間殘留 → sweep 立刻把它踢掉。
    """
    client, sf = restore_recompute_client

    # 用直接 DB 操作建測試場景（public register 只能建 enrolled/waitlist，
    # 沒有建 promoted_pending 的流程）
    with sf() as s:
        from utils.academic import resolve_current_academic_term

        sy, sem = resolve_current_academic_term()
        _add_admin(s)
        # 建一門容量=2 的課（有充裕空間，restore 不降為 waitlist）
        course = ActivityCourse(
            name="游泳",
            price=800,
            capacity=2,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        s.add(course)
        s.flush()
        c_id = course.id

        # 建報名（已被拒，is_active=False）
        reg = ActivityRegistration(
            student_name="李小華",
            birthday=date(2021, 3, 1),
            class_name="大象班",
            parent_phone="0933333333",
            is_paid=False,
            is_active=False,
            match_status="rejected",
            school_year=sy,
            semester=sem,
        )
        s.add(reg)
        s.flush()

        # RegistrationCourse：promoted_pending + confirm_deadline 是過去 1 天（已逾期）
        past_deadline = _now() - timedelta(days=1)
        rc = RegistrationCourse(
            registration_id=reg.id,
            course_id=c_id,
            status="promoted_pending",
            price_snapshot=800,
            confirm_deadline=past_deadline,
            reminder_sent_at=_now() - timedelta(hours=30),
            final_reminder_sent_at=_now() - timedelta(hours=7),
        )
        s.add(rc)
        s.commit()

        reg_id = reg.id
        rc_id = rc.id

    _login(client)

    # 前置確認：restore 前 confirm_deadline 確實是過去時間
    with sf() as s:
        rc_db = s.query(RegistrationCourse).filter_by(id=rc_id).one()
        assert rc_db.confirm_deadline is not None
        assert rc_db.confirm_deadline < _now(), "前置：confirm_deadline 應為過去時間"

    # restore（容量 2 有空間，RC 維持 promoted_pending，但 deadline 應清 None）
    res = client.post(f"/api/activity/registrations/{reg_id}/restore")
    assert res.status_code == 200, res.text

    # 核心斷言：confirm_deadline 已清為 None（停錶）
    with sf() as s:
        rc_db = s.query(RegistrationCourse).filter_by(id=rc_id).one()
        assert (
            rc_db.confirm_deadline is None
        ), f"restore 後 confirm_deadline 應清為 None，實際={rc_db.confirm_deadline}"
        assert (
            rc_db.reminder_sent_at is None
        ), f"restore 後 reminder_sent_at 應清為 None，實際={rc_db.reminder_sent_at}"
        assert rc_db.final_reminder_sent_at is None, (
            f"restore 後 final_reminder_sent_at 應清為 None，"
            f"實際={rc_db.final_reminder_sent_at}"
        )
        # RC 仍維持 promoted_pending（容量足夠，未降級）
        assert (
            rc_db.status == "promoted_pending"
        ), f"容量充裕時 RC 應仍為 promoted_pending，實際={rc_db.status}"


# ------------------------------------------------------------------ #
# Bug 2 端到端 — restore 後 sweep 不得刪掉 promoted_pending 課程
# ------------------------------------------------------------------ #


def test_restore_promoted_pending_survives_sweep(restore_recompute_client):
    """restore 後若 promoted_pending confirm_deadline 未清 None，
    下一輪 sweep_expired_pending_promotions 會立刻把它刪掉（家長被靜默踢掉名額）。
    修正後 confirm_deadline=None → sweep 的 IS NOT NULL filter 跳過此列 → 名額保住。
    """
    from services.activity_service import ActivityService

    client, sf = restore_recompute_client

    with sf() as s:
        from utils.academic import resolve_current_academic_term

        sy, sem = resolve_current_academic_term()

        # 補建 admin（_seed_course 不在此路徑）
        _add_admin(s)

        course = ActivityCourse(
            name="鋼琴",
            price=1500,
            capacity=2,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        s.add(course)
        s.flush()
        c_id = course.id

        reg = ActivityRegistration(
            student_name="張大衛",
            birthday=date(2020, 7, 7),
            class_name="大象班",
            parent_phone="0944444444",
            is_paid=False,
            is_active=False,
            match_status="rejected",
            school_year=sy,
            semester=sem,
        )
        s.add(reg)
        s.flush()

        # promoted_pending + 過去的 deadline（未修前 sweep 會立刻刪它）
        past_deadline = _now() - timedelta(days=1)
        rc = RegistrationCourse(
            registration_id=reg.id,
            course_id=c_id,
            status="promoted_pending",
            price_snapshot=1500,
            confirm_deadline=past_deadline,
        )
        s.add(rc)
        s.commit()

        reg_id = reg.id
        rc_id = rc.id

    _login(client)

    # restore（容量 2，RC 維持 promoted_pending）
    res = client.post(f"/api/activity/registrations/{reg_id}/restore")
    assert res.status_code == 200, res.text

    # 驗 restore 後 confirm_deadline=None
    with sf() as s:
        rc_db = s.query(RegistrationCourse).filter_by(id=rc_id).one()
        assert (
            rc_db.confirm_deadline is None
        ), "restore 後 confirm_deadline 應 None，sweep 才不會立刻刪它"

    # 呼叫 sweep（此呼叫直接對 SQLite session，模擬排程器）
    svc = ActivityService()
    with sf() as s:
        result = svc.sweep_expired_pending_promotions(s)
        s.commit()

    # 核心斷言：sweep 結果 expired=0，且 RC 仍存在（家長名額保住）
    assert (
        result["expired"] == 0
    ), f"sweep 不應把修正後的 promoted_pending 當逾期刪掉，實際 expired={result['expired']}"
    with sf() as s:
        rc_db = s.query(RegistrationCourse).filter_by(id=rc_id).first()
        assert rc_db is not None, "promoted_pending 課程列被 sweep 誤刪（家長名額消失）"
        assert (
            rc_db.status == "promoted_pending"
        ), f"名額列應仍為 promoted_pending，實際={rc_db.status}"

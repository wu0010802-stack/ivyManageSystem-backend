"""tests/test_withdraw_per_course_signoff.py — withdraw_course 退課 per-course 粒度守衛測試。

問題（I1）：
  withdraw_course 退課時，require_approve_for_refund_diff 收到的是 _full_sugg
  （整筆報名的 needs_manual_review），而不是被退那門課自己的 sessions 狀態。
  若報名含 A(sessions=10, 已知) 與 B(sessions=NULL) 兩門課，整 reg 的
  needs_manual_review=True，退「A 課」（diff=0，可算）也被擋 403 ← 誤擋。

修法：
  把 `suggestion=_full_sugg` 改為
  `suggestion={"needs_manual_review": _course_item is not None and _course_item.get("suggested_amount") is None}`
  即只看被退那門課自己的 sessions 狀態。

測試：
  (a) RED → 報名含 A(sessions=10)+B(sessions=NULL)，退 A 門課 → 無 APPROVE 仍應 200
      （diff=0，A 自身 sessions 已知）。修前會 403。
  (b) 同報名退 B 門課（NULL sessions） → 無 APPROVE 應 403（per-course 正確攔截）。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    RegistrationCourse,
    RegistrationSupply,
)
from tests.test_activity_pos import _create_admin, _login, _setup_reg

# ── Constants ─────────────────────────────────────────────────────────────────

REFUND_REASON = "家長要求退費，已確認原因符合園所政策。"
TODAY = date.today().isoformat()
COURSE_A_NAME = "音樂"  # sessions 已知（10 堂）
COURSE_B_NAME = "美術"  # sessions IS NULL
COURSE_A_PRICE = 800
COURSE_B_PRICE = 600


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    """SQLite TestClient，每個測試獨立 DB，對齊 e2e 檔慣例。"""
    db_path = tmp_path / "per_course_signoff.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


# ── Helper ────────────────────────────────────────────────────────────────────


def _setup_two_course_reg(
    session, *, paid_amount: int = COURSE_A_PRICE + COURSE_B_PRICE
):
    """建立含 A(sessions=10)+B(sessions=NULL) 兩門 enrolled 課程的報名（已全繳）。

    sessions=NULL 需在 flush 後明確設為 None（column default 陷阱：SQLite
    可能將 INSERT 省略欄位填 0 而非 NULL，必須明確指定）。
    """
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()

    # ── 課程 A：sessions 已知（10 堂） ──────────────────────────────────────
    course_a = ActivityCourse(
        name=COURSE_A_NAME,
        price=COURSE_A_PRICE,
        sessions=10,  # 明確 10 堂
        capacity=30,
        allow_waitlist=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course_a)

    # ── 課程 B：sessions IS NULL ────────────────────────────────────────────
    course_b = ActivityCourse(
        name=COURSE_B_NAME,
        price=COURSE_B_PRICE,
        sessions=None,  # 明確 NULL（陷阱：若省略 SQLite 可能存 0）
        capacity=30,
        allow_waitlist=True,
        school_year=sy,
        semester=sem,
    )
    session.add(course_b)
    session.flush()

    # 確保 B 的 sessions 真的是 NULL（防 SQLite default 陷阱）
    session.expire(course_b)
    refreshed_b = session.get(ActivityCourse, course_b.id)
    assert refreshed_b.sessions is None, (
        f"course_b.sessions 應為 NULL，實際為 {refreshed_b.sessions}；"
        "SQLite column default 陷阱，需明確設 None"
    )

    # ── 報名 ──────────────────────────────────────────────────────────────────
    reg = ActivityRegistration(
        student_name="測試生",
        birthday="2020-01-01",
        class_name="大班",
        paid_amount=paid_amount,
        is_paid=True,
        is_active=True,
        school_year=sy,
        semester=sem,
    )
    session.add(reg)
    session.flush()

    # RegistrationCourse for A（enrolled，繳費快照 = COURSE_A_PRICE）
    session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course_a.id,
            status="enrolled",
            price_snapshot=COURSE_A_PRICE,
        )
    )
    # RegistrationCourse for B（enrolled，繳費快照 = COURSE_B_PRICE）
    session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course_b.id,
            status="enrolled",
            price_snapshot=COURSE_B_PRICE,
        )
    )
    session.flush()
    return reg, course_a, course_b


# ═══════════════════════════════════════════════════════════════════════════════
# 核心測試：per-course 粒度
# ═══════════════════════════════════════════════════════════════════════════════


class TestWithdrawPerCourseSignoff:
    """確保 withdraw_course 只看被退那門課自己的 sessions 決定是否強制簽核。"""

    def test_withdraw_known_sessions_course_no_approve_needed(self, client):
        """(a) 退 A 課（sessions=10 已知，diff=0），無 APPROVE 帳號 → 200 放行。

        RED 描述（修前）：
          _full_sugg.needs_manual_review=True（因 B 課 sessions IS NULL），
          require_approve_for_refund_diff(suggestion=_full_sugg) 讀到 True，
          無 APPROVE 帳號被 403 誤擋，即使 A 課 sessions 已知、diff=0。

        GREEN 描述（修後）：
          suggestion={"needs_manual_review": False}（A 課 suggested_amount 非 None），
          守衛不觸發，200 放行。
        """
        c, sf = client
        with sf() as s:
            _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
            reg, course_a, _ = _setup_two_course_reg(
                s, paid_amount=COURSE_A_PRICE + COURSE_B_PRICE
            )
            s.commit()
            reg_id = reg.id
            course_a_id = course_a.id

        _login(c)
        # 退 A 課（sessions=10，建議退=COURSE_A_PRICE=800，實退 800，diff=0）
        resp = c.delete(
            f"/api/activity/registrations/{reg_id}/courses/{course_a_id}",
            params={"force_refund": "true", "refund_reason": REFUND_REASON},
        )
        assert resp.status_code == 200, (
            f"(a) 退 sessions 已知的 A 課（diff=0），無 APPROVE 應放行（200），"
            f"實際：{resp.status_code} {resp.json()}"
        )

    def test_withdraw_null_sessions_course_blocked_without_approve(self, client):
        """(b) 退 B 課（sessions=NULL），無 APPROVE 帳號 → 403 強制簽核。

        B 課 per-course sessions IS NULL → needs_manual_review=True → 403 正確攔截。
        """
        c, sf = client
        with sf() as s:
            _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
            reg, _, course_b = _setup_two_course_reg(
                s, paid_amount=COURSE_A_PRICE + COURSE_B_PRICE
            )
            s.commit()
            reg_id = reg.id
            course_b_id = course_b.id

        _login(c)
        # 退 B 課（sessions=NULL，建議退=fallback=amount_due=600，實退 600，diff=0 但 NULL sessions）
        resp = c.delete(
            f"/api/activity/registrations/{reg_id}/courses/{course_b_id}",
            params={"force_refund": "true", "refund_reason": REFUND_REASON},
        )
        assert resp.status_code == 403, (
            f"(b) 退 sessions IS NULL 的 B 課，無 APPROVE 應被 403 攔截，"
            f"實際：{resp.status_code} {resp.json()}"
        )
        detail = resp.json().get("detail", "")
        assert (
            "sessions" in detail
            or "堂數" in detail
            or "ACTIVITY_PAYMENT_APPROVE" in detail
        ), f"403 detail 應提到 sessions/堂數/ACTIVITY_PAYMENT_APPROVE，實際：{detail}"

    def test_withdraw_null_sessions_course_allowed_with_approve(self, client):
        """(b 通過版) 退 B 課（sessions=NULL），有 APPROVE → 200 放行。

        確保 per-course 守衛只擋無 APPROVE 帳號，有 APPROVE 仍可執行。
        """
        c, sf = client
        with sf() as s:
            _create_admin(
                s,
                permission_names=[
                    "ACTIVITY_READ",
                    "ACTIVITY_WRITE",
                    "ACTIVITY_PAYMENT_APPROVE",
                ],
            )
            reg, _, course_b = _setup_two_course_reg(
                s, paid_amount=COURSE_A_PRICE + COURSE_B_PRICE
            )
            s.commit()
            reg_id = reg.id
            course_b_id = course_b.id

        _login(c)
        resp = c.delete(
            f"/api/activity/registrations/{reg_id}/courses/{course_b_id}",
            params={"force_refund": "true", "refund_reason": REFUND_REASON},
        )
        assert resp.status_code == 200, (
            f"(b 通過版) 退 B 課 + 有 APPROVE 應放行（200），"
            f"實際：{resp.status_code} {resp.json()}"
        )

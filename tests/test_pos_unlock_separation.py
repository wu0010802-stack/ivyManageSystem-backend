"""
test_pos_unlock_separation.py — 驗證 POS 日結 unlock 4-eye + admin override。

對齊 spec: docs/superpowers/specs/2026-05-06-pos-unlock-separation-design.md
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import ActivityPosDailyClose, ApprovalLog, Base
from utils.permissions import Permission

# 引用既有 helper（在 tests/test_activity_pos.py）
from tests.test_activity_pos import (
    _create_admin,
    _login,
)

APPROVE_PERMS = (
    Permission.ACTIVITY_READ
    | Permission.ACTIVITY_WRITE
    | Permission.ACTIVITY_PAYMENT_APPROVE
)


@pytest.fixture
def unlock_client(tmp_path):
    """提供 client + session_factory；同 pos_client 模式但獨立 fixture。"""
    db_path = tmp_path / "unlock.sqlite"
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


def _seed_signed_close(session_factory, *, target, approver_username):
    """Helper：直接寫一筆已簽核的 ActivityPosDailyClose（避免依賴 approve API）。"""
    from datetime import datetime

    with session_factory() as s:
        s.add(
            ActivityPosDailyClose(
                close_date=target,
                approver_username=approver_username,
                approved_at=datetime.now(),
                payment_total=1000,
                refund_total=0,
                net_total=1000,
                transaction_count=1,
                by_method_json='{"現金": 1000}',
            )
        )
        s.commit()


# ── Test 1-5: unlock 4-eye 守衛 ──────────────────────────────────────


def test_unlock_by_original_approver_rejected_403(unlock_client):
    """原簽核人不可解鎖自己簽過的日子（一般 4-eye 路徑）。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="approver_a")

    assert _login(client, "approver_a").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "想自己解鎖看看會不會被擋", "is_admin_override": False},
    )
    assert res.status_code == 403, res.text
    assert "原簽核人" in res.json()["detail"]


def test_unlock_by_other_approver_succeeds(unlock_client):
    """不同 PAYMENT_APPROVE 持有者解鎖原簽核人的日子 → 200。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="approver_a")

    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "B 解 A 的：發現少收一筆", "is_admin_override": False},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["close_date"] == target.isoformat()
    assert body["is_admin_override"] is False
    with sf() as s:
        log = (
            s.query(ApprovalLog)
            .filter(ApprovalLog.doc_type == "activity_pos_daily")
            .order_by(ApprovalLog.id.desc())
            .first()
        )
        assert log is not None
        assert log.action == "cancelled"
        assert log.approver_username == "approver_b"


def test_admin_override_with_long_reason_succeeds(unlock_client):
    """role='admin' + override + reason ≥ 30 字 → 200，ApprovalLog action='admin_override'。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="boss", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="boss")

    assert _login(client, "boss").status_code == 200
    long_reason = "副手請假，緊急 override 解鎖修正昨日帳務漏記 NT$500 部分"
    assert len(long_reason) >= 30
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": long_reason, "is_admin_override": True},
    )
    assert res.status_code == 200, res.text
    assert res.json()["is_admin_override"] is True
    with sf() as s:
        log = (
            s.query(ApprovalLog)
            .filter(ApprovalLog.doc_type == "activity_pos_daily")
            .order_by(ApprovalLog.id.desc())
            .first()
        )
        assert log.action == "admin_override"
        assert log.approver_role == "admin"


def test_admin_override_short_reason_rejected_422(unlock_client):
    """admin override 但 reason < 30 字 → 422（schema 層 model_validator）。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="boss", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="boss")

    assert _login(client, "boss").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "太短不夠 30 字測試案例", "is_admin_override": True},
    )
    assert res.status_code == 422, res.text
    assert "30" in res.text


def test_non_admin_with_override_flag_rejected_403(unlock_client):
    """非 admin role 帶 is_admin_override=True → 403。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    # 注意：_create_admin 預設 role='admin'。要建立非 admin 帳號需直接寫 DB。
    from utils.auth import hash_password
    from models.database import User

    with sf() as s:
        s.add(
            User(
                username="staff_x",
                password_hash=hash_password("TempPass123"),
                role="staff",
                permissions=APPROVE_PERMS,
                is_active=True,
            )
        )
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="approver_a")

    assert _login(client, "staff_x", "TempPass123").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={
            "reason": "假裝自己是 admin 嘗試 override 但其實沒 admin role 的測試",
            "is_admin_override": True,
        },
    )
    assert res.status_code == 403, res.text
    assert "admin" in res.text.lower()


# ── Test 6: notification_delivered ────────────────────────────────────


def test_unlock_response_notification_delivered_false_when_no_line_binding(
    unlock_client,
):
    """原簽核人未綁定 LINE → response notification_delivered=false，unlock 仍成功 200。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="approver_a")

    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "B 解 A 的；A 未綁 LINE 測試", "is_admin_override": False},
    )
    assert res.status_code == 200, res.text
    assert res.json()["notification_delivered"] is False


# ── Test 7-8: approve warnings ────────────────────────────────────────


def test_approve_warnings_when_approver_is_today_operator(unlock_client):
    """簽核者 = 當日 POS 操作者 → response 帶 warnings 提示。"""
    from datetime import datetime
    from models.database import ActivityPaymentRecord

    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        # 直接寫一筆 payment_record，operator='approver_a'，模擬當日由 approver_a 操作
        # （避開 _add_payment helper 的 operator kwarg 不確定性）
        # 為了 _require_daily_close_unlocked 通過，必須先有一筆 ActivityRegistration
        from models.database import (
            ActivityCourse,
            ActivityRegistration,
            RegistrationCourse,
        )

        course = ActivityCourse(
            name="美術",
            price=1500,
            capacity=30,
            allow_waitlist=True,
            school_year=114,
            semester=1,
        )
        s.add(course)
        s.flush()
        reg = ActivityRegistration(
            student_name="X",
            birthday="2020-01-01",
            class_name="大班",
            paid_amount=0,
            is_paid=False,
            is_active=True,
            school_year=114,
            semester=1,
        )
        s.add(reg)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1500,
            )
        )
        s.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=500,
                payment_date=target,
                payment_method="現金",
                operator="approver_a",
                notes="[POS-TEST-A]",
                created_at=datetime.now(),
            )
        )
        s.commit()

    assert _login(client, "approver_a").status_code == 200
    res = client.post(
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"note": "approver = today operator"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert "warnings" in body
    assert any("收銀者" in w for w in body["warnings"])


def test_approve_no_warnings_when_approver_did_not_operate_today(unlock_client):
    """簽核者 ≠ 當日 POS 操作者 → warnings 為空陣列。"""
    from datetime import datetime
    from models.database import ActivityPaymentRecord

    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        from models.database import (
            ActivityCourse,
            ActivityRegistration,
            RegistrationCourse,
        )

        course = ActivityCourse(
            name="美術",
            price=1500,
            capacity=30,
            allow_waitlist=True,
            school_year=114,
            semester=1,
        )
        s.add(course)
        s.flush()
        reg = ActivityRegistration(
            student_name="Y",
            birthday="2020-01-01",
            class_name="大班",
            paid_amount=0,
            is_paid=False,
            is_active=True,
            school_year=114,
            semester=1,
        )
        s.add(reg)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1500,
            )
        )
        # 操作者 = other_cashier（非簽核人）
        s.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=500,
                payment_date=target,
                payment_method="現金",
                operator="other_cashier",
                notes="[POS-TEST-B]",
                created_at=datetime.now(),
            )
        )
        s.commit()

    assert _login(client, "approver_a").status_code == 200
    res = client.post(
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"note": "approver != operator"},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body.get("warnings", []) == []


# ── Test 9-10: audit endpoint ────────────────────────────────────────


def test_audit_endpoint_returns_recent_unlock_events_only(unlock_client):
    """audit endpoint 只回傳 doc_type='activity_pos_daily' + action 在 unlock 集合的事件。"""
    client, sf = unlock_client
    target_a = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target_a, approver_username="approver_a")

    # B 解 A 簽的 target_a → cancelled 事件
    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target_a.isoformat()}",
        json={
            "reason": "B 解 A 的測試 cancelled 事件 audit",
            "is_admin_override": False,
        },
    )
    assert res.status_code == 200

    # 寫入一筆無關 doc_type 的 ApprovalLog 確認過濾正確
    with sf() as s:
        s.add(
            ApprovalLog(
                doc_type="leave",
                doc_id=999,
                action="approved",
                approver_username="approver_b",
            )
        )
        s.commit()

    # 查 audit endpoint
    res = client.get("/api/activity/audit/pos-unlock-events?days=30")
    assert res.status_code == 200, res.text
    body = res.json()
    events = body["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["close_date"] == target_a.isoformat()
    assert ev["action"] == "cancelled"
    assert ev["unlocker_username"] == "approver_b"


def test_audit_endpoint_orders_desc_and_limits_200(unlock_client):
    """構造 250 筆 unlock 事件，回傳 200 筆按時間倒序。"""
    from datetime import datetime

    client, sf = unlock_client
    with sf() as s:
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        # 構造 250 筆 cancelled 事件
        base = datetime.now()
        for i in range(250):
            s.add(
                ApprovalLog(
                    doc_type="activity_pos_daily",
                    doc_id=20260101 + i,
                    action="cancelled",
                    approver_username="approver_b",
                    created_at=base - timedelta(seconds=i),
                )
            )
        s.commit()

    assert _login(client, "approver_b").status_code == 200
    res = client.get("/api/activity/audit/pos-unlock-events?days=30")
    assert res.status_code == 200
    events = res.json()["events"]
    assert len(events) == 200
    assert events[0]["occurred_at"] > events[-1]["occurred_at"]


# ── Test 11-13: live_diff（spec H2）────────────────────────────────


def test_unlock_live_diff_zero_when_no_change(unlock_client):
    """無新交易也無 voided → live_diff 全 0；snapshot 與 live 相同。"""
    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        s.commit()
    # _seed_signed_close 寫入 snapshot payment_total=1000，但實際無 payment_records
    # → live_snapshot 為 0，diff = 0 - 1000 = -1000（DB 層沒對齊）
    # 為了讓 live_diff=0，需要 _seed_signed_close 對應實際 records
    _seed_signed_close(sf, target=target, approver_username="approver_a")

    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "B 解 A 的：驗證 live_diff 結構", "is_admin_override": False},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "live_diff" in body
    diff = body["live_diff"]
    # _seed_signed_close 寫 snapshot 1000 但無 records → live=0，diff=-1000
    assert diff["original_payment_total"] == 1000
    assert diff["live_payment_total"] == 0
    assert diff["payment_total_diff"] == -1000
    assert diff["net_total_diff"] == -1000


def test_unlock_live_diff_positive_when_new_payment_added(unlock_client):
    """簽核後新增 payment_record → live > original，diff 正值。"""
    from datetime import datetime as _dt
    from models.database import (
        ActivityCourse,
        ActivityPaymentRecord,
        ActivityRegistration,
        RegistrationCourse,
    )

    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        # 寫一筆 reg + 1500 payment_record（payment_date = target）
        course = ActivityCourse(
            name="美術",
            price=1500,
            capacity=30,
            allow_waitlist=True,
            school_year=114,
            semester=1,
        )
        s.add(course)
        s.flush()
        reg = ActivityRegistration(
            student_name="新生",
            birthday="2020-01-01",
            class_name="大班",
            paid_amount=1500,
            is_paid=True,
            is_active=True,
            school_year=114,
            semester=1,
        )
        s.add(reg)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1500,
            )
        )
        # 一筆 payment_record（live snapshot 會看到這筆）
        s.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=1500,
                payment_date=target,
                payment_method="現金",
                operator="approver_a",
                notes="",
                created_at=_dt.now(),
            )
        )
        s.commit()
    # snapshot 寫 1000（假設簽核時只看到 1000，後來補了 500 → live=1500）
    with sf() as s:
        s.add(
            ActivityPosDailyClose(
                close_date=target,
                approver_username="approver_a",
                approved_at=_dt.now(),
                payment_total=1000,
                refund_total=0,
                net_total=1000,
                transaction_count=1,
                by_method_json='{"現金": 1000}',
            )
        )
        s.commit()

    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={
            "reason": "B 解 A 的：驗證新增交易後 live_diff 正值",
            "is_admin_override": False,
        },
    )
    assert res.status_code == 200, res.text
    diff = res.json()["live_diff"]
    # original: 1000；live: 1500 → diff = +500
    assert diff["original_payment_total"] == 1000
    assert diff["live_payment_total"] == 1500
    assert diff["payment_total_diff"] == 500
    assert diff["net_total_diff"] == 500
    assert diff["transaction_count_diff"] == 0  # snapshot 紀錄了 1 筆，live 也是 1 筆


def test_unlock_live_diff_negative_when_voided_after_approve(unlock_client):
    """簽核後紀錄被 void → live < original，diff 負值。"""
    from datetime import datetime as _dt
    from models.database import (
        ActivityCourse,
        ActivityPaymentRecord,
        ActivityRegistration,
        RegistrationCourse,
    )

    client, sf = unlock_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        course = ActivityCourse(
            name="美術",
            price=1000,
            capacity=30,
            allow_waitlist=True,
            school_year=114,
            semester=1,
        )
        s.add(course)
        s.flush()
        reg = ActivityRegistration(
            student_name="退費生",
            birthday="2020-01-01",
            class_name="大班",
            paid_amount=0,
            is_paid=False,
            is_active=True,
            school_year=114,
            semester=1,
        )
        s.add(reg)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1000,
            )
        )
        # 一筆 voided 的 payment（簽核時計入，現在被 void 排除）
        s.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="payment",
                amount=1000,
                payment_date=target,
                payment_method="現金",
                operator="approver_a",
                notes="",
                created_at=_dt.now(),
                voided_at=_dt.now(),
                voided_by="approver_b",
                void_reason="誤刷",
            )
        )
        s.commit()
    # snapshot 簽核時 1000 元（當時還沒 void）
    with sf() as s:
        s.add(
            ActivityPosDailyClose(
                close_date=target,
                approver_username="approver_a",
                approved_at=_dt.now(),
                payment_total=1000,
                refund_total=0,
                net_total=1000,
                transaction_count=1,
                by_method_json='{"現金": 1000}',
            )
        )
        s.commit()

    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={
            "reason": "B 解 A 的：驗證 void 後 live_diff 負值",
            "is_admin_override": False,
        },
    )
    assert res.status_code == 200, res.text
    diff = res.json()["live_diff"]
    # original: 1000；live: 0（voided 排除）→ diff = -1000
    assert diff["original_payment_total"] == 1000
    assert diff["live_payment_total"] == 0
    assert diff["payment_total_diff"] == -1000

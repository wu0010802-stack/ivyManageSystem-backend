"""
test_pos_close_history.py — 驗證 unlock 寫入 ActivityPosDailyCloseHistory（spec H3）+
查詢端點 /audit/pos-close-history。
"""

import os
import sys
from datetime import date, datetime, timedelta

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
    ActivityPosDailyClose,
    ActivityPosDailyCloseHistory,
    Base,
)
from utils.permissions import Permission

from tests.test_activity_pos import _create_admin, _login

APPROVE_PERMS = (
    Permission.ACTIVITY_READ
    | Permission.ACTIVITY_WRITE
    | Permission.ACTIVITY_PAYMENT_APPROVE
)


@pytest.fixture
def history_client(tmp_path):
    db_path = tmp_path / "close_history.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, sf

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_signed_close(sf, *, target, approver_username, by_method='{"現金": 1000}'):
    with sf() as s:
        s.add(
            ActivityPosDailyClose(
                close_date=target,
                approver_username=approver_username,
                approved_at=datetime.now(),
                note="原始備註",
                payment_total=1000,
                refund_total=0,
                net_total=1000,
                transaction_count=1,
                by_method_json=by_method,
                actual_cash_count=950,
                cash_variance=-50,
            )
        )
        s.commit()


def test_unlock_writes_history_snapshot(history_client):
    """unlock 應在 delete 前 append 完整 snapshot 到 history table。"""
    client, sf = history_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(
        sf,
        target=target,
        approver_username="approver_a",
        by_method='{"現金": 800, "系統補齊": 200}',
    )

    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={
            "reason": "B 解 A 的測試 H3 history snapshot",
            "is_admin_override": False,
        },
    )
    assert res.status_code == 200, res.text

    # 驗證 history 寫入完整
    with sf() as s:
        rows = (
            s.query(ActivityPosDailyCloseHistory)
            .filter(ActivityPosDailyCloseHistory.close_date == target)
            .all()
        )
        assert len(rows) == 1
        h = rows[0]
        assert h.approver_username == "approver_a"
        assert h.approve_note == "原始備註"
        assert h.payment_total == 1000
        assert h.refund_total == 0
        assert h.net_total == 1000
        assert h.transaction_count == 1
        assert h.by_method_json == '{"現金": 800, "系統補齊": 200}'
        assert h.actual_cash_count == 950
        assert h.cash_variance == -50
        assert h.unlocked_by == "approver_b"
        assert h.unlocked_by_role == "admin"  # _create_admin 預設 role
        assert h.is_admin_override is False
        assert "B 解 A 的" in h.unlock_reason


def test_unlock_admin_override_flag_recorded(history_client):
    """admin override 路徑應正確記錄 is_admin_override=True。"""
    client, sf = history_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="boss", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(sf, target=target, approver_username="boss")

    assert _login(client, "boss").status_code == 200
    long_reason = "副手請假，緊急 override 解鎖修正昨日帳務漏記 NT$500 部分"
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": long_reason, "is_admin_override": True},
    )
    assert res.status_code == 200

    with sf() as s:
        h = (
            s.query(ActivityPosDailyCloseHistory)
            .filter(ActivityPosDailyCloseHistory.close_date == target)
            .first()
        )
        assert h is not None
        assert h.is_admin_override is True
        assert h.unlocked_by == "boss"


def test_multiple_unlock_cycles_append_rows(history_client):
    """同一 close_date 多次簽核+解鎖循環 → history 多筆，依 unlocked_at 倒序可區分。"""
    client, sf = history_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        s.commit()

    # 第一次簽核 + 解鎖
    _seed_signed_close(
        sf, target=target, approver_username="approver_a", by_method='{"現金": 500}'
    )
    assert _login(client, "approver_b").status_code == 200
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "第一次解鎖測試循環迭代", "is_admin_override": False},
    )
    assert res.status_code == 200

    # 第二次簽核 + 解鎖（不同金額）
    _seed_signed_close(
        sf, target=target, approver_username="approver_a", by_method='{"現金": 1500}'
    )
    res = client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "第二次解鎖測試循環迭代", "is_admin_override": False},
    )
    assert res.status_code == 200

    # 驗證 2 筆 history（不重複、可依 unlocked_at 排序）
    with sf() as s:
        rows = (
            s.query(ActivityPosDailyCloseHistory)
            .filter(ActivityPosDailyCloseHistory.close_date == target)
            .order_by(ActivityPosDailyCloseHistory.unlocked_at.asc())
            .all()
        )
        assert len(rows) == 2
        assert rows[0].by_method_json == '{"現金": 500}'
        assert rows[1].by_method_json == '{"現金": 1500}'


def test_history_endpoint_returns_snapshots(history_client):
    """GET /audit/pos-close-history 回傳該 close_date 的所有 history rows，倒序。"""
    client, sf = history_client
    target = date.today() - timedelta(days=1)
    with sf() as s:
        _create_admin(s, username="approver_a", permissions=APPROVE_PERMS)
        _create_admin(s, username="approver_b", permissions=APPROVE_PERMS)
        s.commit()
    _seed_signed_close(
        sf,
        target=target,
        approver_username="approver_a",
        by_method='{"現金": 1000, "系統補齊": 200}',
    )

    assert _login(client, "approver_b").status_code == 200
    client.request(
        "DELETE",
        f"/api/activity/pos/daily-close/{target.isoformat()}",
        json={"reason": "解鎖供 history 端點測試使用", "is_admin_override": False},
    )

    res = client.get(
        f"/api/activity/audit/pos-close-history?close_date={target.isoformat()}"
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["close_date"] == target.isoformat()
    assert body["count"] == 1
    snap = body["snapshots"][0]
    # 結構化解析（不再依賴 free text）
    assert snap["by_method"] == {"現金": 1000, "系統補齊": 200}
    assert snap["payment_total"] == 1000
    assert snap["approver_username"] == "approver_a"
    assert snap["unlocked_by"] == "approver_b"


def test_history_endpoint_empty_when_no_history(history_client):
    """無 history 紀錄時回 count=0，snapshots=[]。"""
    client, sf = history_client
    with sf() as s:
        _create_admin(s, permissions=APPROVE_PERMS)
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/audit/pos-close-history?close_date=2026-01-01")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] == 0
    assert body["snapshots"] == []


def test_history_endpoint_permission_403(history_client):
    """無 ACTIVITY_PAYMENT_APPROVE → 403。"""
    from utils.auth import hash_password
    from models.database import User

    client, sf = history_client
    with sf() as s:
        s.add(
            User(
                username="reader",
                password_hash=hash_password("Pw123456"),
                role="staff",
                permissions=Permission.ACTIVITY_READ,
                is_active=True,
            )
        )
        s.commit()

    assert _login(client, "reader", "Pw123456").status_code == 200
    res = client.get("/api/activity/audit/pos-close-history?close_date=2026-01-01")
    assert res.status_code == 403

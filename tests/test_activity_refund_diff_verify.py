"""POS refund diff verify e2e 測試（TestClient）。

對應 spec §8 + §11：
- diff <= 100 → pass
- diff > 100 + 一線員工 → 403
- diff > 100 + ACTIVITY_PAYMENT_APPROVE → pass
- 多 reg 同收據 sum(abs(per-reg-diff)) 累加
- 方向抵消防護：reg1 多 60 + reg2 少 60 → diff=120 簽核
- 用品實退觸發 diff
- NULL sessions reg 採 amount_due fallback
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
from utils.taipei_time import now_taipei_naive
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivityPaymentRecord,
    ActivitySession,
    Base,
)
from tests.test_activity_pos import _create_admin, _login, _setup_reg


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "refund_diff.sqlite"
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


@pytest.fixture
def client_with_audit_capture(tmp_path):
    """client fixture 加 audit_changes 捕捉用 middleware intercept（零 race condition）。"""
    db_path = tmp_path / "refund_audit.sqlite"
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

    test_app = FastAPI()
    test_app.include_router(auth_router)
    test_app.include_router(activity_router)

    captured: dict = {"audit_changes": None}

    @test_app.middleware("http")
    async def capture_audit(request, call_next):
        response = await call_next(request)
        ac = getattr(request.state, "audit_changes", None)
        if ac is not None:
            captured["audit_changes"] = ac
        return response

    with TestClient(test_app) as c:
        yield c, session_factory, captured

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


REFUND_REASON = "家長要求退費，已確認原因符合園所政策。"

# 繳費日期改用「台灣今日」（與 validate_payment_date 同基準），避免硬編碼日期
# 跨出 30 天回補窗後變成時間炸彈（例 "2026-05-26" 在 2026-06-26 起 >30 天被 422）。
_PAYMENT_DATE = now_taipei_naive().date().isoformat()


def _set_course_sessions(session, course_name: str, sessions: int):
    """把 _setup_reg 預設建出來 sessions=NULL 的 course 補上 sessions。"""
    c = session.query(ActivityCourse).filter(ActivityCourse.name == course_name).first()
    c.sessions = sessions
    session.flush()


def _mark_attendance(session, reg_id: int, course_id: int, n: int):
    for i in range(n):
        s = ActivitySession(course_id=course_id, session_date=date(2026, 5, i + 1))
        session.add(s)
        session.flush()
        session.add(
            ActivityAttendance(
                session_id=s.id,
                registration_id=reg_id,
                is_present=True,
            )
        )
    session.flush()


def _refund_body(reg_id: int, amount: int, key: str = "REFUNDDIFF-0001") -> dict:
    return {
        "items": [{"registration_id": reg_id, "amount": amount}],
        "payment_method": "現金",
        "payment_date": _PAYMENT_DATE,
        "type": "refund",
        "notes": REFUND_REASON,
        # idempotency_key 自 2026-06-29 契約變更起必填（^[A-Za-z0-9_-]{8,64}$）。
        "idempotency_key": key,
    }


# ── happy paths ────────────────────────────────────────────────────────────


def test_refund_diff_zero_passes_staff(client):
    """員工剛好送 suggested → diff=0 → 一線通過。
    用 course_price=800（<= REFUND_APPROVAL_THRESHOLD=1000）確保守衛一不介入。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        # 不需 _mark_attendance（n=0 attendance 即「未開課」全退）
        s.commit()
        reg_id = reg.id

    _login(c)
    # 0 attendance → suggested=800（not_started 特例全退）；diff=0 → 通過
    resp = c.post(
        "/api/activity/pos/checkout",
        json=_refund_body(reg_id, 800, "REFUNDDIFF-ZERO-01"),
    )
    assert resp.status_code in (200, 201), resp.json()


def test_refund_diff_below_threshold_passes_staff(client):
    """diff=50（< 100）→ 一線通過。
    用 course_price=800 確保守衛一不介入（750 <= 1000）。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    # 0 attendance → suggested=800；員工送 750 → diff=50
    resp = c.post(
        "/api/activity/pos/checkout",
        json=_refund_body(reg_id, 750, "REFUNDDIFF-BELOW-01"),
    )
    assert resp.status_code in (200, 201), resp.json()


def test_refund_diff_over_threshold_blocks_staff(client):
    """diff=200（> 100）+ 無 approve → 403。
    用 course_price=800 確保守衛一不介入（500 <= 1000）；diff=300 > 100 → 守衛三觸發。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    # suggested=800（全退）；員工送 500 → diff=300 > 100 → 守衛三觸發
    resp = c.post(
        "/api/activity/pos/checkout",
        json=_refund_body(reg_id, 500, "REFUNDDIFF-OVER-01"),
    )
    assert resp.status_code == 403
    assert "偏離" in resp.json()["detail"] or "差" in resp.json()["detail"]


def test_refund_diff_over_threshold_passes_approver(client):
    """diff=300 + ACTIVITY_PAYMENT_APPROVE → pass（守衛三略過）。
    pos_checkout 成功回 201 Created。"""
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
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    # suggested=800（全退）；員工送 500 → diff=300 > 100，但 approver 略過
    resp = c.post(
        "/api/activity/pos/checkout",
        json=_refund_body(reg_id, 500, "REFUNDDIFF-APPROVER-01"),
    )
    assert resp.status_code in (200, 201), resp.json()


def test_refund_supply_triggers_diff(client):
    """suggested=0（用品 + course 全退）員工只想退 supply NT$300 → diff 觸發
    （因 course 0 attendance suggested=1500，員工只送 300 → diff=1200）。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=1500, supply_price=500, paid_amount=2000)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.post(
        "/api/activity/pos/checkout",
        json=_refund_body(reg_id, 300, "REFUNDDIFF-SUPPLY-01"),
    )
    assert resp.status_code == 403


def test_refund_null_sessions_uses_amount_due_fallback(client):
    """course.sessions=NULL → suggested 用 amount_due fallback；
    員工少退觸發 diff。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        # _setup_reg 預設不設 course.sessions → NULL
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        s.commit()
        reg_id = reg.id

    _login(c)
    # NULL fallback: suggested=1500；員工送 1000 → diff=500 → 簽核
    resp = c.post(
        "/api/activity/pos/checkout",
        json=_refund_body(reg_id, 1000, "REFUNDDIFF-NULLSESS-01"),
    )
    assert resp.status_code == 403


def test_refund_multi_reg_diff_accumulates(client):
    """多 reg 同收據：reg1 多退 60 + reg2 少退 60 →
    naive abs(total)=0；spec 算法 sum(abs)=120 → 簽核。
    用 course_price=400 確保收據合計 (460+340=800) <= 1000，守衛一不介入。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg1 = _setup_reg(
            s,
            student_name="A",
            course_price=400,
            supply_price=0,
            paid_amount=400,
            course_name="A 課",
        )
        reg2 = _setup_reg(
            s,
            student_name="B",
            course_price=400,
            supply_price=0,
            paid_amount=400,
            course_name="B 課",
        )
        _set_course_sessions(s, "A 課", 10)
        _set_course_sessions(s, "B 課", 10)
        s.commit()
        rid1, rid2 = reg1.id, reg2.id

    _login(c)
    # 兩 reg suggested 都是 400（0 attendance 全退）
    # reg1 actual=460（+60）；reg2 actual=340（-60）
    # total_actual=800=total_suggested → naive diff=0
    # sum(abs) = 60+60 = 120 → 守衛三觸發
    body = {
        "items": [
            {"registration_id": rid1, "amount": 460},
            {"registration_id": rid2, "amount": 340},
        ],
        "payment_method": "現金",
        "payment_date": _PAYMENT_DATE,
        "type": "refund",
        "notes": REFUND_REASON,
        "idempotency_key": "test-refund-diff",  # R7-3：多筆須帶 key
    }
    resp = c.post("/api/activity/pos/checkout", json=body)
    assert resp.status_code == 403
    assert "120" in resp.json()["detail"]


# ── 單筆退費 endpoint /registrations/{id}/payments 退費路徑 ────────────────


def test_single_refund_diff_blocks_staff(client):
    """POST /registrations/{id}/payments (type=refund) diff > 100 → 403。
    用 course_price=800（<= REFUND_APPROVAL_THRESHOLD=1000）確保守衛一不介入；
    diff=300（800-500=300 > 100）→ 守衛三觸發。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    body = {
        "amount": 500,  # suggested=800（全退）；diff=300 > 100；amount=500 < guard1 1000
        "payment_method": "現金",
        "payment_date": _PAYMENT_DATE,
        "type": "refund",
        "notes": REFUND_REASON,
        "idempotency_key": "REFUNDDIFF-SINGLE-BLOCK-01",
    }
    resp = c.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
    assert resp.status_code == 403


def test_single_refund_diff_below_threshold_passes(client):
    """單筆退費 diff <= 100 → 一線通過。
    用 course_price=800（<= REFUND_APPROVAL_THRESHOLD=1000）確保守衛一不介入；
    diff=50（800-750=50 < 100）→ 守衛三亦不介入。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=800, supply_price=0, paid_amount=800)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    body = {
        "amount": 750,  # suggested=800（全退）；diff=50 < 100；amount=750 < guard1 1000
        "payment_method": "現金",
        "payment_date": _PAYMENT_DATE,
        "type": "refund",
        "notes": REFUND_REASON,
        "idempotency_key": "REFUNDDIFF-SINGLE-PASS-01",
    }
    resp = c.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
    assert resp.status_code in (200, 201), resp.json()


def test_single_refund_split_cannot_bypass_diff_gate(client):
    """拆單繞過防護（2026-06-29 audit P2-A）。

    退費建議值無狀態（build_refund_suggestion 不扣既退），故 diff 閘若只比『單筆
    body.amount』vs 建議總額，員工可把退費拆成多筆、每筆都=建議值使 diff=0 恆放行，
    累積超退而無 ACTIVITY_PAYMENT_APPROVE 簽核。

    修正：diff 改以『累積實退（含本次）』vs 建議總額。
    - 首筆 300 = 建議值 → 累積 300，diff=0 → 一線放行（合法全額建議退費）。
    - 次筆 300 → 累積實退 600 > 建議 300 → diff=300 > 100 → 一線須簽核 → 403。
    """
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        # course_price=900、T_served=5/10（ratio 0.5 ∈ [1/3,2/3)）→ suggested=round(900×1/3)=300
        reg = _setup_reg(s, course_price=900, supply_price=0, paid_amount=900)
        _set_course_sessions(s, "美術", 10)
        course = s.query(ActivityCourse).filter(ActivityCourse.name == "美術").first()
        _mark_attendance(s, reg.id, course.id, 5)
        s.commit()
        reg_id = reg.id

    _login(c)
    body1 = {
        "amount": 300,  # = suggested → diff=0
        "payment_method": "現金",
        "payment_date": _PAYMENT_DATE,
        "type": "refund",
        "notes": REFUND_REASON,
        "idempotency_key": "REFUNDSPLIT-A1",
    }
    r1 = c.post(f"/api/activity/registrations/{reg_id}/payments", json=body1)
    assert r1.status_code in (200, 201), r1.json()  # 首筆合法（diff=0）

    body2 = dict(body1, idempotency_key="REFUNDSPLIT-A2")
    r2 = c.post(f"/api/activity/registrations/{reg_id}/payments", json=body2)
    # 累積實退 600 > 建議 300 → diff=300 > 100 → 一線員工須簽核
    assert r2.status_code == 403, r2.json()
    assert "偏離" in r2.json()["detail"] or "差" in r2.json()["detail"]


def test_pos_refund_split_cannot_bypass_diff_gate(client):
    """POS 拆單繞過防護（2026-06-29 audit P2-A）。

    與單筆 payments 路徑同根因：POS diff 閘若只比『本次 checkout 實退』vs 無狀態建議，
    員工可多次 checkout、每次都=建議值使 diff=0，累積超退。修正後第二次 checkout 因
    『累積實退（含 prior）> 建議』而觸發 diff 閘。
    """
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=900, supply_price=0, paid_amount=900)
        _set_course_sessions(s, "美術", 10)
        course = s.query(ActivityCourse).filter(ActivityCourse.name == "美術").first()
        _mark_attendance(s, reg.id, course.id, 5)  # T_served=5/10 → suggested=300
        s.commit()
        reg_id = reg.id

    _login(c)
    r1 = c.post(
        "/api/activity/pos/checkout", json=_refund_body(reg_id, 300, "POSSPLIT-A1")
    )
    assert r1.status_code in (200, 201), r1.json()  # 首次=建議值 diff=0
    r2 = c.post(
        "/api/activity/pos/checkout", json=_refund_body(reg_id, 300, "POSSPLIT-A2")
    )
    # 累積實退 600 > 建議 300 → diff=300 > 100 → 一線員工須簽核
    assert r2.status_code == 403, r2.json()


def test_writeoff_diff_gate_is_cumulative_aware(client):
    """標記未繳沖帳的 diff 閘須含既退累積（2026-06-29 audit P2-A）。

    suggested=300、已有 prior 退費 600（paid 已從 900 退到 300）。標記未繳沖帳剩餘
    current_paid=300。舊碼 diff=abs(300-300)=0 漏放行，但累積實退=600+300=900 >> 建議
    300（超退 600）。修正後 diff=abs((600+300)-300)=600 > 100 → 一線員工須簽核 → 403。
    （prior 退費直接寫 DB 以隔離 writeoff 閘：不論既退從哪條路徑產生，此閘都須累積感知。）
    """
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=900, supply_price=0, paid_amount=300)
        _set_course_sessions(s, "美術", 10)
        course = s.query(ActivityCourse).filter(ActivityCourse.name == "美術").first()
        _mark_attendance(s, reg.id, course.id, 5)  # T_served=5/10 → suggested=300
        s.add(
            ActivityPaymentRecord(
                registration_id=reg.id,
                type="refund",
                amount=600,
                payment_date=date(2026, 5, 1),
                payment_method="現金",
                notes="prior refund injected",
                operator="x",
            )
        )
        s.commit()
        reg_id = reg.id

    _login(c)
    body = {
        "is_paid": False,
        "confirm_refund_amount": 300,  # = current_paid（reg.paid_amount）
        "refund_reason": REFUND_REASON,
    }
    resp = c.put(f"/api/activity/registrations/{reg_id}/payment", json=body)
    # 累積實退 600+300=900 > 建議 300 → diff=600 > 100 → 須簽核
    assert resp.status_code == 403, resp.json()


def test_pos_refund_audit_changes_contains_suggestion(client_with_audit_capture):
    """成功退費後 request.state.audit_changes 應含 refund_suggested_total / actual / diff。

    使用 middleware intercept 直接斷言 request.state.audit_changes（零 race condition）。
    wiring 在 pos.py spec §12 區塊保證。
    """
    c, sf, captured = client_with_audit_capture
    with sf() as s:
        _create_admin(
            s,
            permission_names=[
                "ACTIVITY_READ",
                "ACTIVITY_WRITE",
                "ACTIVITY_PAYMENT_APPROVE",
            ],
        )
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.post(
        "/api/activity/pos/checkout",
        json=_refund_body(reg_id, 1450, "REFUNDDIFF-AUDIT-01"),
    )
    assert resp.status_code in (200, 201), resp.json()

    ac = captured["audit_changes"]
    assert ac is not None, "POS checkout 退費必須設 request.state.audit_changes"
    assert ac.get("refund_suggested_total") == 1500
    assert ac.get("refund_actual_total") == 1450
    assert ac.get("refund_diff") == 50

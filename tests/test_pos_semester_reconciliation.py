"""tests/test_pos_semester_reconciliation.py — 學期對帳總表端點測試。

涵蓋四態簽核判斷與 approval_status 篩選。
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
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityPosDailyClose,
    ActivityRegistration,
    ActivitySupply,
    Base,
    RegistrationCourse,
    RegistrationSupply,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def pos_client(tmp_path):
    db_path = tmp_path / "pos_semester.sqlite"
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


def _create_admin(
    session,
    username: str = "pos_admin",
    password: str = "TempPass123",
    permission_names: list[str] = ["ACTIVITY_READ", "ACTIVITY_WRITE"],
) -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=permission_names,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username="pos_admin", password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _setup_reg(
    session,
    *,
    student_name: str,
    class_name: str = "玫瑰",
    course_price: int = 2000,
    paid_amount: int = 0,
    is_paid: bool = False,
    course_name: str = "美術",
) -> ActivityRegistration:
    """建立本學期一筆報名（1 門課，含用品）。total = course_price。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course = (
        session.query(ActivityCourse)
        .filter(
            ActivityCourse.name == course_name,
            ActivityCourse.school_year == sy,
            ActivityCourse.semester == sem,
        )
        .first()
    )
    if not course:
        course = ActivityCourse(
            name=course_name,
            price=course_price,
            capacity=30,
            allow_waitlist=True,
            school_year=sy,
            semester=sem,
        )
        session.add(course)
        session.flush()

    reg = ActivityRegistration(
        student_name=student_name,
        birthday="2020-01-01",
        class_name=class_name,
        paid_amount=paid_amount,
        is_paid=is_paid,
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
            status="enrolled",
            price_snapshot=course_price,
        )
    )
    session.flush()
    return reg


def _add_payment(
    session,
    *,
    reg_id: int,
    amount: int,
    payment_date: date,
    type_: str = "payment",
    method: str = "現金",
    notes: str = "[POS-TEST]",
):
    rec = ActivityPaymentRecord(
        registration_id=reg_id,
        type=type_,
        amount=amount,
        payment_date=payment_date,
        payment_method=method,
        notes=notes,
        operator="pos_admin",
    )
    session.add(rec)
    session.flush()
    return rec


def _mark_closed(session, close_date: date):
    row = ActivityPosDailyClose(
        close_date=close_date,
        approver_username="pos_admin",
        approved_at=datetime.now(),
        payment_total=0,
        refund_total=0,
        net_total=0,
        transaction_count=0,
        by_method_json="{}",
    )
    session.add(row)
    session.flush()


def _get(client, **params):
    return client.get("/api/activity/pos/semester-reconciliation", params=params)


def _find_item(data, reg_id):
    for it in data["items"]:
        if it["id"] == reg_id:
            return it
    raise AssertionError(f"reg {reg_id} not in items")


# ── 測試 ─────────────────────────────────────────────────────────────


def test_fully_approved(pos_client):
    client, sf = pos_client
    d1 = date.today() - timedelta(days=3)
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="全簽核", paid_amount=2000, is_paid=True)
        _add_payment(s, reg_id=reg.id, amount=2000, payment_date=d1)
        _mark_closed(s, d1)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200, res.text
    data = res.json()
    it = _find_item(data, rid)
    assert it["approval_status"] == "fully_approved"
    assert it["approved_paid_amount"] == 2000
    assert it["pending_paid_amount"] == 0


def test_partially_approved(pos_client):
    client, sf = pos_client
    d1 = date.today() - timedelta(days=3)  # 已簽核
    d2 = date.today() - timedelta(days=1)  # 未簽核
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="半簽核", paid_amount=1500, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=1000, payment_date=d1)
        _add_payment(s, reg_id=reg.id, amount=500, payment_date=d2)
        _mark_closed(s, d1)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200
    it = _find_item(res.json(), rid)
    assert it["approval_status"] == "partially_approved"
    assert it["approved_paid_amount"] == 1000
    assert it["pending_paid_amount"] == 500


def test_pending_approval(pos_client):
    client, sf = pos_client
    d1 = date.today()
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="待簽核", paid_amount=800, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=800, payment_date=d1)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    it = _find_item(res.json(), rid)
    assert it["approval_status"] == "pending_approval"
    assert it["approved_paid_amount"] == 0
    assert it["pending_paid_amount"] == 800


def test_no_payment(pos_client):
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="未繳費", paid_amount=0)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    it = _find_item(res.json(), rid)
    assert it["approval_status"] == "no_payment"
    assert it["paid_amount"] == 0
    assert it["owed"] == 2000


def test_filter_by_approval_status(pos_client):
    client, sf = pos_client
    d1 = date.today() - timedelta(days=3)
    d2 = date.today()
    with sf() as s:
        _create_admin(s)
        r_full = _setup_reg(s, student_name="全簽核", paid_amount=2000, is_paid=True)
        _add_payment(s, reg_id=r_full.id, amount=2000, payment_date=d1)
        _mark_closed(s, d1)

        r_pend = _setup_reg(
            s, student_name="待簽核", paid_amount=2000, is_paid=True, course_name="勞作"
        )
        _add_payment(s, reg_id=r_pend.id, amount=2000, payment_date=d2)

        r_none = _setup_reg(s, student_name="未繳費", paid_amount=0, course_name="圍棋")
        s.commit()
        rid_full, rid_pend, rid_none = r_full.id, r_pend.id, r_none.id

    assert _login(client).status_code == 200

    # 不過濾：3 筆都在
    res_all = _get(client)
    ids_all = {it["id"] for it in res_all.json()["items"]}
    assert {rid_full, rid_pend, rid_none} <= ids_all

    # 過濾 pending_approval：只 r_pend
    res = _get(client, approval_status="pending_approval")
    assert res.status_code == 200
    items = res.json()["items"]
    assert [it["id"] for it in items] == [rid_pend]

    # 過濾 no_payment：只 r_none
    res = _get(client, approval_status="no_payment")
    assert [it["id"] for it in res.json()["items"]] == [rid_none]

    # totals 反映過濾後結果
    res = _get(client, approval_status="fully_approved")
    totals = res.json()["totals"]
    assert totals["registration_count"] == 1
    assert totals["approved_paid_amount"] == 2000
    assert totals["pending_paid_amount"] == 0


def test_offline_paid_no_records(pos_client):
    """reg 有 paid_amount 但完全沒 payment_record（歷史匯入資料）。"""
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="歷史匯入", paid_amount=1800, is_paid=False)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200
    data = res.json()
    it = _find_item(data, rid)
    # 無 POS 紀錄但有 paid_amount → 歸類 pending_approval（尚未透過日結覆蓋）
    assert it["approval_status"] == "pending_approval"
    assert it["approved_paid_amount"] == 0
    assert it["pending_paid_amount"] == 0
    assert it["offline_paid_amount"] == 1800
    # totals 也要累計
    assert data["totals"]["offline_paid_amount"] >= 1800


def test_offline_paid_partial_mix(pos_client):
    """reg.paid_amount 比 POS 紀錄加總大 → 差額視為 offline_paid。"""
    client, sf = pos_client
    d1 = date.today() - timedelta(days=2)
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="混合匯入", paid_amount=2000, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=500, payment_date=d1)
        _mark_closed(s, d1)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    it = _find_item(res.json(), rid)
    # 500 已簽核、其餘 1500 沒 POS 紀錄
    assert it["approved_paid_amount"] == 500
    assert it["pending_paid_amount"] == 0
    assert it["offline_paid_amount"] == 1500


def test_invalid_approval_status_rejected(pos_client):
    client, _sf = pos_client
    with pos_client[1]() as s:
        _create_admin(s)
        s.commit()

    assert _login(client).status_code == 200
    res = _get(client, approval_status="not_a_valid_state")
    assert res.status_code == 400


def test_cross_bucket_refund_payment_approved_refund_pending(pos_client):
    """M1 案例 A：繳費日已簽核 + 退費日未簽核（最常見退費時序）。

    payment 1000（closed）+ refund 400（open），reg.paid_amount=600。
    修前：pending_net = max(0, 0-400) = 0 → 退費 400 被 clamp 蒸發，
    approved_net 仍 1000 > paid 600 自相矛盾。
    修後：退費跨 bucket 沖銷 → approved_net=600、pending_net=0、offline=0，
    三 bucket 加總 == paid。"""
    client, sf = pos_client
    d_closed = date.today() - timedelta(days=3)
    d_open = date.today()
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="跨桶退費A", paid_amount=600, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=1000, payment_date=d_closed)
        _add_payment(s, reg_id=reg.id, amount=400, payment_date=d_open, type_="refund")
        _mark_closed(s, d_closed)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200, res.text
    it = _find_item(res.json(), rid)
    assert it["approved_paid_amount"] == 600
    assert it["pending_paid_amount"] == 0
    assert it["offline_paid_amount"] == 0
    assert (
        it["approved_paid_amount"]
        + it["pending_paid_amount"]
        + it["offline_paid_amount"]
        == it["paid_amount"]
        == 600
    )


def test_cross_bucket_refund_payment_pending_refund_approved(pos_client):
    """M1 案例 B：繳費日未簽核 + 退費日已簽核（反向，少見但對稱）。

    payment 1000（open）+ refund 400（closed），paid=600。
    修前：approved_net = max(0, 0-400) = 0 蒸發退費 → pending_net 仍 1000。
    修後：pending_net=600、approved_net=0、offline=0。"""
    client, sf = pos_client
    d_closed = date.today() - timedelta(days=3)
    d_open = date.today()
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="跨桶退費B", paid_amount=600, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=1000, payment_date=d_open)
        _add_payment(
            s, reg_id=reg.id, amount=400, payment_date=d_closed, type_="refund"
        )
        _mark_closed(s, d_closed)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200, res.text
    it = _find_item(res.json(), rid)
    assert it["approved_paid_amount"] == 0
    assert it["pending_paid_amount"] == 600
    assert it["offline_paid_amount"] == 0
    assert (
        it["approved_paid_amount"]
        + it["pending_paid_amount"]
        + it["offline_paid_amount"]
        == it["paid_amount"]
        == 600
    )


def test_cross_bucket_refund_exceeds_pos_records(pos_client):
    """M1 案例 C：退費總額超過 POS 流水（offline 沖銷邊界）。

    payment 1000（closed）+ refund 1500（open），paid=500
    （隱含 1000 為歷史離線收款，其中 500 已被退）。
    兩 bucket 沖銷後仍負 → 殘額由 offline 吸收，全部 >= 0 且加總 == paid。"""
    client, sf = pos_client
    d_closed = date.today() - timedelta(days=3)
    d_open = date.today()
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="跨桶退費C", paid_amount=500, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=1000, payment_date=d_closed)
        _add_payment(s, reg_id=reg.id, amount=1500, payment_date=d_open, type_="refund")
        _mark_closed(s, d_closed)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200, res.text
    it = _find_item(res.json(), rid)
    assert it["approved_paid_amount"] >= 0
    assert it["pending_paid_amount"] >= 0
    assert it["offline_paid_amount"] >= 0
    assert (
        it["approved_paid_amount"]
        + it["pending_paid_amount"]
        + it["offline_paid_amount"]
        == it["paid_amount"]
        == 500
    )
    assert it["approved_paid_amount"] == 0
    assert it["pending_paid_amount"] == 0
    assert it["offline_paid_amount"] == 500


def test_semester_reconciliation_truncated_flag(pos_client, monkeypatch):
    """M3：超過查詢上限時不可無聲截斷——response 需帶 truncated=True 與 total_active。"""
    from api.activity import pos as pos_mod

    monkeypatch.setattr(pos_mod, "_POS_LIST_QUERY_LIMIT", 1)
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        _setup_reg(s, student_name="截斷甲", paid_amount=0)
        _setup_reg(s, student_name="截斷乙", paid_amount=0, course_name="勞作")
        s.commit()

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["truncated"] is True
    assert data["total_active"] == 2
    assert len(data["items"]) == 1


def test_semester_reconciliation_not_truncated(pos_client):
    """未超限時 truncated=False、total_active = 全量筆數。"""
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        _setup_reg(s, student_name="未截斷", paid_amount=0)
        s.commit()

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["truncated"] is False
    assert data["total_active"] == 1


def test_outstanding_truncated_flag(pos_client, monkeypatch):
    """M3：未結清查詢超過上限同樣需帶 truncated/total_active（兩端點一致）。"""
    from api.activity import pos as pos_mod

    monkeypatch.setattr(pos_mod, "_POS_LIST_QUERY_LIMIT", 1)
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        _setup_reg(s, student_name="欠費甲", paid_amount=0)
        _setup_reg(s, student_name="欠費乙", paid_amount=0, course_name="勞作")
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/pos/outstanding-by-student")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["truncated"] is True
    assert data["total_active"] == 2
    assert len(data["groups"]) == 1


def test_outstanding_not_truncated(pos_client):
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        _setup_reg(s, student_name="欠費單", paid_amount=0)
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/pos/outstanding-by-student")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["truncated"] is False
    assert data["total_active"] == 1


def test_outstanding_search_escapes_like_wildcards(pos_client):
    """M4：搜尋字串含 `%`/`_` 不可被當 LIKE 萬用字元——搜 `%` 只命中名字
    真的含 `%` 的學生，而非萬用匹配全部。"""
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        _setup_reg(s, student_name="百分%生", paid_amount=0)
        _setup_reg(s, student_name="普通生", paid_amount=0, course_name="勞作")
        s.commit()

    assert _login(client).status_code == 200
    res = client.get("/api/activity/pos/outstanding-by-student", params={"q": "%"})
    assert res.status_code == 200, res.text
    data = res.json()
    names = [g["student_name"] for g in data["groups"]]
    assert names == ["百分%生"]

    # 底線同理：`_` 不可匹配任意單一字元
    res = client.get("/api/activity/pos/outstanding-by-student", params={"q": "_"})
    assert res.status_code == 200, res.text
    assert res.json()["groups"] == []


def test_voided_refund_excluded_from_offline_paid(pos_client):
    """Finding C / backlog ④：學期對帳 bucket 必須排除 voided 流水。

    場景：一筆 active POS 收款 1000（未結日 → pending）+ 一筆已作廢(voided)退費 1000。
    reg.paid_amount 是排除 voided 後的權威值（1000）。若 bucket 把 voided refund
    算進 pending_refund，pending_net 會被砍到 0 → offline_paid = paid − 0 = 1000，
    把正常 POS 收款誤歸成「非 POS 離線已繳」。修前 offline=1000、修後應為 0。"""
    client, sf = pos_client
    d1 = date.today()  # 未結日 → pending
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="作廢退費", paid_amount=1000, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=1000, payment_date=d1)
        refund = _add_payment(
            s, reg_id=reg.id, amount=1000, payment_date=d1, type_="refund"
        )
        refund.voided_at = datetime.now()
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200, res.text
    it = _find_item(res.json(), rid)
    assert (
        it["offline_paid_amount"] == 0
    ), "voided refund 不應讓正常 POS 收款被歸成離線已繳"
    assert it["pending_paid_amount"] == 1000


def test_voided_payment_and_refund_do_not_affect_buckets(pos_client):
    """voided payment 與 voided refund 都不得進三 bucket。

    基準：closed 日收款 800 + open 日收款 400，paid=1200
    → approved=800 / pending=400 / offline=0。
    再各加一筆 voided payment 9999（closed 日）與 voided refund 9999（open 日）；
    reg.paid_amount 權威值不變（void 時已重算排除），三 bucket 應與基準完全相同。
    若 voided 流水滲入：approved 會虛增 9999、或 voided refund 砍掉 pending
    → offline 虛增。"""
    client, sf = pos_client
    d_closed = date.today() - timedelta(days=3)
    d_open = date.today()
    with sf() as s:
        _create_admin(s)
        reg = _setup_reg(s, student_name="作廢不影響", paid_amount=1200, is_paid=False)
        _add_payment(s, reg_id=reg.id, amount=800, payment_date=d_closed)
        _add_payment(s, reg_id=reg.id, amount=400, payment_date=d_open)
        voided_pay = _add_payment(s, reg_id=reg.id, amount=9999, payment_date=d_closed)
        voided_pay.voided_at = datetime.now()
        voided_refund = _add_payment(
            s, reg_id=reg.id, amount=9999, payment_date=d_open, type_="refund"
        )
        voided_refund.voided_at = datetime.now()
        _mark_closed(s, d_closed)
        s.commit()
        rid = reg.id

    assert _login(client).status_code == 200
    res = _get(client)
    assert res.status_code == 200, res.text
    it = _find_item(res.json(), rid)
    assert it["approved_paid_amount"] == 800
    assert it["pending_paid_amount"] == 400
    assert it["offline_paid_amount"] == 0
    assert (
        it["approved_paid_amount"]
        + it["pending_paid_amount"]
        + it["offline_paid_amount"]
        == it["paid_amount"]
        == 1200
    )


# ── _net_reconciliation_buckets 純函式測試 ───────────────────────────────


def test_net_buckets_excess_guard_pure():
    """excess>0 資料不一致守衛：歷史手改 paid_amount 低於 POS 軋差總額時，
    依序壓低 pending（未簽核較不權威）再壓 approved，維持不變式
    approved + pending + offline == max(0, paid) 且全部 >= 0。"""
    from api.activity.pos import _net_reconciliation_buckets

    # 只壓 pending 即可吸收殘額（excess=300 < pending_net=500）
    assert _net_reconciliation_buckets(
        approved_paid=1000,
        approved_refund=0,
        pending_paid=500,
        pending_refund=0,
        paid=1200,
    ) == (1000, 200, 0)

    # pending 壓光仍有殘額 → 再壓 approved（excess=900 → pending 0、approved 600）
    assert _net_reconciliation_buckets(
        approved_paid=1000,
        approved_refund=0,
        pending_paid=500,
        pending_refund=0,
        paid=600,
    ) == (600, 0, 0)

    # paid=0：兩 bucket 全壓光，offline 也為 0
    assert _net_reconciliation_buckets(
        approved_paid=1000,
        approved_refund=0,
        pending_paid=500,
        pending_refund=0,
        paid=0,
    ) == (0, 0, 0)

    # paid 為負（理論上不會發生）：clamp 至 0，輸出不可為負
    assert _net_reconciliation_buckets(
        approved_paid=1000,
        approved_refund=0,
        pending_paid=0,
        pending_refund=0,
        paid=-50,
    ) == (0, 0, 0)


# ── E2：payment_status 篩選須同樣套用於 inactive-with-records 分支 ──────────


def test_inactive_branch_respects_payment_status_filter(pos_client):
    """payment_status=unpaid 對帳時，有付款紀錄的 inactive 報名（paid>0、非
    unpaid）不應被納入，否則污染 totals。修正前 inactive 分支忽略 payment_status。"""
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        # 主動未繳：total=2000、paid=0 → unpaid
        active_unpaid = _setup_reg(s, student_name="未繳生", paid_amount=0)
        # 已繳但已軟刪、且仍有非作廢付款紀錄：paid=2000==total → 'paid'，非 unpaid
        inactive_paid = _setup_reg(
            s, student_name="已繳離場生", paid_amount=2000, is_paid=True
        )
        inactive_paid.is_active = False
        s.flush()
        _add_payment(s, reg_id=inactive_paid.id, amount=2000, payment_date=date.today())
        s.commit()
        active_id, inactive_id = active_unpaid.id, inactive_paid.id

    _login(client)
    data = _get(client, payment_status="unpaid").json()
    ids = {it["id"] for it in data["items"]}
    assert active_id in ids, "主動未繳應落 unpaid 篩選"
    assert inactive_id not in ids, "已繳的 inactive 報名不應出現在 unpaid 篩選"


def test_inactive_branch_included_when_no_payment_status_filter(pos_client):
    """無 payment_status 篩選時，inactive-with-records 仍須納入（對帳預設視圖不變）。"""
    client, sf = pos_client
    with sf() as s:
        _create_admin(s)
        inactive_paid = _setup_reg(
            s, student_name="已繳離場生", paid_amount=2000, is_paid=True
        )
        inactive_paid.is_active = False
        s.flush()
        _add_payment(s, reg_id=inactive_paid.id, amount=2000, payment_date=date.today())
        s.commit()
        inactive_id = inactive_paid.id

    _login(client)
    data = _get(client).json()  # 無 payment_status
    ids = {it["id"] for it in data["items"]}
    assert inactive_id in ids, "無篩選時 inactive-with-records 應仍納入對帳"

"""tests/test_batch_payment_count.py — P3 稽核準確性回歸測試。

覆蓋 batch_update_payment 的兩個 audit 問題：
(a) total=0 報名（no_fee）：is_paid 不變，卻被計入 updated 且寫 log_change。
(b) 已繳清（is_paid=True）報名：金額處理被跳過（正確），但仍寫 log_change 並計入。

修後預期：只有真正從 is_paid=False → True 的報名才計入 updated，也才寫 log_change。
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
from models.activity import RegistrationChange
from models.database import (
    ActivityCourse,
    ActivityPaymentRecord,
    ActivityRegistration,
    Base,
    RegistrationCourse,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def bpc_client(tmp_path):
    db_path = tmp_path / "bpc.sqlite"
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


def _create_admin(session) -> User:
    user = User(
        username="bpc_admin",
        password_hash=hash_password("TempPass123"),
        role="admin",
        permission_names=[
            "ACTIVITY_READ",
            "ACTIVITY_WRITE",
            "ACTIVITY_PAYMENT_APPROVE",
        ],
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient):
    return client.post(
        "/api/auth/login",
        json={"username": "bpc_admin", "password": "TempPass123"},
    )


def _make_reg(
    session,
    *,
    student_name: str,
    course_price: int,
    paid_amount: int = 0,
    is_paid: bool = False,
) -> ActivityRegistration:
    """建立一筆含一門課的報名，course 名稱以 student_name 去重。"""
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    course_name = f"測試課_{student_name}"
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
        class_name="大班",
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


# ══════════════════════════════════════════════════════════════════════
# 核心 P3 回歸測試
# ══════════════════════════════════════════════════════════════════════


class TestBatchPaymentCount:
    """批次繳費只計入/記錄真正從未繳費變已繳費的筆數。"""

    def test_only_truly_changed_counted_and_logged(self, bpc_client):
        """
        三筆報名 A/B/C：
          A: total=1000, is_paid=False, paid=0  → 真正會從 False 變 True
          B: total=500,  is_paid=True,  paid=500 → 已繳清，不應再被記錄
          C: total=0,    is_paid=False, paid=0   → 0 元免課，_compute_is_paid(0,0)=False
                                                   不應計入且不應有 log_change

        斷言：
        ① 回傳 updated == 1（只有 A 真變更）
        ② registration_changes 裡只有 A 有本次新增的「批次更新付款狀態」紀錄
           （B、C 不應有）
        ③ C 的 is_paid 仍然是 False
        """
        client, sf = bpc_client
        with sf() as s:
            _create_admin(s)
            reg_a = _make_reg(s, student_name="甲生", course_price=1000)
            reg_b = _make_reg(
                s,
                student_name="乙生",
                course_price=500,
                paid_amount=500,
                is_paid=True,
            )
            reg_c = _make_reg(s, student_name="丙生", course_price=0)
            s.commit()
            id_a, id_b, id_c = reg_a.id, reg_b.id, reg_c.id

        assert _login(client).status_code == 200

        res = client.put(
            "/api/activity/registrations/batch-payment",
            json={
                "ids": [id_a, id_b, id_c],
                "is_paid": True,
                "reason": "P3 回歸測試批次標記已繳費（測試用，勿刪）",
            },
        )
        assert res.status_code == 200, res.text
        data = res.json()

        # ① 回傳 updated 應為 1
        assert (
            data["updated"] == 1
        ), f"預期 updated=1（只有甲生真正從未繳費變已繳費），實際={data['updated']}"

        with sf() as s:
            # ② registration_changes 驗證
            changes_a = (
                s.query(RegistrationChange)
                .filter(
                    RegistrationChange.registration_id == id_a,
                    RegistrationChange.change_type == "批次更新付款狀態",
                )
                .all()
            )
            changes_b = (
                s.query(RegistrationChange)
                .filter(
                    RegistrationChange.registration_id == id_b,
                    RegistrationChange.change_type == "批次更新付款狀態",
                )
                .all()
            )
            changes_c = (
                s.query(RegistrationChange)
                .filter(
                    RegistrationChange.registration_id == id_c,
                    RegistrationChange.change_type == "批次更新付款狀態",
                )
                .all()
            )

            assert (
                len(changes_a) == 1
            ), f"甲生（A）應有 1 筆批次更新變更紀錄，實際={len(changes_a)}"
            assert (
                len(changes_b) == 0
            ), f"乙生（B）已繳清，不應有批次更新變更紀錄，實際={len(changes_b)}"
            assert (
                len(changes_c) == 0
            ), f"丙生（C）0 元免課，不應有批次更新變更紀錄，實際={len(changes_c)}"

            # ③ C 的 is_paid 仍為 False
            reg_c_db = s.get(ActivityRegistration, id_c)
            assert (
                reg_c_db.is_paid is False
            ), f"丙生（C）0 元免課，is_paid 不應被改為 True，實際={reg_c_db.is_paid}"

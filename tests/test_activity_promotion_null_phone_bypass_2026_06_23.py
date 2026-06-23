"""P1-1 回歸（2026-06-23 深度 audit）：公開 confirm/decline-promotion 的身分驗證
空對空（NULL-phone）繞過。

威脅：後台建立的報名（registrations.py create 路徑）不寫 parent_phone（NULL）也不寫
query_token_hash（NULL）→ _parent_mutation_identity_ok 走 legacy 三欄分支，phone_ok =
_normalize_phone(reg.parent_phone) == _normalize_phone(parent_phone)。_normalize_phone("-")
strip 連字號後為空字串 → None；_normalize_phone(None) → None → None == None → phone_ok=True。
攻擊者只要湊對 name+birthday（低熵）+ 送 parent_phone="-"（過 min_length=1），即可對該報名
promoted_pending 課程執行 decline（刪課釋名額）/ confirm，未經授權。

對照 /public/update 因 PublicUpdatePayload.parent_phone 有 _validate_tw_mobile validator 而
免疫（reject "-"）；confirm/decline 的 _PromotionActionPayload 缺同一道驗證即此漏洞。

DB 隔離：沿用 SQLite + monkeypatch base_module（不碰 dev PG）。
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
from api.activity.public import _public_confirm_limiter_instance
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    RegistrationCourse,
    Student,
)
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def client_sf(tmp_path):
    db_path = tmp_path / "null_phone_bypass.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    _public_confirm_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, sf

    _public_confirm_limiter_instance._timestamps.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed_course(session):
    sy, sem = _term()
    c = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
    session.add(c)
    session.add(
        ActivityCourse(
            name="圍棋", price=1000, school_year=sy, semester=sem, is_active=True
        )
    )
    session.commit()
    return s_course_id(session)


def s_course_id(session):
    return session.query(ActivityCourse).filter_by(name="圍棋").first().id


def _insert_null_phone_reg(session, course_id):
    """後台建立報名的等價：parent_phone=NULL + query_token_hash=NULL，
    且該課程已被自動升位為 promoted_pending（待家長確認）。"""
    sy, sem = _term()
    reg = ActivityRegistration(
        student_name="王小明",
        birthday="2020-05-10",
        class_name="海豚班",
        school_year=sy,
        semester=sem,
        parent_phone=None,  # ← 後台建立路徑不寫手機
        query_token_hash=None,  # ← 也不發 query_token
        is_active=True,
        paid_amount=0,
    )
    session.add(reg)
    session.flush()
    session.add(
        RegistrationCourse(
            registration_id=reg.id,
            course_id=course_id,
            price_snapshot=1000,
            status="promoted_pending",
            confirm_deadline=now_taipei_naive() + timedelta(hours=24),
        )
    )
    session.commit()
    return reg.id


def test_decline_promotion_null_phone_dash_bypass_blocked(client_sf):
    """攻擊者僅知 name+birthday，送 parent_phone='-'（normalize→None）不得繞過身分驗證
    對 NULL-phone 報名執行 decline。"""
    client, sf = client_sf
    with sf() as s:
        cid = _seed_course(s)
        rid = _insert_null_phone_reg(s, cid)

    res = client.post(
        f"/api/activity/public/registrations/{rid}/courses/{cid}/decline-promotion",
        json={
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "-",  # strip 後為空 → None；不得與 NULL reg.parent_phone 視為相符
        },
    )
    # 身分驗證須失敗（404）或 schema 驗證須擋下（422）；總之不得成功
    assert res.status_code != 200, res.text
    with sf() as s:
        rc = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=rid, course_id=cid)
            .first()
        )
        assert (
            rc is not None and rc.status == "promoted_pending"
        ), "報名不得被未授權刪改"


def test_confirm_promotion_null_phone_dash_bypass_blocked(client_sf):
    """同理，confirm 也不得被空對空繞過。"""
    client, sf = client_sf
    with sf() as s:
        cid = _seed_course(s)
        rid = _insert_null_phone_reg(s, cid)

    res = client.post(
        f"/api/activity/public/registrations/{rid}/courses/{cid}/confirm-promotion",
        json={
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "-",
        },
    )
    assert res.status_code != 200, res.text
    with sf() as s:
        rc = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=rid, course_id=cid)
            .first()
        )
        assert rc is not None and rc.status == "promoted_pending"

"""tests/test_activity_promotion_audit.py

家長端候補轉正「確認 / 放棄」端點之稽核覆蓋測試（P3）。

問題：唯一的 /api/activity/public/* audit pattern 是 /api/activity/public/update，
confirm-promotion / decline-promotion 不命中，端點內也只寫 RegistrationChange
（log_change），不寫 audit_logs。後果：家長確認候補轉正（影響名額/收費）或
放棄並刪除課程報名（釋出名額給下一位），audit_logs 無任何軌跡可溯。

修正後：兩端點顯式 write_explicit_audit（confirm=UPDATE / decline=DELETE，
entity_type=activity_registration），留下家長操作軌跡（含 IP）。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from models.audit import AuditLog
from models.database import ActivityCourse, Base, RegistrationCourse
from tests.test_activity_public_mutation_token_2026_06_17 import (
    _LIMITERS,
    _add_promoted_pending,
    _insert_reg,
    _seed,
)

_LEGACY_IDENTITY = {
    "name": "王小明",
    "birthday": "2020-05-10",
    "parent_phone": "0912345678",
}


@pytest.fixture
def client_sf(tmp_path):
    db_path = tmp_path / "promotion_audit.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_e, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    for lim in _LIMITERS:
        lim._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)
    with TestClient(app) as client:
        yield client, sf

    for lim in _LIMITERS:
        lim._timestamps.clear()
    base_module._engine = old_e
    base_module._SessionFactory = old_sf
    engine.dispose()


def _setup_promoted_pending(sf):
    """建立一筆無 token 舊報名 + promoted_pending 的「圍棋」課程項目。"""
    with sf() as s:
        _seed(s)
        rid = _insert_reg(s)  # token_plain=None → 走 legacy 三欄驗證
        cid = s.query(ActivityCourse).filter_by(name="圍棋").first().id
        _add_promoted_pending(s, rid, cid)  # commit reg + RC
    return rid, cid


def _audits_for_reg(sf, rid):
    with sf() as s:
        return (
            s.query(AuditLog)
            .filter(
                AuditLog.entity_type == "activity_registration",
                AuditLog.entity_id == str(rid),
            )
            .all()
        )


def test_confirm_promotion_writes_audit(client_sf):
    client, sf = client_sf
    rid, cid = _setup_promoted_pending(sf)

    resp = client.post(
        f"/api/activity/public/registrations/{rid}/courses/{cid}/confirm-promotion",
        json=_LEGACY_IDENTITY,
    )
    assert resp.status_code == 200, resp.json()

    audits = _audits_for_reg(sf, rid)
    assert audits, "候補轉正確認未留稽核"
    assert any(a.action == "UPDATE" for a in audits), [a.action for a in audits]


def test_confirm_expired_releases_slot_and_promotes_next(client_sf):
    """F3（2026-06-29 audit）：家長 confirm 撞到逾期 → 410，且**同步**釋出名額
    （刪除逾期 promoted_pending）並遞補下一位候補，使「名額已釋出給下一位候補」
    名實相符（不依賴預設停用的 sweeper）。

    本 test 同時涵蓋端點層風險點：confirm_waitlist_promotion 在 raise EXPIRED 後，
    endpoint 仍以同一 session 續做釋出 + commit（驗證 session 在例外後可用、不爆 500）。
    """
    from datetime import timedelta

    from models.database import ActivityRegistration
    from utils.taipei_time import now_taipei_naive

    client, sf = client_sf
    with sf() as s:
        _seed(s)
        rid = _insert_reg(s)  # 王小明（與 _LEGACY_IDENTITY 相符）
        cid = s.query(ActivityCourse).filter_by(name="圍棋").first().id
        # 第一位：逾期 promoted_pending
        s.add(
            RegistrationCourse(
                registration_id=rid,
                course_id=cid,
                price_snapshot=1000,
                status="promoted_pending",
                confirm_deadline=now_taipei_naive() - timedelta(hours=1),
            )
        )
        # 第二位：候補在後（待遞補）
        reg2 = ActivityRegistration(
            student_name="李小華",
            birthday="2020-06-06",
            class_name="海豚班",
            parent_phone="0922333444",
            is_active=True,
            paid_amount=0,
        )
        s.add(reg2)
        s.flush()
        rid2 = reg2.id
        s.add(
            RegistrationCourse(
                registration_id=rid2,
                course_id=cid,
                price_snapshot=1000,
                status="waitlist",
            )
        )
        s.commit()

    resp = client.post(
        f"/api/activity/public/registrations/{rid}/courses/{cid}/confirm-promotion",
        json=_LEGACY_IDENTITY,
    )
    assert resp.status_code == 410, resp.json()

    with sf() as s:
        rc1 = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=rid, course_id=cid)
            .first()
        )
        assert rc1 is None, "逾期 pending 應被同步釋出（刪除）"
        rc2 = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=rid2, course_id=cid)
            .first()
        )
        assert (
            rc2 is not None and rc2.status == "promoted_pending"
        ), "下一位應遞補為待確認"


def _setup_expired_with_next(sf):
    """逾期 promoted_pending（王小明）+ 候補在後（李小華）。回 (rid, rid2, cid)。"""
    from datetime import timedelta

    from models.database import ActivityRegistration
    from utils.taipei_time import now_taipei_naive

    with sf() as s:
        _seed(s)
        rid = _insert_reg(s)  # 王小明（與 _LEGACY_IDENTITY 相符）
        cid = s.query(ActivityCourse).filter_by(name="圍棋").first().id
        s.add(
            RegistrationCourse(
                registration_id=rid,
                course_id=cid,
                price_snapshot=1000,
                status="promoted_pending",
                confirm_deadline=now_taipei_naive() - timedelta(hours=1),
            )
        )
        reg2 = ActivityRegistration(
            student_name="李小華",
            birthday="2020-06-06",
            class_name="海豚班",
            parent_phone="0922333444",
            is_active=True,
            paid_amount=0,
        )
        s.add(reg2)
        s.flush()
        rid2 = reg2.id
        s.add(
            RegistrationCourse(
                registration_id=rid2,
                course_id=cid,
                price_snapshot=1000,
                status="waitlist",
            )
        )
        s.commit()
    return rid, rid2, cid


def test_confirm_expired_writes_audit(client_sf):
    """EXPIRED 同步釋出是會 commit 的破壞性跨家庭 mutation（刪逾期 pending + 遞補
    下一位），須與 confirm 成功 / decline 兩姊妹分支一致留 IP 級稽核
    （2026-06-29 audit P3-E）。AuditMiddleware 未涵蓋此 public 子路由，須端點顯式寫。
    """
    client, sf = client_sf
    rid, rid2, cid = _setup_expired_with_next(sf)

    resp = client.post(
        f"/api/activity/public/registrations/{rid}/courses/{cid}/confirm-promotion",
        json=_LEGACY_IDENTITY,
    )
    assert resp.status_code == 410, resp.json()

    audits = _audits_for_reg(sf, rid)
    assert audits, "逾期同步釋出未留稽核"
    assert any(a.action == "DELETE" for a in audits), [a.action for a in audits]


def test_decline_promotion_writes_audit(client_sf):
    client, sf = client_sf
    rid, cid = _setup_promoted_pending(sf)

    resp = client.post(
        f"/api/activity/public/registrations/{rid}/courses/{cid}/decline-promotion",
        json=_LEGACY_IDENTITY,
    )
    assert resp.status_code == 200, resp.json()

    # 放棄會刪除該課程報名（釋出名額），更需留軌跡
    with sf() as s:
        rc = (
            s.query(RegistrationCourse)
            .filter_by(registration_id=rid, course_id=cid)
            .first()
        )
        assert rc is None, "decline 應刪除該課程報名"

    audits = _audits_for_reg(sf, rid)
    assert audits, "候補轉正放棄未留稽核"
    assert any(a.action == "DELETE" for a in audits), [a.action for a in audits]

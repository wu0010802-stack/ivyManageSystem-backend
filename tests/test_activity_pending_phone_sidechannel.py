"""tests/test_activity_pending_phone_sidechannel.py

待審清單（GET /activity/registrations/pending）家長手機反查側信道（code review #5）。

問題：list_pending_registrations 對缺 GUARDIANS_READ 的使用者會遮罩輸出
parent_phone，但 search 條件仍無條件含 ActivityRegistration.parent_phone.ilike(...)。
缺權限者可用候選電話觀察「有/無命中」反查電話↔學生關聯，繞過遮罩。

修正後（對齊 /activity/students/search）：
- 缺 GUARDIANS_READ → 搜尋條件不含手機欄位（以手機反查不到）
- 有 GUARDIANS_READ → 仍可用手機搜尋
- 姓名/班級搜尋不受影響（修正只移除手機 predicate）
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import ActivityRegistration, Base
from utils.academic import resolve_current_academic_term
from tests.test_activity_pos import _create_admin, _login

_PHONE = "0912345678"
_NAME = "王小明"


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "pending_sidechannel.sqlite"
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


def _seed_pending(sf):
    """建立一筆待審核報名：student_name=王小明、phone=0912345678、當前學期。"""
    sy, sem = resolve_current_academic_term()
    with sf() as s:
        s.add(
            ActivityRegistration(
                student_name=_NAME,
                birthday="2020-05-10",
                parent_phone=_PHONE,
                school_year=sy,
                semester=sem,
                is_active=True,
                pending_review=True,
                match_status="pending",
            )
        )
        s.commit()


def _mk_user(sf, username, perms):
    with sf() as s:
        _create_admin(s, username=username, permission_names=perms)
        s.commit()


def test_pending_search_by_phone_blocked_without_guardians_read(client):
    c, sf = client
    _seed_pending(sf)
    _mk_user(sf, "act_only", ["ACTIVITY_READ", "ACTIVITY_WRITE"])
    assert _login(c, username="act_only").status_code == 200

    # 以手機號搜尋：缺 GUARDIANS_READ 時手機不在搜尋條件 → 反查不到
    res = c.get("/api/activity/registrations/pending", params={"search": _PHONE})
    assert res.status_code == 200, res.text
    assert (
        res.json()["total"] == 0
    ), "缺 GUARDIANS_READ 不應能以手機反查待審報名（手機反查側信道）"


def test_pending_search_by_name_still_works_without_guardians_read(client):
    """反回歸：修正只移除手機 predicate，姓名搜尋仍正常命中。"""
    c, sf = client
    _seed_pending(sf)
    _mk_user(sf, "act_only", ["ACTIVITY_READ", "ACTIVITY_WRITE"])
    assert _login(c, username="act_only").status_code == 200

    res = c.get("/api/activity/registrations/pending", params={"search": _NAME})
    assert res.status_code == 200, res.text
    assert res.json()["total"] == 1, "姓名搜尋不應受手機 predicate 移除影響"


def test_pending_search_by_phone_allowed_with_guardians_read(client):
    c, sf = client
    _seed_pending(sf)
    _mk_user(sf, "with_guardian", ["ACTIVITY_READ", "ACTIVITY_WRITE", "GUARDIANS_READ"])
    assert _login(c, username="with_guardian").status_code == 200

    res = c.get("/api/activity/registrations/pending", params={"search": _PHONE})
    assert res.status_code == 200, res.text
    assert res.json()["total"] == 1, "有 GUARDIANS_READ 仍應可用手機搜尋"

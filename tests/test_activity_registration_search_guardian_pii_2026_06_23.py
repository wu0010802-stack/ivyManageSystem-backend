"""tests/test_activity_registration_search_guardian_pii_2026_06_23.py

主報名列表/匯出 filter（_build_registration_filter_query）的家長手機反查把關。

問題：list 輸出在 registrations.py 依 GUARDIANS_READ 遮罩 parent_phone，但共用
filter _build_registration_filter_query 的 search 無條件把 parent_phone.ilike(...)
放進 or_(...)。結果持 ACTIVITY_READ 但無 GUARDIANS_READ 的人，仍能用部分手機號
搜尋 /registrations、/registrations/export、/registrations/payment-report 是否
命中某學生（側信道反查），與 students/search（A1）同類。

修正後：
- 缺 GUARDIANS_READ → search 不含 parent_phone 欄位（關閉手機反查側信道）
- 有 GUARDIANS_READ → 可用手機搜尋
- 姓名/班級搜尋不受權限影響（只移除手機 clause，非關閉整個 search）
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.activity._shared import _build_registration_filter_query
from models.database import ActivityRegistration, Base

_PHONE = "0912345678"

_NO_GUARDIAN = {"permission_names": ["ACTIVITY_READ"]}
_WITH_GUARDIAN = {"permission_names": ["ACTIVITY_READ", "GUARDIANS_READ"]}


@pytest.fixture
def sf(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rsg.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _mk_reg(session, *, name: str, phone: str) -> int:
    reg = ActivityRegistration(
        student_name=name,
        birthday="2020-01-01",
        class_name="大班",
        parent_phone=phone,
        paid_amount=0,
        is_active=True,
        school_year=114,
        semester=1,
    )
    session.add(reg)
    session.flush()
    return reg.id


def _search_ids(session, q, current_user):
    return [
        r.id
        for r in _build_registration_filter_query(
            session, search=q, current_user=current_user
        ).all()
    ]


def test_phone_search_blocked_without_guardians_read(sf):
    with sf() as s:
        rid = _mk_reg(s, name="王小明", phone=_PHONE)
        s.commit()
        # 用部分手機號搜尋（姓名/班級皆不含此字串），缺 GUARDIANS_READ 應反查不到
        ids = _search_ids(s, "0912", _NO_GUARDIAN)
    assert rid not in ids, "缺 GUARDIANS_READ 不應能以手機反查報名"
    assert ids == []


def test_phone_search_allowed_with_guardians_read(sf):
    with sf() as s:
        rid = _mk_reg(s, name="王小明", phone=_PHONE)
        s.commit()
        ids = _search_ids(s, "0912", _WITH_GUARDIAN)
    assert ids == [rid], "持 GUARDIANS_READ 仍可用手機搜尋"


def test_name_search_unaffected_by_guardian_perm(sf):
    with sf() as s:
        rid = _mk_reg(s, name="王小明", phone=_PHONE)
        s.commit()
        # 姓名搜尋與權限無關（只移除手機 clause，不應關閉整個 search）
        ids = _search_ids(s, "王小明", _NO_GUARDIAN)
    assert ids == [rid], "姓名搜尋不受 GUARDIANS_READ 影響"


# ── HTTP 端到端：鎖住 /registrations 端點確實把 current_user 傳進 filter ──────────

import models.base as base_module  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.activity import router as activity_router  # noqa: E402
from api.auth import _account_failures, _ip_attempts  # noqa: E402
from api.auth import router as auth_router  # noqa: E402
from models.database import User  # noqa: E402
from utils.auth import hash_password  # noqa: E402

_HTTP_PHONE = "0988777666"
_PASSWORD = "Temp123456"


@pytest.fixture
def http_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'rsg_http.sqlite'}",
        connect_args={"check_same_thread": False},
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


def _seed_http(sf, username, perms):
    with sf() as s:
        s.add(
            User(
                username=username,
                password_hash=hash_password(_PASSWORD),
                role="hr",
                permission_names=perms,
                is_active=True,
            )
        )
        # 姓名不含手機字串，確保命中只可能來自 parent_phone 比對
        s.add(
            ActivityRegistration(
                student_name="林大華",
                birthday="2020-01-01",
                class_name="大班",
                parent_phone=_HTTP_PHONE,
                paid_amount=0,
                is_active=True,
                school_year=114,
                semester=1,
            )
        )
        s.commit()


def _login_http(c, username):
    r = c.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    assert r.status_code == 200, r.text


def test_registrations_phone_reverse_lookup_blocked_without_guardians_read(http_client):
    c, sf = http_client
    _seed_http(sf, "clerk_noguard", ["ACTIVITY_READ"])
    _login_http(c, "clerk_noguard")

    res = c.get(
        "/api/activity/registrations",
        params={"search": "0988", "school_year": 114, "semester": 1},
    )
    assert res.status_code == 200, res.text
    assert res.json()["items"] == [], "缺 GUARDIANS_READ 不應能透過列表以手機反查"


def test_registrations_phone_search_allowed_with_guardians_read(http_client):
    c, sf = http_client
    _seed_http(sf, "clerk_guard", ["ACTIVITY_READ", "GUARDIANS_READ"])
    _login_http(c, "clerk_guard")

    res = c.get(
        "/api/activity/registrations",
        params={"search": "0988", "school_year": 114, "semester": 1},
    )
    assert res.status_code == 200, res.text
    items = res.json()["items"]
    assert len(items) == 1, "持 GUARDIANS_READ 仍可用手機搜尋列表"
    assert items[0]["parent_phone"] == _HTTP_PHONE

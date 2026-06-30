"""才藝公開端 code review #3（2026-06-22）修補回歸測試。

涵蓋四個 finding：
- Finding 1：無 settings 設定列時 /public/registration-time 應回 is_open=True，
  與 _check_registration_open 放行行為一致（業主裁：維持放行）。
- Finding 2：手足共用家長電話時，第二筆（不同姓名/生日）應正常寫入，不被
  phone-only soft-dedup 靜默丟棄；同一學生（同 name+birthday+phone）重送仍 dedup。
- Finding 5：公開報名不得建立完全空白報名（courses 與 supplies 皆空 → 422），
  比照家長端「至少一門課程或一項用品」。
- Finding 8：班級欄位需有 max_length=50（對齊 DB VARCHAR(50)），避免 PG 超長 500。
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import _public_register_limiter_instance
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    ActivityRegistrationSettings,
    Base,
    Classroom,
)
from schemas.activity_public import PublicRegistrationPayload, PublicUpdatePayload


@pytest.fixture
def public_client(tmp_path):
    db_path = tmp_path / "activity-public-review.sqlite"
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
    _public_register_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _public_register_limiter_instance._timestamps.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _current_term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed_for_register(session, *, is_open=True, course_name="圍棋"):
    sy, sem = _current_term()
    if is_open is not None:
        session.add(ActivityRegistrationSettings(id=1, is_open=is_open))
    session.add(Classroom(name="大象班", is_active=True, school_year=sy, semester=sem))
    session.add(
        ActivityCourse(
            name=course_name,
            price=1200,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
    )
    session.commit()


def _register_payload(*, name="王小明", birthday="2020-05-10", phone="0912345678"):
    return {
        "name": name,
        "birthday": birthday,
        "parent_phone": phone,
        "class": "大象班",
        "courses": [{"name": "圍棋", "price": "1"}],
        "supplies": [],
    }


def _valid_payload_dict(**overrides):
    base = {
        "name": "王小明",
        "birthday": "2020-05-10",
        "class": "大象班",
        "parent_phone": "0912345678",
        "courses": [{"name": "圍棋"}],
        "supplies": [],
    }
    base.update(overrides)
    return base


# ── Finding 1：無 settings 時 registration-time 與放行行為一致 ──────────────


class TestRegistrationTimeNoSettings:
    def test_registration_time_open_when_no_settings(self, public_client):
        """無 settings 列時，_check_registration_open 放行 → registration-time
        也應回 is_open=True，避免 UI 顯示關閉但 API 實際開放的不一致。"""
        client, _ = public_client
        res = client.get("/api/activity/public/registration-time")
        assert res.status_code == 200
        assert res.json()["is_open"] is True

    def test_register_succeeds_when_no_settings(self, public_client):
        """無 settings 列時報名仍可成功（維持放行口徑）。"""
        client, sf = public_client
        with sf() as s:
            _seed_for_register(s, is_open=None)  # 不建 settings 列
        res = client.post("/api/activity/public/register", json=_register_payload())
        assert res.status_code == 201


# ── Finding 2：手足共用家長電話 ────────────────────────────────────────────


class TestSiblingSharedPhone:
    def test_siblings_same_phone_both_saved(self, public_client):
        """同一家長電話、不同姓名/生日的兩個孩子，兩筆報名都應寫入。"""
        client, sf = public_client
        with sf() as s:
            _seed_for_register(s)

        r1 = client.post(
            "/api/activity/public/register",
            json=_register_payload(name="王小明", birthday="2020-05-10"),
        )
        assert r1.status_code == 201
        r2 = client.post(
            "/api/activity/public/register",
            json=_register_payload(name="王小美", birthday="2019-03-03"),
        )
        assert r2.status_code == 201

        with sf() as s:
            count = s.query(ActivityRegistration).count()
        assert count == 2, f"手足兩筆都應寫入，實際 {count}"

    def test_exact_duplicate_child_still_deduped(self, public_client):
        """同一學生（同 name+birthday+phone）重送仍只留一筆（既有去重不退化）。"""
        client, sf = public_client
        with sf() as s:
            _seed_for_register(s)

        r1 = client.post("/api/activity/public/register", json=_register_payload())
        assert r1.status_code == 201
        r2 = client.post("/api/activity/public/register", json=_register_payload())
        assert r2.status_code == 201
        with sf() as s:
            count = s.query(ActivityRegistration).count()
        assert count == 1, f"完全相同的重送應 dedup 成一筆，實際 {count}"


# ── Finding 5：禁空白報名 ──────────────────────────────────────────────────


class TestNoEmptyRegistration:
    def test_register_payload_rejects_empty_courses_and_supplies(self):
        with pytest.raises(ValidationError):
            PublicRegistrationPayload.model_validate(
                _valid_payload_dict(courses=[], supplies=[])
            )

    def test_update_payload_allows_empty_courses(self):
        """update 清空課程是合法流程（觸發退費），不套 finding 5 守衛。"""
        obj = PublicUpdatePayload.model_validate(
            _valid_payload_dict(id=1, courses=[], supplies=[])
        )
        assert obj.courses == [] and obj.supplies == []

    def test_register_payload_accepts_only_supply(self):
        """只報用品、無課程也算有效（≥1 項即可）。"""
        obj = PublicRegistrationPayload.model_validate(
            _valid_payload_dict(courses=[], supplies=[{"name": "畫具"}])
        )
        assert obj.supplies and not obj.courses

    def test_register_endpoint_rejects_empty(self, public_client):
        client, sf = public_client
        with sf() as s:
            _seed_for_register(s)
        payload = _register_payload()
        payload["courses"] = []
        payload["supplies"] = []
        res = client.post("/api/activity/public/register", json=payload)
        assert res.status_code == 422


# ── Finding 8：班級長度上限 ────────────────────────────────────────────────


class TestClassNameLength:
    def test_register_payload_rejects_overlong_class(self):
        with pytest.raises(ValidationError):
            PublicRegistrationPayload.model_validate(
                _valid_payload_dict(**{"class": "班" * 51})
            )

    def test_update_payload_rejects_overlong_class(self):
        with pytest.raises(ValidationError):
            PublicUpdatePayload.model_validate(
                _valid_payload_dict(id=1, **{"class": "班" * 51})
            )

    def test_register_payload_accepts_max_length_class(self):
        obj = PublicRegistrationPayload.model_validate(
            _valid_payload_dict(**{"class": "班" * 50})
        )
        assert len(obj.class_) == 50


# ── 2026-06-29 audit P3-D：清單內 item name 長度上限 ────────────────────────
# courses/supplies 每項 name 為公開端唯二未受限的字串欄；對齊 ActivityCourse.name /
# ActivitySupply.name 的 VARCHAR(100)，補 1..100 防 DoS 級超長 payload（與本模組
# parent_phone/remark/class 等既有上限政策一致）。


class TestItemNameLength:
    def test_register_payload_rejects_overlong_course_name(self):
        with pytest.raises(ValidationError):
            PublicRegistrationPayload.model_validate(
                _valid_payload_dict(courses=[{"name": "課" * 101}])
            )

    def test_register_payload_rejects_overlong_supply_name(self):
        with pytest.raises(ValidationError):
            PublicRegistrationPayload.model_validate(
                _valid_payload_dict(courses=[], supplies=[{"name": "品" * 101}])
            )

    def test_register_payload_rejects_empty_course_name(self):
        with pytest.raises(ValidationError):
            PublicRegistrationPayload.model_validate(
                _valid_payload_dict(courses=[{"name": ""}])
            )

    def test_update_payload_rejects_overlong_course_name(self):
        with pytest.raises(ValidationError):
            PublicUpdatePayload.model_validate(
                _valid_payload_dict(id=1, courses=[{"name": "課" * 101}])
            )

    def test_register_payload_accepts_max_length_course_name(self):
        obj = PublicRegistrationPayload.model_validate(
            _valid_payload_dict(courses=[{"name": "課" * 100}])
        )
        assert len(obj.courses[0].name) == 100

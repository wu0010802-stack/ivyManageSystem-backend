"""tests/test_activity_availability_cache.py — availability 10s TTL 快取測試。

驗證：
1. 連打兩次 /public/courses/availability，第二次不重跑聚合 query（hit cache）
2. 清快取後重打 → 重算（miss，計數+1）
3. 快取期間 ETag/304 行為仍正確
4. 快取命中時不開 DB session（完整省掉 DB round-trip）

測試策略：把聚合邏輯抽出後可 spy 的函式 `_compute_availability`，透過
unittest.mock.patch 計數呼叫次數。
"""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from models.database import (
    Base,
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)
from utils.cache_layer import get_cache, reset_cache_for_testing

# ── 常數 ────────────────────────────────────────────────────────────────────────
PATH = "/api/activity/public/courses/availability"
_NS = "public_availability"  # 與 public.py 中的 namespace 對齊


# ── Fixtures ─────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_cache():
    """每個 test 前後清快取，避免狀態滲漏。"""
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


@pytest.fixture
def avail_client(tmp_path):
    db_path = tmp_path / "activity-avail-cache.sqlite"
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

    app = FastAPI()
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_course(session, name="鋼琴", capacity=20):
    from utils.academic import resolve_current_academic_term

    sy, sem = resolve_current_academic_term()
    session.add(
        ActivityCourse(
            name=name,
            price=1000,
            sessions=10,
            capacity=capacity,
            allow_waitlist=True,
            is_active=True,
            school_year=sy,
            semester=sem,
        )
    )
    session.commit()


# ── Tests ─────────────────────────────────────────────────────────────────────────


class TestAvailabilityCache:
    """快取命中：第二次不重跑聚合。"""

    def test_second_request_hits_cache(self, avail_client):
        """連打兩次，_compute_availability 只被呼叫一次。"""
        client, session_factory = avail_client
        with session_factory() as session:
            _seed_course(session)

        call_count = {"n": 0}

        # 取得原始函式
        import api.activity.public as pub_mod

        original = pub_mod._compute_availability

        def counting_compute(session):
            call_count["n"] += 1
            return original(session)

        with patch.object(pub_mod, "_compute_availability", counting_compute):
            r1 = client.get(PATH)
            r2 = client.get(PATH)

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert (
            call_count["n"] == 1
        ), f"_compute_availability 應只呼叫 1 次（快取命中），實際 {call_count['n']} 次"

    def test_after_cache_clear_recomputes(self, avail_client):
        """清快取後，下次請求重算（計數+1）。"""
        client, session_factory = avail_client
        with session_factory() as session:
            _seed_course(session)

        call_count = {"n": 0}

        import api.activity.public as pub_mod

        original = pub_mod._compute_availability

        def counting_compute(session):
            call_count["n"] += 1
            return original(session)

        with patch.object(pub_mod, "_compute_availability", counting_compute):
            # 第一次 miss → 計算
            client.get(PATH)
            assert call_count["n"] == 1

            # 清快取（模擬 TTL 到期）
            get_cache().clear_namespace(pub_mod._CACHE_NS_PUBLIC_AVAILABILITY)

            # 第二次 miss → 重算
            client.get(PATH)
            assert (
                call_count["n"] == 2
            ), f"清快取後應重算，呼叫次數應為 2，實際 {call_count['n']}"

    def test_third_request_after_two_hits_still_cached(self, avail_client):
        """三次連打：只算一次（第 2、3 次均 hit）。"""
        client, session_factory = avail_client
        with session_factory() as session:
            _seed_course(session)

        call_count = {"n": 0}

        import api.activity.public as pub_mod

        original = pub_mod._compute_availability

        def counting_compute(session):
            call_count["n"] += 1
            return original(session)

        with patch.object(pub_mod, "_compute_availability", counting_compute):
            for _ in range(3):
                client.get(PATH)

        assert call_count["n"] == 1, f"三次連打應只計算一次，實際 {call_count['n']} 次"


class TestAvailabilityCacheETagBehavior:
    """快取期間 ETag/304 行為仍正確。"""

    def test_etag_present_with_cache(self, avail_client):
        """快取命中時仍帶 ETag header。"""
        client, session_factory = avail_client
        with session_factory() as session:
            _seed_course(session)

        r1 = client.get(PATH)
        assert r1.status_code == 200
        etag1 = r1.headers.get("ETag")
        assert etag1, "第一次請求應帶 ETag"

        # 第二次（快取命中）
        r2 = client.get(PATH)
        assert r2.status_code == 200
        etag2 = r2.headers.get("ETag")
        assert etag2, "快取命中時仍應帶 ETag"
        assert etag1 == etag2, "相同資料的 ETag 應一致"

    def test_304_on_if_none_match_with_cache(self, avail_client):
        """快取命中期間，帶 If-None-Match 仍回 304。"""
        client, session_factory = avail_client
        with session_factory() as session:
            _seed_course(session)

        r1 = client.get(PATH)
        etag = r1.headers.get("ETag")
        assert etag

        # 第二次帶 If-None-Match → 應 304
        r2 = client.get(PATH, headers={"If-None-Match": etag})
        assert (
            r2.status_code == 304
        ), f"快取命中時帶 If-None-Match 應回 304，實際 {r2.status_code}"

    def test_cache_control_no_cache(self, avail_client):
        """Cache-Control 應為 no-cache（不論 cache hit/miss）。"""
        client, session_factory = avail_client
        with session_factory() as session:
            _seed_course(session)

        for _ in range(2):
            r = client.get(PATH)
            cc = r.headers.get("Cache-Control", "")
            assert "no-cache" in cc, f"Cache-Control 應含 no-cache，實際: {cc!r}"


class TestAvailabilityCacheCorrectness:
    """快取返回資料正確性。"""

    def test_availability_values_correct(self, avail_client):
        """availability 值計算正確（capacity - enrolled）。"""
        client, session_factory = avail_client
        with session_factory() as session:
            _seed_course(session, name="圍棋", capacity=5)

        r = client.get(PATH)
        assert r.status_code == 200
        data = r.json()
        assert "圍棋" in data
        assert data["圍棋"] == 5  # 無人報名，全空名額

    def test_empty_courses_returns_empty_dict(self, avail_client):
        """無課程時回空 dict，快取也不壞。"""
        client, _ = avail_client
        r1 = client.get(PATH)
        assert r1.status_code == 200
        assert r1.json() == {}

        # 第二次 hit
        r2 = client.get(PATH)
        assert r2.status_code == 200
        assert r2.json() == {}

"""GET /public/bootstrap 一次回傳報名頁靜態資料 + 30s 快取的回歸測試。

背景：上線穩定度稽核（2026-06-23）——報名開放尖峰每位家長開頁並發打 5 支 GET，
對單 worker 後端造成放大。新增 bootstrap 端點合併為 1 支並加 process-global 快取。
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
from api.activity.public import _CACHE_NS_PUBLIC_BOOTSTRAP
from models.database import (
    ActivityCourse,
    ActivityRegistrationSettings,
    ActivitySupply,
    Base,
    Classroom,
)
from utils.cache_layer import get_cache


@pytest.fixture
def public_client(tmp_path):
    db_path = tmp_path / "activity-bootstrap.sqlite"
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
    # process-global 快取跨測試殘留，必須清掉避免上一個測試的 bundle 洩漏
    get_cache().clear_namespace(_CACHE_NS_PUBLIC_BOOTSTRAP)

    app = FastAPI()
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    get_cache().clear_namespace(_CACHE_NS_PUBLIC_BOOTSTRAP)
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _current_term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed(session, *, course_name="圍棋"):
    sy, sem = _current_term()
    session.add(ActivityRegistrationSettings(id=1, is_open=True, page_title="才藝報名"))
    session.add(Classroom(name="大象班", is_active=True, school_year=sy, semester=sem))
    session.add(
        ActivityCourse(
            name=course_name,
            price=1200,
            school_year=sy,
            semester=sem,
            is_active=True,
            video_url="https://example.com/v.mp4",
        )
    )
    session.add(
        ActivitySupply(
            name="圍棋墊", price=300, school_year=sy, semester=sem, is_active=True
        )
    )
    session.commit()


def test_bootstrap_returns_all_sections(public_client):
    client, sf = public_client
    with sf() as s:
        _seed(s)

    res = client.get("/api/activity/public/bootstrap")
    assert res.status_code == 200
    data = res.json()
    assert set(data.keys()) == {
        "registration_time",
        "courses",
        "supplies",
        "classes",
        "course_videos",
    }
    assert data["registration_time"]["is_open"] is True
    assert data["registration_time"]["page_title"] == "才藝報名"
    assert [c["name"] for c in data["courses"]] == ["圍棋"]
    assert [s["name"] for s in data["supplies"]] == ["圍棋墊"]
    assert data["classes"] == ["大象班"]
    assert data["course_videos"] == {"圍棋": "https://example.com/v.mp4"}


def test_bootstrap_matches_individual_endpoints(public_client):
    """bootstrap 各區塊應與個別端點回傳一致（共用 builder，無漂移）。"""
    client, sf = public_client
    with sf() as s:
        _seed(s)

    boot = client.get("/api/activity/public/bootstrap").json()
    assert boot["courses"] == client.get("/api/activity/public/courses").json()
    assert boot["supplies"] == client.get("/api/activity/public/supplies").json()
    assert boot["classes"] == client.get("/api/activity/public/classes").json()
    assert (
        boot["course_videos"] == client.get("/api/activity/public/course-videos").json()
    )


def test_bootstrap_is_cached(public_client):
    """30s 快取：第一次後新增課程，TTL 內第二次仍回舊 bundle（證明 cache hit 跳過 DB）。"""
    client, sf = public_client
    with sf() as s:
        _seed(s)

    first = client.get("/api/activity/public/bootstrap").json()
    assert len(first["courses"]) == 1

    # 新增第二門課；若未快取，bootstrap 會回 2 門
    with sf() as s:
        sy, sem = _current_term()
        s.add(
            ActivityCourse(
                name="畫畫",
                price=900,
                school_year=sy,
                semester=sem,
                is_active=True,
            )
        )
        s.commit()

    cached = client.get("/api/activity/public/bootstrap").json()
    assert len(cached["courses"]) == 1  # 仍是快取的舊值

    # 清快取後應反映新課程
    get_cache().clear_namespace(_CACHE_NS_PUBLIC_BOOTSTRAP)
    fresh = client.get("/api/activity/public/bootstrap").json()
    assert len(fresh["courses"]) == 2

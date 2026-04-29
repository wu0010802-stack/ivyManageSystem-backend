"""才藝公開端點 ETag/no-cache 行為測試。

確認以下端點：
- /public/courses
- /public/supplies
- /public/classes
- /public/course-videos
- /public/registration-time

均符合：
1. 回應帶 ETag、Cache-Control 為 no-cache（不是 max-age=300）
2. 帶 If-None-Match 時回 304
3. 後台異動內容後 ETag 會變、原 If-None-Match 不再命中

Why: 原本以 max-age=300 主動快取，導致後台新增課程後前台需等 5~6 分鐘才出現。
改成 ETag + no-cache 讓資料即時、命中時 304 仍只回 header。
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
from models.database import (
    Base,
    ActivityCourse,
    ActivitySupply,
    ActivityRegistrationSettings,
    Classroom,
)


@pytest.fixture
def public_client(tmp_path):
    db_path = tmp_path / "activity-public-etag.sqlite"
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


def _current_term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


# 五個受測端點 + 各自的初始資料 seeder
def _seed_courses(session):
    sy, sem = _current_term()
    session.add(
        ActivityCourse(
            name="圍棋",
            price=1200,
            sessions=12,
            capacity=30,
            allow_waitlist=True,
            is_active=True,
            school_year=sy,
            semester=sem,
        )
    )
    session.commit()


def _seed_supplies(session):
    sy, sem = _current_term()
    session.add(
        ActivitySupply(
            name="畫具組",
            price=350,
            is_active=True,
            school_year=sy,
            semester=sem,
        )
    )
    session.commit()


def _seed_classes(session):
    session.add(Classroom(name="海豚班", is_active=True))
    session.commit()


def _seed_course_video(session):
    sy, sem = _current_term()
    session.add(
        ActivityCourse(
            name="珠心算",
            price=1000,
            sessions=10,
            capacity=20,
            video_url="https://example.com/intro.mp4",
            is_active=True,
            school_year=sy,
            semester=sem,
        )
    )
    session.commit()


def _seed_registration_time(session):
    session.add(
        ActivityRegistrationSettings(
            is_open=True,
            open_at="2026-04-01T08:00",
            close_at="2026-05-31T18:00",
            page_title="春季才藝報名",
        )
    )
    session.commit()


def _add_course(session, name: str = "陶藝", video_url: str = ""):
    sy, sem = _current_term()
    session.add(
        ActivityCourse(
            name=name,
            price=900,
            sessions=8,
            capacity=15,
            video_url=video_url,
            allow_waitlist=True,
            is_active=True,
            school_year=sy,
            semester=sem,
        )
    )
    session.commit()


def _add_supply(session, name: str = "美術紙"):
    sy, sem = _current_term()
    session.add(
        ActivitySupply(
            name=name,
            price=80,
            is_active=True,
            school_year=sy,
            semester=sem,
        )
    )
    session.commit()


def _add_classroom(session, name: str = "鯨魚班"):
    session.add(Classroom(name=name, is_active=True))
    session.commit()


def _change_settings_title(session, new_title: str = "夏季才藝報名"):
    settings = session.query(ActivityRegistrationSettings).first()
    settings.page_title = new_title
    session.commit()


# parametrize: (path, seeder, mutator)
ENDPOINTS = [
    ("/api/activity/public/courses", _seed_courses, _add_course),
    ("/api/activity/public/supplies", _seed_supplies, _add_supply),
    ("/api/activity/public/classes", _seed_classes, _add_classroom),
    (
        "/api/activity/public/course-videos",
        _seed_course_video,
        lambda s: _add_course(s, "新影片課", "https://example.com/new.mp4"),
    ),
    (
        "/api/activity/public/registration-time",
        _seed_registration_time,
        _change_settings_title,
    ),
]


@pytest.mark.parametrize("path,seeder,_mutator", ENDPOINTS)
def test_public_endpoint_returns_etag_and_no_cache(
    public_client, path, seeder, _mutator
):
    client, session_factory = public_client
    with session_factory() as session:
        seeder(session)

    res = client.get(path)
    assert res.status_code == 200
    etag = res.headers.get("ETag")
    assert etag, f"{path} 應該回傳 ETag header"
    assert etag.startswith('"') and etag.endswith('"'), "ETag 應為 quoted string"
    cache_control = res.headers.get("Cache-Control", "")
    assert (
        "no-cache" in cache_control
    ), f"{path} 應使用 no-cache（即時 + ETag 驗證），實際: {cache_control!r}"
    # 不可再帶 max-age（避免重新引入延遲）
    assert "max-age=300" not in cache_control
    assert "max-age=600" not in cache_control


@pytest.mark.parametrize("path,seeder,_mutator", ENDPOINTS)
def test_public_endpoint_returns_304_on_if_none_match(
    public_client, path, seeder, _mutator
):
    client, session_factory = public_client
    with session_factory() as session:
        seeder(session)

    first = client.get(path)
    etag = first.headers.get("ETag")
    assert etag

    second = client.get(path, headers={"If-None-Match": etag})
    assert second.status_code == 304
    # 304 body 應為空
    assert second.content in (b"", None)


@pytest.mark.parametrize("path,seeder,mutator", ENDPOINTS)
def test_public_endpoint_etag_changes_after_mutation(
    public_client, path, seeder, mutator
):
    client, session_factory = public_client
    with session_factory() as session:
        seeder(session)

    first = client.get(path)
    old_etag = first.headers.get("ETag")
    assert old_etag

    with session_factory() as session:
        mutator(session)

    second = client.get(path, headers={"If-None-Match": old_etag})
    assert (
        second.status_code == 200
    ), f"{path} 資料異動後不應再回 304，實際: {second.status_code}"
    new_etag = second.headers.get("ETag")
    assert new_etag and new_etag != old_etag

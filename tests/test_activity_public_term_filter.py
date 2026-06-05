"""tests/test_activity_public_term_filter.py

E1（2026-06-06 QA live 發現）：公開報名端的讀取端點未過濾學年/學期。

`/public/courses`、`/public/supplies`、`/public/courses/availability`
原本只過濾 `is_active`，沒有過濾 school_year/semester。當系統存在上一學期
（或「複製上學期」留下）的 is_active 課程/用品時，家長公開報名頁會把
**所有學期**的項目全列出來（同名重複），且 availability 以 course.name 當 key
跨學期碰撞 → 顯示錯學期的剩餘名額。

註：register 寫入路徑本來就用 resolve_academic_term_filters 過濾當學期，
故報名不會進錯學期；本組測試鎖定「讀取端點也必須只回當學期」。
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
from models.database import ActivityCourse, ActivitySupply, Base
from utils.academic import resolve_current_academic_term
from utils.cache_layer import get_cache


@pytest.fixture
def term_client(tmp_path):
    db_path = tmp_path / "term_filter.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    # 清掉跨測試殘留的 availability 記憶體快取，避免污染
    get_cache().clear_namespace("public_availability")

    app = FastAPI()
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    get_cache().clear_namespace("public_availability")
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_two_terms(session):
    """當前學期 + 前一學年（非當前）各放 active 課程/用品，且故意同名。"""
    cur_sy, cur_sem = resolve_current_academic_term()
    prev_sy = cur_sy - 1  # 同 semester、前一學年 → 保證為「非當前學期」

    # 當前學期：陶藝 容量 10
    session.add(
        ActivityCourse(
            name="陶藝",
            price=2400,
            capacity=10,
            school_year=cur_sy,
            semester=cur_sem,
            is_active=True,
        )
    )
    session.add(
        ActivitySupply(
            name="材料包",
            price=300,
            school_year=cur_sy,
            semester=cur_sem,
            is_active=True,
        )
    )
    # 前一學年（非當前，但 is_active=True）：同名「陶藝」容量 5 + 獨有「舊課」
    session.add(
        ActivityCourse(
            name="陶藝",
            price=2400,
            capacity=5,
            school_year=prev_sy,
            semester=cur_sem,
            is_active=True,
        )
    )
    session.add(
        ActivityCourse(
            name="舊課",
            price=999,
            capacity=8,
            school_year=prev_sy,
            semester=cur_sem,
            is_active=True,
        )
    )
    session.add(
        ActivitySupply(
            name="材料包",
            price=300,
            school_year=prev_sy,
            semester=cur_sem,
            is_active=True,
        )
    )
    session.add(
        ActivitySupply(
            name="舊用品",
            price=50,
            school_year=prev_sy,
            semester=cur_sem,
            is_active=True,
        )
    )
    session.commit()


def test_public_courses_only_current_term(term_client):
    """/public/courses 只回當學期課程，不含上一學年的同名/獨有課程。"""
    client, sf = term_client
    session = sf()
    try:
        _seed_two_terms(session)
    finally:
        session.close()

    res = client.get("/api/activity/public/courses")
    assert res.status_code == 200
    names = [c["name"] for c in res.json()]
    assert names == ["陶藝"], f"應只回當學期 1 門課，實得 {names}"


def test_public_supplies_only_current_term(term_client):
    """/public/supplies 只回當學期用品。"""
    client, sf = term_client
    session = sf()
    try:
        _seed_two_terms(session)
    finally:
        session.close()

    res = client.get("/api/activity/public/supplies")
    assert res.status_code == 200
    names = [s["name"] for s in res.json()]
    assert names == ["材料包"], f"應只回當學期 1 項用品，實得 {names}"


def test_public_availability_only_current_term_no_name_collision(term_client):
    """/public/courses/availability 只含當學期課名，且剩餘名額為當學期數字。

    跨學期同名（陶藝 當學期容量 10 / 前學年容量 5）以 name 當 key 會碰撞；
    修正後 availability 應只有當學期的「陶藝」且 = 10，不含「舊課」。
    """
    client, sf = term_client
    session = sf()
    try:
        _seed_two_terms(session)
    finally:
        session.close()

    get_cache().clear_namespace("public_availability")
    res = client.get("/api/activity/public/courses/availability")
    assert res.status_code == 200
    availability = res.json()
    assert set(availability.keys()) == {
        "陶藝"
    }, f"availability 應只含當學期課名，實得 {sorted(availability.keys())}"
    assert (
        availability["陶藝"] == 10
    ), f"剩餘名額應為當學期容量 10（非前學年 5），實得 {availability['陶藝']}"

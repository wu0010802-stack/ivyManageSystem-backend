"""tests/test_activity_public_update_vacated_lock_2026_06_24.py

P3-5（2026-06-24 才藝模組稽核）：public_update 退課所釋出的 vacated 課程未納入
開頭的 order_by(ActivityCourse.id) 批次列鎖——它延後到尾段 _auto_promote_first_waitlist
才首次取鎖（已在 ActivityRegistration 列鎖之後）。兩筆 desired/vacated 課程集合相反
的並發 public_update 因此可 ABBA（雖已由 _set_hot_path_lock_timeout(3s) +
is_lock_contention_error→409 緩解，仍補齊鎖序一致性）。

修法：開頭以未鎖預讀取得本筆報名目前佔位課程 id，與 desired 課程併成單一
order_by(id) 批次列鎖（reg 列鎖之前）。

測法：spy with_for_update 並以 literal_binds 編譯每個 ActivityCourse 鎖查詢的 SQL，
斷言「在 ActivityRegistration 被鎖之前」就有一個 course 鎖查詢涵蓋 vacated 課程 id。
"""

import os
import sys
from datetime import date

import pytest
import sqlalchemy.orm
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import (
    _public_query_limiter_instance,
    _public_register_limiter_instance,
)
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySupply,
    Base,
    Classroom,
    Student,
)


@pytest.fixture
def client_and_sf(tmp_path):
    db_path = tmp_path / "vacated_lock.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    _public_register_limiter_instance._timestamps.clear()
    _public_query_limiter_instance._timestamps.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _term():
    from utils.academic import resolve_current_academic_term

    return resolve_current_academic_term()


def _seed(session):
    sy, sem = _term()
    classroom = Classroom(name="海豚班", is_active=True, school_year=sy, semester=sem)
    session.add(classroom)
    session.flush()
    for nm, pr in (("圍棋", 1000), ("畫畫", 800)):
        session.add(
            ActivityCourse(
                name=nm, price=pr, school_year=sy, semester=sem, is_active=True
            )
        )
    session.add(
        ActivitySupply(
            name="畫具", price=200, school_year=sy, semester=sem, is_active=True
        )
    )
    session.add(
        Student(
            student_id="S001",
            name="王小明",
            birthday=date(2020, 5, 10),
            classroom_id=classroom.id,
            parent_phone="0912345678",
            is_active=True,
        )
    )
    session.commit()


def test_public_update_locks_vacated_course_before_registration(
    client_and_sf, monkeypatch
):
    """退課釋出的 vacated 課程須在 ActivityRegistration 列鎖之前就被 course 列鎖涵蓋。"""
    client, sf = client_and_sf
    with sf() as s:
        _seed(s)

    # 報名圍棋 + 畫畫
    reg_resp = client.post(
        "/api/activity/public/register",
        json={
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "0912345678",
            "class": "海豚班",
            "courses": [
                {"name": "圍棋", "price": "1000"},
                {"name": "畫畫", "price": "800"},
            ],
            "supplies": [],
        },
    )
    assert reg_resp.status_code in (200, 201), reg_resp.text
    token = reg_resp.json()["query_token"]
    q = client.post(
        "/api/activity/public/query",
        json={"name": "王小明", "birthday": "2020-05-10", "parent_phone": "0912345678"},
    ).json()

    with sf() as s:
        painting_id = (
            s.query(ActivityCourse.id).filter(ActivityCourse.name == "畫畫").scalar()
        )

    # spy：記錄 (entity, 該鎖查詢的 literal_binds SQL)
    recorded = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        entity = cds[0]["entity"] if cds else None
        sql = None
        if entity is ActivityCourse:
            try:
                sql = str(
                    self.statement.compile(compile_kwargs={"literal_binds": True})
                )
            except Exception:
                sql = None
        recorded.append((entity, sql))
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)

    # 退掉畫畫（vacated），只留圍棋
    res = client.post(
        "/api/activity/public/update",
        json={
            "id": q["id"],
            "name": "王小明",
            "birthday": "2020-05-10",
            "parent_phone": "0912345678",
            "class": q["class_name"],
            "courses": [{"name": "圍棋", "price": "1000"}],
            "supplies": [],
            "query_token": token,
        },
    )
    assert res.status_code == 200, res.text

    # 找 ActivityRegistration 第一次被鎖的位置
    reg_lock_idx = next(
        (i for i, (e, _) in enumerate(recorded) if e is ActivityRegistration),
        None,
    )
    assert reg_lock_idx is not None, f"未鎖 ActivityRegistration；recorded={recorded}"

    # 在 reg 鎖之前，須有一個 course 鎖查詢以 `activity_courses.id IN (...)` 涵蓋
    # vacated 課程（畫畫）id。精確抽取 IN 清單避免誤中 SQL 中其他整數（semester /
    # school_year / is_active 等）。修正前：reg 前唯一的 course 鎖是 name-based 查詢，
    # 不含 id IN 清單 → 找不到 painting_id → RED。
    import re

    course_locks_before_reg = [
        sql
        for (e, sql) in recorded[:reg_lock_idx]
        if e is ActivityCourse and sql is not None
    ]
    locked_ids_before_reg: set[int] = set()
    for sql in course_locks_before_reg:
        for m in re.finditer(r"activity_courses\.id IN \(([^)]+)\)", sql):
            for tok in m.group(1).split(","):
                tok = tok.strip()
                if tok.isdigit():
                    locked_ids_before_reg.add(int(tok))

    assert painting_id in locked_ids_before_reg, (
        "vacated 課程（畫畫 id=%s）未在 reg 列鎖前被 `id IN (...)` course 列鎖涵蓋；"
        "reg 鎖前 course 鎖涵蓋的 id=%s，SQL=%s"
        % (painting_id, locked_ids_before_reg, course_locks_before_reg)
    )

"""tests/test_activity_register_lock_order_2026_06_23.py

Finding 4 (code review, P2)：多課程報名的課程 FOR UPDATE 鎖缺 ORDER BY。

三條報名路徑都以
    session.query(ActivityCourse).filter(... id.in_ / name.in_ ...).with_for_update().all()
批次鎖課程，註解聲稱「以 id 排序整批鎖定」，但查詢其實沒有 ORDER BY：
  - 家長端 register_courses（api/parent_portal/activity.py）：id.in_(sorted(...))
  - 公開 public_register（api/activity/public.py）：name.in_(course_names)
  - 後台 admin_create_registration（api/activity/registrations.py）：name.in_(course_names)

`sorted()` 只排了 Python 端 IN 清單的值，不決定 FOR UPDATE 列鎖的取得順序——
後者由查詢計畫決定。兩個並發交易以重疊但不同順序的課程組合報名時，鎖序不穩定，
仍有 ABBA 死鎖風險。

修正：三條鎖查詢都補 .order_by(ActivityCourse.id)（置於 with_for_update 之前），
固定批次內列鎖的取得順序。

測法：spy sqlalchemy Query.with_for_update，擷取鎖定 ActivityCourse 的查詢編譯後
SQL，斷言含 `ORDER BY activity_courses.id`。SQLite 下 FOR UPDATE 為 no-op，但
order_by 仍會編譯進語句，故可驗證鎖序已固定。
"""

import os
import sys

import pytest
import sqlalchemy.orm
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import ActivityCourse

# 公開 + 後台路徑：複用 regressions 的 app fixture（auth + activity router）與 helper。
from tests.test_activity_regressions import (  # noqa: F401
    activity_client,
    _create_admin,
    _create_classroom,
    _create_course as _admin_create_course,
    _login,
)

# 家長端 helper（純函式，無 fixture 名稱衝突）。
from tests.test_parent_activity import (
    _setup_family,
    _create_course as _parent_create_course,
    _parent_token,
)
from api.parent_portal import parent_router as parent_portal_router

# 例：activity_courses.id —— 動態取表名/欄名，避免硬編。
_ORDER_COL = f"{ActivityCourse.__table__.name}.{ActivityCourse.id.key}"


@pytest.fixture
def parent_client(tmp_path):
    """家長端 app（複製自 test_parent_activity，含 SQLite parent RLS UDF shim）。

    自建獨立 fixture 而非 import test_parent_activity.activity_client，避免與
    regressions 模組同名 fixture 衝突。
    """
    db_path = tmp_path / "lock-order-parent.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    from models.database import Base

    Base.metadata.create_all(engine)

    app = FastAPI()
    from utils.exception_handlers import register_exception_handlers

    register_exception_handlers(app)
    app.include_router(parent_portal_router)

    from api.parent_portal._dependencies import get_parent_db
    from tests._parent_rls_test_utils import (
        make_sqlite_parent_db_override,
        register_sqlite_parent_rls_udfs,
    )

    register_sqlite_parent_rls_udfs(engine)
    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )

    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _spy_course_lock_sql(monkeypatch):
    """monkeypatch Query.with_for_update，回傳擷取到的『鎖定 ActivityCourse』SQL 列表。"""
    captured: list[str] = []
    orig = sqlalchemy.orm.Query.with_for_update

    def _spy(self, *args, **kwargs):
        cds = self.column_descriptions
        if cds and cds[0]["entity"] is ActivityCourse:
            captured.append(str(self.statement))
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(sqlalchemy.orm.Query, "with_for_update", _spy)
    return captured


def _assert_ordered_by_course_id(captured):
    assert captured, "未捕捉到任何鎖定 ActivityCourse 的 FOR UPDATE 查詢"
    for sql in captured:
        assert "ORDER BY" in sql, (
            "課程鎖查詢缺 ORDER BY → FOR UPDATE 列鎖取得順序不固定，並發報名仍有 "
            f"ABBA 死鎖風險。SQL={sql}"
        )
        order_tail = sql.split("ORDER BY", 1)[1]
        assert (
            _ORDER_COL in order_tail
        ), f"課程鎖查詢未以 {_ORDER_COL} 排序固定鎖序。ORDER BY 子句={order_tail!r}"


# ── 家長端 register_courses ──────────────────────────────────────────────────


def test_parent_register_locks_courses_ordered_by_id(parent_client, monkeypatch):
    """家長端多課程報名：課程 FOR UPDATE 鎖須以 id 排序固定鎖序。"""
    client, sf = parent_client
    with sf() as s:
        user, _, student, _ = _setup_family(s)
        c1 = _parent_create_course(s, name="圍棋", capacity=30)
        c2 = _parent_create_course(s, name="畫畫", capacity=30)
        s.commit()
        token = _parent_token(user)
        sid = student.id
        cids = [c1.id, c2.id]

    captured = _spy_course_lock_sql(monkeypatch)
    res = client.post(
        "/api/parent/activity/register",
        json={
            "student_id": sid,
            "school_year": 115,
            "semester": 1,
            "course_ids": cids,
            "supply_ids": [],
        },
        cookies={"access_token": token},
    )
    assert res.status_code == 201, res.text
    _assert_ordered_by_course_id(captured)


# ── 公開 public_register ─────────────────────────────────────────────────────


def test_public_register_locks_courses_ordered_by_id(activity_client, monkeypatch):
    """公開多課程報名：課程 FOR UPDATE 鎖須以 id 排序固定鎖序。"""
    client, sf = activity_client
    with sf() as s:
        _create_classroom(s, "海豚班")
        _admin_create_course(s, "圍棋", 1200)
        _admin_create_course(s, "畫畫", 1500)
        s.commit()

    captured = _spy_course_lock_sql(monkeypatch)
    res = client.post(
        "/api/activity/public/register",
        json={
            "name": "王小明",
            "birthday": "2020-01-01",
            "parent_phone": "0912345678",
            "class": "海豚班",
            "courses": [{"name": "圍棋", "price": "1"}, {"name": "畫畫", "price": "1"}],
            "supplies": [],
        },
    )
    assert res.status_code == 201, res.text
    _assert_ordered_by_course_id(captured)


# ── 後台 admin_create_registration ───────────────────────────────────────────


def test_admin_register_locks_courses_ordered_by_id(activity_client, monkeypatch):
    """後台多課程報名：課程 FOR UPDATE 鎖須以 id 排序固定鎖序。"""
    client, sf = activity_client
    with sf() as s:
        _create_admin(s)
        _create_classroom(s, "海豚班")
        _admin_create_course(s, "圍棋", 1200)
        _admin_create_course(s, "畫畫", 1500)
        s.commit()
    assert _login(client).status_code == 200

    captured = _spy_course_lock_sql(monkeypatch)
    res = client.post(
        "/api/activity/registrations",
        json={
            "name": "王小明",
            "birthday": "2020-01-01",
            "class": "海豚班",
            "courses": [{"name": "圍棋", "price": ""}, {"name": "畫畫", "price": ""}],
            "supplies": [],
        },
    )
    assert res.status_code == 201, res.text
    _assert_ordered_by_course_id(captured)

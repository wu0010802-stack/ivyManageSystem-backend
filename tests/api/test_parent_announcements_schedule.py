"""Parent portal announcements time-predicate enforcement (PR #1, T10).

驗證 parent portal announcements 已套 publish_at/expires_at time predicate：
- 未來 publish_at 的公告不應出現在 list
- 已過 expires_at 的公告不應出現在 list
- 未來 publish_at 的公告 mark_read 應回 403
"""

import os
import sys
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

import models.base as base_module
from api.parent_portal.announcements import router as parent_ann_router
from api.parent_portal._dependencies import get_parent_db
from models.database import (
    Announcement,
    AnnouncementParentRecipient,
    Base,
    Classroom,
    Guardian,
    Student,
    User,
)
from tests._parent_rls_test_utils import make_sqlite_parent_db_override
from utils.auth import create_access_token
from utils.taipei_time import now_taipei_naive


@pytest.fixture
def parent_ann_client(tmp_path):
    """隔離 sqlite 測試 app（僅含 parent announcements router）。"""
    db_path = tmp_path / "parent-ann-schedule.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)

    from utils.exception_handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(parent_ann_router, prefix="/api/parent")

    # 繞過 RLS parent engine（SQLite 沒有 SET LOCAL / RLS）
    app.dependency_overrides[get_parent_db] = make_sqlite_parent_db_override(
        session_factory
    )

    with TestClient(app) as client:
        yield client, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _seed_parent(session, *, line_uid: str = "UPA001"):
    """建立一個家長 User + Student + Guardian，回傳 (user, student)。"""
    user = User(
        username=f"parent_line_{line_uid}",
        password_hash="!LINE_ONLY",
        role="parent",
        permission_names=[],
        is_active=True,
        token_version=0,
    )
    session.add(user)
    session.flush()

    classroom = Classroom(name="向日葵", is_active=True)
    session.add(classroom)
    session.flush()

    student = Student(
        student_id=f"STU_{line_uid}",
        name="小測試",
        classroom_id=classroom.id,
        is_active=True,
    )
    session.add(student)
    session.flush()

    guardian = Guardian(
        student_id=student.id,
        user_id=user.id,
        name="測試家長",
        relation="父親",
    )
    session.add(guardian)
    session.flush()
    return user, student


def _parent_token(user: User) -> str:
    return create_access_token(
        {
            "user_id": user.id,
            "employee_id": None,
            "role": "parent",
            "name": user.username,
            "permission_names": [],
            "token_version": user.token_version or 0,
        }
    )


def _make_ann_scope_all(
    session,
    *,
    title: str = "測試公告",
    publish_at=None,
    expires_at=None,
):
    """建立 scope='all' 公告，回傳 Announcement ORM 物件。"""
    a = Announcement(
        title=title,
        content="內容",
        created_by=1,  # 行政人員 id（不影響家長可見性）
        publish_at=publish_at,
        expires_at=expires_at,
    )
    session.add(a)
    session.flush()
    session.add(AnnouncementParentRecipient(announcement_id=a.id, scope="all"))
    session.commit()
    return a


# ── Test 1: publish_at 在未來 → list 不見 ────────────────────────────────────


def test_parent_hides_scheduled_announcement(parent_ann_client):
    """scope='all' 公告 publish_at 在未來 → list 不應包含此公告。"""
    client, session_factory = parent_ann_client
    with session_factory() as session:
        user, _ = _seed_parent(session, line_uid="UPA001")
        future = now_taipei_naive() + timedelta(hours=2)
        a = _make_ann_scope_all(session, title="未來公告", publish_at=future)
        token = _parent_token(user)
        ann_id = a.id

    res = client.get("/api/parent/announcements", cookies={"access_token": token})
    assert res.status_code == 200, res.text
    ids = [i["id"] for i in res.json()["items"]]
    assert (
        ann_id not in ids
    ), f"排程公告（publish_at 未來）不應出現在家長端列表，但 id={ann_id} 出現了"


# ── Test 2: expires_at 已過 → list 不見 ──────────────────────────────────────


def test_parent_hides_expired_announcement(parent_ann_client):
    """scope='all' 公告 expires_at 已過 → list 不應包含此公告。"""
    client, session_factory = parent_ann_client
    with session_factory() as session:
        user, _ = _seed_parent(session, line_uid="UPA002")
        past = now_taipei_naive() - timedelta(hours=1)
        a = _make_ann_scope_all(session, title="過期公告", expires_at=past)
        token = _parent_token(user)
        ann_id = a.id

    res = client.get("/api/parent/announcements", cookies={"access_token": token})
    assert res.status_code == 200, res.text
    ids = [i["id"] for i in res.json()["items"]]
    assert ann_id not in ids, f"已過期公告不應出現在家長端列表，但 id={ann_id} 出現了"


# ── Test 3: publish_at 在未來 → mark_read 403 ────────────────────────────────


def test_parent_mark_read_rejects_unpublished(parent_ann_client):
    """publish_at 在未來的公告嘗試 mark_read 應回 403。"""
    client, session_factory = parent_ann_client
    with session_factory() as session:
        user, _ = _seed_parent(session, line_uid="UPA003")
        future = now_taipei_naive() + timedelta(hours=2)
        a = _make_ann_scope_all(session, title="未來公告T", publish_at=future)
        token = _parent_token(user)
        ann_id = a.id

    res = client.post(
        f"/api/parent/announcements/{ann_id}/read",
        cookies={"access_token": token},
    )
    assert (
        res.status_code == 403
    ), f"publish_at 未來的公告 mark_read 應回 403，但得到 {res.status_code}: {res.text}"

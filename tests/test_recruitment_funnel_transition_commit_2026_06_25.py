"""funnel transition 端點持久化回歸（2026-06-25 P1 資料遺失）。

Bug：`api/recruitment/funnel.py` 的 `post_transition` 用
`Depends(get_session_dep)` 注入 session，呼叫 `transition_visit` →
`convert_recruitment_to_student`（內部只 `flush()`，docstring 明示
「呼叫端負責 commit」），但端點整條鏈 **沒有** 任何 `session.commit()`。
`get_session_dep` 的 `finally` 只 `session.close()` → 未 commit 的交易
被 rollback。端點回 200 + student_id，但 DB 零持久化（成功轉換被丟掉）。

對照 `api/appraisal/__init__.py` 同樣用 `get_session_dep` 卻在成功路徑
顯式 `session.commit()`。

驗證方式：以 TestClient 打 POST /transition 把一筆 deposited visit 轉
enrolled，再用「另一個全新 session」（不同於端點的 request session，
但共用同一 SQLite engine）查 students 表。
- RED（修前）：查不到（交易被 rollback）。
- GREEN（修後）：查得到 recruitment_visit_id=該 visit 的 Student。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.base as base_module
from models.base import Base
from models.classroom import Classroom, Student
from models.recruitment import RecruitmentVisit
import models.student_log  # noqa: F401 — 註冊 student_change_logs 進 metadata
import models.fees  # noqa: F401 — 註冊 student_fee_records 進 metadata
import models.portfolio  # noqa: F401 — 註冊 portfolio 表進 metadata
import models.guardian  # noqa: F401 — 註冊 guardians 進 metadata

from api.recruitment.funnel import router as funnel_router
from utils.auth import get_current_user

# 不受限管理身分：role=admin（非 teacher/parent，過結構封鎖）+ wildcard
# 權限（resolve_grant 對任何 code scope=all → is_unrestricted True）。
_ADMIN_USER = {
    "role": "admin",
    "permission_names": ["*"],
    "user_id": 7,
    "username": "e2e_admin",
}


@pytest.fixture
def app_client(tmp_path):
    """以檔案型 SQLite 取代全域 engine/session factory，讓 get_session_dep
    與測試驗證 session 共用同一 DB（跨 session 才能看見持久化差異）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'funnel.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(funnel_router)
    app.dependency_overrides[get_current_user] = lambda: _ADMIN_USER

    with TestClient(app) as client:
        yield client, session_factory

    app.dependency_overrides.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_deposited_visit_and_classroom(session_factory):
    """建一筆 deposited 的 RecruitmentVisit + 一個班級，回傳 (visit_id, classroom_id)。"""
    s = session_factory()
    classroom = Classroom(name="小班-甲", school_year=114, semester=1, class_code="A")
    s.add(classroom)
    visit = RecruitmentVisit(
        month="115.03",
        child_name="測試幼生",
        phone="0912345678",
        has_deposit=True,
        enrolled=False,
    )
    s.add(visit)
    s.commit()
    visit_id, classroom_id = visit.id, classroom.id
    s.close()
    return visit_id, classroom_id


def test_transition_to_enrolled_persists_student(app_client):
    """deposited → enrolled 轉換成功後，Student 必須真的寫進 DB（commit）。

    修前：端點 0 commit → get_session_dep close 時 rollback → 全新 session
    查不到 Student（資料遺失）。修後：查得到。
    """
    client, session_factory = app_client
    visit_id, classroom_id = _seed_deposited_visit_and_classroom(session_factory)

    resp = client.post(
        f"/api/recruitment/funnel/visits/{visit_id}/transition",
        json={"to_stage": "enrolled", "classroom_id": classroom_id},
    )
    assert resp.status_code == 200, f"轉換端點非 200：{resp.status_code} {resp.text}"
    body = resp.json()
    assert body["to_stage"] == "enrolled"
    assert body["student_id"] is not None

    # 另開全新 session（非端點 request session）查持久化結果。
    verify = session_factory()
    try:
        persisted = (
            verify.query(Student)
            .filter(Student.recruitment_visit_id == visit_id)
            .first()
        )
    finally:
        verify.close()

    assert persisted is not None, (
        "成功轉換的 Student 未持久化到 DB —— post_transition 缺 session.commit()，"
        "get_session_dep close 時交易被 rollback（P1 資料遺失）。"
    )
    assert persisted.id == body["student_id"]

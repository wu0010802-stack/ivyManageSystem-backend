"""student_leaves list 班級 scope 須認顯式 STUDENTS_READ:own_class（覆蓋角色預設）。

qa-loop P2#3（2026-06-26 全掃）：list_leaves 以 no-code `is_unrestricted(current_user)` +
`accessible_classroom_ids(session, current_user)` 做班級 scope，未傳 `code=`。被顯式授
STUDENTS_READ:own_class 的管理角色（admin/hr/supervisor）在 no-code 路徑回 True → 略過
own_class 限制，可列任一班學生請假單。與 portfolio_access.is_row_unrestricted 的顯式 scope
覆蓋契約不一致（test_search 驗證 supervisor :own_class 須限自班）。
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.student_leaves import router as student_leaves_router
from models.classroom import Classroom
from models.database import Base, Employee, Student, User
from models.student_leave import StudentLeaveRequest
from utils.auth import create_access_token, hash_password


@pytest.fixture
def client_with_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sl-scope.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)
    old_engine, old_sf = base_module._engine, base_module._SessionFactory
    base_module._engine, base_module._SessionFactory = engine, sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(student_leaves_router)
    try:
        with TestClient(app) as client:
            yield client, sf
    finally:
        base_module._engine, base_module._SessionFactory = old_engine, old_sf
        engine.dispose()


def _setup(sf):
    s = sf()
    emp = Employee(employee_id="E-scoped", name="範圍主任", is_active=True)
    parent = User(
        username="parent1",
        password_hash=hash_password("pw123456"),
        role="parent",
        is_active=True,
    )
    s.add_all([emp, parent])
    s.flush()
    own = Classroom(
        name="自班", school_year=114, semester=1, is_active=True, head_teacher_id=emp.id
    )
    other = Classroom(name="他班", school_year=114, semester=1, is_active=True)
    s.add_all([own, other])
    s.flush()
    own_stu = Student(
        name="自班生",
        student_id="S-own",
        classroom_id=own.id,
        is_active=True,
        lifecycle_status="active",
    )
    other_stu = Student(
        name="他班生",
        student_id="S-other",
        classroom_id=other.id,
        is_active=True,
        lifecycle_status="active",
    )
    s.add_all([own_stu, other_stu])
    s.flush()
    # 兩班各一筆 approved 請假
    for stu in (own_stu, other_stu):
        s.add(
            StudentLeaveRequest(
                student_id=stu.id,
                applicant_user_id=parent.id,
                leave_type="病假",
                start_date=date(2026, 3, 12),
                end_date=date(2026, 3, 12),
                status="approved",
            )
        )
    sup = User(
        username="scoped_sup",
        password_hash=hash_password("pw123456"),
        role="supervisor",
        employee_id=emp.id,
        permission_names=["STUDENTS_READ:own_class"],
        is_active=True,
    )
    s.add(sup)
    s.flush()
    uid, eid, own_id, other_id = sup.id, emp.id, own.id, other.id
    s.commit()
    s.close()
    token = create_access_token(
        {
            "user_id": uid,
            "employee_id": eid,
            "role": "supervisor",
            "name": "範圍主任",
            "permission_names": ["STUDENTS_READ:own_class"],
            "token_version": 0,
        }
    )
    return {"Authorization": f"Bearer {token}"}, own_id, other_id


def test_leaves_other_class_blocked_for_own_class_scope(client_with_db):
    client, sf = client_with_db
    headers, own_id, other_id = _setup(sf)

    # 指定他班 → 顯式 :own_class 應收斂、即使管理角色也 403
    r_other = client.get(
        "/api/student-leaves",
        params={"status": "approved", "classroom_id": other_id},
        headers=headers,
    )
    assert (
        r_other.status_code == 403
    ), f"顯式 STUDENTS_READ:own_class 的管理角色查他班請假應 403，got {r_other.status_code}"


def test_leaves_unfiltered_only_returns_own_class(client_with_db):
    client, sf = client_with_db
    headers, own_id, other_id = _setup(sf)

    # 不帶 classroom_id → 應僅回自班請假，不含他班
    r = client.get(
        "/api/student-leaves", params={"status": "approved"}, headers=headers
    )
    assert r.status_code == 200
    names = {item["student_name"] for item in r.json()["items"]}
    assert names == {
        "自班生"
    }, f"顯式 :own_class 應僅見自班請假，實際 {names}（含他班=越權）"

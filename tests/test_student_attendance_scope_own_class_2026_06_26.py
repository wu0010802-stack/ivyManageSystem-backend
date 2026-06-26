"""student_attendance 班級 scope 須認顯式 STUDENTS_READ/WRITE:own_class（覆蓋角色預設）。

qa-loop P2#2（2026-06-26 全掃）：daily list / monthly / export / batch write 原以 no-code
`is_unrestricted(current_user)` + `accessible_classroom_ids(session, current_user)` 做班級
scope，未傳 `code=`。依 portfolio_access.is_row_unrestricted 契約，顯式 `<code>:own_class`
應「覆蓋角色預設、即使 admin/hr/supervisor 也限自班」（見 test_search）。但 no-code 路徑只看
role，對管理角色一律回 True → 被刻意以 STUDENTS_READ:own_class 收斂的管理角色可讀／匯出／批改
任一他班出席（search.py / student_enrollment.py 同端點已改用 is_row_unrestricted(code=...)）。

修法：把 code=Permission.STUDENTS_READ/WRITE 一路傳入 helper，與 is_row_unrestricted 契約對齊。
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
from api.student_attendance import router as student_attendance_router
from models.classroom import Classroom
from models.database import Base, Employee, Student, User
from utils.auth import create_access_token, hash_password


@pytest.fixture
def client_with_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sa-scope.sqlite'}",
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
    app.include_router(student_attendance_router)
    try:
        with TestClient(app) as client:
            yield client, sf
    finally:
        base_module._engine, base_module._SessionFactory = old_engine, old_sf
        engine.dispose()


def _setup_scoped_supervisor(sf, perms):
    """建立一個 supervisor（管理角色）顯式收斂 :own_class，擔任 own 班 head teacher。

    回傳 (headers, own_classroom_id, other_classroom_id)。
    """
    s = sf()
    emp = Employee(employee_id="E-scoped", name="範圍主任", is_active=True)
    s.add(emp)
    s.flush()
    own = Classroom(
        name="自班", school_year=114, semester=1, is_active=True, head_teacher_id=emp.id
    )
    other = Classroom(name="他班", school_year=114, semester=1, is_active=True)
    s.add_all([own, other])
    s.flush()
    s.add_all(
        [
            Student(
                name="自班生",
                student_id="S-own",
                classroom_id=own.id,
                is_active=True,
                lifecycle_status="active",
            ),
            Student(
                name="他班生",
                student_id="S-other",
                classroom_id=other.id,
                is_active=True,
                lifecycle_status="active",
            ),
        ]
    )
    u = User(
        username="scoped_sup",
        password_hash=hash_password("pw123456"),
        role="supervisor",
        employee_id=emp.id,
        permission_names=perms,
        is_active=True,
    )
    s.add(u)
    s.flush()
    uid, eid, own_id, other_id = u.id, emp.id, own.id, other.id
    s.commit()
    s.close()
    token = create_access_token(
        {
            "user_id": uid,
            "employee_id": eid,
            "role": "supervisor",
            "name": "範圍主任",
            "permission_names": perms,
            "token_version": 0,
        }
    )
    return {"Authorization": f"Bearer {token}"}, own_id, other_id


def test_daily_attendance_own_class_scope_blocks_other_class(client_with_db):
    client, sf = client_with_db
    headers, own_id, other_id = _setup_scoped_supervisor(
        sf, ["STUDENTS_READ:own_class"]
    )

    # 自班 → 放行
    r_own = client.get(
        "/api/student-attendance",
        params={"date": "2026-03-12", "classroom_id": own_id},
        headers=headers,
    )
    assert r_own.status_code == 200, f"自班應放行，got {r_own.status_code}"

    # 他班 → 顯式 :own_class 應收斂、即使管理角色也 403
    r_other = client.get(
        "/api/student-attendance",
        params={"date": "2026-03-12", "classroom_id": other_id},
        headers=headers,
    )
    assert r_other.status_code == 403, (
        f"顯式 STUDENTS_READ:own_class 的管理角色查他班出席應 403，got {r_other.status_code}"
        "（no-code is_unrestricted 漏認顯式 scope → 越權讀他班）"
    )


def test_batch_write_own_class_scope_blocks_other_class(client_with_db):
    client, sf = client_with_db
    headers, own_id, other_id = _setup_scoped_supervisor(
        sf, ["STUDENTS_READ:own_class", "STUDENTS_WRITE:own_class"]
    )
    # 取他班學生 id
    s = sf()
    other_student = s.query(Student).filter(Student.classroom_id == other_id).first()
    other_sid = other_student.id
    s.close()

    r = client.post(
        "/api/student-attendance/batch",
        json={
            "date": "2026-03-12",
            "entries": [{"student_id": other_sid, "status": "出席"}],
        },
        headers=headers,
    )
    assert (
        r.status_code == 403
    ), f"顯式 STUDENTS_WRITE:own_class 的管理角色批改他班出席應 403，got {r.status_code}"

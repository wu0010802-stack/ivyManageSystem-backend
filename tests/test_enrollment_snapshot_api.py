"""在籍人數快照 API（L2，spec 2026-06-13-enrollment-count-correctness）。"""

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
from models.database import (
    Base,
    ClassGrade,
    Classroom,
    Employee,
    SalaryRecord,
    Student,
    User,
)
from utils.auth import create_access_token, hash_password


@pytest.fixture
def client_ctx(tmp_path):
    from api.salary.enrollment_snapshot import router as snapshot_router

    engine = create_engine(
        f"sqlite:///{tmp_path / 'enr-snap-api.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)
    old_engine, old_factory = base_module._engine, base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)

    session = session_factory()
    try:
        emp = Employee(employee_id="T99", name="管理員", is_active=True)
        session.add(emp)
        session.flush()
        admin = User(
            username="admin",
            password_hash=hash_password("Admin1234"),
            role="admin",
            is_active=True,
            permission_names=["*"],
            employee_id=emp.id,
        )
        session.add(admin)
        session.flush()
        admin_id = admin.id
        emp_id = emp.id

        grade = ClassGrade(name="大班", is_active=True)
        session.add(grade)
        session.flush()
        room = Classroom(name="天堂鳥", grade_id=grade.id, is_active=True)
        session.add(room)
        session.flush()
        room_id = room.id
        for idx in range(12):
            session.add(
                Student(
                    student_id=f"SA{idx:03d}",
                    name=f"學生{idx}",
                    classroom_id=room.id,
                    enrollment_date=date(2025, 9, 1),
                    is_active=True,
                )
            )
        session.commit()
    finally:
        session.close()

    token = create_access_token(
        {
            "user_id": admin_id,
            "employee_id": emp_id,
            "role": "admin",
            "name": "管理員",
            "permission_names": ["*"],
            "token_version": 0,
        }
    )
    app = FastAPI()
    app.include_router(snapshot_router, prefix="/api")
    client = TestClient(app)
    client.cookies.set("access_token", token)
    yield client, session_factory, room_id, emp_id

    base_module._engine, base_module._SessionFactory = old_engine, old_factory
    engine.dispose()


def test_get_empty_month_exists_false(client_ctx):
    client, _, _, _ = client_ctx
    res = client.get("/api/salaries/enrollment-snapshot?year=2026&month=3")
    assert res.status_code == 200
    body = res.json()
    assert body["exists"] is False
    assert body["rows"] == []


def test_generate_then_get_rows(client_ctx):
    client, _, room_id, _ = client_ctx
    res = client.post(
        "/api/salaries/enrollment-snapshot/generate",
        json={"year": 2026, "month": 3},
    )
    assert res.status_code == 200
    assert res.json()["generated"] >= 2

    res2 = client.get("/api/salaries/enrollment-snapshot?year=2026&month=3")
    body = res2.json()
    assert body["exists"] is True
    by_class = {row["classroom_id"]: row for row in body["rows"]}
    assert by_class[room_id]["student_count"] == 12
    assert by_class[None]["student_count"] == 12  # 全校列
    assert by_class[room_id]["classroom_name"] == "天堂鳥"


def test_patch_requires_reason_and_marks_salary_stale(client_ctx):
    client, session_factory, room_id, emp_id = client_ctx
    client.post(
        "/api/salaries/enrollment-snapshot/generate",
        json={"year": 2026, "month": 3},
    )
    with session_factory() as session:
        rec = SalaryRecord(
            employee_id=emp_id,
            salary_year=2026,
            salary_month=6,
            is_finalized=False,
            needs_recalc=False,
            version=1,
        )
        session.add(rec)
        session.commit()
        rec_id = rec.id

    rows = client.get("/api/salaries/enrollment-snapshot?year=2026&month=3").json()[
        "rows"
    ]
    target = next(r for r in rows if r["classroom_id"] == room_id)

    # 無 reason → 422/400
    res_bad = client.patch(
        f"/api/salaries/enrollment-snapshot/{target['id']}",
        json={"student_count": 10},
    )
    assert res_bad.status_code in (400, 422)

    res = client.patch(
        f"/api/salaries/enrollment-snapshot/{target['id']}",
        json={"student_count": 10, "reason": "三月中有兩位學生退學一位轉出"},
    )
    assert res.status_code == 200

    with session_factory() as session:
        from models.enrollment_snapshot import ClassEnrollmentSnapshot

        row = session.get(ClassEnrollmentSnapshot, target["id"])
        assert float(row.student_count) == 10
        assert row.count_mode == "manual"
        assert row.is_confirmed is True
        rec = session.get(SalaryRecord, rec_id)
        assert rec.needs_recalc is True  # 3 月人數 → 6 月發放月標 stale


def test_confirm_month(client_ctx):
    client, session_factory, _, _ = client_ctx
    client.post(
        "/api/salaries/enrollment-snapshot/generate",
        json={"year": 2026, "month": 3},
    )
    res = client.post(
        "/api/salaries/enrollment-snapshot/confirm",
        json={"year": 2026, "month": 3},
    )
    assert res.status_code == 200
    rows = client.get("/api/salaries/enrollment-snapshot?year=2026&month=3").json()[
        "rows"
    ]
    assert all(r["is_confirmed"] for r in rows)


def test_patch_blocked_when_distribution_month_finalized(client_ctx):
    client, session_factory, room_id, emp_id = client_ctx
    client.post(
        "/api/salaries/enrollment-snapshot/generate",
        json={"year": 2026, "month": 3},
    )
    with session_factory() as session:
        session.add(
            SalaryRecord(
                employee_id=emp_id,
                salary_year=2026,
                salary_month=6,
                is_finalized=True,
                version=1,
            )
        )
        session.commit()

    rows = client.get("/api/salaries/enrollment-snapshot?year=2026&month=3").json()[
        "rows"
    ]
    target = next(r for r in rows if r["classroom_id"] == room_id)
    res = client.patch(
        f"/api/salaries/enrollment-snapshot/{target['id']}",
        json={"student_count": 10, "reason": "三月中有兩位學生退學一位轉出"},
    )
    assert res.status_code == 409

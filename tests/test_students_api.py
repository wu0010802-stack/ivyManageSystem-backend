"""學生管理 API 回歸測試。"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.students import router as students_router
from models.database import Base, Classroom, Student, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def students_client(tmp_path):
    db_path = tmp_path / "students-api.sqlite"
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
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(students_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_user(session, username: str, password: str = "TempPass123") -> User:
    user = User(
        username=username,
        password_hash=hash_password(password),
        role="admin",
        permission_names=["STUDENTS_READ", "STUDENTS_WRITE"],
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str = "TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


class TestStudentsApi:
    def test_get_students_filters_by_academic_term_and_classroom(self, students_client):
        client, session_factory = students_client
        with session_factory() as session:
            _create_user(session, "student_filter_admin")
            current_classroom = Classroom(
                name="海豚班", school_year=114, semester=2, is_active=True
            )
            same_term_other_classroom = Classroom(
                name="星星班", school_year=114, semester=2, is_active=True
            )
            old_classroom = Classroom(
                name="月亮班", school_year=113, semester=2, is_active=True
            )
            session.add_all(
                [current_classroom, same_term_other_classroom, old_classroom]
            )
            session.flush()
            session.add_all(
                [
                    Student(
                        student_id="S001",
                        name="小明",
                        classroom_id=current_classroom.id,
                        is_active=True,
                    ),
                    Student(
                        student_id="S002",
                        name="小美",
                        classroom_id=same_term_other_classroom.id,
                        is_active=True,
                    ),
                    Student(
                        student_id="S003",
                        name="小宇",
                        classroom_id=old_classroom.id,
                        is_active=True,
                    ),
                ]
            )
            session.commit()
            classroom_id = current_classroom.id

        login_res = _login(client, "student_filter_admin")
        assert login_res.status_code == 200

        same_term_res = client.get(
            "/api/students",
            params={"school_year": 114, "semester": 2},
        )
        assert same_term_res.status_code == 200
        assert [item["student_id"] for item in same_term_res.json()["items"]] == [
            "S001",
            "S002",
        ]

        classroom_res = client.get(
            "/api/students",
            params={"school_year": 114, "semester": 2, "classroom_id": classroom_id},
        )
        assert classroom_res.status_code == 200
        assert [item["student_id"] for item in classroom_res.json()["items"]] == [
            "S001"
        ]

    def test_bulk_transfer_moves_active_students_to_target_classroom(
        self, students_client
    ):
        client, session_factory = students_client
        with session_factory() as session:
            _create_user(session, "student_transfer_admin")
            source = Classroom(
                name="海豚班", school_year=114, semester=2, is_active=True
            )
            target = Classroom(
                name="星星班", school_year=114, semester=2, is_active=True
            )
            session.add_all([source, target])
            session.flush()
            session.add_all(
                [
                    Student(
                        student_id="S001",
                        name="小明",
                        classroom_id=source.id,
                        is_active=True,
                    ),
                    Student(
                        student_id="S002",
                        name="小美",
                        classroom_id=source.id,
                        is_active=True,
                    ),
                ]
            )
            session.commit()
            target_id = target.id

        login_res = _login(client, "student_transfer_admin")
        assert login_res.status_code == 200

        res = client.post(
            "/api/students/bulk-transfer",
            json={
                "student_ids": [1, 2],
                "target_classroom_id": target_id,
            },
        )

        assert res.status_code == 200
        assert res.json()["moved_count"] == 2

        with session_factory() as session:
            classrooms = {
                student.student_id: student.classroom_id
                for student in session.query(Student).order_by(Student.student_id).all()
            }
            assert len(set(classrooms.values())) == 1
            assert list(classrooms.values())[0] == target_id

    def test_bulk_transfer_rejects_inactive_target_classroom(self, students_client):
        client, session_factory = students_client
        with session_factory() as session:
            _create_user(session, "student_transfer_invalid")
            source = Classroom(
                name="海豚班", school_year=114, semester=2, is_active=True
            )
            target = Classroom(
                name="停用班", school_year=114, semester=2, is_active=False
            )
            session.add_all([source, target])
            session.flush()
            session.add(
                Student(
                    student_id="S001",
                    name="小明",
                    classroom_id=source.id,
                    is_active=True,
                )
            )
            session.commit()
            target_id = target.id

        login_res = _login(client, "student_transfer_invalid")
        assert login_res.status_code == 200

        res = client.post(
            "/api/students/bulk-transfer",
            json={
                "student_ids": [1],
                "target_classroom_id": target_id,
            },
        )

        assert res.status_code == 400
        assert "班級不存在或已停用" in res.json()["detail"]

    def test_delete_sets_status_to_deleted(self, students_client):
        """刪除學生（軟刪除）後 status 應設為「已刪除」"""
        client, session_factory = students_client
        with session_factory() as session:
            _create_user(session, "delete_test_admin")
            classroom = Classroom(
                name="刪除測試班", school_year=114, semester=2, is_active=True
            )
            session.add(classroom)
            session.flush()
            session.add(
                Student(
                    student_id="DEL001",
                    name="被刪除的學生",
                    classroom_id=classroom.id,
                    is_active=True,
                )
            )
            session.commit()

        login_res = _login(client, "delete_test_admin")
        assert login_res.status_code == 200

        # 取得學生 id
        list_res = client.get(
            "/api/students", params={"school_year": 114, "semester": 2}
        )
        assert list_res.status_code == 200
        student_id = list_res.json()["items"][0]["id"]

        # 刪除
        del_res = client.delete(f"/api/students/{student_id}")
        assert del_res.status_code == 200

        # 確認 is_active=False 且 status="已刪除"
        with session_factory() as session:
            student = session.query(Student).filter(Student.id == student_id).first()
            assert student.is_active is False
            assert student.status == "已刪除"

    def test_create_student_auto_assigns_enrollment_seq(self, students_client):
        """POST /students 不需傳 student_id，自動配發 enrollment_seq（A5 自動配號）"""
        client, session_factory = students_client
        with session_factory() as session:
            _create_user(session, "validation_admin_1")
            session.commit()

        login_res = _login(client, "validation_admin_1")
        assert login_res.status_code == 200

        res = client.post("/api/students", json={"name": "測試生"})
        assert res.status_code == 201

        new_id = res.json()["id"]
        with session_factory() as session:
            student = session.query(Student).filter(Student.id == new_id).first()
            assert student is not None
            assert student.enrollment_seq is not None
            assert student.enrollment_seq >= 1
            assert student.enrollment_school_year is not None

    def test_create_student_rejects_invalid_phone_format(self, students_client):
        """電話格式不符時應回傳 422"""
        client, session_factory = students_client
        with session_factory() as session:
            _create_user(session, "validation_admin_2")
            session.commit()

        login_res = _login(client, "validation_admin_2")
        assert login_res.status_code == 200

        res = client.post(
            "/api/students",
            json={"name": "測試生", "parent_phone": "abc-invalid"},
        )
        assert res.status_code == 422

    def test_create_student_accepts_iso_date_string(self, students_client):
        """Pydantic date 欄位應接受 ISO string（向下相容）"""
        client, session_factory = students_client
        with session_factory() as session:
            _create_user(session, "validation_admin_3")
            session.commit()

        login_res = _login(client, "validation_admin_3")
        assert login_res.status_code == 200

        res = client.post(
            "/api/students",
            json={"name": "日期測試生", "birthday": "2020-05-15"},
        )
        assert res.status_code == 201

    def test_students_search_multi_token(self, students_client):
        """搜尋字串含空格時各 token 做 AND，多 token 可縮小結果"""
        client, session_factory = students_client
        with session_factory() as session:
            _create_user(session, "search_multi_token_admin")
            classroom = Classroom(
                name="搜尋測試班", school_year=114, semester=2, is_active=True
            )
            session.add(classroom)
            session.flush()
            session.add_all(
                [
                    Student(
                        student_id="SM001",
                        name="林美麗",
                        classroom_id=classroom.id,
                        is_active=True,
                    ),
                    Student(
                        student_id="SM002",
                        name="林大同",
                        classroom_id=classroom.id,
                        is_active=True,
                    ),
                ]
            )
            session.commit()

        login_res = _login(client, "search_multi_token_admin")
        assert login_res.status_code == 200

        resp = client.get("/api/students", params={"search": "林 美"})
        assert resp.status_code == 200
        names = [s["name"] for s in resp.json()["items"]]
        assert "林美麗" in names
        assert "林大同" not in names

    def test_students_search_wildcard_escaped(self, students_client):
        """`%` 字元應被跳脫，不可當萬用字元匹配所有學生"""
        client, session_factory = students_client
        with session_factory() as session:
            _create_user(session, "search_escape_admin")
            classroom = Classroom(
                name="跳脫測試班", school_year=114, semester=2, is_active=True
            )
            session.add(classroom)
            session.flush()
            session.add(
                Student(
                    student_id="SE001",
                    name="林美麗",
                    classroom_id=classroom.id,
                    is_active=True,
                )
            )
            session.commit()

        login_res = _login(client, "search_escape_admin")
        assert login_res.status_code == 200

        resp = client.get("/api/students", params={"search": "%"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []

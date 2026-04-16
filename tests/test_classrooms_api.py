"""班級 CRUD API 回歸測試。"""

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
from api.auth import router as auth_router
from api.classrooms import router as classrooms_router
from api.auth import _account_failures, _ip_attempts
from models.database import Base, ClassGrade, Classroom, Employee, Student, User
from utils.auth import hash_password
from utils.permissions import Permission


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "classrooms-api.sqlite"
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
    app.include_router(classrooms_router)

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
        permissions=Permission.CLASSROOMS_READ | Permission.CLASSROOMS_WRITE,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _create_teacher(session, employee_id: str, name: str) -> Employee:
    teacher = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=32000,
        is_active=True,
    )
    session.add(teacher)
    session.flush()
    return teacher


def _login(client: TestClient, username: str, password: str = "TempPass123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestClassroomsApi:
    def test_create_defaults_to_current_academic_term(self, client_with_db, monkeypatch):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_term_admin")
            session.commit()

        import api.classrooms as classrooms_module

        monkeypatch.setattr(
            classrooms_module,
            "resolve_current_academic_term",
            lambda target_date=None: (114, 2),
        )

        login_res = _login(client, "classroom_term_admin")
        assert login_res.status_code == 200

        res = client.post(
            "/api/classrooms",
            json={
                "name": "向日葵班",
                "capacity": 20,
            },
        )

        assert res.status_code == 201

        detail_res = client.get(f"/api/classrooms/{res.json()['id']}")
        assert detail_res.status_code == 200
        assert detail_res.json()["school_year"] == 114
        assert detail_res.json()["semester"] == 2
        assert "下學期" in detail_res.json()["semester_label"]

    def test_same_classroom_name_is_allowed_in_different_semesters(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_term_duplicate")
            session.commit()

        login_res = _login(client, "classroom_term_duplicate")
        assert login_res.status_code == 200

        first_res = client.post(
            "/api/classrooms",
            json={
                "name": "彩虹班",
                "capacity": 20,
                "school_year": 114,
                "semester": 1,
            },
        )
        assert first_res.status_code == 201

        second_res = client.post(
            "/api/classrooms",
            json={
                "name": "彩虹班",
                "capacity": 20,
                "school_year": 114,
                "semester": 2,
            },
        )
        assert second_res.status_code == 201

        same_term_res = client.post(
            "/api/classrooms",
            json={
                "name": "彩虹班",
                "capacity": 20,
                "school_year": 114,
                "semester": 2,
            },
        )
        assert same_term_res.status_code == 400
        assert "班級名稱已存在" in same_term_res.json()["detail"]

    def test_get_classrooms_supports_term_filtering(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_term_filter")
            first_term = Classroom(name="海豚班", capacity=20, school_year=114, semester=1, is_active=True)
            second_term = Classroom(name="海豚班", capacity=20, school_year=114, semester=2, is_active=True)
            session.add_all([
                first_term,
                second_term,
                Classroom(name="星星班", capacity=20, school_year=113, semester=2, is_active=True),
            ])
            session.flush()
            session.add_all([
                Student(student_id="S001", name="小安", classroom_id=second_term.id, is_active=True),
                Student(student_id="S002", name="小寶", classroom_id=second_term.id, is_active=True),
                Student(student_id="S003", name="小晴", classroom_id=second_term.id, is_active=True),
                Student(student_id="S004", name="小涵", classroom_id=second_term.id, is_active=True),
                Student(student_id="S005", name="已畢業", classroom_id=second_term.id, is_active=False, status="已畢業"),
            ])
            session.commit()

        login_res = _login(client, "classroom_term_filter")
        assert login_res.status_code == 200

        res = client.get("/api/classrooms", params={"school_year": 114, "semester": 2})

        assert res.status_code == 200
        names = [item["name"] for item in res.json()]
        assert names == ["海豚班"]
        assert res.json()[0]["semester_label"] == "114學年度下學期"
        assert res.json()[0]["current_count"] == 4
        assert [student["name"] for student in res.json()[0]["student_preview"]] == ["小安", "小寶", "小晴"]
        assert res.json()[0]["has_more_students"] is True

    def test_clone_term_copies_classrooms_into_target_term(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_clone_admin")
            grade = ClassGrade(name="小班", is_active=True)
            teacher = _create_teacher(session, "T201", "張老師")
            session.add(grade)
            session.flush()
            session.add_all([
                Classroom(
                    name="海豚班",
                    class_code="DOL-01",
                    grade_id=grade.id,
                    capacity=22,
                    head_teacher_id=teacher.id,
                    school_year=114,
                    semester=1,
                    is_active=True,
                ),
                Classroom(
                    name="星星班",
                    class_code="STA-01",
                    grade_id=grade.id,
                    capacity=20,
                    head_teacher_id=teacher.id,
                    school_year=114,
                    semester=1,
                    is_active=True,
                ),
            ])
            session.commit()

        login_res = _login(client, "classroom_clone_admin")
        assert login_res.status_code == 200

        clone_res = client.post(
            "/api/classrooms/clone-term",
            json={
                "source_school_year": 114,
                "source_semester": 1,
                "target_school_year": 114,
                "target_semester": 2,
                "copy_teachers": True,
            },
        )

        assert clone_res.status_code == 201
        assert clone_res.json()["created_count"] == 2

        target_res = client.get("/api/classrooms", params={"school_year": 114, "semester": 2})
        assert target_res.status_code == 200
        assert {item["name"] for item in target_res.json()} == {"海豚班", "星星班"}
        assert all(item["semester"] == 2 for item in target_res.json())

    def test_clone_term_rejects_existing_target_term_conflict(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_clone_conflict")
            session.add_all([
                Classroom(name="海豚班", capacity=20, school_year=114, semester=1, is_active=True),
                Classroom(name="海豚班", capacity=20, school_year=114, semester=2, is_active=True),
            ])
            session.commit()

        login_res = _login(client, "classroom_clone_conflict")
        assert login_res.status_code == 200

        clone_res = client.post(
            "/api/classrooms/clone-term",
            json={
                "source_school_year": 114,
                "source_semester": 1,
                "target_school_year": 114,
                "target_semester": 2,
                "copy_teachers": False,
            },
        )

        assert clone_res.status_code == 409
        assert "已存在" in clone_res.json()["detail"]

    def test_promote_academic_year_uses_grade_sort_order_for_promotion(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_promote_admin")
            grade_big = ClassGrade(name="大班", sort_order=1, is_active=True)
            grade_middle = ClassGrade(name="中班", sort_order=2, is_active=True)
            grade_small = ClassGrade(name="小班", sort_order=3, is_active=True)
            session.add_all([grade_big, grade_middle, grade_small])
            teacher_a = _create_teacher(session, "T301", "王老師")
            teacher_b = _create_teacher(session, "T302", "李老師")
            session.flush()
            source = Classroom(
                name="向日葵班",
                class_code="SUN-01",
                grade_id=grade_small.id,
                capacity=25,
                head_teacher_id=teacher_a.id,
                assistant_teacher_id=teacher_b.id,
                school_year=114,
                semester=2,
                is_active=True,
            )
            session.add(source)
            session.flush()
            session.add_all([
                Student(student_id="S101", name="小明", classroom_id=source.id, is_active=True),
                Student(student_id="S102", name="小美", classroom_id=source.id, is_active=True),
            ])
            session.commit()
            source_id = source.id
            target_grade_id = grade_middle.id

        login_res = _login(client, "classroom_promote_admin")
        assert login_res.status_code == 200

        promote_res = client.post(
            "/api/classrooms/promote-academic-year",
            json={
                "source_school_year": 114,
                "source_semester": 2,
                "target_school_year": 115,
                "target_semester": 1,
                "classrooms": [
                    {
                        "source_classroom_id": source_id,
                        "target_name": "海洋探索班",
                        "target_grade_id": target_grade_id,
                    }
                ],
            },
        )

        assert promote_res.status_code == 201
        assert promote_res.json()["created_count"] == 1
        assert promote_res.json()["moved_student_count"] == 2

        target_res = client.get("/api/classrooms", params={"school_year": 115, "semester": 1})
        assert target_res.status_code == 200
        assert target_res.json()[0]["name"] == "海洋探索班"
        assert target_res.json()[0]["grade_id"] == target_grade_id
        assert target_res.json()[0]["grade_name"] == "中班"
        assert target_res.json()[0]["head_teacher_name"] == "王老師"
        assert target_res.json()[0]["assistant_teacher_name"] == "李老師"

        with session_factory() as session:
            moved_students = session.query(Student).order_by(Student.student_id).all()
            assert len({student.classroom_id for student in moved_students}) == 1
            assert moved_students[0].classroom_id != source_id

    def test_promote_academic_year_does_not_advance_grade_between_semesters_in_same_school_year(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_same_year_no_jump")
            grade_big = ClassGrade(name="大班", sort_order=1, is_active=True)
            grade_middle = ClassGrade(name="中班", sort_order=2, is_active=True)
            grade_small = ClassGrade(name="小班", sort_order=3, is_active=True)
            session.add_all([grade_big, grade_middle, grade_small])
            session.flush()
            source = Classroom(
                name="薔薇班",
                grade_id=grade_big.id,
                capacity=20,
                school_year=114,
                semester=1,
                is_active=True,
            )
            session.add(source)
            session.flush()
            session.add(Student(student_id="S111", name="小宇", classroom_id=source.id, is_active=True))
            session.commit()
            source_id = source.id

        login_res = _login(client, "classroom_same_year_no_jump")
        assert login_res.status_code == 200

        promote_res = client.post(
            "/api/classrooms/promote-academic-year",
            json={
                "source_school_year": 114,
                "source_semester": 1,
                "target_school_year": 114,
                "target_semester": 2,
                "classrooms": [
                    {
                        "source_classroom_id": source_id,
                        "target_name": "薔薇班",
                    }
                ],
            },
        )

        assert promote_res.status_code == 201
        assert promote_res.json()["created_count"] == 1
        assert promote_res.json()["graduated_count"] == 0

        target_res = client.get("/api/classrooms", params={"school_year": 114, "semester": 2})
        assert target_res.status_code == 200
        assert target_res.json()[0]["grade_name"] == "大班"

    def test_promote_academic_year_graduates_students_when_no_next_grade(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_promote_grade_required")
            grade = ClassGrade(name="大班", sort_order=99, is_active=True)
            session.add(grade)
            session.flush()
            classroom = Classroom(
                name="星星班",
                grade_id=grade.id,
                capacity=20,
                school_year=114,
                semester=2,
                is_active=True,
            )
            session.add(classroom)
            session.flush()
            session.add_all([
                Student(student_id="S201", name="小杰", classroom_id=classroom.id, is_active=True),
                Student(student_id="S202", name="小安", classroom_id=classroom.id, is_active=True),
            ])
            session.commit()
            classroom_id = classroom.id

        login_res = _login(client, "classroom_promote_grade_required")
        assert login_res.status_code == 200

        promote_res = client.post(
            "/api/classrooms/promote-academic-year",
            json={
                "source_school_year": 114,
                "source_semester": 2,
                "target_school_year": 115,
                "target_semester": 1,
                "classrooms": [
                    {
                        "source_classroom_id": classroom_id,
                    }
                ],
            },
        )

        assert promote_res.status_code == 201
        assert promote_res.json()["created_count"] == 0
        assert promote_res.json()["graduated_count"] == 2

        target_res = client.get("/api/classrooms", params={"school_year": 115, "semester": 1})
        assert target_res.status_code == 200
        assert target_res.json() == []

        with session_factory() as session:
            graduated_students = session.query(Student).order_by(Student.student_id).all()
            assert all(student.is_active is False for student in graduated_students)
            assert all(student.status == "已畢業" for student in graduated_students)
            assert all(student.graduation_date.isoformat() == "2026-08-01" for student in graduated_students)

    def test_promote_academic_year_reuses_inactive_target_classroom_with_same_name(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_promote_reuse_inactive")
            grade_big = ClassGrade(name="大班", sort_order=1, is_active=True)
            grade_middle = ClassGrade(name="中班", sort_order=2, is_active=True)
            grade_small = ClassGrade(name="小班", sort_order=3, is_active=True)
            session.add_all([grade_big, grade_middle, grade_small])
            teacher = _create_teacher(session, "T401", "王老師")
            session.flush()
            source = Classroom(
                name="向日葵",
                class_code="SUN-01",
                grade_id=grade_small.id,
                capacity=25,
                head_teacher_id=teacher.id,
                school_year=114,
                semester=2,
                is_active=True,
            )
            inactive_target = Classroom(
                name="向日葵",
                class_code="OLD-01",
                grade_id=grade_middle.id,
                capacity=10,
                school_year=115,
                semester=1,
                is_active=False,
            )
            session.add_all([source, inactive_target])
            session.flush()
            session.add(Student(student_id="S301", name="小晴", classroom_id=source.id, is_active=True))
            session.commit()
            source_id = source.id
            reused_target_id = inactive_target.id
            teacher_id = teacher.id

        login_res = _login(client, "classroom_promote_reuse_inactive")
        assert login_res.status_code == 200

        promote_res = client.post(
            "/api/classrooms/promote-academic-year",
            json={
                "source_school_year": 114,
                "source_semester": 2,
                "target_school_year": 115,
                "target_semester": 1,
                "classrooms": [
                    {
                        "source_classroom_id": source_id,
                        "target_name": "向日葵",
                    }
                ],
            },
        )

        assert promote_res.status_code == 201
        assert promote_res.json()["created_count"] == 1
        assert promote_res.json()["moved_student_count"] == 1

        with session_factory() as session:
            target = session.query(Classroom).filter(Classroom.id == reused_target_id).first()
            assert target is not None
            assert target.is_active is True
            assert target.class_code == "SUN-01"
            assert target.capacity == 25
            assert target.head_teacher_id == teacher_id
            moved_student = session.query(Student).filter(Student.student_id == "S301").first()
            assert moved_student.classroom_id == reused_target_id

    def test_create_rejects_duplicate_teacher_roles(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_admin")
            grade = ClassGrade(name="大班", is_active=True)
            teacher = _create_teacher(session, "T001", "王老師")
            session.add(grade)
            session.commit()
            grade_id = grade.id
            teacher_id = teacher.id

        login_res = _login(client, "classroom_admin")
        assert login_res.status_code == 200

        res = client.post(
            "/api/classrooms",
            json={
                "name": "向日葵班",
                "grade_id": grade_id,
                "capacity": 20,
                "head_teacher_id": teacher_id,
                "assistant_teacher_id": teacher_id,
            },
        )

        assert res.status_code == 400
        assert "同一位老師" in res.json()["detail"]

    def test_crud_flow_supports_create_update_and_soft_delete(self, client_with_db):
        client, session_factory = client_with_db
        with session_factory() as session:
            _create_user(session, "classroom_admin_flow")
            grade = ClassGrade(name="中班", is_active=True)
            session.add(grade)
            teacher_a = _create_teacher(session, "T101", "陳老師")
            teacher_b = _create_teacher(session, "T102", "林老師")
            teacher_c = _create_teacher(session, "T103", "黃老師")
            session.commit()
            grade_id = grade.id
            teacher_a_id = teacher_a.id
            teacher_b_id = teacher_b.id
            teacher_c_id = teacher_c.id

        login_res = _login(client, "classroom_admin_flow")
        assert login_res.status_code == 200

        create_res = client.post(
            "/api/classrooms",
            json={
                "name": "海豚班",
                "class_code": "DOL-01",
                "grade_id": grade_id,
                "capacity": 18,
                "head_teacher_id": teacher_a_id,
                "english_teacher_id": teacher_c_id,
            },
        )
        assert create_res.status_code == 201
        classroom_id = create_res.json()["id"]

        detail_res = client.get(f"/api/classrooms/{classroom_id}")
        assert detail_res.status_code == 200
        assert detail_res.json()["class_code"] == "DOL-01"
        assert detail_res.json()["head_teacher_id"] == teacher_a_id
        assert detail_res.json()["english_teacher_id"] == teacher_c_id
        assert detail_res.json()["english_teacher_name"] == "黃老師"
        assert detail_res.json()["art_teacher_id"] == teacher_c_id

        update_res = client.put(
            f"/api/classrooms/{classroom_id}",
            json={
                "name": "海豚探索班",
                "capacity": 22,
                "assistant_teacher_id": teacher_b_id,
                "art_teacher_id": teacher_a_id,
                "english_teacher_id": teacher_c_id,
            },
        )
        assert update_res.status_code == 200

        updated_detail_res = client.get(f"/api/classrooms/{classroom_id}")
        assert updated_detail_res.status_code == 200
        updated = updated_detail_res.json()
        assert updated["name"] == "海豚探索班"
        assert updated["capacity"] == 22
        assert updated["assistant_teacher_id"] == teacher_b_id
        assert updated["english_teacher_id"] == teacher_c_id
        assert updated["english_teacher_name"] == "黃老師"
        assert updated["art_teacher_id"] == teacher_c_id

        with session_factory() as session:
            student = Student(
                student_id="S002",
                name="小朋友乙",
                classroom_id=classroom_id,
                parent_phone="0912000111",
                status="已畢業",
                is_active=False,
            )
            session.add(student)
            session.commit()

        detail_with_history_res = client.get(f"/api/classrooms/{classroom_id}")
        assert detail_with_history_res.status_code == 200
        detail_with_history = detail_with_history_res.json()
        assert detail_with_history["current_count"] == 0
        assert len(detail_with_history["students"]) == 1
        inactive_student = next(student for student in detail_with_history["students"] if student["student_id"] == "S002")
        assert inactive_student["parent_phone"] == "0912000111"
        assert inactive_student["status"] == "已畢業"
        assert inactive_student["is_active"] is False

        with session_factory() as session:
            student = Student(
                student_id="S001",
                name="小朋友甲",
                classroom_id=classroom_id,
                is_active=True,
            )
            session.add(student)
            session.commit()

        blocked_delete_res = client.delete(f"/api/classrooms/{classroom_id}")
        assert blocked_delete_res.status_code == 409
        assert "在學學生" in blocked_delete_res.json()["detail"]

        with session_factory() as session:
            student = session.query(Student).filter(
                Student.classroom_id == classroom_id,
                Student.is_active == True,
            ).first()
            student.is_active = False
            session.commit()

        delete_res = client.delete(f"/api/classrooms/{classroom_id}")
        assert delete_res.status_code == 200

        list_res = client.get("/api/classrooms", params={"include_inactive": True})
        assert list_res.status_code == 200
        deleted = next(item for item in list_res.json() if item["id"] == classroom_id)
        assert deleted["is_active"] is False

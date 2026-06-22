"""tests/test_pos_student_key_pii_mask.py — POS outstanding-by-student：
student_key 對遮罩 caller 不得洩漏生日明文。

修補對象：api/activity/pos.py line 602
  "student_key": f"{student_name}|{birthday}"
  → can_see_student=False 時改用索引 / reg.id 避免生日外洩。
"""

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
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
    Employee,
    RegistrationCourse,
    User,
)
from utils.auth import hash_password
from utils.academic import resolve_current_academic_term

PASSWORD = "Temp123456"


@pytest.fixture
def pos_scope_client(tmp_path):
    db_path = tmp_path / "pos_scope.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _login(c, username, password=PASSWORD):
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def _seed(sf):
    """建立兩班，each 含一個欠費中學生報名；三種 caller：
    - own_teacher：STUDENTS_READ:own_class，導師班=大象班（管轄 c1）
    - admin_user ：wildcard *，全園可見
    回傳 (c1_id, c2_id, birthday_c1, birthday_c2)。
    """
    with sf() as s:
        sy, sem = resolve_current_academic_term()

        emp = Employee(
            employee_id="PT001", name="周老師", base_salary=32000, is_active=True
        )
        s.add(emp)
        s.flush()

        c1 = Classroom(name="大象班", is_active=True, head_teacher_id=emp.id)
        c2 = Classroom(name="長頸鹿班", is_active=True)
        s.add_all([c1, c2])
        s.flush()

        course = ActivityCourse(
            name="圍棋",
            price=1000,
            capacity=30,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
        s.add(course)
        s.flush()

        bday_c1 = "2020-03-15"
        bday_c2 = "2021-07-22"

        # 自班欠費報名（classroom_id = c1）
        reg1 = ActivityRegistration(
            student_name="自班生",
            birthday=bday_c1,
            class_name=c1.name,
            classroom_id=c1.id,
            is_active=True,
            paid_amount=0,
            school_year=sy,
            semester=sem,
        )
        s.add(reg1)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg1.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1000,
            )
        )

        # 他班欠費報名（classroom_id = c2，own_teacher 應遮）
        reg2 = ActivityRegistration(
            student_name="他班生",
            birthday=bday_c2,
            class_name=c2.name,
            classroom_id=c2.id,
            is_active=True,
            paid_amount=0,
            school_year=sy,
            semester=sem,
        )
        s.add(reg2)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg2.id,
                course_id=course.id,
                status="enrolled",
                price_snapshot=1000,
            )
        )

        # own_teacher：STUDENTS_READ:own_class，管轄班=大象班（c1）
        s.add(
            User(
                username="own_teacher",
                password_hash=hash_password(PASSWORD),
                role="activity_clerk",
                employee_id=emp.id,
                permission_names=[
                    "ACTIVITY_READ",
                    "ACTIVITY_WRITE",
                    "STUDENTS_READ:own_class",
                ],
                is_active=True,
            )
        )
        # admin：wildcard
        s.add(
            User(
                username="admin_user",
                password_hash=hash_password(PASSWORD),
                role="admin",
                permission_names=["*"],
                is_active=True,
            )
        )
        s.commit()
        return c1.id, c2.id, bday_c1, bday_c2


class TestStudentKeyPiiMask:
    """P1：student_key 對遮罩 caller 不得包含生日明文。"""

    def test_masked_caller_student_key_no_birthday(self, pos_scope_client):
        """own_teacher 對非管轄班（他班生）：
        - birthday 欄位應為 None
        - student_key 不得含其生日字串
        - 所有 group 的 student_key 互異（無 key 衝突）
        """
        c, sf = pos_scope_client
        c1_id, c2_id, bday_c1, bday_c2 = _seed(sf)

        _login(c, "own_teacher")
        res = c.get("/api/activity/pos/outstanding-by-student?q=班生")
        assert res.status_code == 200, res.text
        groups = res.json()["groups"]

        # 應有兩個 group（兩個欠費學生）
        assert len(groups) == 2, f"預期 2 groups，實得 {len(groups)}"

        # 找他班生 group
        other_group = next((g for g in groups if g["student_name"] == "他班生"), None)
        assert other_group is not None, "找不到他班生 group"

        # birthday 欄位必須是 None（遮罩）
        assert (
            other_group["birthday"] is None
        ), f"他班生 birthday 應遮為 None，實得 {other_group['birthday']!r}"

        # student_key 不得包含他班生的生日字串（核心 PII 洩漏檢查）
        assert (
            bday_c2 not in other_group["student_key"]
        ), f"student_key {other_group['student_key']!r} 仍含生日 {bday_c2}，PII 洩漏！"

        # 所有 group 的 student_key 互異（避免 Vue key 衝突）
        keys = [g["student_key"] for g in groups]
        assert len(keys) == len(set(keys)), f"student_key 有重複：{keys}"

    def test_admin_full_scope_key_contains_birthday(self, pos_scope_client):
        """防迴歸：admin（全園可見）birthday 欄位與 student_key 皆完整保留。"""
        c, sf = pos_scope_client
        c1_id, c2_id, bday_c1, bday_c2 = _seed(sf)

        _login(c, "admin_user")
        res = c.get("/api/activity/pos/outstanding-by-student?q=班生")
        assert res.status_code == 200, res.text
        groups = res.json()["groups"]

        assert len(groups) == 2

        for g in groups:
            if g["student_name"] == "自班生":
                assert g["birthday"] == bday_c1, "自班生 birthday 不應被遮"
                assert (
                    f"自班生|{bday_c1}" == g["student_key"]
                ), f"自班生 student_key 格式錯誤：{g['student_key']!r}"
            elif g["student_name"] == "他班生":
                assert g["birthday"] == bday_c2, "他班生（admin）birthday 不應被遮"
                assert (
                    f"他班生|{bday_c2}" == g["student_key"]
                ), f"他班生 student_key 格式錯誤：{g['student_key']!r}"
            else:
                pytest.fail(f"未預期的 group: {g['student_name']}")

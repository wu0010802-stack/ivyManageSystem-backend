"""RA-MED-10：StudentAllergy 結構化過敏表 EncryptedText 加密 + §6 稽核。

allergen / reaction_symptom / first_aid_note 改用 EncryptedText（ORM 透明加解密）。
severity 維持明文 String(10)：parent_portal/profile.py 以 DB-level
ORDER BY severity.desc() 排序（severe>moderate>mild 字母序剛好對），加密會破壞
此功能且 severity 為低敏感 3 值列舉 — 屬刻意偏離計畫（見報告）。

§6 稽核：list_allergies 讀回明文過敏內容時補寫 medical_access_log（field=allergy）。
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.base import Base
from models.classroom import LIFECYCLE_ACTIVE, Classroom, Student
from models.database import Employee, User
from models.medical_access_log import MEDICAL_FIELD_ALLERGY, MedicalAccessLog
from models.portfolio import StudentAllergy
from utils.auth import create_access_token


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "allergy-enc.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)
    yield sf
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def test_allergy_encrypted_at_rest_plaintext_via_orm(db_session):
    """寫一筆 StudentAllergy → DB 原始 allergen/symptom/note 為 Fernet 密文；
    ORM 讀回明文；severity 維持明文列舉。"""
    sf = db_session
    with sf() as s:
        stu = Student(
            student_id="ALG01",
            name="過敏生",
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        s.add(stu)
        s.flush()
        a = StudentAllergy(
            student_id=stu.id,
            allergen="花生",
            severity="severe",
            reaction_symptom="呼吸困難",
            first_aid_note="立即施打腎上腺素",
            active=True,
        )
        s.add(a)
        s.commit()
        alg_id = a.id

    # DB 原始值（繞過 ORM）：三個加密欄位應為 gAAAAA 開頭密文
    with sf() as s:
        row = s.execute(
            text(
                "SELECT allergen, severity, reaction_symptom, first_aid_note "
                "FROM student_allergies WHERE id = :id"
            ),
            {"id": alg_id},
        ).one()
        raw_allergen, raw_severity, raw_symptom, raw_note = row
        assert raw_allergen.startswith("gAAAAA"), raw_allergen
        assert raw_symptom.startswith("gAAAAA"), raw_symptom
        assert raw_note.startswith("gAAAAA"), raw_note
        # severity 維持明文（DB-level 排序需求）
        assert raw_severity == "severe"

    # ORM 透明解密
    with sf() as s:
        a = s.query(StudentAllergy).filter(StudentAllergy.id == alg_id).first()
        assert a.allergen == "花生"
        assert a.reaction_symptom == "呼吸困難"
        assert a.first_aid_note == "立即施打腎上腺素"
        assert a.severity == "severe"


def test_legacy_plaintext_allergen_passthrough(db_session):
    """遷移窗口：既有明文 allergen（含中文）ORM 讀取應原樣回（decrypt passthrough）。"""
    sf = db_session
    with sf() as s:
        stu = Student(
            student_id="ALG02",
            name="遺留生",
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        s.add(stu)
        s.flush()
        sid = stu.id
        # 繞過 ORM 直接寫明文（模擬加密前既有資料）
        s.execute(
            text(
                "INSERT INTO student_allergies "
                "(student_id, allergen, severity, active, created_at, updated_at) "
                "VALUES (:sid, '塵蟎', 'mild', 1, '2026-01-01', '2026-01-01')"
            ),
            {"sid": sid},
        )
        s.commit()

    with sf() as s:
        a = s.query(StudentAllergy).filter(StudentAllergy.student_id == sid).first()
        assert a.allergen == "塵蟎"  # legacy plaintext passthrough


# ── §6 稽核：list_allergies ────────────────────────────────────────────────


@pytest.fixture
def health_app(tmp_path):
    db_path = tmp_path / "allergy-audit.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    from api.student_health import router as health_router

    app = FastAPI()
    app.include_router(health_router)
    with TestClient(app) as c:
        yield c, sf
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _seed_allergy_and_teacher(sf, *, with_allergy=True):
    with sf() as s:
        emp = Employee(employee_id="E1", name="老師", is_active=True, base_salary=30000)
        s.add(emp)
        s.flush()
        cls = Classroom(name="A班", is_active=True, head_teacher_id=emp.id)
        s.add(cls)
        s.flush()
        stu = Student(
            student_id="ALG_AUD",
            name="稽核生",
            classroom_id=cls.id,
            is_active=True,
            lifecycle_status=LIFECYCLE_ACTIVE,
        )
        s.add(stu)
        s.flush()
        if with_allergy:
            s.add(
                StudentAllergy(
                    student_id=stu.id,
                    allergen="花生",
                    severity="severe",
                    active=True,
                )
            )
        teacher = User(
            username="t_alg",
            password_hash="!",
            role="teacher",
            employee_id=emp.id,
            permission_names=["STUDENTS_HEALTH_READ"],
            is_active=True,
            token_version=0,
        )
        s.add(teacher)
        s.commit()
        return {"student_id": stu.id, "teacher_id": teacher.id, "emp_id": emp.id}


def _token(uid, emp_id):
    return create_access_token(
        {
            "user_id": uid,
            "employee_id": emp_id,
            "role": "teacher",
            "name": "t_alg",
            "permission_names": ["STUDENTS_HEALTH_READ"],
            "token_version": 0,
        }
    )


def test_list_allergies_writes_medical_access_log(health_app):
    client, sf = health_app
    seed = _seed_allergy_and_teacher(sf, with_allergy=True)
    tk = _token(seed["teacher_id"], seed["emp_id"])

    r = client.get(
        f"/api/students/{seed['student_id']}/allergies",
        cookies={"access_token": tk},
    )
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["allergen"] == "花生"  # 明文回出

    with sf() as s:
        logs = (
            s.query(MedicalAccessLog)
            .filter(MedicalAccessLog.student_id == seed["student_id"])
            .all()
        )
        assert len(logs) == 1
        assert logs[0].field_name == MEDICAL_FIELD_ALLERGY
        assert logs[0].reason


def test_list_allergies_no_log_when_empty(health_app):
    """無過敏紀錄 → 不寫 §6 log（避免無意義噪音）。"""
    client, sf = health_app
    seed = _seed_allergy_and_teacher(sf, with_allergy=False)
    tk = _token(seed["teacher_id"], seed["emp_id"])

    r = client.get(
        f"/api/students/{seed['student_id']}/allergies",
        cookies={"access_token": tk},
    )
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []

    with sf() as s:
        logs = (
            s.query(MedicalAccessLog)
            .filter(MedicalAccessLog.student_id == seed["student_id"])
            .all()
        )
        assert len(logs) == 0

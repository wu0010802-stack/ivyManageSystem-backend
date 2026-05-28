"""P0d-2 兒童醫療欄位 reason-gated 讀取：

GET /students/{id}/medical?reason=...
- reason ≥10 字必填
- STUDENTS_HEALTH_READ 權限
- 每次取用寫 medical_access_log（不入 audit_log）
- ORM 透明加解密（既存 plaintext + 新加密混合）

Refs: docs/superpowers/specs/2026-05-28-medical-fields-encryption-design.md §4.3
"""

from __future__ import annotations

import os
import sys

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.students import router as students_router
from models.classroom import LIFECYCLE_ACTIVE
from models.database import Base, Classroom, Employee, Student, User
from models.medical_access_log import MedicalAccessLog
from utils.auth import create_access_token


@pytest.fixture(autouse=True)
def _set_test_medical_key(monkeypatch):
    test_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("MEDICAL_FIELD_ENCRYPTION_KEY", test_key)
    from config import get_settings
    from utils import medical_encryption

    get_settings.cache_clear()
    medical_encryption._get_fernet.cache_clear()
    yield
    get_settings.cache_clear()
    medical_encryption._get_fernet.cache_clear()


@pytest.fixture
def medical_client(tmp_path):
    db_path = tmp_path / "medical.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(students_router)
    with TestClient(app) as c:
        yield c, sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _admin_token() -> str:
    return create_access_token(
        {
            "user_id": 1,
            "employee_id": None,
            "role": "admin",
            "name": "admin",
            "permission_names": [
                "STUDENTS_READ",
                "STUDENTS_HEALTH_READ",
            ],
            "token_version": 0,
        }
    )


def _seed_admin_and_student(sf, *, allergy="花粉過敏發作打噴嚏"):
    with sf() as s:
        admin = User(
            id=1,
            username="admin",
            password_hash="!",
            role="admin",
            permission_names=["STUDENTS_READ", "STUDENTS_HEALTH_READ"],
            is_active=True,
            token_version=0,
        )
        s.add(admin)
        s.flush()

        student = Student(
            student_id="S001",
            name="王小寶",
            lifecycle_status=LIFECYCLE_ACTIVE,
            allergy=allergy,
            medication="氣管擴張劑",
            special_needs="冬季氣喘需注意保暖",
            is_active=True,
        )
        s.add(student)
        s.commit()
        return student.id


# ── 主路徑：reason ≥10 字 + 寫 access log ──


def test_medical_endpoint_returns_decrypted_fields(medical_client):
    client, sf = medical_client
    student_id = _seed_admin_and_student(sf)
    token = _admin_token()

    r = client.get(
        f"/api/students/{student_id}/medical?reason=2026-05-28 教師回報過敏反應評估",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # ORM 自動解密回中文
    assert body["allergy"] == "花粉過敏發作打噴嚏"
    assert body["medication"] == "氣管擴張劑"
    assert body["special_needs"] == "冬季氣喘需注意保暖"


def test_medical_endpoint_writes_access_log(medical_client):
    client, sf = medical_client
    student_id = _seed_admin_and_student(sf)
    token = _admin_token()

    reason = "2026-05-28 教師回報過敏反應評估與詢問用藥史"
    client.get(
        f"/api/students/{student_id}/medical?reason={reason}",
        headers={"Authorization": f"Bearer {token}"},
    )

    with sf() as s:
        logs = s.query(MedicalAccessLog).all()
        assert len(logs) == 1
        assert logs[0].student_id == student_id
        assert logs[0].field_name == "bundle"
        assert logs[0].reason == reason
        assert logs[0].user_id == 1


def test_medical_db_raw_column_is_encrypted(medical_client):
    """確認 ORM 加密：raw SQL 看到密文，不是明文。"""
    client, sf = medical_client
    student_id = _seed_admin_and_student(sf, allergy="獨特字串123abc")
    with sf() as s:
        row = s.execute(
            text("SELECT allergy FROM students WHERE id = :id"), {"id": student_id}
        ).first()
        raw_allergy = row[0]
        # raw 不應等於明文（已加密）
        assert raw_allergy != "獨特字串123abc"
        # 但 ORM 讀取應解密回明文
        student = s.query(Student).get(student_id)
        assert student.allergy == "獨特字串123abc"


# ── reason gate ──


def test_medical_endpoint_rejects_short_reason(medical_client):
    client, sf = medical_client
    student_id = _seed_admin_and_student(sf)
    token = _admin_token()

    r = client.get(
        f"/api/students/{student_id}/medical?reason=短",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


def test_medical_endpoint_rejects_missing_reason(medical_client):
    client, sf = medical_client
    student_id = _seed_admin_and_student(sf)
    token = _admin_token()

    r = client.get(
        f"/api/students/{student_id}/medical",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


def test_medical_endpoint_rejects_oversized_reason(medical_client):
    """reason 太長（>500 字）也要擋"""
    client, sf = medical_client
    student_id = _seed_admin_and_student(sf)
    token = _admin_token()

    r = client.get(
        f"/api/students/{student_id}/medical?reason={'a' * 501}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422


# ── permission gate ──


def test_medical_endpoint_requires_health_read_permission(medical_client):
    """無 STUDENTS_HEALTH_READ → 403"""
    client, sf = medical_client
    student_id = _seed_admin_and_student(sf)

    # 給只有 STUDENTS_READ 的 token（缺 HEALTH_READ）
    weak_token = create_access_token(
        {
            "user_id": 2,
            "employee_id": None,
            "role": "teacher",
            "name": "weak",
            "permission_names": ["STUDENTS_READ"],
            # no HEALTH_READ
            "token_version": 0,
        }
    )

    r = client.get(
        f"/api/students/{student_id}/medical?reason=測試取用權限機制",
        headers={"Authorization": f"Bearer {weak_token}"},
    )
    # 401 (user_id 2 不存在 DB / auth check fail) 或 403 (perm check fail) 都代表被擋
    assert r.status_code in (401, 403)


def test_medical_endpoint_requires_auth(medical_client):
    client, sf = medical_client
    student_id = _seed_admin_and_student(sf)
    r = client.get(f"/api/students/{student_id}/medical?reason=匿名取用測試")
    assert r.status_code in (401, 403)


# ── Legacy plaintext 兼容 ──


def test_medical_endpoint_reads_legacy_plaintext_unchanged(medical_client):
    """既有未加密 plaintext row 在切到 EncryptedText 後仍可讀（migration window）。"""
    client, sf = medical_client

    # 直接 raw SQL 插入 plaintext（繞過 ORM 加密）
    with sf() as s:
        admin = User(
            id=1,
            username="admin",
            password_hash="!",
            role="admin",
            permission_names=["STUDENTS_HEALTH_READ"],
            is_active=True,
            token_version=0,
        )
        s.add(admin)
        s.flush()
        s.execute(
            text(
                "INSERT INTO students (student_id, name, lifecycle_status, "
                "allergy, medication, special_needs, is_active) "
                "VALUES ('S_legacy', '舊資料學生', 'active', '舊明文過敏', "
                "'舊明文藥單', '舊明文需求', 1)"
            )
        )
        s.commit()
        student_id = s.execute(
            text("SELECT id FROM students WHERE student_id='S_legacy'")
        ).scalar()

    token = _admin_token()
    r = client.get(
        f"/api/students/{student_id}/medical?reason=測試 legacy plaintext 兼容",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    # legacy plaintext 應原樣回（TypeDecorator decrypt 對非 Fernet token passthrough）
    assert body["allergy"] == "舊明文過敏"
    assert body["medication"] == "舊明文藥單"

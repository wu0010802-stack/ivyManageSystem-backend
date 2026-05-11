"""tests/test_gov_moe_certificates.py — 在學證明 model + endpoint tests."""

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
from api.gov_moe import router as gov_moe_router
from models.base import Base
from models.database import Student, User
from models.gov_moe import EnrollmentCertificate  # noqa: F401 — registers table on Base
from utils.auth import hash_password

# ---------------------------------------------------------------------------
# Fixtures (copied verbatim from test_gov_moe_disability_documents.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def gov_moe_client(tmp_path):
    db_path = tmp_path / "gov_moe.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(gov_moe_router, prefix="/api")

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_enrollment_certificate_model_exists():
    from models.gov_moe import EnrollmentCertificate

    for f in (
        "id",
        "student_id",
        "year",
        "seq",
        "purpose",
        "copies",
        "issue_date",
        "issued_by_user_id",
        "pdf_path",
        "created_at",
    ):
        assert hasattr(EnrollmentCertificate, f), f"missing {f}"


def test_enrollment_certificate_unique_year_seq():
    from models.gov_moe import EnrollmentCertificate

    constraints = [c.name for c in EnrollmentCertificate.__table__.constraints]
    assert any("uq_enrollment_cert_year_seq" in (n or "") for n in constraints)


def test_enrollment_certificate_serial_format():
    from models.gov_moe import EnrollmentCertificate

    c = EnrollmentCertificate(year=2026, seq=7)
    assert c.serial == "EC-2026-0007"


def test_enrollment_cert_pdf_contains_required_fields():
    from datetime import date
    from services.enrollment_certificate_pdf import generate_enrollment_cert_pdf

    pdf_bytes = generate_enrollment_cert_pdf(
        student_name="王小明",
        student_no="S0001",
        id_number="A123456789",
        admit_date=date(2024, 8, 1),
        classroom_name="向日葵班",
        purpose="申請育兒津貼",
        issue_date=date(2026, 5, 12),
        serial="EC-2026-0001",
        copies=2,
        institution_name="義華幼兒園",
    )
    assert isinstance(pdf_bytes, bytes) and len(pdf_bytes) > 1000
    assert pdf_bytes.startswith(b"%PDF")


# ---------------------------------------------------------------------------
# Endpoint tests (C4)
# ---------------------------------------------------------------------------


def _login_admin(client, session_factory):
    from models.database import User
    from utils.auth import hash_password

    with session_factory() as s:
        s.add(
            User(
                username="admin",
                password_hash=hash_password("AdminPass1"),
                role="admin",
                permissions=-1,
                is_active=True,
            )
        )
        s.commit()
    resp = client.post(
        "/api/auth/login", json={"username": "admin", "password": "AdminPass1"}
    )
    return resp.json().get("access_token") or resp.cookies.get("access_token")


def _seed_student(session_factory, student_id_field: str = "S0001"):
    from models.database import Student

    with session_factory() as s:
        st = Student(
            name="王小明",
            student_id=student_id_field,
            enrollment_date=date(2024, 8, 1),
            is_active=True,
        )
        s.add(st)
        s.commit()
        s.refresh(st)
        return st.id


def test_generate_certificate_assigns_serial_and_returns_pdf(gov_moe_client):
    client, session_factory = gov_moe_client
    token = _login_admin(client, session_factory)
    sid = _seed_student(session_factory)

    resp = client.post(
        f"/api/gov-moe/certificates/{sid}/generate",
        json={"issue_date": "2026-05-12", "purpose": "申請育兒津貼", "copies": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["serial"] == "EC-2026-0001"
    assert body["copies"] == 2
    assert "pdf_url" in body or "pdf_base64" in body


def test_generate_certificate_increments_serial_per_year(gov_moe_client):
    client, session_factory = gov_moe_client
    token = _login_admin(client, session_factory)
    sid = _seed_student(session_factory)

    for i in range(1, 4):
        r = client.post(
            f"/api/gov-moe/certificates/{sid}/generate",
            json={"issue_date": "2026-06-01", "purpose": "test", "copies": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201
        assert r.json()["serial"] == f"EC-2026-{i:04d}"


def test_get_history_filters_by_student(gov_moe_client):
    client, session_factory = gov_moe_client
    token = _login_admin(client, session_factory)
    s1 = _seed_student(session_factory, "S0001")
    s2 = _seed_student(session_factory, "S0002")

    client.post(
        f"/api/gov-moe/certificates/{s1}/generate",
        json={"issue_date": "2026-05-12", "purpose": "p1", "copies": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    client.post(
        f"/api/gov-moe/certificates/{s2}/generate",
        json={"issue_date": "2026-05-12", "purpose": "p2", "copies": 1},
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = client.get(
        f"/api/gov-moe/certificates/history?student_id={s1}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1 and rows[0]["student_id"] == s1


def test_teacher_cannot_generate_certificate(gov_moe_client):
    from models.database import User
    from utils.auth import hash_password

    client, session_factory = gov_moe_client
    sid = _seed_student(session_factory)
    with session_factory() as s:
        s.add(
            User(
                username="t",
                password_hash=hash_password("Teach123"),
                role="teacher",
                permissions=0,
                is_active=True,
            )
        )
        s.commit()
    _resp = client.post(
        "/api/auth/login", json={"username": "t", "password": "Teach123"}
    )
    tok = _resp.json().get("access_token") or _resp.cookies.get("access_token")

    resp = client.post(
        f"/api/gov-moe/certificates/{sid}/generate",
        json={"issue_date": "2026-05-12", "purpose": "x", "copies": 1},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 403


def test_audit_pattern_registered_for_certificate():
    from utils.audit import ENTITY_PATTERNS, ENTITY_LABELS

    assert any("enrollment_certificate" in (et or "") for _, et in ENTITY_PATTERNS)
    assert ENTITY_LABELS.get("enrollment_certificate") == "在學證明"

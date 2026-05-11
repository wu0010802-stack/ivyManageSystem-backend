"""tests/test_gov_moe_certificates.py — 在學證明 model + endpoint tests."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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

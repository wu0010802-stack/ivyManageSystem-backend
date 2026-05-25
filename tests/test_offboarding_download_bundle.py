"""驗證離職 ZIP bundle 產生（§Task 4）。"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, User
from models.offboarding import EmployeeOffboardingRecord
from utils.auth import hash_password
from models.salary import SalaryRecord

from services.offboarding.download_bundle import build_offboarding_zip

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（對齊既有 offboarding test pattern）。"""
    db_path = tmp_path / "download_bundle.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)

    session = session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def employee_factory(db_session):
    def _factory(*, hire_date: date = date(2020, 1, 1)) -> Employee:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"BNDL{_counter:04d}",
            name=f"測試員工{_counter}",
            hire_date=hire_date,
            is_active=True,
        )
        db_session.add(emp)
        db_session.flush()
        return emp

    return _factory


@pytest.fixture
def record_factory(db_session):
    def _factory(
        *,
        employee_id: int,
        resign_date: date = date(2026, 6, 15),
        certificate_pdf_path: str | None = None,
    ) -> EmployeeOffboardingRecord:
        global _counter
        _counter += 1
        user = User(
            username=f"admin_{_counter}",
            password_hash=hash_password("Passw0rd!"),
            role="admin",
            is_active=True,
        )
        db_session.add(user)
        db_session.flush()
        rec = EmployeeOffboardingRecord(
            employee_id=employee_id,
            resign_date=resign_date,
            opened_at=datetime.now(),
            opened_by_user_id=user.id,
            certificate_pdf_path=certificate_pdf_path,
        )
        db_session.add(rec)
        db_session.flush()
        return rec

    return _factory


@pytest.fixture
def salary_record_factory(db_session):
    def _factory(
        *,
        employee_id: int,
        salary_year: int,
        salary_month: int,
    ) -> SalaryRecord:
        sr = SalaryRecord(
            employee_id=employee_id,
            salary_year=salary_year,
            salary_month=salary_month,
        )
        db_session.add(sr)
        db_session.flush()
        return sr

    return _factory


def test_raises_when_certificate_pdf_path_is_none(
    db_session, employee_factory, record_factory
):
    """record.certificate_pdf_path 為 None → raise ValueError。"""
    emp = employee_factory()
    rec = record_factory(employee_id=emp.id, certificate_pdf_path=None)

    with pytest.raises(ValueError, match="certificate_pdf_path"):
        build_offboarding_zip(db_session, rec)


def test_builds_zip_with_certificate_and_csv(
    tmp_path,
    db_session,
    employee_factory,
    record_factory,
    salary_record_factory,
):
    """有離職證明 PDF + 有薪資記錄 → ZIP 含 certificate.pdf + attendance.csv（+ 月薪 PDF skip 不擋）。"""
    emp = employee_factory(hire_date=date(2025, 1, 1))

    # 建立假 certificate PDF 檔案
    cert_path = tmp_path / "cert.pdf"
    cert_path.write_bytes(b"%PDF-1.4 fake content")

    rec = record_factory(
        employee_id=emp.id,
        resign_date=date(2026, 6, 15),
        certificate_pdf_path=str(cert_path),
    )

    # 建立薪資記錄（讓 bundle 嘗試產 salary PDF）
    salary_record_factory(employee_id=emp.id, salary_year=2026, salary_month=5)

    # mock generate_salary_pdf 讓它回假 PDF bytes（避免真實 reportlab 依賴）
    fake_pdf = b"%PDF-1.4 salary fake"
    with patch(
        "services.offboarding.download_bundle.generate_salary_pdf",
        return_value=fake_pdf,
    ):
        zip_bytes = build_offboarding_zip(db_session, rec)

    assert isinstance(zip_bytes, bytes)
    assert len(zip_bytes) > 0

    # 驗證 ZIP 結構
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert any("certificate" in n for n in names), f"缺 certificate：{names}"
        assert any("attendance" in n for n in names), f"缺 attendance CSV：{names}"
        # 有薪資 PDF（salary 2026-05）
        assert any(
            "salary" in n and "2026" in n and "05" in n for n in names
        ), f"缺 salary PDF：{names}"

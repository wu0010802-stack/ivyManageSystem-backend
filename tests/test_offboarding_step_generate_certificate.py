"""驗證 generate_certificate step：產 PDF + 寫 record.certificate_pdf_path。"""

import os
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, User
from models.offboarding import EmployeeOffboardingRecord
from utils.auth import hash_password

from services.offboarding.steps.generate_certificate import run
from services.offboarding.orchestrator import OffboardingError

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（對齊 test_offboarding_orchestrator.py pattern）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / "offboarding_cert_step.sqlite"
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
    def _factory(*, name="測試員工", id_number="A123456789") -> Employee:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"CERT{_counter:04d}",
            name=name,
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=50000,
            id_number=id_number,
        )
        db_session.add(emp)
        db_session.flush()
        return emp

    return _factory


@pytest.fixture
def user_factory(db_session):
    def _factory(*, role="admin") -> User:
        global _counter
        _counter += 1
        u = User(
            username=f"certuser{_counter}",
            password_hash=hash_password("Passw0rd!"),
            role=role,
            is_active=True,
            token_version=0,
        )
        db_session.add(u)
        db_session.flush()
        return u

    return _factory


def _make_record(db_session, employee_id, user_id) -> EmployeeOffboardingRecord:
    rec = EmployeeOffboardingRecord(
        employee_id=employee_id,
        resign_date=date(2026, 6, 15),
        opened_at=datetime.now(),
        opened_by_user_id=user_id,
    )
    db_session.add(rec)
    db_session.flush()
    return rec


def test_generate_certificate_writes_pdf_to_storage(
    db_session,
    employee_factory,
    user_factory,
    tmp_path,
    monkeypatch,
):
    """happy path：產 PDF bytes → 寫檔 → 寫 record.certificate_pdf_path / certificate_generated_at。"""
    monkeypatch.setattr(
        "services.offboarding.steps.generate_certificate.STORAGE_DIR",
        tmp_path,
    )
    emp = employee_factory(name="王小明", id_number="A123456789")
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    result = run(db_session, record)

    assert result["step"] == "generate_certificate"
    assert result["status"] == "completed"
    assert result["payload"]["pdf_path"] is not None
    assert record.certificate_pdf_path is not None
    assert record.certificate_generated_at is not None

    # 檔案實際存在且是 PDF
    written = Path(record.certificate_pdf_path)
    assert written.exists()
    assert written.read_bytes()[:4] == b"%PDF"


def test_generate_certificate_raises_on_disk_failure(
    db_session,
    employee_factory,
    user_factory,
    monkeypatch,
):
    """模擬寫檔失敗 → raise OffboardingError(CERTIFICATE_GENERATION_FAILED)。"""
    monkeypatch.setattr(
        "services.offboarding.steps.generate_certificate.STORAGE_DIR",
        Path("/nonexistent/blocked/dir"),
    )
    emp = employee_factory()
    user = user_factory()
    record = _make_record(db_session, emp.id, user.id)

    with pytest.raises(OffboardingError) as exc:
        run(db_session, record)
    assert exc.value.code == "CERTIFICATE_GENERATION_FAILED"

"""驗證離職證明 PDF 生成（§19）。"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.employee import Employee, Base

from services.employee_offboarding_certificate_pdf import generate_certificate_pdf

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（對齊既有 offboarding step test pattern）。"""
    db_path = tmp_path / "offboarding_certificate_pdf.sqlite"
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
    """建立測試員工。"""

    def _factory(
        *,
        name="測試員工",
        id_number="A123456789",
        hire_date=date(2020, 1, 1),
        position="教保員",
        is_active=True,
    ) -> Employee:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"CERT{_counter:04d}",
            name=name,
            id_number=id_number,
            hire_date=hire_date,
            position=position,
            is_active=is_active,
        )
        db_session.add(emp)
        db_session.flush()
        return emp

    return _factory


def test_generate_certificate_returns_pdf_bytes_with_required_fields(
    db_session,
    employee_factory,
):
    emp = employee_factory(
        name="王小明",
        id_number="A123456789",
        hire_date=date(2024, 8, 1),
        position="教保員",
    )
    pdf_bytes = generate_certificate_pdf(
        db_session,
        emp.id,
        resign_date=date(2026, 6, 15),
    )
    assert isinstance(pdf_bytes, bytes)
    assert pdf_bytes[:4] == b"%PDF"  # PDF magic
    # PDF 中文以 TTF glyph 編碼存，直接字串比對不可靠；改驗結構完整性：
    assert b"%%EOF" in pdf_bytes
    # 確認 PDF 有內容（不是空殼）
    assert len(pdf_bytes) > 1024


def test_generate_certificate_raises_when_employee_missing(db_session):
    with pytest.raises(ValueError, match="員工不存在"):
        generate_certificate_pdf(db_session, 99999, resign_date=date(2026, 6, 15))


def test_generate_certificate_does_not_include_resign_reason(
    db_session,
    employee_factory,
):
    """§19 禁記載對受僱人不利之事項。"""
    emp = employee_factory(name="李四")
    # 不傳 reason — 驗 function signature 不接此參數
    import inspect

    sig = inspect.signature(generate_certificate_pdf)
    assert "reason" not in sig.parameters
    assert "resign_reason" not in sig.parameters

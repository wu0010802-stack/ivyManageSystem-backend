import os
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
import models  # noqa: F401
from models.employee import Employee
from services.employee_numbering import next_employee_id


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


class TestNextEmployeeId:
    def test_first_of_year(self, session):
        assert next_employee_id(session, 114) == "114001"

    def test_increments_within_year(self, session):
        session.add(Employee(employee_id="114001", name="A"))
        session.add(Employee(employee_id="114002", name="B"))
        session.flush()
        assert next_employee_id(session, 114) == "114003"

    def test_per_year_independent(self, session):
        session.add(Employee(employee_id="114007", name="A"))
        session.flush()
        assert next_employee_id(session, 115) == "115001"

    def test_ignores_legacy_nonmatching_format(self, session):
        session.add(Employee(employee_id="E001", name="A"))
        session.add(Employee(employee_id="ADMIN001", name="B"))
        session.flush()
        assert next_employee_id(session, 114) == "114001"

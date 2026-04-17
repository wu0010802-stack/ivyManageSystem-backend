"""Repository 層的單元測試（使用 SQLite in-memory 隔離 DB）。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.employee import Employee, JobTitle
from models.classroom import Student
from repositories import BaseRepository, EmployeeRepository, StudentRepository


@pytest.fixture
def memory_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class TestBaseRepositoryValidation:
    def test_subclass_without_model_raises(self, memory_session):
        class BadRepo(BaseRepository):
            pass  # 忘了設定 model

        with pytest.raises(NotImplementedError):
            BadRepo(memory_session)

    def test_list_rejects_unknown_filter_key(self, memory_session):
        repo = EmployeeRepository(memory_session)
        with pytest.raises(ValueError, match="無欄位 'not_a_column'"):
            repo.list(not_a_column=1)

    def test_count_rejects_unknown_filter_key(self, memory_session):
        repo = EmployeeRepository(memory_session)
        with pytest.raises(ValueError):
            repo.count(not_a_column=1)


class TestEmployeeRepository:
    def test_add_then_get_by_id(self, memory_session):
        repo = EmployeeRepository(memory_session)
        emp = Employee(employee_id="E001", name="王小明", is_active=True)
        repo.add(emp)
        repo.flush()
        assert emp.id is not None
        fetched = repo.get_by_id(emp.id)
        assert fetched.name == "王小明"

    def test_get_by_employee_id_business_key(self, memory_session):
        repo = EmployeeRepository(memory_session)
        repo.add(Employee(employee_id="E010", name="Alice", is_active=True))
        repo.flush()
        found = repo.get_by_employee_id("E010")
        assert found is not None
        assert found.name == "Alice"
        assert repo.get_by_employee_id("DOES_NOT_EXIST") is None

    def test_list_active_filters_correctly(self, memory_session):
        repo = EmployeeRepository(memory_session)
        repo.add(Employee(employee_id="E100", name="A", is_active=True))
        repo.add(Employee(employee_id="E101", name="B", is_active=False))
        repo.add(Employee(employee_id="E102", name="C", is_active=True))
        repo.flush()
        active = repo.list_active()
        assert len(active) == 2
        assert {e.name for e in active} == {"A", "C"}

    def test_search_by_name_or_employee_id(self, memory_session):
        repo = EmployeeRepository(memory_session)
        repo.add(Employee(employee_id="E200", name="王大明", is_active=True))
        repo.add(Employee(employee_id="E201", name="張小華", is_active=True))
        repo.flush()
        assert len(repo.search("王")) == 1
        assert len(repo.search("E20")) == 2
        assert repo.search("") == []

    def test_exists(self, memory_session):
        repo = EmployeeRepository(memory_session)
        emp = Employee(employee_id="E300", name="X", is_active=True)
        repo.add(emp)
        repo.flush()
        assert repo.exists(emp.id) is True
        assert repo.exists(999999) is False

    def test_count_with_filter(self, memory_session):
        repo = EmployeeRepository(memory_session)
        repo.add(Employee(employee_id="E400", name="A", is_active=True))
        repo.add(Employee(employee_id="E401", name="B", is_active=False))
        repo.flush()
        assert repo.count() == 2
        assert repo.count(is_active=True) == 1
        assert repo.count(is_active=False) == 1

    def test_delete_removes_entity(self, memory_session):
        repo = EmployeeRepository(memory_session)
        emp = Employee(employee_id="E500", name="Z", is_active=True)
        repo.add(emp)
        repo.flush()
        emp_id = emp.id
        repo.delete(emp)
        repo.flush()
        assert repo.get_by_id(emp_id) is None


class TestStudentRepository:
    def test_list_active_by_classroom(self, memory_session):
        repo = StudentRepository(memory_session)
        repo.add(Student(student_id="S001", name="A", classroom_id=1, is_active=True))
        repo.add(Student(student_id="S002", name="B", classroom_id=1, is_active=False))
        repo.add(Student(student_id="S003", name="C", classroom_id=2, is_active=True))
        repo.flush()
        class1 = repo.list_active_by_classroom(1)
        assert len(class1) == 1
        assert class1[0].name == "A"
        assert repo.list_active_by_classroom(99) == []

    def test_search_by_parent_name(self, memory_session):
        repo = StudentRepository(memory_session)
        repo.add(
            Student(
                student_id="S010",
                name="孩子A",
                parent_name="王爸爸",
                classroom_id=1,
                is_active=True,
            )
        )
        repo.flush()
        found = repo.search("王爸爸")
        assert len(found) == 1

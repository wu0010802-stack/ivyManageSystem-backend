import sys
import os
import pytest

# 讓 tests 可以 import backend 模組
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.salary_engine import SalaryEngine
from services.attendance_parser import AttendanceResult


@pytest.fixture
def engine():
    """SalaryEngine 實例（不從 DB 載入）"""
    return SalaryEngine(load_from_db=False)


@pytest.fixture
def sample_attendance():
    """標準考勤資料"""
    return AttendanceResult(
        employee_name="測試員工",
        total_days=22,
        normal_days=20,
        late_count=2,
        early_leave_count=1,
        missing_punch_in_count=1,
        missing_punch_out_count=0,
        total_late_minutes=45,
        total_early_minutes=15,
        details=[],
    )


@pytest.fixture
def sample_employee():
    """正職員工資料"""
    return {
        "employee_id": "E001",
        "name": "王小明",
        "title": "幼兒園教師",
        "position": "幼兒園教師",
        "employee_type": "regular",
        "base_salary": 30000,
        "hourly_rate": 0,
        "insurance_salary": 30000,
        "dependents": 0,
        "hire_date": "2025-01-01",
    }


@pytest.fixture
def sample_classroom_context():
    """班級上下文（班導，大班）"""
    return {
        "role": "head_teacher",
        "grade_name": "大班",
        "current_enrollment": 27,
        "has_assistant": True,
        "is_shared_assistant": False,
    }


class QueryCounter:
    """計數 SQLAlchemy engine 發出的查詢次數，用於 N+1 回歸測試。

    使用方式：
        def test_list_avoids_n_plus_one(counter, session):
            with counter:
                result = list_employees_with_classroom(session)
            assert counter.count <= 2, f"expected ≤ 2 queries, got {counter.count}"
    """

    def __init__(self, engine):
        self.engine = engine
        self.count = 0
        self.statements: list[str] = []

    def _on_execute(self, conn, cursor, statement, parameters, context, executemany):
        self.count += 1
        self.statements.append(statement)

    def __enter__(self):
        from sqlalchemy import event

        event.listen(self.engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, exc_type, exc, tb):
        from sqlalchemy import event

        event.remove(self.engine, "before_cursor_execute", self._on_execute)
        return False


@pytest.fixture
def query_counter():
    """回傳 QueryCounter 工廠（call with engine）。

    因為測試可能使用不同 engine（in-memory SQLite vs staging PG），
    這個 fixture 讓測試自己傳入 engine。
    """
    return QueryCounter

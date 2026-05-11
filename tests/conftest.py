import sys
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 讓 tests 可以 import backend 模組
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.salary_engine import SalaryEngine
from services.attendance_parser import AttendanceResult
import models.base as base_module
from models.database import Base


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


@pytest.fixture
def test_db_session(tmp_path):
    """共用 SQLite in-memory 測試 DB fixture。

    建立 SQLite 引擎、swap 全域 engine/_SessionFactory，
    以 Base.metadata.create_all 建立全部 ORM 表，
    yield 測試用 session，測試結束後還原全域狀態。

    適用：需要實際 DB 操作的 model / CRUD 測試。
    """
    db_path = tmp_path / "test_gov_data.sqlite"
    test_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    test_session_factory = sessionmaker(bind=test_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = test_engine
    base_module._SessionFactory = test_session_factory

    Base.metadata.create_all(test_engine)

    session = test_session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    test_engine.dispose()


import pytest as _pytest_for_gov_data


@_pytest_for_gov_data.fixture
def pending_brackets_staging(test_db_session):
    """提供一筆 pending InsuranceBracketsStaging（year=2027 / 1 列簡化資料）。"""
    from models.database import InsuranceBracketsStaging, session_scope

    with session_scope() as s:
        st = InsuranceBracketsStaging(
            effective_year=2027,
            composed_from={"mol_labor_brackets": 1},
            brackets=[
                {
                    "amount": 30000,
                    "labor_employee": 600,
                    "labor_employer": 2100,
                    "health_employee": 470,
                    "health_employer": 1450,
                    "pension": 1800,
                },
            ],
            rates={"labor_max_insured": 45800},
            diff_summary={"added": [], "removed": [], "modified": []},
            status="pending",
        )
        s.add(st)
        s.flush()
        return st.id


@_pytest_for_gov_data.fixture
def sample_unfinalized_salary_2027(test_db_session):
    """模擬一筆 2027 年未封存的 SalaryRecord，用於驗證 mark_stale 觸發。

    SalaryRecord 使用 salary_year / salary_month 欄位（非 year/month）。
    """
    from models.database import SalaryRecord, session_scope

    with session_scope() as s:
        rec = SalaryRecord(
            employee_id=1,
            salary_year=2027,
            salary_month=1,
            base_salary=30000,
            net_salary=27000,
            is_finalized=False,
            needs_recalc=False,
        )
        s.add(rec)
        s.flush()

        # 回傳一個含 id 的 namedtuple-like 物件，確保 id 可在 session 外存取
        class _Rec:
            def __init__(self, id_):
                self.id = id_

        return _Rec(rec.id)


@_pytest_for_gov_data.fixture
def sample_minimum_wage_staging_2027(test_db_session):
    """提供一筆 pending MinimumWageStaging + 對應 GovDataSnapshot（年度 2027）。"""
    from datetime import date
    from models.database import GovDataSnapshot, MinimumWageStaging, session_scope

    with session_scope() as s:
        snap = GovDataSnapshot(
            source="mol_minimum_wage",
            source_url="https://example.com",
            http_status=200,
            raw_payload={},
            payload_hash="b" * 64,
        )
        s.add(snap)
        s.flush()
        st = MinimumWageStaging(
            effective_date=date(2027, 1, 1),
            monthly=30500,
            hourly=200,
            source_snapshot_id=snap.id,
            status="pending",
        )
        s.add(st)
        s.flush()
        return st.id

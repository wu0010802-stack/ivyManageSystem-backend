"""驗證 orchestrator types + 主入口 signature + 串接 4 step + 失敗 rollback。"""

import os
import sys
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import Base, Employee, User
from models.leave import LeaveQuota
from models.salary import SalaryRecord
from utils.auth import hash_password

from services.offboarding.orchestrator import (
    OffboardingError,
    OffboardingResult,
    StepResult,
    process_offboarding,
)

_counter = 0


@pytest.fixture
def db_session(tmp_path):
    """SQLite test session（inline — 對齊 test_leave_quota_helpers pattern）。"""
    db_path = tmp_path / "offboarding_orchestrator.sqlite"
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
    def _factory(
        *,
        hire_date=date(2020, 1, 1),
        is_active=True,
        daily_wage=None,
        monthly_salary=None,
    ) -> Employee:
        global _counter
        _counter += 1
        # daily_wage → base_salary = daily_wage * 30
        if daily_wage is not None:
            base_salary = int(daily_wage * 30)
        elif monthly_salary is not None:
            base_salary = int(monthly_salary)
        else:
            base_salary = 0
        emp = Employee(
            employee_id=f"OBT{_counter:04d}",
            name=f"測試員工{_counter}",
            hire_date=hire_date,
            is_active=is_active,
            base_salary=base_salary,
        )
        db_session.add(emp)
        db_session.flush()
        return emp

    return _factory


@pytest.fixture
def user_factory(db_session):
    def _factory(
        *, role="admin", employee_id=None, is_active=True, token_version=0
    ) -> User:
        global _counter
        _counter += 1
        u = User(
            username=f"testuser{_counter}",
            password_hash=hash_password("Passw0rd!"),
            role=role,
            is_active=is_active,
            token_version=token_version,
            employee_id=employee_id,
        )
        db_session.add(u)
        db_session.flush()
        return u

    return _factory


@pytest.fixture
def leave_quota_factory(db_session):
    def _factory(
        *,
        employee_id: int,
        year: int,
        leave_type: str,
        total_hours: float,
        school_year=None,
    ) -> LeaveQuota:
        quota = LeaveQuota(
            employee_id=employee_id,
            year=year,
            leave_type=leave_type,
            total_hours=total_hours,
            school_year=school_year,
        )
        db_session.add(quota)
        db_session.flush()
        return quota

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


# ── tests ──────────────────────────────────────────────────────────────────


def test_step_result_typeddict_fields():
    sr: StepResult = {
        "step": "mark_appraisal",
        "status": "completed",
        "completed_at": None,
        "payload": None,
        "error": None,
    }
    assert sr["step"] == "mark_appraisal"


def test_offboarding_error_subclass_of_exception():
    err = OffboardingError("test", code="LEAVE_BALANCE_NOT_FOUND")
    assert isinstance(err, Exception)
    assert err.code == "LEAVE_BALANCE_NOT_FOUND"


def test_process_offboarding_creates_record_with_only_default_step_unimplemented(
    db_session, employee_factory, user_factory, monkeypatch
):
    """初版 orchestrator 殼：steps 已接 4 step。
    此測試確認框架可呼叫；happy path 細節另見 test_happy_path_all_4_steps_complete。"""
    emp = employee_factory(daily_wage=1800)
    user = user_factory()
    leave_quota_factory_inline = None  # 此測試不建 quota，檢驗只建 record + steps list

    # monkeypatch snapshot_leave.run 讓它直接回 completed（避免 LEAVE_BALANCE_NOT_FOUND）
    from services.offboarding.steps import snapshot_leave as sl_mod

    def _fake_run(session, record):
        record.leave_balance_snapshot = {
            "snapshot_date": str(date(2026, 6, 15)),
            "total_hours": 0,
            "used_hours": 0,
            "remaining_hours": 0,
            "remaining_days": 0.0,
            "daily_wage": 1800.0,
            "payout_amount": 0.0,
            "calc_rule_version": "labor_act_38_2026_v1",
        }
        from datetime import datetime

        record.leave_snapshot_at = datetime.now()
        return {
            "step": "snapshot_leave",
            "status": "completed",
            "completed_at": datetime.now(),
            "payload": {"days": 0.0, "payout": 0.0},
            "error": None,
        }

    monkeypatch.setattr(sl_mod, "run", _fake_run)

    result = process_offboarding(
        session=db_session,
        employee_id=emp.id,
        resign_date=date(2026, 6, 15),
        resign_reason="test",
        operator_user_id=user.id,
    )
    assert result["employee_id"] == emp.id
    assert result["resign_date"] == date(2026, 6, 15)
    assert isinstance(result["steps"], list)

    from models.offboarding import EmployeeOffboardingRecord

    record = (
        db_session.query(EmployeeOffboardingRecord)
        .filter_by(employee_id=emp.id)
        .first()
    )
    assert record is not None
    assert record.opened_by_user_id == user.id


def test_happy_path_all_6_steps_complete(
    db_session,
    employee_factory,
    user_factory,
    leave_quota_factory,
    salary_record_factory,
    tmp_path,
    monkeypatch,
):
    # monkeypatch generate_certificate STORAGE_DIR → tmp_path 避免寫到真實磁碟
    import services.offboarding.steps.generate_certificate as gc_mod

    monkeypatch.setattr(gc_mod, "STORAGE_DIR", tmp_path)

    emp = employee_factory(daily_wage=1800)
    admin = user_factory()
    user_factory(employee_id=emp.id, is_active=True, token_version=1)
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=112
    )
    salary_record_factory(
        employee_id=emp.id, salary_year=2026, salary_month=date.today().month
    )

    result = process_offboarding(
        session=db_session,
        employee_id=emp.id,
        resign_date=date.today(),
        resign_reason="個人因素",
        operator_user_id=admin.id,
    )
    db_session.commit()

    step_names = [s["step"] for s in result["steps"]]
    assert step_names == [
        "mark_appraisal",
        "snapshot_leave",
        "prefill_leave_payout",
        "revoke_user",
        "generate_certificate",
        "homeroom_reassignment_check",
    ]
    assert all(s["status"] == "completed" for s in result["steps"])
    assert result["user_account_revoked"] is True
    assert result["is_active_after"] is False
    assert result["certificate_pdf_path"] is not None


def test_snapshot_leave_failure_rolls_back_record(
    db_session, employee_factory, user_factory
):
    """員工無 daily_wage → snapshot_leave 422 → record + Employee 寫入全 rollback"""
    emp = employee_factory(daily_wage=None, monthly_salary=None)
    admin = user_factory()
    emp_id = emp.id  # 記住 id，rollback 後物件 detached 仍可 query

    # 必須建 quota 才能觸發 daily_wage 檢查（quota=0 時不觸發 payout 計算路徑）
    # 直接用 LeaveQuota 建（不用 fixture，fixture 未傳入此 test）
    from models.leave import LeaveQuota

    quota = LeaveQuota(
        employee_id=emp.id,
        year=2026,
        leave_type="annual",
        total_hours=80,
    )
    db_session.add(quota)
    # commit 確保 emp + admin + quota 已持久化，process_offboarding 的寫入才能被 rollback
    db_session.commit()

    with pytest.raises(OffboardingError) as exc:
        process_offboarding(
            session=db_session,
            employee_id=emp_id,
            resign_date=date.today(),
            resign_reason="test",
            operator_user_id=admin.id,
        )
    db_session.rollback()
    assert exc.value.code == "LEAVE_BALANCE_NOT_FOUND"

    # rollback 後 record 不存在
    from models.offboarding import EmployeeOffboardingRecord

    assert (
        db_session.query(EmployeeOffboardingRecord)
        .filter_by(employee_id=emp_id)
        .first()
        is None
    )
    # rollback 後以 id 重新 query，驗 Employee 狀態已復原
    emp_fresh = db_session.query(Employee).filter_by(id=emp_id).first()
    assert emp_fresh is not None
    assert emp_fresh.resign_date is None
    assert emp_fresh.is_active is True


def test_duplicate_offboarding_raises_already_offboarded(
    db_session,
    employee_factory,
    user_factory,
    leave_quota_factory,
):
    emp = employee_factory(daily_wage=1800)
    admin = user_factory()
    leave_quota_factory(
        employee_id=emp.id, year=2026, leave_type="annual", total_hours=80
    )
    process_offboarding(
        session=db_session,
        employee_id=emp.id,
        resign_date=date.today(),
        resign_reason="first",
        operator_user_id=admin.id,
    )
    db_session.commit()

    with pytest.raises(OffboardingError) as exc:
        process_offboarding(
            session=db_session,
            employee_id=emp.id,
            resign_date=date.today() + timedelta(days=1),
            resign_reason="second",
            operator_user_id=admin.id,
        )
    assert exc.value.code == "ALREADY_OFFBOARDED"


def test_process_offboarding_blocks_self(db_session, employee_factory, user_factory):
    """R6-5：操作者不可離職自己（會 is_active=False+token bump 自我登出鎖死）→
    CANNOT_OFFBOARD_SELF。"""
    from datetime import date

    emp = employee_factory()
    operator = user_factory(role="admin", employee_id=emp.id)
    with pytest.raises(OffboardingError) as exc:
        process_offboarding(
            session=db_session,
            employee_id=emp.id,
            resign_date=date(2026, 6, 30),
            resign_reason=None,
            operator_user_id=operator.id,
        )
    assert exc.value.code == "CANNOT_OFFBOARD_SELF"

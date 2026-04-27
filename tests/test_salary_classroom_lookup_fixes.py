"""薪資引擎與班級指派同步：2026-04-27 回歸測試。

對應 fix list：
  #1 薪資引擎不再依賴 Employee.classroom_id；改為依 Classroom 表反查 +
     api/classrooms.py 在指派老師時同步 Employee.classroom_id。
  #2 calculate_period_accrual_row 的 overtime_bonus 走 _calculate_classroom_bonus_result
     （含共用副班導加權平均），與 calculate_salary 路徑一致。
  #3 _compute_period_accrual_totals 對每個 (y, m) 依該月份解析班級當期，
     避免跨學期回算用錯班級。
  #4 api/salary.py simulate_salary 在發放月時帶入 period accrual override，
     以與正式落帳口徑一致。
  #5 AttendancePolicy / BonusConfig 載入時依 id desc 取最新 active。
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import (
    AttendancePolicy,
    Base,
    BonusConfig,
    ClassGrade,
    Classroom,
    Employee,
    Student,
)
from services.salary_engine import SalaryEngine
from utils.academic import resolve_current_academic_term


@pytest.fixture
def db_session(tmp_path):
    """In-memory sqlite + Base.create_all，回傳 session factory。"""
    db_path = tmp_path / "salary-fixes-test.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    yield session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def salary_engine_no_db():
    return SalaryEngine(load_from_db=False)


def _current_term() -> tuple[int, int]:
    return resolve_current_academic_term()


def _make_grade(session, name="大班"):
    g = ClassGrade(name=name)
    session.add(g)
    session.flush()
    return g


def _make_employee(
    session,
    *,
    employee_id: str,
    name: str,
    title="幼兒園教師",
    position="幼兒園教師",
    base_salary=35000,
    hire_date=date(2024, 1, 1),
):
    emp = Employee(
        employee_id=employee_id,
        name=name,
        title=title,
        position=position,
        base_salary=base_salary,
        hire_date=hire_date,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_classroom(
    session,
    *,
    name: str,
    grade,
    head_teacher_id=None,
    assistant_teacher_id=0,
    art_teacher_id=None,
    school_year=None,
    semester=None,
    is_active=True,
):
    sy, sem = _current_term() if school_year is None else (school_year, semester)
    cls = Classroom(
        name=name,
        grade_id=grade.id,
        head_teacher_id=head_teacher_id,
        assistant_teacher_id=assistant_teacher_id,
        art_teacher_id=art_teacher_id,
        school_year=sy,
        semester=sem,
        is_active=is_active,
    )
    session.add(cls)
    session.flush()
    return cls


def _add_students(session, classroom, count: int, prefix="S"):
    for i in range(count):
        session.add(
            Student(
                student_id=f"{prefix}{classroom.id:02d}{i:03d}",
                name=f"學生{i}",
                classroom_id=classroom.id,
                enrollment_date=date(2024, 1, 1),
                is_active=True,
            )
        )
    session.flush()


# ─── Fix #1：薪資引擎反查不依賴 Employee.classroom_id ─────────────────────


class TestEngineReverseLookup:
    def test_resolve_in_term_picks_head_over_assistant(
        self, db_session, salary_engine_no_db
    ):
        """同一員工同時是 A 班 head_teacher 與 B 班 assistant_teacher：取 A 班。"""
        with db_session() as session:
            grade = _make_grade(session)
            teacher = _make_employee(session, employee_id="T1", name="王老師")
            cls_head = _make_classroom(
                session, name="A 班", grade=grade, head_teacher_id=teacher.id
            )
            _make_classroom(
                session,
                name="B 班",
                grade=grade,
                assistant_teacher_id=teacher.id,
            )
            session.commit()

            today = date.today()
            picked = salary_engine_no_db._resolve_classroom_for_employee_in_month(
                session, teacher.id, today.year, today.month
            )

        assert picked is not None
        assert picked.id == cls_head.id

    def test_resolve_works_when_classroom_id_is_stale(
        self, db_session, salary_engine_no_db
    ):
        """Employee.classroom_id 為 None 但實際是班導：仍能反查到。

        對應老 bug：班級頁面更新 head_teacher 但沒同步 Employee.classroom_id，
        導致薪資全面歸 0。新引擎不再讀 emp.classroom_id 應免疫此情境。
        """
        with db_session() as session:
            grade = _make_grade(session)
            teacher = _make_employee(session, employee_id="T2", name="李老師")
            cls = _make_classroom(
                session, name="C 班", grade=grade, head_teacher_id=teacher.id
            )
            # 模擬 Employee.classroom_id 沒同步（保持 None）
            assert teacher.classroom_id is None
            session.commit()

            today = date.today()
            picked = salary_engine_no_db._resolve_classroom_for_employee_in_month(
                session, teacher.id, today.year, today.month
            )

        assert picked is not None
        assert picked.id == cls.id


# ─── Fix #1：api/classrooms.py 同步 Employee.classroom_id ──────────────────


class TestClassroomSyncHelper:
    def test_sync_sets_classroom_id_for_assigned_head_teacher(self, db_session):
        from api.classrooms import _sync_employee_classroom_id

        with db_session() as session:
            grade = _make_grade(session)
            teacher = _make_employee(session, employee_id="T3", name="陳老師")
            cls = _make_classroom(
                session, name="D 班", grade=grade, head_teacher_id=teacher.id
            )
            assert teacher.classroom_id is None
            session.commit()
            cls_id = cls.id
            teacher_id = teacher.id

            _sync_employee_classroom_id(session, [teacher_id])
            session.commit()
            session.refresh(teacher)
            actual = teacher.classroom_id

        assert actual == cls_id

    def test_sync_clears_classroom_id_when_unassigned(self, db_session):
        from api.classrooms import _sync_employee_classroom_id

        with db_session() as session:
            grade = _make_grade(session)
            teacher = _make_employee(session, employee_id="T4", name="林老師")
            cls = _make_classroom(
                session, name="E 班", grade=grade, head_teacher_id=teacher.id
            )
            teacher.classroom_id = cls.id
            session.commit()
            teacher_id = teacher.id

            # 取消指派
            cls.head_teacher_id = None
            session.flush()
            _sync_employee_classroom_id(session, [teacher_id])
            session.commit()
            session.refresh(teacher)
            actual = teacher.classroom_id

        assert actual is None

    def test_sync_prefers_head_role_over_assistant(self, db_session):
        from api.classrooms import _sync_employee_classroom_id

        with db_session() as session:
            grade = _make_grade(session)
            teacher = _make_employee(session, employee_id="T5", name="吳老師")
            cls_head = _make_classroom(
                session, name="F 班", grade=grade, head_teacher_id=teacher.id
            )
            _make_classroom(
                session,
                name="G 班",
                grade=grade,
                assistant_teacher_id=teacher.id,
            )
            session.commit()
            cls_head_id = cls_head.id
            teacher_id = teacher.id

            _sync_employee_classroom_id(session, [teacher_id])
            session.commit()
            session.refresh(teacher)
            actual = teacher.classroom_id

        assert actual == cls_head_id


# ─── Fix #2：共用副班導 overtime 走加權平均 ────────────────────────────────


class TestSharedAssistantOvertimeWeighting:
    def test_period_accrual_overtime_matches_breakdown_path(
        self, db_session, salary_engine_no_db
    ):
        """共用副班導：calculate_period_accrual_row 的 overtime_bonus 必須等於
        _calculate_classroom_bonus_result 的回傳值（含跨班加權平均），不可
        只用主班單班公式。
        """
        with db_session() as session:
            grade = _make_grade(session)
            assistant = _make_employee(
                session, employee_id="A1", name="副班導 A", title="助理教保員"
            )
            head_a = _make_employee(session, employee_id="HA", name="A 班導")
            head_b = _make_employee(session, employee_id="HB", name="B 班導")
            cls_a = _make_classroom(
                session,
                name="共用 A",
                grade=grade,
                head_teacher_id=head_a.id,
                assistant_teacher_id=assistant.id,
            )
            cls_b = _make_classroom(
                session,
                name="共用 B",
                grade=grade,
                head_teacher_id=head_b.id,
                assistant_teacher_id=assistant.id,
            )
            _add_students(session, cls_a, 20, prefix="A")
            _add_students(session, cls_b, 25, prefix="B")
            session.commit()

            today = date.today()
            row = salary_engine_no_db.calculate_period_accrual_row(
                assistant.id, today.year, today.month
            )

            # 對照組：直接以 _build_classroom_context_from_db + _calculate_classroom_bonus_result
            # 計算「應有的 overtime_bonus」。
            cls_ctx = salary_engine_no_db._build_classroom_context_from_db(
                session, cls_a, assistant.id, reference_date=today
            )
            assert cls_ctx is not None
            assert cls_ctx["is_shared_assistant"] is True
            expected = salary_engine_no_db._calculate_classroom_bonus_result(
                "助理教保員", cls_ctx
            )

        expected_overtime = int(expected.get("overtime_bonus") or 0)
        # 必須非 0：避免「兩邊都壞掉變 0」也通過的退化測試
        assert (
            expected_overtime > 0
        ), "測試 fixture 應產生非 0 overtime；請檢查 BonusConfig/grade 設定"
        assert row["overtime_bonus"] == expected_overtime


# ─── Fix #3：期間累積依年月解析班級當期 ────────────────────────────────────


class TestPeriodAccrualTermResolution:
    def test_resolve_picks_correct_term_when_multiple_terms_exist(
        self, db_session, salary_engine_no_db
    ):
        """同一員工在 (學年X, 上學期) 與 (學年X, 下學期) 各有一班：
        對應月份必須回到對應學期的班級，而非任意一個。
        """
        with db_session() as session:
            grade = _make_grade(session)
            teacher = _make_employee(session, employee_id="T6", name="許老師")
            cls_sem1 = _make_classroom(
                session,
                name="上學期班",
                grade=grade,
                head_teacher_id=teacher.id,
                school_year=114,
                semester=1,
            )
            cls_sem2 = _make_classroom(
                session,
                name="下學期班",
                grade=grade,
                head_teacher_id=teacher.id,
                school_year=114,
                semester=2,
            )
            session.commit()

            # 學年 114 上學期涵蓋月份：8-1 月（民國 114 = 西元 2025）
            picked_sem1 = salary_engine_no_db._resolve_classroom_for_employee_in_term(
                session, teacher.id, 114, 1
            )
            picked_sem2 = salary_engine_no_db._resolve_classroom_for_employee_in_term(
                session, teacher.id, 114, 2
            )

        assert picked_sem1.id == cls_sem1.id
        assert picked_sem2.id == cls_sem2.id

    def test_fallback_when_term_has_no_match(self, db_session, salary_engine_no_db):
        """員工只在學年 X 下學期有班級紀錄；查上學期應 fallback 至下學期。

        合理場景：學校未替每學期建立獨立 Classroom，沿用單筆紀錄。
        """
        with db_session() as session:
            grade = _make_grade(session)
            teacher = _make_employee(session, employee_id="T7", name="周老師")
            sole_cls = _make_classroom(
                session,
                name="唯一班",
                grade=grade,
                head_teacher_id=teacher.id,
                school_year=114,
                semester=2,
            )
            session.commit()

            picked = salary_engine_no_db._resolve_classroom_for_employee_in_term(
                session, teacher.id, 114, 1
            )

        assert picked is not None
        assert picked.id == sole_cls.id


# ─── Fix #4：simulate_salary 接發放期累積獎金 ──────────────────────────────


class TestSimulateUsesPeriodAccrualOverride:
    def test_simulate_passes_period_overrides_to_calculate_salary(
        self, db_session, monkeypatch
    ):
        """發放月（2/6/9/12）試算必須帶 period_festival_override / period_overtime_override
        給 engine.calculate_salary，否則 simulated 是「單月」而 actual 是「期間累積」。
        """
        from unittest.mock import MagicMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import api.salary as salary_module
        from api.auth import (
            router as auth_router,
            _account_failures,
            _ip_attempts,
        )
        from api.salary import router as salary_router
        from models.database import User
        from services.salary_engine import SalaryEngine
        from utils.auth import hash_password
        from utils.permissions import Permission

        with db_session() as session:
            grade = _make_grade(session)
            teacher = _make_employee(session, employee_id="SIM1", name="試算老師")
            cls = _make_classroom(
                session, name="試算班", grade=grade, head_teacher_id=teacher.id
            )
            _add_students(session, cls, 20, prefix="SIM")
            session.add(
                User(
                    username="admin",
                    password_hash=hash_password("pw"),
                    role="admin",
                    permissions=int(Permission.SALARY_READ),
                    is_active=True,
                    must_change_password=False,
                )
            )
            teacher_id = teacher.id
            session.commit()

        _ip_attempts.clear()
        _account_failures.clear()
        engine = SalaryEngine(load_from_db=False)
        salary_module.init_salary_services(engine, MagicMock())

        # 攔截 engine.calculate_salary，捕捉收到的 override；同時讓
        # _compute_period_accrual_totals 回傳已知值，驗證 simulate 確實 plumb 過去。
        captured: dict = {}

        original_calc = engine.calculate_salary

        def spy_calc(**kwargs):
            captured["period_festival_override"] = kwargs.get(
                "period_festival_override"
            )
            captured["period_overtime_override"] = kwargs.get(
                "period_overtime_override"
            )
            return original_calc(**kwargs)

        monkeypatch.setattr(engine, "calculate_salary", spy_calc)
        monkeypatch.setattr(
            engine,
            "_compute_period_accrual_totals",
            lambda *a, **kw: (12345, 6789),
        )

        app = FastAPI()
        app.include_router(auth_router)
        app.include_router(salary_router)
        with TestClient(app) as c:
            r = c.post(
                "/api/auth/login",
                json={"username": "admin", "password": "pw"},
            )
            assert r.status_code == 200, r.text

            # 6 月為發放月：必須走 period override 路徑
            r = c.post(
                "/api/salaries/simulate",
                json={
                    "employee_id": teacher_id,
                    "year": 2026,
                    "month": 6,
                    "overrides": {},
                },
            )
            assert r.status_code == 200, r.text

        assert captured["period_festival_override"] == 12345
        assert captured["period_overtime_override"] == 6789
        _ip_attempts.clear()
        _account_failures.clear()


# ─── Fix #5：AttendancePolicy / BonusConfig 取最新 active id ───────────────


class TestActiveConfigSelection:
    def test_load_attendance_policy_picks_latest_id(self, db_session):
        """同時存在多筆 is_active=True 時，必須取 id 最大的（最新版本）。"""
        with db_session() as session:
            old = AttendancePolicy(
                version=1,
                festival_bonus_months="2,6,9,12",
                is_active=True,
            )
            new = AttendancePolicy(
                version=2,
                festival_bonus_months="2,6,9",
                is_active=True,
            )
            session.add(old)
            session.add(new)
            session.commit()
            old_id, new_id = old.id, new.id
            assert new_id > old_id

        engine = SalaryEngine(load_from_db=True)

        assert engine._attendance_policy_id == new_id

    def test_load_bonus_config_picks_latest_id(self, db_session):
        with db_session() as session:
            session.add(
                BonusConfig(
                    config_year=2025,
                    head_teacher_ab=1000,
                    head_teacher_c=1000,
                    assistant_teacher_ab=900,
                    assistant_teacher_c=900,
                    is_active=True,
                    version=1,
                )
            )
            session.add(
                BonusConfig(
                    config_year=2026,
                    head_teacher_ab=2000,
                    head_teacher_c=2000,
                    assistant_teacher_ab=1800,
                    assistant_teacher_c=1800,
                    is_active=True,
                    version=2,
                )
            )
            session.commit()
            latest_id = (
                session.query(BonusConfig)
                .filter(BonusConfig.is_active == True)
                .order_by(BonusConfig.id.desc())
                .first()
                .id
            )

        engine = SalaryEngine(load_from_db=True)
        assert engine._bonus_config_id == latest_id
        # 也驗證實際載入的是最新版本的 base
        assert engine._bonus_base["head_teacher"]["A"] == 2000

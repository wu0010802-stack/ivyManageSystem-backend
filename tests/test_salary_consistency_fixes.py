"""薪資一致性修補回歸測試(2026-04-28)。

涵蓋以下 6 項缺陷修補:
1. PUT /api/config/bonus 與 /api/config/attendance-policy 改版後,既有
   未封存且非新版本計算的薪資應被標 needs_recalc=True
2. SalaryEngine.config_for_month 應載入該月份對應的歷史版本(以 created_at)
3. (TOCTOU) finalize 取鎖後 refresh + 重檢 stale,避免 lock 前後旗標分叉
4. /api/attendance/record CRUD 異動會標 stale
5. mark_salary_stale 排除已封存 record;batch_confirm_anomalies 加封存守衛
6. _load_manual_salary_fields 把既有 performance_bonus / special_bonus
   塞回 emp_dict,重算不會清掉 HR 手動加的獎金
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.salary as salary_module
import api.config as config_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.salary import router as salary_router
from api.config import router as config_router
from api.attendance import router as attendance_router
import api.attendance.records as att_records_module  # noqa: F401  ensure registered
import api.attendance.anomalies as att_anomalies_module  # noqa: F401
from models.database import (
    Base,
    Employee,
    User,
    SalaryRecord,
    Attendance,
    AttendancePolicy,
    BonusConfig as DBBonusConfig,
    GradeTarget,
    InsuranceRate,
)
from utils.auth import hash_password


@pytest.fixture
def consistency_client(tmp_path):
    db_path = tmp_path / "consistency.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    fake_engine = MagicMock()
    salary_module.init_salary_services(fake_engine, MagicMock())
    salary_module._snapshot_lazy_guard.clear()
    config_module.init_config_services(fake_engine, MagicMock())

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(salary_router)
    app.include_router(config_router)
    app.include_router(attendance_router)

    with TestClient(app) as client:
        yield client, session_factory, fake_engine

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _login_admin(client, sf, username="admin", password="AdminPass123"):
    with sf() as session:
        session.add(
            User(
                employee_id=None,
                username=username,
                password_hash=hash_password(password),
                role="admin",
                permissions=-1,
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


def _seed_employee(sf, name="員工A", emp_no="E001"):
    with sf() as session:
        emp = Employee(
            employee_id=emp_no,
            name=name,
            base_salary=30000,
            employee_type="regular",
            is_active=True,
            hire_date=date(2025, 1, 1),
        )
        session.add(emp)
        session.commit()
        return emp.id


def _seed_record(
    sf,
    emp_id,
    year=2026,
    month=3,
    *,
    needs_recalc=False,
    is_finalized=False,
    bonus_config_id=None,
    attendance_policy_id=None,
    performance_bonus=0,
    special_bonus=0,
):
    with sf() as session:
        rec = SalaryRecord(
            employee_id=emp_id,
            salary_year=year,
            salary_month=month,
            base_salary=30000,
            gross_salary=30000,
            net_salary=28000,
            total_deduction=2000,
            performance_bonus=performance_bonus,
            special_bonus=special_bonus,
            needs_recalc=needs_recalc,
            is_finalized=is_finalized,
            bonus_config_id=bonus_config_id,
            attendance_policy_id=attendance_policy_id,
        )
        session.add(rec)
        session.commit()
        return rec.id


# ─────────────────────────────────────────────────────────────────────────────
# 問題 5:mark_salary_stale 排除 finalized + batch_confirm 加封存守衛
# ─────────────────────────────────────────────────────────────────────────────


class TestMarkStaleExcludesFinalized:
    def test_finalized_record_not_marked_stale(self, consistency_client):
        _, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "甲")
        _seed_record(sf, emp_id, is_finalized=True, needs_recalc=False)

        from services.salary.utils import mark_salary_stale

        with sf() as session:
            updated = mark_salary_stale(session, emp_id, 2026, 3)
            session.commit()
        assert updated is False

        with sf() as session:
            rec = (
                session.query(SalaryRecord)
                .filter_by(employee_id=emp_id, salary_year=2026, salary_month=3)
                .one()
            )
            assert rec.needs_recalc is False
            assert rec.is_finalized is True

    def test_unfinalized_record_marked_stale(self, consistency_client):
        _, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "乙")
        _seed_record(sf, emp_id, is_finalized=False, needs_recalc=False)

        from services.salary.utils import mark_salary_stale

        with sf() as session:
            updated = mark_salary_stale(session, emp_id, 2026, 3)
            session.commit()
        assert updated is True


class TestAnomaliesBatchConfirmFinalizeGuard:
    def _seed_anomaly(self, sf, emp_id, year=2026, month=3, day=10):
        with sf() as session:
            att = Attendance(
                employee_id=emp_id,
                attendance_date=date(year, month, day),
                punch_in_time=datetime(year, month, day, 8, 30),
                punch_out_time=datetime(year, month, day, 17, 0),
                is_late=True,
                late_minutes=30,
                status="late",
            )
            session.add(att)
            session.commit()
            return att.id

    def test_batch_confirm_blocked_when_target_month_finalized(
        self, consistency_client
    ):
        client, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "員工Z", "Z001")
        att_id = self._seed_anomaly(sf, emp_id)
        _seed_record(sf, emp_id, is_finalized=True, needs_recalc=False)
        _login_admin(client, sf)

        res = client.post(
            "/api/attendance/anomalies/batch-confirm",
            json={"attendance_ids": [att_id], "action": "admin_waive"},
        )
        assert res.status_code == 409, res.text
        assert "封存" in res.json()["detail"]


# ─────────────────────────────────────────────────────────────────────────────
# 問題 4:attendance CRUD 標 stale
# ─────────────────────────────────────────────────────────────────────────────


class TestAttendanceCrudMarksStale:
    def test_create_record_marks_existing_salary_stale(self, consistency_client):
        client, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "員工C", "C001")
        rec_id = _seed_record(sf, emp_id, year=2026, month=3, needs_recalc=False)
        _login_admin(client, sf)

        res = client.post(
            "/api/attendance/record",
            json={
                "employee_id": emp_id,
                "date": "2026-03-15",
                "punch_in": "08:00",
                "punch_out": "17:00",
            },
        )
        assert res.status_code == 201, res.text

        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            assert rec.needs_recalc is True

    def test_delete_single_record_marks_stale(self, consistency_client):
        """5 年保存期外的考勤可刪除;刪除後對應月薪資 needs_recalc 應被設 True。"""
        client, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "員工D", "D001")
        # 2020/03/12 已超過 5 年保存期(以 2026-04-28 為 today)
        old_date = date(2020, 3, 12)
        with sf() as session:
            att = Attendance(
                employee_id=emp_id,
                attendance_date=old_date,
                status="normal",
            )
            session.add(att)
            session.commit()
        rec_id = _seed_record(
            sf, emp_id, year=old_date.year, month=old_date.month, needs_recalc=False
        )
        _login_admin(client, sf)

        res = client.delete(f"/api/attendance/record/{emp_id}/{old_date.isoformat()}")
        assert res.status_code == 200, res.text
        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            assert rec.needs_recalc is True


# ─────────────────────────────────────────────────────────────────────────────
# 問題 1:config 改版後標既有 record stale
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigUpdateMarksStale:
    def test_update_attendance_policy_marks_old_records_stale(self, consistency_client):
        client, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "員工E", "E001")

        # 先建立既有 policy v1
        with sf() as session:
            old_policy = AttendancePolicy(is_active=True, version=1)
            session.add(old_policy)
            session.commit()
            old_id = old_policy.id

        # 建一筆綁舊版 policy 的未封存 record
        rec_id = _seed_record(
            sf,
            emp_id,
            year=2026,
            month=3,
            needs_recalc=False,
            attendance_policy_id=old_id,
        )
        # 另一筆已封存的不該被改
        finalized_rec_id = _seed_record(
            sf,
            _seed_employee(sf, "員工E2", "E002"),
            year=2026,
            month=2,
            needs_recalc=False,
            is_finalized=True,
            attendance_policy_id=old_id,
        )

        _login_admin(client, sf)
        res = client.put(
            "/api/config/attendance-policy",
            json={"festival_bonus_months": 2},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["salary_records_marked_stale"] >= 1

        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            assert rec.needs_recalc is True
            fr = session.query(SalaryRecord).filter_by(id=finalized_rec_id).one()
            assert fr.needs_recalc is False
            assert fr.is_finalized is True

    def test_update_bonus_marks_old_records_stale(self, consistency_client):
        client, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "員工F", "F001")

        with sf() as session:
            old_bonus = DBBonusConfig(
                is_active=True, version=1, config_year=2025, head_teacher_ab=2000
            )
            session.add(old_bonus)
            session.commit()
            old_id = old_bonus.id

        rec_id = _seed_record(
            sf,
            emp_id,
            year=2026,
            month=3,
            needs_recalc=False,
            bonus_config_id=old_id,
        )

        _login_admin(client, sf)
        res = client.put(
            "/api/config/bonus",
            json={"head_teacher_ab": 2500, "config_year": 2026},
        )
        assert res.status_code == 200, res.text
        assert res.json()["salary_records_marked_stale"] >= 1

        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            assert rec.needs_recalc is True


class TestUpdateGradeTargetMarksStale:
    """PUT /api/config/grade-targets 不升 bonus_config 版本,直接 mutate 現役 row。
    指到該 bonus_config 且未封存的薪資需被標 needs_recalc,封存的維持鎖定。
    """

    def test_update_grade_target_marks_active_unfinalized_stale(
        self, consistency_client
    ):
        client, sf, _ = consistency_client
        emp_unfin = _seed_employee(sf, "員工GT1", "GT001")
        emp_fin = _seed_employee(sf, "員工GT2", "GT002")

        with sf() as session:
            active_bonus = DBBonusConfig(
                is_active=True, version=1, config_year=2026, head_teacher_ab=1000
            )
            session.add(active_bonus)
            session.commit()
            active_id = active_bonus.id
            session.add(
                GradeTarget(
                    config_year=2026,
                    grade_name="小班",
                    bonus_config_id=active_id,
                    festival_two_teachers=10,
                    festival_one_teacher=8,
                    festival_shared=5,
                    overtime_two_teachers=12,
                    overtime_one_teacher=10,
                    overtime_shared=6,
                )
            )
            session.commit()

        rec_unfin = _seed_record(
            sf,
            emp_unfin,
            year=2026,
            month=3,
            needs_recalc=False,
            bonus_config_id=active_id,
        )
        rec_fin = _seed_record(
            sf,
            emp_fin,
            year=2026,
            month=2,
            needs_recalc=False,
            is_finalized=True,
            bonus_config_id=active_id,
        )

        _login_admin(client, sf)
        res = client.put(
            "/api/config/grade-targets",
            json={
                "grade_name": "小班",
                "festival_two_teachers": 15,
                "festival_one_teacher": 12,
                "festival_shared": 7,
                "overtime_two_teachers": 18,
                "overtime_one_teacher": 14,
                "overtime_shared": 9,
            },
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["salary_records_marked_stale"] >= 1

        with sf() as session:
            unfin = session.query(SalaryRecord).filter_by(id=rec_unfin).one()
            assert unfin.needs_recalc is True
            fin = session.query(SalaryRecord).filter_by(id=rec_fin).one()
            assert fin.needs_recalc is False
            assert fin.is_finalized is True

    def test_update_grade_target_does_not_mark_other_bonus_config(
        self, consistency_client
    ):
        """指到非現役 bonus_config 的薪資不應被標 stale(避免誤傷其他版本)。"""
        client, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "員工GT3", "GT003")

        with sf() as session:
            old_bonus = DBBonusConfig(
                is_active=False, version=1, config_year=2025, head_teacher_ab=900
            )
            active_bonus = DBBonusConfig(
                is_active=True, version=2, config_year=2026, head_teacher_ab=1000
            )
            session.add_all([old_bonus, active_bonus])
            session.commit()
            old_id = old_bonus.id
            active_id = active_bonus.id
            session.add(
                GradeTarget(
                    config_year=2026,
                    grade_name="中班",
                    bonus_config_id=active_id,
                    festival_two_teachers=10,
                )
            )
            session.commit()

        rec_old_ver = _seed_record(
            sf,
            emp_id,
            year=2026,
            month=3,
            needs_recalc=False,
            bonus_config_id=old_id,
        )

        _login_admin(client, sf)
        res = client.put(
            "/api/config/grade-targets",
            json={"grade_name": "中班", "festival_two_teachers": 20},
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_old_ver).one()
            assert rec.needs_recalc is False


# ─────────────────────────────────────────────────────────────────────────────
# 問題 6:_load_manual_salary_fields 保留手動欄位
# ─────────────────────────────────────────────────────────────────────────────


class TestManualBonusPreserved:
    def test_load_manual_fields_returns_existing_values(self, consistency_client):
        _, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "員工G", "G001")
        _seed_record(
            sf,
            emp_id,
            year=2026,
            month=3,
            needs_recalc=False,
            performance_bonus=5000,
            special_bonus=2000,
        )

        from services.salary.engine import SalaryEngine

        engine = SalaryEngine(load_from_db=False)
        with sf() as session:
            fields = engine._load_manual_salary_fields(session, emp_id, 2026, 3)
        assert fields["performance_bonus"] == 5000
        assert fields["special_bonus"] == 2000

    def test_load_manual_fields_zero_when_no_record(self, consistency_client):
        _, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "員工H", "H001")

        from services.salary.engine import SalaryEngine

        engine = SalaryEngine(load_from_db=False)
        with sf() as session:
            fields = engine._load_manual_salary_fields(session, emp_id, 2026, 3)
        assert fields["performance_bonus"] == 0
        assert fields["special_bonus"] == 0


class TestManualOverridesPreservedAcrossRecalc:
    """Issue 6 修補:manual_adjust 寫過的欄位透過 manual_overrides 清單,
    在後續上游事件觸發的重算(_fill_salary_record)中不被覆寫。"""

    def _make_breakdown(self, **overrides):
        from services.salary.breakdown import SalaryBreakdown

        defaults = dict(
            employee_name="X",
            employee_id="X001",
            year=2026,
            month=3,
            base_salary=30000,
            festival_bonus=2000,
            overtime_bonus=1500,
            performance_bonus=0,
            special_bonus=0,
            supervisor_dividend=0,
            overtime_work_pay=500,
            meeting_overtime_pay=0,
            birthday_bonus=0,
            hourly_total=0,
            labor_insurance=300,
            health_insurance=200,
            pension_self=0,
            labor_insurance_employer=600,
            health_insurance_employer=400,
            pension_employer=1800,
            late_deduction=100,
            early_leave_deduction=0,
            missing_punch_deduction=0,
            leave_deduction=0,
            absence_deduction=0,
            meeting_absence_deduction=0,
            other_deduction=0,
            gross_salary=30500,
            total_deduction=600,
            net_salary=29900,
            bonus_separate=True,
            bonus_amount=3500,
        )
        defaults.update(overrides)
        return SalaryBreakdown(**defaults)

    def _fake_engine(self, bonus_config_id=None, attendance_policy_id=None):
        from unittest.mock import MagicMock

        eng = MagicMock()
        eng._bonus_config_id = bonus_config_id
        eng._attendance_policy_id = attendance_policy_id
        return eng

    def test_fill_salary_record_skips_overridden_field(self, consistency_client):
        """manual_overrides 包含 festival_bonus → 重算後 record.festival_bonus 維持原值。"""
        from services.salary.engine import _fill_salary_record

        record = SalaryRecord(
            employee_id=1,
            salary_year=2026,
            salary_month=3,
            festival_bonus=9999,  # 人工調整值
            manual_overrides=["festival_bonus"],
        )
        breakdown = self._make_breakdown(festival_bonus=2000)

        _fill_salary_record(record, breakdown, self._fake_engine())

        assert record.festival_bonus == 9999, "override 欄位不該被 breakdown 覆寫"
        # 其他非 override 欄位仍會被覆寫
        assert record.overtime_bonus == 1500
        assert record.base_salary == 30000

    def test_fill_salary_record_overwrites_non_overridden_field(self):
        """manual_overrides 不含 overtime_bonus → 該欄位仍會被 breakdown 覆寫。"""
        from services.salary.engine import _fill_salary_record

        record = SalaryRecord(
            employee_id=1,
            salary_year=2026,
            salary_month=3,
            overtime_bonus=8000,
            manual_overrides=["festival_bonus"],
        )
        breakdown = self._make_breakdown(overtime_bonus=1500)

        _fill_salary_record(record, breakdown, self._fake_engine())

        assert record.overtime_bonus == 1500

    def test_fill_salary_record_recomputes_totals_when_override_exists(self):
        """有 override 時,gross/total/net 應從 record 重算,反映保留的人工值。"""
        from services.salary.engine import _fill_salary_record

        record = SalaryRecord(
            employee_id=1,
            salary_year=2026,
            salary_month=3,
            performance_bonus=10000,  # 人工加的
            manual_overrides=["performance_bonus"],
        )
        # breakdown 算出來 performance_bonus=0、gross=30500
        breakdown = self._make_breakdown(performance_bonus=0)

        _fill_salary_record(record, breakdown, self._fake_engine())

        # performance_bonus 維持 10000
        assert record.performance_bonus == 10000
        # gross 應反映保留的 performance_bonus,而非沿用 breakdown.gross_salary=30500
        # gross = base(30000) + hourly_total(0) + perf(10000) + special(0) +
        #         supervisor_div(0) + meeting_ot(0) + birthday(0) + ot_pay(500) = 40500
        assert record.gross_salary == 40500
        # total = labor(300) + health(200) + pension(0) + late(100) = 600
        assert record.total_deduction == 600
        assert record.net_salary == 39900

    def test_fill_salary_record_no_override_uses_breakdown_totals(self):
        """無 override 時,維持原行為:gross/total/net 直接取 breakdown 值。"""
        from services.salary.engine import _fill_salary_record

        record = SalaryRecord(
            employee_id=1,
            salary_year=2026,
            salary_month=3,
            manual_overrides=[],
        )
        breakdown = self._make_breakdown(
            gross_salary=12345, total_deduction=500, net_salary=11845
        )

        _fill_salary_record(record, breakdown, self._fake_engine())

        assert record.gross_salary == 12345
        assert record.total_deduction == 500
        assert record.net_salary == 11845

    def test_fill_salary_record_handles_null_overrides(self):
        """既有資料 manual_overrides=None 應視為空清單,不報錯。"""
        from services.salary.engine import _fill_salary_record

        record = SalaryRecord(
            employee_id=1, salary_year=2026, salary_month=3, manual_overrides=None
        )
        breakdown = self._make_breakdown()

        _fill_salary_record(record, breakdown, self._fake_engine())

        assert record.festival_bonus == 2000
        assert record.gross_salary == 30500


class TestManualAdjustWritesOverrides:
    """manual_adjust_salary 將寫過的欄位累積至 record.manual_overrides。"""

    def _seed_record_for_adjust(self, sf):
        emp_id = _seed_employee(sf, "員工OV", "OV001")
        rec_id = _seed_record(sf, emp_id, year=2026, month=3, needs_recalc=False)
        return emp_id, rec_id

    def test_manual_adjust_records_field_in_overrides(self, consistency_client):
        client, sf, _ = consistency_client
        _emp_id, rec_id = self._seed_record_for_adjust(sf)
        _login_admin(client, sf)

        res = client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={
                "adjustment_reason": "測試 override 紀錄(績效)",
                "performance_bonus": 5000,
            },
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            assert rec.performance_bonus == 5000
            assert "performance_bonus" in (rec.manual_overrides or [])

    def test_manual_adjust_accumulates_overrides_across_calls(
        self, consistency_client
    ):
        client, sf, _ = consistency_client
        _emp_id, rec_id = self._seed_record_for_adjust(sf)
        _login_admin(client, sf)

        client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={"adjustment_reason": "第一次調整", "performance_bonus": 3000},
        )
        client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={"adjustment_reason": "第二次調整", "special_bonus": 2000},
        )

        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            overrides = set(rec.manual_overrides or [])
        assert {"performance_bonus", "special_bonus"} <= overrides

    def test_manual_adjust_meeting_absence_connect_marks_festival_override(
        self, consistency_client
    ):
        """改 meeting_absence_deduction 時連動寫的 festival_bonus 也視為 override。"""
        client, sf, _ = consistency_client
        emp_id = _seed_employee(sf, "員工OVF", "OVF001")
        with sf() as session:
            rec = SalaryRecord(
                employee_id=emp_id,
                salary_year=2026,
                salary_month=3,
                base_salary=30000,
                festival_bonus=1800,
                meeting_absence_deduction=200,
                gross_salary=30000,
                net_salary=30000,
                total_deduction=0,
            )
            session.add(rec)
            session.commit()
            rec_id = rec.id
        _login_admin(client, sf)

        res = client.put(
            f"/api/salaries/{rec_id}/manual-adjust",
            json={
                "adjustment_reason": "測試連動 override",
                "meeting_absence_deduction": 0,
            },
        )
        assert res.status_code == 200, res.text

        with sf() as session:
            rec = session.query(SalaryRecord).filter_by(id=rec_id).one()
            overrides = set(rec.manual_overrides or [])
        assert "meeting_absence_deduction" in overrides
        assert "festival_bonus" in overrides, "連動寫入的 festival_bonus 也應列入 override"


# ─────────────────────────────────────────────────────────────────────────────
# 問題 2:config_for_month 載入該月版本
# ─────────────────────────────────────────────────────────────────────────────


class TestConfigForMonth:
    def test_swap_uses_version_active_at_month_end(self, consistency_client):
        _, sf, _ = consistency_client

        # 建兩個版本:v1(2026-01-15)、v2(2026-04-20)
        with sf() as session:
            v1 = DBBonusConfig(
                is_active=False,
                version=1,
                config_year=2026,
                head_teacher_ab=1000,
                head_teacher_c=900,
                assistant_teacher_ab=800,
                assistant_teacher_c=700,
                principal_festival=5000,
                director_festival=3000,
                leader_festival=1500,
                driver_festival=800,
                designer_festival=900,
                admin_festival=1100,
                principal_dividend=4000,
                director_dividend=3500,
                leader_dividend=2500,
                vice_leader_dividend=1200,
                overtime_head_normal=300,
                overtime_head_baby=350,
                overtime_assistant_normal=80,
                overtime_assistant_baby=120,
                school_wide_target=140,
                created_at=datetime(2026, 1, 15),
            )
            session.add(v1)
            session.commit()
            v2 = DBBonusConfig(
                is_active=True,
                version=2,
                config_year=2026,
                head_teacher_ab=9999,  # 故意大數字
                head_teacher_c=8888,
                assistant_teacher_ab=7777,
                assistant_teacher_c=6666,
                principal_festival=5000,
                director_festival=3000,
                leader_festival=1500,
                driver_festival=800,
                designer_festival=900,
                admin_festival=1100,
                principal_dividend=4000,
                director_dividend=3500,
                leader_dividend=2500,
                vice_leader_dividend=1200,
                overtime_head_normal=300,
                overtime_head_baby=350,
                overtime_assistant_normal=80,
                overtime_assistant_baby=120,
                school_wide_target=180,
                created_at=datetime(2026, 4, 20),
            )
            session.add(v2)
            session.commit()
            v1_id = v1.id
            v2_id = v2.id

        from services.salary.engine import SalaryEngine

        engine = SalaryEngine(load_from_db=False)

        with sf() as session:
            # 重算 2026-02:應拿到 v1
            with engine.config_for_month(session, 2026, 2):
                assert engine._bonus_config_id == v1_id
                assert engine._bonus_base["head_teacher"]["A"] == 1000
                assert engine._school_wide_target == 140
            # 重算 2026-05:應拿到 v2
            with engine.config_for_month(session, 2026, 5):
                assert engine._bonus_config_id == v2_id
                assert engine._bonus_base["head_teacher"]["A"] == 9999
                assert engine._school_wide_target == 180

    def test_state_restored_after_context_exit(self, consistency_client):
        """離開 context 後 engine state 回到原值,即使 swap 過數次。"""
        _, sf, _ = consistency_client

        with sf() as session:
            old_b = DBBonusConfig(
                is_active=True,
                version=1,
                config_year=2026,
                head_teacher_ab=3333,
                head_teacher_c=3000,
                assistant_teacher_ab=2000,
                assistant_teacher_c=1800,
                principal_festival=5000,
                director_festival=3000,
                leader_festival=1500,
                driver_festival=800,
                designer_festival=900,
                admin_festival=1100,
                principal_dividend=4000,
                director_dividend=3500,
                leader_dividend=2500,
                vice_leader_dividend=1200,
                overtime_head_normal=300,
                overtime_head_baby=350,
                overtime_assistant_normal=80,
                overtime_assistant_baby=120,
                school_wide_target=160,
                created_at=datetime(2026, 1, 1),
            )
            session.add(old_b)
            session.commit()

        from services.salary.engine import SalaryEngine

        engine = SalaryEngine(load_from_db=False)
        # 以「原始預設值」為基準快照
        original_bonus_base = dict(engine._bonus_base["head_teacher"])

        with sf() as session:
            with engine.config_for_month(session, 2026, 6):
                # 已切換成 DB 版本
                assert engine._bonus_base["head_teacher"]["A"] == 3333
            # 離開 context 後恢復
            assert engine._bonus_base["head_teacher"] == original_bonus_base

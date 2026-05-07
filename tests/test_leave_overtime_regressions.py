"""請假與加班邏輯漏洞回歸測試。"""

import os
import sys
from datetime import date
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.overtimes as overtimes_module
import models.base as base_module
from api.auth import router as auth_router
from api.auth import _account_failures, _ip_attempts
from api.leaves import router as leaves_router
from api.overtimes import router as overtimes_router
from api.portal.leaves import router as portal_leaves_router
from models.database import (
    Base,
    Employee,
    Holiday,
    LeaveQuota,
    LeaveRecord,
    OvertimeRecord,
    User,
)
from utils.auth import hash_password


@pytest.fixture
def leave_overtime_client(tmp_path, monkeypatch):
    """建立隔離的 sqlite 測試 app。"""
    db_path = tmp_path / "leave-overtime-regressions.sqlite"
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

    fake_salary_engine = MagicMock()
    monkeypatch.setattr(overtimes_module, "_salary_engine", fake_salary_engine)

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(leaves_router)
    app.include_router(overtimes_router)
    app.include_router(portal_leaves_router, prefix="/api/portal")

    with TestClient(app) as client:
        yield client, session_factory, fake_salary_engine

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _create_employee(session, employee_id: str, name: str) -> Employee:
    employee = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=True,
    )
    session.add(employee)
    session.flush()
    return employee


def _create_user(
    session,
    *,
    username: str,
    password: str,
    role: str,
    permissions: int,
    employee: Employee | None = None,
) -> User:
    user = User(
        employee_id=employee.id if employee else None,
        username=username,
        password_hash=hash_password(password),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _login(client: TestClient, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


class TestPortalLeaveDeductionRatio:
    def test_portal_leave_persists_deduction_ratio_from_leave_type(
        self, leave_overtime_client
    ):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "T001", "教師甲")
            _create_user(
                session,
                username="teacher_portal",
                password="PortalPass123",
                role="teacher",
                permissions=0,
                employee=employee,
            )
            session.commit()

        login_res = _login(client, "teacher_portal", "PortalPass123")
        assert login_res.status_code == 200

        create_res = client.post(
            "/api/portal/my-leaves",
            json={
                "leave_type": "annual",
                "start_date": "2026-03-12",
                "end_date": "2026-03-12",
                "leave_hours": 8,
                "reason": "特休",
            },
        )
        assert create_res.status_code == 201

        with session_factory() as session:
            leave = session.query(LeaveRecord).one()
            assert leave.deduction_ratio == 0.0
            assert leave.is_deductible is False

    def test_leave_longer_than_two_days_requires_attachment_before_approval(
        self, leave_overtime_client
    ):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "T002", "教師乙")
            leave = LeaveRecord(
                employee_id=employee.id,
                leave_type="personal",
                start_date=date(2026, 3, 12),
                end_date=date(2026, 3, 14),
                leave_hours=24,
                is_approved=None,
            )
            session.add(leave)
            _create_user(
                session,
                username="admin_leave_approve",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            leave_id = leave.id

        login_res = _login(client, "admin_leave_approve", "AdminPass123")
        assert login_res.status_code == 200

        approve_res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": True},
        )
        assert approve_res.status_code == 400
        assert "超過 2 天" in approve_res.json()["detail"]

    def test_portal_leave_rejects_hours_that_count_weekend_and_holiday(
        self, leave_overtime_client
    ):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "T002A", "教師假日")
            session.add(Holiday(date=date(2026, 3, 16), name="補假", is_active=True))
            _create_user(
                session,
                username="teacher_holiday_guard",
                password="PortalPass123",
                role="teacher",
                permissions=0,
                employee=employee,
            )
            session.commit()

        login_res = _login(client, "teacher_holiday_guard", "PortalPass123")
        assert login_res.status_code == 200

        create_res = client.post(
            "/api/portal/my-leaves",
            json={
                "leave_type": "annual",
                "start_date": "2026-03-13",
                "end_date": "2026-03-16",
                "leave_hours": 16,
                "reason": "跨假日測試",
            },
        )

        assert create_res.status_code == 400
        assert "自動排除週末與國定假日" in create_res.json()["detail"]

    def test_portal_leave_rejects_substitute_with_overlapping_pending_leave(
        self, leave_overtime_client
    ):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "T003", "教師丙")
            substitute = _create_employee(session, "T004", "代理老師")
            session.add(
                LeaveRecord(
                    employee_id=substitute.id,
                    leave_type="personal",
                    start_date=date(2026, 3, 20),
                    end_date=date(2026, 3, 20),
                    leave_hours=8,
                    is_approved=None,
                )
            )
            _create_user(
                session,
                username="teacher_with_substitute",
                password="PortalPass123",
                role="teacher",
                permissions=0,
                employee=employee,
            )
            session.commit()
            substitute_id = substitute.id

        login_res = _login(client, "teacher_with_substitute", "PortalPass123")
        assert login_res.status_code == 200

        create_res = client.post(
            "/api/portal/my-leaves",
            json={
                "leave_type": "personal",
                "start_date": "2026-03-20",
                "end_date": "2026-03-20",
                "leave_hours": 8,
                "reason": "家中有事",
                "substitute_employee_id": substitute_id,
            },
        )

        assert create_res.status_code == 409
        assert "代理人" in create_res.json()["detail"]
        assert "請假" in create_res.json()["detail"]

    def test_leave_approval_rejects_substitute_who_later_has_overlapping_leave(
        self, leave_overtime_client
    ):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "T005", "教師丁")
            substitute = _create_employee(session, "T006", "代理老師乙")
            leave = LeaveRecord(
                employee_id=employee.id,
                leave_type="personal",
                start_date=date(2026, 3, 20),
                end_date=date(2026, 3, 20),
                leave_hours=8,
                is_approved=None,
                substitute_employee_id=substitute.id,
                substitute_status="accepted",
            )
            substitute_leave = LeaveRecord(
                employee_id=substitute.id,
                leave_type="personal",
                start_date=date(2026, 3, 20),
                end_date=date(2026, 3, 20),
                leave_hours=8,
                is_approved=True,
            )
            session.add_all([leave, substitute_leave])
            _create_user(
                session,
                username="admin_substitute_guard",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            leave_id = leave.id

        login_res = _login(client, "admin_substitute_guard", "AdminPass123")
        assert login_res.status_code == 200

        approve_res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": True},
        )

        assert approve_res.status_code == 409
        assert "代理人" in approve_res.json()["detail"]

    def test_leave_approval_can_force_approve_without_substitute_acceptance(
        self, leave_overtime_client
    ):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "T005A", "教師戊")
            substitute = _create_employee(session, "T006A", "代理老師丙")
            leave = LeaveRecord(
                employee_id=employee.id,
                leave_type="personal",
                start_date=date(2026, 3, 26),
                end_date=date(2026, 3, 26),
                leave_hours=8,
                is_approved=None,
                substitute_employee_id=substitute.id,
                substitute_status="pending",
            )
            session.add(leave)
            _create_user(
                session,
                username="admin_force_substitute",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            leave_id = leave.id

        login_res = _login(client, "admin_force_substitute", "AdminPass123")
        assert login_res.status_code == 200

        approve_res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": True, "force_without_substitute": True},
        )

        assert approve_res.status_code == 200
        with session_factory() as session:
            leave = session.query(LeaveRecord).filter(LeaveRecord.id == leave_id).one()
            assert leave.is_approved is True
            assert leave.substitute_status == "waived"


class TestPortalSubstitutePendingCount:
    def test_only_counts_pending_requests_for_current_substitute(
        self, leave_overtime_client
    ):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            requester = _create_employee(session, "T007", "請假老師")
            substitute = _create_employee(session, "T008", "代理老師甲")
            other_substitute = _create_employee(session, "T009", "代理老師乙")
            _create_user(
                session,
                username="substitute_portal",
                password="PortalPass123",
                role="teacher",
                permissions=0,
                employee=substitute,
            )
            _create_user(
                session,
                username="other_substitute_portal",
                password="PortalPass123",
                role="teacher",
                permissions=0,
                employee=other_substitute,
            )
            session.add_all(
                [
                    LeaveRecord(
                        employee_id=requester.id,
                        leave_type="personal",
                        start_date=date(2026, 3, 21),
                        end_date=date(2026, 3, 21),
                        leave_hours=8,
                        substitute_employee_id=substitute.id,
                        substitute_status="pending",
                        is_approved=None,
                    ),
                    LeaveRecord(
                        employee_id=requester.id,
                        leave_type="personal",
                        start_date=date(2026, 3, 22),
                        end_date=date(2026, 3, 22),
                        leave_hours=8,
                        substitute_employee_id=substitute.id,
                        substitute_status="pending",
                        is_approved=None,
                    ),
                    LeaveRecord(
                        employee_id=requester.id,
                        leave_type="personal",
                        start_date=date(2026, 3, 23),
                        end_date=date(2026, 3, 23),
                        leave_hours=8,
                        substitute_employee_id=substitute.id,
                        substitute_status="accepted",
                        is_approved=None,
                    ),
                    LeaveRecord(
                        employee_id=requester.id,
                        leave_type="personal",
                        start_date=date(2026, 3, 24),
                        end_date=date(2026, 3, 24),
                        leave_hours=8,
                        substitute_employee_id=substitute.id,
                        substitute_status="rejected",
                        is_approved=None,
                    ),
                    LeaveRecord(
                        employee_id=requester.id,
                        leave_type="personal",
                        start_date=date(2026, 3, 25),
                        end_date=date(2026, 3, 25),
                        leave_hours=8,
                        substitute_employee_id=other_substitute.id,
                        substitute_status="pending",
                        is_approved=None,
                    ),
                ]
            )
            session.commit()

        login_res = _login(client, "substitute_portal", "PortalPass123")
        assert login_res.status_code == 200

        count_res = client.get("/api/portal/substitute-pending-count")
        assert count_res.status_code == 200
        assert count_res.json() == {"pending_count": 2}

        other_login_res = _login(client, "other_substitute_portal", "PortalPass123")
        assert other_login_res.status_code == 200

        other_count_res = client.get("/api/portal/substitute-pending-count")
        assert other_count_res.status_code == 200
        assert other_count_res.json() == {"pending_count": 1}

    def test_requires_portal_login(self, leave_overtime_client):
        client, _, _ = leave_overtime_client

        res = client.get("/api/portal/substitute-pending-count")

        assert res.status_code == 401


class TestLeaveScheduleGuard:
    def test_admin_update_rejects_hours_that_exceed_workdays_after_holiday_exclusion(
        self, leave_overtime_client
    ):
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "T010", "教師己")
            leave = LeaveRecord(
                employee_id=employee.id,
                leave_type="annual",
                start_date=date(2026, 3, 13),
                end_date=date(2026, 3, 13),
                leave_hours=8,
                is_approved=None,
            )
            session.add_all(
                [
                    leave,
                    Holiday(date=date(2026, 3, 16), name="補假", is_active=True),
                ]
            )
            _create_user(
                session,
                username="admin_update_guard",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            leave_id = leave.id

        login_res = _login(client, "admin_update_guard", "AdminPass123")
        assert login_res.status_code == 200

        update_res = client.put(
            f"/api/leaves/{leave_id}",
            json={
                "start_date": "2026-03-13",
                "end_date": "2026-03-16",
                "leave_hours": 16,
            },
        )

        assert update_res.status_code == 400
        assert "自動排除週末與國定假日" in update_res.json()["detail"]


class TestApprovedOvertimeRollback:
    def test_update_approved_overtime_revokes_comp_leave_and_recalculates_salary(
        self, leave_overtime_client
    ):
        client, session_factory, fake_salary_engine = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "E001", "員工甲")
            overtime = OvertimeRecord(
                employee_id=employee.id,
                overtime_date=date(2026, 3, 12),
                overtime_type="weekday",
                hours=2,
                overtime_pay=0,
                use_comp_leave=True,
                comp_leave_granted=True,
                is_approved=True,
                approved_by="admin",
            )
            quota = LeaveQuota(
                employee_id=employee.id,
                year=2026,
                leave_type="compensatory",
                total_hours=2,
            )
            session.add_all([overtime, quota])
            overtime_id = overtime.id
            _create_user(
                session,
                username="admin_update_ot",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            overtime_id = overtime.id
            employee_id = employee.id

        login_res = _login(client, "admin_update_ot", "AdminPass123")
        assert login_res.status_code == 200

        fake_salary_engine.reset_mock()
        update_res = client.put(
            f"/api/overtimes/{overtime_id}",
            json={"hours": 1.5},
        )
        assert update_res.status_code == 200
        assert update_res.json()["salary_recalculated"] is True
        fake_salary_engine.process_salary_calculation.assert_called_once_with(
            employee_id, 2026, 3
        )

        with session_factory() as session:
            overtime = (
                session.query(OvertimeRecord)
                .filter(OvertimeRecord.id == overtime_id)
                .one()
            )
            quota = (
                session.query(LeaveQuota)
                .filter(
                    LeaveQuota.employee_id == employee_id,
                    LeaveQuota.year == 2026,
                    LeaveQuota.leave_type == "compensatory",
                )
                .one()
            )
            assert overtime.is_approved is None
            assert overtime.comp_leave_granted is False
            assert overtime.hours == 1.5
            assert quota.total_hours == 0.0

    def test_delete_approved_overtime_recalculates_salary(self, leave_overtime_client):
        client, session_factory, fake_salary_engine = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "E002", "員工乙")
            overtime = OvertimeRecord(
                employee_id=employee.id,
                overtime_date=date(2026, 4, 8),
                overtime_type="weekday",
                hours=2,
                overtime_pay=500,
                use_comp_leave=False,
                comp_leave_granted=False,
                is_approved=True,
                approved_by="admin",
            )
            session.add(overtime)
            _create_user(
                session,
                username="admin_delete_ot",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            overtime_id = overtime.id
            employee_id = employee.id

        login_res = _login(client, "admin_delete_ot", "AdminPass123")
        assert login_res.status_code == 200

        fake_salary_engine.reset_mock()
        delete_res = client.delete(f"/api/overtimes/{overtime_id}")
        assert delete_res.status_code == 200
        assert delete_res.json()["salary_recalculated"] is True
        fake_salary_engine.process_salary_calculation.assert_called_once_with(
            employee_id, 2026, 4
        )

        with session_factory() as session:
            overtime = (
                session.query(OvertimeRecord)
                .filter(OvertimeRecord.id == overtime_id)
                .first()
            )
            assert overtime is None

    def test_rejecting_previously_approved_comp_overtime_revokes_granted_quota(
        self, leave_overtime_client
    ):
        client, session_factory, fake_salary_engine = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "E003", "員工丙")
            overtime = OvertimeRecord(
                employee_id=employee.id,
                overtime_date=date(2026, 5, 6),
                overtime_type="weekday",
                hours=3,
                overtime_pay=0,
                use_comp_leave=True,
                comp_leave_granted=True,
                is_approved=True,
                approved_by="admin",
            )
            quota = LeaveQuota(
                employee_id=employee.id,
                year=2026,
                leave_type="compensatory",
                total_hours=3,
            )
            session.add_all([overtime, quota])
            _create_user(
                session,
                username="admin_reject_ot",
                password="AdminPass123",
                role="admin",
                permissions=-1,
            )
            session.commit()
            overtime_id = overtime.id
            employee_id = employee.id

        login_res = _login(client, "admin_reject_ot", "AdminPass123")
        assert login_res.status_code == 200

        fake_salary_engine.reset_mock()
        # audit P1（2026-05-07）：overtime 駁回必填 rejection_reason ≥3 字
        reject_res = client.put(
            f"/api/overtimes/{overtime_id}/approve",
            params={"approved": "false", "rejection_reason": "事後審核發現問題"},
        )
        assert reject_res.status_code == 200
        assert reject_res.json()["salary_recalculated"] is True
        fake_salary_engine.process_salary_calculation.assert_called_once_with(
            employee_id, 2026, 5
        )

        with session_factory() as session:
            overtime = (
                session.query(OvertimeRecord)
                .filter(OvertimeRecord.id == overtime_id)
                .one()
            )
            quota = (
                session.query(LeaveQuota)
                .filter(
                    LeaveQuota.employee_id == employee_id,
                    LeaveQuota.year == 2026,
                    LeaveQuota.leave_type == "compensatory",
                )
                .one()
            )
            assert overtime.is_approved is False
            assert overtime.approved_by is None
            assert overtime.comp_leave_granted is False
            assert quota.total_hours == 0.0


class TestSelfApprovalGuard:
    """H2：自我核准防護——有 employee_id 的帳號不可核准自己的假單/加班。"""

    def test_employee_with_account_cannot_self_approve_leave(
        self, leave_overtime_client
    ):
        """提交假單的教師若同時具備 LEAVES_WRITE 權限，不可自我核准。"""
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "SA001", "雙角色教師")
            leave = LeaveRecord(
                employee_id=employee.id,
                leave_type="personal",
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                leave_hours=8,
                is_approved=None,
            )
            session.add(leave)
            _create_user(
                session,
                username="dual_role_teacher",
                password="DualPass123",
                role="hr",
                permissions=-1,
                employee=employee,
            )
            session.commit()
            leave_id = leave.id

        login_res = _login(client, "dual_role_teacher", "DualPass123")
        assert login_res.status_code == 200

        approve_res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": True},
        )
        assert approve_res.status_code == 403
        assert "自我核准" in approve_res.json()["detail"]

    def test_admin_without_employee_id_can_approve_others_leave(
        self, leave_overtime_client
    ):
        """純管理員帳號（無 employee_id）可正常核准他人假單。"""
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            employee = _create_employee(session, "SA002", "一般教師")
            leave = LeaveRecord(
                employee_id=employee.id,
                leave_type="personal",
                start_date=date(2026, 4, 2),
                end_date=date(2026, 4, 2),
                leave_hours=8,
                is_approved=None,
            )
            session.add(leave)
            _create_user(
                session,
                username="pure_admin_approver",
                password="AdminPass123",
                role="admin",
                permissions=-1,
                # employee=None（無 employee_id）
            )
            session.commit()
            leave_id = leave.id

        login_res = _login(client, "pure_admin_approver", "AdminPass123")
        assert login_res.status_code == 200

        approve_res = client.put(
            f"/api/leaves/{leave_id}/approve",
            json={"approved": True},
        )
        # 純管理員（無 employee_id）核准他人假單應成功（200）
        assert approve_res.status_code == 200


class TestBatchSelfApprovalGuard:
    """C1：批次核准中的自我核准防護。"""

    def test_batch_approve_leaves_blocks_self_approval(self, leave_overtime_client):
        """員工不可在批次核准中核准自己的假單。"""
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            # 申請人本身
            self_employee = _create_employee(session, "BA001", "批次自我核准教師")
            # 另一位員工的假單（應可正常核准）
            other_employee = _create_employee(session, "BA002", "他人教師")

            self_leave = LeaveRecord(
                employee_id=self_employee.id,
                leave_type="personal",
                start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 1),
                leave_hours=8,
                is_approved=None,
            )
            # 使用 5/4（週一）避開週末；schedule guard 會擋掉週末 0 工時的請假
            other_leave = LeaveRecord(
                employee_id=other_employee.id,
                leave_type="personal",
                start_date=date(2026, 5, 4),
                end_date=date(2026, 5, 4),
                leave_hours=8,
                is_approved=None,
            )
            session.add_all([self_leave, other_leave])
            # 使用 admin 角色：無 ApprovalPolicy 時 admin 可核准任何人
            _create_user(
                session,
                username="batch_self_approver",
                password="BatchPass123",
                role="admin",
                permissions=-1,
                employee=self_employee,
            )
            session.commit()
            self_leave_id = self_leave.id
            other_leave_id = other_leave.id

        login_res = _login(client, "batch_self_approver", "BatchPass123")
        assert login_res.status_code == 200

        res = client.post(
            "/api/leaves/batch-approve",
            json={"ids": [self_leave_id, other_leave_id], "approved": True},
        )
        assert res.status_code == 200
        data = res.json()
        # 自己的假單應在 failed 清單中
        failed_ids = [f["id"] for f in data["failed"]]
        assert self_leave_id in failed_ids
        failed_reasons = [
            f["reason"] for f in data["failed"] if f["id"] == self_leave_id
        ]
        assert any("自我核准" in r for r in failed_reasons)
        # 他人假單應成功
        assert other_leave_id in data["succeeded"]

    def test_batch_approve_overtimes_blocks_self_approval(self, leave_overtime_client):
        """員工不可在批次核准中核准自己的加班單。"""
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            self_employee = _create_employee(session, "BA003", "批次加班自核員工")
            other_employee = _create_employee(session, "BA004", "他人加班員工")

            self_ot = OvertimeRecord(
                employee_id=self_employee.id,
                overtime_date=date(2026, 5, 3),
                overtime_type="weekday",
                hours=2.0,
                is_approved=None,
            )
            other_ot = OvertimeRecord(
                employee_id=other_employee.id,
                overtime_date=date(2026, 5, 4),
                overtime_type="weekday",
                hours=2.0,
                is_approved=None,
            )
            session.add_all([self_ot, other_ot])
            # 使用 admin 角色：無 ApprovalPolicy 時 admin 可核准任何人
            _create_user(
                session,
                username="batch_ot_self_approver",
                password="BatchOTPass123",
                role="admin",
                permissions=-1,
                employee=self_employee,
            )
            session.commit()
            self_ot_id = self_ot.id
            other_ot_id = other_ot.id

        login_res = _login(client, "batch_ot_self_approver", "BatchOTPass123")
        assert login_res.status_code == 200

        res = client.post(
            "/api/overtimes/batch-approve",
            json={"ids": [self_ot_id, other_ot_id], "approved": True},
        )
        assert res.status_code == 200
        data = res.json()
        # 自己的加班單應在 failed 清單中
        failed_ids = [f["id"] for f in data["failed"]]
        assert self_ot_id in failed_ids
        failed_reasons = [f["reason"] for f in data["failed"] if f["id"] == self_ot_id]
        assert any("自我核准" in r for r in failed_reasons)
        # 他人加班單應成功
        assert other_ot_id in data["succeeded"]


class TestConcurrentApprovalQuotaGuard:
    """V11：多張待審假單同時核准時，配額應正確計算不超支。"""

    def test_approving_second_leave_blocked_when_first_is_pending(
        self, leave_overtime_client
    ):
        """核准第二張待審假單時，應計入其他待審假單使用量，防止超額核准（race condition 防護）。"""
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            emp = _create_employee(session, "QR001", "配額競態教師")
            quota = LeaveQuota(
                employee_id=emp.id,
                year=2026,
                leave_type="annual",
                total_hours=8.0,  # 只有 1 天
            )
            session.add(quota)
            _create_user(
                session,
                username="quota_race_approver",
                password="QuotaPass123",
                role="admin",
                permissions=-1,
            )
            # 兩張各 8 小時的待審特休（總計 16 小時 > 配額 8 小時）
            leave1 = LeaveRecord(
                employee_id=emp.id,
                leave_type="annual",
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 1),
                leave_hours=8,
                is_approved=None,
            )
            leave2 = LeaveRecord(
                employee_id=emp.id,
                leave_type="annual",
                start_date=date(2026, 4, 2),
                end_date=date(2026, 4, 2),
                leave_hours=8,
                is_approved=None,
            )
            session.add_all([leave1, leave2])
            session.commit()
            leave1_id = leave1.id
            leave2_id = leave2.id

        login_res = _login(client, "quota_race_approver", "QuotaPass123")
        assert login_res.status_code == 200

        # 核准第一張：此時 leave2 仍 pending，include_pending=True 會計入
        # approved=0, pending=8(leave2), committed=8, leave1.hours=8 > remaining=0 → 應被阻擋
        res1 = client.put(f"/api/leaves/{leave1_id}/approve", json={"approved": True})
        assert (
            res1.status_code == 400
        ), f"第一張核准應因另一張待審假單佔用配額而被阻擋，但回傳 {res1.status_code}: {res1.json()}"

    def test_approving_second_leave_blocked_after_first_approved(
        self, leave_overtime_client
    ):
        """第一張核准後，第二張因配額耗盡應被阻擋。"""
        client, session_factory, _ = leave_overtime_client
        with session_factory() as session:
            emp = _create_employee(session, "QR002", "配額序列教師")
            quota = LeaveQuota(
                employee_id=emp.id,
                year=2026,
                leave_type="annual",
                total_hours=8.0,
            )
            session.add(quota)
            _create_user(
                session,
                username="quota_seq_approver",
                password="QuotaSeq123",
                role="admin",
                permissions=-1,
            )
            # 第一張 8 小時（pending），第二張 8 小時（pending）
            leave_a = LeaveRecord(
                employee_id=emp.id,
                leave_type="annual",
                start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 1),
                leave_hours=8,
                is_approved=None,
            )
            session.add(leave_a)
            session.commit()
            leave_a_id = leave_a.id

        login_res = _login(client, "quota_seq_approver", "QuotaSeq123")
        assert login_res.status_code == 200

        # 先核准第一張（唯一一張 pending，應成功）
        res_a = client.put(f"/api/leaves/{leave_a_id}/approve", json={"approved": True})
        assert res_a.status_code == 200

        # 再建立第二張（核准後才新增，模擬真實情境）
        with session_factory() as session:
            leave_b = LeaveRecord(
                employee_id=_get_employee_id(session, "QR002"),
                leave_type="annual",
                start_date=date(2026, 5, 2),
                end_date=date(2026, 5, 2),
                leave_hours=8,
                is_approved=None,
            )
            session.add(leave_b)
            session.commit()
            leave_b_id = leave_b.id

        res_b = client.put(f"/api/leaves/{leave_b_id}/approve", json={"approved": True})
        assert (
            res_b.status_code == 400
        ), f"第二張 8 小時特休核准應因配額耗盡而被阻擋，但回傳 {res_b.status_code}: {res_b.json()}"


def _get_employee_id(session, employee_code: str) -> int:
    """輔助：依員工編號查 DB 主鍵"""
    emp = session.query(Employee).filter(Employee.employee_id == employee_code).first()
    return emp.id

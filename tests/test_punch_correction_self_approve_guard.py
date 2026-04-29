"""F-015 回歸測試：punch_corrections.py approve 缺自我核准守衛

修補目標：在 `PUT /api/punch-corrections/{correction_id}/approve` 取得
correction 物件後（存在性檢查之後），補上「approver_eid 不可等於
correction.employee_id」的自我守衛，對齊：
- api/leaves.py:1014-1019
- api/overtimes.py:1075-1080

威脅：員工 A 為主管或具 `Permission.APPROVALS` 之角色，先以 portal 提交
自己的補打卡申請，再以管理端 API 核准 → 直接漂白遲到/缺卡、取消扣款，
構成金流 A 錢路徑。

涵蓋：
- 自我核准 → 403
- 主管核准下屬補打卡（policy 已設）→ 200
- admin 核准下屬補打卡（admin fallback）→ 200
- 純 admin（user.employee_id is None）核准任何人 → 200（None-safe）
"""

import os
import sys
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.punch_corrections import router as punch_corrections_router
from models.database import (
    ApprovalPolicy,
    Base,
    Employee,
    PunchCorrectionRequest,
    User,
)
from utils.auth import hash_password
from utils.permissions import Permission

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def punch_client(tmp_path):
    """建立隔離的 sqlite 測試 app（補打卡自我核准守衛用）。"""
    db_path = tmp_path / "punch-correction-self-guard.sqlite"
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

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(punch_corrections_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_employee(session, *, employee_id: str, name: str) -> Employee:
    emp = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    return emp


def _make_user(
    session,
    *,
    username: str,
    role: str,
    permissions: int,
    employee_id: int | None = None,
) -> User:
    user = User(
        employee_id=employee_id,
        username=username,
        password_hash=hash_password("Passw0rd!"),
        role=role,
        permissions=permissions,
        is_active=True,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    return user


def _make_correction(
    session, *, employee_id: int, on_date: date | None = None
) -> PunchCorrectionRequest:
    on_date = on_date or (date.today() - timedelta(days=2))
    corr = PunchCorrectionRequest(
        employee_id=employee_id,
        attendance_date=on_date,
        correction_type="punch_out",
        requested_punch_in=None,
        requested_punch_out=datetime(on_date.year, on_date.month, on_date.day, 18, 0),
        reason="忘記打下班",
        is_approved=None,
    )
    session.add(corr)
    session.flush()
    return corr


def _seed_policy(
    session,
    *,
    submitter_role: str,
    approver_roles: str,
    doc_type: str = "punch_correction",
):
    policy = ApprovalPolicy(
        doc_type=doc_type,
        submitter_role=submitter_role,
        approver_roles=approver_roles,
        is_active=True,
    )
    session.add(policy)
    session.flush()
    return policy


def _login(client: TestClient, username: str):
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": "Passw0rd!"},
    )


APPROVALS_PERMS = int(Permission.APPROVALS) | int(Permission.ATTENDANCE_READ)


# ══════════════════════════════════════════════════════════════════════
# F-015 主要測試
# ══════════════════════════════════════════════════════════════════════


class TestF015PunchCorrectionApprove:
    """F-015：補打卡核准必須擋自我核准。"""

    def test_employee_cannot_approve_own_punch_correction(self, punch_client):
        """提交補打卡的員工若同時持 APPROVALS 權限，自我核准必須 403。

        守衛必須在 _check_approval_eligibility 之前即觸發；故即便 policy
        允許 supervisor 互審 supervisor，自我守衛仍應先擋下這筆。"""
        client, sf = punch_client
        with sf() as s:
            emp = _make_employee(s, employee_id="E_self", name="自核員工")
            _make_user(
                s,
                username="self_approver",
                role="supervisor",
                permissions=APPROVALS_PERMS,
                employee_id=emp.id,
            )
            # 設 policy 允許 supervisor 審 supervisor，確保「角色資格」這層
            # 不會先擋下；如此才能精準測到「自我守衛」這層。
            _seed_policy(
                s,
                submitter_role="supervisor",
                approver_roles="supervisor,admin",
            )
            corr = _make_correction(s, employee_id=emp.id)
            s.commit()
            corr_id = corr.id

        assert _login(client, "self_approver").status_code == 200

        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 403, res.text
        detail = res.json().get("detail", "")
        assert "自" in detail and ("核准" in detail or "補打卡" in detail)

    def test_supervisor_can_approve_subordinate_punch_correction(self, punch_client):
        """主管核准下屬補打卡（policy 已設 supervisor 可審 teacher）→ 200。"""
        client, sf = punch_client
        with sf() as s:
            sub_emp = _make_employee(s, employee_id="E_subordinate", name="下屬")
            sup_emp = _make_employee(s, employee_id="E_supervisor", name="主管")
            # 下屬 user（teacher 角色）
            _make_user(
                s,
                username="teacher_sub",
                role="teacher",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=sub_emp.id,
            )
            # 主管 user（持 APPROVALS）
            _make_user(
                s,
                username="supervisor_ok",
                role="supervisor",
                permissions=APPROVALS_PERMS,
                employee_id=sup_emp.id,
            )
            # 政策：teacher 的補打卡可由 supervisor 審
            _seed_policy(
                s,
                submitter_role="teacher",
                approver_roles="supervisor,admin",
            )
            corr = _make_correction(s, employee_id=sub_emp.id)
            s.commit()
            corr_id = corr.id

        assert _login(client, "supervisor_ok").status_code == 200

        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

    def test_admin_can_approve_subordinate_punch_correction(self, punch_client):
        """admin 帳號（綁了 employee_id 但非申請人）核准下屬 → 200（admin fallback）。"""
        client, sf = punch_client
        with sf() as s:
            sub_emp = _make_employee(s, employee_id="E_sub2", name="下屬2")
            admin_emp = _make_employee(s, employee_id="E_admin", name="管理員")
            _make_user(
                s,
                username="teacher_sub2",
                role="teacher",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=sub_emp.id,
            )
            _make_user(
                s,
                username="admin_with_eid",
                role="admin",
                permissions=APPROVALS_PERMS,
                employee_id=admin_emp.id,
            )
            # 不設 ApprovalPolicy → 走 admin fallback
            corr = _make_correction(s, employee_id=sub_emp.id)
            s.commit()
            corr_id = corr.id

        assert _login(client, "admin_with_eid").status_code == 200

        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

    def test_pure_admin_account_without_employee_id_can_approve(self, punch_client):
        """純 admin（user.employee_id is None）→ 不應因 None 比對誤擋。"""
        client, sf = punch_client
        with sf() as s:
            target_emp = _make_employee(s, employee_id="E_target", name="目標員工")
            _make_user(
                s,
                username="teacher_target",
                role="teacher",
                permissions=int(Permission.ATTENDANCE_READ),
                employee_id=target_emp.id,
            )
            _make_user(
                s,
                username="pure_admin",
                role="admin",
                permissions=APPROVALS_PERMS,
                employee_id=None,
            )
            # 不設 policy → admin fallback
            corr = _make_correction(s, employee_id=target_emp.id)
            s.commit()
            corr_id = corr.id

        assert _login(client, "pure_admin").status_code == 200

        res = client.put(
            f"/api/punch-corrections/{corr_id}/approve",
            json={"approved": True},
        )
        assert res.status_code == 200, res.text

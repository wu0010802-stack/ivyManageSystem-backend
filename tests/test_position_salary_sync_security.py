"""tests/test_position_salary_sync_security.py — 職位薪資同步 P1 安全回歸（2026-04-28）。

修正前風險（Security Review P1-2）：
- POST /api/config/position-salary/sync 只要 SETTINGS_WRITE 即可批次改 base_salary，
  繞過 SalaryManualAdjustRequest 的 le=500_000 上限與 require_finance_approve 簽核。
- PositionSalaryUpdate 欄位只有 ge=0，沒有 le 上限；先把標準設成天文數字再 sync，
  下個月薪資、勞健保、加班費全部吃到異常底薪。

修正後守衛：
1. 標準底薪設定（PUT /position-salary）每欄位加 le=_POSITION_SALARY_MAX = 500_000
2. /position-salary/sync 改要 SALARY_WRITE
3. PositionSalarySyncRequest 加 adjustment_reason: str ≥ 5 字
4. 任一員工 |delta| > FINANCE_APPROVAL_THRESHOLD 需 ACTIVITY_PAYMENT_APPROVE
5. 員工不得 sync 自己的薪資（require_not_self_salary_record 同模式）
6. audit_changes 寫每位員工 old/new
"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.config import router as config_router
from models.base import Base
from models.database import Employee, PositionSalaryConfig, User
from utils.auth import hash_password
from utils.permissions import Permission

# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def pos_client(tmp_path):
    db_path = tmp_path / "pos_salary.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
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
    app.include_router(config_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _make_user(session, *, username, permissions, employee_id=None, role="admin"):
    u = User(
        username=username,
        password_hash=hash_password("Temp123456"),
        role=role,
        permissions=permissions,
        employee_id=employee_id,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _make_employee(
    session,
    *,
    name="教師甲",
    base_salary=30000,
    position="班導",
    title="幼兒園教師",
    bonus_grade="A",
):
    """建立一名「班導 A」教師：_map_employee_to_standard_key 會對到 head_teacher_a。"""
    emp = Employee(
        employee_id=f"E_{name}",
        name=name,
        base_salary=base_salary,
        employee_type="regular",
        is_active=True,
        position=position,
        title=title,
        bonus_grade=bonus_grade,
    )
    session.add(emp)
    session.flush()
    return emp


def _seed_position_config(session, *, head_teacher_a=35000):
    """建立 PositionSalaryConfig；只設 head_teacher_a，其餘欄位走預設。"""
    cfg = PositionSalaryConfig(head_teacher_a=head_teacher_a, version=1)
    session.add(cfg)
    session.flush()
    return cfg


def _login(client: TestClient, username: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": "Temp123456"}
    )


# ══════════════════════════════════════════════════════════════════════
# #1 PositionSalaryUpdate 加 le 上限：阻擋天文數字標準薪資
# ══════════════════════════════════════════════════════════════════════


class TestPositionSalaryUpdateUpperBound:
    def test_put_position_salary_rejects_above_cap(self, pos_client):
        """PUT /position-salary 標準底薪 > 500_000 應 422。"""
        client, sf = pos_client
        with sf() as s:
            _make_user(
                s,
                username="settings_admin",
                permissions=Permission.SETTINGS_READ | Permission.SETTINGS_WRITE,
            )
            s.commit()

        assert _login(client, "settings_admin").status_code == 200
        res = client.put(
            "/api/config/position-salary",
            json={"head_teacher_a": 1_000_000},  # 超過 le=500_000
        )
        assert res.status_code == 422, res.text

    def test_put_position_salary_accepts_at_cap(self, pos_client):
        """剛好 = 500_000 應通過（邊界）。"""
        client, sf = pos_client
        with sf() as s:
            _make_user(
                s,
                username="settings_admin",
                permissions=Permission.SETTINGS_READ | Permission.SETTINGS_WRITE,
            )
            s.commit()

        assert _login(client, "settings_admin").status_code == 200
        res = client.put(
            "/api/config/position-salary",
            json={"head_teacher_a": 500_000},
        )
        assert res.status_code == 200, res.text


# ══════════════════════════════════════════════════════════════════════
# #2 sync 端點權限：SALARY_WRITE 才能執行（移除 SETTINGS_WRITE 旁路）
# ══════════════════════════════════════════════════════════════════════


class TestSyncPermissionTightening:
    def test_settings_write_alone_cannot_sync_403(self, pos_client):
        """僅有 SETTINGS_WRITE（無 SALARY_WRITE）應 403。"""
        client, sf = pos_client
        with sf() as s:
            _make_employee(s)
            _seed_position_config(s)
            _make_user(
                s,
                username="settings_only",
                permissions=Permission.SETTINGS_READ | Permission.SETTINGS_WRITE,
            )
            s.commit()

        assert _login(client, "settings_only").status_code == 200
        res = client.post(
            "/api/config/position-salary/sync",
            json={"adjustment_reason": "標準調薪每年例行同步"},
        )
        assert res.status_code == 403, res.text

    def test_salary_write_can_sync(self, pos_client):
        """SALARY_WRITE 應通過權限檢查。"""
        client, sf = pos_client
        with sf() as s:
            _make_employee(s, base_salary=34000)  # delta = 1000，不觸發大額簽核
            _seed_position_config(s, head_teacher_a=35000)
            _make_user(
                s,
                username="salary_admin",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
            )
            s.commit()

        assert _login(client, "salary_admin").status_code == 200
        res = client.post(
            "/api/config/position-salary/sync",
            json={"adjustment_reason": "標準調薪每年例行同步"},
        )
        assert res.status_code == 200, res.text


# ══════════════════════════════════════════════════════════════════════
# #3 adjustment_reason 必填
# ══════════════════════════════════════════════════════════════════════


class TestSyncRequiresReason:
    def test_sync_missing_reason_422(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _make_employee(s)
            _seed_position_config(s)
            _make_user(
                s,
                username="salary_admin",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
            )
            s.commit()

        assert _login(client, "salary_admin").status_code == 200
        # 無 adjustment_reason
        res = client.post("/api/config/position-salary/sync", json={})
        assert res.status_code == 422, res.text

    def test_sync_short_reason_422(self, pos_client):
        client, sf = pos_client
        with sf() as s:
            _make_employee(s)
            _seed_position_config(s)
            _make_user(
                s,
                username="salary_admin",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
            )
            s.commit()

        assert _login(client, "salary_admin").status_code == 200
        res = client.post(
            "/api/config/position-salary/sync",
            json={"adjustment_reason": "abc"},  # 3 字 < 5
        )
        assert res.status_code == 422, res.text


# ══════════════════════════════════════════════════════════════════════
# #4 大額調薪須具備 ACTIVITY_PAYMENT_APPROVE
# ══════════════════════════════════════════════════════════════════════


class TestSyncLargeDeltaRequiresApproval:
    def test_large_delta_without_approve_403(self, pos_client):
        """單員工 |delta| > FINANCE_APPROVAL_THRESHOLD (1000) 但無金流簽核 → 403。"""
        client, sf = pos_client
        with sf() as s:
            _make_employee(s, base_salary=30000)  # 與標準差 5000 > 1000
            _seed_position_config(s, head_teacher_a=35000)
            _make_user(
                s,
                username="salary_admin",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
            )
            s.commit()

        assert _login(client, "salary_admin").status_code == 200
        res = client.post(
            "/api/config/position-salary/sync",
            json={"adjustment_reason": "全員年度調薪同步"},
        )
        assert res.status_code == 403, res.text
        assert "簽核" in res.json().get("detail", "")

        # 應 fail-fast：sync 不應執行
        with sf() as s:
            emp = s.query(Employee).first()
            assert float(emp.base_salary) == 30000, "403 後不應改 base_salary"

    def test_large_delta_with_approve_succeeds(self, pos_client):
        """有 ACTIVITY_PAYMENT_APPROVE → 通過。"""
        client, sf = pos_client
        with sf() as s:
            _make_employee(s, base_salary=30000)
            _seed_position_config(s, head_teacher_a=35000)
            _make_user(
                s,
                username="salary_approver",
                permissions=(
                    Permission.SALARY_READ
                    | Permission.SALARY_WRITE
                    | Permission.ACTIVITY_PAYMENT_APPROVE
                ),
            )
            s.commit()

        assert _login(client, "salary_approver").status_code == 200
        res = client.post(
            "/api/config/position-salary/sync",
            json={"adjustment_reason": "全員年度調薪同步"},
        )
        assert res.status_code == 200, res.text

        with sf() as s:
            emp = s.query(Employee).first()
            assert float(emp.base_salary) == 35000


# ══════════════════════════════════════════════════════════════════════
# #5 員工不得 sync 自己的薪資（self-edit 守衛）
# ══════════════════════════════════════════════════════════════════════


class TestSyncSelfEditGuard:
    def test_employee_cannot_sync_own_salary_403(self, pos_client):
        """user.employee_id 對應到目標員工，sync 觸及自己 → 403。"""
        client, sf = pos_client
        with sf() as s:
            emp = _make_employee(s, base_salary=34500)  # delta=500，不會被大額擋
            _seed_position_config(s, head_teacher_a=35000)
            _make_user(
                s,
                username="self_sync",
                permissions=Permission.SALARY_READ | Permission.SALARY_WRITE,
                employee_id=emp.id,
            )
            s.commit()
            emp_id = emp.id

        assert _login(client, "self_sync").status_code == 200
        # 顯式只 sync 自己
        res = client.post(
            "/api/config/position-salary/sync",
            json={
                "adjustment_reason": "標準調薪同步",
                "employee_ids": [emp_id],
            },
        )
        assert res.status_code == 403, res.text
        assert "自己" in res.json().get("detail", "")

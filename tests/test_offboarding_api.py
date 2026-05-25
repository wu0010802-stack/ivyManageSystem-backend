"""驗證 router 註冊 + endpoint path 存在（後續 task 補完整 case）。

Task 8 smoke test：
- test_router_registered: api.offboarding 模組可 import，router prefix=/offboarding，
  可掛入 FastAPI app，且 main.py 有對應 include_router 呼叫。
Task 9 preview endpoint：
- test_preview_returns_leave_snapshot_and_warnings: happy path，回 200 + 正確 snapshot
- test_preview_returns_404_for_unknown_employee: 未知員工 → 404
- test_preview_does_not_write_to_db: 純讀，emp.resign_date 不被寫入
"""

from __future__ import annotations

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.auth import router as auth_router, _account_failures, _ip_attempts
from api.offboarding import router as offboarding_router
from models.database import Base, Employee, User, LeaveQuota
from models.salary import SalaryRecord
from utils.auth import hash_password

_counter = 0


@pytest.fixture
def client(tmp_path):
    """SQLite in-memory + TestClient（auth + offboarding router）。"""
    db_path = tmp_path / "offboarding-smoke.sqlite"
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
    app.include_router(offboarding_router, prefix="/api")

    with TestClient(app) as c:
        yield c

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def db_session(tmp_path):
    """與 client fixture 共用同一個 SQLite DB 的 session（供 factory fixtures 寫入）。

    注意：此 fixture 獨立建立 engine，若需要與 client 共用資料，
    請在 client fixture 已 swap base_module._engine 後、在同一 tmp_path 下建立。
    實際上 client + db_session 一起搭配時，兩者 tmp_path 會不同。
    因此 factory fixtures 直接用 db_session；client 另需用 _seed_* 函式。
    本 fixture 主要供純 DB 驗證（test_preview_does_not_write_to_db）使用。
    """
    db_path = tmp_path / "offboarding-smoke.sqlite"
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

    session = session_factory()
    yield session
    session.close()

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture
def employee_factory(db_session):
    """建立測試員工。daily_wage 換算為 base_salary = daily_wage * 30 存入 DB。"""

    def _factory(
        *,
        name: str = None,
        hire_date=date(2020, 1, 1),
        is_active=True,
        daily_wage=None,
    ) -> Employee:
        global _counter
        _counter += 1
        base_salary = int(daily_wage * 30) if daily_wage is not None else 0
        emp = Employee(
            employee_id=f"OBD{_counter:04d}",
            name=name or f"測試員工{_counter}",
            hire_date=hire_date,
            is_active=is_active,
            base_salary=base_salary,
        )
        db_session.add(emp)
        db_session.flush()
        return emp

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


def _seed_admin_user(session_factory, username="ob_admin", password="AdminPass123"):
    """在 DB 建立 admin 帳號並回傳 (username, password)。"""
    with session_factory() as session:
        session.add(
            User(
                employee_id=None,
                username=username,
                password_hash=hash_password(password),
                role="admin",
                permission_names=["*"],
                is_active=True,
                must_change_password=False,
            )
        )
        session.commit()
    return username, password


@pytest.fixture
def integrated_client(tmp_path):
    """client + session_factory 一起回傳，供需要同時操作 HTTP + DB 的 test 使用。"""
    db_path = tmp_path / "offboarding-integrated.sqlite"
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
    app.include_router(offboarding_router, prefix="/api")

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def test_router_registered(client):
    """router 已掛載：模組可 import，router prefix 正確，app 建立無異常。

    Task 8 只建 router 殼（無 endpoint），因此不做 HTTP path 存在性驗證。
    HTTP endpoint 驗證由 Task 9+ 補入（test_preview_* / test_process_* 等）。

    驗證三件事：
    1. `from api.offboarding import router` 無 ImportError（fixture 的 import 已驗證）
    2. router.prefix == "/offboarding"
    3. client fixture 可正常建立（app.include_router 不拋例外）
    """
    # 1 & 2: router 可 import 且 prefix 正確
    assert (
        offboarding_router.prefix == "/offboarding"
    ), f"router prefix 錯誤：{offboarding_router.prefix!r}，期望 '/offboarding'"

    # 3: main.py 有正確 include offboarding_router（grep 驗證）
    import pathlib

    main_py = pathlib.Path(__file__).parent.parent / "main.py"
    content = main_py.read_text()
    assert (
        "from api.offboarding import router as offboarding_router" in content
    ), "main.py 缺少 offboarding_router import"
    assert (
        "app.include_router(offboarding_router" in content
    ), "main.py 缺少 app.include_router(offboarding_router...)"

    # 4: client fixture 正常建立（proxy：GET /docs 應 200，確認 app 正常啟動）
    resp = client.get("/docs")
    assert resp.status_code == 200, f"app 啟動異常，/docs 回 {resp.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# Task 9: POST /offboarding/{id}/preview
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Task 10: POST /offboarding/{id}/process
# ─────────────────────────────────────────────────────────────────────────────


def test_process_happy_path(integrated_client):
    """happy path：4 step 完成 + user_account_revoked + SalaryRecord.unused_leave_payout=25200。

    員工 base_salary = 1800 * 30 = 54000；
    leave quota 112 小時 → remaining_days = 14.0；
    payout = 14 * 1800 = 25200。
    """
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="proc_admin_hp")
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    # 建員工
    with sf() as session:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"OBP{_counter:04d}",
            name=f"離職員工{_counter}",
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=54000,
        )
        session.add(emp)
        session.commit()
        emp_id = emp.id

    # 建 active User（供 revoke_user step）
    with sf() as session:
        _counter += 1
        user = User(
            employee_id=emp_id,
            username=f"teacher_{_counter}",
            password_hash=hash_password("pass"),
            role="teacher",
            is_active=True,
            must_change_password=False,
            token_version=1,
        )
        session.add(user)
        session.commit()

    # 建 leave quota（112 小時 = 14 天）
    with sf() as session:
        session.add(
            LeaveQuota(
                employee_id=emp_id,
                year=2026,
                leave_type="annual",
                total_hours=112,
            )
        )
        session.commit()

    # 建離職當月 salary record（resign_date=2026-05-20 → 5 月）
    with sf() as session:
        sr = SalaryRecord(
            employee_id=emp_id,
            salary_year=2026,
            salary_month=5,
        )
        session.add(sr)
        session.commit()
        sr_id = sr.id

    # resign_date 用過去日期，確保 is_active=False + user_account_revoked=True
    response = client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2026-05-20", "resign_reason": "個人因素"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_active"] is False
    assert body["user_account_revoked"] is True
    steps = [s["step"] for s in body["steps"]]
    assert "mark_appraisal" in steps
    assert "snapshot_leave" in steps
    assert "revoke_user" in steps

    with sf() as session:
        reloaded_sr = session.query(SalaryRecord).filter_by(id=sr_id).first()
        assert float(reloaded_sr.unused_leave_payout) == 25200.0


def test_process_already_offboarded_returns_409(integrated_client):
    """第二次呼叫 process → 409 ALREADY_OFFBOARDED。"""
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="proc_admin_409")
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    with sf() as session:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"OB4{_counter:04d}",
            name=f"二次離職{_counter}",
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=54000,
        )
        session.add(emp)
        session.commit()
        emp_id = emp.id

    with sf() as session:
        session.add(
            LeaveQuota(
                employee_id=emp_id,
                year=2026,
                leave_type="annual",
                total_hours=80,
            )
        )
        session.commit()

    # 第一次成功
    r1 = client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2026-06-15"},
    )
    assert r1.status_code == 200, r1.text

    # 第二次 409
    r2 = client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2026-07-01"},
    )
    assert r2.status_code == 409
    assert r2.json()["detail"] == "ALREADY_OFFBOARDED"


def test_process_resign_before_hire_returns_400(integrated_client):
    """resign_date < hire_date → 400 RESIGN_DATE_BEFORE_HIRE。"""
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="proc_admin_400")
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    with sf() as session:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"OB5{_counter:04d}",
            name=f"早辭員工{_counter}",
            hire_date=date(2025, 12, 1),
            is_active=True,
            base_salary=54000,
        )
        session.add(emp)
        session.commit()
        emp_id = emp.id

    response = client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2025-11-15"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "RESIGN_DATE_BEFORE_HIRE"


def test_process_failure_rolls_back_all_changes(integrated_client):
    """員工 base_salary=0（無日薪）→ 422，Employee/Record 全 rollback。"""
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="proc_admin_rb")
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    with sf() as session:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"OBR{_counter:04d}",
            name=f"零薪員工{_counter}",
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=0,  # _resolve_daily_wage → None → LEAVE_BALANCE_NOT_FOUND
        )
        session.add(emp)
        session.commit()
        emp_id = emp.id

    response = client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2026-06-15"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "LEAVE_BALANCE_NOT_FOUND"

    # 驗 rollback：resign_date 未被寫入、is_active 仍為 True
    with sf() as session:
        reloaded = session.query(Employee).filter_by(id=emp_id).first()
        assert reloaded.resign_date is None
        assert reloaded.is_active is True

    # 驗 rollback：OffboardingRecord 未被建立
    from models.offboarding import EmployeeOffboardingRecord

    with sf() as session:
        assert (
            session.query(EmployeeOffboardingRecord)
            .filter_by(employee_id=emp_id)
            .first()
        ) is None


# ─────────────────────────────────────────────────────────────────────────────
# Task 9: POST /offboarding/{id}/preview
# ─────────────────────────────────────────────────────────────────────────────


def test_preview_returns_leave_snapshot_and_warnings(integrated_client):
    """happy path：回 200 + 正確 leave snapshot + salary_record_target.exists。

    員工 base_salary = 1800 * 30 = 54000；
    leave quota 112 小時 → remaining_days = 14.0；
    payout = 14 * 1800 = 25200。
    """
    client, sf = integrated_client

    # 建 admin 帳號並登入（cookie 自動帶）
    username, password = _seed_admin_user(sf)
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    # 建員工
    with sf() as session:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"OBP{_counter:04d}",
            name="王小明",
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=54000,  # daily_wage = 54000 / 30 = 1800
        )
        session.add(emp)
        session.commit()
        emp_id = emp.id

    # 建 leave quota（112 小時 = 14 天）
    with sf() as session:
        session.add(
            LeaveQuota(
                employee_id=emp_id,
                year=2026,
                leave_type="annual",
                total_hours=112,
            )
        )
        session.commit()

    # 建當月 salary record
    with sf() as session:
        session.add(
            SalaryRecord(
                employee_id=emp_id,
                salary_year=2026,
                salary_month=6,
            )
        )
        session.commit()

    response = client.post(
        f"/api/offboarding/{emp_id}/preview",
        json={"resign_date": "2026-06-15", "resign_reason": "test"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["employee_id"] == emp_id
    assert body["employee_name"] == "王小明"
    assert body["preview"]["leave_snapshot"]["special_leave_days"] == 14.0
    assert body["preview"]["leave_snapshot"]["payout_amount"] == 25200.0
    assert body["preview"]["salary_record_target"]["exists"] is True


def test_preview_returns_404_for_unknown_employee(integrated_client):
    """未知 employee_id → 404 EMPLOYEE_NOT_FOUND。"""
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="ob_admin_404")
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    response = client.post(
        "/api/offboarding/99999/preview",
        json={"resign_date": "2026-06-15"},
    )
    assert response.status_code == 404


def test_preview_does_not_write_to_db(integrated_client):
    """preview 純讀：呼叫後 emp.resign_date 仍為 None。"""
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="ob_admin_ro")
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    with sf() as session:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"OBR{_counter:04d}",
            name=f"純讀員工{_counter}",
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=54000,
        )
        session.add(emp)
        session.commit()
        emp_id = emp.id

    with sf() as session:
        session.add(
            LeaveQuota(
                employee_id=emp_id,
                year=2026,
                leave_type="annual",
                total_hours=80,
            )
        )
        session.commit()

    client.post(
        f"/api/offboarding/{emp_id}/preview",
        json={"resign_date": "2026-06-15"},
    )

    # 確認 DB 未寫入 resign_date
    with sf() as session:
        reloaded = session.query(Employee).filter_by(id=emp_id).first()
        assert reloaded.resign_date is None  # 純讀


# ─────────────────────────────────────────────────────────────────────────────
# Task 11: GET /offboarding/{id} + PATCH /offboarding/{id}/nhi-unenroll
# ─────────────────────────────────────────────────────────────────────────────


def test_get_detail_returns_full_record(integrated_client):
    """happy path：process 後 GET，驗 leave_balance_snapshot 等欄位。

    員工 base_salary = 1500 * 30 = 45000；
    leave quota 80 小時 → remaining_days = 10.0。
    """
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="get_detail_admin")
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    # 建員工
    with sf() as session:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"GD1{_counter:04d}",
            name="李四",
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=45000,  # daily_wage = 45000 / 30 = 1500
        )
        session.add(emp)
        session.commit()
        emp_id = emp.id

    # 建 leave quota（80 小時 = 10 天）
    with sf() as session:
        session.add(
            LeaveQuota(
                employee_id=emp_id,
                year=2026,
                leave_type="annual",
                total_hours=80,
            )
        )
        session.commit()

    # process 離職
    process_res = client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2026-06-15", "resign_reason": "另謀高就"},
    )
    assert process_res.status_code == 200, process_res.text

    # GET detail
    response = client.get(f"/api/offboarding/{emp_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["employee_id"] == emp_id
    assert body["employee_name"] == "李四"
    assert body["resign_reason"] == "另謀高就"
    assert body["leave_snapshot_at"] is not None
    assert body["leave_balance_snapshot"]["remaining_days"] == 10.0
    assert body["magic_link_active"] is False  # Phase 1 未產 token


def test_get_detail_404_for_employee_without_record(integrated_client):
    """無離職紀錄的員工 GET → 404 OFFBOARDING_RECORD_NOT_FOUND。"""
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="get_detail_404_admin")
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    # 建員工（未觸發離職流程）
    with sf() as session:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"GD2{_counter:04d}",
            name=f"未離職員工{_counter}",
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=45000,
        )
        session.add(emp)
        session.commit()
        emp_id = emp.id

    response = client.get(f"/api/offboarding/{emp_id}")
    assert response.status_code == 404
    assert response.json()["detail"] == "OFFBOARDING_RECORD_NOT_FOUND"


def test_patch_nhi_unenroll_sets_timestamp(integrated_client):
    """PATCH nhi-unenroll submitted=true 設時間戳；submitted=false 清空。"""
    client, sf = integrated_client
    username, password = _seed_admin_user(sf, username="nhi_unenroll_admin")
    login_res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert login_res.status_code == 200, login_res.text

    # 建員工
    with sf() as session:
        global _counter
        _counter += 1
        emp = Employee(
            employee_id=f"NHI{_counter:04d}",
            name=f"健保退保員工{_counter}",
            hire_date=date(2020, 1, 1),
            is_active=True,
            base_salary=45000,
        )
        session.add(emp)
        session.commit()
        emp_id = emp.id

    # 建 leave quota
    with sf() as session:
        session.add(
            LeaveQuota(
                employee_id=emp_id,
                year=2026,
                leave_type="annual",
                total_hours=80,
            )
        )
        session.commit()

    # process 離職（先建 OffboardingRecord）
    process_res = client.post(
        f"/api/offboarding/{emp_id}/process",
        json={"resign_date": "2026-06-15"},
    )
    assert process_res.status_code == 200, process_res.text

    # PATCH submitted=true → 應有時間戳
    r1 = client.patch(
        f"/api/offboarding/{emp_id}/nhi-unenroll",
        json={"submitted": True},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["nhi_unenroll_submitted_at"] is not None

    # 驗 GET 也反映
    r2 = client.get(f"/api/offboarding/{emp_id}")
    assert r2.json()["nhi_unenroll_submitted_at"] is not None

    # PATCH submitted=false → 清空
    r3 = client.patch(
        f"/api/offboarding/{emp_id}/nhi-unenroll",
        json={"submitted": False},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["nhi_unenroll_submitted_at"] is None

    # 驗 GET 也反映清空
    r4 = client.get(f"/api/offboarding/{emp_id}")
    assert r4.json()["nhi_unenroll_submitted_at"] is None

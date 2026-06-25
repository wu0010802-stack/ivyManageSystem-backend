"""崩潰防護（bug-hunt 2026-06-25）：events / punch-corrections / portal my-punch-corrections
/ exports calendar / exports employee-attendance 的 month / year query param 缺 ge/le。

問題：這幾個 GET 端點宣告 month / year 為無約束 int，handler body 只有 try/finally
（無 except ValueError），直接把值丟進 date(year, month, 1) / calendar.monthrange(year, month)。

- /api/events、/api/punch-corrections、/api/portal/my-punch-corrections：month / year 皆
  Query(None) 無約束，?month=13（year 同時 truthy）→ monthrange/date raise ValueError
  → 500 + Sentry 噪音；year=99999 → date(99999,...) 超出 date 上限 raise。
- /api/exports/calendar、/api/exports/employee-attendance：month 已有 ge=1/le=12，但
  year 為 Query(...) 無 ge/le（對比同檔 export_leaves 的 year=Query(..., ge=2000, le=2100)）
  → ?year=99999999 → date(year, month, 1) raise ValueError → 500。

修法：month: Query(..., ge=1, le=12)、year: Query(..., ge=2000, le=2100)
（FastAPI 在進 handler 前驗證 → 乾淨 422）。
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
from api.events import router as events_router
from api.exports import router as exports_router
from api.portal import router as portal_router
from api.punch_corrections import router as punch_corrections_router
from models.base import Base
from models.database import Employee, User
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "month-year-param.sqlite"
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

    with session_factory() as s:
        emp = Employee(
            employee_id="E_mp", name="員工", base_salary=30000, is_active=True
        )
        s.add(emp)
        s.flush()
        # 連結 employee_id 讓 portal /my-punch-corrections 的 _get_employee 取得到員工；
        # role=admin 同時滿足 exports/employee-attendance 的 self-or-full-salary 守衛。
        admin = User(
            username="param_admin",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["CALENDAR", "APPROVALS", "ATTENDANCE_READ"],
            employee_id=emp.id,
            is_active=True,
            must_change_password=False,
        )
        s.add(admin)
        s.commit()
        emp_id = emp.id

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(events_router)
    app.include_router(punch_corrections_router)
    app.include_router(portal_router)
    app.include_router(exports_router)

    # raise_server_exceptions=False：模擬 prod（有全域 handler）下未捕捉例外回 500，
    # 而非在測試端 re-raise。如此「越界 month/year 目前 500、修後 422」可清楚對比。
    with TestClient(app, raise_server_exceptions=False) as c:
        login = c.post(
            "/api/auth/login",
            json={"username": "param_admin", "password": "Temp123456"},
        )
        assert login.status_code == 200, login.text
        yield c, emp_id

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ---- month 越界（這三個端點僅在 year and month 皆 truthy 時用 monthrange，
#      故 month=0 為 falsy 不觸發崩潰；補上 ge/le 後 0 也應被 422 擋下）----
_MONTH_ENDPOINTS = [
    "/api/events?year=2026&month={m}",
    "/api/punch-corrections?year=2026&month={m}",
    "/api/portal/my-punch-corrections?year=2026&month={m}",
]


@pytest.mark.parametrize("url_tpl", _MONTH_ENDPOINTS)
@pytest.mark.parametrize("bad_month", [0, 13, -1, 99])
def test_list_endpoints_reject_out_of_range_month(client, url_tpl, bad_month):
    c, _ = client
    res = c.get(url_tpl.format(m=bad_month))
    assert (
        res.status_code == 422
    ), f"{url_tpl} month={bad_month} 應回 422，實得 {res.status_code}"


@pytest.mark.parametrize("url_tpl", _MONTH_ENDPOINTS)
@pytest.mark.parametrize("bad_year", [0, -1, 99999])
def test_list_endpoints_reject_out_of_range_year(client, url_tpl, bad_year):
    c, _ = client
    res = c.get(url_tpl.replace("year=2026", f"year={bad_year}").format(m=6))
    assert (
        res.status_code == 422
    ), f"{url_tpl} year={bad_year} 應回 422，實得 {res.status_code}"


# ---- exports：month 已約束，缺的是 year 範圍 ----
@pytest.mark.parametrize("bad_year", [0, -1, 99999999])
def test_export_calendar_rejects_out_of_range_year(client, bad_year):
    c, _ = client
    res = c.get(f"/api/exports/calendar?year={bad_year}&month=6")
    assert (
        res.status_code == 422
    ), f"export calendar year={bad_year} 應回 422，實得 {res.status_code}"


@pytest.mark.parametrize("bad_year", [0, -1, 99999999])
def test_export_employee_attendance_rejects_out_of_range_year(client, bad_year):
    c, emp_id = client
    res = c.get(
        f"/api/exports/employee-attendance?employee_id={emp_id}&year={bad_year}&month=6"
    )
    assert (
        res.status_code == 422
    ), f"export employee-attendance year={bad_year} 應回 422，實得 {res.status_code}"


def test_valid_month_year_still_works(client):
    """合法 month/year 不被誤擋（list 端點回 200）。"""
    c, _ = client
    assert c.get("/api/events?year=2026&month=6").status_code == 200
    assert c.get("/api/punch-corrections?year=2026&month=6").status_code == 200
    assert (
        c.get("/api/portal/my-punch-corrections?year=2026&month=6").status_code == 200
    )

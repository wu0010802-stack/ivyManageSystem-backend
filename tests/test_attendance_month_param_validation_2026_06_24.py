"""崩潰防護 P1：考勤/加班 GET 端點的 month query param 必須有 ge=1, le=12 約束。

問題：records / summary / calendar / overtimes 四個 GET 端點宣告 month 為無約束
int，handler body 只有 try/finally（無 except ValueError），直接把 month 丟進
date(year, month, 1) / calendar.monthrange(year, month)。打 ?month=13 → ValueError
逸出 → 500 + 每次噴 Sentry（fuzzer / 監控 / 前端手滑可觸發）。同 repo 的
reports.py:get_attendance_summary 旁邊的 calendar API 與 anomalies 已正確約束，屬
不一致遺漏。

修法：month: int = Query(..., ge=1, le=12)（FastAPI 在進 handler 前驗證 → 乾淨 422）。
year 也加合理範圍下限避免 date(0,...) 之類。
"""

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
from api.attendance import router as attendance_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.overtimes import router as overtimes_router
from models.base import Base
from models.database import Employee, User
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "month-param.sqlite"
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
        admin = User(
            username="month_admin",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["ATTENDANCE_READ", "OVERTIME_READ"],
            employee_id=None,
            is_active=True,
            must_change_password=False,
        )
        s.add(admin)
        s.commit()
        emp_id = emp.id

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(attendance_router)
    app.include_router(overtimes_router)

    # raise_server_exceptions=False：模擬 prod（有全域 handler）下未捕捉例外回 500，
    # 而非在測試端 re-raise。如此「month=13 目前 500、修後 422」可清楚對比。
    with TestClient(app, raise_server_exceptions=False) as c:
        login = c.post(
            "/api/auth/login",
            json={"username": "month_admin", "password": "Temp123456"},
        )
        assert login.status_code == 200, login.text
        yield c, emp_id

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.mark.parametrize("bad_month", [0, 13, -1, 99])
def test_records_rejects_out_of_range_month(client, bad_month):
    c, _ = client
    res = c.get(f"/api/attendance/records?year=2026&month={bad_month}")
    assert res.status_code == 422, f"month={bad_month} 應回 422，實得 {res.status_code}"


@pytest.mark.parametrize("bad_month", [0, 13, -1, 99])
def test_summary_rejects_out_of_range_month(client, bad_month):
    c, _ = client
    res = c.get(f"/api/attendance/summary?year=2026&month={bad_month}")
    assert res.status_code == 422, f"month={bad_month} 應回 422，實得 {res.status_code}"


@pytest.mark.parametrize("bad_month", [0, 13, -1, 99])
def test_calendar_rejects_out_of_range_month(client, bad_month):
    c, emp_id = client
    res = c.get(
        f"/api/attendance/calendar?employee_id={emp_id}&year=2026&month={bad_month}"
    )
    assert res.status_code == 422, f"month={bad_month} 應回 422，實得 {res.status_code}"


@pytest.mark.parametrize("bad_month", [13, -1, 99])
def test_overtimes_rejects_out_of_range_month(client, bad_month):
    c, _ = client
    # overtimes 僅在 year and month 皆 truthy 時用 monthrange；month=0 為 falsy 跳過，
    # 故不測 0（不會觸發 monthrange）。
    res = c.get(f"/api/overtimes?year=2026&month={bad_month}")
    assert res.status_code == 422, f"month={bad_month} 應回 422，實得 {res.status_code}"


@pytest.mark.parametrize("bad_year", [0, -1, 99999])
def test_endpoints_reject_out_of_range_year(client, bad_year):
    """year=0 / 負數 / 超大年同樣會讓 date(year, ...) 崩 → 須被 422 擋下。"""
    c, emp_id = client
    urls = [
        f"/api/attendance/records?year={bad_year}&month=6",
        f"/api/attendance/summary?year={bad_year}&month=6",
        f"/api/attendance/calendar?employee_id={emp_id}&year={bad_year}&month=6",
        f"/api/overtimes?year={bad_year}&month=6",
    ]
    for url in urls:
        res = c.get(url)
        assert res.status_code == 422, f"{url} 應回 422，實得 {res.status_code}"


def test_valid_month_still_works(client):
    """合法 month 不被誤擋（回 200，不論有無資料）。"""
    c, emp_id = client
    assert c.get("/api/attendance/records?year=2026&month=6").status_code == 200
    assert c.get("/api/attendance/summary?year=2026&month=6").status_code == 200
    assert (
        c.get(
            f"/api/attendance/calendar?employee_id={emp_id}&year=2026&month=6"
        ).status_code
        == 200
    )
    assert c.get("/api/overtimes?year=2026&month=6").status_code == 200

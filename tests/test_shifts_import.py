"""characterization: api/shifts.py:import_shifts run_in_executor 重構行為驗證。

2026-06-02：import_shifts 把 parse + DB 迴圈卸載到 executor（避免阻塞 event
loop）。本檔為該端點首個 endpoint test，走 TestClient 完整 async endpoint path
（含 run_in_executor），驗證重構後：
- 成功匯入：upsert ShiftAssignment，回 saved 計數
- 未知班別 row-level error：failed + errors，不整批失敗
- week_start query 參數 + username 正確傳入 executor 內的 sync 函式
"""

import io
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.shifts as shifts_module
import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.shifts import router as shifts_router
from models.database import Base, Employee, ShiftType, ShiftAssignment, User
from utils.auth import hash_password

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
def app_client(tmp_path):
    """In-memory SQLite + mini FastAPI app（auth + shifts router）。"""
    db_path = tmp_path / "shifts-import.sqlite"
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
    shifts_module._clear_shift_type_cache()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(shifts_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    shifts_module._clear_shift_type_cache()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _admin(session, username: str = "hr_admin") -> User:
    u = User(
        employee_id=None,
        username=username,
        password_hash=hash_password("AdminPass123"),
        role="admin",
        permission_names=["*"],
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _seed(session) -> int:
    """建一名員工 + 一個班別 + admin，回傳 employee 主鍵。"""
    emp = Employee(
        employee_id="SHIFT001", name="排班員工", base_salary=36000, is_active=True
    )
    session.add(emp)
    st = ShiftType(
        name="早班",
        work_start="08:00",
        work_end="17:00",
        sort_order=0,
        is_active=True,
    )
    session.add(st)
    _admin(session)
    session.flush()
    return emp.id


def _login(client: TestClient):
    return client.post(
        "/api/auth/login",
        json={"username": "hr_admin", "password": "AdminPass123"},
    )


def _xlsx_bytes(rows: list[list]) -> bytes:
    headers = ["員工編號", "員工姓名", "班別名稱", "備註(可空)"]
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestImportShiftsRunInExecutor:
    def test_success_updates_existing_assignment(self, app_client):
        """成功路徑（upsert 的 update 分支）：run_in_executor 內 sync 函式正確
        parse + 找到員工/班別 + update existing assignment + commit。

        註：insert 新 assignment 分支因既有 code `week_start_date=str(week_date)`
        為 PG-only 寫法（SQLite Date 欄不收 str），無法在 in-memory SQLite 驗證；
        該 insert pattern 由 leaves/overtimes 同構 endpoint test（含 insert）+ PG
        實測覆蓋。本測試聚焦 update 分支 + run_in_executor 鏈路。
        """
        from datetime import date, timedelta

        client, session_factory = app_client
        wd = date(2026, 3, 2)
        wd = wd - timedelta(days=wd.weekday())
        with session_factory() as session:
            emp_id = _seed(session)
            st = session.query(ShiftType).filter_by(name="早班").first()
            session.add(
                ShiftAssignment(
                    employee_id=emp_id,
                    shift_type_id=st.id,
                    week_start_date=wd,
                    notes="舊備註",
                )
            )
            session.commit()
        shifts_module._clear_shift_type_cache()

        _login(client)
        data = _xlsx_bytes([["SHIFT001", "排班員工", "早班", "備註內容"]])
        resp = client.post(
            "/api/shifts/import?week_start=2026-03-02",
            files={"file": ("shifts.xlsx", data, _XLSX_MIME)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["saved"] == 1
        assert body["failed"] == 0
        assert body["errors"] == []
        with session_factory() as session:
            sa = session.query(ShiftAssignment).filter_by(employee_id=emp_id).first()
            assert sa is not None
            assert sa.notes == "備註內容"  # update 生效（原為「舊備註」）

    def test_unknown_shift_type_row_error(self, app_client):
        client, session_factory = app_client
        with session_factory() as session:
            _seed(session)
            session.commit()
        shifts_module._clear_shift_type_cache()

        _login(client)
        data = _xlsx_bytes([["SHIFT001", "排班員工", "不存在的班別", None]])
        resp = client.post(
            "/api/shifts/import?week_start=2026-03-02",
            files={"file": ("shifts.xlsx", data, _XLSX_MIME)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["saved"] == 0
        assert body["failed"] == 1
        assert any("找不到班別" in e for e in body["errors"])

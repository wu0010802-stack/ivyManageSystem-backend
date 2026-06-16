"""回歸測試：補打卡端點（POST /api/attendance/record）改走 shift-aware 班別視窗

修補目標：create_or_update_attendance_record 原本呼叫
  recompute_attendance_status(work_start_str=employee.work_start_time, ...)
  只用員工預設工時（08:00），不查 DailyShift / ShiftAssignment。

匯入路徑（CSV/Excel）已改走 compute_status_for_employee_date，
但補打卡端點沒有同步，導致有排班教師（例如夜班 13:00-22:00）
補卡後狀態以 08:00 算 → 假遲到 302 分 / 錯扣款。

本測試重現此 bug（RED），再驗證修補後正確算出 2 分鐘遲到（GREEN）。
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
from api.auth import _account_failures, _ip_attempts, router as auth_router
from models.base import Base
from models.database import Attendance, Employee, User, ShiftType, DailyShift
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    """建立隔離的 SQLite 測試 app（shift-aware record 用）。"""
    from utils.cache_layer import reset_cache_for_testing

    # 重置 cache singleton，防止前一個測試把空 ShiftType dict 快取住
    reset_cache_for_testing()

    engine = create_engine(
        f"sqlite:///{tmp_path / 'shift_aware_record.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)
    old_e, old_s = base_module._engine, base_module._SessionFactory
    base_module._engine, base_module._SessionFactory = engine, sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(attendance_router)
    with TestClient(app) as c:
        yield c, sf
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine, base_module._SessionFactory = old_e, old_s
    engine.dispose()

    # 收尾重置 cache，防止把 ShiftType dict 污染後續測試
    reset_cache_for_testing()


def _seed(sf):
    """建立晚班教師 + DailyShift 排班 + 純 admin 帳號。"""
    with sf() as s:
        emp = Employee(
            employee_id="E01",
            name="晚班師",
            base_salary=30000,
            is_active=True,
            # 員工預設工時仍設 08:00，以確認修補前會產生 302 分鐘假遲到
            work_start_time="08:00",
            work_end_time="17:00",
        )
        s.add(emp)
        s.flush()

        st = ShiftType(
            name="晚班",
            work_start="13:00",
            work_end="22:00",
            is_active=True,
        )
        s.add(st)
        s.flush()

        # 2026-02-05 排晚班（DailyShift 優先級最高）
        s.add(
            DailyShift(
                employee_id=emp.id,
                shift_type_id=st.id,
                date=date(2026, 2, 5),
            )
        )

        # 純 admin（無 employee_id），不觸發自我守衛
        admin = User(
            username="pure_admin",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["ATTENDANCE_READ", "ATTENDANCE_WRITE"],
            employee_id=None,
            is_active=True,
            must_change_password=False,
        )
        s.add(admin)
        s.commit()
        return emp.id


def test_record_uses_daily_shift_not_default_0800(client):
    """補打卡端點應查 DailyShift 取班別起迄；晚班 13:00 打卡 13:02 → 遲到 2 分鐘。

    Bug：修補前用 employee.work_start_time or "08:00"，
         晚班教師 13:02 打卡被算成 (13:02 - 08:00) = 302 分鐘遲到。
    """
    c, sf = client
    emp_id = _seed(sf)

    login_res = c.post(
        "/api/auth/login",
        json={"username": "pure_admin", "password": "Temp123456"},
    )
    assert login_res.status_code == 200, login_res.text

    res = c.post(
        "/api/attendance/record",
        json={
            "employee_id": emp_id,
            "date": "2026-02-05",
            "punch_in": "13:02",
            "punch_out": "22:00",
        },
    )
    assert res.status_code in (200, 201), res.text

    with sf() as s:
        att = (
            s.query(Attendance)
            .filter_by(employee_id=emp_id, attendance_date=date(2026, 2, 5))
            .one()
        )
        assert att.is_late is True, f"期望 is_late=True，實際 is_late={att.is_late}"
        assert att.late_minutes == 2, (
            f"期望 late_minutes=2（晚班 13:00，打卡 13:02），"
            f"實際 late_minutes={att.late_minutes}"
            f"（Bug：可能是相對 08:00 的 302 分）"
        )
        assert (
            att.is_early_leave is False
        ), f"22:00 下班準時，不應 early_leave；實際={att.is_early_leave}"

"""回歸測試 bh-attendance #2：複合考勤 status 串接超過 DB 欄位上限 + legacy 寫入失敗被靜默吞掉。

背景：
- 考勤 status 為「開放複合值域」，utils/attendance_calc.py、services/attendance_parser.py、
  api/attendance/upload.py 以 '+' 串接多個旗標，最長如
  'late+early_leave+missing_punch_out'（34 字），超過原 `attendances.status` VARCHAR(20)。
  在 PostgreSQL 上寫入即 `value too long for type character varying(20)`（DataError）。
- 更糟的是 api/attendance/upload.py 舊格式（legacy）寫入分支的 `except Exception`
  只 `logger.error` 後吞掉，接著 fall-through 回傳由 summary_df 組的「成功」摘要
  → DB 其實一筆都沒存（rollback）卻回報 200 成功，造成靜默資料遺失。

本檔兩支測試：
1. test_attendance_status_column_widened：schema 斷言欄位已放寬到可容納 34 字複合值
   （>= 40 或 Text）。SQLite 不檢查 VARCHAR 長度，故不能靠「塞長字串會炸」測；用
   schema 斷言證明 model 定義已加長。
2. test_legacy_commit_failure_is_reraised_not_reported_success：模擬 legacy 寫入時 DB
   寫入失敗（DataError），斷言回應為 500（例外被 re-raise）而非 200 假成功。
"""

import io
import os
import sys
from datetime import date, datetime, time
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import String, Text, create_engine
from sqlalchemy.exc import DataError
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.attendance import router as attendance_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.base import Base
from models.database import Attendance, Employee, User
from utils.auth import hash_password


# ──────────────────────────────────────────────────────────────────────────
# 1. Schema 斷言：status 欄位足以容納最長複合值
# ──────────────────────────────────────────────────────────────────────────
def test_attendance_status_column_widened():
    """attendances.status 必須能容納 'late+early_leave+missing_punch_out'（34 字）。

    SQLite 不檢查 VARCHAR 長度，無法用寫入觸發；改以 model schema 斷言證明已加長。
    """
    col_type = Attendance.__table__.c.status.type
    if isinstance(col_type, Text):
        # Text 無上限，直接通過
        return
    assert isinstance(col_type, String)
    assert col_type.length is not None and col_type.length >= 40, (
        "attendances.status 長度不足以容納最長複合值 "
        "'late+early_leave+missing_punch_out'（34 字），需 >= 40 或改 Text，"
        f"目前 length={col_type.length}"
    )


# ──────────────────────────────────────────────────────────────────────────
# 2. legacy 寫入失敗必須 re-raise（不可回報成功）
# ──────────────────────────────────────────────────────────────────────────
@pytest.fixture
def upload_client(tmp_path):
    db_path = tmp_path / "att-status-len.sqlite"
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
    app.include_router(attendance_router)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_admin_and_employee(sf):
    with sf() as s:
        emp = Employee(
            employee_id="E_admin_statuslen",
            name="管理員",
            base_salary=30000,
            employee_type="regular",
            is_active=True,
        )
        s.add(emp)
        target = Employee(
            employee_id="E_target_statuslen",
            name="張小明",
            base_salary=28000,
            employee_type="regular",
            is_active=True,
        )
        s.add(target)
        s.flush()
        user = User(
            username="admin_statuslen_upload",
            password_hash=hash_password("Temp123456"),
            role="admin",
            permission_names=["ATTENDANCE_READ", "ATTENDANCE_WRITE"],
            employee_id=None,  # 純 admin 走 unrestricted 路徑
            is_active=True,
            must_change_password=False,
        )
        s.add(user)
        s.commit()


def _login(client: TestClient):
    return client.post(
        "/api/auth/login",
        json={"username": "admin_statuslen_upload", "password": "Temp123456"},
    )


def _legacy_format_xlsx_bytes() -> bytes:
    """僅含「姓名/時間」欄（無「上班時間/下班時間」）→ 走舊格式分支。"""
    df = pd.DataFrame({"姓名": ["張小明"], "時間": ["2026-02-02 08:00:00"]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def _fake_parser_result_with_one_row():
    """模擬 parse_attendance_file 回傳一筆會走進 DB 寫入迴圈的舊格式結果。"""
    a_date = date(2026, 2, 2)
    detail = {
        "date": a_date,
        "punch_in": time(8, 30),
        "punch_out": time(16, 0),
        "punch_in_dt": datetime(2026, 2, 2, 8, 30),
        "punch_out_dt": datetime(2026, 2, 2, 16, 0),
        # 最長複合值（34 字）：正是觸發 value-too-long 的字串
        "status": "late+early_leave+missing_punch_out",
        "is_late": True,
        "is_early_leave": True,
        "is_missing_punch_in": False,
        "is_missing_punch_out": True,
        "late_minutes": 30,
        "early_minutes": 60,
    }
    result = SimpleNamespace(details=[detail])
    results = {"張小明": result}
    summary_df = pd.DataFrame([{"姓名": "張小明", "遲到": 1}])
    anomaly_df = pd.DataFrame()
    return results, anomaly_df, summary_df


def test_legacy_commit_failure_is_reraised_not_reported_success(upload_client):
    """legacy 寫入若 DB 失敗（如 status 超長 DataError），必須回 500 而非假成功。

    修補前：except Exception 吞掉 → fall-through 回 200 + summary「成功 1 人」。
    修補後：commit 失敗 re-raise → raise_safe_500 → 500。
    """
    client, sf = upload_client
    _seed_admin_and_employee(sf)
    assert _login(client).status_code == 200

    results = _fake_parser_result_with_one_row()

    def _fake_parser(file_or_buffer, *a, **kw):
        return results

    # 模擬 legacy 寫入路徑中 DB 寫入失敗：_mark_attendance_upload_stale 緊接在
    # session.commit() 之前、與其同屬一個 try，拋 DataError 等同 PG 上 status 超長。
    def _boom(*a, **kw):
        raise DataError(
            "UPDATE attendances ...",
            {},
            Exception("value too long for type character varying(20)"),
        )

    with (
        patch(
            "services.attendance_parser.parse_attendance_file",
            side_effect=_fake_parser,
        ),
        patch(
            "api.attendance.upload._mark_attendance_upload_stale",
            side_effect=_boom,
        ),
    ):
        res = client.post(
            "/api/attendance/upload",
            files={
                "file": (
                    "legacy.xlsx",
                    _legacy_format_xlsx_bytes(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )

    assert res.status_code == 500, (
        "legacy 寫入 DB 失敗時必須 re-raise 成 500，"
        f"而非吞掉回報成功。實際 status={res.status_code}, body={res.text}"
    )
    # 確認 DB 真的沒存到任何考勤（rollback 生效）
    with sf() as s:
        assert s.query(Attendance).count() == 0

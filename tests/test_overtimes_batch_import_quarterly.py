"""admin batch import /overtimes/import 季 138h cap per-row 擋下驗證。

驗證：
- seed W1 (M-2 ~ M) = 130h approved（mock monthly cap off 後），
  batch import 3 筆，第 3 筆觸發 quarterly cap raise。
- 整批 HTTP 200（batch import 採 partial-success 模式，不整批 400）。
- 第 3 筆回報 failed，errors 含 "138"。
- DB 確認：違規那筆未插入（但合法筆已插入）。

實際行為：batch import endpoint 內部以 per-row except Exception 吸收
HTTPException，cumulate results["failed"]，session.commit() 最後提交成功筆。
因此本測試不斷言整批 400，而是斷言 per-row enforcement 正確擋下並回報。

與 Task 4 不同：本檔測 HTTP endpoint path（TestClient），
而非 service function mock-verifying unit test。
"""

import io
import os
import sys
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.overtimes as overtimes_module
import models.base as base_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.overtimes import router as overtimes_router
from models.database import Base, Employee, OvertimeRecord, User
from utils.auth import hash_password

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """In-memory SQLite + mini FastAPI app（auth + overtimes router）。"""
    db_path = tmp_path / "batch-quarterly.sqlite"
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

    # salary_engine/line_service 不需要真實實作
    fake_salary_engine = MagicMock()
    monkeypatch.setattr(overtimes_module, "_salary_engine", fake_salary_engine)
    # 2026-05-25 後 api.overtimes 不再有 _line_service module 變數（改走 dispatch.enqueue）

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(overtimes_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _emp(session, employee_id: str = "BIQT001", name: str = "季測員工") -> Employee:
    e = Employee(
        employee_id=employee_id,
        name=name,
        base_salary=36000,
        is_active=True,
    )
    session.add(e)
    session.flush()
    return e


def _admin(session, username: str = "hr_admin") -> User:
    """純管理員（employee_id=None，不觸發自我核准守衛）。"""
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


def _seed_approved_ot(
    session, emp_id: int, ot_date: date, hours: float
) -> OvertimeRecord:
    """建已核准的加班紀錄（不走 endpoint，直接寫 DB）。"""
    ot = OvertimeRecord(
        employee_id=emp_id,
        overtime_date=ot_date,
        overtime_type="weekday",
        hours=hours,
        overtime_pay=0.0,
        status="approved",
    )
    session.add(ot)
    session.flush()
    return ot


def _login(
    client: TestClient, username: str = "hr_admin", password: str = "AdminPass123"
):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


def _xlsx_bytes(rows: list[list]) -> bytes:
    """產生符合 import-template header 的 xlsx bytes。"""
    headers = [
        "員工編號",
        "員工姓名",
        "加班日期",
        "加班類型",
        "時數",
        "開始時間(可空)",
        "結束時間(可空)",
        "原因(可空)",
        "補休(是/否,可空)",
    ]
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ── Tests ────────────────────────────────────────────────────────────────────


class TestBatchImportQuarterlyCapPerRow:
    """batch import /overtimes/import：per-row 季 138h cap 擋下（mock monthly off）。

    驗證 defense-in-depth：admin import 路徑一樣受 quarterly cap 守衛，
    違規筆回報 failed + error 含 "138"，合法筆正常插入。
    """

    def test_batch_import_quarterly_cap_blocks_offending_row(
        self, app_client, monkeypatch
    ):
        """seed W1 (3~5月) = 130h，import 3 筆，第 3 筆觸發 quarterly raise。

        expected：
        - HTTP 200（partial-success 模式）
        - results["failed"] >= 1（第 3 筆 blocked）
        - errors 中含 "138" 字樣
        - 前 2 筆 DB 已插入，第 3 筆未插入（rollback per-row）
        """
        client, session_factory = app_client
        with session_factory() as session:
            emp = _emp(session)
            _admin(session)
            emp_id = emp.id
            emp_code = emp.employee_id
            emp_name = emp.name
            # seed W1 (2026/03~2026/05) = 130h approved
            _seed_approved_ot(session, emp_id, date(2026, 3, 5), 45.0)
            _seed_approved_ot(session, emp_id, date(2026, 4, 5), 45.0)
            _seed_approved_ot(session, emp_id, date(2026, 5, 5), 40.0)
            session.commit()

        before_count = _count_ot(session_factory, emp_id)

        assert _login(client).status_code == 200

        # import 3 筆
        # 第 1、2 筆各 3h → W1+3=133 < 138，合法
        # 第 3 筆 6h → W1+6+前兩筆已 flush 但 import 第 3 筆時累計 130+3+3+6=142 > 138
        # mock monthly cap off（otherwise 5/5 已 40h, +3 = 43h < 46h, +6 = 49h > 46h → 月上限先擋）
        rows = [
            [emp_code, emp_name, "2026-05-20", "weekday", 3, None, None, "加班1", "否"],
            [emp_code, emp_name, "2026-05-21", "weekday", 3, None, None, "加班2", "否"],
            [
                emp_code,
                emp_name,
                "2026-05-22",
                "weekday",
                6,
                None,
                None,
                "加班3（超季）",
                "否",
            ],
        ]
        xlsx = _xlsx_bytes(rows)

        with patch(
            "api.overtimes._check_monthly_overtime_cap",
            return_value=None,
        ):
            resp = client.post(
                "/api/overtimes/import",
                files={
                    "file": (
                        "import.xlsx",
                        xlsx,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )

        assert (
            resp.status_code == 200
        ), f"expected 200 partial-success, got {resp.status_code}: {resp.text}"
        body = resp.json()

        # 只有第 3 筆超季，前 2 筆應成功；精確驗 failed == 1
        assert body.get("failed", 0) == 1, f"expected failed==1, got: {body}"

        # errors 含 "138"
        errors = body.get("errors", [])
        assert any(
            "138" in err for err in errors
        ), f"expected error string containing '138', got errors={errors}"

        # DB：前 2 筆應已插入，第 3 筆（hours=6 on 2026-05-22）不應插入
        after_count = _count_ot(session_factory, emp_id)
        # seed had 3 rows; 2 valid import rows should be inserted
        inserted = after_count - before_count
        assert (
            inserted == 2
        ), f"expected 2 valid rows inserted (before={before_count}, after={after_count})"

        # 確認第 3 筆（hours=6, date=2026-05-22）沒有插入
        with session_factory() as session:
            row3 = (
                session.query(OvertimeRecord)
                .filter(
                    OvertimeRecord.employee_id == emp_id,
                    OvertimeRecord.overtime_date == date(2026, 5, 22),
                    OvertimeRecord.hours == 6.0,
                )
                .first()
            )
        assert (
            row3 is None
        ), "第 3 筆違規行不應插入 DB（quarterly cap rollback per-row）"


def _count_ot(session_factory, emp_id: int) -> int:
    with session_factory() as session:
        return (
            session.query(OvertimeRecord)
            .filter(OvertimeRecord.employee_id == emp_id)
            .count()
        )

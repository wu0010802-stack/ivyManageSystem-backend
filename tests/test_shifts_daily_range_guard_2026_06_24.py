"""崩潰防護 P2：GET /api/shifts/daily 日期範圍須有上限 + row cap，防整表載入 OOM。

問題：start_date/end_date 為 caller 任意指定的裸字串，query 無 .limit()（全 repo 唯一
漏掉 5000 安全網的列表端點）。傳 2000-01-01~2100-12-31 即載入歷來所有 DailyShift +
兩個 eager join 物件。另：date.fromisoformat(壞字串) 會 ValueError → 500（try/finally
無 except）。

修法：① 壞日期格式 → 400；② end<start → 400；③ 範圍 > 366 天 → 400（排班檢視最長一年）；
④ query 補 .limit() 安全網（對齊本檔其他列表端點慣例）。
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
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.shifts import router as shifts_router
from models.base import Base
from models.database import User
from utils.auth import hash_password


@pytest.fixture
def client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'shifts.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sf = sessionmaker(bind=engine)
    old_e, old_s = base_module._engine, base_module._SessionFactory
    base_module._engine, base_module._SessionFactory = engine, sf
    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    with sf() as s:
        s.add(
            User(
                username="sched_admin",
                password_hash=hash_password("Temp123456"),
                role="admin",
                permission_names=["SCHEDULE"],
                employee_id=None,
                is_active=True,
                must_change_password=False,
            )
        )
        s.commit()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(shifts_router)

    with TestClient(app, raise_server_exceptions=False) as c:
        login = c.post(
            "/api/auth/login",
            json={"username": "sched_admin", "password": "Temp123456"},
        )
        assert login.status_code == 200, login.text
        yield c

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine, base_module._SessionFactory = old_e, old_s
    engine.dispose()


def test_daily_rejects_oversized_range(client):
    res = client.get("/api/shifts/daily?start_date=2000-01-01&end_date=2100-12-31")
    assert res.status_code == 400, f"超寬範圍應 400，實得 {res.status_code}"


def test_daily_rejects_bad_date_format(client):
    res = client.get("/api/shifts/daily?start_date=not-a-date&end_date=2026-06-30")
    assert res.status_code == 400, f"壞日期格式應 400，實得 {res.status_code}"


def test_daily_rejects_end_before_start(client):
    res = client.get("/api/shifts/daily?start_date=2026-06-30&end_date=2026-06-01")
    assert res.status_code == 400, f"end<start 應 400，實得 {res.status_code}"


def test_daily_normal_range_ok(client):
    res = client.get("/api/shifts/daily?start_date=2026-06-01&end_date=2026-06-30")
    assert res.status_code == 200, res.text
    assert res.json() == []

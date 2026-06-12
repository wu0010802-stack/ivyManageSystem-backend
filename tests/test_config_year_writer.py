"""tests/test_config_year_writer.py — config_year writer 蓋章回歸（TDD 2026-06-12）

Why: cfgyear01 之後 reader（services/salary/config_resolver）以
`config_year == 年度` 解析 AttendancePolicy / PositionSalaryConfig（不看
is_active、缺年度列即 PayrollConfigMissingError → calculate/simulate 422），
但三個 writer 落 model default 0：

- PUT /api/config/attendance-policy：新版本列 config_year=0 → 引擎永遠撿舊列，
  UI 改考勤政策後「新值靜默失效」。
- PUT /api/config/position-salary：同款；空表 insert 分支也寫 0。
- startup/seed.py seed_default_configs：全新部署 seed 出 config_year=0
  → 結薪直接 422 且無 API 自救。

修法（對照組：BonusConfig writer 的 config_year 在 _BONUS_FIELDS 且 schema 可寫）：
Update schema 加 optional config_year；未帶時 stamp「當前台北年度」；帶
config_year=2027 即建立 2027 年度列（行政建立新年度設定的入口）。
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
import api.config as config_module
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.config import router as config_router
from models.base import Base
from models.database import AttendancePolicy, PositionSalaryConfig, User
from services.salary.config_resolver import resolve_config
from utils.auth import hash_password
from utils.taipei_time import today_taipei

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures（沿用 test_employee_config_stale_marking.py 的 client pattern）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cfg_client(tmp_path):
    db_path = tmp_path / "cfgyear.sqlite"
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

    fake_engine = MagicMock()
    config_module.init_config_services(fake_engine, MagicMock())

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


def _login_admin(client, sf, username="admin", password="AdminPass123"):
    with sf() as session:
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
    res = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text


# ─────────────────────────────────────────────────────────────────────────────
# AttendancePolicy writer
# ─────────────────────────────────────────────────────────────────────────────


def test_put_attendance_policy_stamps_current_year_and_resolvable(cfg_client):
    """PUT 未帶 config_year → 新版本列蓋章當前台北年度，resolver 撿得到新值。"""
    client, sf = cfg_client
    _login_admin(client, sf)

    res = client.put("/api/config/attendance-policy", json={"festival_bonus_months": 6})
    assert res.status_code == 200, res.text

    year = today_taipei().year
    with sf() as s:
        row = resolve_config(s, AttendancePolicy, year, year_col="config_year")
        assert row.config_year == year
        assert row.festival_bonus_months == 6


def test_put_attendance_policy_explicit_year_creates_year_row(cfg_client):
    """PUT 帶 config_year=2077 → 建立 2077 年度列；當前年度列不受影響。"""
    client, sf = cfg_client
    _login_admin(client, sf)

    res = client.put("/api/config/attendance-policy", json={"festival_bonus_months": 3})
    assert res.status_code == 200, res.text
    res = client.put(
        "/api/config/attendance-policy",
        json={"config_year": 2077, "festival_bonus_months": 9},
    )
    assert res.status_code == 200, res.text

    year = today_taipei().year
    with sf() as s:
        future = resolve_config(s, AttendancePolicy, 2077, year_col="config_year")
        assert future.config_year == 2077
        assert future.festival_bonus_months == 9
        current = resolve_config(s, AttendancePolicy, year, year_col="config_year")
        assert current.festival_bonus_months == 3


def test_put_attendance_policy_no_zero_year_rows(cfg_client):
    """PUT 後不得殘留 config_year=0 的列（reader 永遠撿不到的死資料）。"""
    client, sf = cfg_client
    _login_admin(client, sf)

    res = client.put("/api/config/attendance-policy", json={"festival_bonus_months": 4})
    assert res.status_code == 200, res.text
    with sf() as s:
        zero_rows = (
            s.query(AttendancePolicy).filter(AttendancePolicy.config_year == 0).count()
        )
        assert zero_rows == 0


# ─────────────────────────────────────────────────────────────────────────────
# PositionSalaryConfig writer
# ─────────────────────────────────────────────────────────────────────────────


def test_put_position_salary_stamps_current_year_and_resolvable(cfg_client):
    """空表 insert 分支：PUT 未帶 config_year → 蓋章當前年度，resolver 撿得到。"""
    client, sf = cfg_client
    _login_admin(client, sf)

    res = client.put("/api/config/position-salary", json={"driver": 31000})
    assert res.status_code == 200, res.text

    year = today_taipei().year
    with sf() as s:
        row = resolve_config(s, PositionSalaryConfig, year, year_col="config_year")
        assert row.config_year == year
        assert float(row.driver) == 31000


def test_put_position_salary_explicit_year_creates_year_row(cfg_client):
    """PUT 帶 config_year=2077 → 建立 2077 列；當前年度列保留舊值（歷史重算不漂移）。"""
    client, sf = cfg_client
    _login_admin(client, sf)

    res = client.put("/api/config/position-salary", json={"driver": 31000})
    assert res.status_code == 200, res.text
    res = client.put(
        "/api/config/position-salary", json={"config_year": 2077, "driver": 32000}
    )
    assert res.status_code == 200, res.text

    year = today_taipei().year
    with sf() as s:
        future = resolve_config(s, PositionSalaryConfig, 2077, year_col="config_year")
        assert float(future.driver) == 32000
        current = resolve_config(s, PositionSalaryConfig, year, year_col="config_year")
        assert float(current.driver) == 31000


def test_put_position_salary_same_year_updates_in_place(cfg_client):
    """同年度重複 PUT → 該年度 version 遞增、值更新（不爆 row 數）。"""
    client, sf = cfg_client
    _login_admin(client, sf)

    res = client.put("/api/config/position-salary", json={"driver": 31000})
    assert res.status_code == 200, res.text
    res = client.put("/api/config/position-salary", json={"driver": 31500})
    assert res.status_code == 200, res.text

    year = today_taipei().year
    with sf() as s:
        row = resolve_config(s, PositionSalaryConfig, year, year_col="config_year")
        assert float(row.driver) == 31500
        assert (
            s.query(PositionSalaryConfig)
            .filter(PositionSalaryConfig.config_year == year)
            .count()
            == 1
        )


# ─────────────────────────────────────────────────────────────────────────────
# startup seed
# ─────────────────────────────────────────────────────────────────────────────


def test_seed_attendance_policy_stamps_current_year(tmp_path):
    """全新部署 seed 出來的 AttendancePolicy 必須 config_year=當年（否則結薪 422）。"""
    from startup.seed import seed_default_configs

    db_path = tmp_path / "seed.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    try:
        Base.metadata.create_all(engine)
        seed_default_configs()
        year = today_taipei().year
        with session_factory() as s:
            row = resolve_config(s, AttendancePolicy, year, year_col="config_year")
            assert row.config_year == year
    finally:
        base_module._engine = old_engine
        base_module._SessionFactory = old_session_factory
        engine.dispose()

"""Appraisal + year_end 共用測試 fixtures（SQLite in-memory）。

SQLite 相容性修補（必須在匯入任何模型前執行）：
1. JSONB → JSON（year_end / score_items 的 calc_meta、attachments 等）
2. BigInteger → Integer（PK 自動遞增）

修補成功後 fixture 提供：
- `db_session` —— in-memory SQLite session，已植入 16 條 catalog + 10 條 bonus_rates
- `admin_user`, `admin_headers` —— 全權限管理員
- `appraisal_app` —— FastAPI app（含 auth + appraisal + year_end router）
- `client` —— TestClient
- `employee_factory` —— 建 Employee 工廠
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── SQLite 相容性修補（必須在匯入 SQLAlchemy 模型前執行）────────────────────
import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects

_pg_dialects.JSONB = _sa.JSON  # type: ignore[assignment]


class _SQLiteInteger(_sa.Integer):
    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]
# ─────────────────────────────────────────────────────────────────────────────

import pytest
from datetime import date
from decimal import Decimal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.base as base_module
from models.base import Base

from models.database import *  # noqa: F401, F403
import models.appraisal  # noqa: F401
import models.year_end  # noqa: F401

from models.appraisal import (
    AppraisalBonusRate,
    AppraisalScoreItemCatalog,
    Grade,
    RoleGroup,
    ScoreItemSign,
)
from models.auth import User
from models.employee import Employee, JobTitle

from utils.auth import create_access_token, hash_password
from utils.permissions import Permission

from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.appraisal import appraisal_router
from api.year_end import year_end_router


# ── Seed 資料（與 migration 同步） ───────────────────────────────────────────

CATALOG_SEED = [
    ("LEAVE", "請休假", "MINUS", 1.0, "leaves"),
    ("LATE_EARLY", "遲到早退", "MINUS", 0.5, "attendance"),
    ("NO_CLOCK", "未打卡", "MINUS", 0.5, "attendance"),
    ("MISS_PRESCHOOL_MEETING", "園務會議未參加", "MINUS", 1.0, "meetings"),
    ("ORG_MEETING_0913", "9/13 機構會議", "MINUS", 1.0, "manual"),
    ("ORG_MEETING_1115", "11/15 機構會議", "MINUS", 1.0, "manual"),
    ("TEAM_ACTIVITY_1115", "11/15 自強活動", "MINUS", 1.0, "manual"),
    ("DROPOUT_0915", "9/15 休學人數", "MINUS", 1.0, "students"),
    ("DROPOUT_0315", "3/15 休學人數", "MINUS", 1.0, "students"),
    ("CHILD_INCIDENT", "幼兒意外", "MINUS", 1.0, "manual"),
    ("RETURNING_RATE_0315", "3/15 舊生註冊率", "PLUS", 1.0, "students"),
    ("CLASS_SIZE", "帶班人數加分", "PLUS", 1.0, "classroom"),
    ("AFTER_CLASS_RATE", "才藝班參加率", "PLUS", 1.0, "activity"),
    ("SPED", "特教生加分", "PLUS", 2.0, "students"),
    ("REWARD_PUNISH", "獎懲（大過/嘉獎）", "BOTH", 1.0, "manual"),
    ("OTHER_ADJUST", "其他主管調整", "BOTH", 1.0, "manual"),
]

BONUS_RATES_SEED = [
    ("2025-08-01", "SUPERVISOR", "OUTSTANDING", 10000),
    ("2025-08-01", "SUPERVISOR", "GOOD", 5000),
    ("2025-08-01", "HEAD_TEACHER", "OUTSTANDING", 8000),
    ("2025-08-01", "HEAD_TEACHER", "GOOD", 4000),
    ("2025-08-01", "ASSISTANT", "OUTSTANDING", 6000),
    ("2025-08-01", "ASSISTANT", "GOOD", 3500),
    ("2025-08-01", "STAFF", "OUTSTANDING", 6000),
    ("2025-08-01", "STAFF", "GOOD", 3500),
    ("2025-08-01", "COOK", "OUTSTANDING", 6000),
    ("2025-08-01", "COOK", "GOOD", 3500),
]


def _seed_catalog(session):
    for idx, (code, label, sign, weight, ds) in enumerate(CATALOG_SEED):
        session.add(
            AppraisalScoreItemCatalog(
                code=code,
                label=label,
                sign=ScoreItemSign(sign),
                default_weight=Decimal(str(weight)),
                data_source=ds,
                display_order=(idx + 1) * 10,
                is_active=True,
            )
        )
    session.flush()


def _seed_bonus_rates(session):
    for eff, rg, gr, amt in BONUS_RATES_SEED:
        session.add(
            AppraisalBonusRate(
                effective_from=date.fromisoformat(eff),
                role_group=RoleGroup(rg),
                grade=Grade(gr),
                base_amount=Decimal(str(amt)),
            )
        )
    session.flush()


# ── App / DB fixture ──────────────────────────────────────────────────────


@pytest.fixture(scope="function")
def appraisal_app(tmp_path):
    db_path = tmp_path / "appraisal_test.sqlite"
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

    with session_factory() as seed_session:
        _seed_catalog(seed_session)
        _seed_bonus_rates(seed_session)
        seed_session.commit()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(appraisal_router)
    app.include_router(year_end_router)

    yield app, session_factory, engine

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture(scope="function")
def client(appraisal_app):
    app, sf, engine = appraisal_app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def db_session(appraisal_app):
    app, sf, engine = appraisal_app
    with sf() as session:
        yield session


# ── User fixtures ─────────────────────────────────────────────────────────


_ADMIN_PERMS = -1


@pytest.fixture(scope="function")
def admin_user(db_session):
    u = User(
        username="admin_test",
        password_hash=hash_password("Test1234!"),
        role="admin",
        permissions=_ADMIN_PERMS,
        employee_id=None,
        is_active=True,
        must_change_password=False,
    )
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture(scope="function")
def admin_headers(admin_user):
    token = create_access_token(
        {
            "user_id": admin_user.id,
            "id": admin_user.id,
            "role": admin_user.role,
            "permissions": admin_user.permissions,
            "employee_id": admin_user.employee_id,
        }
    )
    return {"Authorization": f"Bearer {token}"}


# ── Employee factory ──────────────────────────────────────────────────────


@pytest.fixture(scope="function")
def employee_factory(db_session):
    counter = [0]

    def _factory(
        *,
        name: str | None = None,
        is_active: bool = True,
        resign_date=None,
        hire_date=None,
        base_salary: Decimal = Decimal("0"),
    ) -> Employee:
        counter[0] += 1
        emp_id = f"EMP_TEST_{counter[0]:04d}"
        if name is None:
            name = f"測試員工{counter[0]:04d}"
        emp = Employee(
            employee_id=emp_id,
            name=name,
            employee_type="regular",
            is_active=is_active,
            resign_date=resign_date,
            hire_date=hire_date,
            base_salary=base_salary,
        )
        db_session.add(emp)
        db_session.commit()
        db_session.refresh(emp)
        return emp

    return _factory

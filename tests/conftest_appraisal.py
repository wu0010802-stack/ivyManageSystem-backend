"""tests/conftest_appraisal.py — 考核系統測試 fixtures

作法：
  - 在 conftest.py 層級之外，自成一模組，由各 test_appraisal_*.py 透過
    conftest.py 的 pytest plugin 機制自動載入（放在 tests/ 下即可被 pytest 蒐集）。
  - SQLite in-memory 取代 PostgreSQL，與 test_employees.py 等現有測試一致。

SQLite 相容性修補（必須在匯入任何模型前執行）：
  1. JSONB → JSON：AppraisalEvent.attachments 使用 PG JSONB；SQLite 不支援，
     需在匯入前將 sqlalchemy.dialects.postgresql.JSONB 替換為 sqlalchemy.JSON。
  2. BigInteger PK → Integer：SQLite 僅對 INTEGER PRIMARY KEY 自動遞增；
     BigInteger 對應到 BIGINT，不觸發 AUTOINCREMENT，需替換為 Integer。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── SQLite 相容性修補（必須在匯入任何 SQLAlchemy 模型前執行）──────────────
import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects

# 1. JSONB → JSON（AppraisalEvent.attachments）
_pg_dialects.JSONB = _sa.JSON  # type: ignore[assignment]


# 2. BigInteger → Integer（appraisal 表的 id 欄位）
class _SQLiteInteger(_sa.Integer):
    """替代 BigInteger 使 SQLite 可自動遞增主鍵。"""

    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]
# ─────────────────────────────────────────────────────────────────────────────

import pytest
from datetime import date, datetime, timezone
from decimal import Decimal
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models.base as base_module
from models.base import Base

# 匯入所有模型以確保 Base.metadata 完整（順序重要：先 database 再 appraisal）
from models.database import *  # noqa: F401, F403
import models.appraisal  # noqa: F401  確保 appraisal 表被登記進 metadata

from models.appraisal import (
    AppraisalBonusRate,
    AppraisalCycle,
    AppraisalEvent,
    AppraisalParticipant,
    AppraisalPenaltyCatalogItem,
    AppraisalSummary,
    CatalogCategory,
    CycleStatus,
    EventType,
    Grade,
    RoleGroup,
    Semester,
    SummaryStatus,
)
from models.auth import User
from models.employee import Employee, JobTitle

from utils.auth import create_access_token, hash_password
from utils.permissions import Permission

from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from api.appraisal import appraisal_router

# ── 種子資料（與 migration 一致） ──────────────────────────────────────────

CATALOG_SEED = [
    # (code, category, subcategory, description, default_event_type,
    #  default_score_delta, severity_max, display_order)
    (
        "MISCONDUCT_LEAVE_CLASSROOM",
        "MISCONDUCT",
        "離開教室",
        "離開教室時未委請其他老師代為看顧，將幼生獨留在教室",
        "WARNING",
        -2.0,
        1,
        10,
    ),
    (
        "MISCONDUCT_PARENT_COMPLAINT_WITHDRAWAL",
        "MISCONDUCT",
        "親師溝通無果致退學",
        "家長抱怨老師教保不力，親師溝通無果，導致家長決定讓幼生休退學",
        "WARNING",
        -2.0,
        1,
        11,
    ),
    (
        "MISCONDUCT_INTIMIDATION_VOLUME",
        "MISCONDUCT",
        "恐嚇-音量過大",
        "對孩子說話的音量大至樓上(下)都知悉",
        "WARNING",
        -2.0,
        1,
        12,
    ),
    (
        "MISCONDUCT_INTIMIDATION_VERBAL",
        "MISCONDUCT",
        "恐嚇-言語",
        "言語恐嚇孩子",
        "MINOR_DEMERIT",
        -3.0,
        1,
        13,
    ),
    (
        "MISCONDUCT_INTIMIDATION_ISOLATION",
        "MISCONDUCT",
        "隔離至教室外",
        "為處罰而隔離孩子到教室以外的空間",
        "MINOR_DEMERIT",
        -3.0,
        1,
        14,
    ),
    (
        "MISCONDUCT_PHYSICAL_HARM",
        "MISCONDUCT",
        "身心痛苦/侵害",
        "讓幼兒身心遭遇痛苦或侵害",
        "WARNING",
        -2.0,
        3,
        15,
    ),
    (
        "MISCONDUCT_CORPORAL_PUNISHMENT",
        "MISCONDUCT",
        "體罰",
        "依家長知情/反應遞增",
        "SCORE_ADJUST",
        -3.0,
        5,
        16,
    ),
    (
        "MISCONDUCT_VIOLENCE",
        "MISCONDUCT",
        "暴力管教",
        "以暴力管教小孩",
        "MAJOR_DEMERIT",
        -6.0,
        1,
        17,
    ),
    (
        "MEDICATION_SELF_SERVE",
        "MEDICATION",
        "讓幼生自行服藥",
        "未依規定協助餵藥",
        "ORAL_WARNING",
        0.0,
        1,
        20,
    ),
    (
        "MEDICATION_WRONG_NO_SYMPTOM",
        "MEDICATION",
        "餵錯藥-無症狀",
        "未依指示餵藥或餵錯藥，幼生無症狀",
        "ORAL_WARNING",
        0.0,
        3,
        21,
    ),
    (
        "MEDICATION_WRONG_WITH_SYMPTOM",
        "MEDICATION",
        "餵錯藥-有症狀",
        "未依指示餵藥或餵錯藥，幼生有症狀",
        "SCORE_ADJUST",
        -3.0,
        5,
        22,
    ),
    (
        "ACCIDENT_MINOR_NO_SUTURE",
        "ACCIDENT",
        "輕傷無縫合",
        "幼生意外送醫無縫合",
        "SCORE_ADJUST",
        -1.0,
        5,
        30,
    ),
    (
        "ACCIDENT_MINOR_WITH_SUTURE",
        "ACCIDENT",
        "輕傷有縫合",
        "幼生意外送醫有縫合",
        "SCORE_ADJUST",
        -5.0,
        5,
        31,
    ),
    (
        "ACCIDENT_SEVERE",
        "ACCIDENT",
        "重傷需就醫",
        "幼生受重傷（需就醫）",
        "WARNING",
        -2.0,
        5,
        32,
    ),
    (
        "DISPUTE_VERBAL_RESOLVED",
        "DISPUTE",
        "口角和解",
        "口頭吵架未影響校譽，雙方和解",
        "ORAL_WARNING",
        0.0,
        1,
        40,
    ),
    (
        "DISPUTE_VERBAL_DAMAGE",
        "DISPUTE",
        "口角影響校譽",
        "口頭吵架有影響校譽",
        "SCORE_ADJUST",
        -2.0,
        1,
        41,
    ),
    (
        "DISPUTE_PHYSICAL",
        "DISPUTE",
        "肢體衝突",
        "肢體衝突",
        "SCORE_ADJUST",
        -2.0,
        4,
        42,
    ),
    (
        "NEGLIGENCE_ACCOUNTING",
        "NEGLIGENCE",
        "行政會計疏失",
        "薪資核算錯誤等",
        "WARNING",
        -2.0,
        1,
        50,
    ),
    (
        "NEGLIGENCE_KITCHEN",
        "NEGLIGENCE",
        "廚房疏失",
        "餐點量不足等",
        "WARNING",
        -2.0,
        3,
        51,
    ),
    (
        "NEGLIGENCE_DRIVER",
        "NEGLIGENCE",
        "司機疏失",
        "違反交通條例等",
        "MINOR_DEMERIT",
        -3.0,
        4,
        52,
    ),
    (
        "NEGLIGENCE_DRESS_CODE",
        "NEGLIGENCE",
        "未依規定穿著",
        "員工未依規定穿著服裝",
        "ORAL_WARNING",
        0.0,
        1,
        53,
    ),
    (
        "NEGLIGENCE_DOC_LATE",
        "NEGLIGENCE",
        "文件未按時繳交",
        "員工文件未按時繳交",
        "ORAL_WARNING",
        0.0,
        1,
        54,
    ),
    ("MERIT_COMMENDATION", "MERIT", "嘉獎", "嘉獎", "COMMENDATION", 2.0, 1, 60),
    ("MERIT_MINOR", "MERIT", "小功", "小功", "MINOR_MERIT", 3.0, 1, 61),
    ("MERIT_MAJOR", "MERIT", "大功", "大功", "MAJOR_MERIT", 6.0, 1, 62),
    (
        "SPECIAL_RECOMMENDATION",
        "SPECIAL",
        "主管推薦優異",
        "單位主管呈報表現優異人員",
        "SCORE_ADJUST",
        2.0,
        1,
        70,
    ),
    (
        "SPECIAL_SPECIAL_NEEDS",
        "SPECIAL",
        "班級特教生",
        "班級有政府核定補助的特教生",
        "SCORE_ADJUST",
        2.0,
        1,
        71,
    ),
    (
        "SPECIAL_SEED_INSTRUCTOR",
        "SPECIAL",
        "內部種子講師",
        "經機構檢定為內部種子講師",
        "SCORE_ADJUST",
        2.0,
        1,
        72,
    ),
    (
        "SPECIAL_ART_CLASS_FULL_TERM",
        "SPECIAL",
        "才藝班全期授課",
        "參與課後才藝課全期授課者",
        "SCORE_ADJUST",
        2.0,
        1,
        73,
    ),
]

BONUS_RATES_SEED = [
    # (effective_from, role_group, grade, base_amount)
    ("2026-08-01", "SUPERVISOR", "OUTSTANDING", 10000),
    ("2026-08-01", "SUPERVISOR", "GOOD", 5000),
    ("2026-08-01", "HEAD_TEACHER", "OUTSTANDING", 8000),
    ("2026-08-01", "HEAD_TEACHER", "GOOD", 4000),
    ("2026-08-01", "ASSISTANT", "OUTSTANDING", 6000),
    ("2026-08-01", "ASSISTANT", "GOOD", 3500),
]


def _seed_catalog(session):
    """植入 29 條懲處事由目錄（對應 migration T3）。"""
    for code, cat, sub, desc, evt, score, sev, ord_ in CATALOG_SEED:
        item = AppraisalPenaltyCatalogItem(
            code=code,
            category=CatalogCategory(cat),
            subcategory=sub,
            description=desc,
            default_event_type=EventType(evt),
            default_score_delta=Decimal(str(score)),
            severity_max=sev,
            display_order=ord_,
            is_active=True,
        )
        session.add(item)
    session.flush()


def _seed_bonus_rates(session, admin_user_id=None):
    """植入 6 筆考核獎金率（對應 migration T4）。"""
    for eff, rg, gr, amt in BONUS_RATES_SEED:
        rate = AppraisalBonusRate(
            effective_from=date.fromisoformat(eff),
            role_group=RoleGroup(rg),
            grade=Grade(gr),
            base_amount=Decimal(str(amt)),
            created_by=admin_user_id,
        )
        session.add(rate)
    session.flush()


# ── 主要 fixture：appraisal_client ─────────────────────────────────────────


@pytest.fixture(scope="function")
def appraisal_app(tmp_path):
    """建立獨立 SQLite in-memory 環境，掛載 auth + appraisal router。

    與 test_employees.py 相同的 base_module swap 模式。
    每個測試函數重建，確保隔離。
    """
    db_path = tmp_path / "appraisal_test.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    # 保存原始 engine/session（測試結束後恢復）
    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    # 建立所有表格
    Base.metadata.create_all(engine)

    # 清除 rate-limit 狀態
    _ip_attempts.clear()
    _account_failures.clear()

    # 植入種子資料（catalog + bonus_rates）
    with session_factory() as seed_session:
        _seed_catalog(seed_session)
        _seed_bonus_rates(seed_session)
        seed_session.commit()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(appraisal_router)

    yield app, session_factory, engine

    # 清理
    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


@pytest.fixture(scope="function")
def client(appraisal_app):
    """TestClient，已掛載 appraisal_app。"""
    app, sf, engine = appraisal_app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="function")
def db_session(appraisal_app):
    """可直接操作 DB 的 SQLAlchemy session（用於建立 fixtures 物件或查詢驗證）。"""
    app, sf, engine = appraisal_app
    with sf() as session:
        yield session


@pytest.fixture(scope="function")
def session_factory(appraisal_app):
    """回傳 session_factory（需要自行管理 session 生命週期的 fixture 使用）。"""
    app, sf, engine = appraisal_app
    return sf


# ── 使用者 fixtures ─────────────────────────────────────────────────────────


def _make_user(
    session,
    *,
    username: str,
    role: str,
    permissions: int,
    employee_id: int | None = None,
) -> User:
    """通用建立 User helper。"""
    u = User(
        username=username,
        password_hash=hash_password("Test1234!"),
        role=role,
        permissions=permissions,
        employee_id=employee_id,
        is_active=True,
        must_change_password=False,
    )
    session.add(u)
    session.flush()
    return u


def _make_token(user: User) -> str:
    """為 User 建立 JWT access token。

    注意：appraisal routers 使用 current_user["id"]（非 "user_id"）；
    此處同時寫入兩個 key 確保相容（"id" 用於 appraisal router，
    "user_id" 用於 get_current_user 的 DB 查詢鏈）。
    """
    return create_access_token(
        {
            "user_id": user.id,
            "id": user.id,  # appraisal routers 使用 current_user["id"]
            "role": user.role,
            "permissions": user.permissions,
            "employee_id": user.employee_id,
        }
    )


# ── 全權限 admin ──────────────────────────────────────────────────────────

# Permission.ALL = 0xFFFFFFFFFFFFFFFF 在 SQLite 的有號 64-bit 整數中會溢位；
# 改用 -1（有號等價值），與 User.permissions 欄位儲存 BigInteger(signed) 一致。
_ADMIN_PERMS = -1


@pytest.fixture(scope="function")
def admin_user(db_session):
    """全權限 admin 使用者（無對應員工，純管理帳號）。"""
    u = _make_user(
        db_session,
        username="admin_test",
        role="admin",
        permissions=_ADMIN_PERMS,
        employee_id=None,
    )
    db_session.commit()
    return u


@pytest.fixture(scope="function")
def admin_headers(admin_user):
    """admin JWT headers。"""
    token = _make_token(admin_user)
    return {"Authorization": f"Bearer {token}"}


# ── supervisor（第一階簽核） ──────────────────────────────────────────────

_SUPERVISOR_PERMS = int(
    Permission.APPRAISAL_READ
    | Permission.APPRAISAL_EVENT_WRITE
    | Permission.APPRAISAL_REVIEW
    | Permission.SETTINGS_WRITE
)


@pytest.fixture(scope="function")
def supervisor_user(db_session):
    """supervisor 使用者（role=supervisor，有 APPRAISAL_REVIEW）。"""
    u = _make_user(
        db_session,
        username="supervisor_test",
        role="supervisor",
        permissions=_SUPERVISOR_PERMS,
    )
    db_session.commit()
    return u


@pytest.fixture(scope="function")
def supervisor_headers(supervisor_user):
    """supervisor JWT headers。"""
    token = _make_token(supervisor_user)
    return {"Authorization": f"Bearer {token}"}


# ── accountant（第二階簽核） ──────────────────────────────────────────────

_ACCOUNTANT_PERMS = int(Permission.APPRAISAL_READ | Permission.APPRAISAL_ACCOUNTING)


@pytest.fixture(scope="function")
def accountant_user(db_session):
    """accountant 使用者（role=hr，有 APPRAISAL_ACCOUNTING）。"""
    u = _make_user(
        db_session, username="accountant_test", role="hr", permissions=_ACCOUNTANT_PERMS
    )
    db_session.commit()
    return u


@pytest.fixture(scope="function")
def accountant_headers(accountant_user):
    """accountant JWT headers。"""
    token = _make_token(accountant_user)
    return {"Authorization": f"Bearer {token}"}


# ── principal（第三階簽核 + cycle 操作） ────────────────────────────────────

_PRINCIPAL_PERMS = int(
    Permission.APPRAISAL_READ
    | Permission.APPRAISAL_REVIEW
    | Permission.APPRAISAL_ACCOUNTING
    | Permission.APPRAISAL_FINALIZE
    | Permission.SETTINGS_WRITE
)


@pytest.fixture(scope="function")
def principal_user(db_session):
    """principal 使用者（role=admin，有 APPRAISAL_FINALIZE）。"""
    u = _make_user(
        db_session,
        username="principal_test",
        role="admin",
        permissions=_PRINCIPAL_PERMS,
    )
    db_session.commit()
    return u


@pytest.fixture(scope="function")
def principal_headers(principal_user):
    """principal JWT headers。"""
    token = _make_token(principal_user)
    return {"Authorization": f"Bearer {token}"}


# ── teacher（只有 APPRAISAL_EVENT_WRITE） ───────────────────────────────────
#  注意：require_staff_permission 對 role="teacher" 回 403。
#  test_create_event_自己不能登自己 需要「有 employee_id 的 supervisor/hr，
#  不是 teacher role」。這裡 teacher_user 使用 role="supervisor" 但帶 employee_id。

_TEACHER_PERMS = int(Permission.APPRAISAL_READ | Permission.APPRAISAL_EVENT_WRITE)


@pytest.fixture(scope="function")
def teacher_employee(db_session):
    """教師對應的 Employee 記錄。"""
    emp = Employee(
        employee_id="TEACHER001",
        name="教師測試員工",
        employee_type="regular",
        is_active=True,
    )
    db_session.add(emp)
    db_session.flush()
    db_session.commit()
    return emp


@pytest.fixture(scope="function")
def teacher_user(db_session, teacher_employee):
    """與 teacher_employee 關聯的使用者（role=supervisor，有 employee_id 用於自登守衛）。"""
    u = _make_user(
        db_session,
        username="teacher_test",
        role="supervisor",
        permissions=_TEACHER_PERMS,
        employee_id=teacher_employee.id,
    )
    db_session.commit()
    return u


@pytest.fixture(scope="function")
def teacher_headers(teacher_user):
    """teacher JWT headers。"""
    token = _make_token(teacher_user)
    return {"Authorization": f"Bearer {token}"}


# ── regular_user（無 appraisal 權限） ────────────────────────────────────────


@pytest.fixture(scope="function")
def regular_user(db_session):
    """無 appraisal 權限的普通使用者（用於 403 測試）。"""
    u = _make_user(
        db_session,
        username="regular_test",
        role="hr",
        permissions=int(Permission.EMPLOYEES_READ),
    )
    db_session.commit()
    return u


@pytest.fixture(scope="function")
def regular_user_headers(regular_user):
    """regular_user JWT headers。"""
    token = _make_token(regular_user)
    return {"Authorization": f"Bearer {token}"}


# ── Employee factory ──────────────────────────────────────────────────────

_emp_counter = 0


@pytest.fixture(scope="function")
def employee_factory(db_session):
    """回傳工廠函式，可建立 Employee。

    用法：
        e = employee_factory(is_active=True)
        e = employee_factory(job_title_name="園長", is_active=True)
        e = employee_factory(is_active=False, resign_date=some_date)
    """
    created: list[Employee] = []
    counter = [0]

    def _factory(
        *,
        name: str | None = None,
        is_active: bool = True,
        resign_date=None,
        job_title_name: str | None = None,
    ) -> Employee:
        counter[0] += 1
        emp_id = f"EMP_TEST_{counter[0]:04d}"
        if name is None:
            name = f"測試員工{counter[0]:04d}"

        job_title_id = None
        if job_title_name:
            from sqlalchemy import select

            jt = db_session.execute(
                select(JobTitle).where(JobTitle.name == job_title_name)
            ).scalar_one_or_none()
            if jt is None:
                jt = JobTitle(name=job_title_name, is_active=True)
                db_session.add(jt)
                db_session.flush()
            job_title_id = jt.id

        emp = Employee(
            employee_id=emp_id,
            name=name,
            employee_type="regular",
            is_active=is_active,
            resign_date=resign_date,
            job_title_id=job_title_id,
        )
        db_session.add(emp)
        # commit 讓 FastAPI router 開的新 session 也能看到此員工
        db_session.commit()
        db_session.refresh(emp)
        created.append(emp)
        return emp

    yield _factory

    # cleanup 已由 SQLite file 自動處理


# ── AppraisalCycle fixtures ───────────────────────────────────────────────


@pytest.fixture(scope="function")
def sample_cycle(db_session, admin_user):
    """基礎 OPEN 週期。

    使用 115 第二學期（2026-02-01 ~ 2026-07-31）使得 event_date=date.today()
    在 2026-05-11 附近的測試可以正常落在 cycle 範圍內。
    cycles 測試不依賴這裡的具體日期（start/end），故安全修改。
    """
    cycle = AppraisalCycle(
        academic_year=115,
        semester=Semester.SECOND,
        start_date=date(2026, 2, 1),
        end_date=date(2026, 7, 31),
        base_score_calc_date=date(2026, 3, 15),
        status=CycleStatus.OPEN,
        created_by=admin_user.id,
    )
    db_session.add(cycle)
    db_session.commit()
    db_session.refresh(cycle)
    return cycle


@pytest.fixture(scope="function")
def sample_closed_cycle(db_session, admin_user):
    """CLOSED 狀態的週期（用於測試封存後不可修改）。"""
    cycle = AppraisalCycle(
        academic_year=113,
        semester=Semester.FIRST,
        start_date=date(2024, 8, 1),
        end_date=date(2025, 1, 31),
        base_score_calc_date=date(2024, 9, 15),
        status=CycleStatus.CLOSED,
        created_by=admin_user.id,
    )
    db_session.add(cycle)
    db_session.commit()
    db_session.refresh(cycle)
    return cycle


@pytest.fixture(scope="function")
def locked_cycle_with_participants(db_session, admin_user, employee_factory):
    """LOCKED 週期 + 3 位參與者（不同 role_group + base_score）。"""
    cycle = AppraisalCycle(
        academic_year=114,
        semester=Semester.SECOND,
        start_date=date(2026, 2, 1),
        end_date=date(2026, 7, 31),
        base_score_calc_date=date(2026, 3, 15),
        status=CycleStatus.LOCKED,
        created_by=admin_user.id,
    )
    db_session.add(cycle)
    db_session.flush()

    for i, (role, score) in enumerate(
        [
            (RoleGroup.SUPERVISOR, Decimal("92")),
            (RoleGroup.HEAD_TEACHER, Decimal("85")),
            (RoleGroup.ASSISTANT, Decimal("78")),
        ]
    ):
        emp = employee_factory(name=f"鎖定週期員工{i+1}")
        p = AppraisalParticipant(
            cycle_id=cycle.id,
            employee_id=emp.id,
            role_group=role,
            base_score=score,
        )
        db_session.add(p)

    db_session.commit()
    db_session.refresh(cycle)
    return cycle


# ── 含 summary 的 finalized cycle（用於 close cycle 測試）───────────────────


@pytest.fixture(scope="function")
def sample_cycle_all_finalized(db_session, admin_user):
    """LOCKED 週期 + 1 位參與者，其 summary 已 FINALIZED（用於成功 close 測試）。"""
    cycle = AppraisalCycle(
        academic_year=112,
        semester=Semester.FIRST,
        start_date=date(2023, 8, 1),
        end_date=date(2024, 1, 31),
        base_score_calc_date=date(2023, 9, 15),
        status=CycleStatus.LOCKED,
        created_by=admin_user.id,
    )
    db_session.add(cycle)
    db_session.flush()

    emp = Employee(
        employee_id="EMP_FINALL_001",
        name="全部完成員工",
        employee_type="regular",
        is_active=True,
    )
    db_session.add(emp)
    db_session.flush()

    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        base_score=Decimal("90"),
    )
    db_session.add(p)
    db_session.flush()

    summary = AppraisalSummary(
        participant_id=p.id,
        cycle_id=cycle.id,
        base_score=Decimal("90"),
        event_score_sum=Decimal("0"),
        total_score=Decimal("90"),
        grade=Grade.OUTSTANDING,
        status=SummaryStatus.FINALIZED,
        finalized_at=datetime.now(timezone.utc),
        finalized_by=admin_user.id,
    )
    db_session.add(summary)
    db_session.commit()
    db_session.refresh(cycle)
    return cycle


@pytest.fixture(scope="function")
def sample_cycle_with_unfinalized_summary(db_session, admin_user):
    """LOCKED 週期 + 1 位參與者，其 summary 為 DRAFT（用於 close 失敗測試）。"""
    cycle = AppraisalCycle(
        academic_year=111,
        semester=Semester.FIRST,
        start_date=date(2022, 8, 1),
        end_date=date(2023, 1, 31),
        base_score_calc_date=date(2022, 9, 15),
        status=CycleStatus.LOCKED,
        created_by=admin_user.id,
    )
    db_session.add(cycle)
    db_session.flush()

    emp = Employee(
        employee_id="EMP_UNFIN_001",
        name="未完成員工",
        employee_type="regular",
        is_active=True,
    )
    db_session.add(emp)
    db_session.flush()

    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        base_score=Decimal("80"),
    )
    db_session.add(p)
    db_session.flush()

    summary = AppraisalSummary(
        participant_id=p.id,
        cycle_id=cycle.id,
        base_score=Decimal("80"),
        event_score_sum=Decimal("0"),
        total_score=Decimal("80"),
        grade=Grade.GOOD,
        status=SummaryStatus.DRAFT,  # 未 FINALIZED
    )
    db_session.add(summary)
    db_session.commit()
    db_session.refresh(cycle)
    return cycle


# ── Participant fixtures ──────────────────────────────────────────────────


@pytest.fixture(scope="function")
def participant(db_session, sample_cycle, admin_user):
    """單一參與者（HEAD_TEACHER，base_score=85），隸屬於 sample_cycle。"""
    emp = Employee(
        employee_id="EMP_PART_001",
        name="參與者測試員工",
        employee_type="regular",
        is_active=True,
    )
    db_session.add(emp)
    db_session.flush()

    p = AppraisalParticipant(
        cycle_id=sample_cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        base_score=Decimal("85"),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture(scope="function")
def teacher_participant(db_session, sample_cycle, teacher_employee):
    """teacher_user 對應員工的 participant（用於自登守衛測試）。"""
    p = AppraisalParticipant(
        cycle_id=sample_cycle.id,
        employee_id=teacher_employee.id,
        role_group=RoleGroup.HEAD_TEACHER,
        base_score=Decimal("85"),
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


# ── Event fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="function")
def existing_event(db_session, participant, admin_user):
    """已存在的 COMMENDATION 事件，隸屬於 participant。

    event_date 選用 sample_cycle 範圍內（115-SECOND: 2026-02-01 ~ 2026-07-31）的日期。
    """
    ev = AppraisalEvent(
        participant_id=participant.id,
        cycle_id=participant.cycle_id,
        event_type=EventType.COMMENDATION,
        event_date=date(2026, 3, 1),  # sample_cycle 115-SECOND 範圍內
        score_delta=Decimal("2.0"),
        title="測試嘉獎事件",
        created_by=admin_user.id,
        attachments=[],
    )
    db_session.add(ev)
    db_session.commit()
    db_session.refresh(ev)
    return ev


@pytest.fixture(scope="function")
def participant_with_event(db_session, participant, existing_event):
    """含有事件的參與者（用於刪除時回 409 測試）。"""
    return participant


# ── Summary fixtures ──────────────────────────────────────────────────────


def _make_summary(
    db_session,
    *,
    participant,
    status,
    admin_id,
    base=Decimal("85"),
    event_sum=Decimal("5"),
):
    """通用 summary 建立 helper。"""
    total = base + event_sum
    grade = Grade.OUTSTANDING if total >= 90 else Grade.GOOD
    s = AppraisalSummary(
        participant_id=participant.id,
        cycle_id=participant.cycle_id,
        base_score=base,
        event_score_sum=event_sum,
        total_score=total,
        grade=grade,
        status=status,
    )
    if status in (
        SummaryStatus.SUPERVISOR_SIGNED,
        SummaryStatus.ACCOUNTING_SIGNED,
        SummaryStatus.FINALIZED,
    ):
        s.supervisor_signed_at = datetime.now(timezone.utc)
        s.supervisor_signed_by = admin_id
        s.supervisor_comment = "主管簽核"
    if status in (SummaryStatus.ACCOUNTING_SIGNED, SummaryStatus.FINALIZED):
        s.accounting_signed_at = datetime.now(timezone.utc)
        s.accounting_signed_by = admin_id
        s.accounting_comment = "會計核數"
    if status == SummaryStatus.FINALIZED:
        s.finalized_at = datetime.now(timezone.utc)
        s.finalized_by = admin_id
        s.finalized_comment = "已核定"
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture(scope="function")
def draft_summary(db_session, participant, admin_user):
    """DRAFT 狀態的 summary（base=85 + event_sum=5 → total=90 OUTSTANDING）。"""
    return _make_summary(
        db_session,
        participant=participant,
        status=SummaryStatus.DRAFT,
        admin_id=admin_user.id,
    )


@pytest.fixture(scope="function")
def signed_summary(db_session, participant, admin_user):
    """SUPERVISOR_SIGNED 狀態的 summary。"""
    return _make_summary(
        db_session,
        participant=participant,
        status=SummaryStatus.SUPERVISOR_SIGNED,
        admin_id=admin_user.id,
    )


@pytest.fixture(scope="function")
def accounting_signed_summary(db_session, admin_user, sample_cycle):
    """ACCOUNTING_SIGNED 狀態的 summary（獨立 participant，避免與其他 summary fixture 衝突）。"""
    emp = Employee(
        employee_id="EMP_ACCT_SUM_001",
        name="會計簽核測試員工",
        employee_type="regular",
        is_active=True,
    )
    db_session.add(emp)
    db_session.flush()

    p = AppraisalParticipant(
        cycle_id=sample_cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        base_score=Decimal("85"),
    )
    db_session.add(p)
    db_session.flush()

    return _make_summary(
        db_session,
        participant=p,
        status=SummaryStatus.ACCOUNTING_SIGNED,
        admin_id=admin_user.id,
    )


@pytest.fixture(scope="function")
def finalized_summary(db_session, admin_user, sample_cycle):
    """FINALIZED 狀態的 summary（獨立 participant）。"""
    emp = Employee(
        employee_id="EMP_FIN_SUM_001",
        name="已定稿測試員工",
        employee_type="regular",
        is_active=True,
    )
    db_session.add(emp)
    db_session.flush()

    p = AppraisalParticipant(
        cycle_id=sample_cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.SUPERVISOR,
        base_score=Decimal("92"),
    )
    db_session.add(p)
    db_session.flush()

    s = _make_summary(
        db_session,
        participant=p,
        status=SummaryStatus.FINALIZED,
        admin_id=admin_user.id,
    )
    # finalized_summary.cycle_id 要能被 test_recompute_FINALIZED_summary_被擋 使用
    return s


# ── finalized_cycle（報表測試用）──────────────────────────────────────────


@pytest.fixture(scope="function")
def finalized_cycle(db_session, admin_user):
    """CLOSED 週期 + 1 位 FINALIZED summary 的參與者（報表匯出測試用）。"""
    cycle = AppraisalCycle(
        academic_year=110,
        semester=Semester.FIRST,
        start_date=date(2021, 8, 1),
        end_date=date(2022, 1, 31),
        base_score_calc_date=date(2021, 9, 15),
        status=CycleStatus.CLOSED,
        created_by=admin_user.id,
    )
    db_session.add(cycle)
    db_session.flush()

    emp = Employee(
        employee_id="EMP_RPT_001",
        name="報表測試員工",
        employee_type="regular",
        is_active=True,
    )
    db_session.add(emp)
    db_session.flush()

    p = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=emp.id,
        role_group=RoleGroup.HEAD_TEACHER,
        base_score=Decimal("88"),
    )
    db_session.add(p)
    db_session.flush()

    summary = AppraisalSummary(
        participant_id=p.id,
        cycle_id=cycle.id,
        base_score=Decimal("88"),
        event_score_sum=Decimal("2"),
        total_score=Decimal("90"),
        grade=Grade.OUTSTANDING,
        status=SummaryStatus.FINALIZED,
        supervisor_signed_at=datetime.now(timezone.utc),
        supervisor_signed_by=admin_user.id,
        supervisor_comment="主管核簽",
        accounting_signed_at=datetime.now(timezone.utc),
        accounting_signed_by=admin_user.id,
        accounting_comment="會計核數",
        finalized_at=datetime.now(timezone.utc),
        finalized_by=admin_user.id,
        finalized_comment="已核定 110 年度",
    )
    db_session.add(summary)
    db_session.commit()
    db_session.refresh(cycle)
    return cycle

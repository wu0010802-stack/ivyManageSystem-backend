"""scripts/seed/_common.py — 冷門模組 seed 共用 helper。

匯入方向單向:本檔(及 scripts/seed/*)依賴主腳本 scripts.seed_test_data_114_2 的純 helper，
主腳本不反向依賴本套件，避免循環 import。

提供:
- session_scope（短交易；每個 step 自行 with session_scope() as session）
- 學年日期錨點:YEAR_START/YEAR_END/TERM1/TERM2/TODAY
- 純 helper 轉出:_random_name/_random_phone/_date_range/_is_workday + 姓名常數
- 取資料:get_active_students / get_active_employees / get_admin_user / get_classrooms
- rand_date_between(a, b)
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 觸發所有 model 載入(避免 cross-table FK 解析失敗)
import models.database  # noqa: F401,E402
from models.base import session_scope  # noqa: E402,F401
from models.auth import User  # noqa: E402
from models.employee import Employee  # noqa: E402
from models.classroom import Classroom, Student  # noqa: E402

# 主腳本的純 helper / 姓名常數（單一來源，勿在此重複定義）
from scripts.seed_test_data_114_2 import (  # noqa: E402,F401
    _date_range,
    _is_workday,
    _random_name,
    _random_phone,
    SURNAMES,
    GIVEN_NAMES_BOY,
    GIVEN_NAMES_GIRL,
)

# ===== 學年日期錨點（全 114 學年）=====
ACADEMIC_YEAR = 114
YEAR_START = date(2025, 8, 1)
YEAR_END = date(2026, 7, 31)
TERM1 = (date(2025, 8, 1), date(2026, 1, 20))  # 上學期
TERM2 = (date(2026, 2, 1), date(2026, 6, 5))  # 下學期（不生未來，上限=今天）
TODAY = date(2026, 6, 5)


def get_active_students(session, limit: int | None = None):
    q = (
        session.query(Student).filter(Student.is_active == True).order_by(Student.id)
    )  # noqa: E712
    if limit:
        q = q.limit(limit)
    return q.all()


def get_active_employees(session):
    return (
        session.query(Employee)
        .filter(Employee.is_active == True)  # noqa: E712
        .order_by(Employee.id)
        .all()
    )


def get_admin_user(session):
    """取 admin user（provenance 用，例如 recorded_by / created_by）。找不到回 None。"""
    return (
        session.query(User).filter_by(username="admin").first()
        or session.query(User).filter_by(role="admin").first()
        or session.query(User).first()
    )


def get_classrooms(session):
    return (
        session.query(Classroom)
        .filter(Classroom.is_active == True)  # noqa: E712
        .order_by(Classroom.id)
        .all()
    )


def rand_date_between(a: date, b: date) -> date:
    """[a, b] 之間隨機一天（含端點）。a>b 時回 a。"""
    span = (b - a).days
    if span <= 0:
        return a
    return a + timedelta(days=random.randint(0, span))


def rand_datetime_between(a: date, b: date) -> datetime:
    d = rand_date_between(a, b)
    return datetime(
        d.year, d.month, d.day, random.randint(8, 17), random.choice([0, 15, 30, 45])
    )

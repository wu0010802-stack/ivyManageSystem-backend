"""TDD：_build_employee_resolver / _build_classroom_resolver 重名歧義修法驗證。

P1 bug（qa-loop 2026-06-17）：
  舊實作用 {e.name: e.id for e in employees} 建 dict——重複 key 靜默保留最後一筆，
  重名員工（在職＋離職）或重名班級時，resolver 會把年終結算/特別獎金 silently 掛到
  同名的另一人。

修法後規則（_build_employee_resolver）：
  1. 唯一姓名           → 回該 id（唯一、無歧義）
  2. 多名中恰一位在職   → 回在職者 id（在職新人＋同名離職老同仁是常態）
  3. 多名中 0 或 ≥2 在職 → None（歧義不可解析，讓 skipped_unresolved_names 回報 HR）

修法後規則（_build_classroom_resolver）：
  1. 唯一姓名 → 回該 id
  2. 多個同名 → None（無 is_active 偏好→歧義，依 Classroom.is_active 做相同偏好）

測試規格：
  - test_employee_resolver_unique            — 唯一姓名 → 正確 id
  - test_employee_resolver_active_wins       — 在職＋離職同名 → 在職 id（修前 dict 可能回離職者）
  - test_employee_resolver_two_active_ambiguous — 兩在職同名 → None（修前回某 id，此為 RED 前標靶）
  - test_employee_resolver_unknown           — 不存在姓名 → None
  - test_classroom_resolver_unique           — 唯一班名 → 正確 id
  - test_classroom_resolver_ambiguous        — 兩個同名班（不同學年/學期）→ None
  - test_classroom_resolver_active_wins      — 同名一個 active 一個 inactive → 回 active id
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models.base import Base
from models.classroom import Classroom
from models.employee import Employee
from api.year_end import _build_employee_resolver, _build_classroom_resolver

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def _make_employee(session, emp_id: int, name: str, is_active: bool) -> Employee:
    e = Employee(
        id=emp_id,
        employee_id=f"EMP{emp_id:04d}",
        name=name,
        is_active=is_active,
    )
    session.add(e)
    session.flush()
    return e


def _make_classroom(
    session,
    room_id: int,
    name: str,
    school_year: int = 114,
    semester: int = 1,
    is_active: bool = True,
) -> Classroom:
    r = Classroom(
        id=room_id,
        name=name,
        school_year=school_year,
        semester=semester,
        is_active=is_active,
    )
    session.add(r)
    session.flush()
    return r


# ──────────────────────────────────────────────
# Employee resolver tests
# ──────────────────────────────────────────────


def test_employee_resolver_unique(test_db_session):
    """唯一姓名 → 回正確 id。"""
    session = test_db_session
    _make_employee(session, 1, "王唯一", True)
    session.commit()

    resolver = _build_employee_resolver(session)
    assert resolver("王唯一") == 1


def test_employee_resolver_unknown(test_db_session):
    """不存在姓名 → None。"""
    session = test_db_session
    _make_employee(session, 1, "王存在", True)
    session.commit()

    resolver = _build_employee_resolver(session)
    assert resolver("王不存在") is None


def test_employee_resolver_active_wins(test_db_session):
    """在職（id=10）＋離職同名（id=20）→ 回在職者 id=10。

    修前：dict {name: id} last-wins，結果依 query 順序不定，可能回 20（離職者）。
    修後：恰一位在職 → 回在職 id。
    """
    session = test_db_session
    _make_employee(session, 10, "林重名", True)  # 在職
    _make_employee(session, 20, "林重名", False)  # 離職
    session.commit()

    resolver = _build_employee_resolver(session)
    assert resolver("林重名") == 10, "應回在職者 id=10，不應回離職者 id=20"


def test_employee_resolver_two_active_ambiguous(test_db_session):
    """兩位**都在職**同名 → None（歧義不可解析）。

    ⭐ 這是修前 RED 標靶：舊 dict 會回某一個 id 而非 None。
    """
    session = test_db_session
    _make_employee(session, 30, "陳歧義", True)
    _make_employee(session, 31, "陳歧義", True)
    session.commit()

    resolver = _build_employee_resolver(session)
    assert resolver("陳歧義") is None, "兩位在職同名應回 None（歧義）"


def test_employee_resolver_two_inactive_ambiguous(test_db_session):
    """兩位**都離職**同名 → None（無在職可偏好，歧義）。"""
    session = test_db_session
    _make_employee(session, 40, "趙離職", False)
    _make_employee(session, 41, "趙離職", False)
    session.commit()

    resolver = _build_employee_resolver(session)
    assert resolver("趙離職") is None, "兩位離職同名應回 None（歧義）"


# ──────────────────────────────────────────────
# Classroom resolver tests
# ──────────────────────────────────────────────


def test_classroom_resolver_unique(test_db_session):
    """唯一班名 → 回正確 id。"""
    session = test_db_session
    _make_classroom(session, 100, "小班A")
    session.commit()

    resolver = _build_classroom_resolver(session)
    assert resolver("小班A") == 100


def test_classroom_resolver_unknown(test_db_session):
    """不存在班名 → None。"""
    session = test_db_session
    _make_classroom(session, 100, "小班A")
    session.commit()

    resolver = _build_classroom_resolver(session)
    assert resolver("不存在班") is None


def test_classroom_resolver_ambiguous(test_db_session):
    """兩個同名班（不同學年） → None（歧義）。

    ⭐ 修前：dict last-wins，回某個 id；修後：None。
    """
    session = test_db_session
    _make_classroom(session, 200, "中班B", school_year=113, semester=1, is_active=False)
    _make_classroom(session, 201, "中班B", school_year=114, semester=1, is_active=True)
    session.commit()

    resolver = _build_classroom_resolver(session)
    # 若其中一個 is_active=True，回 active；若兩者均 active 或均 inactive → None
    # 在這個 case：一個 inactive(200) + 一個 active(201) → 回 active id=201
    assert resolver("中班B") == 201, "一個 active + 一個 inactive 同名班 → 回 active id"


def test_classroom_resolver_two_active_ambiguous(test_db_session):
    """兩個同名且都 active 的班 → None（歧義）。"""
    session = test_db_session
    _make_classroom(session, 300, "大班C", school_year=114, semester=1, is_active=True)
    _make_classroom(session, 301, "大班C", school_year=114, semester=2, is_active=True)
    session.commit()

    resolver = _build_classroom_resolver(session)
    assert resolver("大班C") is None, "兩個 active 同名班應回 None（歧義）"


def test_classroom_resolver_active_wins(test_db_session):
    """同名：一個 is_active=True、一個 is_active=False → 回 active id。"""
    session = test_db_session
    _make_classroom(session, 400, "幼班D", school_year=113, semester=2, is_active=False)
    _make_classroom(session, 401, "幼班D", school_year=114, semester=1, is_active=True)
    session.commit()

    resolver = _build_classroom_resolver(session)
    assert resolver("幼班D") == 401

"""Bug #15 回歸：配額檢查的「配額列年度」必須與「用量年度」對齊到假單日期。

Bug（2026-06-16 bug hunt 發現）：
  api/leaves_quota.py 的 _check_quota / _check_compensatory_quota 透過
  _resolve_quota_row 讀配額列時，_resolve_quota_row 預設 target_date=None →
  解析「今天」的學年（resolve_current_academic_term）與「今天」的西元年；但用量
  加總（_get_approved_hours_in_year 等）卻是按**假單年度**（start_date.year）。

  當假單 start_date 與今天分屬不同學年（典型為跨學年邊界，如今天在下學期、補登
  一張新學年上學期的假單），就會出現「拿 A 學年的配額額度，去比對 B 學年的用量」
  → 誤判超額（擋下合法假單）或誤判可用（放行超額假單）。

修法：_resolve_quota_row 接受 target_date（假單 start_date），_check_quota /
  _check_compensatory_quota 下傳 start_date，使配額列年度與用量年度一致。

本測試固定「今天」與假單 start_date 分屬不同學年（同西元年，隔離學年維度），
  斷言 _resolve_quota_row 與 _check_quota 都對齊到假單學年。
"""

import os
import sys
from datetime import date

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.leaves_quota as quota_module
import utils.academic as academic_module
from models.database import Employee, LeaveQuota

# 今天固定在 113 學年下學期（2025-03，西元 2025）；假單在 114 學年上學期（2025-09，
# 同西元年 2025），純粹差在學年維度。
_TODAY = date(2025, 3, 15)
_LEAVE_START = date(2025, 9, 1)  # 同西元年、不同學年
_TODAY_SCHOOL_YEAR = 113  # _resolve_by_date(2025-03) → 113/2
_LEAVE_SCHOOL_YEAR = 114  # _resolve_by_date(2025-09) → 114/1


@pytest.fixture
def _fixed_today(monkeypatch):
    """把 academic 與 leaves_quota 兩處 today_taipei 都釘到固定日期。"""
    monkeypatch.setattr(academic_module, "today_taipei", lambda: _TODAY)
    monkeypatch.setattr(quota_module, "today_taipei", lambda: _TODAY)


def _seed_employee_and_quotas(session) -> int:
    """建立員工 + 兩張不同學年的 personal 配額列（今天學年 8h、假單學年 200h）。"""
    emp = Employee(
        employee_id="QXY001",
        name="跨學年配額測試員工",
        base_salary=36000,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    emp_id = emp.id

    # 今天學年（113）配額很小
    session.add(
        LeaveQuota(
            employee_id=emp_id,
            year=2025,
            school_year=_TODAY_SCHOOL_YEAR,
            leave_type="personal",
            total_hours=8,
        )
    )
    # 假單學年（114）配額充足
    session.add(
        LeaveQuota(
            employee_id=emp_id,
            year=2026,
            school_year=_LEAVE_SCHOOL_YEAR,
            leave_type="personal",
            total_hours=200,
        )
    )
    session.commit()
    return emp_id


def test_resolve_quota_row_uses_leave_target_date(test_db_session, _fixed_today):
    """_resolve_quota_row(target_date=假單start_date) 必須回傳假單學年（114）的配額列。"""
    session = test_db_session
    emp_id = _seed_employee_and_quotas(session)

    row = quota_module._resolve_quota_row(
        session, emp_id, "personal", target_date=_LEAVE_START
    )
    assert row is not None
    assert row.school_year == _LEAVE_SCHOOL_YEAR, (
        f"配額列應對齊假單學年 {_LEAVE_SCHOOL_YEAR}，"
        f"實際 {row.school_year}（誤用今天學年 {_TODAY_SCHOOL_YEAR}）"
    )
    assert row.total_hours == 200


def test_check_quota_aligns_quota_row_and_usage_year(test_db_session, _fixed_today):
    """_check_quota 對假單學年用量比對假單學年配額：80h ≤ 114學年 200h → 不應 raise。

    未修前：配額列讀「今天學年 113」的 8h，用量按假單年度（2026）加總，80h > 8h
    → 誤判超額 raise HTTPException（擋下合法假單）。
    """
    session = test_db_session
    emp_id = _seed_employee_and_quotas(session)

    # 不應拋例外：用假單學年（114, 200h）的額度比對假單年度用量（0h）
    quota_module._check_quota(
        session,
        emp_id,
        "personal",
        _LEAVE_START.year,  # 用量年度 = 假單年度
        80.0,
        include_pending=True,
        target_date=_LEAVE_START,
    )


def test_check_quota_still_blocks_real_overage_in_leave_year(
    test_db_session, _fixed_today
):
    """對齊後仍須擋下真正超額：申請 300h > 假單學年 200h 配額 → raise。"""
    session = test_db_session
    emp_id = _seed_employee_and_quotas(session)

    with pytest.raises(HTTPException) as exc:
        quota_module._check_quota(
            session,
            emp_id,
            "personal",
            _LEAVE_START.year,
            300.0,
            include_pending=True,
            target_date=_LEAVE_START,
        )
    assert exc.value.status_code == 400


def test_check_compensatory_quota_aligns_quota_row_to_leave_year(
    test_db_session, _fixed_today
):
    """補休配額檢查同樣對齊假單學年：今天學年補休 0h、假單學年補休 80h → 申請 40h 放行。"""
    session = test_db_session
    emp = Employee(
        employee_id="QXY002",
        name="跨學年補休配額測試員工",
        base_salary=36000,
        is_active=True,
    )
    session.add(emp)
    session.flush()
    emp_id = emp.id

    # 今天學年補休配額 0（會擋下任何申請）
    session.add(
        LeaveQuota(
            employee_id=emp_id,
            year=2025,
            school_year=_TODAY_SCHOOL_YEAR,
            leave_type="compensatory",
            total_hours=0,
        )
    )
    # 假單學年補休配額 80
    session.add(
        LeaveQuota(
            employee_id=emp_id,
            year=2026,
            school_year=_LEAVE_SCHOOL_YEAR,
            leave_type="compensatory",
            total_hours=80,
        )
    )
    session.commit()

    # 申請 40h 補休：用假單學年（80h）額度應放行，不該被今天學年（0h）誤擋
    quota_module._check_compensatory_quota(
        session,
        emp_id,
        _LEAVE_START.year,
        40.0,
        include_pending=True,
        target_date=_LEAVE_START,
    )

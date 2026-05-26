"""DB-aware 測試：services.overtime_conflict_service.check_quarterly_overtime_cap

涵蓋場景：
- 3 個 rolling 3-month 窗口都不超過 → pass
- 中段窗口 W2 (M-1 ~ M+1) 超過 → block，訊息標明 W2
- exclude_id 排除自己舊紀錄（update 路徑）
- rejected (is_approved=False) 不算進累計
- 跨年窗口（target=2026-01 → W1=2025/11~2026/01）正確跨年
- pending (is_approved=None) 算進累計（與 monthly 同口徑）
"""

import os
import sys
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.employee import Employee
from models.overtime import OvertimeRecord
from services.overtime_conflict_service import check_quarterly_overtime_cap


@pytest.fixture
def session():
    """獨立 in-memory SQLite session（不污染其他 test）。"""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    # 建一個測試員工
    emp = Employee(
        employee_id="E001",
        name="測試員工",
        base_salary=40000,
        employee_type="regular",
    )
    s.add(emp)
    s.commit()
    yield s
    s.close()
    engine.dispose()


def _add_ot(session, emp_id, ot_date, hours, is_approved=None):
    """快速建一筆 OvertimeRecord。"""
    ot = OvertimeRecord(
        employee_id=emp_id,
        overtime_date=ot_date,
        overtime_type="weekday",
        hours=hours,
        overtime_pay=0,
        is_approved=is_approved,
    )
    session.add(ot)
    session.commit()
    return ot


class TestCheckQuarterlyOvertimeCap:

    def test_all_windows_pass(self, session):
        """3 個窗口都不超過 138 → 不 raise"""
        _add_ot(session, 1, date(2026, 3, 10), 15.0, is_approved=True)
        _add_ot(session, 1, date(2026, 4, 10), 10.0, is_approved=True)
        _add_ot(session, 1, date(2026, 5, 10), 15.0, is_approved=True)
        check_quarterly_overtime_cap(session, 1, date(2026, 5, 20), 5.0)

    def test_middle_window_blocks_with_w2_label(self, session):
        """W2 (2026/04~06) 超過 → block 且訊息提 "2026/04~2026/06" """
        _add_ot(session, 1, date(2026, 3, 5), 5.0, is_approved=True)
        _add_ot(session, 1, date(2026, 4, 5), 45.0, is_approved=True)
        _add_ot(session, 1, date(2026, 5, 5), 45.0, is_approved=True)
        _add_ot(session, 1, date(2026, 6, 5), 45.0, is_approved=True)
        # caller 邏輯：先檢查 W1=95, W2=135, W3=90
        # W1+new=100 過，W2+new=140 raise（訊息提 2026/04~2026/06）
        with pytest.raises(HTTPException) as exc:
            check_quarterly_overtime_cap(session, 1, date(2026, 5, 20), 5.0)
        assert exc.value.status_code == 400
        assert "2026/04~2026/06" in exc.value.detail

    def test_exclude_id_excludes_self(self, session):
        """update 路徑：exclude_id 排除自己舊紀錄"""
        _add_ot(session, 1, date(2026, 3, 5), 50.0, is_approved=True)
        _add_ot(session, 1, date(2026, 4, 5), 50.0, is_approved=True)
        old = _add_ot(session, 1, date(2026, 5, 5), 30.0, is_approved=True)
        # 沒 exclude：W1=130, new=10 → 140 raise
        with pytest.raises(HTTPException):
            check_quarterly_overtime_cap(session, 1, date(2026, 5, 5), 10.0)
        # exclude 自己：W1=100, new=10 → 110 pass
        check_quarterly_overtime_cap(
            session, 1, date(2026, 5, 5), 10.0, exclude_id=old.id
        )

    def test_rejected_not_counted(self, session):
        """is_approved=False 不算進累計"""
        _add_ot(session, 1, date(2026, 3, 5), 50.0, is_approved=False)
        _add_ot(session, 1, date(2026, 4, 5), 50.0, is_approved=False)
        _add_ot(session, 1, date(2026, 5, 5), 50.0, is_approved=False)
        check_quarterly_overtime_cap(session, 1, date(2026, 5, 10), 10.0)

    def test_year_boundary_wraps_correctly(self, session):
        """target=2026-01 → W1=2025/11~2026/01 跨年正確"""
        _add_ot(session, 1, date(2025, 11, 5), 45.0, is_approved=True)
        _add_ot(session, 1, date(2025, 12, 5), 45.0, is_approved=True)
        _add_ot(session, 1, date(2026, 1, 5), 45.0, is_approved=True)
        with pytest.raises(HTTPException) as exc:
            check_quarterly_overtime_cap(session, 1, date(2026, 1, 20), 5.0)
        assert "2025/11~2026/01" in exc.value.detail

    def test_pending_counted(self, session):
        """is_approved=None (pending) 算進累計"""
        _add_ot(session, 1, date(2026, 3, 5), 45.0, is_approved=None)
        _add_ot(session, 1, date(2026, 4, 5), 45.0, is_approved=None)
        _add_ot(session, 1, date(2026, 5, 5), 45.0, is_approved=None)
        with pytest.raises(HTTPException):
            check_quarterly_overtime_cap(session, 1, date(2026, 5, 20), 5.0)

    def test_first_window_wins_when_multiple_exceed(self, session):
        """文件契約：多窗口同時超過時按 W1→W2→W3 順序回報第一個（W1）"""
        # target=2026-05-15。
        # 每 3、4、5、6 月各 50h → W1 (3~5)=150 超、W2 (4~6)=150 超、W3 (5~7)=50 過
        _add_ot(session, 42, date(2026, 3, 1), 50.0, is_approved=True)
        _add_ot(session, 42, date(2026, 4, 1), 50.0, is_approved=True)
        _add_ot(session, 42, date(2026, 5, 1), 50.0, is_approved=True)
        _add_ot(session, 42, date(2026, 6, 1), 50.0, is_approved=True)
        with pytest.raises(HTTPException) as exc:
            check_quarterly_overtime_cap(session, 42, date(2026, 5, 15), 0.1)
        # 第一個違反的是 W1，訊息應提 2026/03~2026/05 而非 2026/04~2026/06
        assert "2026/03~2026/05" in exc.value.detail
        assert "2026/04~2026/06" not in exc.value.detail

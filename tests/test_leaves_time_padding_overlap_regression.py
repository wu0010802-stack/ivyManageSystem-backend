"""回歸：請假時段字串未補零（如 '9:00'）→ 加班↔請假重疊偵測字典序誤判。

Bug sweep 2026-06-03 ②：
- LeaveCreate/LeaveUpdate 對 start_time/end_time 未做 HH:MM 正規化 → 原始字串落庫。
- check_employee_has_conflicting_leave（services/overtime_conflict_service.py）與
  api/leaves._check_employee_has_conflicting_overtime 用 max()/min() 對字串做
  「字典序」比較。單位數小時 '9:00' 字典序排在 '10:00'~'23:00' 之後（'9'>'1'），
  使 max/min overlap 公式失準 → 真重疊被漏判 → 同日請假扣款 + 加班費雙重溢付。

具體漏判：請假 '9:00'~'17:00'，加班 10:00~11:00（落在請假區間內，真重疊）。
  max('10:00','9:00')='9:00'；min('11:00','17:00')='11:00'；'9:00' < '11:00' = False
  → 舊碼不 raise（漏判）。修後應 raise 409。
"""

import os
import sys
from datetime import date, datetime, time

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import Employee, LeaveRecord, OvertimeRecord  # noqa: E402
from services.overtime_conflict_service import (  # noqa: E402
    check_employee_has_conflicting_leave,
)


def _seed_emp_and_leave(session, lv_start, lv_end):
    emp = Employee(
        employee_id="V900", name="時段測試", base_salary=36000, is_active=True
    )
    session.add(emp)
    session.flush()
    lv = LeaveRecord(
        employee_id=emp.id,
        leave_type="personal",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 1),
        leave_hours=4.0,
        start_time=lv_start,
        end_time=lv_end,
        status="approved",
    )
    session.add(lv)
    session.commit()
    return emp.id


class TestUnpaddedLeaveTimeOverlap:
    def test_real_overlap_detected_with_unpadded_leave_start(self, test_db_session):
        """請假 '9:00'~'17:00'（未補零），加班 10:00~11:00 真重疊 → 應 409。"""
        emp_id = _seed_emp_and_leave(test_db_session, "9:00", "17:00")
        with pytest.raises(HTTPException) as ei:
            check_employee_has_conflicting_leave(
                test_db_session, emp_id, date(2026, 6, 1), time(10, 0), time(11, 0)
            )
        assert ei.value.status_code == 409

    def test_padded_leave_real_overlap_still_detected(self, test_db_session):
        """補零版本（sanity）：'09:00'~'17:00' vs 10:00~11:00 → 應 409。"""
        emp_id = _seed_emp_and_leave(test_db_session, "09:00", "17:00")
        with pytest.raises(HTTPException) as ei:
            check_employee_has_conflicting_leave(
                test_db_session, emp_id, date(2026, 6, 1), time(10, 0), time(11, 0)
            )
        assert ei.value.status_code == 409

    def test_non_overlapping_not_flagged(self, test_db_session):
        """請假 '9:00'~'12:00'（上午），加班 13:00~15:00（下午）不重疊 → 不應 raise。"""
        emp_id = _seed_emp_and_leave(test_db_session, "9:00", "12:00")
        check_employee_has_conflicting_leave(
            test_db_session, emp_id, date(2026, 6, 1), time(13, 0), time(15, 0)
        )  # 不應 raise


class TestSubstituteConflictUnpaddedTime:
    """_check_substitute_leave_conflict 同樣用字串比較（api/leaves.py:570）。

    申請人請假時段未補零 '9:00'~'17:00'，代理人同日 10:00~11:00 有加班（真重疊）
    → 代理人不可代理，應 409。舊碼字典序漏判 → 誤放行。
    """

    def test_substitute_ot_conflict_detected_with_unpadded_applicant_time(
        self, test_db_session
    ):
        from api.leaves import _check_substitute_leave_conflict

        sub = Employee(
            employee_id="V901", name="代理人", base_salary=36000, is_active=True
        )
        test_db_session.add(sub)
        test_db_session.flush()
        ot = OvertimeRecord(
            employee_id=sub.id,
            overtime_date=date(2026, 6, 1),
            overtime_type="weekday",
            start_time=datetime(2026, 6, 1, 10, 0),
            end_time=datetime(2026, 6, 1, 11, 0),
            hours=1.0,
            status="approved",
        )
        test_db_session.add(ot)
        test_db_session.commit()

        with pytest.raises(HTTPException) as ei:
            _check_substitute_leave_conflict(
                test_db_session,
                sub.id,
                date(2026, 6, 1),
                date(2026, 6, 1),
                "9:00",
                "17:00",
            )
        assert ei.value.status_code == 409


class TestLeaveSchemaTimeNormalization:
    def test_leave_create_pads_unpadded_time(self):
        from api.leaves import LeaveCreate

        m = LeaveCreate.model_validate(
            {
                "employee_id": 1,
                "leave_type": "personal",
                "start_date": "2026-06-01",
                "end_date": "2026-06-01",
                "leave_hours": 4,
                "start_time": "9:00",
                "end_time": "12:00",
            }
        )
        assert m.start_time == "09:00"
        assert m.end_time == "12:00"

    def test_leave_update_pads_unpadded_time(self):
        from api.leaves import LeaveUpdate

        m = LeaveUpdate.model_validate({"start_time": "9:5", "end_time": "9:30"})
        assert m.start_time == "09:05"
        assert m.end_time == "09:30"

    def test_leave_create_rejects_out_of_range_time(self):
        from api.leaves import LeaveCreate

        with pytest.raises(Exception):
            LeaveCreate.model_validate(
                {
                    "employee_id": 1,
                    "leave_type": "personal",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-01",
                    "leave_hours": 4,
                    "start_time": "25:00",
                    "end_time": "26:00",
                }
            )

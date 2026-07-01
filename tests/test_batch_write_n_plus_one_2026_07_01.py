"""批次寫入 N+1 回歸測試（2026-07-01 quickwins）

兩個管理端批次寫入端點原本在迴圈內逐筆 SELECT：
- api/shifts.py `save_assignments`：逐員工 SELECT ShiftAssignment（找該週既有排班）
- api/overtimes.py `batch_create_overtimes`：逐員工 SELECT Employee（驗證存在 + 取底薪）

修法：迴圈前一次 `.in_()` 預載成 dict。本測試以 QueryCounter 斷言
「對目標表的 SELECT 次數與員工數無關（== 1）」。

shifts 另加行為保持測試：原本逐筆 `.first()` 靠 autoflush 讓「同批重複
employee_id」第二次看到剛插入的列走 update；預載改寫後必須在迴圈內同步維護
map（insert 加入 / delete 移除），否則第二次 insert 會撞
UniqueConstraint(employee_id, week_start_date) → IntegrityError。
"""

import types
from datetime import date

import models.base as base_module
from models.database import Employee
from models.shift import ShiftAssignment, ShiftType

import api.overtimes as overtimes_module
import api.shifts as shifts_module


def _emp(session, i: int) -> Employee:
    e = Employee(
        employee_id=f"NP1{i:03d}",
        name=f"N加一測試員工{i}",
        base_salary=36000,
        is_active=True,
    )
    session.add(e)
    session.flush()
    return e


def _shift_type(session, name: str = "早班", work_start="08:00", work_end="17:00"):
    st = ShiftType(name=name, work_start=work_start, work_end=work_end)
    session.add(st)
    session.flush()
    return st


def _count_table_selects(counter, table: str) -> int:
    """計數 counter 捕獲到的、針對指定表的 SELECT statement 數。"""
    return sum(
        1
        for s in counter.statements
        if s.lstrip().lower().startswith("select") and f"from {table}" in s.lower()
    )


# ---------------------------------------------------------------------------
# shifts: save_assignments
# ---------------------------------------------------------------------------


class TestSaveAssignmentsNPlusOne:
    def test_single_select_regardless_of_employee_count(
        self, test_db_session, query_counter, monkeypatch
    ):
        emps = [_emp(test_db_session, i) for i in range(5)]
        st = _shift_type(test_db_session)
        test_db_session.commit()

        # 尾段「週工時超時預警」對每位員工各查一次（get_employee_weekly_shift_hours
        # 內含 ShiftAssignment 查詢）——非本次修法的 upsert 迴圈，patch 成空以隔離。
        monkeypatch.setattr(
            shifts_module, "get_employee_weekly_shift_hours", lambda *a, **k: {}
        )

        req = shifts_module.BulkAssignmentRequest(
            week_start_date="2026-06-29",  # 週一
            assignments=[
                shifts_module.AssignmentItem(employee_id=e.id, shift_type_id=st.id)
                for e in emps
            ],
        )

        counter = query_counter(base_module._engine)
        with counter:
            resp = shifts_module.save_assignments(
                data=req, current_user={"role": "admin"}
            )

        n = _count_table_selects(counter, "shift_assignments")
        assert (
            n == 1
        ), f"預期對 shift_assignments 只 SELECT 1 次（批量預載），實際 {n} 次（N+1）"
        assert "已儲存 5 筆" in resp["message"]

    def test_duplicate_employee_last_wins_no_integrity_error(self, test_db_session):
        """同批重複 employee_id：淨 1 列、最後一筆勝出、不撞唯一約束（行為保持）。"""
        e = _emp(test_db_session, 0)
        st1 = _shift_type(test_db_session, name="早班")
        st2 = _shift_type(
            test_db_session, name="晚班", work_start="13:00", work_end="22:00"
        )
        test_db_session.commit()

        req = shifts_module.BulkAssignmentRequest(
            week_start_date="2026-06-29",
            assignments=[
                shifts_module.AssignmentItem(employee_id=e.id, shift_type_id=st1.id),
                shifts_module.AssignmentItem(employee_id=e.id, shift_type_id=st2.id),
            ],
        )
        shifts_module.save_assignments(data=req, current_user={"role": "admin"})

        test_db_session.rollback()  # 結束自身交易，確保讀到 endpoint 已 commit 的最新狀態
        rows = (
            test_db_session.query(ShiftAssignment)
            .filter(ShiftAssignment.employee_id == e.id)
            .all()
        )
        assert len(rows) == 1, f"同批重複應淨 1 列，實際 {len(rows)} 列"
        assert rows[0].shift_type_id == st2.id, "應由最後一筆（晚班）勝出"


# ---------------------------------------------------------------------------
# overtimes: batch_create_overtimes
# ---------------------------------------------------------------------------


class TestBatchCreateOvertimesNPlusOne:
    def test_single_employee_select_regardless_of_count(
        self, test_db_session, query_counter, monkeypatch
    ):
        emps = [_emp(test_db_session, i) for i in range(5)]
        test_db_session.commit()

        # 驗證鏈（overlap/leave/cap/calendar）非本次修法目標，patch 成 no-op
        # 讓測試聚焦於 Employee 預載的 N+1。
        monkeypatch.setattr(
            overtimes_module,
            "_validate_overtime_for_employee",
            lambda *a, **k: None,
        )

        data = overtimes_module.BatchOvertimeCreate(
            overtime_date=date(2026, 7, 1),  # 週三、非假日
            overtime_type="weekday",
            start_time="18:00",
            end_time="20:00",
            use_comp_leave=False,
            employees=[{"employee_id": e.id, "hours": 2.0} for e in emps],
        )
        fake_req = types.SimpleNamespace(state=types.SimpleNamespace())

        counter = query_counter(base_module._engine)
        with counter:
            resp = overtimes_module.batch_create_overtimes(
                data=data, request=fake_req, current_user={"role": "admin"}
            )

        n = _count_table_selects(counter, "employees")
        assert (
            n == 1
        ), f"預期對 employees 只 SELECT 1 次（批量預載），實際 {n} 次（N+1）"
        assert len(resp["created_ids"]) == 5

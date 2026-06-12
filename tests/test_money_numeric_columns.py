"""tests/test_money_numeric_columns.py — Float 金額欄 → Money(Numeric 12,2) 回歸

Why（設計體檢 2026-06-12 Finding 3）:
    overtime_records.overtime_pay / meeting_records.overtime_pay 以 Float
    （double precision）儲存，浮點表示誤差（0.1+0.2=0.30000000000000004）
    會原樣 persist 並流入 gross_salary 累加（engine 對 approved overtimes /
    meeting_records 直接 sum）。改用 models/types.Money（Numeric(12,2)，
    讀出自動轉 float）後儲存精度固定小數 2 位、對帳尾數不再失真。

    SQLite 下 SQLAlchemy Numeric 讀出同樣以 scale=2 量化，故本檔在全套
    pytest（SQLite）即為有效 gate。
"""

from __future__ import annotations

from datetime import date

from models.database import Employee, MeetingRecord, OvertimeRecord


def _seed_employee(s) -> int:
    emp = Employee(
        employee_id="MONEY01",
        name="精度測試員",
        base_salary=30000,
        employee_type="regular",
        is_active=True,
        hire_date=date(2025, 1, 1),
    )
    s.add(emp)
    s.flush()
    return emp.id


def test_overtime_pay_persists_two_decimal_precision(test_db_session):
    """0.1+0.2 寫入後讀回必須是 0.3（Float 會原樣存 0.30000000000000004）。"""
    s = test_db_session
    emp_id = _seed_employee(s)
    rec = OvertimeRecord(
        employee_id=emp_id,
        overtime_date=date(2026, 6, 1),
        overtime_type="weekday",
        hours=1.5,
        overtime_pay=0.1 + 0.2,  # float 表示誤差 0.30000000000000004
        status="approved",
    )
    s.add(rec)
    s.flush()
    s.expire(rec)
    assert rec.overtime_pay == 0.3


def test_meeting_overtime_pay_persists_two_decimal_precision(test_db_session):
    s = test_db_session
    emp_id = _seed_employee(s)
    rec = MeetingRecord(
        employee_id=emp_id,
        meeting_date=date(2026, 6, 2),
        attended=True,
        overtime_hours=2,
        overtime_pay=0.1 + 0.2,
    )
    s.add(rec)
    s.flush()
    s.expire(rec)
    assert rec.overtime_pay == 0.3


def test_overtime_pay_gross_accumulation_no_drift(test_db_session):
    """模擬 engine 的 sum(o.overtime_pay) 累加：多筆含表示誤差的金額讀回
    累加後必須等於精確的 2 位小數總和（Float 會累積 1e-13 級尾差）。"""
    s = test_db_session
    emp_id = _seed_employee(s)
    # 每筆都帶 float 表示誤差（*.1 / *.2 相加）
    payloads = [0.1 + 0.2, 1100.1 + 0.2, 2200.1 + 0.2]
    for i, pay in enumerate(payloads, start=1):
        s.add(
            OvertimeRecord(
                employee_id=emp_id,
                overtime_date=date(2026, 6, i),
                overtime_type="weekday",
                hours=1,
                overtime_pay=pay,
                status="approved",
            )
        )
    s.flush()
    s.expire_all()
    rows = s.query(OvertimeRecord).filter(OvertimeRecord.employee_id == emp_id).all()
    total = sum(o.overtime_pay or 0 for o in rows)
    assert total == 0.3 + 1100.3 + 2200.3

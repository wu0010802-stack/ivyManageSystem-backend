"""
P6: Money TypeDecorator 行為測試。

鎖定：
- 寫入 float/int 後讀出仍為 float（不是 Decimal，避免前端 JSON 序列化為 string）
- 小數 2 位精度保留（Numeric 12, 2）
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import Base, Employee, SalaryRecord


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        yield s


def _seed(session, **overrides):
    emp = Employee(employee_id="M1", name="精度測試", base_salary=30000, is_active=True)
    session.add(emp)
    session.flush()
    record = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=4,
        **overrides,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


class TestMoneyReturnsFloat:
    def test_integer_input_reads_back_as_float(self, session):
        record = _seed(session, base_salary=30000)
        # 讀出時 Money TypeDecorator process_result_value 轉 float
        assert isinstance(record.base_salary, float)
        assert record.base_salary == 30000.0

    def test_float_input_preserved_to_2dp(self, session):
        record = _seed(session, net_salary=1234.56)
        assert isinstance(record.net_salary, float)
        assert record.net_salary == pytest.approx(1234.56, abs=0.005)

    def test_string_numeric_input_accepted(self, session):
        # 寫入字串數字也可正確存取（process_bind_param 會 Decimal(str(v))）
        record = _seed(session, festival_bonus="2500.75")
        assert isinstance(record.festival_bonus, float)
        assert record.festival_bonus == pytest.approx(2500.75, abs=0.005)

    def test_none_stays_none(self, session):
        record = _seed(session)
        # 未指定的金額欄位 default=0 → 0.0
        assert record.overtime_pay == 0.0
        assert isinstance(record.overtime_pay, float)

    def test_zero_is_float_zero(self, session):
        record = _seed(session, gross_salary=0)
        assert isinstance(record.gross_salary, float)
        assert record.gross_salary == 0.0


class TestMoneyArithmeticRoundtrip:
    def test_multiply_ratio_stable_after_roundtrip(self, session):
        """模擬節慶獎金比例計算：round → 存 → 讀，應無浮點尾數殘留"""
        computed = round(2000 * 27 / 30, 2)  # 1800.0
        record = _seed(session, festival_bonus=computed)
        assert record.festival_bonus == 1800.0
        # 再寫入另一個帶尾數的值
        record.festival_bonus = round(2000 * 14 / 31, 2)  # 903.23
        session.commit()
        session.refresh(record)
        assert record.festival_bonus == pytest.approx(903.23, abs=0.005)

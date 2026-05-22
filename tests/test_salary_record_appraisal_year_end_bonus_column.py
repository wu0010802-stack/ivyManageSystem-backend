"""驗證 SalaryRecord 加上 appraisal_year_end_bonus column。"""

from sqlalchemy import inspect

from models.salary import SalaryRecord


def test_salary_record_has_appraisal_year_end_bonus_column():
    cols = {c.name: c for c in inspect(SalaryRecord).columns}
    assert "appraisal_year_end_bonus" in cols
    col = cols["appraisal_year_end_bonus"]
    assert col.nullable is False
    assert str(col.default.arg) == "0" or col.default.arg == 0

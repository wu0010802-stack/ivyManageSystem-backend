"""防漂移 + 回歸：SalarySnapshot 必須涵蓋 SalaryRecord 所有金額欄。

_copy_record_to_snapshot（services/finance/salary_snapshot_service）以兩表欄位
「交集」反射複製。SalarySnapshot model 漏欄 → 交集漏 → 該金額在快照遺失，稽核
重印歷史薪條時憑空消失（supplementary_health_employee / appraisal_year_end_bonus
/ unused_leave_payout 三欄即如此漏掉）。本測試把「PR checklist 提醒」升級為強制
契約，未來 SalaryRecord 新增 Money 欄而 SalarySnapshot 漏補時立即 fail。
"""

from datetime import date

from sqlalchemy import inspect as sa_inspect

from models.salary import SalaryRecord, SalarySnapshot
from models.types import Money


def test_snapshot_covers_all_salaryrecord_money_columns():
    rec_money = {
        c.name for c in sa_inspect(SalaryRecord).columns if isinstance(c.type, Money)
    }
    snap_cols = {c.name for c in sa_inspect(SalarySnapshot).columns}
    missing = rec_money - snap_cols
    assert not missing, (
        f"SalarySnapshot 漏複製 SalaryRecord 金額欄: {sorted(missing)}；"
        "請在 SalarySnapshot 補上對應 Money 欄（_copy_record_to_snapshot 依兩表交集反射複製）。"
    )


def test_copy_record_to_snapshot_copies_independent_payout_columns(test_db_session):
    """端到端：三個獨立轉帳/拆分欄位的值確實被快照保存（非僅 model 有欄）。"""
    from models.database import Employee
    from services.finance.salary_snapshot_service import _copy_record_to_snapshot

    s = test_db_session
    emp = Employee(
        employee_id="A001",
        name="員工A",
        base_salary=30000,
        employee_type="regular",
        is_active=True,
        hire_date=date(2025, 1, 1),
    )
    s.add(emp)
    s.commit()

    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=6,
        base_salary=30000,
        gross_salary=30000,
        net_salary=30000,
        total_deduction=0,
        supplementary_health_employee=123.45,
        appraisal_year_end_bonus=678.90,
        unused_leave_payout=111.11,
    )
    s.add(rec)
    s.flush()

    snap = _copy_record_to_snapshot(rec, "month_end", "tester")

    assert snap.supplementary_health_employee == 123.45
    assert snap.appraisal_year_end_bonus == 678.90
    assert snap.unused_leave_payout == 111.11

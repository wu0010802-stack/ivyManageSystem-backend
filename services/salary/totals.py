"""SalaryRecord 的聚合欄位重算公式 — engine 與 api 共用。

單一 source of truth:gross_salary / total_deduction / net_salary / bonus_amount /
bonus_separate 從 SalaryRecord 個別欄位推導的公式。

Why: 原本 engine.py 的 _recompute_record_totals_from_fields 與 api/salary.py 的
    _recalculate_salary_record_totals 是同一條公式的兩份實作,任一份新增扣款 /
    獎金欄位忘了同步另一份就會 drift(例如財報顯示 net_salary 與 record fields
    sum 不一致)。集中於此避免 drift。
"""


def recompute_record_totals(record):
    """從 SalaryRecord 各欄位重算 gross/total_deduction/net/bonus_amount/bonus_separate。

    使用情境:
    - manual_adjust 寫完欄位後,以新欄位值重算 totals
    - 重算路徑(_fill_salary_record)在有 manual_overrides 時,以保留+新算的
      混合欄位值重算 totals,避免使用 breakdown 的 totals 與保留欄位脫節
    """
    record.gross_salary = round(
        (record.base_salary or 0)
        + (record.hourly_total or 0)
        + (record.performance_bonus or 0)
        + (record.special_bonus or 0)
        + (record.supervisor_dividend or 0)
        + (record.meeting_overtime_pay or 0)
        + (record.birthday_bonus or 0)
        + (record.overtime_pay or 0)
    )
    record.total_deduction = round(
        (record.labor_insurance_employee or 0)
        + (record.health_insurance_employee or 0)
        + (record.pension_employee or 0)
        + (record.late_deduction or 0)
        + (record.early_leave_deduction or 0)
        + (record.missing_punch_deduction or 0)
        + (record.leave_deduction or 0)
        + (record.absence_deduction or 0)
        + (record.other_deduction or 0)
    )
    record.bonus_amount = round(
        (record.festival_bonus or 0)
        + (record.overtime_bonus or 0)
        + (record.supervisor_dividend or 0)
    )
    record.bonus_separate = (record.bonus_amount or 0) > 0
    record.net_salary = round(
        (record.gross_salary or 0) - (record.total_deduction or 0)
    )

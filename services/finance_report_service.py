"""園務統計收支整合 service。

Source provider 模式：每個收入/支出來源是一個純函式，回傳
`dict[int month, int amount]`（或薪資的巢狀 dict）。Aggregator
`build_finance_summary` 把所有 provider 聚合成 API 回傳結構。

新增來源時只需：
1. 寫一個 get_xxx_by_month provider
2. 在 build_finance_summary 對應 category list 加一筆
3. （可選）為新來源補測試
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import extract, func
from sqlalchemy.orm import Session

from models.activity import ActivityPaymentRecord, ActivityRegistration
from models.employee import Employee
from models.fees import (
    StudentFeePayment,
    StudentFeeRecord,
    StudentFeeRefund,
)
from models.salary import SalaryRecord


def _month_totals_from(rows) -> dict[int, int]:
    return {int(m): int(a or 0) for m, a in rows if m is not None}


# ─────────────────────────────────────────────────────────────────────────────
# 收入 providers
# ─────────────────────────────────────────────────────────────────────────────


def get_tuition_revenue_by_month(session: Session, year: int) -> dict[int, int]:
    """學費已繳金額，按 StudentFeePayment.payment_date 月份聚合（append-only 流水）。

    Why: 舊版用 StudentFeeRecord.status='paid' + payment_date 聚合會有三個問題：
    1) 分期收款第一筆的日期被後續覆寫 → 收入搬到最後月份
    2) 退款後 status 變 partial/unpaid → 整筆收入從月報消失
    3) partial 狀態的現金不計入（條件是 status='paid'）
    改讀 StudentFeePayment 每筆 append-only 流水即可正確歸月。
    """
    rows = (
        session.query(
            extract("month", StudentFeePayment.payment_date).label("m"),
            func.sum(StudentFeePayment.amount),
        )
        .filter(extract("year", StudentFeePayment.payment_date) == year)
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)


def get_tuition_refund_by_month(session: Session, year: int) -> dict[int, int]:
    """學費退款，按 refunded_at 月份聚合。"""
    rows = (
        session.query(
            extract("month", StudentFeeRefund.refunded_at).label("m"),
            func.sum(StudentFeeRefund.amount),
        )
        .filter(extract("year", StudentFeeRefund.refunded_at) == year)
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)


def get_activity_revenue_by_month(session: Session, year: int) -> dict[int, int]:
    """才藝繳費（type='payment'），按 payment_date 月份聚合；排除 voided 軟刪紀錄。"""
    rows = (
        session.query(
            extract("month", ActivityPaymentRecord.payment_date).label("m"),
            func.sum(ActivityPaymentRecord.amount),
        )
        .filter(
            ActivityPaymentRecord.type == "payment",
            extract("year", ActivityPaymentRecord.payment_date) == year,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)


def get_activity_refund_by_month(session: Session, year: int) -> dict[int, int]:
    """才藝退費（type='refund'），按 payment_date 月份聚合；排除 voided 軟刪紀錄。"""
    rows = (
        session.query(
            extract("month", ActivityPaymentRecord.payment_date).label("m"),
            func.sum(ActivityPaymentRecord.amount),
        )
        .filter(
            ActivityPaymentRecord.type == "refund",
            extract("year", ActivityPaymentRecord.payment_date) == year,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .group_by("m")
        .all()
    )
    return _month_totals_from(rows)


# ─────────────────────────────────────────────────────────────────────────────
# 支出 providers
# ─────────────────────────────────────────────────────────────────────────────


def get_salary_expense_by_month(
    session: Session, year: int
) -> dict[int, dict[str, int]]:
    """薪資支出按月聚合。

    拆兩層方便前端分類顯示：
    - employee_gross：員工應發（gross_salary + festival_bonus + overtime_bonus）
      Why: SalaryEngine 的 gross_salary 不包含另行轉帳的節慶與超額獎金
      （bonus_separate=True 的部分），但那仍是園方實際現金流出。若不加
      進來，財務總表在獎金發放月會低估 total_expense 與 net_cashflow。
    - employer_benefit：雇主勞健保 + 雇主勞退（園方實質保費支出）
    """
    rows = (
        session.query(
            SalaryRecord.salary_month,
            func.sum(SalaryRecord.gross_salary),
            func.sum(SalaryRecord.festival_bonus),
            func.sum(SalaryRecord.overtime_bonus),
            func.sum(SalaryRecord.labor_insurance_employer),
            func.sum(SalaryRecord.health_insurance_employer),
            func.sum(SalaryRecord.pension_employer),
        )
        .filter(SalaryRecord.salary_year == year)
        .group_by(SalaryRecord.salary_month)
        .all()
    )
    out: dict[int, dict[str, int]] = {}
    for m, gross, fest, ot_bonus, li, hi, pen in rows:
        out[int(m)] = {
            "employee_gross": int((gross or 0) + (fest or 0) + (ot_bonus or 0)),
            "employer_benefit": int((li or 0) + (hi or 0) + (pen or 0)),
        }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Aggregator
# ─────────────────────────────────────────────────────────────────────────────


def build_finance_summary(
    session: Session, year: int, month: Optional[int] = None
) -> dict:
    """聚合所有來源為 API 回傳結構。"""
    tuition_rev = get_tuition_revenue_by_month(session, year)
    tuition_ref = get_tuition_refund_by_month(session, year)
    activity_rev = get_activity_revenue_by_month(session, year)
    activity_ref = get_activity_refund_by_month(session, year)
    salary_exp = get_salary_expense_by_month(session, year)

    months = list(range(1, 13)) if month is None else [month]

    trend = []
    for m in months:
        revenue = tuition_rev.get(m, 0) + activity_rev.get(m, 0)
        refund = tuition_ref.get(m, 0) + activity_ref.get(m, 0)
        sal = salary_exp.get(m, {"employee_gross": 0, "employer_benefit": 0})
        expense = sal["employee_gross"] + sal["employer_benefit"]
        trend.append(
            {
                "month": m,
                "revenue": revenue,
                "refund": refund,
                "expense": expense,
                "net": (revenue - refund) - expense,
            }
        )

    total_rev = sum(r["revenue"] for r in trend)
    total_ref = sum(r["refund"] for r in trend)
    total_exp = sum(r["expense"] for r in trend)

    tuition_rev_total = sum(tuition_rev.get(m, 0) for m in months)
    tuition_ref_total = sum(tuition_ref.get(m, 0) for m in months)
    activity_rev_total = sum(activity_rev.get(m, 0) for m in months)
    activity_ref_total = sum(activity_ref.get(m, 0) for m in months)
    gross_total = sum(salary_exp.get(m, {}).get("employee_gross", 0) for m in months)
    employer_total = sum(
        salary_exp.get(m, {}).get("employer_benefit", 0) for m in months
    )

    return {
        "period": {"year": year, "month": month},
        "summary": {
            "total_revenue": total_rev,
            "total_refund": total_ref,
            "net_revenue": total_rev - total_ref,
            "total_expense": total_exp,
            "net_cashflow": (total_rev - total_ref) - total_exp,
        },
        "revenue_by_category": [
            {
                "category": "tuition",
                "label": "學費",
                "amount": tuition_rev_total,
                "refund": tuition_ref_total,
            },
            {
                "category": "activity",
                "label": "才藝",
                "amount": activity_rev_total,
                "refund": activity_ref_total,
            },
        ],
        "expense_by_category": [
            {
                "category": "salary_gross",
                "label": "員工應發",
                "amount": gross_total,
            },
            {
                "category": "employer_benefit",
                "label": "雇主保費+勞退",
                "amount": employer_total,
            },
        ],
        "monthly_trend": trend,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 月度明細（下鑽）
# ─────────────────────────────────────────────────────────────────────────────


def _iso(d) -> Optional[str]:
    return d.isoformat() if d else None


def get_tuition_detail(session: Session, year: int, month: int) -> list[dict]:
    """回傳該月學費繳費 + 退款的明細列。

    每筆來源以 kind 區分：'payment'（已繳學費流水）或 'refund'（退款）。
    繳費從 StudentFeePayment 逐筆取（保留原收款日），JOIN record 拿學生/項目 snapshot。
    """
    out: list[dict] = []
    paid_rows = (
        session.query(StudentFeePayment, StudentFeeRecord)
        .join(StudentFeeRecord, StudentFeePayment.record_id == StudentFeeRecord.id)
        .filter(
            extract("year", StudentFeePayment.payment_date) == year,
            extract("month", StudentFeePayment.payment_date) == month,
        )
        .order_by(StudentFeePayment.payment_date)
        .all()
    )
    for payment, record in paid_rows:
        out.append(
            {
                "kind": "payment",
                "date": _iso(payment.payment_date),
                "student_name": record.student_name,
                "classroom_name": record.classroom_name,
                "fee_item_name": record.fee_item_name,
                "amount": int(payment.amount or 0),
                "payment_method": payment.payment_method,
            }
        )
    refunds = (
        session.query(StudentFeeRefund)
        .filter(
            extract("year", StudentFeeRefund.refunded_at) == year,
            extract("month", StudentFeeRefund.refunded_at) == month,
        )
        .all()
    )
    for r in refunds:
        out.append(
            {
                "kind": "refund",
                "date": _iso(r.refunded_at.date() if r.refunded_at else None),
                "amount": int(r.amount or 0),
                "reason": r.reason,
                "refunded_by": r.refunded_by,
            }
        )
    out.sort(key=lambda x: x.get("date") or "")
    return out


def get_activity_detail(session: Session, year: int, month: int) -> list[dict]:
    """才藝繳費/退費明細（含報名關聯的學生姓名）；排除 voided 軟刪紀錄。"""
    rows = (
        session.query(ActivityPaymentRecord, ActivityRegistration)
        .outerjoin(
            ActivityRegistration,
            ActivityPaymentRecord.registration_id == ActivityRegistration.id,
        )
        .filter(
            extract("year", ActivityPaymentRecord.payment_date) == year,
            extract("month", ActivityPaymentRecord.payment_date) == month,
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .order_by(ActivityPaymentRecord.payment_date)
        .all()
    )
    result = []
    for rec, reg in rows:
        result.append(
            {
                "kind": rec.type,  # payment / refund
                "date": _iso(rec.payment_date),
                "registration_id": rec.registration_id,
                "student_name": getattr(reg, "student_name", None) if reg else None,
                "amount": int(rec.amount or 0),
                "payment_method": rec.payment_method,
                "operator": rec.operator,
                "receipt_no": rec.receipt_no,
            }
        )
    return result


def get_salary_detail(session: Session, year: int, month: int) -> list[dict]:
    """該月所有員工薪資支出明細（每人一列）。"""
    rows = (
        session.query(SalaryRecord, Employee)
        .join(Employee, SalaryRecord.employee_id == Employee.id)
        .filter(
            SalaryRecord.salary_year == year,
            SalaryRecord.salary_month == month,
        )
        .order_by(Employee.name)
        .all()
    )
    result = []
    for rec, emp in rows:
        gross = int(rec.gross_salary or 0)
        festival = int(rec.festival_bonus or 0)
        overtime_bonus = int(rec.overtime_bonus or 0)
        employer = (
            int(rec.labor_insurance_employer or 0)
            + int(rec.health_insurance_employer or 0)
            + int(rec.pension_employer or 0)
        )
        # 對齊月摘要 get_salary_expense_by_month：employee_gross = gross + festival + overtime
        # supervisor_dividend 已含於 gross_salary，不再重複加。
        real_cost = gross + festival + overtime_bonus + employer
        result.append(
            {
                "employee_name": emp.name,
                "employee_id": emp.id,
                "gross_salary": gross,
                "festival_bonus": festival,
                "overtime_bonus": overtime_bonus,
                "net_salary": int(rec.net_salary or 0),
                "employer_benefit": employer,
                "real_cost": real_cost,
                "is_finalized": bool(rec.is_finalized),
            }
        )
    return result


def build_finance_detail(session: Session, year: int, month: int) -> dict:
    """下鑽明細彙總：回傳三來源的明細陣列。"""
    return {
        "period": {"year": year, "month": month},
        "tuition": get_tuition_detail(session, year, month),
        "activity": get_activity_detail(session, year, month),
        "salary": get_salary_detail(session, year, month),
    }

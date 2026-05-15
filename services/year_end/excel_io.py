"""年終獎金 Excel I/O。

22 sheets 的 import / export 在實務上 sheet 結構零散，本模組提供：
- export：年終總表、轉帳名冊、個人獎金條
- import：特別獎金（4 種主要 sheet 共用一支 importer，以 bonus_type 區分）

班級經營績效、節慶獎金差額由業主在 UI 上手動建檔（importer 可後續擴充）。
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.orm import Session

from models.employee import Employee
from models.year_end import (
    SpecialBonusType,
    YearEndCycle,
    YearEndSettlement,
    YearEndSpecialBonusItem,
)

logger = logging.getLogger(__name__)


def _decimal_or_none(raw) -> Optional[Decimal]:
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


# ── Import: 特別獎金 ───────────────────────────────────────────────────────


def import_special_bonus_excel(
    db: Session,
    cycle: YearEndCycle,
    bonus_type: SpecialBonusType,
    file_bytes: bytes,
    *,
    period_label: str = "",
) -> dict:
    """匯入特別獎金 Excel：第一欄員工編號 / 姓名，第二欄金額；可選第三欄 calc_meta JSON 文字。

    冪等：UNIQUE(cycle_id, employee_id, bonus_type, period_label)。
    """
    wb = load_workbook(BytesIO(file_bytes), data_only=True)
    ws = wb.active

    stats = {"created": 0, "updated": 0, "skipped": 0}

    for row in ws.iter_rows(min_row=2, values_only=True):
        if row is None or len(row) < 2:
            continue
        emp_key, amount_raw = row[0], row[1]
        note_raw = row[2] if len(row) > 2 else None

        if emp_key is None:
            continue
        emp = db.execute(
            select(Employee).where(Employee.employee_id == str(emp_key))
        ).scalar_one_or_none()
        if emp is None:
            emp = db.execute(
                select(Employee).where(Employee.name == str(emp_key))
            ).scalar_one_or_none()
        if emp is None:
            stats["skipped"] += 1
            continue

        amount = _decimal_or_none(amount_raw)
        if amount is None:
            stats["skipped"] += 1
            continue

        existing = db.execute(
            select(YearEndSpecialBonusItem).where(
                YearEndSpecialBonusItem.cycle_id == cycle.id,
                YearEndSpecialBonusItem.employee_id == emp.id,
                YearEndSpecialBonusItem.bonus_type == bonus_type,
                YearEndSpecialBonusItem.period_label == period_label,
            )
        ).scalar_one_or_none()

        calc_meta: dict = {}
        if note_raw is not None:
            calc_meta["note"] = str(note_raw)

        if existing is None:
            db.add(
                YearEndSpecialBonusItem(
                    cycle_id=cycle.id,
                    employee_id=emp.id,
                    bonus_type=bonus_type,
                    period_label=period_label,
                    amount=amount,
                    calc_meta=calc_meta,
                )
            )
            stats["created"] += 1
        else:
            existing.amount = amount
            existing.calc_meta = calc_meta or existing.calc_meta
            stats["updated"] += 1

    db.flush()
    return stats


# ── Export ──────────────────────────────────────────────────────────────────


def build_settlement_report(db: Session, cycle: YearEndCycle) -> bytes:
    """年終獎金總表：每員工一列。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "年終獎金總表"

    ws.append([f"{cycle.academic_year} 學年度年終獎金總表"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)

    ws.append(
        [
            "員工編號",
            "員工",
            "平均績效%",
            "毛額",
            "小計",
            "扣項合計",
            "應領小計",
            "特別獎金",
            "年終總額",
        ]
    )
    for cell in ws[2]:
        cell.font = Font(bold=True)

    rows = db.execute(
        select(YearEndSettlement, Employee)
        .join(Employee, Employee.id == YearEndSettlement.employee_id)
        .where(YearEndSettlement.cycle_id == cycle.id)
        .order_by(Employee.id)
    ).all()

    for s, emp in rows:
        ws.append(
            [
                emp.employee_id or "",
                emp.name or "",
                float(s.avg_performance_rate),
                float(s.gross_amount),
                float(s.subtotal_amount),
                float(s.deduction_total),
                float(s.payable_subtotal),
                float(s.special_bonus_sum),
                float(s.total_amount),
            ]
        )

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_transfer_roster(db: Session, cycle: YearEndCycle) -> bytes:
    """年終轉帳名冊：只列 total_amount > 0。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "年終獎金轉帳"

    ws.append([f"{cycle.academic_year} 學年度年終獎金轉帳名冊"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.append(["員工編號", "員工", "銀行代碼", "銀行帳號", "戶名", "年終總額"])
    for cell in ws[2]:
        cell.font = Font(bold=True)

    rows = db.execute(
        select(YearEndSettlement, Employee)
        .join(Employee, Employee.id == YearEndSettlement.employee_id)
        .where(
            YearEndSettlement.cycle_id == cycle.id,
            YearEndSettlement.total_amount > 0,
        )
        .order_by(Employee.id)
    ).all()

    for s, emp in rows:
        ws.append(
            [
                emp.employee_id or "",
                emp.name or "",
                getattr(emp, "bank_code", "") or "",
                getattr(emp, "bank_account", "") or "",
                getattr(emp, "bank_account_name", "") or emp.name or "",
                float(s.total_amount),
            ]
        )

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_employee_slip(db: Session, settlement: YearEndSettlement) -> bytes:
    """個人年終獎金條（New 年終獎金條格式）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "個人獎金條"

    emp = db.get(Employee, settlement.employee_id)
    cycle = db.get(YearEndCycle, settlement.cycle_id)

    ws["A1"] = f"{cycle.academic_year} 學年度年終獎金通知"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"姓名：{emp.name or ''}"
    ws["B2"] = f"員工編號：{emp.employee_id or ''}"

    layout = [
        ("step1 平均績效 (%)", settlement.avg_performance_rate),
        ("step2 年終毛額", settlement.gross_amount),
        ("step3 小計", settlement.subtotal_amount),
        ("step4 扣項：請假遲到", settlement.deduction_late),
        ("step4 扣項：事假", settlement.deduction_personal_leave),
        ("step4 扣項：病假", settlement.deduction_sick_leave),
        ("step4 扣項：機構會議", settlement.deduction_meeting),
        ("step4 扣項：獎懲", settlement.deduction_disciplinary),
        ("step4 扣項：育嬰假", settlement.deduction_parental_leave),
        ("step4 扣項合計", settlement.deduction_total),
        ("step5 應領小計（含到職比例）", settlement.payable_subtotal),
        ("step6 特別獎金", settlement.special_bonus_sum),
        ("年終總額", settlement.total_amount),
    ]
    for idx, (label, value) in enumerate(layout, start=4):
        ws.cell(row=idx, column=1, value=label)
        ws.cell(row=idx, column=2, value=float(value))

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()

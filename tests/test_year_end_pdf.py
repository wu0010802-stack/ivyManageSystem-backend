"""年終獎金 PDF 產出 smoke tests（M6）。

只驗證：
- bytes 不為空
- 開頭含 %PDF 簽章
- 三大模板皆可產出
"""

from __future__ import annotations

from decimal import Decimal

from models.year_end import SpecialBonusType
from services.year_end.print_pdf import (
    PersonalBonusSlipData,
    SummaryTableRow,
    TransferEntry,
    generate_personal_bonus_slip_pdf,
    generate_summary_table_pdf,
    generate_transfer_roster_pdf,
)


def test_personal_bonus_slip_pdf_smoke():
    pdf = generate_personal_bonus_slip_pdf(
        PersonalBonusSlipData(
            employee_name="蔡宜倩",
            academic_year=114,
            print_date="115.02.12",
            year_end_amount=Decimal("29044.71"),
            bonus_by_type={
                SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST: Decimal("3312"),
                SpecialBonusType.SEMESTER_DIVIDEND_FIRST: Decimal("1500"),
                SpecialBonusType.SEMESTER_DIVIDEND_SECOND: Decimal("1000"),
                SpecialBonusType.AFTER_CLASS_AWARD: Decimal("1275"),
                SpecialBonusType.EXCESS_ENROLLMENT: Decimal("2000"),
                SpecialBonusType.FESTIVAL_DIFF: Decimal("1975"),
            },
        )
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_transfer_roster_pdf_smoke():
    pdf = generate_transfer_roster_pdf(
        entries=[
            TransferEntry(
                bank_account="0727-979-096436",
                name="王雅玲",
                amount=Decimal("37292.65"),
            ),
            TransferEntry(
                bank_account="0152-979-062379",
                name="蔡宜倩",
                amount=Decimal("40106.71"),
            ),
            TransferEntry(
                bank_account="ZERO",
                name="不應出現",
                amount=Decimal("0"),
            ),
        ],
        academic_year=114,
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000


def test_summary_table_pdf_smoke():
    pdf = generate_summary_table_pdf(
        rows=[
            SummaryTableRow(
                name="蔡宜倩",
                year_end_amount=Decimal("29044.71"),
                bonus_by_type={
                    SpecialBonusType.APPRAISAL_HALF_BONUS_FIRST: Decimal("3312"),
                    SpecialBonusType.SEMESTER_DIVIDEND_FIRST: Decimal("1500"),
                    SpecialBonusType.AFTER_CLASS_AWARD: Decimal("1275"),
                    SpecialBonusType.EXCESS_ENROLLMENT: Decimal("2000"),
                    SpecialBonusType.FESTIVAL_DIFF: Decimal("1975"),
                },
                total=Decimal("40106.71"),
            ),
            SummaryTableRow(
                name="陳品棻",
                year_end_amount=Decimal("30242.87"),
                bonus_by_type={
                    SpecialBonusType.SEMESTER_DIVIDEND_FIRST: Decimal("500"),
                    SpecialBonusType.AFTER_CLASS_AWARD: Decimal("1105"),
                },
                total=Decimal("39127.87"),
            ),
        ],
        academic_year=114,
    )
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1000

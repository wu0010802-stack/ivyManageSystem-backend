"""composer：5 表合成 → IvyKids 82 列級距 oracle 測試。

關鍵：餵 2026 真實 raw → 應合成出與既有 INSURANCE_TABLE_2026 一致。
若失敗：修 composer，不可修 oracle。
"""

import json
from pathlib import Path

import pytest

from services.gov_data import composer, parser
from services.insurance_service import INSURANCE_TABLE_2026

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "gov_data"


def _load(filename: str):
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


@pytest.fixture
def parsed_2026():
    return {
        "labor_brackets": parser.parse_mol_labor_brackets(
            _load("mol_labor_brackets_2026.json")
        ),
        "labor_premium": parser.parse_mol_labor_premium(
            _load("mol_labor_premium_2026.json")
        ),
        "pension": parser.parse_mol_pension(_load("mol_pension_2026.json")),
        "nhi_brackets": parser.parse_nhi_brackets(_load("nhi_brackets_2026.json")),
        "nhi_premium": parser.parse_nhi_premium(_load("nhi_premium_2026.json")),
    }


def test_compose_2026_matches_insurance_table_oracle(parsed_2026):
    """ORACLE: 餵 2026 真實 raw → 合成 82 列 = INSURANCE_TABLE_2026。"""
    composed = composer.compose_brackets(
        effective_year=2026,
        labor_brackets=parsed_2026["labor_brackets"],
        labor_premium=parsed_2026["labor_premium"],
        pension=parsed_2026["pension"],
        nhi_brackets=parsed_2026["nhi_brackets"],
        nhi_premium=parsed_2026["nhi_premium"],
        composed_from={
            "mol_labor_brackets": 1,
            "mol_labor_premium": 2,
            "mol_pension": 3,
            "nhi_brackets": 4,
            "nhi_premium": 5,
        },
    )

    composed_dicts = [
        {
            "amount": r.amount,
            "labor_employee": r.labor_employee,
            "labor_employer": r.labor_employer,
            "health_employee": r.health_employee,
            "health_employer": r.health_employer,
            "pension": r.pension,
        }
        for r in composed.rows
    ]
    composed_sorted = sorted(composed_dicts, key=lambda d: d["amount"])
    oracle_sorted = sorted(INSURANCE_TABLE_2026, key=lambda d: d["amount"])

    # 列數要對齊
    assert len(composed_sorted) == len(
        oracle_sorted
    ), f"composed has {len(composed_sorted)} rows, oracle has {len(oracle_sorted)}"
    # 逐列比對；遇差異列出明細以利除錯
    diffs = []
    for c, o in zip(composed_sorted, oracle_sorted):
        if c != o:
            diffs.append({"composed": c, "oracle": o})
    assert not diffs, f"oracle mismatch on {len(diffs)} rows; first 3: {diffs[:3]}"


def test_compose_minimum_wage_returns_latest(parsed_2026):
    from datetime import date
    from services.gov_data.schemas import MinimumWageResult

    mw = MinimumWageResult(
        history=[
            (date(2023, 1, 1), 25250, 168),
            (date(2024, 1, 1), 27470, 183),
        ]
    )
    eff, monthly, hourly = composer.compose_minimum_wage(mw)
    assert eff == date(2024, 1, 1)
    assert monthly == 27470


def test_compose_brackets_missing_amount_raises():
    """若 labor_premium 缺對應 amount 的 X，應 raise ComposeError。"""
    from services.gov_data.schemas import (
        LaborBracketsResult,
        LaborPremiumResult,
        PensionResult,
        NhiBracketsResult,
        NhiPremiumResult,
    )

    labor_b = LaborBracketsResult(
        effective_year=2027, amounts=[29500], max_insured=45800
    )
    labor_p = LaborPremiumResult(effective_year=2027, by_amount={})  # 空
    pension = PensionResult(effective_year=2027, amounts=[29500], max_insured=150000)
    nhi_b = NhiBracketsResult(effective_year=2027, amounts=[29500], max_insured=313000)
    nhi_p = NhiPremiumResult(
        effective_year=2027,
        by_amount={29500: {"single": {"employee": 458, "employer": 1428}, "deps": {}}},
    )
    with pytest.raises(composer.ComposeError):
        composer.compose_brackets(
            effective_year=2027,
            labor_brackets=labor_b,
            labor_premium=labor_p,
            pension=pension,
            nhi_brackets=nhi_b,
            nhi_premium=nhi_p,
            composed_from={},
        )

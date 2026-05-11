"""驗證 gov_data 內部資料模型（dataclass）+ 通用 helper（hash/diff）。"""

import pytest

from services.gov_data.schemas import (
    BracketRow,
    LaborBracketsResult,
    MinimumWageResult,
    SOURCE_KEYS,
)
from services.gov_data.utils import sha256_of_payload, compute_brackets_diff


def test_source_keys_complete():
    assert SOURCE_KEYS == [
        "mol_labor_brackets",
        "mol_labor_premium",
        "mol_pension",
        "nhi_brackets",
        "nhi_premium",
        "mol_minimum_wage",
    ]


def test_bracket_row_validation():
    row = BracketRow(
        amount=29500,
        labor_employee=590,
        labor_employer=2065,
        health_employee=458,
        health_employer=1428,
        pension=1770,
    )
    assert row.amount == 29500
    with pytest.raises(ValueError):
        BracketRow(
            amount=0,
            labor_employee=0,
            labor_employer=0,
            health_employee=0,
            health_employer=0,
            pension=0,
        )


def test_sha256_stable():
    a = sha256_of_payload({"a": 1, "b": [1, 2]})
    b = sha256_of_payload({"b": [1, 2], "a": 1})  # 鍵序不同
    assert a == b  # 應排序後 hash


def test_compute_brackets_diff_added_modified_removed():
    current = [
        {"amount": 29500, "labor_employee": 590},
        {"amount": 30300, "labor_employee": 606},
    ]
    new = [
        {"amount": 29500, "labor_employee": 600},  # modified
        {"amount": 31800, "labor_employee": 636},  # added
        # 30300 removed
    ]
    diff = compute_brackets_diff(current, new)
    assert {"amount": 29500, "field": "labor_employee", "old": 590, "new": 600} in diff[
        "modified"
    ]
    assert any(r["amount"] == 31800 for r in diff["added"])
    assert any(r["amount"] == 30300 for r in diff["removed"])

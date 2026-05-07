"""parser：6 source raw JSON → 內部 dataclass 測試。"""

import json
from datetime import date
from pathlib import Path

import pytest

from services.gov_data import parser
from services.gov_data.schemas import (
    LaborBracketsResult,
    LaborPremiumResult,
    MinimumWageResult,
    NhiBracketsResult,
    NhiPremiumResult,
    PensionResult,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "gov_data"


def _load(filename: str):
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


def test_parse_mol_labor_brackets_2026():
    raw = _load("mol_labor_brackets_2026.json")
    result = parser.parse_mol_labor_brackets(raw)
    assert isinstance(result, LaborBracketsResult)
    # 勞保最低投保薪資 11100（庇護性身心障礙者/部分工時勞工），基本工資 29500，最高 45800
    assert 11100 in result.amounts
    assert 29500 in result.amounts
    assert result.max_insured == 45800


def test_parse_mol_labor_premium_2026():
    raw = _load("mol_labor_premium_2026.json")
    result = parser.parse_mol_labor_premium(raw)
    assert isinstance(result, LaborPremiumResult)
    # amount=11100：勞工 277 / 雇主 972
    assert result.by_amount[11100]["labor_employee"] == 277
    assert result.by_amount[11100]["labor_employer"] == 972
    # amount=29500：勞工 738 / 雇主 2582
    assert result.by_amount[29500]["labor_employee"] == 738
    assert result.by_amount[29500]["labor_employer"] == 2582


def test_parse_mol_pension_2026():
    raw = _load("mol_pension_2026.json")
    result = parser.parse_mol_pension(raw)
    assert isinstance(result, PensionResult)
    assert 1500 in result.amounts
    assert 150000 in result.amounts
    # 勞退最高月提繳 150000
    assert result.max_insured == 150000


def test_parse_nhi_brackets_2026():
    raw = _load("nhi_brackets_2026.json")
    result = parser.parse_nhi_brackets(raw)
    assert isinstance(result, NhiBracketsResult)
    # 健保最低 29500；fixture 2026 最高為 313000
    assert 29500 in result.amounts
    assert result.max_insured == 313000


def test_parse_nhi_premium_2026():
    raw = _load("nhi_premium_2026.json")
    result = parser.parse_nhi_premium(raw)
    assert isinstance(result, NhiPremiumResult)
    # amount=29500：本人 458 / 投保單位 1428
    assert result.by_amount[29500]["single"]["employee"] == 458
    assert result.by_amount[29500]["single"]["employer"] == 1428
    # 眷屬欄位也應抽出（即使 IvyKids 不採加權，仍保留資料）
    assert 1 in result.by_amount[29500]["deps"]
    assert result.by_amount[29500]["deps"][1] == 916


def test_parse_mol_minimum_wage():
    raw = _load("mol_minimum_wage.json")
    result = parser.parse_mol_minimum_wage(raw)
    assert isinstance(result, MinimumWageResult)
    # fixture 最新一筆：實施日 2024-01-01、月薪 27470、時薪 183
    latest = result.latest()
    assert latest is not None
    eff_date, monthly, hourly = latest
    assert eff_date == date(2024, 1, 1)
    assert monthly == 27470
    assert hourly == 183


def test_parse_invalid_schema_raises():
    with pytest.raises(parser.ParserError):
        parser.parse_mol_labor_brackets({"unexpected": "shape"})

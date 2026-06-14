"""reference_data.py 法定參考資料的契約測試。

斷言 canonical 值筆數與鍵名對齊 model 欄位（不連 DB，純常數驗證）。
"""

from scripts.seedgen.reference_data import (
    appraisal_catalog,
    base_salary_for_role,
    insurance_brackets,
    insurance_rates,
    position_salary_standards,
)

# m01 會用到的 7 個 role key（對齊「共享契約」employees_by_role）。
_SEVEN_ROLES = (
    "supervisor",
    "admin",
    "accountant",
    "homeroom",
    "assistant",
    "art",
    "support",
)

# migration ``_BRACKETS_2026`` 的 canonical 筆數（單一年度）。
_BRACKETS_PER_YEAR = 82

_BRACKET_REQUIRED_KEYS = {
    "effective_year",
    "amount",
    "labor_employee",
    "labor_employer",
    "health_employee",
    "health_employer",
    "pension",
}


def test_insurance_brackets_count_matches_migration():
    """單一年度筆數 == migration 的 82 筆。"""
    rows_2026 = insurance_brackets(years=(2026,))
    assert len(rows_2026) == _BRACKETS_PER_YEAR


def test_insurance_brackets_default_two_config_years():
    """預設跨 2025/2026 兩套，總筆數 == 82 × 2。"""
    rows = insurance_brackets()
    assert len(rows) == _BRACKETS_PER_YEAR * 2
    years = {r["effective_year"] for r in rows}
    assert years == {2025, 2026}


def test_insurance_brackets_required_keys():
    """每筆含必要鍵（對齊 InsuranceBracket 欄位）。"""
    for row in insurance_brackets():
        assert _BRACKET_REQUIRED_KEYS <= set(row.keys())
        # 投保金額與各負擔金額皆為正整數
        assert isinstance(row["amount"], int) and row["amount"] > 0


def test_insurance_rates_cover_two_years():
    """費率每年一列，含 labor_rate/health_rate 等核心鍵。"""
    rows = insurance_rates()
    assert {r["rate_year"] for r in rows} == {2025, 2026}
    for row in rows:
        assert row["labor_rate"] == 0.125
        assert row["health_rate"] == 0.0517
        assert "labor_max_insured" in row


def test_position_salary_standards_cover_seven_roles():
    """7 職稱皆能解析到標準底薪欄位（涵蓋 m01 配比）。"""
    standards = position_salary_standards()
    for role in _SEVEN_ROLES:
        # 每個 role 都對得到 PositionSalaryConfig 的底薪欄位
        value = base_salary_for_role(role)
        # 值為正整數，或 supervisor 走 director(None)＝由個人底薪決定
        assert value is None or (isinstance(value, int) and value > 0)
    # 班導/助教/才藝/行政底薪與引擎 _POSITION_SALARY_DEFAULTS 對齊
    assert standards["head_teacher_b"] == 37160
    assert standards["assistant_teacher_b"] == 33000
    assert standards["art_teacher"] == 30000
    assert standards["admin_staff"] == 37160


def test_appraisal_catalog_is_15_items():
    """考核計分目錄為 15 項，鍵名對齊 AppraisalScoreItemCatalog 欄位。"""
    catalog = appraisal_catalog()
    assert len(catalog) == 15
    required = {
        "code",
        "label",
        "sign",
        "default_weight",
        "data_source",
        "description",
        "display_order",
        "is_active",
    }
    codes = set()
    for item in catalog:
        assert required <= set(item.keys())
        assert item["sign"] in {"POSITIVE", "NEGATIVE", "NEUTRAL"}
        codes.add(item["code"])
    # code 唯一且含關鍵項
    assert len(codes) == 15
    assert {"LEAVE", "LATE_EARLY", "REWARD_PUNISH"} <= codes
    # display_order 為 1..15 連續
    orders = sorted(item["display_order"] for item in catalog)
    assert orders == list(range(1, 16))

"""薪資設定的期間感知解析（period-aware config resolver）。

取代散落在 engine.py / insurance_service.py 的 `is_active + id.desc()`（當期）與
`created_at <= 月底`（歷史）兩套查詢。一律以設定表的「年度欄位 + 最高 version」解析，
讓年中訂正回溯套用整年；該年度無設定列即 fail-loud。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PayrollConfigMissingError(Exception):
    """指定年度的薪資設定列不存在（fail-loud）。

    caller（API 層 / bulk 預檢）應接住此例外並回可讀訊息，要求行政先建立該年度設定。
    """

    def __init__(self, config_type: str, year: int):
        self.config_type = config_type
        self.year = year
        super().__init__(
            f"找不到 {year} 年度的「{config_type}」設定，"
            f"請先於設定頁建立該年度設定後再結算。"
        )


def resolve_config(
    session, model, year: int, *, year_col: str, version_col: str = "version"
):
    """回傳該年度「最高 version」的設定列；無列則 raise PayrollConfigMissingError。

    Args:
        session: SQLAlchemy session。
        model: ORM 設定 model class（須有 year_col、version_col、id 欄位）。
        year: 西元年度。
        year_col: 年度欄位名（如 'config_year' / 'rate_year'）。
        version_col: 同年內排序用欄位（預設 'version'）。
    """
    row = (
        session.query(model)
        .filter(getattr(model, year_col) == year)
        .order_by(getattr(model, version_col).desc(), model.id.desc())
        .first()
    )
    if row is None:
        raise PayrollConfigMissingError(model.__name__, year)
    return row


def resolve_brackets(session, year: int) -> list[dict]:
    """回傳該年度全部投保級距（依 amount 升冪）；無列則 raise。

    brackets 一年一組（多列），故與 resolve_config（回單列）分開。回傳 dict list
    對齊 InsuranceService.table 既有結構。
    """
    from models.database import InsuranceBracket

    rows = (
        session.query(InsuranceBracket)
        .filter(InsuranceBracket.effective_year == year)
        .order_by(InsuranceBracket.amount.asc())
        .all()
    )
    if not rows:
        raise PayrollConfigMissingError("InsuranceBracket", year)
    return [
        {
            "amount": r.amount,
            "labor_employee": r.labor_employee,
            "labor_employer": r.labor_employer,
            "health_employee": r.health_employee,
            "health_employer": r.health_employer,
            "pension": r.pension,
        }
        for r in rows
    ]

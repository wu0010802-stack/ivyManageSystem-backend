"""Shared salary display helpers for API responses."""


def calculate_display_bonus_total(record) -> float:
    """Display-only bonus total used by portal/history/reporting surfaces.

    `bonus_amount` is excluded because it already aggregates separate fields in
    the persisted record and would double-count festival/overtime bonuses.

    `supervisor_dividend` 亦刻意**不**納入本合計：前端歷史頁將主管紅利列為
    獨立欄位顯示；若加入此合計會與頁面的「主管紅利」欄位造成雙計視覺。
    如需包含主管紅利的完整現金流，應使用 `SalaryRecord.bonus_amount`
    （= festival + overtime + supervisor_dividend）。
    """
    return (
        (getattr(record, "festival_bonus", 0) or 0)
        + (getattr(record, "overtime_bonus", 0) or 0)
        + (getattr(record, "performance_bonus", 0) or 0)
        + (getattr(record, "special_bonus", 0) or 0)
    )

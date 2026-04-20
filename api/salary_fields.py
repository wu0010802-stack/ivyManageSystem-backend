"""Shared salary display helpers for API responses."""


def calculate_display_bonus_total(record) -> float:
    """Display-only bonus total used by portal/history/reporting surfaces.

    `bonus_amount` is excluded because it already aggregates separate fields in
    the persisted record and would double-count festival/overtime bonuses.
    """
    return (
        (getattr(record, "festival_bonus", 0) or 0)
        + (getattr(record, "overtime_bonus", 0) or 0)
        + (getattr(record, "performance_bonus", 0) or 0)
        + (getattr(record, "special_bonus", 0) or 0)
    )

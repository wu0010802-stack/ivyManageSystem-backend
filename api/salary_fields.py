"""Shared salary display helpers for API responses."""


def calculate_total_allowances(record) -> float:
    return (
        (getattr(record, "supervisor_allowance", 0) or 0) +
        (getattr(record, "teacher_allowance", 0) or 0) +
        (getattr(record, "meal_allowance", 0) or 0) +
        (getattr(record, "transportation_allowance", 0) or 0) +
        (getattr(record, "other_allowance", 0) or 0)
    )


def calculate_display_bonus_total(record) -> float:
    """Display-only bonus total used by portal/history/reporting surfaces.

    `bonus_amount` is excluded because it already aggregates separate fields in
    the persisted record and would double-count festival/overtime bonuses.
    """
    return (
        (getattr(record, "festival_bonus", 0) or 0) +
        (getattr(record, "overtime_bonus", 0) or 0) +
        (getattr(record, "performance_bonus", 0) or 0) +
        (getattr(record, "special_bonus", 0) or 0)
    )

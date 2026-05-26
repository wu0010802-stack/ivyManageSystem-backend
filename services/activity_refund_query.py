"""router-side helper：query attendance + course.sessions → 餵 calculator。

對應 spec §6 build_refund_suggestion。endpoint 與 POS verify 共用此 helper。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    ActivitySupply,
    RegistrationCourse,
    RegistrationSupply,
)
from services.activity_refund_calculator import (
    calc_course_refund,
    calc_supply_refund,
)


def build_refund_suggestion(session: Session, reg_id: int) -> dict[str, Any]:
    """組裝 registration-level 退費建議（spec §6）。

    Args:
        session: SQLAlchemy session
        reg_id: ActivityRegistration.id

    Raises:
        ValueError: reg 不存在或 is_active=False

    Returns:
        spec §6 結構：registration_id, computed_at, total_suggested_amount,
        total_amount_due, items[].
    """
    reg = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.id == reg_id,
            ActivityRegistration.is_active.is_(True),
        )
        .first()
    )
    if reg is None:
        raise ValueError(f"registration {reg_id} not found or inactive")

    items: list[dict[str, Any]] = []
    total_suggested = 0
    total_amount_due = 0

    # ── 課程 items（僅 status='enrolled'）─────────────────────────────────
    course_rows = (
        session.query(RegistrationCourse, ActivityCourse)
        .join(ActivityCourse, ActivityCourse.id == RegistrationCourse.course_id)
        .filter(
            RegistrationCourse.registration_id == reg_id,
            RegistrationCourse.status == "enrolled",
        )
        .all()
    )

    for rc, course in course_rows:
        amount_due = int(rc.price_snapshot or 0)
        total_amount_due += amount_due

        if course.sessions is None or course.sessions <= 0:
            # NULL sessions: item.suggested=None + warning，total 採 amount_due fallback
            items.append(
                {
                    "type": "course",
                    "target_id": course.id,
                    "name": course.name,
                    "amount_due": amount_due,
                    "suggested_amount": None,
                    "calc_method": "activity_course_unknown_total",
                    "calc_payload": {
                        "amount_due": amount_due,
                        "formula": "課程總堂數未設定，採保守 fallback 為 amount_due（全退）",
                    },
                    "warnings": [
                        "課程未設定總堂數（ActivityCourse.sessions IS NULL），"
                        "採保守 fallback 全退；請 admin 補設定後重算。"
                    ],
                }
            )
            total_suggested += amount_due
            continue

        T_served = (
            session.query(func.count(ActivityAttendance.id))
            .join(ActivitySession, ActivitySession.id == ActivityAttendance.session_id)
            .filter(
                ActivityAttendance.registration_id == reg_id,
                ActivitySession.course_id == course.id,
                ActivityAttendance.is_present.is_(True),
            )
            .scalar()
        ) or 0

        result = calc_course_refund(
            amount_due=amount_due,
            T_total=int(course.sessions),
            T_served=int(T_served),
        )
        items.append(
            {
                "type": "course",
                "target_id": course.id,
                "name": course.name,
                "amount_due": amount_due,
                "suggested_amount": result["suggested_amount"],
                "calc_method": result["calc_method"],
                "calc_payload": result["calc_payload"],
                "warnings": result["warnings"],
            }
        )
        total_suggested += result["suggested_amount"]

    # ── 用品 items（一律不退）─────────────────────────────────────────────
    supply_rows = (
        session.query(RegistrationSupply, ActivitySupply)
        .join(ActivitySupply, ActivitySupply.id == RegistrationSupply.supply_id)
        .filter(RegistrationSupply.registration_id == reg_id)
        .all()
    )
    for rs, sup in supply_rows:
        amount_due = int(rs.price_snapshot or 0)
        total_amount_due += amount_due
        result = calc_supply_refund(amount_due=amount_due)
        items.append(
            {
                "type": "supply",
                "target_id": sup.id,
                "name": sup.name,
                "amount_due": amount_due,
                "suggested_amount": result["suggested_amount"],
                "calc_method": result["calc_method"],
                "calc_payload": result["calc_payload"],
                "warnings": result["warnings"],
            }
        )
        # supply suggested=0，不增 total_suggested

    return {
        "registration_id": reg_id,
        "computed_at": datetime.utcnow().isoformat(),
        "total_suggested_amount": total_suggested,
        "total_amount_due": total_amount_due,
        "items": items,
    }

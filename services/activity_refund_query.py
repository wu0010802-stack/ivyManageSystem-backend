"""router-side helper：query attendance + course.sessions → 餵 calculator。

對應 spec §6 build_refund_suggestion。endpoint 與 POS verify 共用此 helper。
"""

from __future__ import annotations

from datetime import datetime
from utils.taipei_time import now_taipei_naive, today_taipei
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivityPaymentRecord,
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
    needs_manual_review = False  # True when any course has unknown total sessions

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

    # 一次 GROUP BY 取回該 reg 各課程的 T_served（出席堂數），消除原本 loop 內
    # 逐課一次 COUNT 的 N+1（此 helper 被 POS 退費 verify 共用、且在持鎖路徑內，
    # 往返放大會拉長鎖持有時間）。缺 key → 0，與原 `.scalar() or 0` 等價。
    served_by_course = {
        course_id: count
        for course_id, count in (
            session.query(ActivitySession.course_id, func.count(ActivityAttendance.id))
            .select_from(ActivityAttendance)
            .join(ActivitySession, ActivitySession.id == ActivityAttendance.session_id)
            .filter(
                ActivityAttendance.registration_id == reg_id,
                ActivityAttendance.is_present.is_(True),
                # 只計「已上課」場次：未來場次即使被預先點名 is_present 也不算
                # 已出席堂數，否則 T_served 膨脹會讓退款建議被低估（少退、虧家長），
                # 並使「實退 vs 建議偏離簽核閘」以被膨脹值為基準而失效。
                # 用台北今日 date 比對（含當日），避免 naive UTC 偏移。
                ActivitySession.session_date <= today_taipei(),
            )
            .group_by(ActivitySession.course_id)
            .all()
        )
    }

    for rc, course in course_rows:
        amount_due = int(rc.price_snapshot or 0)
        total_amount_due += amount_due

        if course.sessions is None or course.sessions <= 0:
            # NULL sessions: item.suggested=None + warning，total 採 amount_due fallback
            needs_manual_review = True  # cannot compute server-side suggestion
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

        T_served = served_by_course.get(course.id, 0)

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

    # ── 已退累計 → 剩餘建議額 ─────────────────────────────────────────────
    # prior_refunded：該 reg 過去未作廢（voided_at IS NULL）的退費金額累計，
    # 與簽核閘 require_approve_for_cumulative_refund / refund_diff 同口徑。
    # remaining_suggested：total_suggested 扣掉已退、夾到 0。前端應預填此值（非
    # total_suggested），否則第二次退費會把全額建議再預填一次，使累積實退超過建議
    # 總額而踩到 diff 簽核閘容差（2026-06-29 audit F1）。
    prior_refunded = (
        session.query(func.coalesce(func.sum(ActivityPaymentRecord.amount), 0))
        .filter(
            ActivityPaymentRecord.registration_id == reg_id,
            ActivityPaymentRecord.type == "refund",
            ActivityPaymentRecord.voided_at.is_(None),
        )
        .scalar()
    ) or 0
    prior_refunded = int(prior_refunded)
    remaining_suggested = max(total_suggested - prior_refunded, 0)

    return {
        "registration_id": reg_id,
        "computed_at": now_taipei_naive().isoformat(),
        "total_suggested_amount": total_suggested,
        "total_amount_due": total_amount_due,
        "prior_refunded_amount": prior_refunded,
        "remaining_suggested_amount": remaining_suggested,
        "needs_manual_review": needs_manual_review,
        "items": items,
    }

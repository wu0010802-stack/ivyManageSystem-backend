"""園內 kiosk 即時打卡端點（IP 白名單 + PIN，無 JWT）。"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from models.database import get_session, Employee, Attendance
from utils.taipei_time import today_taipei
from utils.kiosk_guard import assert_kiosk_ip_allowed
from utils.errors import raise_safe_500

logger = logging.getLogger(__name__)
router = APIRouter()


class KioskRosterEntry(BaseModel):
    employee_id: int
    name: str
    has_pin: bool
    today_state: str  # none / in_only / done


@router.get(
    "/kiosk/roster",
    response_model=list[KioskRosterEntry],
    dependencies=[Depends(assert_kiosk_ip_allowed)],
)
def kiosk_roster():
    """打卡名單：在職員工 + 是否已設 PIN + 今日打卡狀態。最小揭露，無 PII。"""
    session = get_session()
    try:
        emps = (
            session.query(Employee)
            .filter(
                Employee.is_active == True, Employee.resign_date.is_(None)
            )  # noqa: E712
            .order_by(Employee.name)
            .all()
        )
        today = today_taipei()
        rows = (
            (
                session.query(Attendance)
                .filter(
                    Attendance.attendance_date == today,
                    Attendance.employee_id.in_([e.id for e in emps]),
                )
                .all()
            )
            if emps
            else []
        )
        by_emp = {a.employee_id: a for a in rows}

        out = []
        for e in emps:
            a = by_emp.get(e.id)
            if a is None or a.punch_in_time is None:
                state = "none"
            elif a.punch_out_time is None:
                state = "in_only"
            else:
                state = "done"
            out.append(
                KioskRosterEntry(
                    employee_id=e.id,
                    name=e.name,
                    has_pin=e.punch_pin_hash is not None,
                    today_state=state,
                )
            )
        return out
    except Exception as e:
        raise_safe_500(e)
    finally:
        session.close()

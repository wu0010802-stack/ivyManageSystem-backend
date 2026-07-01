"""園內 kiosk 即時打卡端點（IP 白名單 + PIN，無 JWT）。"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from models.database import get_session, Employee, Attendance
from utils.taipei_time import today_taipei, now_taipei_naive
from utils.kiosk_guard import assert_kiosk_ip_allowed
from utils.errors import raise_safe_500
from utils.auth import verify_password
from utils.rate_limit import create_limiter
from services.attendance_kiosk import (
    apply_punch,
    MonthFinalizedError,
    resolve_punch_action,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# per-employee PIN 失敗限流（key=employee_id；不可 per-IP——kiosk 合法流量集中單一園內 IP）
_pin_fail_limiter = create_limiter(
    max_calls=5,
    window_seconds=900,
    name="kiosk_pin_fail",
    error_detail="PIN 錯誤次數過多，請稍後再試",
)


class KioskRosterEntry(BaseModel):
    employee_id: int
    name: str
    has_pin: bool
    today_state: str  # none / in_only / done


class KioskPunchRequest(BaseModel):
    employee_id: int
    pin: str

    @field_validator("pin")
    @classmethod
    def _valid_pin(cls, v: str) -> str:
        if not (v.isdigit() and 4 <= len(v) <= 6):
            raise ValueError("PIN 須為 4-6 位數字")
        return v


class KioskPreviewResponse(BaseModel):
    employee_name: str
    action: str
    will_overwrite: bool
    current_punch_out: Optional[datetime]
    server_time: datetime


class KioskPunchResponse(BaseModel):
    employee_name: str
    action: str
    punch_time: datetime
    status: str


def _authenticate_pin(session, employee_id: int, pin: str) -> Employee:
    """驗 PIN：成功回 Employee；無 PIN 400；錯誤 401（並記一次失敗，超限 429）。"""
    emp = (
        session.query(Employee)
        .filter(
            Employee.id == employee_id,
            Employee.is_active == True,  # noqa: E712
            Employee.resign_date.is_(None),
        )
        .first()
    )
    if emp is None:
        raise HTTPException(status_code=404, detail="找不到員工")
    if not emp.punch_pin_hash:
        raise HTTPException(
            status_code=400, detail="尚未設定打卡 PIN，請先至教師入口設定"
        )
    if not verify_password(pin, emp.punch_pin_hash):
        _pin_fail_limiter.check(str(employee_id))  # 記一次失敗；超限拋 429
        raise HTTPException(status_code=401, detail="PIN 錯誤")
    return emp


@router.post(
    "/kiosk/preview",
    response_model=KioskPreviewResponse,
    dependencies=[Depends(assert_kiosk_ip_allowed)],
)
def kiosk_preview(body: KioskPunchRequest):
    """打卡預判（確認用，不寫入）：驗 PIN 後回傳即將記為上班/下班。"""
    session = get_session()
    try:
        emp = _authenticate_pin(session, body.employee_id, body.pin)
        p = resolve_punch_action(session, emp, now_taipei_naive())
        return KioskPreviewResponse(
            employee_name=p.employee_name,
            action=p.action,
            will_overwrite=p.will_overwrite,
            current_punch_out=p.current_punch_out,
            server_time=p.server_time,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e)
    finally:
        session.close()


@router.post(
    "/kiosk/punch",
    response_model=KioskPunchResponse,
    dependencies=[Depends(assert_kiosk_ip_allowed)],
)
def kiosk_punch(body: KioskPunchRequest):
    """即時打卡：驗 PIN 後寫入伺服器當前時間（first-in/last-out）。body 無時間戳欄位，天然防止 self 反向注入。"""
    session = get_session()
    try:
        emp = _authenticate_pin(session, body.employee_id, body.pin)
        try:
            r = apply_punch(session, emp, now_taipei_naive())
        except MonthFinalizedError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return KioskPunchResponse(
            employee_name=r.employee_name,
            action=r.action,
            punch_time=r.punch_time,
            status=r.status,
        )
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


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

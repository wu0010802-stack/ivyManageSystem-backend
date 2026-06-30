"""教師自助設定打卡 PIN。"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from models.database import get_session
from utils.auth import get_current_user, hash_password
from utils.taipei_time import now_taipei_naive
from utils.errors import raise_safe_500
from api.portal._shared import _get_employee

logger = logging.getLogger(__name__)
router = APIRouter()


class PunchPinSetRequest(BaseModel):
    pin: str

    @field_validator("pin")
    @classmethod
    def _valid_pin(cls, v: str) -> str:
        if not (v.isdigit() and 4 <= len(v) <= 6):
            raise ValueError("PIN 須為 4-6 位數字")
        return v


@router.put("/me/punch-pin")
def set_my_punch_pin(
    body: PunchPinSetRequest,
    current_user: dict = Depends(get_current_user),
):
    """教師設定/更新自己的打卡 PIN（沿用 Portal 登入身分，不需舊 PIN）。"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)
        emp.punch_pin_hash = hash_password(body.pin)
        emp.punch_pin_set_at = now_taipei_naive()
        session.commit()
        return {"message": "打卡 PIN 已更新"}
    except HTTPException:
        raise
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()

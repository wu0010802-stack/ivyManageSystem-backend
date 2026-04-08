"""
共用的業務邏輯驗證函式，供管理端與 portal 路由使用。
"""

from datetime import datetime, date
from typing import Optional, Tuple

from fastapi import HTTPException

from utils.constants import VALID_ASSESSMENT_TYPES, VALID_DOMAINS, VALID_RATINGS, VALID_INCIDENT_TYPES, VALID_SEVERITIES


def validate_hhmm_format(v: Optional[str]) -> Optional[str]:
    """驗證並標準化 HH:MM 格式時間字串。None 值直接通過。
    Pydantic field_validator 使用時應 raise ValueError；
    此函式供一般呼叫端使用，拋出 ValueError。
    """
    if v is None:
        return v
    parts = v.strip().split(":")
    if len(parts) < 2:
        raise ValueError("時間格式錯誤，應為 HH:MM")
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError("時間格式錯誤，應為 HH:MM")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("時間值超出範圍（小時 0-23，分鐘 0-59）")
    return f"{h:02d}:{m:02d}"


def parse_date_range_params(
    start_date: Optional[str],
    end_date: Optional[str],
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """解析 start_date / end_date 查詢參數（YYYY-MM-DD 格式），
    end_date 自動補齊至當天 23:59:59。
    格式錯誤拋出 HTTPException 400。
    """
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date 格式錯誤，請使用 YYYY-MM-DD")
    if end_date:
        try:
            end_dt = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            raise HTTPException(status_code=400, detail="end_date 格式錯誤，請使用 YYYY-MM-DD")
    return start_dt, end_dt


def _validate_enum_field(field_name: str, value, valid_set: set) -> None:
    """若 value 非 None 且不在 valid_set 中，拋出 HTTPException 400。"""
    if value is not None and value not in valid_set:
        raise HTTPException(status_code=400, detail=f"無效的{field_name}，允許值：{valid_set}")


def validate_assessment_fields(
    assessment_type: Optional[str] = None,
    domain: Optional[str] = None,
    rating: Optional[str] = None,
) -> None:
    """驗證評量欄位，失敗拋出 HTTPException 400"""
    _validate_enum_field("評量類型", assessment_type, VALID_ASSESSMENT_TYPES)
    _validate_enum_field("領域",     domain,          VALID_DOMAINS)
    _validate_enum_field("評等",     rating,          VALID_RATINGS)


def validate_incident_fields(
    incident_type: Optional[str] = None,
    severity: Optional[str] = None,
) -> None:
    """驗證事件欄位，失敗拋出 HTTPException 400"""
    _validate_enum_field("事件類型", incident_type, VALID_INCIDENT_TYPES)
    _validate_enum_field("嚴重程度", severity,      VALID_SEVERITIES)


def parse_optional_date(value) -> Optional[date]:
    """將 'YYYY-MM-DD' 字串轉為 date，空值回傳 None。"""
    if not value:
        return None
    if isinstance(value, date):
        return value
    return datetime.strptime(value, '%Y-%m-%d').date()

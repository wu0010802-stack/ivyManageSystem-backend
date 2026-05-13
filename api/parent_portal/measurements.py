"""api/parent_portal/measurements.py — 家長端量測 read-only.

Endpoints:
- GET /api/parent/measurements?student_id=&months=
- GET /api/parent/measurements/chart-data?student_id=&months=
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from models.database import StudentMeasurement, get_session
from utils.auth import require_parent_role
from utils.errors import raise_safe_500

from ._shared import _assert_student_owned

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/measurements", tags=["parent-measurements"])


def _to_dict(m: StudentMeasurement) -> dict:
    return {
        "id": m.id,
        "measured_on": m.measured_on.isoformat() if m.measured_on else None,
        "height_cm": str(m.height_cm) if m.height_cm is not None else None,
        "weight_kg": str(m.weight_kg) if m.weight_kg is not None else None,
        "head_circumference_cm": (
            str(m.head_circumference_cm)
            if m.head_circumference_cm is not None
            else None
        ),
        "vision_left": str(m.vision_left) if m.vision_left is not None else None,
        "vision_right": str(m.vision_right) if m.vision_right is not None else None,
        "note": m.note,
    }


# months 上限 36（3 年足以涵蓋一個小孩的整個幼兒園生涯：學齡前 0-6 歲）；
# 原本 le=120 = 10 年資料一次性 dump，無 limit/skip 屬於 DoS 面（agent P2 #9）。
# limit cap 防單一學生意外累積大量量測時整批送出。
_MONTHS_MAX = 36
_HARD_ROW_LIMIT = 500


@router.get("")
async def parent_list_measurements(
    student_id: int = Query(...),
    months: int = Query(24, ge=1, le=_MONTHS_MAX),
    current_user: dict = Depends(require_parent_role()),
) -> dict:
    try:
        session = get_session()
        try:
            user_id = current_user["user_id"]
            _assert_student_owned(session, user_id, student_id)
            since = date.today() - timedelta(days=30 * months)
            rows = (
                session.query(StudentMeasurement)
                .filter(
                    StudentMeasurement.student_id == student_id,
                    StudentMeasurement.measured_on >= since,
                )
                .order_by(StudentMeasurement.measured_on.desc())
                .limit(_HARD_ROW_LIMIT)
                .all()
            )
            return {"items": [_to_dict(r) for r in rows]}
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端查詢量測失敗")


@router.get("/chart-data")
async def parent_measurement_chart(
    student_id: int = Query(...),
    months: int = Query(24, ge=1, le=_MONTHS_MAX),
    current_user: dict = Depends(require_parent_role()),
) -> dict:
    """回傳 admin chart-data 同結構 (asc by date)."""
    try:
        session = get_session()
        try:
            user_id = current_user["user_id"]
            _assert_student_owned(session, user_id, student_id)
            since = date.today() - timedelta(days=30 * months)
            rows = (
                session.query(StudentMeasurement)
                .filter(
                    StudentMeasurement.student_id == student_id,
                    StudentMeasurement.measured_on >= since,
                )
                .order_by(StudentMeasurement.measured_on.asc())
                .limit(_HARD_ROW_LIMIT)
                .all()
            )
            series: dict[str, list[dict]] = {
                "height": [],
                "weight": [],
                "head_circumference": [],
                "vision_left": [],
                "vision_right": [],
            }
            for r in rows:
                d = r.measured_on.isoformat()
                if r.height_cm is not None:
                    series["height"].append({"x": d, "y": str(r.height_cm)})
                if r.weight_kg is not None:
                    series["weight"].append({"x": d, "y": str(r.weight_kg)})
                if r.head_circumference_cm is not None:
                    series["head_circumference"].append(
                        {"x": d, "y": str(r.head_circumference_cm)}
                    )
                if r.vision_left is not None:
                    series["vision_left"].append({"x": d, "y": str(r.vision_left)})
                if r.vision_right is not None:
                    series["vision_right"].append({"x": d, "y": str(r.vision_right)})
            return series
        finally:
            session.close()
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="家長端量測曲線查詢失敗")

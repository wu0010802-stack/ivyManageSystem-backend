"""Portfolio Measurements router — 學生量測紀錄（身高/體重/視力/頭圍）.

路由：
- GET    /api/students/{student_id}/measurements
- POST   /api/students/{student_id}/measurements
- PATCH  /api/students/{student_id}/measurements/{m_id}
- DELETE /api/students/{student_id}/measurements/{m_id}
- GET    /api/students/{student_id}/measurements/chart-data

權限：
- READ  需 PORTFOLIO_READ
- WRITE 需 PORTFOLIO_WRITE
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from models.database import StudentMeasurement, User, session_scope
from utils.audit import write_explicit_audit
from utils.auth import require_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/students", tags=["portfolio-measurements"])


# ── 合理範圍常數 ────────────────────────────────────────────────────────
HEIGHT_MAX_CM = Decimal("200.00")
WEIGHT_MAX_KG = Decimal("100.00")
VISION_MIN = Decimal("0.10")
VISION_MAX = Decimal("2.00")


class MeasurementBase(BaseModel):
    measured_on: date
    height_cm: Optional[Decimal] = Field(
        default=None, ge=Decimal("0.01"), le=HEIGHT_MAX_CM
    )
    weight_kg: Optional[Decimal] = Field(
        default=None, ge=Decimal("0.01"), le=WEIGHT_MAX_KG
    )
    head_circumference_cm: Optional[Decimal] = Field(
        default=None, ge=Decimal("0.01"), le=HEIGHT_MAX_CM
    )
    vision_left: Optional[Decimal] = Field(default=None, ge=VISION_MIN, le=VISION_MAX)
    vision_right: Optional[Decimal] = Field(default=None, ge=VISION_MIN, le=VISION_MAX)
    note: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("measured_on")
    @classmethod
    def _no_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("measured_on 不可為未來日期")
        return v


class MeasurementCreate(MeasurementBase):
    @model_validator(mode="after")
    def _require_at_least_one_value(self) -> "MeasurementCreate":
        if all(
            getattr(self, k) is None
            for k in (
                "height_cm",
                "weight_kg",
                "head_circumference_cm",
                "vision_left",
                "vision_right",
            )
        ):
            raise ValueError("至少需提供一個量測值")
        return self


class MeasurementUpdate(BaseModel):
    measured_on: Optional[date] = None
    height_cm: Optional[Decimal] = Field(
        default=None, ge=Decimal("0.01"), le=HEIGHT_MAX_CM
    )
    weight_kg: Optional[Decimal] = Field(
        default=None, ge=Decimal("0.01"), le=WEIGHT_MAX_KG
    )
    head_circumference_cm: Optional[Decimal] = Field(
        default=None, ge=Decimal("0.01"), le=HEIGHT_MAX_CM
    )
    vision_left: Optional[Decimal] = Field(default=None, ge=VISION_MIN, le=VISION_MAX)
    vision_right: Optional[Decimal] = Field(default=None, ge=VISION_MIN, le=VISION_MAX)
    note: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("measured_on")
    @classmethod
    def _no_future(cls, v: Optional[date]) -> Optional[date]:
        if v is not None and v > date.today():
            raise ValueError("measured_on 不可為未來日期")
        return v


def _measurement_to_dict(m: StudentMeasurement) -> dict:
    return {
        "id": m.id,
        "student_id": m.student_id,
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
        "created_by": m.created_by,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


@router.get("/{student_id}/measurements")
async def list_measurements(
    student_id: int,
    request: Request,
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            query = session.query(StudentMeasurement).filter(
                StudentMeasurement.student_id == student_id
            )
            if from_date:
                query = query.filter(StudentMeasurement.measured_on >= from_date)
            if to_date:
                query = query.filter(StudentMeasurement.measured_on <= to_date)
            total = query.count()
            rows = (
                query.order_by(
                    StudentMeasurement.measured_on.desc(),
                    StudentMeasurement.id.desc(),
                )
                .offset(skip)
                .limit(limit)
                .all()
            )
            write_explicit_audit(
                request,
                action="READ",
                entity_type="student_measurement",
                entity_id=str(student_id),
                summary=f"查詢學生量測列表：student_id={student_id} total={total}",
                changes={
                    "from": from_date.isoformat() if from_date else None,
                    "to": to_date.isoformat() if to_date else None,
                    "total": total,
                    "returned": len(rows),
                },
                dedup=True,
            )
            return {
                "total": total,
                "items": [_measurement_to_dict(r) for r in rows],
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢量測紀錄失敗")


@router.post("/{student_id}/measurements", status_code=201)
async def create_measurement(
    student_id: int,
    payload: MeasurementCreate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            # created_by → employees.id；透過 User.employee_id 轉換
            user_id = current_user.get("user_id")
            employee_id: int | None = None
            if user_id is not None:
                u = session.query(User).filter(User.id == user_id).first()
                employee_id = u.employee_id if u else None
            m = StudentMeasurement(
                student_id=student_id,
                measured_on=payload.measured_on,
                height_cm=payload.height_cm,
                weight_kg=payload.weight_kg,
                head_circumference_cm=payload.head_circumference_cm,
                vision_left=payload.vision_left,
                vision_right=payload.vision_right,
                note=payload.note,
                created_by=employee_id,
            )
            session.add(m)
            session.flush()
            session.refresh(m)
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"新增量測：student_id={student_id} date={payload.measured_on}"
            )
            logger.info(
                "新增量測：student_id=%d m_id=%d operator=%s",
                student_id,
                m.id,
                current_user.get("username"),
            )
            return _measurement_to_dict(m)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增量測失敗")


@router.patch("/{student_id}/measurements/{m_id}")
async def update_measurement(
    student_id: int,
    m_id: int,
    payload: MeasurementUpdate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            m = (
                session.query(StudentMeasurement)
                .filter(
                    StudentMeasurement.id == m_id,
                    StudentMeasurement.student_id == student_id,
                )
                .first()
            )
            if not m:
                raise HTTPException(status_code=404, detail="量測紀錄不存在")
            data = payload.model_dump(exclude_unset=True)
            for key, value in data.items():
                setattr(m, key, value)
            session.flush()
            session.refresh(m)
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"更新量測：student_id={student_id} m_id={m_id}"
            )
            return _measurement_to_dict(m)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="更新量測失敗")


@router.delete("/{student_id}/measurements/{m_id}", status_code=204)
async def delete_measurement(
    student_id: int,
    m_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_WRITE)),
) -> Response:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            m = (
                session.query(StudentMeasurement)
                .filter(
                    StudentMeasurement.id == m_id,
                    StudentMeasurement.student_id == student_id,
                )
                .first()
            )
            if not m:
                raise HTTPException(status_code=404, detail="量測紀錄不存在")
            session.delete(m)
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"刪除量測：student_id={student_id} m_id={m_id}"
            )
            return Response(status_code=204)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="刪除量測失敗")


@router.get("/{student_id}/measurements/chart-data")
async def chart_data(
    student_id: int,
    request: Request,
    months: int = Query(24, ge=1, le=120),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> dict:
    """回傳折線圖用的 (x=date, y=value) 點陣列；asc 排序。

    結構：
    {
      "height": [{"x": "2025-05-01", "y": "100.00"}, ...],
      "weight": [...],
      "head_circumference": [...],
      "vision_left": [...],
      "vision_right": [...]
    }
    """
    from datetime import timedelta

    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            since = date.today() - timedelta(days=30 * months)
            rows = (
                session.query(StudentMeasurement)
                .filter(
                    StudentMeasurement.student_id == student_id,
                    StudentMeasurement.measured_on >= since,
                )
                .order_by(StudentMeasurement.measured_on.asc())
                .all()
            )
            write_explicit_audit(
                request,
                action="READ",
                entity_type="student_measurement",
                entity_id=str(student_id),
                summary=f"查詢量測圖表：student_id={student_id} months={months}",
                changes={"months": months, "points": len(rows)},
                dedup=True,
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
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="量測曲線資料查詢失敗")

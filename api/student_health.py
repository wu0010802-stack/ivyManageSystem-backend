"""
Student Health router — 過敏管理 + 用藥單管理 + 餵藥紀錄 + 今日用藥彙總

路由：
- 過敏
  - GET    /api/students/{id}/allergies
  - POST   /api/students/{id}/allergies
  - PATCH  /api/students/{id}/allergies/{alg_id}
  - DELETE /api/students/{id}/allergies/{alg_id}

- 用藥單（當日）
  - GET    /api/students/{id}/medication-orders?date=YYYY-MM-DD
  - POST   /api/students/{id}/medication-orders
  - GET    /api/students/{id}/medication-orders/{order_id}

- 餵藥紀錄（不可變；修正走 /correct）
  - POST   /api/medication-logs/{log_id}/administer
  - POST   /api/medication-logs/{log_id}/skip
  - POST   /api/medication-logs/{log_id}/correct

- 今日用藥彙總
  - GET    /api/portfolio/today-medication   （班級 scope）
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from models.database import (
    Student,
    StudentAllergy,
    StudentMedicationLog,
    StudentMedicationOrder,
    session_scope,
)
from models.portfolio import ALLERGY_SEVERITIES, MEDICATION_SOURCE_TEACHER
from utils.auth import require_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.portfolio_access import (
    assert_student_access,
    student_ids_in_scope,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["student-health"])


# ── Pydantic schemas ────────────────────────────────────────────────────


class AllergyCreate(BaseModel):
    allergen: str = Field(..., min_length=1, max_length=100)
    severity: Literal["mild", "moderate", "severe"]
    reaction_symptom: Optional[str] = Field(default=None, max_length=200)
    first_aid_note: Optional[str] = None
    active: bool = True


class AllergyUpdate(BaseModel):
    allergen: Optional[str] = Field(default=None, min_length=1, max_length=100)
    severity: Optional[Literal["mild", "moderate", "severe"]] = None
    reaction_symptom: Optional[str] = Field(default=None, max_length=200)
    first_aid_note: Optional[str] = None
    active: Optional[bool] = None


_TIME_SLOT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class MedicationOrderCreate(BaseModel):
    order_date: date
    medication_name: str = Field(..., min_length=1, max_length=100)
    dose: str = Field(..., min_length=1, max_length=50)
    time_slots: list[str] = Field(..., min_length=1, max_length=10)
    note: Optional[str] = None

    @field_validator("time_slots")
    @classmethod
    def _validate_slots(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        for slot in v:
            if not _TIME_SLOT_RE.match(slot):
                raise ValueError(f"時段格式錯誤（應為 HH:MM）：{slot}")
            if slot in seen:
                raise ValueError(f"時段重複：{slot}")
            seen.add(slot)
        return sorted(v)


class AdministerPayload(BaseModel):
    note: Optional[str] = Field(default=None, max_length=200)


class SkipPayload(BaseModel):
    skipped_reason: str = Field(..., min_length=1, max_length=200)


class CorrectPayload(BaseModel):
    """修正一筆已執行的 log；新增一筆 correction_of 指向原 log 的 log，原 log 不動。"""

    correction_reason: str = Field(..., min_length=1, max_length=200)
    # 修正後的狀態：administered_at / skipped / skipped_reason / note
    administered_at: Optional[datetime] = None
    skipped: bool = False
    skipped_reason: Optional[str] = Field(default=None, max_length=200)
    note: Optional[str] = Field(default=None, max_length=200)


# ── to_dict helpers ──────────────────────────────────────────────────────


def _allergy_to_dict(a: StudentAllergy) -> dict:
    return {
        "id": a.id,
        "student_id": a.student_id,
        "allergen": a.allergen,
        "severity": a.severity,
        "reaction_symptom": a.reaction_symptom,
        "first_aid_note": a.first_aid_note,
        "active": a.active,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _order_to_dict(
    order: StudentMedicationOrder, logs: list[StudentMedicationLog]
) -> dict:
    return {
        "id": order.id,
        "student_id": order.student_id,
        "order_date": order.order_date.isoformat(),
        "medication_name": order.medication_name,
        "dose": order.dose,
        "time_slots": list(order.time_slots or []),
        "note": order.note,
        "source": order.source,
        "created_by": order.created_by,
        "logs": [_log_to_dict(lg) for lg in logs],
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


def _log_to_dict(lg: StudentMedicationLog) -> dict:
    status: str
    if lg.correction_of is not None:
        status = "correction"
    elif lg.administered_at is not None:
        status = "administered"
    elif lg.skipped:
        status = "skipped"
    else:
        status = "pending"
    return {
        "id": lg.id,
        "order_id": lg.order_id,
        "scheduled_time": lg.scheduled_time,
        "status": status,
        "administered_at": (
            lg.administered_at.isoformat() if lg.administered_at else None
        ),
        "administered_by": lg.administered_by,
        "skipped": lg.skipped,
        "skipped_reason": lg.skipped_reason,
        "note": lg.note,
        "correction_of": lg.correction_of,
        "created_at": lg.created_at.isoformat() if lg.created_at else None,
    }


def _load_logs_for_order(session, order_id: int) -> list[StudentMedicationLog]:
    return (
        session.query(StudentMedicationLog)
        .filter(StudentMedicationLog.order_id == order_id)
        .order_by(
            StudentMedicationLog.scheduled_time.asc(),
            StudentMedicationLog.id.asc(),
        )
        .all()
    )


# ══════════════════════════════════════════════════════════════════════════
# 過敏管理
# ══════════════════════════════════════════════════════════════════════════


@router.get("/students/{student_id}/allergies")
async def list_allergies(
    student_id: int,
    include_inactive: bool = Query(False),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_HEALTH_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            query = session.query(StudentAllergy).filter(
                StudentAllergy.student_id == student_id
            )
            if not include_inactive:
                query = query.filter(StudentAllergy.active.is_(True))
            rows = query.order_by(StudentAllergy.id.asc()).all()
            return {
                "items": [_allergy_to_dict(a) for a in rows],
                "total": len(rows),
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢過敏失敗")


@router.post("/students/{student_id}/allergies", status_code=201)
async def create_allergy(
    student_id: int,
    payload: AllergyCreate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_HEALTH_WRITE)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            if payload.severity not in ALLERGY_SEVERITIES:
                raise HTTPException(status_code=400, detail="severity 不合法")

            a = StudentAllergy(
                student_id=student_id,
                allergen=payload.allergen,
                severity=payload.severity,
                reaction_symptom=payload.reaction_symptom,
                first_aid_note=payload.first_aid_note,
                active=payload.active,
                created_by=current_user.get("user_id"),
            )
            session.add(a)
            session.flush()
            session.refresh(a)
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"新增過敏紀錄：allergen={payload.allergen}, "
                f"severity={payload.severity}"
            )
            logger.info(
                "新增過敏：student_id=%d allergen=%s severity=%s operator=%s",
                student_id,
                payload.allergen,
                payload.severity,
                current_user.get("username"),
            )
            return _allergy_to_dict(a)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增過敏失敗")


@router.patch("/students/{student_id}/allergies/{alg_id}")
async def update_allergy(
    student_id: int,
    alg_id: int,
    payload: AllergyUpdate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_HEALTH_WRITE)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            a = (
                session.query(StudentAllergy)
                .filter(
                    StudentAllergy.id == alg_id,
                    StudentAllergy.student_id == student_id,
                )
                .first()
            )
            if not a:
                raise HTTPException(status_code=404, detail="過敏紀錄不存在")

            data = payload.model_dump(exclude_unset=True)
            for k, v in data.items():
                setattr(a, k, v)
            a.updated_at = datetime.now()
            session.flush()
            session.refresh(a)

            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"編輯過敏：alg_id={alg_id} fields={list(data.keys())}"
            )
            return _allergy_to_dict(a)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="編輯過敏失敗")


@router.delete("/students/{student_id}/allergies/{alg_id}")
async def delete_allergy(
    student_id: int,
    alg_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_HEALTH_WRITE)),
) -> dict:
    """直接刪除過敏紀錄（不做軟刪；若要保留歷史請用 PATCH active=false）。"""
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            a = (
                session.query(StudentAllergy)
                .filter(
                    StudentAllergy.id == alg_id,
                    StudentAllergy.student_id == student_id,
                )
                .first()
            )
            if not a:
                raise HTTPException(status_code=404, detail="過敏紀錄不存在")
            session.delete(a)
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = f"刪除過敏：alg_id={alg_id}"
            return {"message": "刪除成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="刪除過敏失敗")


# ══════════════════════════════════════════════════════════════════════════
# 用藥單管理
# ══════════════════════════════════════════════════════════════════════════


@router.get("/students/{student_id}/medication-orders")
async def list_medication_orders(
    student_id: int,
    order_date: Optional[date] = Query(None, alias="date"),
    current_user: dict = Depends(require_permission(Permission.STUDENTS_HEALTH_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            query = session.query(StudentMedicationOrder).filter(
                StudentMedicationOrder.student_id == student_id
            )
            if order_date:
                query = query.filter(StudentMedicationOrder.order_date == order_date)
            orders = query.order_by(
                StudentMedicationOrder.order_date.desc(),
                StudentMedicationOrder.id.desc(),
            ).all()
            items = []
            for o in orders:
                logs = _load_logs_for_order(session, o.id)
                items.append(_order_to_dict(o, logs))
            return {"items": items, "total": len(items)}
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢用藥單失敗")


@router.get("/students/{student_id}/medication-orders/{order_id}")
async def get_medication_order(
    student_id: int,
    order_id: int,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_HEALTH_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)
            o = (
                session.query(StudentMedicationOrder)
                .filter(
                    StudentMedicationOrder.id == order_id,
                    StudentMedicationOrder.student_id == student_id,
                )
                .first()
            )
            if not o:
                raise HTTPException(status_code=404, detail="用藥單不存在")
            logs = _load_logs_for_order(session, o.id)
            return _order_to_dict(o, logs)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢用藥單失敗")


@router.post("/students/{student_id}/medication-orders", status_code=201)
async def create_medication_order(
    student_id: int,
    payload: MedicationOrderCreate,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.STUDENTS_HEALTH_WRITE)),
) -> dict:
    """建立當日用藥單。會自動依 time_slots 預建 N 筆 pending logs。"""
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id)

            order = StudentMedicationOrder(
                student_id=student_id,
                order_date=payload.order_date,
                medication_name=payload.medication_name,
                dose=payload.dose,
                time_slots=payload.time_slots,
                note=payload.note,
                created_by=current_user.get("user_id"),
                source=MEDICATION_SOURCE_TEACHER,
            )
            session.add(order)
            session.flush()

            # 預建 pending logs
            for slot in payload.time_slots:
                session.add(
                    StudentMedicationLog(
                        order_id=order.id,
                        scheduled_time=slot,
                    )
                )
            session.flush()
            session.refresh(order)
            logs = _load_logs_for_order(session, order.id)

            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"新增用藥單：order_id={order.id} "
                f"date={payload.order_date} "
                f"medication={payload.medication_name} "
                f"slots={payload.time_slots}"
            )
            logger.info(
                "新增用藥單：student_id=%d order_id=%d slots=%s operator=%s",
                student_id,
                order.id,
                payload.time_slots,
                current_user.get("username"),
            )
            return _order_to_dict(order, logs)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="新增用藥單失敗")


# ══════════════════════════════════════════════════════════════════════════
# 餵藥紀錄（不可變）
# ══════════════════════════════════════════════════════════════════════════


def _get_log_with_access(
    session, log_id: int, current_user: dict
) -> tuple[StudentMedicationLog, StudentMedicationOrder, int]:
    """取 log + order，檢查學生班級 scope。回傳 (log, order, student_id)。"""
    lg = (
        session.query(StudentMedicationLog)
        .filter(StudentMedicationLog.id == log_id)
        .first()
    )
    if not lg:
        raise HTTPException(status_code=404, detail="餵藥紀錄不存在")
    o = (
        session.query(StudentMedicationOrder)
        .filter(StudentMedicationOrder.id == lg.order_id)
        .first()
    )
    if not o:
        raise HTTPException(status_code=500, detail="對應的用藥單不存在")
    assert_student_access(session, current_user, o.student_id)
    return lg, o, o.student_id


def _reject_if_finalized(lg: StudentMedicationLog) -> None:
    if lg.administered_at is not None or lg.skipped:
        raise HTTPException(
            status_code=409,
            detail="此紀錄已執行或已跳過，不可修改；請改用 /correct 新增修正紀錄",
        )


@router.post("/medication-logs/{log_id}/administer")
async def administer_medication(
    log_id: int,
    payload: AdministerPayload,
    request: Request,
    current_user: dict = Depends(
        require_permission(Permission.STUDENTS_MEDICATION_ADMINISTER)
    ),
) -> dict:
    """標記某筆 pending log 為「已餵藥」。"""
    try:
        with session_scope() as session:
            lg, o, student_id = _get_log_with_access(session, log_id, current_user)
            _reject_if_finalized(lg)

            lg.administered_at = datetime.now()
            lg.administered_by = current_user.get("user_id")
            if payload.note is not None:
                lg.note = payload.note
            session.flush()
            session.refresh(lg)

            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"餵藥：log_id={log_id} order_id={o.id} " f"slot={lg.scheduled_time}"
            )
            logger.info(
                "餵藥：log_id=%d student_id=%d slot=%s operator=%s",
                log_id,
                student_id,
                lg.scheduled_time,
                current_user.get("username"),
            )
            return _log_to_dict(lg)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="標記餵藥失敗")


@router.post("/medication-logs/{log_id}/skip")
async def skip_medication(
    log_id: int,
    payload: SkipPayload,
    request: Request,
    current_user: dict = Depends(
        require_permission(Permission.STUDENTS_MEDICATION_ADMINISTER)
    ),
) -> dict:
    """標記某筆 pending log 為「跳過」。"""
    try:
        with session_scope() as session:
            lg, o, student_id = _get_log_with_access(session, log_id, current_user)
            _reject_if_finalized(lg)

            lg.skipped = True
            lg.skipped_reason = payload.skipped_reason
            # 記錄操作者，方便稽核
            lg.administered_by = current_user.get("user_id")
            session.flush()
            session.refresh(lg)

            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"跳過餵藥：log_id={log_id} " f"reason={payload.skipped_reason}"
            )
            return _log_to_dict(lg)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="跳過餵藥失敗")


@router.post("/medication-logs/{log_id}/correct", status_code=201)
async def correct_medication_log(
    log_id: int,
    payload: CorrectPayload,
    request: Request,
    current_user: dict = Depends(
        require_permission(Permission.STUDENTS_MEDICATION_ADMINISTER)
    ),
) -> dict:
    """對一筆已 administered / skipped 的 log 新增修正紀錄。

    原 log **不動**（DB trigger 會阻擋 UPDATE）；本端點新增一筆 correction log，
    `correction_of` 指向原 log。
    """
    try:
        with session_scope() as session:
            lg, o, student_id = _get_log_with_access(session, log_id, current_user)
            # correction 只能 apply 在已執行 / 已跳過的 log 上
            if lg.administered_at is None and not lg.skipped:
                raise HTTPException(
                    status_code=409,
                    detail="此紀錄尚未執行，請直接 /administer 或 /skip",
                )
            if lg.correction_of is not None:
                raise HTTPException(
                    status_code=409,
                    detail="不能對已有 correction 的紀錄再做 correction",
                )
            if not (payload.administered_at or payload.skipped):
                raise HTTPException(
                    status_code=400,
                    detail="修正紀錄必須指定 administered_at 或 skipped=true 其一",
                )

            corr = StudentMedicationLog(
                order_id=lg.order_id,
                scheduled_time=lg.scheduled_time,
                administered_at=payload.administered_at,
                administered_by=current_user.get("user_id"),
                skipped=payload.skipped,
                skipped_reason=payload.skipped_reason,
                note=(
                    f"[修正] {payload.correction_reason}"
                    + (f" / {payload.note}" if payload.note else "")
                ),
                correction_of=lg.id,
            )
            session.add(corr)
            session.flush()
            session.refresh(corr)

            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"修正餵藥：original_log_id={log_id} "
                f"corr_log_id={corr.id} reason={payload.correction_reason}"
            )
            logger.warning(
                "修正餵藥：original=%d correction=%d student_id=%d operator=%s",
                log_id,
                corr.id,
                student_id,
                current_user.get("username"),
            )
            return _log_to_dict(corr)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="修正餵藥失敗")


# ══════════════════════════════════════════════════════════════════════════
# 今日用藥彙總
# ══════════════════════════════════════════════════════════════════════════


@router.get("/portfolio/today-medication")
async def today_medication_summary(
    current_user: dict = Depends(require_permission(Permission.STUDENTS_HEALTH_READ)),
) -> dict:
    """回傳呼叫者班級範圍內，今日所有用藥任務（pending + done）。"""
    try:
        today = date.today()
        with session_scope() as session:
            scope = student_ids_in_scope(session, current_user)
            query = session.query(StudentMedicationOrder).filter(
                StudentMedicationOrder.order_date == today
            )
            if scope is not None:
                if not scope:
                    return {
                        "date": today.isoformat(),
                        "pending": 0,
                        "administered": 0,
                        "skipped": 0,
                        "orders": [],
                    }
                query = query.filter(StudentMedicationOrder.student_id.in_(scope))

            orders = query.order_by(StudentMedicationOrder.id.asc()).all()

            # 一次撈所有相關 logs 避免 N+1
            order_ids = [o.id for o in orders]
            logs_by_order: dict[int, list[StudentMedicationLog]] = {}
            if order_ids:
                all_logs = (
                    session.query(StudentMedicationLog)
                    .filter(StudentMedicationLog.order_id.in_(order_ids))
                    .order_by(
                        StudentMedicationLog.scheduled_time.asc(),
                        StudentMedicationLog.id.asc(),
                    )
                    .all()
                )
                for lg in all_logs:
                    logs_by_order.setdefault(lg.order_id, []).append(lg)

            # 一次撈學生姓名、班級資訊
            student_ids = list({o.student_id for o in orders})
            students_by_id: dict[int, Student] = {}
            if student_ids:
                for s in (
                    session.query(Student).filter(Student.id.in_(student_ids)).all()
                ):
                    students_by_id[s.id] = s

            pending = administered = skipped = 0
            enriched_orders = []
            for o in orders:
                logs = logs_by_order.get(o.id, [])
                for lg in logs:
                    if lg.correction_of is not None:
                        continue  # 修正紀錄不計入狀態統計
                    if lg.administered_at is not None:
                        administered += 1
                    elif lg.skipped:
                        skipped += 1
                    else:
                        pending += 1
                s = students_by_id.get(o.student_id)
                enriched_orders.append(
                    {
                        **_order_to_dict(o, logs),
                        "student_name": s.name if s else None,
                        "classroom_id": s.classroom_id if s else None,
                    }
                )
            return {
                "date": today.isoformat(),
                "pending": pending,
                "administered": administered,
                "skipped": skipped,
                "orders": enriched_orders,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢今日用藥失敗")

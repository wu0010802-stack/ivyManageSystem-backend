"""MOE Phase 2 月報 API — generate / get / export。"""

from __future__ import annotations

import logging
from datetime import date, datetime
from utils.taipei_time import now_taipei_naive
from io import BytesIO
from typing import Literal
from urllib.parse import quote as _quote

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from models.classroom import Classroom
from models.gov_moe import MonthlyEnrollmentSnapshot
from services.gov_moe.monthly_calculator import build_snapshot_rows
from services.gov_moe.monthly_excel_writer import build_monthly_xlsx_bytes
from utils.audit import write_audit_in_session
from utils.auth import require_staff_permission
from utils.permissions import Permission

# 重用 disability_documents 的 get_db，確保測試環境 _SessionFactory 替換正確生效
from api.gov_moe.disability_documents import get_db

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/monthly", tags=["gov_moe_monthly"])

_MIN_YEAR = 2020
_MAX_YEAR_OFFSET = 1


class GenerateRequest(BaseModel):
    year: int
    month: int = Field(ge=1, le=12)


class GenerateResponse(BaseModel):
    year: int
    month: int
    rows_generated: int
    snapshot_date: date
    generated_at: datetime
    generated_by: str


def _validate_year(year: int) -> None:
    current_year = now_taipei_naive().year
    if year < _MIN_YEAR or year > current_year + _MAX_YEAR_OFFSET:
        raise HTTPException(
            status_code=400,
            detail=f"year 必須介於 {_MIN_YEAR}~{current_year + _MAX_YEAR_OFFSET}",
        )


def _try_advisory_lock(db: Session, year: int, month: int) -> bool:
    """PG advisory lock；若取不到回 False。SQLite / 非 PG 測試模式 fall back 永遠 True。"""
    try:
        dialect = db.get_bind().dialect.name
    except Exception:
        return True
    if dialect != "postgresql":
        _logger.warning("advisory lock skipped: dialect=%s (non-pg)", dialect)
        return True
    lock_key = abs(hash(f"moe_monthly_gen_{year}_{month}")) % (2**31)
    result = db.execute(
        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
    ).scalar()
    return bool(result)


def _identity_from_user(user: dict) -> str:
    return user.get("email") or user.get("username") or "unknown"


def _classroom_name_map(db: Session) -> dict[int, str]:
    return {c.id: c.name for c in db.query(Classroom).all()}


@router.post(
    "/generate",
    response_model=GenerateResponse,
)
def generate_monthly_report(
    payload: GenerateRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_staff_permission(Permission.GOV_REPORTS_EXPORT)
    ),
):
    _validate_year(payload.year)

    if not _try_advisory_lock(db, payload.year, payload.month):
        raise HTTPException(status_code=409, detail="另一個產生請求進行中")

    rows_before = (
        db.query(MonthlyEnrollmentSnapshot)
        .filter(
            MonthlyEnrollmentSnapshot.year == payload.year,
            MonthlyEnrollmentSnapshot.month == payload.month,
        )
        .count()
    )

    if rows_before > 0:
        db.query(MonthlyEnrollmentSnapshot).filter(
            MonthlyEnrollmentSnapshot.year == payload.year,
            MonthlyEnrollmentSnapshot.month == payload.month,
        ).delete()
        action = "REGENERATE"
    else:
        action = "GENERATE"

    identity = _identity_from_user(current_user)
    rows, _details = build_snapshot_rows(
        db, payload.year, payload.month, generated_by=identity
    )

    for r in rows:
        db.add(
            MonthlyEnrollmentSnapshot(
                year=r["year"],
                month=r["month"],
                classroom_id=r["classroom_id"],
                age_group=r["age_group"],
                total_count=r["total_count"],
                male_count=r["male_count"],
                female_count=r["female_count"],
                disadvantaged_count=r["disadvantaged_count"],
                disability_count=r["disability_count"],
                indigenous_count=r["indigenous_count"],
                foreign_count=r["foreign_count"],
                expected_attendance_days=r["expected_attendance_days"],
                actual_attendance_days=r["actual_attendance_days"],
                attendance_rate=r["attendance_rate"],
                snapshot_date=r["snapshot_date"],
                generated_at=r["generated_at"],
                generated_by=r["generated_by"],
            )
        )

    write_audit_in_session(
        db,
        request,
        action=action,
        entity_type="monthly_enrollment_snapshot",
        summary=f"月報 {payload.year}-{payload.month:02d} {action.lower()}",
        entity_id=f"{payload.year}-{payload.month:02d}",
        changes={
            "rows_before": rows_before,
            "rows_after": len(rows),
            "year": payload.year,
            "month": payload.month,
        },
    )

    db.commit()

    snapshot_date = date(payload.year, payload.month, 1) + relativedelta(
        months=1, days=-1
    )

    return GenerateResponse(
        year=payload.year,
        month=payload.month,
        rows_generated=len(rows),
        snapshot_date=snapshot_date,
        generated_at=now_taipei_naive(),
        generated_by=identity,
    )


@router.get(
    "",
    dependencies=[Depends(require_staff_permission(Permission.GOV_REPORTS_VIEW))],
)
def get_monthly_report(
    year: int = Query(..., ge=2020, le=2100),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
):
    snapshot_rows = (
        db.query(MonthlyEnrollmentSnapshot)
        .filter(
            MonthlyEnrollmentSnapshot.year == year,
            MonthlyEnrollmentSnapshot.month == month,
        )
        .all()
    )
    if not snapshot_rows:
        raise HTTPException(status_code=404, detail="尚未產生此月份月報")

    cls_map = _classroom_name_map(db)

    classroom_summary = []
    total_exp = total_act = total_students = 0
    total_disadv = total_disab = total_ind = total_for = 0
    by_age_group: dict[str, int] = {"2-3": 0, "3-4": 0, "4-5": 0, "5-6": 0}

    for r in snapshot_rows:
        classroom_summary.append(
            {
                "classroom_id": r.classroom_id,
                "classroom_name": cls_map.get(r.classroom_id, "(未分班)"),
                "age_group": r.age_group or "未知",
                "expected_days": r.expected_attendance_days,
                "actual_days": r.actual_attendance_days,
                "attendance_rate_pct": round(r.attendance_rate / 100, 2),
                "total_count": r.total_count,
                "male_count": r.male_count,
                "female_count": r.female_count,
                "disadvantaged_count": r.disadvantaged_count,
                "disability_count": r.disability_count,
                "indigenous_count": r.indigenous_count,
                "foreign_count": r.foreign_count,
            }
        )
        total_exp += r.expected_attendance_days
        total_act += r.actual_attendance_days
        total_students += r.total_count
        total_disadv += r.disadvantaged_count
        total_disab += r.disability_count
        total_ind += r.indigenous_count
        total_for += r.foreign_count
        if r.age_group in by_age_group:
            by_age_group[r.age_group] += r.total_count

    # NOTE: student_detail 採 live-recompute 而非從 snapshot 表讀回，
    # 因 Phase 2 spec 沒有為 per-student rows 建表。
    # 若使用者改動學生/出勤資料後 generate 未重跑，group aggregates（frozen）
    # 與 student_detail（live）可能不一致 — 解法是請使用者「重算本月」。
    # 未來如果要嚴格一致性，可 (a) 新建 monthly_student_detail 表或
    # (b) 改在 generate 時把 details JSONB 序列化到 snapshot row 上。
    _, student_details = build_snapshot_rows(db, year, month, generated_by="(query)")
    for sd in student_details:
        sd["classroom_name"] = cls_map.get(sd.get("classroom_id"), "(未分班)")

    overview = {
        "total_students": total_students,
        "by_age_group": by_age_group,
        "disadvantaged_pct": (
            round(total_disadv / total_students * 100, 2) if total_students else 0
        ),
        "disability_pct": (
            round(total_disab / total_students * 100, 2) if total_students else 0
        ),
        "indigenous_pct": (
            round(total_ind / total_students * 100, 2) if total_students else 0
        ),
        "foreign_pct": (
            round(total_for / total_students * 100, 2) if total_students else 0
        ),
        "total_expected_days": total_exp,
        "total_actual_days": total_act,
        "total_attendance_rate_pct": (
            round(total_act / total_exp * 100, 2) if total_exp else 0
        ),
    }

    first_row = snapshot_rows[0]
    return {
        "year": year,
        "month": month,
        "snapshot_date": (
            first_row.snapshot_date.isoformat() if first_row.snapshot_date else None
        ),
        "generated_at": (
            first_row.generated_at.isoformat() if first_row.generated_at else None
        ),
        "generated_by": first_row.generated_by,
        "classroom_summary": classroom_summary,
        "student_detail": student_details,
        "overview": overview,
    }


@router.get(
    "/export",
    dependencies=[Depends(require_staff_permission(Permission.GOV_REPORTS_EXPORT))],
)
def export_monthly_report(
    year: int = Query(..., ge=2020, le=2100),
    month: int = Query(..., ge=1, le=12),
    fmt: Literal["xlsx"] = Query("xlsx", alias="format"),
    db: Session = Depends(get_db),
):

    snapshot_rows = (
        db.query(MonthlyEnrollmentSnapshot)
        .filter(
            MonthlyEnrollmentSnapshot.year == year,
            MonthlyEnrollmentSnapshot.month == month,
        )
        .all()
    )
    if not snapshot_rows:
        raise HTTPException(status_code=404, detail="尚未產生此月份月報")

    cls_map = _classroom_name_map(db)
    rows_payload = []
    total_exp = total_act = total_students = 0
    total_disadv = total_disab = total_ind = total_for = 0
    by_age_group: dict[str, int] = {"2-3": 0, "3-4": 0, "4-5": 0, "5-6": 0}

    for r in snapshot_rows:
        rows_payload.append(
            {
                "classroom_name": cls_map.get(r.classroom_id, "(未分班)"),
                "teacher_names": "",  # TODO: wire classroom teacher names
                "age_group": r.age_group or "未知",
                "expected_attendance_days": r.expected_attendance_days,
                "actual_attendance_days": r.actual_attendance_days,
                "attendance_rate": r.attendance_rate,
                "male_count": r.male_count,
                "female_count": r.female_count,
                "disadvantaged_count": r.disadvantaged_count,
                "disability_count": r.disability_count,
                "indigenous_count": r.indigenous_count,
                "foreign_count": r.foreign_count,
            }
        )
        total_exp += r.expected_attendance_days
        total_act += r.actual_attendance_days
        total_students += r.total_count
        total_disadv += r.disadvantaged_count
        total_disab += r.disability_count
        total_ind += r.indigenous_count
        total_for += r.foreign_count
        if r.age_group in by_age_group:
            by_age_group[r.age_group] += r.total_count

    # NOTE: student_detail 採 live-recompute 而非從 snapshot 表讀回，
    # 因 Phase 2 spec 沒有為 per-student rows 建表。
    # 若使用者改動學生/出勤資料後 generate 未重跑，group aggregates（frozen）
    # 與 student_detail（live）可能不一致 — 解法是請使用者「重算本月」。
    # 未來如果要嚴格一致性，可 (a) 新建 monthly_student_detail 表或
    # (b) 改在 generate 時把 details JSONB 序列化到 snapshot row 上。
    _, student_details = build_snapshot_rows(db, year, month, generated_by="(export)")
    for sd in student_details:
        sd["classroom_name"] = cls_map.get(sd.get("classroom_id"), "(未分班)")

    first_row = snapshot_rows[0]
    overview = {
        "year": year,
        "month": month,
        "snapshot_date": first_row.snapshot_date,
        "generated_at": first_row.generated_at,
        "generated_by": first_row.generated_by,
        "total_students": total_students,
        "by_age_group": by_age_group,
        "disadvantaged_pct": (
            round(total_disadv / total_students * 100, 2) if total_students else 0
        ),
        "disability_pct": (
            round(total_disab / total_students * 100, 2) if total_students else 0
        ),
        "indigenous_pct": (
            round(total_ind / total_students * 100, 2) if total_students else 0
        ),
        "foreign_pct": (
            round(total_for / total_students * 100, 2) if total_students else 0
        ),
        "total_expected_days": total_exp,
        "total_actual_days": total_act,
        "total_attendance_rate_pct": (
            round(total_act / total_exp * 100, 2) if total_exp else 0
        ),
    }

    xlsx_bytes = build_monthly_xlsx_bytes(rows_payload, student_details, overview)
    today_str = now_taipei_naive().strftime("%Y-%m-%d")
    filename = f"義華幼兒園_月報_{year}-{month:02d}_產生於{today_str}.xlsx"

    # RFC 5987: encode non-ASCII filename as UTF-8 percent-encoded
    encoded_filename = _quote(filename, safe="")
    content_disposition = f"attachment; filename*=UTF-8''{encoded_filename}"

    return StreamingResponse(
        BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition},
    )

"""api/year_end — 年終獎金結算 API（M4 新增）。

提供 cycles / settlements / special_bonuses / org_settings / class_targets 與
Excel 雙向 I/O 端點。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from decimal import Decimal
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from models.base import get_session_dep, session_scope
from models.classroom import Classroom
from models.employee import Employee
from models.year_end import (
    ClassEnrollmentTarget,
    EmployeeYearEndSnapshot,
    OrgYearSettings,
    SpecialBonusItem,
    SpecialBonusType,
    YearEndCycle,
    YearEndCycleStatus,
    YearEndSettlement,
    YearEndSettlementStatus,
)
from schemas.year_end import (
    ClassEnrollmentTargetOut,
    OrgYearSettingsCreate,
    OrgYearSettingsOut,
    SettlementOut,
    SpecialBonusItemCreate,
    SpecialBonusItemOut,
    YearEndCycleCreate,
    YearEndCycleOut,
    YearEndImportResultOut,
)
from services.year_end.excel_io import (
    SummaryExportRow,
    TransferRow,
    export_year_end_summary_xlsx,
    export_year_end_transfer_xlsx,
    import_year_end_to_db,
    parse_year_end_excel,
)
from services.year_end.print_pdf import (
    PersonalBonusSlipData,
    SummaryTableRow as PdfSummaryRow,
    TransferEntry,
    generate_personal_bonus_slip_pdf,
    generate_summary_table_pdf,
    generate_transfer_roster_pdf,
)
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

year_end_router = APIRouter(prefix="/api/year_end", tags=["year_end"])


# ===== Cycles =====


@year_end_router.get("/cycles", response_model=list[YearEndCycleOut])
def list_cycles(
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    return (
        session.query(YearEndCycle).order_by(YearEndCycle.academic_year.desc()).all()
    )


@year_end_router.post("/cycles", response_model=YearEndCycleOut)
def create_cycle(
    payload: YearEndCycleCreate,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_FINALIZE)),
    session: Session = Depends(get_session_dep),
):
    if (
        session.query(YearEndCycle)
        .filter_by(academic_year=payload.academic_year)
        .first()
    ):
        raise HTTPException(409, "年度週期已存在")
    cycle = YearEndCycle(
        academic_year=payload.academic_year,
        start_date=payload.start_date,
        end_date=payload.end_date,
        bonus_calc_date=payload.bonus_calc_date,
        status=YearEndCycleStatus.OPEN,
        created_by=current_user.get("user_id"),
    )
    session.add(cycle)
    session.commit()
    session.refresh(cycle)
    return cycle


# ===== Org settings =====


@year_end_router.get(
    "/cycles/{cycle_id}/org_settings", response_model=list[OrgYearSettingsOut]
)
def list_org_settings(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    return session.query(OrgYearSettings).filter_by(year_end_cycle_id=cycle_id).all()


@year_end_router.post(
    "/cycles/{cycle_id}/org_settings", response_model=OrgYearSettingsOut
)
def upsert_org_settings(
    cycle_id: int,
    payload: OrgYearSettingsCreate,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_WRITE)),
    session: Session = Depends(get_session_dep),
):
    if not session.get(YearEndCycle, cycle_id):
        raise HTTPException(404, "cycle 不存在")
    existing = (
        session.query(OrgYearSettings)
        .filter_by(year_end_cycle_id=cycle_id, semester_first=payload.semester_first)
        .first()
    )
    if existing is None:
        existing = OrgYearSettings(
            year_end_cycle_id=cycle_id, **payload.model_dump()
        )
        session.add(existing)
    else:
        for k, v in payload.model_dump().items():
            setattr(existing, k, v)
    session.commit()
    session.refresh(existing)
    return existing


# ===== Class targets =====


@year_end_router.get(
    "/cycles/{cycle_id}/class_targets",
    response_model=list[ClassEnrollmentTargetOut],
)
def list_class_targets(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    return (
        session.query(ClassEnrollmentTarget)
        .filter_by(year_end_cycle_id=cycle_id)
        .order_by(
            ClassEnrollmentTarget.semester_first.desc(),
            ClassEnrollmentTarget.classroom_id,
        )
        .all()
    )


# ===== Settlements =====


@year_end_router.get(
    "/cycles/{cycle_id}/settlements", response_model=list[SettlementOut]
)
def list_settlements(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    return (
        session.query(YearEndSettlement)
        .filter_by(year_end_cycle_id=cycle_id)
        .order_by(YearEndSettlement.id)
        .all()
    )


@year_end_router.post(
    "/settlements/{settlement_id}/sign_supervisor", response_model=SettlementOut
)
def sign_supervisor(
    settlement_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_REVIEW)),
    session: Session = Depends(get_session_dep),
):
    s = session.get(YearEndSettlement, settlement_id)
    if s is None:
        raise HTTPException(404)
    if s.status != YearEndSettlementStatus.DRAFT:
        raise HTTPException(400, f"非 DRAFT (current={s.status.value})")
    s.status = YearEndSettlementStatus.SUPERVISOR_SIGNED
    s.supervisor_signed_by = current_user.get("user_id")
    from datetime import datetime, timezone

    s.supervisor_signed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(s)
    return s


@year_end_router.post(
    "/settlements/{settlement_id}/sign_accounting", response_model=SettlementOut
)
def sign_accounting(
    settlement_id: int,
    current_user: dict = Depends(require_permission(Permission.APPRAISAL_ACCOUNTING)),
    session: Session = Depends(get_session_dep),
):
    s = session.get(YearEndSettlement, settlement_id)
    if s is None:
        raise HTTPException(404)
    if s.status != YearEndSettlementStatus.SUPERVISOR_SIGNED:
        raise HTTPException(400, f"非主管已簽 (current={s.status.value})")
    s.status = YearEndSettlementStatus.ACCOUNTING_SIGNED
    s.accounting_signed_by = current_user.get("user_id")
    from datetime import datetime, timezone

    s.accounting_signed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(s)
    return s


@year_end_router.post(
    "/settlements/{settlement_id}/finalize", response_model=SettlementOut
)
def finalize_settlement(
    settlement_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_FINALIZE)),
    session: Session = Depends(get_session_dep),
):
    s = session.get(YearEndSettlement, settlement_id)
    if s is None:
        raise HTTPException(404)
    if s.status != YearEndSettlementStatus.ACCOUNTING_SIGNED:
        raise HTTPException(400, f"非會計已簽 (current={s.status.value})")
    s.status = YearEndSettlementStatus.FINALIZED
    s.finalized_by = current_user.get("user_id")
    from datetime import datetime, timezone

    s.finalized_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(s)
    return s


# ===== Special bonuses =====


@year_end_router.get(
    "/cycles/{cycle_id}/special_bonuses", response_model=list[SpecialBonusItemOut]
)
def list_special_bonuses(
    cycle_id: int,
    employee_id: int | None = Query(None),
    bonus_type: SpecialBonusType | None = Query(None),
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    q = session.query(SpecialBonusItem).filter_by(year_end_cycle_id=cycle_id)
    if employee_id is not None:
        q = q.filter_by(employee_id=employee_id)
    if bonus_type is not None:
        q = q.filter_by(bonus_type=bonus_type)
    return q.order_by(SpecialBonusItem.id).all()


@year_end_router.post(
    "/cycles/{cycle_id}/special_bonuses", response_model=SpecialBonusItemOut
)
def add_special_bonus(
    cycle_id: int,
    payload: SpecialBonusItemCreate,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_WRITE)),
    session: Session = Depends(get_session_dep),
):
    if not session.get(YearEndCycle, cycle_id):
        raise HTTPException(404, "cycle 不存在")
    item = SpecialBonusItem(
        year_end_cycle_id=cycle_id,
        **payload.model_dump(),
        created_by=current_user.get("user_id"),
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    # 重算對應 settlement.special_bonus_total
    _recompute_settlement_special_total(session, cycle_id, payload.employee_id)
    session.commit()
    return item


def _recompute_settlement_special_total(
    session: Session, cycle_id: int, employee_id: int
) -> None:
    total = (
        session.query(SpecialBonusItem)
        .filter_by(year_end_cycle_id=cycle_id, employee_id=employee_id)
        .with_entities(SpecialBonusItem.amount)
        .all()
    )
    total_sum = sum((row.amount for row in total), Decimal("0"))
    s = (
        session.query(YearEndSettlement)
        .filter_by(year_end_cycle_id=cycle_id, employee_id=employee_id)
        .first()
    )
    if s is not None:
        s.special_bonus_total = total_sum
        s.total_amount = s.payable_amount + total_sum


# ===== Excel I/O =====


def _build_employee_resolver(session: Session):
    employees = session.query(Employee).all()
    name_to_id = {e.name: e.id for e in employees}

    def resolver(name: str):
        return name_to_id.get(name)

    return resolver


def _build_classroom_resolver(session: Session):
    rooms = session.query(Classroom).all()
    name_to_id = {r.name: r.id for r in rooms}

    def resolver(name: str):
        return name_to_id.get(name)

    return resolver


@year_end_router.post("/cycles/import_excel", response_model=YearEndImportResultOut)
async def import_excel(
    file: UploadFile = File(...),
    start_date: date = Query(...),
    end_date: date = Query(...),
    bonus_calc_date: date = Query(...),
    org_achievement_rate_first: Decimal = Query(Decimal("83.6")),
    org_achievement_rate_second: Decimal = Query(Decimal("91.5")),
    enrollment_target: int = Query(160),
    current_user: dict = Depends(require_permission(Permission.YEAR_END_WRITE)),
):
    if not file.filename or not file.filename.lower().endswith(".xls"):
        raise HTTPException(400, "年終經營績效目前只支援 .xls (Excel 97-2003)")
    content = await file.read()
    with NamedTemporaryFile(suffix=".xls", delete=True) as tmp:
        tmp.write(content)
        tmp.flush()
        parsed = parse_year_end_excel(tmp.name)

    with session_scope() as session:
        emp_resolver = _build_employee_resolver(session)
        room_resolver = _build_classroom_resolver(session)
        result = import_year_end_to_db(
            parsed,
            session,
            employee_resolver=emp_resolver,
            classroom_resolver=room_resolver,
            cycle_dates=(start_date, end_date, bonus_calc_date),
            org_achievement_rate_first=org_achievement_rate_first,
            org_achievement_rate_second=org_achievement_rate_second,
            enrollment_target=enrollment_target,
        )
        return YearEndImportResultOut(
            cycle_id=result.cycle_id,
            settlements_upserted=result.settlements_upserted,
            special_bonuses_upserted=result.special_bonuses_upserted,
            class_targets_upserted=result.class_targets_upserted,
            skipped_unresolved_names=result.skipped_unresolved_names,
        )


@year_end_router.get("/cycles/{cycle_id}/summary.xlsx")
def export_summary(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    cycle = session.get(YearEndCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404)
    settlements = (
        session.query(YearEndSettlement).filter_by(year_end_cycle_id=cycle_id).all()
    )
    emp_idx = {e.id: e for e in session.query(Employee).all()}
    # 整合 special bonuses
    sb_by_emp: dict[int, dict[SpecialBonusType, Decimal]] = defaultdict(dict)
    for sb in (
        session.query(SpecialBonusItem).filter_by(year_end_cycle_id=cycle_id).all()
    ):
        # 同 type 多筆（如 FESTIVAL_DIFF 不同月）合併加總
        current = sb_by_emp[sb.employee_id].get(sb.bonus_type, Decimal("0"))
        sb_by_emp[sb.employee_id][sb.bonus_type] = current + sb.amount

    rows: list[SummaryExportRow] = []
    for s in settlements:
        emp = emp_idx.get(s.employee_id)
        name = emp.name if emp else f"emp#{s.employee_id}"
        rows.append(
            SummaryExportRow(
                name=name,
                year_end_amount=s.payable_amount,
                bonus_by_type=sb_by_emp.get(s.employee_id, {}),
                total=s.total_amount,
            )
        )
    payload = export_year_end_summary_xlsx(
        rows=rows, academic_year=cycle.academic_year
    )
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="year_end_summary_{cycle.academic_year}.xlsx"'
        },
    )


@year_end_router.get("/cycles/{cycle_id}/transfer_roster.xlsx")
def export_transfer(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    cycle = session.get(YearEndCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404)
    settlements = (
        session.query(YearEndSettlement)
        .filter(
            YearEndSettlement.year_end_cycle_id == cycle_id,
            YearEndSettlement.total_amount > 0,
        )
        .all()
    )
    emp_idx = {e.id: e for e in session.query(Employee).all()}
    rows: list[TransferRow] = []
    for s in settlements:
        e = emp_idx.get(s.employee_id)
        if e is None:
            continue
        rows.append(
            TransferRow(
                bank_account=e.bank_account or "",
                name=e.bank_account_name or e.name,
                amount=s.total_amount,
            )
        )
    payload = export_year_end_transfer_xlsx(
        rows=rows,
        title=f"{cycle.academic_year}年 年終獎金 轉帳名冊",
    )
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="year_end_transfer_{cycle.academic_year}.xlsx"'
        },
    )


# ===== PDF 列印 =====


def _aggregate_bonus_by_type(
    session: Session, cycle_id: int
) -> dict[int, dict[SpecialBonusType, Decimal]]:
    out: dict[int, dict[SpecialBonusType, Decimal]] = defaultdict(dict)
    for sb in (
        session.query(SpecialBonusItem).filter_by(year_end_cycle_id=cycle_id).all()
    ):
        current = out[sb.employee_id].get(sb.bonus_type, Decimal("0"))
        out[sb.employee_id][sb.bonus_type] = current + sb.amount
    return out


@year_end_router.get(
    "/cycles/{cycle_id}/settlements/{settlement_id}/slip.pdf",
    response_class=Response,
)
def export_personal_slip_pdf(
    cycle_id: int,
    settlement_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    """匯出個人年終獎金條 PDF（對應 Excel「New年終獎金條」）。"""
    settlement = session.get(YearEndSettlement, settlement_id)
    if settlement is None or settlement.year_end_cycle_id != cycle_id:
        raise HTTPException(404, "settlement 不存在")
    cycle = session.get(YearEndCycle, cycle_id)
    emp = session.get(Employee, settlement.employee_id)
    if emp is None:
        raise HTTPException(404, "員工不存在")
    bonuses = _aggregate_bonus_by_type(session, cycle_id).get(emp.id, {})

    from datetime import datetime

    pdf = generate_personal_bonus_slip_pdf(
        PersonalBonusSlipData(
            employee_name=emp.name,
            academic_year=cycle.academic_year,
            print_date=datetime.now().strftime("%Y.%m.%d"),
            year_end_amount=settlement.payable_amount,
            bonus_by_type=bonuses,
        )
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="bonus_slip_{emp.name}_{cycle.academic_year}.pdf"'
        },
    )


@year_end_router.get("/cycles/{cycle_id}/transfer_roster.pdf", response_class=Response)
def export_transfer_roster_pdf_endpoint(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    cycle = session.get(YearEndCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404)
    settlements = (
        session.query(YearEndSettlement)
        .filter(
            YearEndSettlement.year_end_cycle_id == cycle_id,
            YearEndSettlement.total_amount > 0,
        )
        .all()
    )
    emp_idx = {e.id: e for e in session.query(Employee).all()}
    entries = []
    for s in settlements:
        e = emp_idx.get(s.employee_id)
        if e is None:
            continue
        entries.append(
            TransferEntry(
                bank_account=e.bank_account or "",
                name=e.bank_account_name or e.name,
                amount=s.total_amount,
            )
        )
    pdf = generate_transfer_roster_pdf(
        entries=entries, academic_year=cycle.academic_year
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="year_end_transfer_{cycle.academic_year}.pdf"'
        },
    )


@year_end_router.get("/cycles/{cycle_id}/summary.pdf", response_class=Response)
def export_summary_pdf(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    cycle = session.get(YearEndCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404)
    settlements = (
        session.query(YearEndSettlement).filter_by(year_end_cycle_id=cycle_id).all()
    )
    emp_idx = {e.id: e for e in session.query(Employee).all()}
    bonus_idx = _aggregate_bonus_by_type(session, cycle_id)
    rows = []
    for s in settlements:
        emp = emp_idx.get(s.employee_id)
        name = emp.name if emp else f"emp#{s.employee_id}"
        rows.append(
            PdfSummaryRow(
                name=name,
                year_end_amount=s.payable_amount,
                bonus_by_type=bonus_idx.get(s.employee_id, {}),
                total=s.total_amount,
            )
        )
    pdf = generate_summary_table_pdf(rows=rows, academic_year=cycle.academic_year)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="year_end_summary_{cycle.academic_year}.pdf"'
        },
    )


__all__ = ["year_end_router"]

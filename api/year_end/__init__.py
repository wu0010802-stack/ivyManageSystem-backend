"""api/year_end — 年終獎金結算 API（M4 新增）。

提供 cycles / settlements / special_bonuses / org_settings / class_targets 與
Excel 雙向 I/O 端點。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date
from utils.taipei_time import now_taipei_naive
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
    BuildResultOut,
    BuildSettlementsRequest,
    ClassEnrollmentTargetOut,
    ClassEnrollmentTargetUpsert,
    GridRowOut,
    ManualPatchRequest,
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
from utils.approval_helpers import assert_not_self_approval
from utils.auth import require_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

year_end_router = APIRouter(prefix="/api/year_end", tags=["year_end"])

from api.year_end.appraisal_payout import (  # noqa: E402
    router as appraisal_payout_router,
)

year_end_router.include_router(appraisal_payout_router)


# ===== Cycles =====


@year_end_router.get("/cycles", response_model=list[YearEndCycleOut])
def list_cycles(
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    return session.query(YearEndCycle).order_by(YearEndCycle.academic_year.desc()).all()


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

    # B2: 若指定 clone_from_academic_year，先驗證來源 cycle 存在
    source_cycle = None
    if payload.clone_from_academic_year is not None:
        source_cycle = (
            session.query(YearEndCycle)
            .filter_by(academic_year=payload.clone_from_academic_year)
            .first()
        )
        if source_cycle is None:
            raise HTTPException(
                422, f"clone 來源學年 {payload.clone_from_academic_year} 不存在"
            )

    cycle = YearEndCycle(
        academic_year=payload.academic_year,
        start_date=payload.start_date,
        end_date=payload.end_date,
        bonus_calc_date=payload.bonus_calc_date,
        status=YearEndCycleStatus.OPEN,
        created_by=current_user.get("user_id"),
    )
    session.add(cycle)
    session.flush()  # 取得 cycle.id 才能建子rows

    if source_cycle is not None:
        # 複製 OrgYearSettings（兩學期）：保留設定欄位，重置實際值
        for src_org in (
            session.query(OrgYearSettings)
            .filter_by(year_end_cycle_id=source_cycle.id)
            .all()
        ):
            session.add(
                OrgYearSettings(
                    year_end_cycle_id=cycle.id,
                    semester_first=src_org.semester_first,
                    enrollment_target=src_org.enrollment_target,
                    enrollment_actual=None,  # 重置：實際值待新週期填入
                    school_achievement_rate=Decimal("0"),  # 重置
                    school_achievement_rate_override=None,  # 重置：HR 覆寫不沿用到新週期
                    org_achievement_rate=src_org.org_achievement_rate,
                    meeting_absence_deduction=src_org.meeting_absence_deduction,
                    festival_bonus_meta=dict(src_org.festival_bonus_meta or {}),
                )
            )
        # 複製 ClassEnrollmentTarget：保留手動欄位，重置計算欄位
        for src_ct in (
            session.query(ClassEnrollmentTarget)
            .filter_by(year_end_cycle_id=source_cycle.id)
            .all()
        ):
            session.add(
                ClassEnrollmentTarget(
                    year_end_cycle_id=cycle.id,
                    semester_first=src_ct.semester_first,
                    classroom_id=src_ct.classroom_id,
                    head_teacher_employee_id=src_ct.head_teacher_employee_id,
                    assistant_employee_id=src_ct.assistant_employee_id,
                    head_count_target=src_ct.head_count_target,
                    returning_student_rate=src_ct.returning_student_rate,
                    avg_monthly_enrollment=Decimal("0"),  # 重置：build_settlements 重算
                    class_performance_rate=Decimal("0"),  # 重置
                )
            )

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
        existing = OrgYearSettings(year_end_cycle_id=cycle_id, **payload.model_dump())
        session.add(existing)
    else:
        # 只寫 client 真正送出的欄位（exclude_unset），保留所有未送出的伺服器/設定欄位；
        # 顯式送 null 的 override 仍會清除。
        # 例：override-only POST 省略 enrollment_target → 既有 176 保留（不被預設 160 覆寫）；
        #      省略 school_achievement_rate → 伺服器自算值保留（不被預設 0 洗掉）。
        # 新建列走上面分支沿用 Pydantic 預設值（enrollment_target=160 等），由 refresh 後續回填。
        for k, v in payload.model_dump(exclude_unset=True).items():
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


@year_end_router.post(
    "/cycles/{cycle_id}/class_targets", response_model=ClassEnrollmentTargetOut
)
def upsert_class_target(
    cycle_id: int,
    payload: ClassEnrollmentTargetUpsert,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_WRITE)),
    session: Session = Depends(get_session_dep),
):
    """手動設定班級招生目標（upsert by cycle+semester+classroom）。
    avg_monthly_enrollment / class_performance_rate 由 build_settlements refresh，
    此端點僅存手動輸入欄位（head_count_target / returning_student_rate / 老師指派）。
    """
    if not session.get(YearEndCycle, cycle_id):
        raise HTTPException(404, "cycle 不存在")
    existing = (
        session.query(ClassEnrollmentTarget)
        .filter_by(
            year_end_cycle_id=cycle_id,
            semester_first=payload.semester_first,
            classroom_id=payload.classroom_id,
        )
        .first()
    )
    if existing is None:
        existing = ClassEnrollmentTarget(
            year_end_cycle_id=cycle_id,
            semester_first=payload.semester_first,
            classroom_id=payload.classroom_id,
            head_teacher_employee_id=payload.head_teacher_employee_id,
            assistant_employee_id=payload.assistant_employee_id,
            head_count_target=payload.head_count_target,
            returning_student_rate=payload.returning_student_rate,
        )
        session.add(existing)
    else:
        existing.head_teacher_employee_id = payload.head_teacher_employee_id
        existing.assistant_employee_id = payload.assistant_employee_id
        existing.head_count_target = payload.head_count_target
        existing.returning_student_rate = payload.returning_student_rate
    session.commit()
    session.refresh(existing)
    return existing


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
    # with_for_update：兩個 reviewer 同時簽核會讓 *signed_by 被後贏者覆蓋
    # 但 status 已改為下一階段，造成稽核軌跡與真實簽核人不符。
    # bug sweep 2026-05-16 P1-3。
    s = (
        session.query(YearEndSettlement)
        .filter(YearEndSettlement.id == settlement_id)
        .with_for_update()
        .first()
    )
    if s is None:
        raise HTTPException(404)
    if s.status != YearEndSettlementStatus.DRAFT:
        raise HTTPException(400, f"非 DRAFT (current={s.status.value})")
    assert_not_self_approval(current_user, s.employee_id, doc_label="年終獎金結算")
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
    # with_for_update：見 sign_supervisor 註解。bug sweep 2026-05-16 P1-3。
    s = (
        session.query(YearEndSettlement)
        .filter(YearEndSettlement.id == settlement_id)
        .with_for_update()
        .first()
    )
    if s is None:
        raise HTTPException(404)
    # 2-gate（會計從 DRAFT 直簽）與 3-gate（經主管）皆為設計支援的流程。
    if s.status not in (
        YearEndSettlementStatus.DRAFT,
        YearEndSettlementStatus.SUPERVISOR_SIGNED,
    ):
        raise HTTPException(400, f"非 DRAFT/主管已簽 (current={s.status.value})")
    assert_not_self_approval(current_user, s.employee_id, doc_label="年終獎金結算")
    # pentest E2：職責分離——若已有主管簽核（3-gate），會計簽核人須與其不同，
    # 防同一人連做兩關。2-gate 時 supervisor_signed_by 為 None，不受影響。
    if (
        s.supervisor_signed_by is not None
        and current_user.get("user_id") == s.supervisor_signed_by
    ):
        raise HTTPException(
            status_code=403, detail="會計簽核人需與主管簽核人為不同人（職責分離）"
        )
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
    # with_for_update：見 sign_supervisor 註解。bug sweep 2026-05-16 P1-3。
    s = (
        session.query(YearEndSettlement)
        .filter(YearEndSettlement.id == settlement_id)
        .with_for_update()
        .first()
    )
    if s is None:
        raise HTTPException(404)
    if s.status != YearEndSettlementStatus.ACCOUNTING_SIGNED:
        raise HTTPException(400, f"非會計已簽 (current={s.status.value})")
    assert_not_self_approval(current_user, s.employee_id, doc_label="年終獎金結算")
    # pentest E2：職責分離——核定人須與會計簽核人為不同人（防同一人連做兩關）。
    if current_user.get("user_id") == s.accounting_signed_by:
        raise HTTPException(
            status_code=403, detail="核定人需與會計簽核人為不同人（職責分離）"
        )
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
    # bug sweep 2026-05-16 P0-3 + P1（2026-06-16）：
    # (a) 反向 race：若 settlement 還未建，_recompute 會 silently no-op，
    #     special_bonus 寫入但 total_amount 不會反映，造成漏算。要求 settlement 先在。
    # (b) 非 DRAFT 不可改：已簽核（SUPERVISOR/ACCOUNTING_SIGNED）或已核定（FINALIZED）
    #     後再加 special_bonus 會改 total_amount，等於簽章還在卻事後改轉帳金額。
    #     與 build_settlements / manual_patch / import_excel 的「非 DRAFT 即凍結」對齊。
    # 取 settlement 時加 with_for_update 防 race（與 sign 端點對齊）。
    settlement = (
        session.query(YearEndSettlement)
        .filter_by(year_end_cycle_id=cycle_id, employee_id=payload.employee_id)
        .with_for_update()
        .first()
    )
    if settlement is None:
        raise HTTPException(
            status_code=400,
            detail="該員工尚未建立年終結算單，請先建立 settlement 再加 special_bonus",
        )
    if settlement.status != YearEndSettlementStatus.DRAFT:
        raise HTTPException(
            status_code=400,
            detail=(
                f"年終結算狀態為 {settlement.status.value}（非 DRAFT），"
                "不允許新增 special_bonus（會改變 total_amount）；已簽核請先退回 DRAFT"
            ),
        )
    # #8（2026-06-16）：對重複 (cycle, emp, bonus_type, period_label) 改為 upsert。
    # 原本盲目 INSERT 會撞 uq_special_bonus_item → IntegrityError 500 並中止交易。
    # 比照 _recompute / Excel 匯入路徑：存在則更新欄位，否則新增。
    fields = payload.model_dump()
    existing = (
        session.query(SpecialBonusItem)
        .filter_by(
            year_end_cycle_id=cycle_id,
            employee_id=payload.employee_id,
            bonus_type=payload.bonus_type,
            period_label=payload.period_label,
        )
        .first()
    )
    if existing is None:
        item = SpecialBonusItem(
            year_end_cycle_id=cycle_id,
            **fields,
            created_by=current_user.get("user_id"),
        )
        session.add(item)
    else:
        # 更新可變欄位（金額/班級/calc_meta/source_ref）；不動 created_by。
        item = existing
        item.amount = payload.amount
        item.classroom_id = payload.classroom_id
        item.calc_meta = payload.calc_meta
        item.source_ref = payload.source_ref
    session.flush()
    # 重算對應 settlement.special_bonus_total
    _recompute_settlement_special_total(session, cycle_id, payload.employee_id)
    session.commit()
    session.refresh(item)
    return item


def _recompute_settlement_special_total(
    session: Session, cycle_id: int, employee_id: int
) -> None:
    """重算指定 (cycle, employee) settlement 的 special_bonus_total / total_amount。

    若 settlement 不存在則 no-op（settlement 建立時會主動回算既有 special_bonus）；
    若 settlement 非 DRAFT（已簽核 / 已核定）則拋 HTTPException，避免簽章還在卻
    事後改動轉帳金額（caller add_special_bonus 已守住，這裡為 defense-in-depth：
    import_excel 等批次 path 也走此口徑）。
    """
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
        .with_for_update()
        .first()
    )
    if s is None:
        return
    if s.status != YearEndSettlementStatus.DRAFT:
        raise HTTPException(
            400,
            f"settlement 狀態為 {s.status.value}（非 DRAFT），"
            "不可重算特別獎金（已簽核請先退回 DRAFT；已核定需重新開啟年終週期）",
        )
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
    # P0a 修 bypass：原本裸 file.read() 缺 size check + magic_bytes 驗證
    from utils.file_upload import read_upload_with_size_check

    content = await read_upload_with_size_check(file, extension=".xls")
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


def _build_summary_rows(session: Session, cycle_id: int) -> list[SummaryExportRow]:
    """組裝年終總表列。

    排除負值結算（時薪/扣款超基數產生的負 payable）：這些不會出現在轉帳名冊
    （export_transfer 過濾 total_amount>0），列入總表會曝出負年終誤導簽核者、
    並使總表合計與名冊合計差「負值總和」。保留 0 列（資訊性）。
    （2026-06-15 運作探測 P3-2；業主裁示不改 engine、僅修總表呈現。）
    """
    settlements = (
        session.query(YearEndSettlement)
        .filter(
            YearEndSettlement.year_end_cycle_id == cycle_id,
            YearEndSettlement.total_amount >= 0,
        )
        .all()
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
    return rows


@year_end_router.get("/cycles/{cycle_id}/summary.xlsx")
def export_summary(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    cycle = session.get(YearEndCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404)
    rows = _build_summary_rows(session, cycle_id)
    payload = export_year_end_summary_xlsx(rows=rows, academic_year=cycle.academic_year)
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
            print_date=now_taipei_naive().strftime("%Y.%m.%d"),
            year_end_amount=settlement.payable_amount,
            bonus_by_type=bonuses,
        )
    )
    from urllib.parse import quote

    # 檔名含中文(emp.name)→ 用 RFC 5987 filename* 避免 latin-1 編碼 500
    _fn = quote(f"bonus_slip_{emp.name}_{cycle.academic_year}.pdf")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{_fn}"},
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


# ===== Task 6: build-settlements / grid / manual-patch =====


@year_end_router.post(
    "/cycles/{cycle_id}/build-settlements", response_model=BuildResultOut
)
def build_settlements_endpoint(
    cycle_id: int,
    payload: BuildSettlementsRequest,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_WRITE)),
    session: Session = Depends(get_session_dep),
):
    """跨員工計算並 upsert 年終結算單（idempotent）。非 DRAFT（已簽核）的結算不覆寫。"""
    from services.year_end.settlement_builder import build_settlements

    cycle = session.get(YearEndCycle, cycle_id)
    if cycle is None:
        raise HTTPException(404, "cycle 不存在")

    actor_id = current_user.get("user_id")
    result = build_settlements(
        session,
        cycle.academic_year,
        set(payload.included_resigned_employee_ids),
        actor_id=actor_id,
        refresh_rates=True,
    )
    session.commit()
    dr = (
        result.derive_report
    )  # None when refresh_rates=False（本端點固定 True，但 None-safe）
    return BuildResultOut(
        built=result.built,
        skipped_finalized=result.skipped_finalized,
        unmatched_count=dr.unmatched_count if dr is not None else 0,
        fallback_classes=dr.fallback_classes if dr is not None else 0,
        warnings=dr.warnings if dr is not None else [],
    )


@year_end_router.get("/cycles/{cycle_id}/grid", response_model=list[GridRowOut])
def grid_endpoint(
    cycle_id: int,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_READ)),
    session: Session = Depends(get_session_dep),
):
    """回傳每位員工一列的年終結算 grid（含 special_bonuses 依 bonus_type 加總）。"""
    if not session.get(YearEndCycle, cycle_id):
        raise HTTPException(404, "cycle 不存在")

    settlements = (
        session.query(YearEndSettlement)
        .filter_by(year_end_cycle_id=cycle_id)
        .order_by(YearEndSettlement.employee_id)
        .all()
    )

    emp_idx = {e.id: e for e in session.query(Employee).all()}

    # 彙整 special_bonuses：{employee_id -> {bonus_type.value -> total}}
    sb_map: dict[int, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for sb in (
        session.query(SpecialBonusItem).filter_by(year_end_cycle_id=cycle_id).all()
    ):
        sb_map[sb.employee_id][sb.bonus_type.value] += sb.amount

    rows = []
    for s in settlements:
        emp = emp_idx.get(s.employee_id)
        name = emp.name if emp else f"emp#{s.employee_id}"
        rows.append(
            GridRowOut(
                settlement_id=s.id,
                employee_id=s.employee_id,
                employee_name=name,
                payable_amount=s.payable_amount,
                special_bonuses=dict(sb_map.get(s.employee_id, {})),
                total_amount=s.total_amount,
                status=s.status.value,
            )
        )
    return rows


@year_end_router.patch(
    "/settlements/{settlement_id}/manual", response_model=SettlementOut
)
def manual_patch_settlement(
    settlement_id: int,
    payload: ManualPatchRequest,
    current_user: dict = Depends(require_permission(Permission.YEAR_END_WRITE)),
    session: Session = Depends(get_session_dep),
):
    """手動微調結算：獎懲扣項、超額獎金、在職月數覆寫。自動重算受影響的結算單。"""
    from services.year_end.settlement_builder import build_settlements

    settlement = session.get(YearEndSettlement, settlement_id)
    if settlement is None:
        raise HTTPException(404, "settlement 不存在")
    if settlement.status != YearEndSettlementStatus.DRAFT:
        raise HTTPException(409, "僅 DRAFT 狀態可手動調整；已簽核請先退回")

    cycle = session.get(YearEndCycle, settlement.year_end_cycle_id)
    if cycle is None:
        raise HTTPException(404, "cycle 不存在")

    # 寫入手動欄位
    if payload.deduction_disciplinary is not None:
        settlement.deduction_disciplinary = payload.deduction_disciplinary

    if payload.excess_amount is not None:
        # upsert SpecialBonusItem(EXCESS_ENROLLMENT)
        # C5：超額獎金每位員工每年僅一筆。以 (cycle, emp, bonus_type) 去重（忽略
        # period_label），避免與 Excel 匯入用的 period_label（"114上"）不同鍵而並存
        # 兩筆，被 build_settlements 重複加總；新建時採與匯入一致的學期標籤。
        existing_excess = (
            session.query(SpecialBonusItem)
            .filter_by(
                year_end_cycle_id=cycle.id,
                employee_id=settlement.employee_id,
                bonus_type=SpecialBonusType.EXCESS_ENROLLMENT,
            )
            .first()
        )
        if existing_excess is None:
            session.add(
                SpecialBonusItem(
                    year_end_cycle_id=cycle.id,
                    employee_id=settlement.employee_id,
                    bonus_type=SpecialBonusType.EXCESS_ENROLLMENT,
                    period_label=f"{cycle.academic_year}上",
                    amount=payload.excess_amount,
                    created_by=current_user.get("user_id"),
                )
            )
        else:
            existing_excess.amount = payload.excess_amount
    session.flush()

    if payload.hire_months_override is not None:
        # merge into calc_meta without clobbering other keys
        meta = dict(settlement.calc_meta or {})
        meta["hire_months_override"] = str(payload.hire_months_override)
        settlement.calc_meta = meta
        session.flush()

    # 重算（idempotent）：若員工已非 active，需納入 included_resigned_ids
    emp = session.get(Employee, settlement.employee_id)
    if emp is None:
        raise HTTPException(409, "員工資料不存在，無法重算結算")
    is_inactive = not getattr(emp, "is_active", True)
    included = {settlement.employee_id} if is_inactive else set()
    actor_id = current_user.get("user_id")
    # 只重算這位被 patch 的員工：避免單筆手調觸發整個 cycle 全員重算與版本 churn
    build_settlements(
        session,
        cycle.academic_year,
        included,
        actor_id=actor_id,
        refresh_rates=False,
        only_employee_ids={settlement.employee_id},
    )
    session.commit()

    # 重新讀回（build_settlements 已 flush+commit）
    session.expire(settlement)
    return session.get(YearEndSettlement, settlement_id)


__all__ = ["year_end_router"]

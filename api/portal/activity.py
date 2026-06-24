"""
Portal - 才藝報名查詢（教師查看自班學生報名）及才藝點名（任何教師可點任意場次完整跨班名冊）
"""

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from models.database import get_session, Classroom
from models.activity import (
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    ActivityAttendance,
    RegistrationCourse,
)
from utils.academic import resolve_current_academic_term
from utils.taipei_time import today_taipei
from utils.audit import write_explicit_audit
from utils.auth import get_current_user
from ._shared import _get_employee
from api.activity._shared import (
    _build_session_detail_response,
    build_session_rows_with_stats,
    query_valid_session_registrations,
    resolve_student_pii_scope,
)
from schemas.activity_admin import (
    ActivitySessionListItemOut,
    ActivitySessionDetailOut,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# portal 場次列表無分頁；兩個日期都沒帶時的預設近窗天數，bounds 全歷史掃描
# （一年涵蓋兩學期場次，足夠教師 portal 點名導覽；更早場次顯式帶 start_date）。
_PORTAL_SESSIONS_DEFAULT_WINDOW_DAYS = 365


# --- Response schema（補契約：原回裸 dict → OpenAPI 無具名 schema → 前端 codegen unknown）。
# sessions list/detail 走共用 helper（build_session_rows_with_stats /
# _build_session_detail_response），輸出形狀與 admin 端一致，故直接重用 admin 的
# ActivitySessionListItemOut / ActivitySessionDetailOut（見下方 response_model）---
class PortalRegistrationCourseOut(BaseModel):
    course_name: str
    status: str
    waitlist_position: Optional[int] = None


class PortalRegistrationItemOut(BaseModel):
    id: int
    student_name: Optional[str] = None
    class_name: Optional[str] = None
    is_paid: bool
    courses: List[PortalRegistrationCourseOut]
    created_at: Optional[str] = None


class PortalRegistrationsSummaryOut(BaseModel):
    total_registrations: int
    total_enrolled: int
    total_waitlist: int
    total_paid: int


class PortalRegistrationsOut(BaseModel):
    classrooms: List[str]
    registrations: List[PortalRegistrationItemOut]
    # 無班級資料時的早返回不帶 summary（{classrooms:[],registrations:[]}），故 Optional。
    summary: Optional[PortalRegistrationsSummaryOut] = None


class PortalBatchAttendanceResultOut(BaseModel):
    ok: bool
    updated: int
    skipped: int


@router.get("/activity/registrations", response_model=PortalRegistrationsOut)
def get_portal_activity_registrations(
    current_user: dict = Depends(get_current_user),
):
    """取得當前教師管理班級的學生才藝報名列表"""
    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        # 找出教師管理的班級（主教、副教、藝術教師皆可查）
        classrooms = (
            session.query(Classroom)
            .filter(
                Classroom.is_active.is_(True),
                or_(
                    Classroom.head_teacher_id == emp.id,
                    Classroom.assistant_teacher_id == emp.id,
                    Classroom.art_teacher_id == emp.id,
                ),
            )
            .all()
        )

        if not classrooms:
            return {"classrooms": [], "registrations": []}

        class_names = [c.name for c in classrooms]
        classroom_ids = [c.id for c in classrooms]

        # 查詢班級內學生的報名資料（以 classroom_id FK 比對，避免字串比對在轉班後失準）。
        # 限當前學期：學期輪替不會讓舊報名失效（is_active 永久為 True），未加學期
        # 條件會把歷史學期 active 報名混入當前報名/候補/繳費統計（對齊 admin 端
        # resolve_academic_term_filters 預設取當前學期的口徑）。
        sy, sem = resolve_current_academic_term()
        regs = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.classroom_id.in_(classroom_ids),
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.match_status != "rejected",
                ActivityRegistration.school_year == sy,
                ActivityRegistration.semester == sem,
            )
            .order_by(
                ActivityRegistration.class_name, ActivityRegistration.student_name
            )
            .all()
        )

        reg_ids = [r.id for r in regs]

        # 批次查詢課程關聯
        course_map: dict[int, list] = defaultdict(list)
        if reg_ids:
            rc_rows = (
                session.query(
                    RegistrationCourse.registration_id,
                    RegistrationCourse.id.label("rc_id"),
                    RegistrationCourse.status,
                    RegistrationCourse.course_id,
                    ActivityCourse.name.label("course_name"),
                )
                .join(ActivityCourse, RegistrationCourse.course_id == ActivityCourse.id)
                .filter(RegistrationCourse.registration_id.in_(reg_ids))
                .order_by(RegistrationCourse.registration_id, RegistrationCourse.id)
                .all()
            )

            # 計算候補排位：必須以「全校同課程」真實順位計算，不能只在自班 rc_rows
            # 內 enumerate（那會把跨班候補生排除，自班順位塌縮成 1,2,3…，與 admin /
            # 家長端用的全校 window function 口徑不一致）。course_id 本身綁定學期，
            # 故 partition_by(course_id) 天然學期隔離。對齊 _shared 全校候補順位算法。
            waitlist_course_ids = {
                row.course_id for row in rc_rows if row.status == "waitlist"
            }
            waitlist_position_map: dict[int, int] = {}  # rc_id → 全校順位
            if waitlist_course_ids:
                pos_subq = (
                    session.query(
                        RegistrationCourse.id.label("rc_id"),
                        func.row_number()
                        .over(
                            partition_by=RegistrationCourse.course_id,
                            order_by=RegistrationCourse.id,
                        )
                        .label("position"),
                    )
                    .join(
                        ActivityRegistration,
                        RegistrationCourse.registration_id == ActivityRegistration.id,
                    )
                    .filter(
                        RegistrationCourse.course_id.in_(list(waitlist_course_ids)),
                        RegistrationCourse.status == "waitlist",
                        ActivityRegistration.is_active.is_(True),
                    )
                    .subquery()
                )
                for row in session.query(pos_subq).all():
                    waitlist_position_map[row.rc_id] = row.position

            for row in rc_rows:
                entry = {
                    "course_name": row.course_name,
                    "status": row.status,
                    "waitlist_position": (
                        waitlist_position_map.get(row.rc_id)
                        if row.status == "waitlist"
                        else None
                    ),
                }
                course_map[row.registration_id].append(entry)

        result = []
        for r in regs:
            result.append(
                {
                    "id": r.id,
                    "student_name": r.student_name,
                    "class_name": r.class_name,
                    "is_paid": r.is_paid,
                    "courses": course_map.get(r.id, []),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )

        # 摘要統計
        total_enrolled = sum(
            1 for r in result for c in r["courses"] if c["status"] == "enrolled"
        )
        total_waitlist = sum(
            1 for r in result for c in r["courses"] if c["status"] == "waitlist"
        )
        total_paid = sum(1 for r in result if r["is_paid"])

        return {
            "classrooms": class_names,
            "registrations": result,
            "summary": {
                "total_registrations": len(result),
                "total_enrolled": total_enrolled,
                "total_waitlist": total_waitlist,
                "total_paid": total_paid,
            },
        }
    finally:
        session.close()


# ── 才藝點名（Portal） ─────────────────────────────────────────────────────────


class PortalAttendanceRecordItem(BaseModel):
    registration_id: int
    is_present: bool
    notes: Optional[str] = ""


class PortalBatchAttendanceUpdate(BaseModel):
    # max_length=500 與 admin 端 BatchAttendanceUpdate 對齊；防止單次請求送入
    # 過量 records 觸發 DoS 級記憶體/查詢壓力
    records: List[PortalAttendanceRecordItem] = Field(..., min_length=1, max_length=500)


@router.get(
    "/activity/attendance/sessions",
    response_model=list[ActivitySessionListItemOut],
)
def portal_list_sessions(
    course_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    current_user: dict = Depends(get_current_user),
):
    """才藝場次列表：列全部才藝場次（任何老師可見），出席統計算整堂。

    放寬前僅列『自班有報名的課程』場次且統計只算自班；現對齊 admin：
    列全部場次、整堂統計。維持回傳陣列（與既有前端相容，無分頁）。

    防無窗查詢：start_date / end_date 皆未帶時（前端清空日期）預設只回最近
    _PORTAL_SESSIONS_DEFAULT_WINDOW_DAYS 天的場次，避免一次撈全部歷史場次並對
    全 session_id 跑出席聚合（employee-gated 非 DoS，但屬不設防的契約退化）。
    要看更早場次顯式帶 start_date 即可。
    """
    session = get_session()
    try:
        # Finding 4：router 層 require_non_parent_role 只擋家長；此端點回全校場次清單
        # 與整堂出席統計，須額外要求 employee 身分（對齊同檔 detail/write 守衛與
        # require_non_parent_role docstring），擋掉無員工關聯的服務／管理帳號。
        _get_employee(session, current_user)
        # 無窗保護：兩個日期都沒帶時套預設近窗，bounds 全歷史掃描（見 docstring）。
        if start_date is None and end_date is None:
            start_date = today_taipei() - timedelta(
                days=_PORTAL_SESSIONS_DEFAULT_WINDOW_DAYS
            )
        query = session.query(
            ActivitySession.id,
            ActivitySession.course_id,
            ActivitySession.session_date,
            ActivitySession.notes,
            ActivitySession.created_by,
            ActivitySession.created_at,
            ActivityCourse.name.label("course_name"),
        ).join(ActivityCourse, ActivitySession.course_id == ActivityCourse.id)
        if course_id:
            query = query.filter(ActivitySession.course_id == course_id)
        if start_date:
            query = query.filter(ActivitySession.session_date >= start_date)
        if end_date:
            query = query.filter(ActivitySession.session_date <= end_date)
        rows = query.order_by(
            ActivitySession.session_date.desc(), ActivitySession.id.desc()
        ).all()

        return build_session_rows_with_stats(session, rows)
    finally:
        session.close()


@router.get(
    "/activity/attendance/sessions/{session_id}",
    response_model=ActivitySessionDetailOut,
)
def portal_get_session_detail(
    session_id: int,
    group_by: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """場次詳情：完整跨班名冊（任何老師可查）。

    放寬前僅回自班學生並以 403 collapse 防列舉；現任何老師皆可查任何場次，
    無受保護資源可列舉，故場次不存在直接回 404（對齊 admin）。
    group_by="classroom" → 額外回傳 groups（按班級分組）。
    """
    session = get_session()
    try:
        # Finding 2：router 層 require_non_parent_role 只擋家長；此端點回完整跨班
        # 名冊，須額外要求 employee 身分（對齊同檔 get_portal_activity_registrations
        # 與 require_non_parent_role docstring），擋掉無員工關聯的服務／管理帳號。
        _get_employee(session, current_user)
        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        if not sess:
            raise HTTPException(status_code=404, detail="找不到場次")
        group_key = "classroom" if group_by == "classroom" else None
        pii_visible, pii_allowed = resolve_student_pii_scope(session, current_user)
        return _build_session_detail_response(
            session,
            sess,
            group_by=group_key,
            mask_student_ids=not pii_visible,
            student_pii_visible_classroom_ids=pii_allowed,
        )
    finally:
        session.close()


@router.put(
    "/activity/attendance/sessions/{session_id}/records",
    response_model=PortalBatchAttendanceResultOut,
)
def portal_batch_update_attendance(
    session_id: int,
    body: PortalBatchAttendanceUpdate,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """批次點名：任何老師可點整堂跨班名冊；無效報名略過（對齊 admin）。

    放寬前限定自班並對非自班 reg 整批 403；現移除自班限制，僅保留
    『該 reg 確實有效報了本場次課程』的有效性檢查（無效者略過、不整批拒絕）。
    """
    session = get_session()
    try:
        # Finding 2：寫出席會影響退費比例（T_served），須要求 employee 身分；
        # router 層 require_non_parent_role 只擋家長不足以授權寫入。
        _get_employee(session, current_user)
        sess = (
            session.query(ActivitySession)
            .filter(ActivitySession.id == session_id)
            .first()
        )
        if not sess:
            raise HTTPException(status_code=404, detail="找不到場次")

        operator = current_user.get("username")
        # P2-6：同 admin 端，去重保留最後一筆避免重複 registration_id 撞 unique 約束 500。
        records = list({item.registration_id: item for item in body.records}.values())
        req_reg_ids = [item.registration_id for item in records]

        existing_map = {
            a.registration_id: a
            for a in session.query(ActivityAttendance)
            .filter(
                ActivityAttendance.session_id == session_id,
                ActivityAttendance.registration_id.in_(req_reg_ids),
            )
            .all()
        }

        valid_reg_rows = query_valid_session_registrations(
            session, sess.course_id, req_reg_ids
        )
        valid_reg_ids = {row[0] for row in valid_reg_rows}
        reg_student_map = dict(valid_reg_rows)

        skipped = [rid for rid in req_reg_ids if rid not in valid_reg_ids]
        if skipped:
            logger.warning(
                "portal_batch_update_attendance skipped invalid registrations: "
                "session=%s ids=%s",
                session_id,
                skipped,
            )

        for item in records:
            if item.registration_id not in valid_reg_ids:
                continue
            existing = existing_map.get(item.registration_id)
            if existing:
                existing.is_present = item.is_present
                existing.notes = item.notes or ""
                existing.recorded_by = operator
                if existing.student_id is None:
                    existing.student_id = reg_student_map.get(item.registration_id)
            else:
                # savepoint：併發請求已先 commit 同一 (session_id, registration_id)
                # 時，撞 uq_activity_attendance_session_reg → savepoint 自動回滾，
                # 不毀外層整批。改為重查後更新（對齊 admin 端 begin_nested 寫法）。
                try:
                    with session.begin_nested():
                        att = ActivityAttendance(
                            session_id=session_id,
                            registration_id=item.registration_id,
                            student_id=reg_student_map.get(item.registration_id),
                            is_present=item.is_present,
                            notes=item.notes or "",
                            recorded_by=operator,
                        )
                        session.add(att)
                        session.flush()
                except IntegrityError:
                    # 另一請求已併發插入同 (session_id, registration_id)；改為更新該列
                    existing = (
                        session.query(ActivityAttendance)
                        .filter_by(
                            session_id=session_id,
                            registration_id=item.registration_id,
                        )
                        .one()
                    )
                    existing.is_present = item.is_present
                    existing.notes = item.notes or ""
                    existing.recorded_by = operator
                    if existing.student_id is None:
                        existing.student_id = reg_student_map.get(item.registration_id)

        session.commit()
        applied = sum(1 for item in records if item.registration_id in valid_reg_ids)

        # AuditMiddleware 不涵蓋 /api/portal/activity/*（ENTITY_PATTERNS 無對應
        # pattern → _parse_entity_type 回 None 短路），故顯式留稽核：教師端點名
        # 同樣改變出席狀態，直接影響退費比例（T_served）與出席統計，須可追溯。
        course_name = (
            session.query(ActivityCourse.name)
            .filter(ActivityCourse.id == sess.course_id)
            .scalar()
        )
        write_explicit_audit(
            request,
            action="UPDATE",
            entity_type="activity_session",
            entity_id=str(session_id),
            summary=(
                f"教師批次點名：「{course_name}」{sess.session_date.isoformat()} "
                f"更新 {applied} 筆（跳過 {len(skipped)}）"
            ),
            changes={
                "course_id": sess.course_id,
                "course_name": course_name,
                "session_date": sess.session_date.isoformat(),
                "updated_count": applied,
                "skipped_count": len(skipped),
                "operator": operator,
                "source": "portal",
                "records": [
                    {
                        "registration_id": item.registration_id,
                        "is_present": item.is_present,
                    }
                    for item in records
                    if item.registration_id in valid_reg_ids
                ],
            },
        )
        return {"ok": True, "updated": applied, "skipped": len(skipped)}
    finally:
        session.close()

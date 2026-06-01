"""Growth report admin API.

Endpoints:
- POST /api/students/{student_id}/growth-reports           觸發生成（bounded PDF worker pool）
- GET  /api/students/{student_id}/growth-reports           列出
- GET  /api/students/{student_id}/growth-reports/{rid}     單筆狀態查詢
- GET  /api/students/{student_id}/growth-reports/{rid}/download  下載 PDF
- DELETE /api/students/{student_id}/growth-reports/{rid}   刪除（含檔案）
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from utils.taipei_time import now_taipei_naive
from utils.taipei_time import today_taipei
from pathlib import Path
from typing import Optional

from config import settings

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from models.database import (
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
    Student,
    StudentAssessment,
    StudentAttendance,
    StudentGrowthReport,
    StudentMeasurement,
    StudentMilestone,
    StudentObservation,
    session_scope,
)
from models.portfolio import (
    REPORT_STATUS_FAILED,
    REPORT_STATUS_GENERATING,
    REPORT_STATUS_PENDING,
    REPORT_STATUS_READY,
)
from services.growth_report_collector import (
    measurements_to_series,
    pick_highlight_observations,
    summarize_attendance,
)
from services import pdf_worker
from services.growth_report_pdf import generate_growth_report_pdf
from utils.audit import write_explicit_audit
from utils.auth import require_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access
from utils.storage import get_backend

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/students", tags=["portfolio-growth-reports"])

REPORT_ROOT = settings.storage.growth_report_root
MAX_PDF_SIZE_BYTES = settings.storage.growth_report_max_bytes

# Module-level LINE service (injected via init_growth_reports_line_service)
_line_service = None


def init_growth_reports_line_service(svc) -> None:
    """由 main.py 注入 LINE service 單例。"""
    global _line_service
    _line_service = svc


class GenerateReportPayload(BaseModel):
    period_label: str = Field(..., min_length=1, max_length=40)
    period_start: date
    period_end: date
    teacher_narrative: Optional[str] = Field(default=None, max_length=5000)


class SendLinePayload(BaseModel):
    message: Optional[str] = Field(default=None, max_length=500)


def _row_to_dict(r: StudentGrowthReport) -> dict:
    return {
        "id": r.id,
        "student_id": r.student_id,
        "period_label": r.period_label,
        "period_start": r.period_start.isoformat() if r.period_start else None,
        "period_end": r.period_end.isoformat() if r.period_end else None,
        "status": r.status,
        "file_size": r.file_size,
        "error_message": r.error_message,
        "generated_by": r.generated_by,
        "generated_at": r.generated_at.isoformat() if r.generated_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "line_sent_at": r.line_sent_at.isoformat() if r.line_sent_at else None,
        "parent_first_viewed_at": (
            r.parent_first_viewed_at.isoformat() if r.parent_first_viewed_at else None
        ),
        "parent_view_count": r.parent_view_count,
        "teacher_narrative": r.teacher_narrative,
    }


def _build_activities_payload(activity_rows, session) -> list[dict]:
    """Join ActivityCourse.name for each ActivityRegistration via RegistrationCourse.

    P4 cleanup T2: replaces the simplified f"報名 #{r.id}" fallback with the
    actual course name looked up from activity_courses.
    """
    reg_ids = [r.id for r in activity_rows]
    if reg_ids:
        pairs = (
            session.query(
                RegistrationCourse.registration_id, RegistrationCourse.course_id
            )
            .filter(RegistrationCourse.registration_id.in_(reg_ids))
            .all()
        )
    else:
        pairs = []

    all_course_ids = list({cid for _, cid in pairs if cid})
    if all_course_ids:
        course_rows = (
            session.query(ActivityCourse.id, ActivityCourse.name)
            .filter(ActivityCourse.id.in_(all_course_ids))
            .all()
        )
    else:
        course_rows = []
    course_name_lookup = dict(course_rows)

    # Map each registration to its first course_id (first row wins)
    reg_first_course: dict[int, int] = {}
    for reg_id, course_id in pairs:
        reg_first_course.setdefault(reg_id, course_id)

    out = []
    for r in activity_rows:
        cid = reg_first_course.get(r.id)
        name = course_name_lookup.get(cid) if cid else None
        if not name:
            name = f"報名 #{r.id}"
        out.append(
            {
                "name": name,
                "registered_at": (
                    r.created_at.date().isoformat() if r.created_at else ""
                ),
            }
        )
    return out


def _collect_report_data(
    session,
    student: Student,
    period_start: date,
    period_end: date,
    report: StudentGrowthReport,
) -> dict:
    """聚合 period 內所有資料，組成 PDF 生成器接受的 dict."""
    from models.database import Classroom

    classroom_obj = (
        session.query(Classroom).filter_by(id=student.classroom_id).first()
        if student.classroom_id
        else None
    )
    classroom_name = classroom_obj.name if classroom_obj else None

    att_rows = (
        session.query(StudentAttendance)
        .filter(
            StudentAttendance.student_id == student.id,
            StudentAttendance.date >= period_start,
            StudentAttendance.date <= period_end,
        )
        .all()
    )
    att_records = [{"date": a.date, "status": a.status} for a in att_rows]

    obs_rows = (
        session.query(StudentObservation)
        .filter(
            StudentObservation.student_id == student.id,
            StudentObservation.deleted_at.is_(None),
            StudentObservation.observation_date >= period_start,
            StudentObservation.observation_date <= period_end,
        )
        .all()
    )

    ms_rows = (
        session.query(StudentMilestone)
        .filter(
            StudentMilestone.student_id == student.id,
            StudentMilestone.deleted_at.is_(None),
            StudentMilestone.achieved_on >= period_start,
            StudentMilestone.achieved_on <= period_end,
        )
        .order_by(StudentMilestone.achieved_on.asc())
        .all()
    )

    measurement_rows = (
        session.query(StudentMeasurement)
        .filter(
            StudentMeasurement.student_id == student.id,
            StudentMeasurement.measured_on >= period_start,
            StudentMeasurement.measured_on <= period_end,
        )
        .order_by(StudentMeasurement.measured_on.asc())
        .all()
    )

    assessment_rows = (
        session.query(StudentAssessment)
        .filter(
            StudentAssessment.student_id == student.id,
            StudentAssessment.assessment_date >= period_start,
            StudentAssessment.assessment_date <= period_end,
        )
        .all()
    )

    activity_rows = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.student_id == student.id,
            ActivityRegistration.created_at >= period_start,
            # round 5 P1：created_at 是 DateTime，period_end 是 Date；
            # PG cast 後 <= period_end 等於 <= period_end 00:00:00，
            # 會吃掉期間結束當天的活動。改半開區間 +1 day 與
            # student_attachments.py:162 同 idiom。
            ActivityRegistration.created_at < (period_end + timedelta(days=1)),
        )
        .all()
    )

    return {
        "student": {
            "name": student.name,
            "student_no": student.student_id,
            "classroom_name": classroom_name,
            "birthday": student.birthday,
        },
        "report": {
            "period_label": report.period_label,
            "period_start": report.period_start,
            "period_end": report.period_end,
            "report_id": report.id,
            "teacher_narrative": report.teacher_narrative,
            "generated_on": today_taipei(),  
        },
        "attendance_summary": summarize_attendance(att_records),
        "highlight_observations": pick_highlight_observations(obs_rows, max_count=5),
        "milestones": [
            {
                "title": m.title,
                "achieved_on": m.achieved_on.isoformat() if m.achieved_on else "",
                "icon": m.icon or "",
            }
            for m in ms_rows
        ],
        "measurement_series": measurements_to_series(measurement_rows),
        "assessments": [
            {
                "domain": a.domain,
                "rating": a.rating,
                "comment": a.content or "",
            }
            for a in assessment_rows
        ],
        "activities": _build_activities_payload(activity_rows, session),
        "institution_name": "義華幼兒園",
    }


def _generate_pdf_job(report_id: int) -> None:
    """Background job：撈資料 → 生 PDF → 寫檔 → 更新 status.

    透過 pdf_worker.submit_pdf_job() 提交到 bounded ThreadPoolExecutor 執行，
    隔離於 starlette request threadpool 之外（見 services/pdf_worker.py）。"""
    try:
        with session_scope() as session:
            report = session.query(StudentGrowthReport).filter_by(id=report_id).first()
            if not report:
                logger.warning("PDF job: report %d not found", report_id)
                return
            report.status = REPORT_STATUS_GENERATING
            session.flush()

            student = session.query(Student).filter_by(id=report.student_id).first()
            if not student:
                report.status = REPORT_STATUS_FAILED
                report.error_message = "Student not found"
                return

            data = _collect_report_data(
                session, student, report.period_start, report.period_end, report
            )
            pdf_bytes = generate_growth_report_pdf(report_data=data)

            if len(pdf_bytes) > MAX_PDF_SIZE_BYTES:
                report.status = REPORT_STATUS_FAILED
                report.error_message = (
                    f"PDF 超過大小上限（{len(pdf_bytes)} > {MAX_PDF_SIZE_BYTES} bytes）"
                )
                logger.error(
                    "growth report %d exceeds size cap: %d bytes",
                    report.id,
                    len(pdf_bytes),
                )
                return

            storage_key = f"students/{student.id}/{report.id}.pdf"

            if settings.storage.backend == "supabase":
                get_backend().save(
                    module="growth_reports",
                    key=storage_key,
                    data=pdf_bytes,
                    content_type="application/pdf",
                )
                report.file_path = storage_key  # 存 key，非 local path
                logger.info(
                    "PDF report %d written to storage: %s", report.id, storage_key
                )
            else:
                student_dir = REPORT_ROOT / str(student.id)
                student_dir.mkdir(parents=True, exist_ok=True)
                path = student_dir / f"{report.id}.pdf"
                path.write_bytes(pdf_bytes)
                # store as relative to cwd for portability
                try:
                    report.file_path = str(path.resolve().relative_to(Path.cwd()))
                except ValueError:
                    report.file_path = str(path.resolve())
                logger.info("PDF report %d ready: %s", report.id, path)

            report.status = REPORT_STATUS_READY
            report.file_size = len(pdf_bytes)
            report.generated_at = now_taipei_naive()
    except Exception as e:
        logger.exception("PDF generation failed for report %d", report_id)
        try:
            with session_scope() as session:
                r = session.query(StudentGrowthReport).filter_by(id=report_id).first()
                if r:
                    r.status = REPORT_STATUS_FAILED
                    r.error_message = str(e)[:1000]
        except Exception:
            logger.exception("Failed to mark report %d as failed", report_id)


def _resolve_pdf_path(file_path: str) -> Path:
    """Resolve stored path to absolute, enforcing REPORT_ROOT containment.

    Raises ValueError when the resolved path escapes REPORT_ROOT — defense in depth
    against DB tampering / future code paths that might let user input reach file_path.
    """
    p = Path(file_path)
    candidate = p if p.is_absolute() else (Path.cwd() / p)
    resolved = candidate.resolve()
    root = REPORT_ROOT.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"report path outside REPORT_ROOT: {file_path}") from exc
    return resolved


@router.post("/{student_id}/growth-reports", status_code=201)
async def create_growth_report(
    student_id: int,
    payload: GenerateReportPayload,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_PUBLISH)),
) -> dict:
    try:
        if payload.period_start > payload.period_end:
            raise HTTPException(
                status_code=422, detail="period_start 必須早於 period_end"
            )
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_PUBLISH.value)
            student = session.query(Student).filter_by(id=student_id).first()
            if not student:
                raise HTTPException(status_code=404, detail="學生不存在")
            # generated_by → employees.id（從 users.employee_id 取，nullable 安全）
            from models.auth import User as _AuthUser

            _auth_user = (
                session.query(_AuthUser)
                .filter_by(id=current_user.get("user_id"))
                .first()
            )
            _emp_id = _auth_user.employee_id if _auth_user else None
            r = StudentGrowthReport(
                student_id=student_id,
                period_label=payload.period_label,
                period_start=payload.period_start,
                period_end=payload.period_end,
                teacher_narrative=payload.teacher_narrative,
                generated_by=_emp_id,
                status=REPORT_STATUS_PENDING,
            )
            # F-V6-02：用 SAVEPOINT + partial unique 接住並發雙擊；存在 active
            # 同 period 報告時 → 409 帶 existing report_id；'failed' 不擋（容許 retry）
            try:
                with session.begin_nested():
                    session.add(r)
                    session.flush()
            except IntegrityError:
                existing = (
                    session.query(StudentGrowthReport)
                    .filter(
                        StudentGrowthReport.student_id == student_id,
                        StudentGrowthReport.period_label == payload.period_label,
                        StudentGrowthReport.period_start == payload.period_start,
                        StudentGrowthReport.period_end == payload.period_end,
                        StudentGrowthReport.status != REPORT_STATUS_FAILED,
                    )
                    .first()
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"同 period 已有報告（report_id={existing.id if existing else '?'}, "
                        f"status={existing.status if existing else 'unknown'}）"
                    ),
                )
            session.refresh(r)
            report_id = r.id
            row_dict = _row_to_dict(r)
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"建立成長報告：student_id={student_id} report_id={report_id} "
                f"period={payload.period_label}"
            )
        pdf_worker.submit_pdf_job(report_id)
        logger.info("growth report queued: student=%d report=%d", student_id, report_id)
        return row_dict
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="建立成長報告失敗")


@router.get("/{student_id}/growth-reports")
async def list_growth_reports(
    student_id: int,
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_READ.value)
            rows = (
                session.query(StudentGrowthReport)
                .filter(StudentGrowthReport.student_id == student_id)
                .order_by(StudentGrowthReport.created_at.desc())
                .offset(skip)
                .limit(limit)
                .all()
            )
            write_explicit_audit(
                request,
                action="READ",
                entity_type="student_growth_report",
                entity_id=str(student_id),
                summary=f"查詢成長報告列表：student_id={student_id} count={len(rows)}",
                changes={"skip": skip, "limit": limit, "count": len(rows)},
                dedup=True,
            )
            return {"items": [_row_to_dict(r) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢成長報告列表失敗")


@router.get("/{student_id}/growth-reports/{report_id}")
async def get_growth_report(
    student_id: int,
    report_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> dict:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_READ.value)
            r = (
                session.query(StudentGrowthReport)
                .filter_by(id=report_id, student_id=student_id)
                .first()
            )
            if not r:
                raise HTTPException(status_code=404, detail="報告不存在")
            write_explicit_audit(
                request,
                action="READ",
                entity_type="student_growth_report",
                entity_id=str(report_id),
                summary=f"查看成長報告詳情：student_id={student_id} report_id={report_id}",
                changes={"student_id": student_id, "period": r.period_label},
                dedup=True,
            )
            return _row_to_dict(r)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="查詢成長報告失敗")


@router.get("/{student_id}/growth-reports/{report_id}/download", response_model=None)
async def download_growth_report(
    student_id: int,
    report_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_READ)),
) -> FileResponse | RedirectResponse:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_READ.value)
            r = (
                session.query(StudentGrowthReport)
                .filter_by(id=report_id, student_id=student_id)
                .first()
            )
            if not r:
                raise HTTPException(status_code=404, detail="報告不存在")
            if r.status != REPORT_STATUS_READY or not r.file_path:
                raise HTTPException(
                    status_code=409, detail=f"報告尚未準備好（status={r.status}）"
                )
            # PDF 下載不 dedup：每次下載都要可溯（個資法 §10 查閱/複製權）
            write_explicit_audit(
                request,
                action="READ",
                entity_type="student_growth_report",
                entity_id=str(report_id),
                summary=(
                    f"下載成長報告 PDF：student_id={student_id} report_id={report_id} "
                    f"period={r.period_label}"
                ),
                changes={"student_id": student_id, "period": r.period_label},
            )
            if settings.storage.backend == "supabase":
                backend = get_backend()
                ttl = settings.storage.supabase_signed_url_ttl
                url = backend.signed_url("growth_reports", r.file_path, ttl)
                return RedirectResponse(url=url, status_code=302)
            else:
                try:
                    path = _resolve_pdf_path(r.file_path)
                except ValueError:
                    logger.error(
                        "growth report %d has illegal file_path: %r",
                        report_id,
                        r.file_path,
                    )
                    raise HTTPException(status_code=410, detail="報告檔案已遺失")
                if not path.exists():
                    raise HTTPException(status_code=410, detail="報告檔案已遺失")
                return FileResponse(
                    str(path),
                    media_type="application/pdf",
                    filename=f"growth_report_{r.id}.pdf",
                )
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="下載報告失敗")


@router.delete("/{student_id}/growth-reports/{report_id}", status_code=204)
async def delete_growth_report(
    student_id: int,
    report_id: int,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_PUBLISH)),
) -> Response:
    try:
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_PUBLISH.value)
            r = (
                session.query(StudentGrowthReport)
                .filter_by(id=report_id, student_id=student_id)
                .first()
            )
            if not r:
                raise HTTPException(status_code=404, detail="報告不存在")
            if r.file_path:
                try:
                    path = _resolve_pdf_path(r.file_path)
                    if path.exists():
                        path.unlink()
                except ValueError:
                    logger.error(
                        "skip unlink for report %d: illegal file_path %r",
                        report_id,
                        r.file_path,
                    )
                except Exception:
                    logger.exception("刪 PDF 檔失敗: %s", r.file_path)
            session.delete(r)
            request.state.audit_entity_id = str(student_id)
            request.state.audit_summary = (
                f"刪除成長報告：student_id={student_id} report_id={report_id}"
            )
            return Response(status_code=204)
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="刪除報告失敗")


@router.post("/{student_id}/growth-reports/{report_id}/send-line")
async def send_growth_report_to_line(
    student_id: int,
    report_id: int,
    payload: SendLinePayload,
    request: Request,
    current_user: dict = Depends(require_permission(Permission.PORTFOLIO_PUBLISH)),
) -> dict:
    """推送報告通知給該學生綁定家長 LINE.

    並發/失敗策略：
    - Phase 1 in session_scope：with_for_update 鎖 row、檢查 5 分鐘冪等、
      預先寫入 line_sent_at（claim slot），避免兩個 request 同時通過檢查雙推。
    - Phase 2 在 session 外執行 LINE 推送（asyncio.to_thread 避免 sync requests
      阻塞 event loop；DB connection 也不會被外網延遲卡住）。
    - Phase 3 若 sent_count == 0（網路掛 / token 過期 / 全部失敗），開新 session
      回滾 line_sent_at 至原值並回 502，admin 可立即重試而非卡 5 分鐘。
    """
    try:
        # Phase 1: lock row + claim idempotency slot inside session_scope
        line_user_ids: list[str] = []
        previous_sent_at: Optional[datetime] = None
        claimed_sent_at: Optional[datetime] = None
        period_label: str = ""
        with session_scope() as session:
            assert_student_access(session, current_user, student_id, code=Permission.PORTFOLIO_PUBLISH.value)
            r = (
                session.query(StudentGrowthReport)
                .filter_by(id=report_id, student_id=student_id)
                .with_for_update()
                .first()
            )
            if not r:
                raise HTTPException(status_code=404, detail="報告不存在")
            if r.status != REPORT_STATUS_READY:
                raise HTTPException(status_code=409, detail="報告尚未準備好")
            # 5 分鐘冪等：防前端重複提交或 admin 連點造成家長收重複 LINE
            if r.line_sent_at and (
                now_taipei_naive() - r.line_sent_at < timedelta(minutes=5)
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"5 分鐘內已推送過（last_sent_at={r.line_sent_at.isoformat()}），"
                        f"請稍後再試"
                    ),
                )

            from models.database import Guardian
            from models.auth import User as _UserModel

            line_user_ids_q = (
                session.query(_UserModel.line_user_id)
                .join(Guardian, Guardian.user_id == _UserModel.id)
                .filter(
                    Guardian.student_id == student_id,
                    Guardian.deleted_at.is_(None),
                    _UserModel.line_user_id.isnot(None),
                )
                .all()
            )
            line_user_ids = [row[0] for row in line_user_ids_q if row[0]]
            if not line_user_ids:
                raise HTTPException(status_code=409, detail="該學生家長未綁定 LINE")

            # Pre-claim：搶占 5 分鐘窗口；推送失敗會在 Phase 3 回滾
            previous_sent_at = r.line_sent_at
            r.line_sent_at = now_taipei_naive()
            claimed_sent_at = r.line_sent_at
            period_label = r.period_label
            report_pk = r.id
        # session_scope 出 with 區塊，pre-claim 已 commit 並釋放 row lock

        # Phase 2 (Phase 4 Section 3): 改走 dispatch.send_to_line_user_sync 同步 API
        # 拿真實 ACK；caller 不再直接呼叫 line_service 的個人推送 method。
        # 走 LINE_HANDLERS["growth_report.published"] handler 構訊息（payload.message
        # 若有給，pass via context 讓 handler 用；無則 handler 用預設模板）。
        from services.notification import dispatch as _dispatch

        push_context = {
            "student_name": "",  # handler 內未使用，避免 KeyError
            "period": period_label,
            "report_id": report_pk,
        }
        if payload.message:
            push_context["custom_message"] = payload.message

        sent_count = 0
        push_error: Optional[BaseException] = None
        try:
            for uid in line_user_ids:
                ok = await asyncio.to_thread(
                    _dispatch.send_to_line_user_sync,
                    uid,
                    "growth_report.published",
                    push_context,
                )
                if ok:
                    sent_count += 1
        except Exception as exc:  # noqa: BLE001
            push_error = exc

        # Phase 3: 全部失敗 → 回滾 claim，回 502 讓 admin 立即重試
        if sent_count == 0:
            with session_scope() as session2:
                r2 = (
                    session2.query(StudentGrowthReport)
                    .filter_by(id=report_id, student_id=student_id)
                    .with_for_update()
                    .first()
                )
                if r2 is not None:
                    r2.line_sent_at = previous_sent_at
            if push_error is not None:
                logger.warning(
                    "LINE 推送對 report_id=%s 全失敗（含例外）：%s",
                    report_id,
                    push_error,
                )
            else:
                logger.warning(
                    "LINE 推送對 report_id=%s 全失敗（共 %s 位家長）",
                    report_id,
                    len(line_user_ids),
                )
            raise HTTPException(
                status_code=502,
                detail=(
                    f"LINE 推送全部失敗（共 {len(line_user_ids)} 位家長），"
                    f"已釋放冪等鎖可立即重試"
                ),
            )

        # Phase 4: 全成功或部份成功 — claim 已生效，記稽核並回應
        request.state.audit_entity_id = str(student_id)
        request.state.audit_summary = (
            f"LINE 推送成長報告：student_id={student_id} report_id={report_id} "
            f"to {sent_count}/{len(line_user_ids)} 位家長"
        )
        return {
            "sent_count": sent_count,
            "line_sent_at": claimed_sent_at.isoformat() if claimed_sent_at else None,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise_safe_500(e, context="LINE 推送失敗")


pdf_worker.configure_job_callable(_generate_pdf_job)

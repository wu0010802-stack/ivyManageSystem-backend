"""
api/activity/registrations_pending.py — 才藝報名審核工作流

含 7 個端點 + 3 個 schema，處理家長公開報名後的後台審核流程：
- GET /registrations/pending     列出待審核 / 已拒絕報名
- GET /students/search           審核時模糊搜尋在籍學生
- POST /registrations/{id}/match        手動匹配 student_id
- POST /registrations/{id}/reject       拒絕報名（軟刪 + reason）
- POST /registrations/{id}/rematch      改正資料後重新比對
- POST /registrations/{id}/force-accept 校外生強制收件
- POST /registrations/{id}/restore      還原已拒絕報名

注意：含 /{id}/match etc. 動態路徑；__init__.py 內必須在
registrations_static_router 之後 include（靜態 batch-payment 等優先）。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from models.database import (
    get_session,
    ActivityRegistration,
    RegistrationCourse,
)
from services.activity_service import activity_service, OCCUPYING_STATUSES
from utils.activity_constants import effective_capacity
from utils.advisory_lock import acquire_activity_registration_lock
from utils.errors import raise_safe_500
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.portfolio_access import can_view_guardian_pii, can_view_student_pii
from utils.search import LIKE_ESCAPE_CHAR, escape_like_pattern

from ._shared import (
    _calc_total_amount,
    _compute_is_paid,
    _invalidate_after_registration_mutation,
    _not_found,
    now_taipei_naive,
    resolve_student_pii_scope,
    student_pii_row_visible,
    terminal_student_ids_in,
)

from schemas.activity_admin import (
    RegistrationMatchRequest,
    RegistrationRejectRequest,
    RegistrationRematchRequest,
    PendingRegistrationActionResultOut,
    PendingRegistrationForceAcceptResultOut,
    PendingRegistrationListOut,
    PendingRegistrationRematchResultOut,
    PendingRegistrationsSearchStudentsOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()
# ── 審核工作流（pending / match / reject / rematch / students-search）─────
# request schemas（RegistrationMatchRequest / RegistrationRejectRequest /
# RegistrationRematchRequest）已移至 schemas/activity_admin.py


def _serialize_pending_item(
    r: ActivityRegistration,
    *,
    can_see_student_pii: bool = True,
    can_see_guardian_pii: bool = True,
) -> dict:
    """F-026：對缺 STUDENTS_READ 遮罩 birthday / classroom_id；對缺 GUARDIANS_READ
    遮罩 parent_phone / email。"""
    return {
        "id": r.id,
        "student_name": r.student_name,
        "birthday": r.birthday if can_see_student_pii else None,
        "class_name": r.class_name,
        "classroom_id": r.classroom_id if can_see_student_pii else None,
        "parent_phone": r.parent_phone if can_see_guardian_pii else None,
        "match_status": r.match_status,
        "pending_review": r.pending_review,
        "email": r.email if can_see_guardian_pii else None,
        "school_year": r.school_year,
        "semester": r.semester,
        "remark": r.remark or "",
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "reviewed_by": r.reviewed_by,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
    }


@router.get("/registrations/pending", response_model=PendingRegistrationListOut)
def list_pending_registrations(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = None,
    school_year: Optional[int] = Query(None, ge=100, le=200),
    semester: Optional[int] = Query(None, ge=1, le=2),
    status: str = Query("all", pattern="^(pending|rejected|all)$"),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_READ)),
):
    """取得待審核 / 已拒絕報名清單（合併於同一頁）。

    status=pending：pending_review=true、is_active=true
    status=rejected：match_status='rejected'、is_active=false
    status=all（預設）：兩者聯集，前端以 match_status / is_active 判斷顯示
    """
    from utils.academic import resolve_academic_term_filters
    from sqlalchemy import and_, or_

    session = get_session()
    try:
        sy, sem = resolve_academic_term_filters(school_year, semester)
        q = session.query(ActivityRegistration).filter(
            ActivityRegistration.school_year == sy,
            ActivityRegistration.semester == sem,
        )
        pending_cond = and_(
            ActivityRegistration.pending_review.is_(True),
            ActivityRegistration.is_active.is_(True),
        )
        rejected_cond = and_(
            ActivityRegistration.match_status == "rejected",
            ActivityRegistration.is_active.is_(False),
        )
        if status == "pending":
            q = q.filter(pending_cond)
        elif status == "rejected":
            q = q.filter(rejected_cond)
        else:
            q = q.filter(or_(pending_cond, rejected_cond))
        # A1：家長電話屬 Guardian PII（與 /activity/students/search 口徑一致）。缺
        # GUARDIANS_READ 時搜尋條件不含手機欄位——否則可用候選手機觀察「有/無命中」
        # 反查電話↔學生關聯，繞過下方輸出端對 parent_phone 的遮罩。
        can_see_guardian = can_view_guardian_pii(current_user)
        if search:
            # S2：跳脫 % / _ 萬用字元，避免搜尋 '%' 全表匹配
            like = f"%{escape_like_pattern(search)}%"
            search_predicates = [
                ActivityRegistration.student_name.ilike(like, escape=LIKE_ESCAPE_CHAR),
                ActivityRegistration.class_name.ilike(like, escape=LIKE_ESCAPE_CHAR),
            ]
            if can_see_guardian:
                search_predicates.append(
                    ActivityRegistration.parent_phone.ilike(
                        like, escape=LIKE_ESCAPE_CHAR
                    )
                )
            q = q.filter(or_(*search_predicates))
        total = q.count()
        # 合併頁：待審核排前（created_at 倒序），已拒絕排後（reviewed_at 倒序）
        rows = (
            q.order_by(
                ActivityRegistration.is_active.desc(),
                ActivityRegistration.created_at.desc(),
            )
            .offset(skip)
            .limit(limit)
            .all()
        )
        # F-026：缺 STUDENTS_READ / GUARDIANS_READ 時遮罩對應 PII 欄位
        # S7：STUDENTS_READ:own_class 者對非管轄班級的列照樣遮罩（per-row）
        pii_visible, pii_allowed = resolve_student_pii_scope(session, current_user)
        # can_see_guardian 已於搜尋條件前算過（手機 predicate 把關），此處沿用同值
        # #4：scoped caller 對終態學生遮 birthday/FK
        terminal_ids = (
            terminal_student_ids_in(session, [r.student_id for r in rows])
            if pii_allowed is not None
            else set()
        )
        return {
            "items": [
                _serialize_pending_item(
                    r,
                    can_see_student_pii=student_pii_row_visible(
                        pii_visible,
                        pii_allowed,
                        r.classroom_id,
                        student_terminal=r.student_id in terminal_ids,
                    ),
                    can_see_guardian_pii=can_see_guardian,
                )
                for r in rows
            ],
            "total": total,
            "skip": skip,
            "limit": limit,
            "school_year": sy,
            "semester": sem,
            "status": status,
        }
    finally:
        session.close()


@router.get("/students/search", response_model=PendingRegistrationsSearchStudentsOut)
def admin_search_students(
    q: str = Query(..., min_length=1, max_length=50),
    limit: int = Query(20, ge=1, le=50),
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台審核用：依姓名/學號/家長手機模糊搜尋在籍學生。

    F-027：搜尋結果含 student_id（學號）/ birthday 等學生 PII，必須額外要求
    STUDENTS_READ 權限；缺則 403（不採欄位遮罩，因搜尋結果無 PII 即無辨識力）。
    A1：parent_phone 屬 Guardian PII，另需 GUARDIANS_READ——缺則輸出遮罩為
    None，且搜尋條件不含手機欄位（關閉手機反查側信道），與 registrations 系列一致。
    """
    # F-027：缺 STUDENTS_READ 直接 403（避免「ACTIVITY_WRITE 拉學生目錄」側信道）
    if not can_view_student_pii(current_user):
        raise HTTPException(status_code=403, detail="缺少學生資料讀取權限")

    from models.database import Student, Classroom
    from sqlalchemy import or_

    session = get_session()
    try:
        # S7（D1）：STUDENTS_READ:own_class 者只能搜尋管轄班級的學生，
        # 防止「ACTIVITY_WRITE + :own_class」自訂角色拉全校學生目錄
        _, pii_allowed = resolve_student_pii_scope(session, current_user)
        if pii_allowed is not None and not pii_allowed:
            return {"items": []}

        # A1：家長電話屬 Guardian PII，與 registrations / pending 列表口徑一致——
        # 缺 GUARDIANS_READ 時 ① 輸出遮罩 parent_phone ② 搜尋條件不含手機欄位
        # （否則可用部分手機號反查學生，形成繞過 GUARDIANS_READ 的側信道）。
        can_guardian = can_view_guardian_pii(current_user)

        # S2：跳脫 % / _ 萬用字元，避免搜尋 '%' 拉全校學生目錄
        like = f"%{escape_like_pattern(q.strip())}%"
        search_predicates = [
            Student.name.ilike(like, escape=LIKE_ESCAPE_CHAR),
            Student.student_id.ilike(like, escape=LIKE_ESCAPE_CHAR),
        ]
        if can_guardian:
            search_predicates.append(
                Student.parent_phone.ilike(like, escape=LIKE_ESCAPE_CHAR)
            )
            search_predicates.append(
                Student.emergency_contact_phone.ilike(like, escape=LIKE_ESCAPE_CHAR)
            )
        query = (
            session.query(Student, Classroom)
            .outerjoin(Classroom, Classroom.id == Student.classroom_id)
            .filter(
                Student.is_active.is_(True),
                or_(*search_predicates),
            )
        )
        if pii_allowed is not None:
            query = query.filter(Student.classroom_id.in_(pii_allowed))
        rows = query.limit(limit).all()
        return {
            "items": [
                {
                    "id": s.id,
                    "student_id": s.student_id,
                    "name": s.name,
                    "birthday": s.birthday.isoformat() if s.birthday else None,
                    "classroom_id": s.classroom_id,
                    "classroom_name": c.name if c else None,
                    "parent_phone": s.parent_phone if can_guardian else None,
                }
                for s, c in rows
            ]
        }
    finally:
        session.close()


@router.post(
    "/registrations/{registration_id}/match",
    response_model=PendingRegistrationActionResultOut,
)
def match_registration(
    registration_id: int,
    body: RegistrationMatchRequest,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台手動將待審核 registration 綁定到指定 student_id。"""
    from models.database import Student, Classroom

    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
                ActivityRegistration.pending_review.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not reg:
            raise HTTPException(
                status_code=409,
                detail="該筆報名已不在待審核佇列（可能已被其他人處理）",
            )

        student = (
            session.query(Student)
            .filter(Student.id == body.student_id, Student.is_active.is_(True))
            .first()
        )
        if not student:
            raise HTTPException(status_code=400, detail="找不到啟用中的學生")

        # F3：擋「同學生同學期兩筆有效報名」。其餘 4 條寫入路徑（公開 register /
        # 家長 register / rematch / restore）皆已守此不變量，唯獨 match 漏掉，會讓
        # 同 student_id 同學期長出第二筆 active reg → 對帳/統計/POS 人頭混亂。
        # dedup 鍵用 student_id（本 bug 前提正是「姓名打錯需人工 match、卻解析到同
        # student_id」，rematch 那套 name+birthday 鍵抓不到）；advisory lock 以「目標
        # 學生身分 + 學期」序列化並發 match（DB partial unique 鍵不含 student_id、攔不住
        # 並發），SQLite no-op。
        acquire_activity_registration_lock(
            session,
            student_name=student.name,
            birthday=(student.birthday.isoformat() if student.birthday else ""),
            school_year=reg.school_year,
            semester=reg.semester,
        )
        dup = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id != reg.id,
                ActivityRegistration.student_id == student.id,
                ActivityRegistration.school_year == reg.school_year,
                ActivityRegistration.semester == reg.semester,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        if dup:
            raise HTTPException(
                status_code=400,
                detail="該學生本學期已有一筆有效報名，無法重複匹配；請改用既有報名編輯",
            )

        classroom = None
        if student.classroom_id:
            classroom = (
                session.query(Classroom)
                .filter(Classroom.id == student.classroom_id)
                .first()
            )

        reg.student_id = student.id
        reg.classroom_id = student.classroom_id
        if classroom:
            reg.class_name = classroom.name
        reg.pending_review = False
        reg.match_status = "manual"
        reg.reviewed_by = current_user.get("username")
        reg.reviewed_at = now_taipei_naive()
        session.commit()
        _invalidate_after_registration_mutation(session)
        logger.info(
            "後台手動匹配報名：reg_id=%s → student_id=%s by %s",
            reg.id,
            student.id,
            current_user.get("username"),
        )
        return {"message": "已完成手動匹配", "registration_id": reg.id}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error("手動匹配失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()


@router.post(
    "/registrations/{registration_id}/reject",
    response_model=PendingRegistrationActionResultOut,
)
def reject_registration(
    registration_id: int,
    body: RegistrationRejectRequest,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台將待審核 registration 視為校外生/資料不符拒絕。

    軟刪除（is_active=False）+ match_status='rejected' + remark 加註原因。
    """
    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not reg:
            raise HTTPException(
                status_code=409,
                detail="該筆報名已不存在或已被處理",
            )

        reg.is_active = False
        reg.match_status = "rejected"
        reg.pending_review = False
        # Phase 3：null 掉查詢碼 hash — 即使後續有人手動把 is_active 改回 True,
        # 舊 token 也無法用來打 /public/query-by-token（hash 比對不上 None）。
        # rejected 的 reg 沒有新 token 要發給誰，直接 invalidate 即可。
        # 資安 P0 (2026-05-07)：同步清 issued_at（避免改 hash 還能用 expiration 視窗）
        reg.query_token_hash = None
        reg.query_token_issued_at = None
        reg.reviewed_by = current_user.get("username")
        reg.reviewed_at = now_taipei_naive()
        reason = body.reason  # validator 已保證非空且已 strip
        prefix = (reg.remark or "").strip()
        note = f"[已拒絕 by {reg.reviewed_by}] {reason}"
        reg.remark = (prefix + "\n" + note).strip() if prefix else note
        activity_service.log_change(
            session,
            registration_id,
            reg.student_name,
            "拒絕報名",
            f"拒絕原因：{reason}",
            reg.reviewed_by or "",
        )

        # 比照 delete_registration：收集被拒報名佔位的課程 id，
        # flush 後對每門課嘗試遞補候補第一位。
        occupying_course_ids = [
            rc.course_id
            for rc in session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == registration_id,
                RegistrationCourse.status.in_(list(OCCUPYING_STATUSES)),
            )
            .all()
        ]
        session.flush()  # 使 is_active=False 對 _active_course_query 生效
        for course_id in occupying_course_ids:
            activity_service._auto_promote_first_waitlist(session, course_id)

        session.commit()
        _invalidate_after_registration_mutation(session)
        logger.warning(
            "後台拒絕報名：reg_id=%s by %s reason=%s",
            reg.id,
            current_user.get("username"),
            reason,
        )
        return {"message": "已拒絕該筆報名", "registration_id": reg.id}
    except HTTPException:
        session.rollback()
        raise
    except Exception as e:
        session.rollback()
        logger.error("拒絕報名失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()


@router.post(
    "/registrations/{registration_id}/rematch",
    response_model=PendingRegistrationRematchResultOut,
)
def rematch_registration(
    registration_id: int,
    body: Optional[RegistrationRematchRequest] = None,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台重跑三欄比對（可同時修正 name/birthday/parent_phone）。

    body 任一欄位非 None 時先寫回 registration，再用新值跑比對。
    即使比對仍失敗，編輯的欄位也會保留，避免校方白打一次。
    """
    from models.database import Classroom
    from ._shared import _match_student_with_parent_phone, find_active_dup_for_student

    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not reg:
            raise _not_found("報名資料")

        new_name = reg.student_name
        new_birthday = reg.birthday
        new_phone = reg.parent_phone
        field_changed = False
        if body is not None:
            if body.name is not None and body.name != reg.student_name:
                new_name = body.name
                field_changed = True
            if body.birthday is not None and body.birthday != reg.birthday:
                new_birthday = body.birthday
                field_changed = True
            if body.parent_phone is not None and body.parent_phone != reg.parent_phone:
                new_phone = body.parent_phone
                field_changed = True

        # 若 name/birthday 有變，檢查同學期是否已有另一筆有效報名會重複
        if field_changed and (
            new_name != reg.student_name or new_birthday != reg.birthday
        ):
            # C6：以「修改後身分」取 advisory lock 序列化同學生同學期的並發改身分，否則
            # 兩筆不同 reg 同時改成同一身分，各自 dup SELECT 看不到對方未 commit 的身分
            # 翻轉 → 雙雙通過 → 兩筆有效報名（partial unique 鍵含 phone，phone 不同/NULL
            # 故不攔）。與 restore（P2-3）對齊。SQLite no-op。
            acquire_activity_registration_lock(
                session,
                student_name=new_name,
                birthday=new_birthday,
                school_year=reg.school_year,
                semester=reg.semester,
            )
            dup = (
                session.query(ActivityRegistration)
                .filter(
                    ActivityRegistration.id != reg.id,
                    ActivityRegistration.student_name == new_name,
                    ActivityRegistration.birthday == new_birthday,
                    ActivityRegistration.school_year == reg.school_year,
                    ActivityRegistration.semester == reg.semester,
                    ActivityRegistration.is_active.is_(True),
                )
                .first()
            )
            if dup:
                raise HTTPException(
                    status_code=400,
                    detail="修改後的姓名/生日與本學期另一筆有效報名重複",
                )

        reg.student_name = new_name
        reg.birthday = new_birthday
        reg.parent_phone = new_phone

        sid, cid = _match_student_with_parent_phone(
            session, reg.student_name, reg.birthday, reg.parent_phone
        )
        matched = False
        if sid and cid:
            classroom = (
                session.query(Classroom)
                .filter(
                    Classroom.id == cid,
                    Classroom.is_active.is_(True),
                )
                .first()
            )
            if classroom:
                # P1（2026-06-23 code review）：rematch 解析到 student_id 後守同學生
                # 同學期唯一性。未改 name/birthday 時上方 advisory（C6）不取、且既有
                # dup 檢查以 name+birthday 為鍵僅在 field_changed 時跑 → 直接 rematch
                # （無欄位變更）會讓 pending 綁到已有 active 報名的同學生長出第二筆。
                # 此處補取同一把報名 advisory（idempotent；reg.student_name 即配對學生
                # 姓名）+ student_id 檢查。
                acquire_activity_registration_lock(
                    session,
                    student_name=reg.student_name,
                    birthday=reg.birthday,
                    school_year=reg.school_year,
                    semester=reg.semester,
                )
                if find_active_dup_for_student(
                    session,
                    student_id=sid,
                    school_year=reg.school_year,
                    semester=reg.semester,
                    exclude_reg_id=reg.id,
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "該學生本學期已有一筆有效報名，無法重複匹配；"
                            "請改用既有報名編輯"
                        ),
                    )
                reg.student_id = sid
                reg.classroom_id = cid
                reg.class_name = classroom.name
                reg.pending_review = False
                reg.match_status = "matched"
                reg.reviewed_by = current_user.get("username")
                reg.reviewed_at = now_taipei_naive()
                matched = True

        session.commit()
        _invalidate_after_registration_mutation(session)
        logger.info(
            "後台重新比對：reg_id=%s matched=%s fields_edited=%s by %s",
            reg.id,
            matched,
            field_changed,
            current_user.get("username"),
        )
        if matched:
            msg = "重新比對成功"
        elif field_changed:
            msg = "仍無符合的在校生，已保留修改後的資料"
        else:
            msg = "仍無符合的在校生，請手動處理"
        return {
            "message": msg,
            "matched": matched,
            "field_changed": field_changed,
            "registration_id": reg.id,
        }
    except HTTPException:
        session.rollback()
        raise
    except IntegrityError:
        # C7：advisory lock 已序列化同身分並發，此處為極窄並發窗口下仍撞 DB partial
        # unique index（同 phone）的兜底，回乾淨 409 而非 raise_safe_500 的 500
        # （與 restore 對齊）。
        session.rollback()
        raise HTTPException(status_code=409, detail="本學期已有同一學生的有效報名")
    except Exception as e:
        session.rollback()
        logger.error("重新比對失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()


@router.post(
    "/registrations/{registration_id}/force-accept",
    response_model=PendingRegistrationForceAcceptResultOut,
)
def force_accept_registration(
    registration_id: int,
    body: Optional[RegistrationRematchRequest] = None,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """跳過三欄比對，強行將報名插入正式課後才藝報名管理並加上 `forced` 標記。

    body 與 rematch 相同三欄可選：校方可同時修正家長打錯的 name/birthday/phone。
    用途：家長是校外生或資料永遠比對不上，但校方決定收這筆報名。
    """
    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id == registration_id,
                ActivityRegistration.is_active.is_(True),
            )
            .with_for_update()
            .first()
        )
        if not reg:
            raise _not_found("報名資料")

        # 守衛：force-accept 是「待審核佇列」動作（家長校外生／資料永遠比對不上、校方決定
        # 收件）。已處理過的報名（matched / manual / forced，皆 pending_review=False）不應
        # 再被強行收件——否則持 ACTIVITY_WRITE 者可把已正確綁定 student_id 的正式報名翻成
        # forced 並改 name/birthday，而 student_id/classroom_id 不會跟著改 → 報名 PII 與
        # 學生 FK 不一致。對齊 restore 的「非目標狀態 → 400」慣例。
        if not reg.pending_review:
            raise HTTPException(
                status_code=400, detail="此報名已非待審核狀態，無法強行收件"
            )

        new_name = reg.student_name
        new_birthday = reg.birthday
        new_phone = reg.parent_phone
        field_changed = False
        if body is not None:
            if body.name is not None and body.name != reg.student_name:
                new_name = body.name
                field_changed = True
            if body.birthday is not None and body.birthday != reg.birthday:
                new_birthday = body.birthday
                field_changed = True
            if body.parent_phone is not None and body.parent_phone != reg.parent_phone:
                new_phone = body.parent_phone
                field_changed = True

        identity_changed = field_changed and (
            new_name != reg.student_name or new_birthday != reg.birthday
        )
        if identity_changed:
            # C6：以「修改後身分」取 advisory lock 序列化同學生同學期的並發改身分
            # （與 rematch / restore P2-3 對齊）。SQLite no-op。
            acquire_activity_registration_lock(
                session,
                student_name=new_name,
                birthday=new_birthday,
                school_year=reg.school_year,
                semester=reg.semester,
            )
            dup = (
                session.query(ActivityRegistration)
                .filter(
                    ActivityRegistration.id != reg.id,
                    ActivityRegistration.student_name == new_name,
                    ActivityRegistration.birthday == new_birthday,
                    ActivityRegistration.school_year == reg.school_year,
                    ActivityRegistration.semester == reg.semester,
                    ActivityRegistration.is_active.is_(True),
                )
                .first()
            )
            if dup:
                raise HTTPException(
                    status_code=400,
                    detail="修改後的姓名/生日與本學期另一筆有效報名重複",
                )

        reg.student_name = new_name
        reg.birthday = new_birthday
        reg.parent_phone = new_phone
        # FK/PII 一致性：pending 報名仍可能帶 student_id（matched→reject→restore 不清
        # student_id）。一旦在強行收件時改了 name/birthday，原 student_id/classroom_id
        # 指向的學生已與本報名新身分不符 → 清掉連結，forced 報名成為與新身分一致的未匹配
        # 紀錄（force-accept 本就「跳過比對」、不保留舊綁定）。未改身分則維持有效綁定。
        if identity_changed:
            reg.student_id = None
            reg.classroom_id = None
        reg.pending_review = False
        reg.match_status = "forced"
        reg.reviewed_by = current_user.get("username")
        reg.reviewed_at = now_taipei_naive()
        prefix = (reg.remark or "").strip()
        note = f"[強行收件 by {reg.reviewed_by}]"
        if prefix and "[強行收件" not in prefix:
            reg.remark = prefix + "\n" + note
        elif not prefix:
            reg.remark = note
        session.commit()
        _invalidate_after_registration_mutation(session)
        logger.warning(
            "後台強行收件報名：reg_id=%s by %s field_changed=%s",
            reg.id,
            current_user.get("username"),
            field_changed,
        )
        return {
            "message": "已強行收件並標記 forced",
            "matched": False,
            "forced": True,
            "field_changed": field_changed,
            "registration_id": reg.id,
        }
    except HTTPException:
        session.rollback()
        raise
    except IntegrityError:
        # C7：與 rematch / restore 對齊——極窄並發窗口下撞 DB partial unique index
        # 的兜底，回乾淨 409 而非 raise_safe_500 的 500。
        session.rollback()
        raise HTTPException(status_code=409, detail="本學期已有同一學生的有效報名")
    except Exception as e:
        session.rollback()
        logger.error("強行收件失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()


@router.post(
    "/registrations/{registration_id}/restore",
    response_model=PendingRegistrationActionResultOut,
)
def restore_registration(
    registration_id: int,
    current_user: dict = Depends(require_staff_permission(Permission.ACTIVITY_WRITE)),
):
    """後台將已拒絕（軟刪除）的報名復原回待審核狀態。

    僅限 match_status='rejected' 且 is_active=False 的報名。
    復原後 is_active=True、match_status='pending'、pending_review=True，
    保留原拒絕人/時間於 remark 作為歷史軌跡。
    """
    session = get_session()
    try:
        reg = (
            session.query(ActivityRegistration)
            .filter(ActivityRegistration.id == registration_id)
            .with_for_update()
            .first()
        )
        if not reg:
            raise _not_found("報名資料")
        if reg.match_status != "rejected" or reg.is_active:
            raise HTTPException(
                status_code=400, detail="此筆報名非已拒絕狀態，無法復原"
            )

        # P2-3：以報名身分取 advisory lock 序列化同學生同學期的並發 restore。否則
        # 兩筆同學生同學期、phone 不同的已拒絕報名同時 restore，dup 檢查（純 SELECT）
        # 各自看不到對方未 commit 的 is_active 翻轉 → 雙雙通過 → 兩筆有效報名（DB
        # partial unique index 鍵含 phone、phone 不同故不攔）。取鎖後第二筆的 dup
        # 檢查必看到第一筆已 commit 的 active → 400。SQLite 測試 no-op。
        acquire_activity_registration_lock(
            session,
            student_name=reg.student_name,
            birthday=reg.birthday,
            school_year=reg.school_year,
            semester=reg.semester,
        )

        # 若本學期已有同姓名/生日的有效報名，擋下避免唯一性衝突
        dup = (
            session.query(ActivityRegistration)
            .filter(
                ActivityRegistration.id != reg.id,
                ActivityRegistration.student_name == reg.student_name,
                ActivityRegistration.birthday == reg.birthday,
                ActivityRegistration.school_year == reg.school_year,
                ActivityRegistration.semester == reg.semester,
                ActivityRegistration.is_active.is_(True),
            )
            .first()
        )
        if dup:
            raise HTTPException(
                status_code=400,
                detail="本學期已有同姓名/生日的有效報名，無法復原此筆",
            )

        # P2-2（2026-06-23 audit）：終態學生守衛。matched 在籍生若期間已離校/畢業/轉出
        # （Student.is_active=False），restore 翻回 active + 保留 enrolled 課程列會長出
        # 幽靈 enrolled（佔容量、inflate total_enrollments，卻因 Student.is_active=False
        # 不出現在點名/出席）。對齊 confirm/promote_waitlist/_auto_promote 的終態守衛，
        # 擋下復原。student_id 為 NULL（校外生）無終態概念，不受影響。
        if reg.student_id is not None:
            from models.database import Student

            student_active = (
                session.query(Student.is_active)
                .filter(Student.id == reg.student_id)
                .scalar()
            )
            if student_active is False:
                raise HTTPException(
                    status_code=400,
                    detail="該生已離校／畢業／轉出，無法復原其報名為有效狀態",
                )

        reg.is_active = True
        reg.match_status = "pending"
        reg.pending_review = True

        # code review #1（High）：reject 清掉 query_token_hash/issued_at，restore 卻不
        # 重發 → _parent_mutation_identity_ok 把 NULL hash 當「無 token 舊報名」退回
        # 姓名+生日+電話三欄弱驗證，等於把 token 時代（資安 #5 強驗證）的報名被拒→
        # 復原後永久降級。restore 時重新產生 token_hash（業主裁定：不回明文）——公開
        # 破壞性 mutation 的三欄路徑即失效；明文無人持有故此筆對公開 mutation 關閉，
        # 家長改走登入家長端或由後台管理。
        from services.activity_query_token import (
            _generate_query_token,
            _hash_query_token,
        )

        reg.query_token_hash = _hash_query_token(_generate_query_token())
        reg.query_token_issued_at = now_taipei_naive()

        # 容量重檢（Task A4 超賣修正）：被拒報名的 RegistrationCourse 列在 reject
        # 時並未清掉（仍掛 enrolled/promoted_pending），且被拒期間名額可能已被其他
        # 報名遞補。直接翻 is_active=True 會讓占容量數超過 capacity（超賣）。
        # 因此 restore 時重數每門課的占位，超出容量者降為 waitlist。
        from sqlalchemy import func
        from models.database import (
            ActivityAttendance,
            ActivityCourse,
            ActivitySession,
            ActivitySupply,
            RegistrationCourse,
            RegistrationSupply,
        )

        rc_rows = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg.id,
                RegistrationCourse.status.in_(list(OCCUPYING_STATUSES)),
            )
            .all()
        )
        # code review #3（Medium）：一次以 id 排序整批鎖定所有相關課程，而非逐課
        # `with_for_update().first()`。否則兩筆分別含 [A,B]、[B,A] 的並行 restore 會以
        # 相反順序逐一鎖課程形成 ABBA 循環等待（advisory lock 為 per-student、不同學生
        # 共用課程時不序列化，擋不住）。對齊 register_courses 的批次鎖策略；SQLite 下
        # FOR UPDATE 為 no-op，真正序列化由 PostgreSQL 行鎖提供。
        course_ids = sorted({rc.course_id for rc in rc_rows})
        locked_courses: dict = {}
        if course_ids:
            locked_courses = {
                c.id: c
                for c in session.query(ActivityCourse)
                .filter(ActivityCourse.id.in_(course_ids))
                .order_by(ActivityCourse.id.asc())
                .with_for_update()
                .all()
            }
        dropped_course_ids: list = []  # 已剔除（停用）課程，迴圈後一併清考勤
        for rc in rc_rows:
            course = locked_courses.get(rc.course_id)
            # 課程不存在（孤兒列）→ 略過
            if not course:
                continue
            # code review #2（High）：被拒期間課程可能被停用（停用守衛只計 active
            # 報名，故被拒報名的課程可被下架）。直接翻 active 會讓 _calc_total_amount 仍
            # 把已下架課程列入計費。業主裁定：剔除已停用課程列（對齊 withdraw_course 的
            # session.delete），不向家長收已下架課程費用。
            if not course.is_active:
                dropped_course_ids.append(rc.course_id)
                session.delete(rc)
                continue
            # 未設容量上限沿用 _attach_courses 慣例：None → 30
            capacity = effective_capacity(course)
            # 占容量 = 其他「有效報名」的 enrolled + promoted_pending（排除本筆 reg）
            occupying = (
                session.query(func.count(RegistrationCourse.id))
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .filter(
                    RegistrationCourse.course_id == rc.course_id,
                    RegistrationCourse.status.in_(list(OCCUPYING_STATUSES)),
                    ActivityRegistration.is_active.is_(True),
                    RegistrationCourse.registration_id != reg.id,
                )
                .scalar()
            )
            if occupying >= capacity:
                # code review（P2）：滿班時只有「開放候補」的課程可降 waitlist；不開放
                # 候補者無處可放（對齊 _attach_courses / add_course / parent_portal 滿班且
                # 不開放候補一律 400 的守衛）。restore 是盡力復原語意，比照上方停用課程
                # 剔除此列（session.delete + 後續清考勤 + 重算 total），不向家長保留違反
                # 「不開放候補」設定、且永不會被 promote 的死候補列。業主裁定：剔除該課程列。
                if course.allow_waitlist:
                    rc.status = "waitlist"
                else:
                    dropped_course_ids.append(rc.course_id)
                    session.delete(rc)
                    continue

            # Bug 2 修正（P2）：無論容量是否足夠，promoted_pending 列的確認計時欄位
            # 必須清為 None（停錶）。restore 把整筆報名打回 pending_review=True，
            # 此時 confirm_deadline 若保留舊的過去時間，下一輪
            # sweep_expired_pending_promotions（篩 confirm_deadline IS NOT NULL AND < now）
            # 會立刻把它當逾期刪掉，家長從未拿到新確認窗就被靜默踢掉名額。
            # 對齊 services/activity_service.py:802-804 promote_waitlist 同樣清這三欄。
            rc.confirm_deadline = None
            rc.reminder_sent_at = None
            rc.final_reminder_sent_at = None

        # code review #2（High）：已停用用品同樣剔除。restore 原本完全不碰用品列，
        # _calc_total_amount 又無條件加總所有 RegistrationSupply（不論 supply.is_active）→
        # 家長被收已下架用品費用。比照課程剔除已停用用品列（session.delete）。
        inactive_supply_rows = (
            session.query(RegistrationSupply)
            .join(ActivitySupply, ActivitySupply.id == RegistrationSupply.supply_id)
            .filter(
                RegistrationSupply.registration_id == reg.id,
                ActivitySupply.is_active.is_(False),
            )
            .all()
        )
        for rs in inactive_supply_rows:
            session.delete(rs)

        # 剔除停用課程後一併清該課考勤（對齊 withdraw_course 慣例）：ActivityAttendance
        # FK 掛在 registration + session（非 registration_course），刪 RegistrationCourse
        # 不會 cascade 清考勤。留孤兒會污染出席統計，且未來此生重報該課時撞
        # uq_activity_attendance_session_reg。
        if dropped_course_ids:
            session_ids_subq = (
                session.query(ActivitySession.id)
                .filter(ActivitySession.course_id.in_(dropped_course_ids))
                .subquery()
            )
            session.query(ActivityAttendance).filter(
                ActivityAttendance.registration_id == reg.id,
                ActivityAttendance.session_id.in_(session_ids_subq),
            ).delete(synchronize_session=False)

        # Bug 1 修正（P2）：上述迴圈可能把課程降 waitlist / 剔除停用課程/用品，改變應繳
        # total。但 restore 原本沒重算 is_paid，導致 reg.is_paid 停在拒絕前的舊值（例如
        # True）→ 帳面出現幽靈超繳。其他改課路徑（withdraw_course / add_course /
        # public update）一律在改動後重算，restore 補上對齊。先 flush 讓 _calc_total_amount
        # 看到剛剛的 delete。參照 api/activity/registrations_items.py:143-144 慣例。
        session.flush()
        total_amount = _calc_total_amount(session, reg.id)
        reg.is_paid = _compute_is_paid(reg.paid_amount or 0, total_amount)

        prefix = (reg.remark or "").strip()
        note = f"[已還原 by {current_user.get('username')}]"
        reg.remark = (prefix + "\n" + note).strip() if prefix else note
        session.commit()
        _invalidate_after_registration_mutation(session)
        logger.info(
            "後台還原拒絕報名：reg_id=%s by %s",
            reg.id,
            current_user.get("username"),
        )
        return {"message": "已還原報名至待審核", "registration_id": reg.id}
    except HTTPException:
        session.rollback()
        raise
    except IntegrityError:
        # P2-3：advisory lock 已序列化同身分 restore，此處為極窄並發窗口下仍撞 DB
        # partial unique index（同 phone）的兜底，回乾淨 409 而非 raise_safe_500 的 500。
        session.rollback()
        raise HTTPException(
            status_code=409, detail="本學期已有同一學生的有效報名，無法復原此筆"
        )
    except Exception as e:
        session.rollback()
        logger.error("還原報名失敗：%s", e)
        raise_safe_500(e)
    finally:
        session.close()

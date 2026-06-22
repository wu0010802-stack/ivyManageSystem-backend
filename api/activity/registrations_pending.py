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
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import IntegrityError

from models.database import (
    get_session,
    ActivityRegistration,
)
from services.activity_service import activity_service
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
    _validate_tw_mobile,
    now_taipei_naive,
    resolve_student_pii_scope,
    student_pii_row_visible,
    terminal_student_ids_in,
)

from schemas.activity_admin import (
    PendingRegistrationActionResultOut,
    PendingRegistrationForceAcceptResultOut,
    PendingRegistrationListOut,
    PendingRegistrationRematchResultOut,
    PendingRegistrationsSearchStudentsOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()
# ── 審核工作流（pending / match / reject / rematch / students-search）─────


class RegistrationMatchRequest(BaseModel):
    student_id: int = Field(..., gt=0)


class RegistrationRejectRequest(BaseModel):
    reason: str = Field(..., min_length=2, max_length=200)

    @field_validator("reason", mode="before")
    @classmethod
    def _strip_reason(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            if len(stripped) < 2:
                raise ValueError("拒絕原因至少需 2 個字，方便事後追溯")
            return stripped
        return v


class RegistrationRematchRequest(BaseModel):
    """重新比對可選欄位：校方可即時修正家長打錯的 name/birthday/parent_phone。

    三欄皆可選——未提供時沿用 registration 原值。提供的欄位會在比對前寫回 reg，
    即使比對仍失敗也保留修改內容，避免校方白打一次字。
    """

    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(None, min_length=1, max_length=50)
    birthday: Optional[str] = None
    parent_phone: Optional[str] = Field(None, min_length=8, max_length=30)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v):
        if isinstance(v, str):
            stripped = v.strip()
            return stripped or None
        return v

    @field_validator("birthday")
    @classmethod
    def _validate_birthday(cls, v):
        if v is None or v == "":
            return None
        from datetime import date as _d

        try:
            _d.fromisoformat(v)
        except ValueError:
            raise ValueError("生日格式必須為 YYYY-MM-DD")
        return v

    @field_validator("parent_phone", mode="before")
    @classmethod
    def _normalize_phone(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        from ._shared import _validate_tw_mobile

        return _validate_tw_mobile(v)


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
        if search:
            # S2：跳脫 % / _ 萬用字元，避免搜尋 '%' 全表匹配
            like = f"%{escape_like_pattern(search)}%"
            q = q.filter(
                or_(
                    ActivityRegistration.student_name.ilike(
                        like, escape=LIKE_ESCAPE_CHAR
                    ),
                    ActivityRegistration.class_name.ilike(
                        like, escape=LIKE_ESCAPE_CHAR
                    ),
                    ActivityRegistration.parent_phone.ilike(
                        like, escape=LIKE_ESCAPE_CHAR
                    ),
                )
            )
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
        can_see_guardian = can_view_guardian_pii(current_user)
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
    from ._shared import _match_student_with_parent_phone

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

        if field_changed and (
            new_name != reg.student_name or new_birthday != reg.birthday
        ):
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

        reg.is_active = True
        reg.match_status = "pending"
        reg.pending_review = True

        # 容量重檢（Task A4 超賣修正）：被拒報名的 RegistrationCourse 列在 reject
        # 時並未清掉（仍掛 enrolled/promoted_pending），且被拒期間名額可能已被其他
        # 報名遞補。直接翻 is_active=True 會讓占容量數超過 capacity（超賣）。
        # 因此 restore 時重數每門課的占位，超出容量者降為 waitlist。
        from sqlalchemy import func
        from models.database import ActivityCourse, RegistrationCourse

        rc_rows = (
            session.query(RegistrationCourse)
            .filter(
                RegistrationCourse.registration_id == reg.id,
                RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
            )
            .all()
        )
        for rc in rc_rows:
            course = (
                session.query(ActivityCourse)
                .filter(ActivityCourse.id == rc.course_id)
                .with_for_update()
                .first()
            )
            # 課程不存在或未設容量上限（沿用 _attach_courses 慣例：None → 30）
            if not course:
                continue
            capacity = course.capacity if course.capacity is not None else 30
            # 占容量 = 其他「有效報名」的 enrolled + promoted_pending（排除本筆 reg）
            occupying = (
                session.query(func.count(RegistrationCourse.id))
                .join(
                    ActivityRegistration,
                    RegistrationCourse.registration_id == ActivityRegistration.id,
                )
                .filter(
                    RegistrationCourse.course_id == rc.course_id,
                    RegistrationCourse.status.in_(["enrolled", "promoted_pending"]),
                    ActivityRegistration.is_active.is_(True),
                    RegistrationCourse.registration_id != reg.id,
                )
                .scalar()
            )
            if occupying >= capacity:
                rc.status = "waitlist"

            # Bug 2 修正（P2）：無論容量是否足夠，promoted_pending 列的確認計時欄位
            # 必須清為 None（停錶）。restore 把整筆報名打回 pending_review=True，
            # 此時 confirm_deadline 若保留舊的過去時間，下一輪
            # sweep_expired_pending_promotions（篩 confirm_deadline IS NOT NULL AND < now）
            # 會立刻把它當逾期刪掉，家長從未拿到新確認窗就被靜默踢掉名額。
            # 對齊 services/activity_service.py:802-804 promote_waitlist 同樣清這三欄。
            rc.confirm_deadline = None
            rc.reminder_sent_at = None
            rc.final_reminder_sent_at = None

        # Bug 1 修正（P2）：rc_rows 迴圈可能把部分課程降為 waitlist，降低了應繳 total。
        # 但 restore 通篇沒有重算 is_paid，導致 reg.is_paid 停在拒絕前的舊值（例如 True）
        # → 帳面出現幽靈超繳。其他改課路徑（withdraw_course / add_course / public update）
        # 一律在改動後重算，restore 補上對齊。
        # 參照 api/activity/registrations_items.py:143-144 慣例。
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

"""後台全域搜尋 endpoint。

GET /api/search?q=xxx → 一次回 8 類 entity 各 ≤ 8 筆。

權限：staff-only（拒絕 parent / teacher，teacher 走 api/portal/search）。
逐類做 READ 權限把關——無對應 READ 權限的類別回空陣列。
回傳含跨人 PII（家長遮罩電話、學生/家長姓名），比照 portal/search 顯式寫 READ audit。
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from models.activity import ActivityRegistration
from models.classroom import (
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)
from models.database import Classroom, Guardian, Student, get_session
from models.event import Announcement
from models.fees import StudentFeeRecord
from models.recruitment import RecruitmentVisit
from utils.audit import write_explicit_audit
from utils.auth import get_current_user
from utils.masking import mask_phone
from utils.permissions import Permission, has_permission
from utils.portfolio_access import (
    accessible_classroom_ids,
    can_view_guardian_pii,
    is_row_unrestricted,
)
from utils.search import (
    build_search_filter,
    normalize_query,
    relevance_key,
    tokenize_query,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["search"])

SECTION_LIMIT = 8
MIN_QUERY_LEN = 2

_TERMINAL = [LIFECYCLE_GRADUATED, LIFECYCLE_WITHDRAWN, LIFECYCLE_TRANSFERRED]


class SearchStudentItem(BaseModel):
    id: int
    name: str
    student_id: Optional[str] = None
    classroom_name: str = ""


class SearchEmployeeItem(BaseModel):
    id: int
    name: str
    employee_id: Optional[str] = None
    title: str = ""


class SearchGuardianItem(BaseModel):
    id: int
    name: str
    phone_masked: str = ""
    child_name: str = ""
    student_id: int


class SearchClassroomItem(BaseModel):
    id: int
    name: str
    school_year: Optional[int] = None
    semester: Optional[int] = None


class SearchFeeItem(BaseModel):
    record_id: int
    student_name: str
    classroom_name: str = ""
    period: str = ""
    status: str = ""


class SearchActivityItem(BaseModel):
    id: int
    student_name: str
    class_name: str = ""
    match_status: str = ""


class SearchRecruitmentItem(BaseModel):
    id: int
    child_name: str
    target_school_year: Optional[int] = None
    enrolled: bool = False


class SearchAnnouncementItem(BaseModel):
    id: int
    title: str
    created_at: Optional[str] = None


class GlobalSearchResult(BaseModel):
    q: str
    students: List[SearchStudentItem] = []
    employees: List[SearchEmployeeItem] = []
    guardians: List[SearchGuardianItem] = []
    classrooms: List[SearchClassroomItem] = []
    fees: List[SearchFeeItem] = []
    activity_registrations: List[SearchActivityItem] = []
    recruitment: List[SearchRecruitmentItem] = []
    announcements: List[SearchAnnouncementItem] = []


# ── module helper ─────────────────────────────────────────────────────────────


def _finalize(items: list[dict], nq: str, key: str) -> list[dict]:
    """相關性排序（穩定排序保留 DB order_by 作 tie-break）後截斷。"""
    items.sort(key=lambda d: relevance_key(d.get(key), nq))
    return items[:SECTION_LIMIT]


# ── section helpers ────────────────────────────────────────────────────────────


def _search_students(session, tokens, nq, current_user: dict) -> list[dict]:
    code = Permission.STUDENTS_READ.value
    unrestricted = is_row_unrestricted(current_user, code=code)
    qy = session.query(Student).filter(
        Student.is_active.is_(True),
        Student.lifecycle_status.notin_(_TERMINAL),
        build_search_filter(tokens, [Student.name, Student.student_id]),
    )
    if not unrestricted:
        scope = accessible_classroom_ids(session, current_user, code=code)
        if not scope:
            return []
        qy = qy.filter(Student.classroom_id.in_(scope))
    rows = qy.order_by(Student.name.asc()).limit(SECTION_LIMIT * 3).all()
    cr_map: dict[int, str] = {}
    cids = {r.classroom_id for r in rows if r.classroom_id}
    if cids:
        cr_map = {
            cid: name
            for cid, name in session.query(Classroom.id, Classroom.name)
            .filter(Classroom.id.in_(cids))
            .all()
        }
    items = [
        {
            "id": r.id,
            "name": r.name,
            "student_id": r.student_id,
            "classroom_name": cr_map.get(r.classroom_id, ""),
        }
        for r in rows
    ]
    return _finalize(items, nq, "name")


def _search_employees(session, tokens, nq) -> list[dict]:
    from models.database import Employee

    rows = (
        session.query(Employee)
        .filter(
            Employee.is_active.is_(True),
            build_search_filter(tokens, [Employee.name, Employee.employee_id]),
        )
        .order_by(Employee.name.asc())
        .limit(SECTION_LIMIT * 3)
        .all()
    )
    items = [
        {
            "id": e.id,
            "name": e.name,
            "employee_id": e.employee_id,
            "title": e.title or "",
        }
        for e in rows
    ]
    return _finalize(items, nq, "name")


def _search_guardians(session, tokens, nq, current_user: dict) -> list[dict]:
    code = Permission.GUARDIANS_READ.value
    unrestricted = is_row_unrestricted(current_user, code=code)
    qy = (
        session.query(Guardian, Student)
        .join(Student, Guardian.student_id == Student.id)
        .filter(
            Student.is_active.is_(True),
            Student.lifecycle_status.notin_(_TERMINAL),
            build_search_filter(tokens, [Guardian.name, Guardian.phone]),
        )
    )
    if not unrestricted:
        scope = accessible_classroom_ids(session, current_user, code=code)
        if not scope:
            return []
        qy = qy.filter(Student.classroom_id.in_(scope))
    rows = qy.order_by(Guardian.name.asc()).limit(SECTION_LIMIT * 3).all()
    items = [
        {
            "id": g.id,
            "name": g.name,
            "phone_masked": mask_phone(g.phone) or "",
            "child_name": stu.name,
            "student_id": stu.id,
        }
        for g, stu in rows
    ]
    return _finalize(items, nq, "name")


def _search_classrooms(session, tokens, nq) -> list[dict]:
    rows = (
        session.query(Classroom)
        .filter(
            Classroom.is_active.is_(True),
            build_search_filter(tokens, [Classroom.name]),
        )
        .order_by(Classroom.school_year.desc(), Classroom.name.asc())
        .limit(SECTION_LIMIT * 3)
        .all()
    )
    items = [
        {
            "id": c.id,
            "name": c.name,
            "school_year": c.school_year,
            "semester": c.semester,
        }
        for c in rows
    ]
    return _finalize(items, nq, "name")


def _search_fees(session, tokens, nq) -> list[dict]:
    rows = (
        session.query(StudentFeeRecord)
        .filter(
            build_search_filter(
                tokens,
                [StudentFeeRecord.student_name, StudentFeeRecord.fee_item_name],
            )
        )
        .order_by(StudentFeeRecord.id.desc())
        .limit(SECTION_LIMIT * 3)
        .all()
    )
    items = [
        {
            "record_id": r.id,
            "student_name": r.student_name,
            "classroom_name": r.classroom_name or "",
            "period": r.period or "",
            "status": r.status or "",
        }
        for r in rows
    ]
    return _finalize(items, nq, "student_name")


def _search_activity(session, tokens, nq, current_user: dict) -> list[dict]:
    # parent_phone 屬家長 PII：比照才藝列表 PII policy（_build_registration_filter_query
    # 依 GUARDIANS_READ 收斂手機 clause），缺 GUARDIANS_READ 者不得以部分手機號反查報名
    # 是否命中（側信道反查 student_name / class_name / match_status）。姓名/班級搜尋不受
    # 影響——僅移除手機 clause，非關閉整個才藝搜尋。
    cols = [ActivityRegistration.student_name, ActivityRegistration.class_name]
    if can_view_guardian_pii(current_user):
        cols.append(ActivityRegistration.parent_phone)
    rows = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.is_active.is_(True),
            build_search_filter(tokens, cols),
        )
        .order_by(ActivityRegistration.id.desc())
        .limit(SECTION_LIMIT * 3)
        .all()
    )
    items = [
        {
            "id": r.id,
            "student_name": r.student_name,
            "class_name": r.class_name or "",
            "match_status": r.match_status or "",
        }
        for r in rows
    ]
    return _finalize(items, nq, "student_name")


def _search_recruitment(session, tokens, nq) -> list[dict]:
    rows = (
        session.query(RecruitmentVisit)
        .filter(
            build_search_filter(
                tokens,
                [
                    RecruitmentVisit.child_name,
                    RecruitmentVisit.address,
                    RecruitmentVisit.notes,
                    RecruitmentVisit.parent_response,
                ],
            )
        )
        .order_by(RecruitmentVisit.id.desc())
        .limit(SECTION_LIMIT * 3)
        .all()
    )
    items = [
        {
            "id": r.id,
            "child_name": r.child_name,
            "target_school_year": r.target_school_year,
            "enrolled": bool(r.enrolled),
        }
        for r in rows
    ]
    return _finalize(items, nq, "child_name")


def _search_announcements(session, tokens, nq) -> list[dict]:
    rows = (
        session.query(Announcement)
        .filter(build_search_filter(tokens, [Announcement.title, Announcement.content]))
        .order_by(Announcement.created_at.desc())
        .limit(SECTION_LIMIT * 3)
        .all()
    )
    items = [
        {
            "id": a.id,
            "title": a.title,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]
    return _finalize(items, nq, "title")


# ── endpoint ───────────────────────────────────────────────────────────────────


@router.get("/search", response_model=GlobalSearchResult)
def global_search(
    request: Request,
    q: str = Query(..., min_length=0, max_length=100),
    current_user: dict = Depends(get_current_user),
):
    """後台全域搜尋。

    Returns:
        GlobalSearchResult 含 8 類 entity，各類 ≤ 8 筆。
        無對應 READ 權限的類別回空陣列。
    """
    role = current_user.get("role")
    if role in ("parent", "teacher"):
        raise HTTPException(status_code=403, detail="此搜尋僅供後台管理端使用")

    q_stripped = (q or "").strip()  # 保留供 audit summary
    nq = normalize_query(q)
    tokens = tokenize_query(q)
    if len(nq) < MIN_QUERY_LEN:
        return GlobalSearchResult(q=q)

    perms = current_user.get("permission_names")

    session = get_session()
    try:
        # 各類逐一以 READ 權限把關（無權回空）。scope 注意：只有 STUDENTS_READ /
        # GUARDIANS_READ 等「綁學生」的類別才有班級 scope 概念。STUDENTS_READ 是
        # scope-aware code，故 _search_students 會對 own_class 角色套 classroom 過濾。
        # 但 GUARDIANS_READ / FEES_READ / ACTIVITY_READ 不在 SCOPE_AWARE_CODES
        # （utils/permissions.SCOPE_AWARE_CODES），故 has_permission 對 `<code>:own_class`
        # 持有者回 False（RA-HIGH-1 fail-closed）→ 這些 gate 等同只放行 all-scope/wildcard。
        # 結果：own_class 自訂角色拿不到家長/學費/才藝結果（fail-closed，無越權）；
        # _search_guardians 內的 scope 分支對現行權限名單為防禦性死碼（GUARDIANS_READ
        # 日後若改成 scope-aware 即生效）。
        students = (
            _search_students(session, tokens, nq, current_user)
            if has_permission(perms, Permission.STUDENTS_READ)
            else []
        )
        employees = (
            _search_employees(session, tokens, nq)
            if has_permission(perms, Permission.EMPLOYEES_READ)
            else []
        )
        guardians = (
            _search_guardians(session, tokens, nq, current_user)
            if has_permission(perms, Permission.GUARDIANS_READ)
            else []
        )
        classrooms = (
            _search_classrooms(session, tokens, nq)
            if has_permission(perms, Permission.CLASSROOMS_READ)
            else []
        )
        fees = (
            _search_fees(session, tokens, nq)
            if has_permission(perms, Permission.FEES_READ)
            else []
        )
        activity_registrations = (
            _search_activity(session, tokens, nq, current_user)
            if has_permission(perms, Permission.ACTIVITY_READ)
            else []
        )
        recruitment = (
            _search_recruitment(session, tokens, nq)
            if has_permission(perms, Permission.RECRUITMENT_READ)
            else []
        )
        announcements = (
            _search_announcements(session, tokens, nq)
            if has_permission(perms, Permission.ANNOUNCEMENTS_READ)
            else []
        )

        result = GlobalSearchResult(
            q=q,
            students=students,
            employees=employees,
            guardians=guardians,
            classrooms=classrooms,
            fees=fees,
            activity_registrations=activity_registrations,
            recruitment=recruitment,
            announcements=announcements,
        )
        write_explicit_audit(
            request,
            action="READ",
            entity_type="admin_global_search",
            summary=f"後台全域搜尋（q={q_stripped[:32]}）",
            changes={
                "q": q_stripped[:64],
                "result_counts": {
                    "students": len(students),
                    "employees": len(employees),
                    "guardians": len(guardians),
                    "classrooms": len(classrooms),
                    "fees": len(fees),
                    "activity_registrations": len(activity_registrations),
                    "recruitment": len(recruitment),
                    "announcements": len(announcements),
                },
            },
        )
        return result
    finally:
        session.close()

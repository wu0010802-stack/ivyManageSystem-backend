"""教師端跨功能快速搜尋 endpoint。

GET /api/portal/search?q=xxx → 一次回 5 個 entity 各 ≤ 5 筆。

權限：portal 路由級 `require_non_parent_role`（在 __init__.py aggregator 掛）。
回傳含跨班/跨人 PII（家長對話 snippet、聯絡簿原文、監護人姓名/遮罩電話），
依 codebase canon（commit 7a25d767/d998214e/b72a87bf 建立的「敏感 GET 必補
READ audit」原則）必須顯式寫 audit；AuditMiddleware 只審計寫操作。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_

from models.classroom import (
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
)
from models.database import (
    Classroom,
    Guardian,
    Student,
    StudentContactBookEntry,
    get_session,
)
from models.event import Announcement, AnnouncementRecipient
from models.parent_message import ParentMessage, ParentMessageThread
from utils.audit import write_explicit_audit
from utils.auth import get_current_user
from utils.masking import mask_phone
from utils.portfolio_access import is_unrestricted

from ._shared import _get_employee, _get_teacher_classroom_ids

logger = logging.getLogger(__name__)

router = APIRouter()

SECTION_LIMIT = 5
SNIPPET_MAX = 120
HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: Optional[str]) -> str:
    if not s:
        return ""
    return HTML_TAG_RE.sub("", s)


def _make_snippet(*parts: Optional[str]) -> str:
    """合併多段欄位 → strip HTML → 截 SNIPPET_MAX 字。"""
    joined = " ".join(_strip_html(p) for p in parts if p).strip()
    return joined[:SNIPPET_MAX]


@router.get("/search")
def portal_search(
    request: Request,
    q: str = Query(..., min_length=0, max_length=100),
    current_user: dict = Depends(get_current_user),
):
    """跨功能快速搜尋。

    Returns:
        {
          "q": str,
          "students": [...],
          "guardians": [...],
          "messages": [...],
          "contact_book": [...],
          "announcements": [...]
        }
    """
    empty_result = {
        "q": q,
        "students": [],
        "guardians": [],
        "messages": [],
        "contact_book": [],
        "announcements": [],
    }

    q_stripped = (q or "").strip()
    if len(q_stripped) < 2:
        return empty_result

    pattern = f"%{q_stripped}%"
    user_id = current_user.get("user_id")

    session = get_session()
    try:
        emp = _get_employee(session, current_user)

        if is_unrestricted(current_user):
            classroom_ids: Optional[list[int]] = None
        else:
            classroom_ids = _get_teacher_classroom_ids(session, emp.id)

        # ── students ───────────────────────────────────────────────
        student_query = session.query(Student).filter(
            Student.is_active == True,  # noqa: E712
            Student.lifecycle_status.notin_(
                [LIFECYCLE_GRADUATED, LIFECYCLE_WITHDRAWN, LIFECYCLE_TRANSFERRED]
            ),
            Student.name.ilike(pattern),
        )
        if classroom_ids is not None:
            student_query = student_query.filter(
                Student.classroom_id.in_(classroom_ids)
            )
        students = student_query.order_by(Student.name.asc()).limit(SECTION_LIMIT).all()

        student_classroom_map: dict[int, str] = {}
        if students:
            cr_rows = (
                session.query(Classroom.id, Classroom.name)
                .filter(
                    Classroom.id.in_(
                        {s.classroom_id for s in students if s.classroom_id}
                    )
                )
                .all()
            )
            student_classroom_map = {cid: name for cid, name in cr_rows}

        student_results = [
            {
                "id": s.id,
                "name": s.name,
                "classroom_name": student_classroom_map.get(s.classroom_id, ""),
                "parent_name": None,
            }
            for s in students
        ]
        if students:
            primary_rows = (
                session.query(Guardian)
                .filter(Guardian.student_id.in_([s.id for s in students]))
                .order_by(Guardian.is_primary.desc(), Guardian.id.asc())
                .all()
            )
            primary_by_student: dict[int, str] = {}
            for g in primary_rows:
                if g.student_id not in primary_by_student:
                    primary_by_student[g.student_id] = g.name
            for r, s in zip(student_results, students):
                r["parent_name"] = primary_by_student.get(s.id)

        # ── guardians ──────────────────────────────────────────────
        guardian_results: list[dict] = []
        if classroom_ids is None or classroom_ids:
            guardian_query = (
                session.query(Guardian, Student)
                .join(Student, Guardian.student_id == Student.id)
                .filter(
                    Student.is_active == True,  # noqa: E712
                    Student.lifecycle_status.notin_(
                        [
                            LIFECYCLE_GRADUATED,
                            LIFECYCLE_WITHDRAWN,
                            LIFECYCLE_TRANSFERRED,
                        ]
                    ),
                    or_(
                        Guardian.name.ilike(pattern),
                        Guardian.phone.ilike(pattern),
                    ),
                )
            )
            if classroom_ids is not None:
                guardian_query = guardian_query.filter(
                    Student.classroom_id.in_(classroom_ids)
                )
            guardian_rows = (
                guardian_query.order_by(Guardian.name.asc()).limit(SECTION_LIMIT).all()
            )
            guardian_results = [
                {
                    "id": g.id,
                    "name": g.name,
                    "phone_masked": mask_phone(g.phone) or "",
                    "child_name": s.name,
                    "student_id": s.id,
                }
                for g, s in guardian_rows
            ]

        # ── messages ───────────────────────────────────────────────
        # 第一階段：在 DB 直接 OR 兩個條件（student name 或 thread 內任一 body 含 q）
        # 用 EXISTS subquery 取代 candidate-then-filter，DB-level 上 LIMIT 比 Python 切片精準。
        body_exists_subq = (
            session.query(ParentMessage.id)
            .filter(
                ParentMessage.thread_id == ParentMessageThread.id,
                ParentMessage.deleted_at.is_(None),
                ParentMessage.body.ilike(pattern),
            )
            .exists()
        )
        matched_thread_rows = (
            session.query(ParentMessageThread, Student)
            .join(Student, ParentMessageThread.student_id == Student.id)
            .filter(
                ParentMessageThread.teacher_user_id == user_id,
                ParentMessageThread.deleted_at.is_(None),
                or_(Student.name.ilike(pattern), body_exists_subq),
            )
            .order_by(
                ParentMessageThread.last_message_at.is_(None).asc(),
                ParentMessageThread.last_message_at.desc(),
            )
            .limit(SECTION_LIMIT)
            .all()
        )
        matched_threads = list(matched_thread_rows)
        message_results: list[dict] = []
        if matched_threads:
            thread_ids = [t.id for t, _ in matched_threads]
            latest_by_thread: dict[int, ParentMessage] = {}
            all_msgs = (
                session.query(ParentMessage)
                .filter(
                    ParentMessage.thread_id.in_(thread_ids),
                    ParentMessage.deleted_at.is_(None),
                )
                .order_by(ParentMessage.created_at.desc())
                .all()
            )
            for m in all_msgs:
                latest_by_thread.setdefault(m.thread_id, m)
            for t, s in matched_threads:
                latest = latest_by_thread.get(t.id)
                message_results.append(
                    {
                        "thread_id": t.id,
                        "student_name": s.name,
                        "snippet": _make_snippet(latest.body if latest else None),
                        "last_message_at": (
                            t.last_message_at.isoformat() if t.last_message_at else None
                        ),
                    }
                )

        # ── contact_book ───────────────────────────────────────────
        contact_book_results: list[dict] = []
        if classroom_ids is None or classroom_ids:
            cb_query = (
                session.query(StudentContactBookEntry, Student, Classroom)
                .join(Student, StudentContactBookEntry.student_id == Student.id)
                .join(Classroom, StudentContactBookEntry.classroom_id == Classroom.id)
                .filter(
                    StudentContactBookEntry.deleted_at.is_(None),
                    or_(
                        StudentContactBookEntry.teacher_note.ilike(pattern),
                        StudentContactBookEntry.learning_highlight.ilike(pattern),
                    ),
                )
            )
            if classroom_ids is not None:
                cb_query = cb_query.filter(
                    StudentContactBookEntry.classroom_id.in_(classroom_ids)
                )
            cb_rows = (
                cb_query.order_by(StudentContactBookEntry.log_date.desc())
                .limit(SECTION_LIMIT)
                .all()
            )
            contact_book_results = [
                {
                    "entry_id": entry.id,
                    "log_date": entry.log_date.isoformat() if entry.log_date else None,
                    "snippet": _make_snippet(
                        entry.teacher_note, entry.learning_highlight
                    ),
                    "student_name": stu.name,
                    "classroom_name": cr.name,
                }
                for entry, stu, cr in cb_rows
            ]

        # ── announcements ──────────────────────────────────────────
        # 必須套 recipient 過濾，否則教師可用關鍵字探勘僅發給 HR/supervisor 的定向公告
        # （AnnouncementRecipient 空代表全員可見；同 pattern 已用於
        # api/portal/announcements.py:50-63）。
        no_recipients_subq = (
            ~session.query(AnnouncementRecipient)
            .filter(AnnouncementRecipient.announcement_id == Announcement.id)
            .exists()
        )
        targeted_to_me_subq = (
            session.query(AnnouncementRecipient)
            .filter(
                AnnouncementRecipient.announcement_id == Announcement.id,
                AnnouncementRecipient.employee_id == emp.id,
            )
            .exists()
        )
        ann_rows = (
            session.query(Announcement)
            .filter(
                no_recipients_subq | targeted_to_me_subq,
                or_(
                    Announcement.title.ilike(pattern),
                    Announcement.content.ilike(pattern),
                ),
            )
            .order_by(Announcement.created_at.desc())
            .limit(SECTION_LIMIT)
            .all()
        )
        announcement_results = [
            {
                "id": a.id,
                "title": a.title,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in ann_rows
        ]

        result = {
            "q": q,
            "students": student_results,
            "guardians": guardian_results,
            "messages": message_results,
            "contact_book": contact_book_results,
            "announcements": announcement_results,
        }
        # 顯式 audit：回傳跨班/跨人 PII，必留稽核軌跡（q 截 32 字避免 audit 表爆）
        write_explicit_audit(
            request,
            action="READ",
            entity_type="portal_search",
            summary=f"教師端跨功能搜尋（q={q_stripped[:32]}）",
            changes={
                "q": q_stripped[:64],
                "result_counts": {
                    "students": len(student_results),
                    "guardians": len(guardian_results),
                    "messages": len(message_results),
                    "contact_book": len(contact_book_results),
                    "announcements": len(announcement_results),
                },
            },
        )
        return result
    finally:
        session.close()

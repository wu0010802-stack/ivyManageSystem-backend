"""
api/bonus_preview.py — 節慶獎金影響預覽 API
"""

import calendar
import logging
from datetime import date
from utils.taipei_time import today_taipei
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_
from sqlalchemy.orm import joinedload

from models.base import session_scope
from models.database import Employee, Classroom
from services.student_enrollment import (
    count_students_active_on,
    classroom_student_count_map,
)
from services.salary.utils import get_bonus_distribution_month
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission
from utils.salary_access import has_full_salary_view

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["bonus-preview"])

# ---------------------------------------------------------------------------
# Service injection（遵循 main.py singleton 注入模式）
# ---------------------------------------------------------------------------

_salary_engine = None


def init_bonus_preview_services(salary_engine):
    global _salary_engine
    _salary_engine = salary_engine


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class BonusImpactRequest(BaseModel):
    operation: Literal["add", "remove", "transfer", "graduate"]
    classroom_id: Optional[int] = None
    source_classroom_id: Optional[int] = None
    student_count_change: int = Field(default=1, ge=1, le=100)


class TeacherImpact(BaseModel):
    employee_id: int
    name: str
    role: str
    # 金額欄位：對非 admin/hr caller 遮罩為 None（F-016）
    current_bonus: Optional[int] = None
    projected_bonus: Optional[int] = None
    change: Optional[int] = None
    current_enrollment: int
    projected_enrollment: int
    target_enrollment: int


class ClassroomImpact(BaseModel):
    classroom_id: int
    classroom_name: str
    grade_name: str
    teachers: list[TeacherImpact]


class SchoolWideImpact(BaseModel):
    employee_id: int
    name: str
    category: str
    # 金額欄位：對非 admin/hr caller 遮罩為 None（F-016）
    current_bonus: Optional[int] = None
    projected_bonus: Optional[int] = None
    change: Optional[int] = None


class BonusImpactResponse(BaseModel):
    is_festival_month: bool
    year: int
    month: int
    affected_classrooms: list[ClassroomImpact]
    school_wide_impact: list[SchoolWideImpact]


# ---------------------------------------------------------------------------
# 內部 helper：用指定的 count_map 計算所有員工獎金明細
# ---------------------------------------------------------------------------


def _compute_all_bonus(session, engine, year, month, cls_count_map, school_total):
    """用指定的 count_map / school_total 計算全員獎金，回傳 list[dict]。

    Audit B.P0.2：預載 assistant_to_classes / art_to_classes，避免
    _build_classroom_context_from_db 在每位帶班員工各跑一次 shared_classes
    反查（ctx 內傳給 calculate_festival_bonus_breakdown）。
    順帶 is_active=True 過濾，避免拉歷年舊班級（A.P1）。
    """
    from models.database import Employee, Classroom

    _, month_last_day = calendar.monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, month_last_day)

    all_active_classrooms = (
        session.query(Classroom)
        .options(joinedload(Classroom.grade))
        .filter(Classroom.is_active == True)  # noqa: E712
        .all()
    )
    classroom_map = {c.id: c for c in all_active_classrooms}
    assistant_to_classes_map: dict[int, list] = {}
    art_to_classes_map: dict[int, list] = {}
    for _c in all_active_classrooms:
        if _c.assistant_teacher_id:
            assistant_to_classes_map.setdefault(_c.assistant_teacher_id, []).append(_c)
        if _c.art_teacher_id:
            art_to_classes_map.setdefault(_c.art_teacher_id, []).append(_c)

    employees = (
        session.query(Employee)
        .options(joinedload(Employee.job_title_rel))
        .filter(
            or_(
                Employee.is_active == True,
                and_(
                    Employee.is_active == False,
                    Employee.resign_date >= month_start,
                    Employee.resign_date <= month_end,
                ),
            )
        )
        .all()
    )

    results = []
    for emp in employees:
        ctx = {
            "session": session,
            "employee": emp,
            "classroom": (
                classroom_map.get(emp.classroom_id) if emp.classroom_id else None
            ),
            "school_active_students": school_total,
            "classroom_count_map": cls_count_map,
            "assistant_to_classes_map": assistant_to_classes_map,
            "art_to_classes_map": art_to_classes_map,
        }
        bonus_data = engine.calculate_festival_bonus_breakdown(
            emp.id, year, month, _ctx=ctx
        )
        bonus_data["employee_id"] = emp.id
        bonus_data["classroom_id"] = emp.classroom_id
        results.append(bonus_data)

    return results


# ---------------------------------------------------------------------------
# POST /api/bonus-impact-preview
# ---------------------------------------------------------------------------


@router.post("/bonus-impact-preview", response_model=BonusImpactResponse)
def preview_bonus_impact(
    req: BonusImpactRequest,
    current_user: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """預覽學生異動對節慶獎金的影響（before/after diff）。

    F-016：對非 admin/hr caller 遮罩 current_bonus / projected_bonus / change
    金額（保留 employee_id / name / role / enrollment 等運維所需欄位）。
    """
    # F-016：非 admin/hr 不可看逐員獎金金額（即使持 STUDENTS_WRITE）
    can_view_amount = has_full_salary_view(current_user)
    from services.salary.engine import SalaryEngine as RuntimeSalaryEngine

    engine = (
        _salary_engine if _salary_engine else RuntimeSalaryEngine(load_from_db=True)
    )

    today = today_taipei()
    year, month = today.year, today.month
    is_festival = get_bonus_distribution_month(month)

    try:
        with session_scope() as session:
            _, month_last_day = calendar.monthrange(year, month)
            month_end = date(year, month, month_last_day)

            # 當前人數：有該月快照讀快照（L2）
            from services.salary.enrollment_snapshot import resolve_bonus_counts

            current_school_total, current_cls_map = resolve_bonus_counts(
                session, year, month
            )

            # 投影人數
            projected_cls_map = dict(current_cls_map)
            delta = req.student_count_change

            affected_classroom_ids = set()
            if req.operation == "add" and req.classroom_id:
                projected_cls_map[req.classroom_id] = (
                    projected_cls_map.get(req.classroom_id, 0) + delta
                )
                affected_classroom_ids.add(req.classroom_id)
            elif req.operation in ("remove", "graduate") and req.source_classroom_id:
                projected_cls_map[req.source_classroom_id] = max(
                    0, projected_cls_map.get(req.source_classroom_id, 0) - delta
                )
                affected_classroom_ids.add(req.source_classroom_id)
            elif req.operation == "transfer":
                if req.source_classroom_id:
                    projected_cls_map[req.source_classroom_id] = max(
                        0, projected_cls_map.get(req.source_classroom_id, 0) - delta
                    )
                    affected_classroom_ids.add(req.source_classroom_id)
                if req.classroom_id:
                    projected_cls_map[req.classroom_id] = (
                        projected_cls_map.get(req.classroom_id, 0) + delta
                    )
                    affected_classroom_ids.add(req.classroom_id)

            projected_school_total = current_school_total
            if req.operation == "add":
                projected_school_total += delta
            elif req.operation in ("remove", "graduate"):
                projected_school_total = max(0, projected_school_total - delta)
            # transfer 不改變全校總人數

            # 分別計算 before / after
            current_results = _compute_all_bonus(
                session, engine, year, month, current_cls_map, current_school_total
            )
            projected_results = _compute_all_bonus(
                session, engine, year, month, projected_cls_map, projected_school_total
            )

            # 建立 projected lookup
            proj_by_emp = {r["employee_id"]: r for r in projected_results}

            # 組裝受影響班級
            classroom_impacts = {}
            for cur in current_results:
                proj = proj_by_emp.get(cur["employee_id"], cur)
                cur_bonus = cur.get("festivalBonus", 0)
                proj_bonus = proj.get("festivalBonus", 0)
                emp_classroom_id = cur.get("classroom_id")

                if cur.get("category") != "帶班老師":
                    continue
                if emp_classroom_id not in affected_classroom_ids:
                    continue

                if emp_classroom_id not in classroom_impacts:
                    # 取得班級資訊
                    classroom_obj = (
                        session.query(Classroom)
                        .options(joinedload(Classroom.grade))
                        .get(emp_classroom_id)
                    )
                    classroom_impacts[emp_classroom_id] = ClassroomImpact(
                        classroom_id=emp_classroom_id,
                        classroom_name=classroom_obj.name if classroom_obj else "",
                        grade_name=(
                            classroom_obj.grade.name
                            if classroom_obj and classroom_obj.grade
                            else ""
                        ),
                        teachers=[],
                    )

                cur_enrollment = current_cls_map.get(emp_classroom_id, 0)
                proj_enrollment = projected_cls_map.get(emp_classroom_id, 0)

                classroom_impacts[emp_classroom_id].teachers.append(
                    TeacherImpact(
                        employee_id=cur["employee_id"],
                        name=cur.get("name", ""),
                        role=cur.get("category", ""),
                        current_bonus=cur_bonus if can_view_amount else None,
                        projected_bonus=proj_bonus if can_view_amount else None,
                        change=(proj_bonus - cur_bonus) if can_view_amount else None,
                        current_enrollment=cur_enrollment,
                        projected_enrollment=proj_enrollment,
                        target_enrollment=cur.get("targetEnrollment", 0),
                    )
                )

            # 全校比例影響（主管 + 辦公室）
            school_wide_impact = []
            for cur in current_results:
                proj = proj_by_emp.get(cur["employee_id"], cur)
                cat = cur.get("category", "")
                if cat not in ("主管", "辦公室"):
                    continue
                cur_bonus = cur.get("festivalBonus", 0)
                proj_bonus = proj.get("festivalBonus", 0)
                if cur_bonus == proj_bonus:
                    continue
                school_wide_impact.append(
                    SchoolWideImpact(
                        employee_id=cur["employee_id"],
                        name=cur.get("name", ""),
                        category=cat,
                        current_bonus=cur_bonus if can_view_amount else None,
                        projected_bonus=proj_bonus if can_view_amount else None,
                        change=(proj_bonus - cur_bonus) if can_view_amount else None,
                    )
                )

        return BonusImpactResponse(
            is_festival_month=is_festival,
            year=year,
            month=month,
            affected_classrooms=list(classroom_impacts.values()),
            school_wide_impact=school_wide_impact,
        )

    except Exception as e:
        logger.exception("獎金影響預覽計算失敗")
        raise_safe_500(e)

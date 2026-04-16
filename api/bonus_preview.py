"""
api/bonus_preview.py — 節慶獎金影響預覽 & 獎金達成儀表板 API
"""

import calendar
import logging
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
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
    current_bonus: int
    projected_bonus: int
    change: int
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
    current_bonus: int
    projected_bonus: int
    change: int


class BonusImpactResponse(BaseModel):
    is_festival_month: bool
    year: int
    month: int
    affected_classrooms: list[ClassroomImpact]
    school_wide_impact: list[SchoolWideImpact]


class DashboardTeacher(BaseModel):
    employee_id: int
    name: str
    role: str
    estimated_bonus: int
    base_amount: int


class DashboardClassroom(BaseModel):
    classroom_id: int
    classroom_name: str
    grade_name: str
    current_enrollment: int
    target_enrollment: int
    achievement_rate: float
    status: str  # "below" | "on_target" | "above"
    teachers: list[DashboardTeacher]


class DashboardSchoolWide(BaseModel):
    total_enrollment: int
    total_target: int
    achievement_rate: float
    estimated_total_bonus: int


class BonusDashboardResponse(BaseModel):
    year: int
    month: int
    is_festival_month: bool
    school_wide: DashboardSchoolWide
    classrooms: list[DashboardClassroom]


# ---------------------------------------------------------------------------
# 內部 helper：用指定的 count_map 計算所有員工獎金明細
# ---------------------------------------------------------------------------


def _compute_all_bonus(session, engine, year, month, cls_count_map, school_total):
    """用指定的 count_map / school_total 計算全員獎金，回傳 list[dict]。"""
    from models.database import Employee, Classroom

    _, month_last_day = calendar.monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, month_last_day)

    classroom_map = {
        c.id: c
        for c in session.query(Classroom).options(joinedload(Classroom.grade)).all()
    }

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
    _: dict = Depends(require_staff_permission(Permission.STUDENTS_WRITE)),
):
    """預覽學生異動對節慶獎金的影響（before/after diff）。"""
    from services.salary.engine import SalaryEngine as RuntimeSalaryEngine

    engine = (
        _salary_engine if _salary_engine else RuntimeSalaryEngine(load_from_db=True)
    )

    today = date.today()
    year, month = today.year, today.month
    is_festival = get_bonus_distribution_month(month)

    try:
        with session_scope() as session:
            _, month_last_day = calendar.monthrange(year, month)
            month_end = date(year, month, month_last_day)

            # 當前人數
            current_cls_map = classroom_student_count_map(session, month_end)
            current_school_total = count_students_active_on(session, month_end)

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
                        current_bonus=cur_bonus,
                        projected_bonus=proj_bonus,
                        change=proj_bonus - cur_bonus,
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
                        current_bonus=cur_bonus,
                        projected_bonus=proj_bonus,
                        change=proj_bonus - cur_bonus,
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


# ---------------------------------------------------------------------------
# GET /api/bonus-preview/dashboard
# ---------------------------------------------------------------------------


@router.get("/bonus-preview/dashboard", response_model=BonusDashboardResponse)
def get_bonus_dashboard(
    _: dict = Depends(require_staff_permission(Permission.STUDENTS_READ)),
    year: Optional[int] = Query(None, ge=2000, le=2100),
    month: Optional[int] = Query(None, ge=1, le=12),
):
    """獎金達成儀表板：各班在籍 vs 目標、達成率、預估獎金。"""
    from services.salary.engine import SalaryEngine as RuntimeSalaryEngine

    engine = (
        _salary_engine if _salary_engine else RuntimeSalaryEngine(load_from_db=True)
    )

    today = date.today()
    if year is None:
        year = today.year
    if month is None:
        month = today.month

    is_festival = get_bonus_distribution_month(month)

    try:
        with session_scope() as session:
            _, month_last_day = calendar.monthrange(year, month)
            month_end = date(year, month, month_last_day)

            cls_count_map = classroom_student_count_map(session, month_end)
            school_total = count_students_active_on(session, month_end)

            all_bonus = _compute_all_bonus(
                session, engine, year, month, cls_count_map, school_total
            )

            # 按班級分組帶班老師
            classroom_teachers: dict[int, list[dict]] = {}
            total_bonus = 0
            school_wide_target = getattr(engine, "_school_wide_target", None) or 160

            for item in all_bonus:
                total_bonus += item.get("festivalBonus", 0)
                cat = item.get("category", "")
                cid = item.get("classroom_id")
                if cat == "帶班老師" and cid:
                    classroom_teachers.setdefault(cid, []).append(item)

            # 建構班級列表
            classrooms_data = []
            all_classrooms = (
                session.query(Classroom)
                .options(joinedload(Classroom.grade))
                .filter(Classroom.is_active == True)
                .all()
            )

            for classroom in all_classrooms:
                enrollment = cls_count_map.get(classroom.id, 0)
                teachers_in_class = classroom_teachers.get(classroom.id, [])
                if not teachers_in_class and enrollment == 0:
                    continue

                # 從教師獎金結果取得 target（各教師可能 target 不同，取班導的）
                target = 0
                for t in teachers_in_class:
                    t_target = t.get("targetEnrollment", 0)
                    if t_target > target:
                        target = t_target

                rate = enrollment / target if target > 0 else 0
                if rate >= 1.0:
                    status = "above"
                elif rate >= 0.8:
                    status = "on_target"
                else:
                    status = "below"

                dashboard_teachers = []
                for t in teachers_in_class:
                    # 判斷角色顯示名稱
                    remark = t.get("remark", "")
                    if "主管" in t.get("category", ""):
                        role_label = "主管"
                    elif "兩班平均" in remark:
                        role_label = "共用副班導"
                    else:
                        # 從 bonusBase 推斷
                        base = t.get("bonusBase", 0)
                        role_label = "班導" if base >= 1500 else "副班導"

                    dashboard_teachers.append(
                        DashboardTeacher(
                            employee_id=t["employee_id"],
                            name=t.get("name", ""),
                            role=role_label,
                            estimated_bonus=t.get("festivalBonus", 0),
                            base_amount=t.get("bonusBase", 0),
                        )
                    )

                classrooms_data.append(
                    DashboardClassroom(
                        classroom_id=classroom.id,
                        classroom_name=classroom.name,
                        grade_name=classroom.grade.name if classroom.grade else "",
                        current_enrollment=enrollment,
                        target_enrollment=target,
                        achievement_rate=round(rate, 4),
                        status=status,
                        teachers=dashboard_teachers,
                    )
                )

            school_rate = (
                school_total / school_wide_target if school_wide_target > 0 else 0
            )

        return BonusDashboardResponse(
            year=year,
            month=month,
            is_festival_month=is_festival,
            school_wide=DashboardSchoolWide(
                total_enrollment=school_total,
                total_target=school_wide_target,
                achievement_rate=round(school_rate, 4),
                estimated_total_bonus=total_bonus,
            ),
            classrooms=classrooms_data,
        )

    except Exception as e:
        logger.exception("獎金達成儀表板查詢失敗")
        raise_safe_500(e)

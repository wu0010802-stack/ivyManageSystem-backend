"""
api/student_enrollment.py — 幼生在籍統計 API endpoints
"""

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, case
from sqlalchemy.orm import aliased

from models.base import session_scope
from models.classroom import Classroom, ClassGrade, Student
from models.database import Employee
from utils.auth import require_permission
from utils.permissions import Permission
from utils.academic import resolve_academic_term_filters

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["student-enrollment"])


# ---------------------------------------------------------------------------
# Response Schemas
# ---------------------------------------------------------------------------

class ClassStats(BaseModel):
    class_name: str
    total: int
    male: int
    female: int


class GradeStats(BaseModel):
    grade_name: str
    total: int
    male: int
    female: int
    classes: list[ClassStats]


class EnrollmentSummary(BaseModel):
    total: int
    male: int
    female: int
    class_count: int


class EnrollmentStatsResponse(BaseModel):
    school_year: int
    semester: int
    semester_label: str
    summary: EnrollmentSummary
    by_grade: list[GradeStats]


class TermOption(BaseModel):
    school_year: int
    semester: int
    label: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/student-enrollment/stats", response_model=EnrollmentStatsResponse)
def get_enrollment_stats(
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None),
    _: None = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """取得指定學年學期的在籍學生統計，未提供則自動使用當前學期。"""
    school_year, semester = resolve_academic_term_filters(school_year, semester)

    with session_scope() as session:
        rows = (
            session.query(
                Classroom.id,
                Classroom.name.label("class_name"),
                ClassGrade.name.label("grade_name"),
                ClassGrade.sort_order,
                func.count(Student.id).label("total"),
                func.sum(case((Student.gender == "男", 1), else_=0)).label("male"),
                func.sum(case((Student.gender == "女", 1), else_=0)).label("female"),
            )
            .join(ClassGrade, Classroom.grade_id == ClassGrade.id)
            .outerjoin(
                Student,
                (Student.classroom_id == Classroom.id) & (Student.is_active.is_(True))
            )
            .filter(
                Classroom.school_year == school_year,
                Classroom.semester == semester,
                Classroom.is_active.is_(True),
            )
            .group_by(Classroom.id, Classroom.name, ClassGrade.name, ClassGrade.sort_order)
            .order_by(ClassGrade.sort_order, Classroom.name)
            .all()
        )

        # 按年級分組
        grade_map: dict[str, dict] = {}
        grade_order: list[str] = []
        for row in rows:
            gname = row.grade_name
            if gname not in grade_map:
                grade_map[gname] = {
                    "grade_name": gname,
                    "total": 0,
                    "male": 0,
                    "female": 0,
                    "classes": [],
                }
                grade_order.append(gname)
            male = int(row.male or 0)
            female = int(row.female or 0)
            total = int(row.total or 0)
            grade_map[gname]["total"] += total
            grade_map[gname]["male"] += male
            grade_map[gname]["female"] += female
            grade_map[gname]["classes"].append(
                ClassStats(class_name=row.class_name, total=total, male=male, female=female)
            )

        by_grade = [GradeStats(**grade_map[g]) for g in grade_order]

        grand_total = sum(g.total for g in by_grade)
        grand_male = sum(g.male for g in by_grade)
        grand_female = sum(g.female for g in by_grade)

        return EnrollmentStatsResponse(
            school_year=school_year,
            semester=semester,
            semester_label="上學期" if semester == 1 else "下學期",
            summary=EnrollmentSummary(
                total=grand_total,
                male=grand_male,
                female=grand_female,
                class_count=len(rows),
            ),
            by_grade=by_grade,
        )


# ---------------------------------------------------------------------------
# Roster Schemas
# ---------------------------------------------------------------------------

class RosterStudent(BaseModel):
    seq: int
    name: str
    status_tag: Optional[str]


class ClassRosterData(BaseModel):
    class_number: int
    classroom_id: int
    class_name: str
    grade_name: str
    head_teacher_name: Optional[str]
    assistant_teacher_name: Optional[str]
    art_teacher_name: Optional[str]
    students: list[RosterStudent]
    total: int
    new_count: int
    old_count: int


class GradeRosterSummary(BaseModel):
    grade_name: str
    class_numbers: list[int]
    total: int
    new_count: int
    old_count: int


class StaffEntry(BaseModel):
    name: str


class RosterResponse(BaseModel):
    school_year: int
    semester: int
    semester_label: str
    generated_date: str
    classes: list[ClassRosterData]
    grade_summaries: list[GradeRosterSummary]
    grand_total: int
    new_grand_total: int
    old_grand_total: int
    staff_by_role: dict[str, list[StaffEntry]]


# ---------------------------------------------------------------------------
# Roster Endpoint
# ---------------------------------------------------------------------------

@router.get("/student-enrollment/roster", response_model=RosterResponse)
def get_enrollment_roster(
    school_year: Optional[int] = Query(None),
    semester: Optional[int] = Query(None),
    _: None = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """取得花名冊格式的在籍記錄，含每班學生姓名、教師、員工名單。"""
    school_year, semester = resolve_academic_term_filters(school_year, semester)

    # ROC 民國日期字串，例如 "1150402"
    today = date.today()
    roc_year = today.year - 1911
    generated_date = f"{roc_year}{today.month:02d}{today.day:02d}"

    HeadTeacher = aliased(Employee)
    AssistantTeacher = aliased(Employee)
    ArtTeacher = aliased(Employee)

    with session_scope() as session:
        # ── 查詢班級與教師 ──────────────────────────────────────────────
        classroom_rows = (
            session.query(
                Classroom.id.label("classroom_id"),
                Classroom.name.label("class_name"),
                ClassGrade.name.label("grade_name"),
                ClassGrade.sort_order,
                HeadTeacher.name.label("head_teacher_name"),
                AssistantTeacher.name.label("assistant_teacher_name"),
                ArtTeacher.name.label("art_teacher_name"),
            )
            .join(ClassGrade, Classroom.grade_id == ClassGrade.id)
            .outerjoin(HeadTeacher, Classroom.head_teacher_id == HeadTeacher.id)
            .outerjoin(AssistantTeacher, Classroom.assistant_teacher_id == AssistantTeacher.id)
            .outerjoin(ArtTeacher, Classroom.art_teacher_id == ArtTeacher.id)
            .filter(
                Classroom.school_year == school_year,
                Classroom.semester == semester,
                Classroom.is_active.is_(True),
            )
            .order_by(ClassGrade.sort_order, Classroom.name)
            .all()
        )

        # ── 查詢各班學生 ────────────────────────────────────────────────
        classroom_ids = [r.classroom_id for r in classroom_rows]
        student_rows = (
            session.query(
                Student.classroom_id,
                Student.name,
                Student.status_tag,
            )
            .filter(
                Student.classroom_id.in_(classroom_ids),
                Student.is_active.is_(True),
            )
            .order_by(Student.classroom_id, Student.id)
            .all()
        ) if classroom_ids else []

        # 按班級分組學生
        students_by_class: dict[int, list] = {cid: [] for cid in classroom_ids}
        for s in student_rows:
            students_by_class[s.classroom_id].append(s)

        # ── 組裝班級花名冊 ──────────────────────────────────────────────
        classes: list[ClassRosterData] = []
        grade_map: dict[str, dict] = {}
        grade_order: list[str] = []

        for idx, row in enumerate(classroom_rows, start=1):
            cid = row.classroom_id
            stu_list = students_by_class.get(cid, [])
            roster_students = [
                RosterStudent(seq=i + 1, name=s.name, status_tag=s.status_tag)
                for i, s in enumerate(stu_list)
            ]
            new_count = sum(1 for s in stu_list if s.status_tag == "新生")
            old_count = len(stu_list) - new_count

            classes.append(ClassRosterData(
                class_number=idx,
                classroom_id=cid,
                class_name=row.class_name,
                grade_name=row.grade_name,
                head_teacher_name=row.head_teacher_name,
                assistant_teacher_name=row.assistant_teacher_name,
                art_teacher_name=row.art_teacher_name,
                students=roster_students,
                total=len(stu_list),
                new_count=new_count,
                old_count=old_count,
            ))

            gname = row.grade_name
            if gname not in grade_map:
                grade_map[gname] = {"grade_name": gname, "class_numbers": [], "total": 0, "new_count": 0, "old_count": 0}
                grade_order.append(gname)
            grade_map[gname]["class_numbers"].append(idx)
            grade_map[gname]["total"] += len(stu_list)
            grade_map[gname]["new_count"] += new_count
            grade_map[gname]["old_count"] += old_count

        grade_summaries = [GradeRosterSummary(**grade_map[g]) for g in grade_order]
        grand_total = sum(c.total for c in classes)
        new_grand_total = sum(c.new_count for c in classes)
        old_grand_total = sum(c.old_count for c in classes)

        # ── 員工依職稱分組 ──────────────────────────────────────────────
        from models.database import JobTitle
        emp_rows = (
            session.query(Employee.name, JobTitle.name.label("job_title"))
            .outerjoin(JobTitle, Employee.job_title_id == JobTitle.id)
            .filter(Employee.is_active.is_(True))
            .order_by(JobTitle.sort_order, Employee.id)
            .all()
        )

        staff_by_role: dict[str, list[StaffEntry]] = {}
        for emp in emp_rows:
            role = emp.job_title or "其他"
            if role not in staff_by_role:
                staff_by_role[role] = []
            staff_by_role[role].append(StaffEntry(name=emp.name))

        return RosterResponse(
            school_year=school_year,
            semester=semester,
            semester_label="上學期" if semester == 1 else "下學期",
            generated_date=generated_date,
            classes=classes,
            grade_summaries=grade_summaries,
            grand_total=grand_total,
            new_grand_total=new_grand_total,
            old_grand_total=old_grand_total,
            staff_by_role=staff_by_role,
        )


@router.get("/student-enrollment/options", response_model=list[TermOption])
def get_enrollment_options(
    _: None = Depends(require_permission(Permission.STUDENTS_READ)),
):
    """取得所有可用的學年/學期組合（用於前端篩選下拉）。"""
    with session_scope() as session:
        rows = (
            session.query(Classroom.school_year, Classroom.semester)
            .filter(Classroom.is_active.is_(True))
            .distinct()
            .order_by(Classroom.school_year.desc(), Classroom.semester.desc())
            .all()
        )

        def _roc_display_year(western: int) -> int:
            return western - 1911 if western > 1911 else western

        return [
            TermOption(
                school_year=r.school_year,
                semester=r.semester,
                label=f"{_roc_display_year(r.school_year)} {'上學期' if r.semester == 1 else '下學期'}",
            )
            for r in rows
        ]

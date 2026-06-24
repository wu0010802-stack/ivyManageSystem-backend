"""薪資頁學生人數展開區用 helper。

不改動 engine 與 student_enrollment.classroom_student_count_map 簽名；本檔僅讀資料組裝
給 records.py 列表 dict 使用。

批次化（2026-06-24，P1 N+1 修補）：列表頁原本逐筆呼叫 compute_enrollment_breakdown
（每員工 3-5 查詢），全園 ~200 員工單頁即 600-1000 次序列查詢。改提供
compute_enrollment_breakdowns 批次入口——一次撈相關 active 班級（joinedload grade）
+ 一次 GROUP BY 各班在籍人數，查詢數不隨員工數成長。單筆函式 compute_enrollment_breakdown
現委派批次（[employee_id]），語意完全不變、由既有 tests/test_salary_breakdown_enrollment.py
守護。
"""

import logging
from datetime import date
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from models.classroom import Classroom
from models.database import Student
from services.student_enrollment import student_active_on_filter
from utils.academic import resolve_current_academic_term

logger = logging.getLogger(__name__)


def _build_breakdown_for_employee(
    employee_id: int,
    target_date: date,
    school_year: int,
    semester: int,
    head_all: list[Classroom],
    assist_all: list[Classroom],
    count_by_classroom: dict[int, int],
) -> Optional[dict]:
    """從預載結構組裝單一員工的 breakdown（無 DB 存取）。

    head_all / assist_all 皆已 id 升冪排序、已過濾 is_active；count_by_classroom 為
    {classroom_id: 在籍人數}。term-first-then-fallback 與 multi_head/fallback warning
    行為與舊逐筆查詢版完全一致。
    """
    head_term = [
        c for c in head_all if c.school_year == school_year and c.semester == semester
    ]
    if head_term:
        head_classrooms = head_term
    else:
        head_classrooms = head_all
        if head_classrooms:
            logger.warning(
                "員工 %s 在 school_year=%s semester=%s 無對應 active head 班級；"
                "fallback 使用 classroom_id=%s",
                employee_id,
                school_year,
                semester,
                head_classrooms[0].id,
            )

    head_classroom = head_classrooms[0] if head_classrooms else None
    multi_head = len(head_classrooms) > 1
    if multi_head:
        logger.warning(
            "員工 %s 同時為多個 active 班級的 head_teacher（ids=%s）；breakdown "
            "僅顯示第一個並標 multi_head=True",
            employee_id,
            [c.id for c in head_classrooms],
        )

    enrollment = None
    if head_classroom is not None:
        total = count_by_classroom.get(head_classroom.id, 0)
        grade_name = head_classroom.grade.name if head_classroom.grade else None
        enrollment = {
            "snapshot_date": target_date.isoformat(),
            "total": total,
            "classroom_id": head_classroom.id,
            "classroom_name": head_classroom.name,
            "grade_name": grade_name,
            "multi_head": multi_head,
        }

    head_classroom_id = head_classroom.id if head_classroom else None
    assist_term = [
        c for c in assist_all if c.school_year == school_year and c.semester == semester
    ]
    assist_classrooms = assist_term if assist_term else assist_all
    assistant_names = [c.name for c in assist_classrooms if c.id != head_classroom_id]
    assistant = {"by_classroom": assistant_names} if assistant_names else None

    if enrollment is None and assistant is None:
        return None
    return {"enrollment": enrollment, "assistant": assistant}


def compute_enrollment_breakdowns(
    session: Session,
    employee_ids: list[int],
    target_date: date,
) -> dict[int, Optional[dict]]:
    """批次版：一次預載算出多名員工的 enrollment breakdown。

    回傳 {employee_id: breakdown_dict_or_None}，涵蓋所有輸入 id（去重）。
    查詢數固定（班級一次 + 各班人數一次），不隨員工數成長。語意與
    compute_enrollment_breakdown 逐筆呼叫完全一致。
    """
    ids = list(dict.fromkeys(employee_ids))  # 去重保序
    if not ids:
        return {}

    school_year, semester = resolve_current_academic_term(target_date)
    id_set = set(ids)

    # 一次撈出與這批員工相關的 active 班級（head / assistant / art），joinedload grade
    # 避免 head_classroom.grade.name 觸發 per-classroom lazy load。id 升冪對齊舊版
    # order_by(Classroom.id.asc())。
    classrooms = (
        session.query(Classroom)
        .options(joinedload(Classroom.grade))
        .filter(
            Classroom.is_active.is_(True),
            or_(
                Classroom.head_teacher_id.in_(ids),
                Classroom.assistant_teacher_id.in_(ids),
                Classroom.art_teacher_id.in_(ids),
            ),
        )
        .order_by(Classroom.id.asc())
        .all()
    )

    head_by_emp: dict[int, list[Classroom]] = {}
    assist_by_emp: dict[int, list[Classroom]] = {}
    head_cids: set[int] = set()
    for c in classrooms:
        if c.head_teacher_id in id_set:
            head_by_emp.setdefault(c.head_teacher_id, []).append(c)
            head_cids.add(c.id)
        # assistant 與 art 同一 row 只入該員工一次（set 去重）；對齊舊版
        # or_(assistant_teacher_id==emp, art_teacher_id==emp) 單列只回一次。
        assist_emps = set()
        if c.assistant_teacher_id in id_set:
            assist_emps.add(c.assistant_teacher_id)
        if c.art_teacher_id in id_set:
            assist_emps.add(c.art_teacher_id)
        for emp_id in assist_emps:
            assist_by_emp.setdefault(emp_id, []).append(c)

    # 各 head 班在籍人數一次 GROUP BY（語意同 count_students_active_on 的逐班查詢：
    # Student.classroom_id == cid + student_active_on_filter）。
    count_by_classroom: dict[int, int] = {}
    if head_cids:
        rows = (
            session.query(Student.classroom_id, func.count(Student.id))
            .filter(
                student_active_on_filter(target_date),
                Student.classroom_id.in_(head_cids),
            )
            .group_by(Student.classroom_id)
            .all()
        )
        count_by_classroom = {cid: int(cnt) for cid, cnt in rows}

    return {
        emp_id: _build_breakdown_for_employee(
            emp_id,
            target_date,
            school_year,
            semester,
            head_by_emp.get(emp_id, []),
            assist_by_emp.get(emp_id, []),
            count_by_classroom,
        )
        for emp_id in ids
    }


def compute_enrollment_breakdown(
    session: Session,
    employee_id: int,
    target_date: date,
) -> Optional[dict]:
    """Return enrollment + assistant breakdown for an employee at target_date.

    Returns None if employee teaches no active classroom (neither head nor
    assistant nor art). Otherwise the dict has shape:

        {
            "enrollment": { snapshot_date, total, classroom_id, classroom_name,
                            grade_name, multi_head } | None,
            "assistant":  { by_classroom: [str, ...] } | None,
        }

    班級反查：依 target_date 解析學年度/學期，先取「當期」班級；若該員工當期
    無對應 active 班級，fallback 至跨期任一 active 並 log warning（與
    services/salary/engine.py:_resolve_classroom_for_employee_in_term 同行為）。

    多頭班級：若同一 employee 同時為多個 active 班級的 head_teacher，回傳第一
    個（id 升冪），但在 enrollment dict 內補 multi_head=True 旗標讓前端揭露；
    並 log warning 方便管理員修資料。

    Note: 本單筆入口委派 compute_enrollment_breakdowns([employee_id])；批次呼叫端
    （例如薪資列表一次數百員工）請直接用 compute_enrollment_breakdowns 一次預載。
    """
    return compute_enrollment_breakdowns(session, [employee_id], target_date).get(
        employee_id
    )

"""Portfolio 模組共用存取檢查 helper。

職責：集中學生/班級存取權限檢查，避免每個 router 重寫相同邏輯。

規則：
- admin / hr / supervisor 不受班級限制
- teacher 只能存取自己擔任導師（head_teacher / assistant_teacher / art_teacher）的班級
- 未分班的學生（classroom_id = NULL）：非 admin 角色不能存取
"""

from __future__ import annotations

from typing import Iterable

from fastapi import HTTPException

from models.classroom import Classroom, Student

_UNRESTRICTED_ROLES = frozenset({"admin", "hr", "supervisor"})


def is_unrestricted(current_user: dict) -> bool:
    """管理角色不受班級限制。"""
    return current_user.get("role", "") in _UNRESTRICTED_ROLES


def accessible_classroom_ids(session, current_user: dict) -> list[int]:
    """回傳該 user 有權存取的班級 id 清單。

    管理角色回傳 [] 搭配 is_unrestricted() == True 表示「全放行」。
    teacher 回傳所擔任的班級 id 清單；若無任何班級則回傳空 list。
    """
    if is_unrestricted(current_user):
        return []
    emp_id = current_user.get("employee_id")
    if not emp_id:
        return []
    classrooms = (
        session.query(Classroom.id)
        .filter(
            (Classroom.head_teacher_id == emp_id)
            | (Classroom.assistant_teacher_id == emp_id)
            | (Classroom.art_teacher_id == emp_id)
        )
        .all()
    )
    return [c.id for c in classrooms]


def assert_student_access(session, current_user: dict, student_id: int) -> Student:
    """檢查 user 是否可存取該學生；不可則 403。回傳 Student 物件。

    - admin/hr/supervisor：一律放行
    - teacher：僅可存取自己班級的學生；未分班學生一律禁
    - 學生不存在：raise 404
    """
    student = session.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="學生不存在")
    if is_unrestricted(current_user):
        return student
    if not student.classroom_id:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    allowed = accessible_classroom_ids(session, current_user)
    if student.classroom_id not in allowed:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    return student


def filter_student_ids_by_access(
    session, current_user: dict, candidate_ids: Iterable[int]
) -> set[int]:
    """把一批 student_id 過濾掉該 user 無權存取的。用於 list 端點。"""
    if is_unrestricted(current_user):
        return set(candidate_ids)
    allowed_classrooms = accessible_classroom_ids(session, current_user)
    if not allowed_classrooms:
        return set()
    rows = (
        session.query(Student.id)
        .filter(
            Student.id.in_(list(candidate_ids)),
            Student.classroom_id.in_(allowed_classrooms),
        )
        .all()
    )
    return {r.id for r in rows}


def student_ids_in_scope(session, current_user: dict) -> list[int] | None:
    """回傳 user 所有可存取的 student_id 清單；管理角色回傳 None（表無限制）。

    用於彙總端點（例：今日用藥）的 WHERE student_id IN (...) 子句。
    """
    if is_unrestricted(current_user):
        return None
    allowed_classrooms = accessible_classroom_ids(session, current_user)
    if not allowed_classrooms:
        return []
    rows = (
        session.query(Student.id)
        .filter(Student.classroom_id.in_(allowed_classrooms))
        .all()
    )
    return [r.id for r in rows]

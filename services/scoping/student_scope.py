"""學生列級 scoping 篩選子句。

此模組為「教師只看自己班學生」邏輯的唯一實作來源（single source of truth）。
Admin router 透過 require_scoped_permission 取得 scope，再呼叫此模組；
Portal router 固定以 scope='own_class' 直接呼叫。

不在此模組處理 lifecycle_status 過濾（如只看在籍學生），
那是呼叫端的業務邏輯責任。
"""

from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.sql.elements import ColumnElement

from models.database import Classroom, Student


def filter_clause(user, scope: str) -> Optional[ColumnElement]:
    """回傳 SQLAlchemy WHERE 子句，依 scope 篩選學生查詢。

    Args:
        user: 必須具有 .employee_id (int | None) 屬性。
        scope: 'all' 表示無限制；'own_class' 表示只看教師所屬班級。

    Returns:
        scope='all' 時回傳 None（呼叫端應跳過 filter）。
        scope='own_class' 時回傳 Student.classroom_id IN (該教師班級 id 子查詢)。

    Raises:
        ValueError: scope='own_class' 但 user.employee_id 為 None。
        ValueError: scope 為不支援的值（含 'unknown scope' 關鍵字）。
    """
    if scope == "all":
        return None

    if scope == "own_class":
        emp_id = getattr(user, "employee_id", None)
        if emp_id is None:
            raise ValueError(
                "student_scope.filter_clause: scope='own_class' 需要 user.employee_id，"
                "但目前 employee_id 為 None"
            )
        return Student.classroom_id.in_(
            select(Classroom.id).where(
                or_(
                    Classroom.head_teacher_id == emp_id,
                    Classroom.assistant_teacher_id == emp_id,
                    Classroom.art_teacher_id == emp_id,
                )
            )
        )

    raise ValueError(
        f"student_scope.filter_clause: unknown scope {scope!r}；"
        f"支援的值為 'all' 或 'own_class'"
    )

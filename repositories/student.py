"""StudentRepository —— 學生資料存取層。"""

from __future__ import annotations

from typing import Optional

from models.classroom import Student
from repositories.base import BaseRepository


class StudentRepository(BaseRepository[Student]):
    model = Student

    def get_by_student_id(self, student_id: str) -> Optional[Student]:
        """以學號（business key）查詢。"""
        return (
            self.session.query(Student).filter(Student.student_id == student_id).first()
        )

    def list_active_by_classroom(self, classroom_id: int) -> list[Student]:
        return (
            self.session.query(Student)
            .filter(
                Student.classroom_id == classroom_id,
                Student.is_active == True,  # noqa: E712
            )
            .order_by(Student.id)
            .all()
        )

    def search(self, keyword: str, *, limit: int = 50) -> list[Student]:
        if not keyword:
            return []
        like = f"%{keyword}%"
        return (
            self.session.query(Student)
            .filter(
                (Student.name.ilike(like))
                | (Student.student_id.ilike(like))
                | (Student.parent_name.ilike(like))
            )
            .limit(limit)
            .all()
        )

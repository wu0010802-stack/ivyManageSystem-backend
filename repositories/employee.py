"""EmployeeRepository —— 員工資料存取層。"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import joinedload

from models.employee import Employee
from repositories.base import BaseRepository


class EmployeeRepository(BaseRepository[Employee]):
    model = Employee

    def get_with_job_title(self, employee_id: int) -> Optional[Employee]:
        """取得員工並預先載入職稱，避免後續存取 employee.job_title_rel 時觸發 N+1。"""
        return (
            self.session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter(Employee.id == employee_id)
            .first()
        )

    def get_by_employee_id(self, employee_id: str) -> Optional[Employee]:
        """以工號（business key）查詢。"""
        return (
            self.session.query(Employee)
            .filter(Employee.employee_id == employee_id)
            .first()
        )

    def list_active(self, *, classroom_id: Optional[int] = None) -> list[Employee]:
        q = (
            self.session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter(Employee.is_active == True)
        )  # noqa: E712
        if classroom_id is not None:
            q = q.filter(Employee.classroom_id == classroom_id)
        return q.order_by(Employee.id).all()

    def search(self, keyword: str, *, limit: int = 50) -> list[Employee]:
        """模糊搜尋員工姓名/工號（in-active 也回傳，由 caller 再篩）。"""
        if not keyword:
            return []
        like = f"%{keyword}%"
        return (
            self.session.query(Employee)
            .options(joinedload(Employee.job_title_rel))
            .filter((Employee.name.ilike(like)) | (Employee.employee_id.ilike(like)))
            .limit(limit)
            .all()
        )

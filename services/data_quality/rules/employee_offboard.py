"""rule: 員工 is_active=True 但 resign_date 已過。"""

from datetime import date

from sqlalchemy.orm import Session

from models.employee import Employee
from services.data_quality._base import Rule, Violation


class EmployeeOffboardRule(Rule):
    code = "employee_active_but_offboarded"
    severity = "P1"
    description = "員工已過離職日但 is_active 仍為 True"

    def check(self, session: Session) -> list[Violation]:
        today = date.today()
        rows = (
            session.query(Employee)
            .filter(
                Employee.is_active.is_(True),
                Employee.resign_date.isnot(None),
                Employee.resign_date <= today,
            )
            .all()
        )
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="employee",
                entity_id=str(r.id),
                summary=f"員工 #{r.id} 離職日 {r.resign_date} 已過，is_active 仍為 True",
            )
            for r in rows
        ]

"""rule: 學生 lifecycle_status 為終態但 is_active 仍 True。"""

from sqlalchemy.orm import Session

from models.classroom import (
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
    Student,
)
from services.data_quality._base import Rule, Violation

_TERMINAL_STATUSES = {
    LIFECYCLE_GRADUATED,
    LIFECYCLE_WITHDRAWN,
    LIFECYCLE_TRANSFERRED,
}


class StudentStaleActiveRule(Rule):
    code = "student_active_but_lifecycle_terminal"
    severity = "P1"
    description = "學生 lifecycle_status 為終態但 is_active 仍 True"

    def check(self, session: Session) -> list[Violation]:
        rows = (
            session.query(Student)
            .filter(
                Student.is_active.is_(True),
                Student.lifecycle_status.in_(_TERMINAL_STATUSES),
            )
            .all()
        )
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="student",
                entity_id=str(r.id),
                summary=f"學生 #{r.id} lifecycle={r.lifecycle_status} 但 is_active 仍 True",
            )
            for r in rows
        ]

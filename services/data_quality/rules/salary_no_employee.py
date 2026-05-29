"""rule: SalaryRecord.employee_id 指向不存在 employee。"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.data_quality._base import Rule, Violation


class SalaryOrphanRule(Rule):
    code = "salary_record_orphan_employee"
    severity = "P0"
    description = "SalaryRecord.employee_id 指向不存在的 employee"

    def check(self, session: Session) -> list[Violation]:
        rows = session.execute(text("""
                SELECT sr.id, sr.employee_id, sr.salary_year, sr.salary_month
                FROM salary_records sr
                LEFT JOIN employees e ON e.id = sr.employee_id
                WHERE e.id IS NULL
                """)).all()
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="salary_record",
                entity_id=str(row.id),
                summary=(
                    f"SalaryRecord #{row.id} "
                    f"({row.salary_year}-{row.salary_month}) "
                    f"指向不存在 employee #{row.employee_id}"
                ),
            )
            for row in rows
        ]

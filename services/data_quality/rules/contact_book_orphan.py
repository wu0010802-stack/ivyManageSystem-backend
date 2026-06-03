"""rule: ContactBookEntry.student_id 指向不存在 student。"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.data_quality._base import Rule, Violation


class ContactBookOrphanRule(Rule):
    code = "contact_book_orphan_student"
    severity = "P0"
    description = "ContactBookEntry.student_id 指向不存在的 student（FK 漏 cascade）"

    def check(self, session: Session) -> list[Violation]:
        rows = session.execute(text("""
                SELECT cb.id, cb.student_id
                FROM student_contact_book_entries cb
                LEFT JOIN students s ON s.id = cb.student_id
                WHERE s.id IS NULL
                """)).all()
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="contact_book_entry",
                entity_id=str(row.id),
                summary=f"ContactBookEntry #{row.id} 指向不存在 student #{row.student_id}",
            )
            for row in rows
        ]

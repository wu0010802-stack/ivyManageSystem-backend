"""rule: Guardian.user_id 指向不存在 user。"""

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.data_quality._base import Rule, Violation


class GuardianOrphanRule(Rule):
    code = "guardian_orphan_user"
    severity = "P0"
    description = "Guardian.user_id 指向不存在的 user"

    def check(self, session: Session) -> list[Violation]:
        rows = session.execute(text("""
                SELECT g.id, g.user_id
                FROM guardians g
                LEFT JOIN users u ON u.id = g.user_id
                WHERE g.user_id IS NOT NULL AND u.id IS NULL
                """)).all()
        return [
            Violation(
                rule_code=self.code,
                severity=self.severity,
                entity_type="guardian",
                entity_id=str(row.id),
                summary=f"Guardian #{row.id} 指向不存在 user #{row.user_id}",
            )
            for row in rows
        ]

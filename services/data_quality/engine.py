"""services/data_quality/engine.py — rule 跑批 orchestrator。"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from services.data_quality._base import Rule, Violation
from services.data_quality.rules.contact_book_orphan import ContactBookOrphanRule
from services.data_quality.rules.employee_offboard import EmployeeOffboardRule
from services.data_quality.rules.guardian_orphan_user import GuardianOrphanRule
from services.data_quality.rules.salary_no_employee import SalaryOrphanRule
from services.data_quality.rules.student_stale_active import StudentStaleActiveRule

logger = logging.getLogger(__name__)


ALL_RULES: list[Rule] = [
    EmployeeOffboardRule(),
    StudentStaleActiveRule(),
    ContactBookOrphanRule(),
    GuardianOrphanRule(),
    SalaryOrphanRule(),
]


def run_all_rules(session: Session) -> list[Violation]:
    """跑全部 ALL_RULES，回合併 list[Violation]。

    單條 rule 異常 swallow 並 log，不阻斷其他 rule（避免一條炸全部）。
    """
    out: list[Violation] = []
    for rule in ALL_RULES:
        try:
            out.extend(rule.check(session))
        except Exception:
            logger.exception("data_quality rule %s failed", rule.code)
    return out

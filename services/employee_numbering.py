"""services/employee_numbering.py — 員工工號自動配發。

格式：{民國到職年:03d}{當年流水:03d}，例 114001。到職即固定、與班級/職務無關。
舊手填工號（E001/ADMIN001 等）不符此格式者一律忽略，不影響流水。
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

_EMP_ID_RE = re.compile(r"^(\d{3})(\d{3,})$")
_LOCK_NS_EMPLOYEE = 1002  # 固定整數 advisory lock namespace（跨 process 穩定）


def next_employee_id(session: Session, hire_year_roc: int) -> str:
    """配發指定到職民國年的下一個工號。

    Postgres 以 pg_advisory_xact_lock 防並發撞號；SQLite 跳過。
    """
    from models.employee import Employee

    prefix = f"{hire_year_roc:03d}"
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, :year)"),
            {"ns": _LOCK_NS_EMPLOYEE, "year": int(hire_year_roc)},
        )

    rows = (
        session.query(Employee.employee_id)
        .filter(Employee.employee_id.like(f"{prefix}%"))
        .all()
    )
    max_seq = 0
    for (eid,) in rows:
        m = _EMP_ID_RE.match(eid or "")
        if m and m.group(1) == prefix:
            max_seq = max(max_seq, int(m.group(2)))
    return f"{prefix}{max_seq + 1:03d}"

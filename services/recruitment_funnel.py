"""services/recruitment_funnel.py — 招生漏斗狀態機 + 寫入入口。

純函式（derive_stage / can_transition / is_destructive）位於檔頭，
orchestrator `transition_visit()` 在後續 task 補。
"""

from __future__ import annotations

import re
from typing import Literal, Optional, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

Stage = Literal["visited", "deposited", "enrolled", "active"]
STAGES: tuple[Stage, ...] = ("visited", "deposited", "enrolled", "active")


class _VisitLike(Protocol):
    has_deposit: bool


class _StudentLike(Protocol):
    lifecycle_status: str


def derive_stage(visit: _VisitLike, student: Optional[_StudentLike]) -> Stage:
    """從 (visit, student) 推導 4 階段。

    規則：student 存在性優先（avoid dual source of truth）。
    """
    if student is not None:
        return "active" if student.lifecycle_status == "active" else "enrolled"
    return "deposited" if visit.has_deposit else "visited"


def can_transition(from_stage: Stage, to_stage: Stage) -> bool:
    """Phase A：任意拖。保留位置給未來收緊規則（例：禁止跨多段躍進）。"""
    return True


_DESTRUCTIVE_FROM: frozenset[Stage] = frozenset({"enrolled", "active"})


def is_destructive(from_stage: Stage, to_stage: Stage) -> bool:
    """destructive = 從 enrolled/active 退回任何前段。"""
    if from_stage not in _DESTRUCTIVE_FROM:
        return False
    order = {s: i for i, s in enumerate(STAGES)}
    return order[to_stage] < order[from_stage]


# ── 學號產生 ────────────────────────────────────────────────────────────────

_STUDENT_ID_RE = re.compile(r"^(\d{3})-([A-Za-z0-9_-]+)-(\d{2,})$")


def next_student_id_code(session: Session, school_year: int, class_code: str) -> str:
    """產 {year}-{class_code}-{NN}（NN 兩位數零填，同年同班遞增）。

    Postgres 上以 pg_advisory_xact_lock 防並發撞號（lock 範圍涵蓋整個 transaction，
    commit/rollback 時自動釋放）。SQLite/其他 dialect 無此 function — 跳過 lock；
    測試時用 in-memory SQLite 單連線本就無並發。
    """
    from models.classroom import Student  # 延遲 import 避免循環

    if session.bind is not None and session.bind.dialect.name == "postgresql":
        lock_key = hash((school_year, class_code)) % (2**31)
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": lock_key})

    prefix = f"{school_year}-{class_code}-"
    rows = (
        session.query(Student.student_id)
        .filter(Student.student_id.like(f"{prefix}%"))
        .all()
    )
    max_seq = 0
    for (sid,) in rows:
        m = _STUDENT_ID_RE.match(sid or "")
        if m and m.group(1) == str(school_year) and m.group(2) == class_code:
            max_seq = max(max_seq, int(m.group(3)))
    return f"{prefix}{max_seq + 1:02d}"

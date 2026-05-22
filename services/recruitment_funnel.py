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


# ── 還原守衛 ─────────────────────────────────────────────────────────────────


class RecruitmentFunnelError(ValueError):
    """Funnel 業務錯誤（caller catch → HTTP 400）。"""

    def __init__(self, message: str, code: str = "FUNNEL_ERROR"):
        super().__init__(message)
        self.code = code


# 下游業務白名單 — 任一存在則無法 revert convert
# 格式：(模組路徑, 類別名, FK 欄位名, 友善標籤)
# 注意：只列確認有 student_id 欄位的 model。
# StudentFeePayment / StudentFeeRefund 透過 record_id 間接關聯，不在此列。
# StudentMedicationLog 透過 order_id 間接關聯，不在此列。
# GuardianBindingCode 以 guardian_id 關聯，不在此列。
_REVERT_BLOCKERS: list[tuple[str, str, str, str]] = [
    ("models.classroom", "StudentAttendance", "student_id", "出席紀錄"),
    ("models.fees", "StudentFeeRecord", "student_id", "繳費資料"),
    ("models.classroom", "StudentAssessment", "student_id", "評量"),
    ("models.classroom", "StudentIncident", "student_id", "獎懲紀錄"),
    ("models.portfolio", "StudentObservation", "student_id", "觀察"),
    ("models.portfolio", "StudentAllergy", "student_id", "過敏資料"),
    ("models.portfolio", "StudentMedicationOrder", "student_id", "餵藥單"),
    ("models.portfolio", "StudentMeasurement", "student_id", "體溫/體重紀錄"),
    ("models.portfolio", "StudentMilestone", "student_id", "里程碑"),
]


def assert_student_revertable(session: Session, student_id: int) -> None:
    """檢查 student 是否有下游業務記錄；任一存在則 raise RecruitmentFunnelError。

    caller 應在 destructive revert 前呼叫此函式，防止業務資料孤兒化。
    """
    import importlib

    for module_path, class_name, fk_col, label in _REVERT_BLOCKERS:
        try:
            module = importlib.import_module(module_path)
            model = getattr(module, class_name, None)
        except ImportError:
            continue
        if model is None:
            continue
        column = getattr(model, fk_col, None)
        if column is None:
            continue
        exists = session.query(model).filter(column == student_id).limit(1).first()
        if exists is not None:
            raise RecruitmentFunnelError(
                f"該學生已有業務資料（{label}），請走退學流程而非退回 funnel",
                code="REVERT_STUDENT_HAS_DATA",
            )
